"""Clean-start workspace command services.

The CLI parses flags and renders results.  This module owns the storage-backed
workflow and keeps the old ledger command paths out of the new workspace.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from honeymoney import importers
from honeymoney.account_bindings import (
    AccountBinding,
    binding_by_id,
    validate_bindings_for_profiles,
    validate_profile_mappings,
)
from honeymoney.corrections import (
    CORRECTION_COLUMNS,
    load_corrections,
    prepare_corrections_document,
    to_review_row,
)
from honeymoney.csv_artifacts import csv_document
from honeymoney.identity import (
    IdentityError,
    IncomingSourceIdentity,
    extractor_contract_id,
    manifest_document,
    resolve_batch,
    resolve_sources,
    source_namespace_id,
    source_revision,
    workspace_record_fingerprint,
    workspace_source_identity,
    workspace_source_revision,
)
from honeymoney.identity_state import IdentityState
from honeymoney.import_records import (
    ATTEMPT_SCHEMA_VERSION,
    IMPORT_RECORD_SCHEMA_VERSION,
    TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
    attempt_document,
    build_summary,
    import_record_path,
    load_attempts,
    next_attempt_number,
    read_transaction_snapshot,
    safe_source_label,
    summary_document,
    transaction_snapshot_document,
)
from honeymoney.learning import plan_learned_rules
from honeymoney.manual_pairs import (
    MANUAL_PAIR_FIELD,
    ManualPairError,
    manual_pair_id,
    manual_pair_marker,
    validate_manual_pair_facts,
)
from honeymoney.overlap import (
    DuplicateResolution,
    list_duplicate_groups,
    resolve_duplicate_group,
)
from honeymoney.parser_contracts import Profile
from honeymoney.periods import PeriodSelection, view_period_for_row
from honeymoney.persistence import private_atomic_write_text
from honeymoney.rates import (
    load_rate_cache,
    merge_rate_cache,
    rate_cache_document,
)
from honeymoney.review_operations import accounting_decision_patch
from honeymoney.review_state import REVIEW_REASON_SOURCE_DATA, review_reason_tokens
from honeymoney.rules import MANAGED_RULE_MARKER, load_rules, validate_rules
from honeymoney.schema import SOURCE_OCCURRENCE_COLUMNS
from honeymoney.source_data_review import (
    SourceDataReviewError,
    inspect_source_data_review,
    source_data_review_active,
)
from honeymoney.workspace_derivation import (
    ViewReportInputs,
    WorkspaceDerivation,
    derive_workspace_rows,
    view_report_inputs,
)
from honeymoney.workspace_index import (
    HONEYMONEY_VERSION,
    InputProof,
    RegisteredView,
    WorkspaceIndex,
    derivation_contract_for_model,
    load_compatible_workspace_index,
    workspace_index_document,
)
from honeymoney.workspace_paths import (
    WorkspacePathError,
    WorkspacePaths,
    checked_workspace_path,
    reject_existing_symlink_components,
    reject_legacy_workspace,
)
from honeymoney.workspace_publication import (
    AttemptReservation,
    FixedAttemptReservation,
    PendingAttemptReservation,
    PublicationError,
    PublicationTarget,
    WorkspaceLock,
    publish_generation,
    publish_reserved_failures,
    publish_reserved_generation,
    reserve_publication,
)
from honeymoney.workspace_queries import WorkspaceQuery, query_workspace_rows
from honeymoney.workspace_views import WorkspaceViewPlan

SNAPSHOT_COLUMNS = (
    "source_record_id",
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "account_binding_id",
    "account_type",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "statement_opening_balance",
    "statement_closing_balance",
    "statement_section",
    "merchant",
    "original_description",
    "source_page",
    "source_row",
)


class WorkspaceCommandError(ValueError):
    """A privacy-safe public command failure."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _load_workspace_rules(config: dict[str, object]) -> list[dict[str, object]]:
    """Load rules without exposing user content in command failures."""
    try:
        rules: list[dict[str, object]] = load_rules(config)
        return rules
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace inputs are invalid."
        ) from error


def _load_workspace_corrections(
    config: dict[str, object],
) -> dict[str, dict[str, str]]:
    """Load corrections without exposing user content in command failures."""
    try:
        return load_corrections(config)
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace inputs are invalid."
        ) from error


def _load_workspace_profiles(config: dict[str, object]) -> list[Profile]:
    """Load profiles without exposing user content in command failures."""
    try:
        return importers._load_profiles(config)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace inputs are invalid."
        ) from error


def _load_workspace_profile_mappings(
    config: dict[str, object],
) -> dict[str, object]:
    """Load profile mappings without exposing user content in failures."""
    try:
        return importers._load_profile_mappings(config)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace inputs are invalid."
        ) from error


def _preflight_account_binding(
    profiles: list[Profile],
    mappings: dict[str, object],
    binding_id: str | None,
    input_path: Path,
) -> AccountBinding | None:
    """Check saved bindings without echoing their values on failure."""
    try:
        validate_bindings_for_profiles(mappings, profiles)
        if binding_id is None:
            return None
        binding = binding_by_id(mappings, binding_id)
        importers._explicit_binding_profile(input_path, profiles, binding)
        return binding
    except (TypeError, ValueError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace profiles or mappings are invalid."
        ) from error


@dataclass(frozen=True)
class WorkspaceContext:
    paths: WorkspacePaths
    config: dict[str, object]
    index: WorkspaceIndex


@dataclass(frozen=True)
class CommandResult:
    data: dict[str, object]
    artifacts: dict[str, object]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedSource:
    path: Path
    source_id: str
    source_label: str
    rows: tuple[dict[str, str], ...]
    identity: IncomingSourceIdentity
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _FailedSourceAttempt:
    source_id: str
    record: Path
    reservation: FixedAttemptReservation
    summary_content: bytes


@dataclass(frozen=True)
class _PlannedSourceAttempt:
    path: Path
    source_id: str
    record: Path
    attempt_number: int
    source_revision: str
    parser_contract: str
    reservation: PendingAttemptReservation


@dataclass(frozen=True)
class _PreflightedSource:
    path: Path
    source_id: str
    source_label: str
    identity: IncomingSourceIdentity
    snapshot: importers.InputSourceSnapshot
    selected_profile: Profile


def load_workspace(config_path: str | Path | None) -> WorkspaceContext:
    """Load one clean-start workspace without changing any bytes."""
    path = Path(config_path or "config.json").expanduser()
    try:
        paths = WorkspacePaths.from_config(path)
        _reject_managed_symlinks(paths)
        raw = json.loads(paths.config.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace config does not exist."
        ) from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace config is not valid JSON."
        ) from error
    if not isinstance(raw, dict):
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace config must be an object."
        )
    reject_legacy_workspace(paths, raw)
    config = _resolved_config(paths, raw)
    try:
        index = load_compatible_workspace_index(paths.workspace_index)
    except ValueError as error:
        code = (
            "newer_honeymoney_required"
            if getattr(error, "code", None) == "newer_honeymoney_required"
            else "workspace_index_invalid"
        )
        raise WorkspaceCommandError(code) from error
    if paths.journal.exists():
        raise WorkspaceCommandError(
            "publication_recovery_required",
            "Run `honeymoney doctor --fix` before another command.",
        )
    return WorkspaceContext(paths, config, index)


def import_workspace(
    source_path: str | Path,
    *,
    config_path: str | Path | None,
    action: str = "import",
    binding_id: str | None = None,
    interactive: bool = False,
    strict: bool = False,
) -> CommandResult:
    """Import one file or folder and publish one clean workspace generation."""
    if action not in {"import", "replace", "reset"}:
        raise WorkspaceCommandError("workspace_input_invalid")
    context = load_workspace(config_path)
    _require_current_input_proofs(context)
    requested_input_path = Path(source_path).expanduser()
    input_path = _checked_import_path(requested_input_path)
    files = importers._discover_input_files(input_path)
    if any(path.is_symlink() for path in files):
        raise WorkspaceCommandError(
            "managed_path_unsafe", "Import files must not be symbolic links."
        )
    if not files:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Import path has no supported files."
        )
    _prove_import_files_readable(files)
    if binding_id is not None and not input_path.is_file():
        raise WorkspaceCommandError(
            "workspace_input_invalid", "--binding requires one CSV or PDF file."
        )
    profiles = _load_workspace_profiles(context.config)
    mappings = _load_workspace_profile_mappings(context.config)
    binding = _preflight_account_binding(profiles, mappings, binding_id, input_path)

    input_root = input_path if input_path.is_dir() else input_path.parent
    _reject_successful_plain_repeats(context, files, action)
    with WorkspaceLock(context.paths.root):
        # A waiting owner must repeat all checks before it allocates attempt numbers.
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        input_path = _checked_import_path(requested_input_path)
        files = importers._discover_input_files(input_path)
        if any(path.is_symlink() for path in files):
            raise WorkspaceCommandError(
                "managed_path_unsafe", "Import files must not be symbolic links."
            )
        if not files:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Import path has no supported files."
            )
        _prove_import_files_readable(files)
        profiles = _load_workspace_profiles(context.config)
        mappings = _load_workspace_profile_mappings(context.config)
        if binding_id is not None:
            if not input_path.is_file():
                raise WorkspaceCommandError(
                    "workspace_input_invalid",
                    "--binding requires one CSV or PDF file.",
                )
        binding = _preflight_account_binding(profiles, mappings, binding_id, input_path)
        input_root = input_path if input_path.is_dir() else input_path.parent
        _reject_successful_plain_repeats(context, files, action)
        if interactive:
            print(
                "Interactive imports do not save profile mappings. "
                "Use `honeymoney profile bind` to save a profile choice."
            )
        preflighted_sources = _preflight_source_assignments(
            context,
            files,
            input_root,
            profiles,
            mappings,
            binding,
            interactive,
            action=action,
        )
        started = _timestamp()
        generation_id = f"gen_{secrets.token_hex(32)}"
        planned_attempts = _plan_source_attempts(
            context,
            preflighted_sources,
            action=action,
            started=started,
        )
        reserve_publication(
            context.paths.root,
            generation_id,
            [item.reservation for item in planned_attempts],
        )
        parsed: list[_ParsedSource] = []
        failures: list[tuple[Path, Exception]] = []
        for preflighted_source in preflighted_sources:
            try:
                parsed.append(
                    _parse_source(
                        context,
                        preflighted_source,
                        input_root,
                        profiles,
                        mappings,
                        binding,
                        interactive,
                    )
                )
            except Exception as error:
                failures.append((preflighted_source.path, error))

        failed_attempts = _prepare_failed_source_attempts(
            context,
            failures,
            planned_attempts,
            preflighted_sources,
            action=action,
            started=started,
        )
        if not parsed:
            _publish_fixed_import_attempts(context, generation_id, failed_attempts)
            raise WorkspaceCommandError("import_failed", "No source completed parsing.")

        try:
            prior_rows = _load_ready_source_rows(context)
            resolution = resolve_batch(
                ledger_rows=prior_rows,
                manifest=context.index["identity_manifest"],
                sources=tuple(item.identity for item in parsed),
                intent=action,
                allow_parser_upgrade_reallocation=action in {"replace", "reset"},
                evidence_key=_content_proof_key(context),
            )

            labels_by_source_id = {item.source_id: item.source_label for item in parsed}
            resolved_rows: list[dict[str, str]] = []
            for resolved_row in resolution.resolved_rows:
                row = dict(resolved_row)
                source_id = row.get("source_id")
                if (
                    not isinstance(source_id, str)
                    or source_id not in labels_by_source_id
                ):
                    raise WorkspaceCommandError("durable_state_conflict")
                row["source_file"] = labels_by_source_id[source_id]
                resolved_rows.append(row)
            current_rows = [
                *(dict(row) for row in resolution.retained_ledger_rows),
                *resolved_rows,
            ]
            corrections = _load_workspace_corrections(context.config)
            rules = _load_workspace_rules(context.config)
            prior_derivation = derive_workspace_rows(
                prior_rows,
                context.index["overlap_manifest"],
                context.config,
                rules=rules,
                corrections=corrections,
                profile_mappings=mappings,
                allow_model=_model_enabled(context.config),
            )
            correction_content: bytes | None = None
            if action == "reset":
                corrections, correction_content = _reset_supported_corrections(
                    corrections,
                    context.index["overlap_manifest"],
                    set(resolution.replaced_source_ids),
                    set(resolution.reset_transaction_ids),
                )
            derivation = derive_workspace_rows(
                current_rows,
                context.index["overlap_manifest"],
                context.config,
                rules=rules,
                corrections=corrections,
                profile_mappings=mappings,
                allow_model=_model_enabled(context.config),
            )
            # Strict mode changes the command exit after a safe commit; review state
            # and parser warnings never turn a valid source snapshot into a failure.
            _ = strict

            next_index = copy.deepcopy(context.index)
            next_index["generation_id"] = generation_id
            next_index["contracts"] = {
                "honeymoney_version": HONEYMONEY_VERSION,
                "import_record_schema_version": IMPORT_RECORD_SCHEMA_VERSION,
                "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
                "transaction_schema_version": TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
                "derivation_contract": _derivation_contract(context.config),
            }
            next_index["identity_manifest"] = resolution.next_manifest
            next_index["overlap_manifest"] = derivation.overlap_manifest
            corrections_path = _configured_input_path(context, "corrections")
            proof_overrides = (
                {corrections_path: correction_content}
                if correction_content is not None
                else None
            )
            next_index["input_proofs"] = _input_proofs(
                context, overrides=proof_overrides
            )

            resolved_by_namespace = {
                item["source_namespace_id"]: item
                for item in resolution.next_manifest["sources"]
            }
            targets: list[PublicationTarget] = [
                PublicationTarget(
                    f"{item.record.relative_to(context.paths.root).as_posix()}/summary.json",
                    item.summary_content,
                )
                for item in failed_attempts
            ]
            attempt_reports_by_source_id: dict[
                str, AttemptReservation | FixedAttemptReservation
            ] = {item.source_id: item.reservation for item in failed_attempts}
            if correction_content is not None:
                targets.append(
                    PublicationTarget(
                        corrections_path.relative_to(context.paths.root).as_posix(),
                        correction_content,
                    )
                )
            record_artifacts: list[dict[str, str]] = [
                {"source_id": item.source_id, "path": str(item.record)}
                for item in failed_attempts
            ]
            planned_by_path = {item.path: item for item in planned_attempts}
            for parsed_source in parsed:
                source = resolved_by_namespace[parsed_source.identity.namespace_id]
                source_id = source["source_id"]
                planned = planned_by_path[parsed_source.path]
                if (
                    source_id != planned.source_id
                    or parsed_source.identity.revision != planned.source_revision
                    or parsed_source.identity.contract_id != planned.parser_contract
                ):
                    raise WorkspaceCommandError("durable_state_conflict")
                record = planned.record
                attempt_number = planned.attempt_number
                rows = [
                    _snapshot_row(row)
                    for row in resolved_rows
                    if row.get("source_id") == source_id
                ]
                snapshot = transaction_snapshot_document(SNAPSHOT_COLUMNS, rows)
                digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
                report = _attempt_report(
                    source_id=source_id,
                    source_label=parsed_source.source_label,
                    attempt_number=attempt_number,
                    action=action,
                    started=started,
                    outcome="success",
                    source_revision=parsed_source.identity.revision,
                    parser_contract=parsed_source.identity.contract_id,
                    transaction_count=len(rows),
                    warnings=parsed_source.warnings,
                    errors=(),
                    transactions_digest=digest,
                )
                interrupted_report = _attempt_report(
                    source_id=source_id,
                    source_label=parsed_source.source_label,
                    attempt_number=attempt_number,
                    action=action,
                    started=started,
                    outcome="failure",
                    source_revision=parsed_source.identity.revision,
                    parser_contract=parsed_source.identity.contract_id,
                    transaction_count=0,
                    warnings=(),
                    errors=("interrupted",),
                )
                summary = {
                    "schema_version": IMPORT_RECORD_SCHEMA_VERSION,
                    "source_id": source_id,
                    "source_label": parsed_source.source_label,
                    "ready": True,
                    "current_attempt_number": attempt_number,
                    "statement_transaction_count": len(rows),
                }
                base = record.relative_to(context.paths.root).as_posix()
                targets.extend(
                    (
                        PublicationTarget(
                            f"{base}/transactions.csv", snapshot.encode()
                        ),
                        PublicationTarget(
                            f"{base}/summary.json", summary_document(summary).encode()
                        ),
                    )
                )
                attempt_reports_by_source_id[source_id] = AttemptReservation(
                    path=f"{base}/attempts/{attempt_number:08d}.json",
                    success_content=attempt_document(report).encode(),
                    interrupted_content=attempt_document(interrupted_report).encode(),
                )
                record_artifacts.append({"source_id": source_id, "path": str(record)})

            attempt_reports = [
                attempt_reports_by_source_id[item.source_id]
                for item in planned_attempts
            ]

            view_targets, registered_views, written_periods = _plan_views(
                context, prior_derivation, derivation
            )
            targets.extend(view_targets)
            next_index["registered_views"] = registered_views
        except PublicationError:
            raise
        except (OSError, csv.Error, ValueError) as error:
            failure = _fixed_post_reservation_error(error)
            _finalize_post_reservation_import_failure(
                context,
                generation_id,
                planned_attempts,
                preflighted_sources,
                action=action,
                started=started,
                error=failure,
            )
            raise failure from error
        try:
            publish_reserved_generation(
                context.paths.root,
                generation_id,
                targets,
                workspace_index_document(next_index).encode(),
                attempt_reports=attempt_reports,
            )
        except PublicationError:
            raise

    warnings = (
        tuple(warning for item in parsed for warning in item.warnings)
        + derivation.warnings
        + tuple("source_import_failed" for _item in failed_attempts)
    )
    return CommandResult(
        data={
            "import_count": len(parsed),
            "statement_transaction_count": sum(len(item.rows) for item in parsed),
            "view_transaction_count": len(derivation.rows),
        },
        artifacts={
            "import_records": record_artifacts,
            "views": [
                {
                    "period": period,
                    "path": str(context.paths.views / period),
                }
                for period in written_periods
            ],
        },
        warnings=warnings,
    )


def list_imports(config_path: str | Path | None) -> CommandResult:
    """List every visible import record without reading transaction values."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        items: list[dict[str, object]] = []
        try:
            if context.paths.import_records.exists():
                for record in sorted(context.paths.import_records.iterdir()):
                    if record.is_symlink():
                        raise WorkspaceCommandError("managed_path_unsafe")
                    if not record.is_dir():
                        continue
                    try:
                        summary = build_summary(record, record.name)
                    except ValueError as error:
                        code = (
                            "managed_path_unsafe"
                            if getattr(error, "unsafe_path", False)
                            else "import_record_invalid"
                        )
                        raise WorkspaceCommandError(code) from error
                    items.append(
                        {
                            "source_id": summary["source_id"],
                            "source_label": summary["source_label"],
                            "ready": summary["ready"],
                            "statement_transaction_count": summary[
                                "statement_transaction_count"
                            ],
                        }
                    )
        except WorkspaceCommandError:
            raise
        except OSError as error:
            raise WorkspaceCommandError(
                "import_record_invalid", "Import records are unavailable."
            ) from error
    return CommandResult(
        data={"import_count": len(items), "import_records": items}, artifacts={}
    )


def show_import(source_id: str, config_path: str | Path | None) -> CommandResult:
    """Show one record's bounded, value-free immutable attempt history."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        try:
            record = import_record_path(context.paths.import_records, source_id)
            summary = build_summary(record, source_id)
            attempts = load_attempts(record)
        except ValueError as error:
            code = (
                "managed_path_unsafe"
                if getattr(error, "unsafe_path", False)
                else "import_record_invalid"
            )
            raise WorkspaceCommandError(code) from error
        if not record.exists():
            raise WorkspaceCommandError("import_record_not_found")
    latest = attempts[-1]["attempt_number"] if attempts else None
    data: dict[str, object] = dict(summary)
    data.update({"latest_attempt_number": latest, "attempts": attempts})
    return CommandResult(data=data, artifacts={})


def rebuild_views(
    selection: PeriodSelection,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Rebuild selected complete view units from all durable authorities."""
    context = load_workspace(config_path)
    current_proofs = _input_proofs(context)
    _require_narrow_rebuild_proofs(context, current_proofs, selection.kind)
    with WorkspaceLock(context.paths.root):
        context = load_workspace(config_path)
        current_proofs = _input_proofs(context)
        _require_narrow_rebuild_proofs(context, current_proofs, selection.kind)
        _source_rows, derivation = _derive_current_workspace(context)
        from honeymoney.workspace_views import plan_workspace_views

        plan = plan_workspace_views(
            derivation.rows,
            selection,
            context.index["registered_views"],
            content_proof_key=_content_proof_key(context),
            installed_files=_installed_view_files(context.paths),
            report_inputs=_view_report_inputs_by_period(derivation),
        )
        proofs_changed = context.index["input_proofs"] != current_proofs
        if not plan.writes and not plan.removals and not proofs_changed:
            return _view_result(context, plan)
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(context.config),
        }
        next_index["overlap_manifest"] = derivation.overlap_manifest
        next_index["registered_views"] = list(plan.next_registered_views)
        next_index["input_proofs"] = current_proofs
        targets = [
            PublicationTarget(item.path, item.content)
            for item in plan.publication_files()
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        for period in plan.removals:
            try:
                (context.paths.views / period).rmdir()
            except OSError:
                pass
        return _view_result(context, plan)


def workspace_status(
    selection: PeriodSelection,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Return selected counts from a full current workspace derivation."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, derivation = _derive_current_workspace(context)
        query = _query_derivation(derivation, selection)
        supporting = _supporting_source_rows(source_rows, derivation, query)
    return CommandResult(
        data={
            "import_count": len(
                {row.get("source_id", "") for row in supporting} - {""}
            ),
            "statement_transaction_count": len(supporting),
            "view_transaction_count": query.view_transaction_count,
            "needs_review_count": query.pending_count,
            "periods": list(query.periods),
        },
        artifacts={},
        warnings=derivation.warnings,
    )


def workspace_pending(
    selection: PeriodSelection,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Return the selected generated review queue without writing."""
    context, source_rows, derivation, query = _locked_workspace_query(
        selection, config_path
    )
    supporting = _supporting_source_rows(source_rows, derivation, query)
    return CommandResult(
        data={
            "import_count": len(
                {row.get("source_id", "") for row in supporting} - {""}
            ),
            "statement_transaction_count": len(supporting),
            "view_transaction_count": query.view_transaction_count,
            "pending_count": query.pending_count,
            "periods": list(query.periods),
            "transactions": [to_review_row(row) for row in query.pending_rows],
        },
        artifacts={},
        warnings=derivation.warnings,
    )


def workspace_missing_valuations(
    selection: PeriodSelection,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Return selected missing base-currency values without changing state."""
    _context, _source_rows, derivation, query = _locked_workspace_query(
        selection, config_path
    )
    fields = (
        "transaction_id",
        "date",
        "transaction_date",
        "posting_date",
        "original_amount",
        "original_currency",
        "posted_amount",
        "posted_currency",
        "flow_type",
        "valuation_status",
        "valuation_source",
        "source_occurrence_count",
    )
    return CommandResult(
        data={
            "view_transaction_count": query.view_transaction_count,
            "missing_valuation_count": query.missing_valuation_count,
            "periods": list(query.periods),
            "transactions": [
                {field: row.get(field, "") for field in fields}
                for row in query.missing_valuation_rows
            ],
        },
        artifacts={},
        warnings=derivation.warnings,
    )


def workspace_report(
    selection: PeriodSelection,
    *,
    config_path: str | Path | None,
    export_path: str | Path | None = None,
) -> CommandResult:
    """Choose a managed report or write one explicit preview or export."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        _source_rows, derivation = _derive_current_workspace(context)
        query = _query_derivation(derivation, selection)
        if export_path is not None:
            raw_target = Path(export_path).expanduser()
            if raw_target.is_symlink():
                raise WorkspaceCommandError("managed_path_unsafe")
            target = raw_target.resolve(strict=False)
            _reject_report_export_target(context, target)
            private_atomic_write_text(target, query.report_html.decode("utf-8"))
        elif selection.kind in {"month", "undated"}:
            [period] = query.periods
            target = context.paths.views / period / "report.html"
            if target.parent.is_symlink() or target.is_symlink():
                raise WorkspaceCommandError("managed_path_unsafe")
            try:
                installed = target.read_bytes()
            except OSError as error:
                raise WorkspaceCommandError(
                    "generated_view_invalid",
                    "Run `honeymoney views rebuild` for this period.",
                ) from error
            if installed != query.report_html:
                raise WorkspaceCommandError(
                    "generated_view_invalid",
                    "Run `honeymoney views rebuild` for this period.",
                )
        else:
            target = context.paths.report_preview
            private_atomic_write_text(target, query.report_html.decode("utf-8"))
    return CommandResult(
        data={
            "view_transaction_count": query.view_transaction_count,
            "periods": list(query.periods),
        },
        artifacts={"report_html": str(target)},
        warnings=derivation.warnings,
    )


def _locked_workspace_query(
    selection: PeriodSelection,
    config_path: str | Path | None,
) -> tuple[WorkspaceContext, list[dict[str, str]], WorkspaceDerivation, WorkspaceQuery]:
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, derivation = _derive_current_workspace(context)
        return (
            context,
            source_rows,
            derivation,
            _query_derivation(derivation, selection),
        )


def _supporting_source_rows(
    source_rows: Sequence[Mapping[str, str]],
    derivation: WorkspaceDerivation,
    query: WorkspaceQuery,
) -> list[dict[str, str]]:
    selected_ids = {row.get("transaction_id", "") for row in query.rows}
    occurrence_ids: set[str] = set()
    for group in derivation.overlap["groups"]:
        if selected_ids.intersection(group["canonical_transaction_ids"]):
            occurrence_ids.update(
                str(identifier)
                for pool in group["source_occurrence_pools"]
                for identifier in pool
            )
    return [
        dict(row)
        for row in source_rows
        if row.get("transaction_id", "") in occurrence_ids
    ]


def workspace_duplicates(*, config_path: str | Path | None) -> CommandResult:
    """List unresolved duplicate memberships from current durable facts."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, derivation = _derive_current_workspace(context)
        groups = list_duplicate_groups(derivation.canonicalization, source_rows)
    return CommandResult(
        data={"duplicate_group_count": len(groups), "groups": groups},
        artifacts={},
        warnings=derivation.warnings,
    )


def resolve_workspace_duplicate(
    review_group_id: str,
    choice: str,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Save one exact-membership duplicate choice and refresh changed views."""
    initial = load_workspace(config_path)
    _require_current_input_proofs(initial)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, prior = _derive_current_workspace(context)
        corrections = _load_workspace_corrections(context.config)
        try:
            resolution = resolve_duplicate_group(
                source_rows,
                prior.rows,
                context.index["overlap_manifest"],
                review_group_id,
                choice,
                corrections,
            )
            correction_path, correction_document, merged = prepare_corrections_document(
                context.config,
                resolution.correction_updates,
                removed_transaction_ids=set(resolution.removed_correction_ids),
            )
        except ValueError as error:
            code = getattr(error, "code", "workspace_input_invalid")
            raise WorkspaceCommandError(str(code)) from error
        correction_content = correction_document.encode()
        try:
            correction_changed = correction_path.read_bytes() != correction_content
        except OSError as error:
            raise WorkspaceCommandError("corrections_invalid") from error
        next_derivation = derive_workspace_rows(
            source_rows,
            resolution.result.manifest,
            context.config,
            rules=_load_workspace_rules(context.config),
            corrections=merged,
            profile_mappings=_load_workspace_profile_mappings(context.config),
            allow_model=_model_enabled(context.config),
        )
        view_targets, registered_views, written_periods = _plan_views(
            context, prior, next_derivation
        )
        if resolution.idempotent and not correction_changed and not view_targets:
            return _duplicate_result(
                resolution,
                next_derivation,
                context,
                (),
            )
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(context.config),
        }
        next_index["overlap_manifest"] = next_derivation.overlap_manifest
        next_index["registered_views"] = registered_views
        next_index["input_proofs"] = _input_proofs(
            context,
            overrides=(
                {correction_path: correction_content} if correction_changed else None
            ),
        )
        targets = [
            *(
                [
                    PublicationTarget(
                        correction_path.relative_to(context.paths.root).as_posix(),
                        correction_content,
                    )
                ]
                if correction_changed
                else []
            ),
            *view_targets,
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        return _duplicate_result(
            resolution,
            next_derivation,
            context,
            written_periods,
        )


def _duplicate_result(
    resolution: DuplicateResolution,
    derivation: WorkspaceDerivation,
    context: WorkspaceContext,
    written_periods: Sequence[str],
) -> CommandResult:
    return CommandResult(
        data={
            "idempotent": resolution.idempotent,
            "old_view_transaction_count": resolution.old_group_canonical_count,
            "new_view_transaction_count": resolution.new_group_canonical_count,
            "remaining_duplicate_group_count": resolution.remaining_unresolved_count,
            "view_transaction_count": len(derivation.rows),
            "written_count": len(written_periods),
        },
        artifacts={
            "views": [
                {
                    "period": period,
                    "path": str(context.paths.views / period),
                }
                for period in written_periods
            ]
        },
        warnings=derivation.warnings,
    )


def apply_workspace_corrections(
    correction_patches: Mapping[str, Mapping[str, str]],
    *,
    config_path: str | Path | None,
    expected_generation_id: str | None = None,
) -> CommandResult:
    """Save explicit choices and refresh every changed view as one generation."""
    patches = {
        str(transaction_id): {str(field): str(value) for field, value in patch.items()}
        for transaction_id, patch in correction_patches.items()
    }
    if not patches or any(
        not transaction_id or not patch for transaction_id, patch in patches.items()
    ):
        raise WorkspaceCommandError("workspace_input_invalid")
    context = load_workspace(config_path)
    _require_current_input_proofs(context)
    with WorkspaceLock(context.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        if (
            expected_generation_id is not None
            and context.index["generation_id"] != expected_generation_id
        ):
            raise WorkspaceCommandError(
                "workspace_busy", "Workspace changed; retry the correction."
            )
        source_rows, prior = _derive_current_workspace(context)
        active_ids = {
            row.get("transaction_id", "")
            for row in prior.rows
            if row.get("transaction_id")
        }
        unknown = sorted(set(patches) - active_ids)
        if unknown:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Unknown view transaction ID."
            )
        try:
            correction_path, correction_document, merged = prepare_corrections_document(
                context.config,
                patches,
            )
        except ValueError as error:
            raise WorkspaceCommandError(
                "corrections_invalid", "Saved corrections are invalid."
            ) from error
        mappings = _load_workspace_profile_mappings(context.config)
        next_derivation = derive_workspace_rows(
            source_rows,
            context.index["overlap_manifest"],
            context.config,
            rules=_load_workspace_rules(context.config),
            corrections=merged,
            profile_mappings=mappings,
            allow_model=_model_enabled(context.config),
        )
        view_targets, registered_views, written_periods = _plan_views(
            context, prior, next_derivation
        )
        correction_content = correction_document.encode()
        try:
            correction_changed = correction_path.read_bytes() != correction_content
        except OSError as error:
            raise WorkspaceCommandError("corrections_invalid") from error
        if not correction_changed and not view_targets:
            return CommandResult(
                data={
                    "corrected_count": len(patches),
                    "view_transaction_count": len(next_derivation.rows),
                    "written_count": 0,
                },
                artifacts={"views": []},
                warnings=next_derivation.warnings,
            )

        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(context.config),
        }
        next_index["overlap_manifest"] = next_derivation.overlap_manifest
        next_index["registered_views"] = registered_views
        next_index["input_proofs"] = _input_proofs(
            context, overrides={correction_path: correction_content}
        )
        targets = [
            *(
                [
                    PublicationTarget(
                        correction_path.relative_to(context.paths.root).as_posix(),
                        correction_content,
                    )
                ]
                if correction_changed
                else []
            ),
            *view_targets,
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        return CommandResult(
            data={
                "corrected_count": len(patches),
                "view_transaction_count": len(next_derivation.rows),
                "written_count": len(written_periods),
            },
            artifacts={
                "views": [
                    {
                        "period": period,
                        "path": str(context.paths.views / period),
                    }
                    for period in written_periods
                ]
            },
            warnings=next_derivation.warnings,
        )


def review_workspace_transaction(
    transaction_id: str,
    decision: str,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Turn one accounting review decision into a saved correction."""
    context = load_workspace(config_path)
    _require_current_input_proofs(context)
    _source_rows, derivation = _derive_current_workspace(context)
    transaction = next(
        (row for row in derivation.rows if row.get("transaction_id") == transaction_id),
        None,
    )
    if transaction is None:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Unknown view transaction ID."
        )
    try:
        patch = accounting_decision_patch(
            transaction,
            decision,
            "Accounting flow confirmed by local review",
        )
    except ValueError as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "The accounting choice is invalid."
        ) from error
    return apply_workspace_corrections(
        {transaction_id: patch},
        config_path=config_path,
        expected_generation_id=context.index["generation_id"],
    )


def workspace_reconciliation_summary(
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Inspect the current reconciliation derivation without publishing bytes."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, derivation = _derive_current_workspace(context)
    reconciliation = dict(derivation.reconciliation)
    reconciliation.pop("transaction_count", None)
    return CommandResult(
        data={
            **reconciliation,
            "import_count": len(
                {row.get("source_id", "") for row in source_rows} - {""}
            ),
            "statement_transaction_count": len(source_rows),
            "view_transaction_count": len(derivation.rows),
            "read_only": True,
        },
        artifacts={},
        warnings=derivation.warnings,
    )


def workspace_source_data_inspect(
    transaction_id: str,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Inspect one view transaction's stored normalized source evidence."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, derivation = _derive_current_workspace(context)
        item = _source_data_inspection(context, source_rows, derivation, transaction_id)
    return CommandResult(
        data={"transaction": item}, artifacts={}, warnings=derivation.warnings
    )


def resolve_workspace_source_data(
    transaction_id: str,
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Clear a stale saved source-data review reason, if stored facts allow it."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, derivation = _derive_current_workspace(context)
        item = _source_data_inspection(context, source_rows, derivation, transaction_id)
        if item["active_evidence_flags"]:
            raise WorkspaceCommandError(
                "source_data_evidence_active",
                "Current stored facts still support this source-data issue.",
            )
        corrections = _load_workspace_corrections(context.config)
        correction = corrections.get(transaction_id)
        if correction is None or not source_data_review_active(correction):
            return CommandResult(
                data={
                    "transaction_id": transaction_id,
                    "result": "already_clear",
                    "changed": False,
                    "evidence_status": "clear",
                },
                artifacts={},
                warnings=derivation.warnings,
            )
        remaining = [
            reason
            for reason in review_reason_tokens(correction.get("review_reasons", ""))
            if reason != REVIEW_REASON_SOURCE_DATA
        ]
        patch = {
            "review_reasons": ";".join(remaining),
            "needs_review": str(bool(remaining)).lower(),
        }
        expected_generation_id = context.index["generation_id"]
    result = apply_workspace_corrections(
        {transaction_id: patch},
        config_path=config_path,
        expected_generation_id=expected_generation_id,
    )
    return CommandResult(
        data={
            "transaction_id": transaction_id,
            "result": "resolved",
            "changed": True,
            "evidence_status": "clear",
            "written_count": result.data["written_count"],
        },
        artifacts=result.artifacts,
        warnings=result.warnings,
    )


def workspace_review_pair(
    transaction_ids: Sequence[str],
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Save a checked manual transfer-pair correction for two view rows."""
    supplied_ids = [str(item) for item in transaction_ids]
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        _source_rows, derivation = _derive_current_workspace(context)
        if len(supplied_ids) != 2 or len(set(supplied_ids)) != 2:
            raise WorkspaceCommandError(
                "manual_pair_requires_two",
                "A manual transfer pair requires two distinct current transaction IDs.",
            )
        rows_by_id = {
            row.get("transaction_id", ""): row
            for row in derivation.rows
            if row.get("transaction_id")
        }
        try:
            left, right = (rows_by_id[item] for item in supplied_ids)
        except KeyError as error:
            raise WorkspaceCommandError(
                "manual_pair_stale_transaction",
                "A nominated transaction is no longer current.",
            ) from error
        try:
            validate_manual_pair_facts(left, right)
        except ManualPairError as error:
            raise WorkspaceCommandError(
                error.code, "The manual pair facts are invalid."
            ) from error
        corrections = _load_workspace_corrections(context.config)
        stored_pairs = {
            pair
            for row in (left, right)
            for pair in (
                manual_pair_marker(row),
                corrections.get(row["transaction_id"], {}).get(MANUAL_PAIR_FIELD, ""),
            )
            if pair
        }
        if len(stored_pairs) > 1:
            raise WorkspaceCommandError(
                "manual_pair_conflict",
                "A nominated transaction belongs to a conflicting active pair.",
            )
        pair_id = next(iter(stored_pairs), manual_pair_id(supplied_ids))
        expected_ids = {left["transaction_id"], right["transaction_id"]}
        stored_members = {
            identifier
            for identifier, correction in corrections.items()
            if correction.get(MANUAL_PAIR_FIELD) == pair_id
        }
        if stored_members and stored_members != expected_ids:
            raise WorkspaceCommandError(
                "manual_pair_conflict",
                "The saved manual pair has conflicting membership.",
            )
        if stored_members == expected_ids:
            return CommandResult(
                data={
                    "paired_count": 2,
                    "unchanged": True,
                    "changed": False,
                    "result": "already_paired",
                    "pair_id": pair_id,
                    "transaction_ids": sorted(expected_ids),
                },
                artifacts={},
                warnings=derivation.warnings,
            )
        patches = {
            row["transaction_id"]: {
                **accounting_decision_patch(
                    row,
                    "internal-transfer",
                    "Internal transfer confirmed by manual pair review",
                ),
                MANUAL_PAIR_FIELD: pair_id,
            }
            for row in (left, right)
        }
        expected_generation_id = context.index["generation_id"]
    result = apply_workspace_corrections(
        patches,
        config_path=config_path,
        expected_generation_id=expected_generation_id,
    )
    return CommandResult(
        data={
            "paired_count": 2,
            "unchanged": False,
            "changed": True,
            "result": "paired",
            "pair_id": pair_id,
            "transaction_ids": sorted(patches),
            "written_count": result.data["written_count"],
        },
        artifacts=result.artifacts,
        warnings=result.warnings,
    )


def _source_data_inspection(
    context: WorkspaceContext,
    source_rows: list[dict[str, str]],
    derivation: WorkspaceDerivation,
    transaction_id: str,
) -> dict[str, object]:
    """Adapt new durable authorities to the source-data inspection domain."""
    state = IdentityState(
        derivation.rows,
        context.index["identity_manifest"],
        manifest_document(context.index["identity_manifest"]),
        source_rows=source_rows,
        overlap_manifest=derivation.overlap_manifest,
    )
    try:
        return dict(
            inspect_source_data_review(
                state,
                transaction_id,
                workspace_root=context.paths.root,
                correction_review_reason_active=source_data_review_active(
                    _load_workspace_corrections(context.config).get(transaction_id)
                ),
            )
        )
    except SourceDataReviewError as error:
        messages = {
            "source_data_transaction_unknown": "Unknown view transaction ID.",
            "source_data_provenance_unavailable": "Stored source evidence is unavailable.",
            "source_data_provenance_inconsistent": "Stored source evidence is inconsistent.",
            "source_data_provenance_ambiguous": "Stored source evidence is ambiguous.",
        }
        raise WorkspaceCommandError(
            error.code, messages.get(error.code, "Source-data inspection failed.")
        ) from error


def apply_workspace_rate_observations(
    observations: Sequence[Mapping[str, object]],
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Merge public rate facts and refresh all changed views in one generation."""
    context = load_workspace(config_path)
    _require_current_input_proofs(context)
    with WorkspaceLock(context.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, prior = _derive_current_workspace(context)
        requested_pairs = _requested_rate_pairs(
            source_rows,
            base_currency=str(context.config.get("base_currency", "HKD")),
        )
        rate_cache_path = _configured_input_path(context, "rate_cache")
        try:
            cache = merge_rate_cache(
                load_rate_cache(rate_cache_path), observations, requested_pairs
            )
            cache_content = rate_cache_document(cache).encode()
        except ValueError as error:
            code = getattr(error, "code", "workspace_input_invalid")
            raise WorkspaceCommandError(str(code), "Rate data is invalid.") from error
        next_config = {**context.config, "_rate_cache": cache}
        next_derivation = derive_workspace_rows(
            source_rows,
            context.index["overlap_manifest"],
            next_config,
            rules=_load_workspace_rules(next_config),
            corrections=_load_workspace_corrections(next_config),
            profile_mappings=_load_workspace_profile_mappings(next_config),
            allow_model=_model_enabled(next_config),
        )
        view_targets, registered_views, written_periods = _plan_views(
            context, prior, next_derivation
        )
        try:
            cache_changed = rate_cache_path.read_bytes() != cache_content
        except OSError as error:
            raise WorkspaceCommandError("workspace_input_invalid") from error
        if not cache_changed and not view_targets:
            return _rate_result(
                context,
                observations,
                cache,
                requested_pairs,
                next_derivation,
                (),
            )
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(next_config),
        }
        next_index["overlap_manifest"] = next_derivation.overlap_manifest
        next_index["registered_views"] = registered_views
        next_index["input_proofs"] = _input_proofs(
            context, overrides={rate_cache_path: cache_content}
        )
        targets = [
            *(
                [
                    PublicationTarget(
                        rate_cache_path.relative_to(context.paths.root).as_posix(),
                        cache_content,
                    )
                ]
                if cache_changed
                else []
            ),
            *view_targets,
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        return _rate_result(
            context,
            observations,
            cache,
            requested_pairs,
            next_derivation,
            written_periods,
        )


def _requested_rate_pairs(
    rows: Sequence[Mapping[str, str]], *, base_currency: str
) -> list[tuple[str, str]]:
    base = base_currency.strip().upper()
    result: set[tuple[str, str]] = set()
    for row in rows:
        currency = row.get("posted_currency", "").strip().upper()
        transaction_date = (
            row.get("transaction_date", "") or row.get("date", "")
        ).strip()
        try:
            date.fromisoformat(transaction_date)
        except ValueError:
            continue
        if currency and currency != base:
            result.add((currency, transaction_date))
    return sorted(result)


def _rate_result(
    context: WorkspaceContext,
    observations: Sequence[Mapping[str, object]],
    cache: Mapping[str, object],
    requested_pairs: Sequence[tuple[str, str]],
    derivation: WorkspaceDerivation,
    written_periods: Sequence[str],
) -> CommandResult:
    resolutions = cache.get("resolutions")
    cached_observations = cache.get("observations")
    if not isinstance(resolutions, list) or not isinstance(cached_observations, list):
        raise AssertionError("validated rate cache has an invalid shape")
    return CommandResult(
        data={
            "imported_observation_count": len(observations),
            "cached_observation_count": len(cached_observations),
            "requested_transaction_date_count": len(requested_pairs),
            "resolved_transaction_date_count": len(resolutions),
            "view_transaction_count": len(derivation.rows),
            "written_count": len(written_periods),
        },
        artifacts={
            "views": [
                {
                    "period": period,
                    "path": str(context.paths.views / period),
                }
                for period in written_periods
            ]
        },
        warnings=derivation.warnings,
    )


def learn_workspace_rules(
    *,
    config_path: str | Path | None,
    apply: bool,
) -> CommandResult:
    """Plan or publish exact learned rules from current saved corrections."""
    initial = load_workspace(config_path)
    _require_current_input_proofs(initial)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        source_rows, prior = _derive_current_workspace(context)
        corrections = _load_workspace_corrections(context.config)
        plan = plan_learned_rules(prior.rows, corrections)
        rules_path = _configured_input_path(context, "rules")
        try:
            document = json.loads(rules_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Rules are not valid JSON."
            ) from error
        if not isinstance(document, dict) or not isinstance(
            document.get("rules"), list
        ):
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Rules must contain a rules list."
            )
        if not all(isinstance(rule, dict) for rule in document["rules"]):
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Rules entries must be objects."
            )
        manual_rules = [
            rule
            for rule in document["rules"]
            if rule.get("managed_by") != MANAGED_RULE_MARKER
        ]
        next_rules = [*manual_rules, *plan.rules]
        try:
            validate_rules(next_rules, context.config)
        except ValueError as error:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Generated rules are invalid."
            ) from error
        next_document = {**document, "rules": next_rules}
        content = (json.dumps(next_document, indent=2, sort_keys=True) + "\n").encode()
        counts = plan.counts()
        try:
            changed = rules_path.read_bytes() != content
        except OSError as error:
            raise WorkspaceCommandError("workspace_input_invalid") from error
        if not apply:
            return CommandResult(
                data={**counts, "changed": False, "written_count": 0},
                artifacts={},
                warnings=prior.warnings,
            )
        next_derivation = derive_workspace_rows(
            source_rows,
            context.index["overlap_manifest"],
            context.config,
            rules=next_rules,
            corrections=corrections,
            profile_mappings=_load_workspace_profile_mappings(context.config),
            allow_model=_model_enabled(context.config),
        )
        view_targets, registered_views, written_periods = _plan_views(
            context, prior, next_derivation
        )
        if not changed and not view_targets:
            return CommandResult(
                data={**counts, "changed": False, "written_count": 0},
                artifacts={"views": []},
                warnings=next_derivation.warnings,
            )
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(context.config),
        }
        next_index["overlap_manifest"] = next_derivation.overlap_manifest
        next_index["registered_views"] = registered_views
        next_index["input_proofs"] = _input_proofs(
            context, overrides={rules_path: content}
        )
        targets = [
            *(
                [
                    PublicationTarget(
                        rules_path.relative_to(context.paths.root).as_posix(), content
                    )
                ]
                if changed
                else []
            ),
            *view_targets,
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        return CommandResult(
            data={
                **counts,
                "changed": changed,
                "written_count": len(written_periods),
            },
            artifacts={
                "rules_json": str(rules_path),
                "views": [
                    {"period": period, "path": str(context.paths.views / period)}
                    for period in written_periods
                ],
            },
            warnings=next_derivation.warnings,
        )


def apply_workspace_profile_mappings(
    mappings: Mapping[str, object],
    *,
    config_path: str | Path | None,
    expected_generation_id: str | None = None,
) -> CommandResult:
    """Validate and publish one user-owned account-mapping document."""
    initial = load_workspace(config_path)
    _require_current_input_proofs(initial)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        if (
            expected_generation_id is not None
            and context.index["generation_id"] != expected_generation_id
        ):
            raise WorkspaceCommandError(
                "workspace_busy", "Workspace changed; retry the mapping update."
            )
        profiles = _load_workspace_profiles(context.config)
        try:
            checked = validate_profile_mappings(dict(mappings), context.config)
            validate_bindings_for_profiles(checked, profiles)
        except ValueError as error:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Profile mappings are invalid."
            ) from error
        mappings_path = _configured_input_path(context, "profile_mappings")
        content = (json.dumps(checked, indent=2, sort_keys=True) + "\n").encode()
        try:
            changed = mappings_path.read_bytes() != content
        except OSError as error:
            raise WorkspaceCommandError("workspace_input_invalid") from error
        source_rows, prior = _derive_current_workspace(context)
        next_derivation = derive_workspace_rows(
            source_rows,
            context.index["overlap_manifest"],
            context.config,
            rules=_load_workspace_rules(context.config),
            corrections=_load_workspace_corrections(context.config),
            profile_mappings=checked,
            allow_model=_model_enabled(context.config),
        )
        view_targets, registered_views, written_periods = _plan_views(
            context, prior, next_derivation
        )
        if not changed and not view_targets:
            return CommandResult(
                data={"changed": False, "written_count": 0},
                artifacts={"views": []},
                warnings=next_derivation.warnings,
            )
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(context.config),
        }
        next_index["overlap_manifest"] = next_derivation.overlap_manifest
        next_index["registered_views"] = registered_views
        next_index["input_proofs"] = _input_proofs(
            context,
            overrides={mappings_path: content},
        )
        targets = [
            *(
                [
                    PublicationTarget(
                        mappings_path.relative_to(context.paths.root).as_posix(),
                        content,
                    )
                ]
                if changed
                else []
            ),
            *view_targets,
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        return CommandResult(
            data={"changed": changed, "written_count": len(written_periods)},
            artifacts={
                "views": [
                    {
                        "period": period,
                        "path": str(context.paths.views / period),
                    }
                    for period in written_periods
                ]
            },
            warnings=next_derivation.warnings,
        )


def workspace_profile_bindings(
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """List checked account bindings without changing workspace state."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        profiles = _load_workspace_profiles(context.config)
        mappings = _load_workspace_profile_mappings(context.config)
        try:
            validate_bindings_for_profiles(mappings, profiles)
        except ValueError as error:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Profile mappings are invalid."
            ) from error
        from honeymoney.account_bindings import binding_views

        views = binding_views(mappings)
    return CommandResult(
        data={"binding_count": len(views), "bindings": views}, artifacts={}
    )


def workspace_public_config(
    *,
    config_path: str | Path | None,
) -> CommandResult:
    """Return the user-visible config without runtime or secret-like fields."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        raw = _workspace_config_document(context.paths)
    return CommandResult(
        data={"config": _public_config_value(raw)},
        artifacts={"config_json": str(context.paths.config)},
    )


def workspace_config_document(
    *,
    config_path: str | Path | None,
) -> tuple[WorkspaceContext, dict[str, object]]:
    """Read a raw config document for a local edit staging step only."""
    initial = load_workspace(config_path)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        return context, _workspace_config_document(context.paths)


def apply_workspace_config(
    raw_config: Mapping[str, object],
    *,
    config_path: str | Path | None,
    expected_generation_id: str | None = None,
) -> CommandResult:
    """Publish a checked config document and every affected view as one generation."""
    candidate = copy.deepcopy(dict(raw_config))
    if any(key.startswith("_") for key in candidate):
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Config must not contain private runtime fields."
        )
    initial = load_workspace(config_path)
    _require_current_input_proofs(initial)
    with WorkspaceLock(initial.paths.root):
        context = load_workspace(config_path)
        _require_current_input_proofs(context)
        if (
            expected_generation_id is not None
            and context.index["generation_id"] != expected_generation_id
        ):
            raise WorkspaceCommandError(
                "workspace_busy", "Workspace changed; retry the config update."
            )
        try:
            reject_legacy_workspace(context.paths, candidate)
            next_config = _resolved_config(context.paths, candidate)
            profiles = _load_workspace_profiles(next_config)
            mappings = _load_workspace_profile_mappings(next_config)
            validate_bindings_for_profiles(mappings, profiles)
            next_rules = _load_workspace_rules(next_config)
            next_corrections = _load_workspace_corrections(next_config)
        except ValueError as error:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "Workspace configuration is invalid."
            ) from error
        content = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()
        try:
            changed = context.paths.config.read_bytes() != content
        except OSError as error:
            raise WorkspaceCommandError("workspace_input_invalid") from error
        source_rows, prior = _derive_current_workspace(context)
        next_context = WorkspaceContext(context.paths, next_config, context.index)
        next_source_rows = _load_ready_source_rows(next_context)
        next_derivation = derive_workspace_rows(
            next_source_rows,
            context.index["overlap_manifest"],
            next_config,
            rules=next_rules,
            corrections=next_corrections,
            profile_mappings=mappings,
            allow_model=_model_enabled(next_config),
        )
        view_targets, registered_views, written_periods = _plan_views(
            context, prior, next_derivation
        )
        if not changed and not view_targets:
            return CommandResult(
                data={"changed": False, "written_count": 0},
                artifacts={"views": []},
                warnings=next_derivation.warnings,
            )
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index = copy.deepcopy(context.index)
        next_index["generation_id"] = generation_id
        next_index["contracts"] = {
            **next_index["contracts"],
            "honeymoney_version": HONEYMONEY_VERSION,
            "derivation_contract": _derivation_contract(next_config),
        }
        next_index["overlap_manifest"] = next_derivation.overlap_manifest
        next_index["registered_views"] = registered_views
        next_index["input_proofs"] = _input_proofs(
            next_context, overrides={context.paths.config: content}
        )
        targets = [
            *(
                [
                    PublicationTarget(
                        context.paths.config.relative_to(context.paths.root).as_posix(),
                        content,
                    )
                ]
                if changed
                else []
            ),
            *view_targets,
        ]
        publish_generation(
            context.paths.root,
            generation_id,
            targets,
            workspace_index_document(next_index).encode(),
        )
        return CommandResult(
            data={"changed": changed, "written_count": len(written_periods)},
            artifacts={
                "config_json": str(context.paths.config),
                "views": [
                    {"period": period, "path": str(context.paths.views / period)}
                    for period in written_periods
                ],
            },
            warnings=next_derivation.warnings,
        )


def _workspace_config_document(paths: WorkspacePaths) -> dict[str, object]:
    try:
        raw = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace config is not valid JSON."
        ) from error
    if not isinstance(raw, dict):
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace config must be an object."
        )
    return copy.deepcopy(raw)


def _public_config_value(value: object) -> object:
    """Remove private runtime and secret-like keys before terminal output."""
    if isinstance(value, dict):
        return {
            str(key): _public_config_value(item)
            for key, item in value.items()
            if isinstance(key, str)
            and not key.startswith("_")
            and not any(
                token in key.casefold()
                for token in ("password", "secret", "token", "api_key")
            )
        }
    if isinstance(value, list):
        return [_public_config_value(item) for item in value]
    return value


def _resolved_config(
    paths: WorkspacePaths, raw: Mapping[str, object]
) -> dict[str, object]:
    config = copy.deepcopy(dict(raw))
    expected_paths = {
        "profile_mappings": paths.profile_mappings,
        "rules": paths.rules,
        "corrections": paths.corrections,
        "rate_cache": paths.rates,
    }
    for field, default in expected_paths.items():
        value = config.get(field, default.name)
        if not isinstance(value, str) or not value.strip():
            raise WorkspaceCommandError("workspace_input_invalid")
        config[field] = _resolved_workspace_input(paths, value)
    profile_values = config.get("profiles", [])
    if not isinstance(profile_values, list) or not profile_values:
        raise WorkspaceCommandError("workspace_input_invalid")
    resolved_profiles = [
        _resolved_workspace_input(paths, value, require_existing=True)
        for value in profile_values
        if isinstance(value, str) and value
    ]
    if len(resolved_profiles) != len(profile_values):
        raise WorkspaceCommandError("workspace_input_invalid")
    config["profiles"] = resolved_profiles
    config["_identity_config_path"] = paths.config
    config["_identity_workspace_root"] = paths.root
    config["_rate_cache"] = load_rate_cache(Path(str(config["rate_cache"])))
    config["_rate_cache_defaulted"] = False
    return config


def _resolved_workspace_input(
    paths: WorkspacePaths,
    value: str,
    *,
    require_existing: bool = False,
) -> str:
    try:
        resolved = checked_workspace_path(
            paths,
            value,
            must_exist=require_existing,
            require_regular_file=require_existing,
        )
        if not require_existing and resolved.exists():
            resolved = checked_workspace_path(
                paths,
                value,
                require_regular_file=True,
            )
        return str(resolved)
    except WorkspacePathError as error:
        raise WorkspaceCommandError(error.code) from error


def _profile_paths(config: Mapping[str, object]) -> tuple[str, ...]:
    value = config.get("profiles")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkspaceCommandError("workspace_input_invalid")
    return tuple(value)


def _configured_input_path(context: WorkspaceContext, field: str) -> Path:
    """Return one checked user-owned input path from resolved configuration."""
    value = context.config.get(field)
    if not isinstance(value, str):
        raise WorkspaceCommandError("workspace_input_invalid")
    return Path(_resolved_workspace_input(context.paths, value))


def _reject_report_export_target(
    context: WorkspaceContext,
    target: Path,
) -> None:
    """Keep one-off reports away from every durable or generated authority."""
    protected_directories = (
        context.paths.internal,
        context.paths.views,
        context.paths.profiles,
    )
    protected_files = {
        context.paths.config,
        context.paths.corrections,
        context.paths.rules,
        context.paths.rates,
        context.paths.profile_mappings,
        *(
            Path(value)
            for field in ("corrections", "rules", "rate_cache", "profile_mappings")
            if isinstance((value := context.config.get(field)), str)
        ),
        *(Path(value) for value in _profile_paths(context.config)),
    }
    if target in protected_files or any(
        target.is_relative_to(directory) for directory in protected_directories
    ):
        raise WorkspaceCommandError("managed_path_unsafe")


def _checked_import_path(path: Path) -> Path:
    try:
        reject_existing_symlink_components(path)
    except WorkspacePathError as error:
        raise WorkspaceCommandError(error.code) from error
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Import path does not exist."
        ) from error


def _prove_import_files_readable(files: Sequence[Path]) -> None:
    for path in files:
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError as error:
            raise WorkspaceCommandError(
                "workspace_input_invalid", "An import file is not readable."
            ) from error


def _reject_managed_symlinks(paths: WorkspacePaths) -> None:
    for path in (
        paths.internal,
        paths.workspace_index,
        paths.import_records,
        paths.views,
        paths.report_preview,
        paths.profiles,
        paths.corrections,
        paths.rules,
        paths.rates,
        paths.profile_mappings,
        paths.lock,
        paths.journal,
    ):
        if path.is_symlink():
            raise WorkspaceCommandError(
                "managed_path_unsafe",
                f"Managed path must not be a symbolic link: {path.name}",
            )


def _model_enabled(config: Mapping[str, object]) -> bool:
    value = config.get("ollama")
    return isinstance(value, Mapping) and value.get("enabled") is True


def _derivation_contract(config: Mapping[str, object]) -> str:
    return derivation_contract_for_model(model_allowed=_model_enabled(config))


def _preflight_source_assignments(
    context: WorkspaceContext,
    files: Sequence[Path],
    input_root: Path,
    profiles: list[Profile],
    mappings: dict[str, object],
    binding: AccountBinding | None,
    interactive: bool,
    *,
    action: str,
) -> list[_PreflightedSource]:
    """Read stable source bytes and resolve source ownership before numbering."""
    sources: list[
        tuple[
            Path,
            importers.InputSourceSnapshot,
            IncomingSourceIdentity,
            Profile,
        ]
    ] = []
    for path in files:
        try:
            snapshot = importers._capture_input_source(path, context.config)
        except (OSError, ValueError) as error:
            raise WorkspaceCommandError("workspace_input_invalid") from error
        selected_profile = _preflight_source_profile(
            path,
            snapshot,
            profiles,
            mappings,
            binding,
            interactive,
            context.config,
        )
        contract_id = extractor_contract_id(
            1
            if path.suffix.casefold() == ".csv"
            else importers._pdf_adapter_tag(selected_profile),
            selected_profile,
        )
        identity = IncomingSourceIdentity(
            stable_handle=str(snapshot.resolved_path),
            source_display=importers._relative_source(path, input_root),
            namespace_id=source_namespace_id(
                snapshot.locator_kind,
                snapshot.locator,
            ),
            revision=source_revision(snapshot.source_bytes),
            contract_id=contract_id,
        )
        sources.append((path, snapshot, identity, selected_profile))
    try:
        resolution = resolve_sources(
            context.index["identity_manifest"],
            (),
            tuple(
                workspace_source_identity(
                    identity,
                    evidence_key=_content_proof_key(context),
                )
                for _path, _snapshot, identity, _profile in sources
            ),
            action,
        )
    except IdentityError as error:
        code = (
            "source_already_imported"
            if error.code == "identity_source_already_imported"
            else error.code
        )
        raise WorkspaceCommandError(code) from error
    resolved_by_handle = {
        assignment.stable_handle: assignment.source_id
        for assignment in resolution.assignments
    }
    return [
        _PreflightedSource(
            path=path,
            source_id=resolved_by_handle[identity.stable_handle],
            source_label=safe_source_label(
                resolved_by_handle[identity.stable_handle], path.suffix
            ),
            identity=identity,
            snapshot=snapshot,
            selected_profile=selected_profile,
        )
        for path, snapshot, identity, selected_profile in sources
    ]


def _preflight_source_profile(
    path: Path,
    snapshot: importers.InputSourceSnapshot,
    profiles: list[Profile],
    mappings: dict[str, object],
    binding: AccountBinding | None,
    interactive: bool,
    config: Mapping[str, object],
) -> Profile:
    """Choose a usable parser profile without writing user mappings."""
    if binding is not None:
        return importers._explicit_binding_profile(path, profiles, binding)
    try:
        if path.suffix.casefold() == ".csv":
            profile, _prompted = importers._select_csv_profile(
                path,
                profiles,
                interactive,
                mappings,
                lambda: None,
                source_bytes=snapshot.source_bytes,
            )
            return profile
        if path.suffix.casefold() == ".pdf":
            pdf = config.get("pdf")
            if isinstance(pdf, Mapping) and pdf.get("enabled") is False:
                raise WorkspaceCommandError("workspace_input_invalid")
            return importers._select_pdf_profile(
                path,
                profiles,
                interactive,
                mappings,
                None,
                lambda: None,
            )
    except (TypeError, UnicodeError, ValueError) as error:
        raise WorkspaceCommandError("workspace_input_invalid") from error
    raise WorkspaceCommandError("workspace_input_invalid")


def _reject_successful_plain_repeats(
    context: WorkspaceContext, files: Sequence[Path], action: str
) -> None:
    if action != "import":
        return
    namespaces = {
        source["source_namespace_id"]
        for source in context.index["identity_manifest"]["sources"]
    }
    if not namespaces:
        return
    from honeymoney.identity import logical_locator, source_namespace_id

    for path in files:
        kind, locator = logical_locator(path, context.paths.root)
        if source_namespace_id(kind, locator) in namespaces:
            raise WorkspaceCommandError("source_already_imported")


def _parse_source(
    context: WorkspaceContext,
    preflighted_source: _PreflightedSource,
    input_root: Path,
    profiles: list[Profile],
    mappings: dict[str, object],
    binding: AccountBinding | None,
    interactive: bool,
) -> _ParsedSource:
    path = preflighted_source.path
    imported = importers._import_transactions(
        [path],
        profiles,
        context.config,
        input_root,
        interactive,
        mappings,
        None,
        explicit_binding=binding,
        include_identity_sources=True,
        source_snapshots={path: preflighted_source.snapshot},
        preselected_profiles={path: preflighted_source.selected_profile},
    )
    rows, warnings, _reports, identities = imported
    if len(identities) != 1:
        raise WorkspaceCommandError("import_failed")
    identity = identities[0]
    return _ParsedSource(
        path=path,
        source_id=preflighted_source.source_id,
        source_label=preflighted_source.source_label,
        rows=tuple(dict(row) for row in rows),
        identity=identity,
        warnings=tuple(warnings),
    )


def _plan_source_attempts(
    context: WorkspaceContext,
    sources: Sequence[_PreflightedSource],
    *,
    action: str,
    started: str,
) -> list[_PlannedSourceAttempt]:
    planned: list[_PlannedSourceAttempt] = []
    reserved_by_record: dict[Path, int] = {}
    for source in sources:
        path = source.path
        source_id = source.source_id
        record = import_record_path(context.paths.import_records, source_id)
        try:
            attempt_number = next_attempt_number(record) + reserved_by_record.get(
                record, 0
            )
        except ValueError as error:
            if getattr(error, "unsafe_path", False):
                raise WorkspaceCommandError("managed_path_unsafe") from error
            raise
        reserved_by_record[record] = reserved_by_record.get(record, 0) + 1
        report = _attempt_report(
            source_id=source_id,
            source_label=source.source_label,
            attempt_number=attempt_number,
            action=action,
            started=started,
            outcome="failure",
            source_revision=source.identity.revision,
            parser_contract=source.identity.contract_id,
            transaction_count=0,
            warnings=(),
            errors=("interrupted",),
        )
        base = record.relative_to(context.paths.root).as_posix()
        planned.append(
            _PlannedSourceAttempt(
                path=path,
                source_id=source_id,
                record=record,
                attempt_number=attempt_number,
                source_revision=source.identity.revision,
                parser_contract=source.identity.contract_id,
                reservation=PendingAttemptReservation(
                    path=f"{base}/attempts/{attempt_number:08d}.json",
                    interrupted_content=attempt_document(report).encode(),
                ),
            )
        )
    return planned


def _prepare_failed_source_attempts(
    context: WorkspaceContext,
    failures: Sequence[tuple[Path, Exception]],
    planned_attempts: Sequence[_PlannedSourceAttempt],
    sources: Sequence[_PreflightedSource],
    *,
    action: str,
    started: str,
) -> list[_FailedSourceAttempt]:
    prepared: list[_FailedSourceAttempt] = []
    planned_by_path = {item.path: item for item in planned_attempts}
    source_by_path = {item.path: item for item in sources}
    for path, error in failures:
        planned = planned_by_path[path]
        source = source_by_path[path]
        source_id = planned.source_id
        record = planned.record
        number = planned.attempt_number
        report = _attempt_report(
            source_id=source_id,
            source_label=source.source_label,
            attempt_number=number,
            action=action,
            started=started,
            outcome="failure",
            source_revision=planned.source_revision,
            parser_contract=planned.parser_contract,
            transaction_count=0,
            warnings=(),
            errors=(_safe_attempt_error_code(error),),
        )
        existing_attempts = load_attempts(record)
        if existing_attempts:
            summary = build_summary(record, source_id)
        else:
            summary = {
                "schema_version": IMPORT_RECORD_SCHEMA_VERSION,
                "source_id": source_id,
                "source_label": source.source_label,
                "ready": False,
                "current_attempt_number": None,
                "statement_transaction_count": 0,
            }
        base = record.relative_to(context.paths.root).as_posix()
        prepared.append(
            _FailedSourceAttempt(
                source_id=source_id,
                record=record,
                reservation=FixedAttemptReservation(
                    path=f"{base}/attempts/{number:08d}.json",
                    failure_content=attempt_document(report).encode(),
                ),
                summary_content=summary_document(summary).encode(),
            )
        )
    return prepared


def _publish_fixed_import_attempts(
    context: WorkspaceContext,
    generation_id: str,
    failed_attempts: Sequence[_FailedSourceAttempt],
) -> None:
    """Publish known failed attempts without changing workspace state."""
    publish_reserved_failures(
        context.paths.root,
        generation_id,
        [
            PublicationTarget(
                f"{failed.record.relative_to(context.paths.root).as_posix()}"
                "/summary.json",
                failed.summary_content,
            )
            for failed in failed_attempts
        ],
        attempt_reports=[failed.reservation for failed in failed_attempts],
    )


def _fixed_post_reservation_error(error: Exception) -> WorkspaceCommandError:
    """Map known import work failures to safe public codes."""
    if isinstance(error, IdentityError):
        code = (
            "source_already_imported"
            if error.code == "identity_source_already_imported"
            else error.code
        )
        return WorkspaceCommandError(code)
    if isinstance(error, WorkspaceCommandError):
        return WorkspaceCommandError(_safe_attempt_error_code(error))
    code = _safe_attempt_error_code(error)
    if code == "parse_failed":
        code = "workspace_input_invalid"
    return WorkspaceCommandError(code)


def _finalize_post_reservation_import_failure(
    context: WorkspaceContext,
    generation_id: str,
    planned_attempts: Sequence[_PlannedSourceAttempt],
    sources: Sequence[_PreflightedSource],
    *,
    action: str,
    started: str,
    error: WorkspaceCommandError,
) -> None:
    """Finalize every reserved attempt when later import work rejects the batch."""
    failed_attempts = _prepare_failed_source_attempts(
        context,
        [(source.path, error) for source in sources],
        planned_attempts,
        sources,
        action=action,
        started=started,
    )
    _publish_fixed_import_attempts(context, generation_id, failed_attempts)


def _safe_attempt_error_code(error: Exception) -> str:
    value = getattr(error, "code", None)
    if (
        isinstance(value, str)
        and value
        and len(value) <= 64
        and value.replace("_", "a").isalnum()
    ):
        return value
    return "parse_failed"


def _load_ready_source_rows(context: WorkspaceContext) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    evidence_key = _content_proof_key(context)
    for source in context.index["identity_manifest"]["sources"]:
        source_id = source["source_id"]
        record = import_record_path(context.paths.import_records, source_id)
        try:
            summary = build_summary(record, source_id)
            attempts = load_attempts(record)
            snapshot = read_transaction_snapshot(
                record / "transactions.csv", SNAPSHOT_COLUMNS
            )
        except ValueError as error:
            code = (
                "managed_path_unsafe"
                if getattr(error, "unsafe_path", False)
                else "durable_state_conflict"
            )
            raise WorkspaceCommandError(code) from error
        if not summary["ready"] or summary["current_attempt_number"] is None:
            raise WorkspaceCommandError("durable_state_conflict")
        attempt = next(
            item
            for item in attempts
            if item["attempt_number"] == summary["current_attempt_number"]
        )
        if (
            workspace_source_revision(
                attempt["source_revision"], evidence_key=evidence_key
            )
            != source["source_revision"]
            or attempt["parser_contract"] != source["extractor_contract_id"]
        ):
            raise WorkspaceCommandError("durable_state_conflict")
        active = {
            item["source_record_id"]: item
            for item in source["records"]
            if item["state"] == "active"
        }
        if set(active) != {item["source_record_id"] for item in snapshot}:
            raise WorkspaceCommandError("durable_state_conflict")
        for item in snapshot:
            owner = active[item["source_record_id"]]
            if (
                workspace_record_fingerprint(item, evidence_key=evidence_key)
                != owner["record_fingerprint"]
            ):
                raise WorkspaceCommandError("durable_state_conflict")
            row = {column: "" for column in SOURCE_OCCURRENCE_COLUMNS}
            row.update(item)
            row.update(
                {
                    "transaction_id": owner["transaction_id"],
                    "source_id": source_id,
                    "source_namespace_id": source["source_namespace_id"],
                    "source_revision": source["source_revision"],
                    "category": "Unknown",
                    "flow_type": "unresolved",
                    "flow_source": "deterministic",
                    "reconciliation_status": "not_applicable",
                    "confidence": "0.00",
                    "needs_review": "true",
                    "review_reasons": "category_decision;accounting_flow",
                    "reason": "No categorization rules have been applied",
                    "flags": "uncategorized",
                    "source_file": summary["source_label"],
                }
            )
            _restore_profile_facts(row, context.config)
            rows.append(row)
    return rows


def _restore_profile_facts(row: dict[str, str], config: Mapping[str, object]) -> None:
    profiles = _load_workspace_profiles(dict(config))
    profile = next(
        (
            item
            for item in profiles
            if str(item.get("account_id", "")) == row.get("account_id", "")
        ),
        None,
    )
    if profile is not None:
        row["owner"] = str(profile.get("owner", "Household"))
        row["payment_method"] = str(profile.get("payment_method", "Unknown"))


def _snapshot_row(row: Mapping[str, object]) -> dict[str, str]:
    return {column: str(row.get(column, "")) for column in SNAPSHOT_COLUMNS}


def _attempt_report(
    *,
    source_id: str,
    source_label: str,
    attempt_number: int,
    action: str,
    started: str,
    outcome: str,
    source_revision: str,
    parser_contract: str,
    transaction_count: int,
    warnings: Sequence[str],
    errors: Sequence[str],
    transactions_digest: str | None = None,
) -> dict[str, object]:
    kept_warnings = [_safe_attempt_warning_code(str(item)) for item in warnings[:32]]
    kept_errors = [str(item) for item in errors[:32]]
    report: dict[str, object] = {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "honeymoney_version": HONEYMONEY_VERSION,
        "source_id": source_id,
        "source_label": source_label,
        "attempt_number": attempt_number,
        "requested_action": action,
        "started_at": started,
        "finished_at": _timestamp(),
        "outcome": outcome,
        "source_revision": source_revision,
        "parser_contract": parser_contract,
        "counts": {"statement_transaction_count": transaction_count},
        "warnings": kept_warnings,
        "warning_count": len(warnings),
        "omitted_warning_count": len(warnings) - len(kept_warnings),
        "error_codes": kept_errors,
        "error_count": len(errors),
        "omitted_error_count": len(errors) - len(kept_errors),
    }
    if outcome == "success":
        report.update(
            {
                "transactions_schema_version": TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
                "transactions_digest": transactions_digest,
            }
        )
    return report


def _safe_attempt_warning_code(warning: str) -> str:
    text = warning.casefold()
    if "no table found" in text:
        return "pdf_table_not_found"
    if "skipped table" in text:
        return "pdf_table_skipped"
    if "text fallback" in text:
        return "pdf_text_fallback_used"
    if "no word transaction table" in text:
        return "pdf_word_table_not_found"
    return "parser_warning"


def _input_proofs(
    context: WorkspaceContext,
    *,
    overrides: Mapping[Path, bytes] | None = None,
) -> list[InputProof]:
    namespace = context.index["overlap_manifest"]["namespace_key"]
    key = bytes.fromhex(namespace.removeprefix("ovns_"))
    inputs = [
        ("config", context.paths.config),
        ("corrections", _configured_input_path(context, "corrections")),
        ("mappings", _configured_input_path(context, "profile_mappings")),
        ("rates", _configured_input_path(context, "rate_cache")),
        ("rules", _configured_input_path(context, "rules")),
        *(
            (f"profile-{index:04d}", Path(path))
            for index, path in enumerate(_profile_paths(context.config))
        ),
    ]
    result: list[InputProof] = []
    replacements = overrides or {}
    for name, path in sorted(inputs):
        try:
            content = replacements[path] if path in replacements else path.read_bytes()
        except FileNotFoundError:
            content = b"honeymoney-input-missing-v1"
        except OSError as error:
            raise WorkspaceCommandError("workspace_input_invalid") from error
        proof = hmac.new(
            key,
            b"honeymoney-input-proof-v1\0" + name.encode("ascii") + b"\0" + content,
            hashlib.sha256,
        ).hexdigest()
        result.append({"name": name, "proof": proof})
    return result


def _reset_supported_corrections(
    corrections: Mapping[str, Mapping[str, str]],
    overlap_manifest: Mapping[str, object],
    reset_source_ids: set[str],
    reset_transaction_ids: set[str],
) -> tuple[dict[str, dict[str, str]], bytes | None]:
    """Clear only choices whose complete current support is being reset."""
    retained = {identifier: dict(patch) for identifier, patch in corrections.items()}
    removable = set(reset_transaction_ids)
    groups = overlap_manifest.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            pools = group.get("support_pools")
            slots = group.get("slots")
            if not isinstance(pools, list) or not isinstance(slots, list):
                continue
            supporting_sources = {
                str(pool.get("source_id", ""))
                for pool in pools
                if isinstance(pool, dict) and pool.get("source_id")
            }
            if supporting_sources and supporting_sources <= reset_source_ids:
                removable.update(
                    str(slot.get("transaction_id", ""))
                    for slot in slots
                    if isinstance(slot, dict)
                    and slot.get("state") == "active"
                    and slot.get("transaction_id")
                )
    for identifier in removable:
        retained.pop(identifier, None)
    if retained == corrections:
        return retained, None
    rows = [
        {
            column: identifier if column == "transaction_id" else patch.get(column, "")
            for column in CORRECTION_COLUMNS
        }
        for identifier, patch in sorted(retained.items())
    ]
    return retained, csv_document(CORRECTION_COLUMNS, rows).encode()


def _require_current_input_proofs(context: WorkspaceContext) -> None:
    stored = context.index["input_proofs"]
    if stored and stored != _input_proofs(context):
        raise WorkspaceCommandError(
            "full_rebuild_required",
            "Workspace inputs changed; run `honeymoney views rebuild --all`.",
        )


def _require_narrow_rebuild_proofs(
    context: WorkspaceContext,
    current: list[InputProof],
    selection_kind: str,
) -> None:
    if (
        selection_kind != "all"
        and context.index["input_proofs"]
        and context.index["input_proofs"] != current
    ):
        raise WorkspaceCommandError(
            "full_rebuild_required",
            "Workspace inputs changed; run `honeymoney views rebuild --all`.",
        )


def _derive_current_workspace(
    context: WorkspaceContext,
) -> tuple[list[dict[str, str]], WorkspaceDerivation]:
    source_rows = _load_ready_source_rows(context)
    try:
        mappings = _load_workspace_profile_mappings(context.config)
        derivation = derive_workspace_rows(
            source_rows,
            context.index["overlap_manifest"],
            context.config,
            rules=_load_workspace_rules(context.config),
            corrections=_load_workspace_corrections(context.config),
            profile_mappings=mappings,
            allow_model=_model_enabled(context.config),
        )
    except WorkspaceCommandError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkspaceCommandError(
            "workspace_input_invalid", "Workspace inputs are invalid."
        ) from error
    return source_rows, derivation


def derive_workspace_for_repair(
    config_path: str | Path,
) -> tuple[WorkspaceContext, WorkspaceDerivation]:
    """Rebuild derived rows from durable state without calling the local model."""
    context = load_workspace(config_path)
    source_rows = _load_ready_source_rows(context)
    mappings = _load_workspace_profile_mappings(context.config)
    derivation = derive_workspace_rows(
        source_rows,
        context.index["overlap_manifest"],
        context.config,
        rules=_load_workspace_rules(context.config),
        corrections=_load_workspace_corrections(context.config),
        profile_mappings=mappings,
        allow_model=False,
    )
    return context, derivation


def _content_proof_key(context: WorkspaceContext) -> bytes:
    return bytes.fromhex(
        context.index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
    )


def _plan_views(
    context: WorkspaceContext,
    previous: WorkspaceDerivation,
    next_derivation: WorkspaceDerivation,
) -> tuple[list[PublicationTarget], list[RegisteredView], tuple[str, ...]]:
    # Kept local so the durable command service does not depend on view internals.
    from honeymoney.workspace_views import plan_automatic_view_refresh

    key = _content_proof_key(context)
    plan = plan_automatic_view_refresh(
        previous_rows=previous.rows,
        next_rows=next_derivation.rows,
        registered_views=context.index["registered_views"],
        content_proof_key=key,
        installed_files=_installed_view_files(context.paths),
        previous_report_inputs=_view_report_inputs_by_period(previous),
        next_report_inputs=_view_report_inputs_by_period(next_derivation),
    )
    targets = [
        PublicationTarget(item.path, item.content) for item in plan.publication_files()
    ]
    return (
        targets,
        list(plan.next_registered_views),
        tuple(unit.period for unit in plan.writes),
    )


def _view_report_inputs_by_period(
    derivation: WorkspaceDerivation,
) -> dict[str, ViewReportInputs]:
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for row in derivation.rows:
        grouped.setdefault(view_period_for_row(row), []).append(row)
    return {
        period: view_report_inputs(derivation, rows) for period, rows in grouped.items()
    }


def _query_derivation(
    derivation: WorkspaceDerivation,
    selection: PeriodSelection,
) -> WorkspaceQuery:
    initial = query_workspace_rows(derivation.rows, selection)
    return query_workspace_rows(
        derivation.rows,
        selection,
        report_inputs=view_report_inputs(derivation, initial.rows),
    )


def _view_result(context: WorkspaceContext, plan: WorkspaceViewPlan) -> CommandResult:
    return CommandResult(
        data={
            "written_count": len(plan.writes),
            "removed_count": len(plan.removals),
            "unchanged_count": len(plan.unchanged),
        },
        artifacts={
            "views": [
                {
                    "period": unit.period,
                    "path": str(context.paths.views / unit.period),
                }
                for unit in plan.writes
            ]
        },
    )


def _installed_view_files(paths: WorkspacePaths) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if not paths.views.exists():
        return files
    for period in sorted(paths.views.iterdir()):
        if not period.is_dir() or period.is_symlink():
            continue
        for name in ("transactions.csv", "review_needed.csv", "report.html"):
            target = period / name
            if target.is_file() and not target.is_symlink():
                files[target.relative_to(paths.root).as_posix()] = target.read_bytes()
    return files


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
