"""Read-only workspace diagnosis and proof-based repair coordination.

The module deliberately has no parser, model, or network dependency.  It only
reads managed workspace state and user-owned configuration files.
"""

from __future__ import annotations

import csv
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Mapping, cast

from honeymoney.account_bindings import validate_profile_mappings
from honeymoney.corrections import CORRECTION_COLUMNS, validate_correction
from honeymoney.identity import workspace_record_fingerprint, workspace_source_revision
from honeymoney.import_records import (
    SOURCE_ID_PATTERN,
    ImportRecordError,
    build_summary,
    load_attempts,
    read_transaction_snapshot,
    summary_document,
)
from honeymoney.periods import view_period_for_row
from honeymoney.rates import validate_rate_cache
from honeymoney.rules import validate_rules
from honeymoney.workspace_index import (
    WorkspaceIndexError,
    derivation_contract_is_rederivable,
    load_compatible_workspace_index,
    workspace_index_document,
)
from honeymoney.workspace_paths import WorkspacePaths, legacy_workspace_markers
from honeymoney.workspace_publication import (
    PublicationDirectory,
    PublicationError,
    PublicationTarget,
    WorkspaceLock,
    inspect_lock,
    inspect_retained_publication,
    publish_generation,
    settle_retained_publication,
)
from honeymoney.workspace_views import (
    VIEW_FILE_NAMES,
    ViewUnit,
    WorkspaceViewError,
    build_view_unit,
    view_content_proof,
)

_SAFE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_ATTEMPT_FILE_NAME = re.compile(r"[0-9]{8}\.json\Z")
_VIEW_PERIOD = re.compile(r"(?:[0-9]{4}-(?:0[1-9]|1[0-2])|undated)\Z")
_MAX_FINDINGS = 128


class FindingSeverity(StrEnum):
    """The stable severity assigned to one doctor finding."""

    WARNING = "warning"
    ERROR = "error"


class RepairClass(StrEnum):
    """The only repair routes doctor may report."""

    NONE = "none"
    MANUAL = "manual"
    FULL_REBUILD = "full_rebuild"
    SAFE = "safe"
    RECOVERY = "recovery"


class RepairActionKind(StrEnum):
    """One concrete change that a checked repair plan may apply."""

    CREATE_PRIVATE_DIRECTORY = "create_private_directory"
    REBUILD_SUMMARY = "rebuild_summary"
    REBUILD_GENERATED_VIEW = "rebuild_generated_view"
    REMOVE_STALE_LOCK = "remove_stale_lock"
    SETTLE_RETAINED_PUBLICATION = "settle_retained_publication"
    SET_PRIVATE_MODE = "set_private_mode"


@dataclass(frozen=True)
class DoctorFinding:
    """One privacy-safe, stable diagnosis."""

    code: str
    severity: FindingSeverity
    repair_class: RepairClass
    path: str | None
    next_action: str
    detail_count: int = 0
    omitted_detail_count: int = 0


@dataclass(frozen=True)
class AuditResult:
    """The complete, sorted result of one read-only workspace audit."""

    root: Path
    findings: tuple[DoctorFinding, ...]
    checked_item_count: int
    omitted_finding_count: int = 0

    @property
    def healthy(self) -> bool:
        return not self.findings

    @property
    def exit_code(self) -> int:
        return 0 if self.healthy else 2


@dataclass(frozen=True)
class RepairAction:
    """One exact byte replacement derived from durable authority."""

    kind: RepairActionKind
    path: str
    source_finding_code: str
    content: bytes | None = dataclass_field(default=None, repr=False)
    mode: int | None = None


@dataclass(frozen=True)
class RepairPlan:
    """A complete repair plan that has not changed the workspace."""

    root: Path
    audit: AuditResult
    actions: tuple[RepairAction, ...]
    blocked: bool
    blocker_codes: tuple[str, ...]


@dataclass(frozen=True)
class FixResult:
    """The result of applying one proof-based repair plan and re-auditing."""

    before: AuditResult
    plan: RepairPlan
    applied_actions: tuple[RepairAction, ...]
    after: AuditResult

    @property
    def healthy(self) -> bool:
        return self.after.healthy


@dataclass(frozen=True)
class _ReadyRecordAuthority:
    source_revision: str
    parser_contract: str
    snapshot_rows: tuple[dict[str, str], ...]


def audit_workspace(root: Path | str) -> AuditResult:
    """Audit a workspace without changing it or opening statement input paths."""
    root_path = Path(os.path.abspath(Path(root).expanduser()))
    if root_path.is_symlink():
        return AuditResult(
            root=root_path,
            findings=(
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    None,
                    "Use the workspace directory itself, not a symbolic link.",
                ),
            ),
            checked_item_count=0,
        )
    if not _path_lexists(root_path):
        return AuditResult(
            root=root_path,
            findings=(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    None,
                    "Create or restore the workspace directory.",
                ),
            ),
            checked_item_count=0,
        )
    if not root_path.is_dir():
        return AuditResult(
            root=root_path,
            findings=(
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    None,
                    "Use a workspace directory, not a non-directory path.",
                ),
            ),
            checked_item_count=0,
        )
    paths = WorkspacePaths.from_root(root_path)
    if _path_lexists(paths.internal) and (
        paths.internal.is_symlink() or not paths.internal.is_dir()
    ):
        return _audit_result(
            paths,
            [
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    ".honeymoney",
                    "Remove the unsafe path or restore a complete workspace backup.",
                )
            ],
            0,
        )
    if _path_lexists(paths.lock) and paths.lock.is_symlink():
        return _audit_result(
            paths,
            [
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    ".honeymoney/workspace.lock",
                    "Remove the unsafe path or restore a complete workspace backup.",
                )
            ],
            0,
        )
    findings: list[DoctorFinding] = []
    checked_item_count = 0
    index: Mapping[str, object] | None = None
    config: dict[str, object] | None = None
    record_ids: set[str] = set()
    ready_record_ids: set[str] = set()
    ready_record_authorities: dict[str, _ReadyRecordAuthority] = {}

    lock_state = inspect_lock(paths.root)
    if lock_state == "live":
        return _audit_result(
            paths,
            [
                DoctorFinding(
                    "workspace_busy",
                    FindingSeverity.ERROR,
                    RepairClass.NONE,
                    ".honeymoney/workspace.lock",
                    "Wait for the active command to finish.",
                )
            ],
            checked_item_count + 1,
        )
    if lock_state == "unknown":
        return _audit_result(
            paths,
            [
                DoctorFinding(
                    "lock_owner_unknown",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    ".honeymoney/workspace.lock",
                    "Restore a complete workspace backup.",
                )
            ],
            checked_item_count + 1,
        )
    if lock_state == "stale":
        findings.append(
            DoctorFinding(
                "stale_lock",
                FindingSeverity.WARNING,
                RepairClass.SAFE,
                ".honeymoney/workspace.lock",
                "Run doctor --fix to remove the stopped lock.",
            )
        )
    checked_item_count += 1

    if _path_lexists(paths.journal):
        if paths.journal.is_symlink():
            return _audit_result(
                paths,
                [
                    *findings,
                    DoctorFinding(
                        "publication_state_invalid",
                        FindingSeverity.ERROR,
                        RepairClass.MANUAL,
                        ".honeymoney/publication-journal.json",
                        "Restore a complete workspace backup.",
                    ),
                ],
                checked_item_count,
            )
        try:
            winner = inspect_retained_publication(paths.root)
        except PublicationError:
            return _audit_result(
                paths,
                [
                    *findings,
                    DoctorFinding(
                        "publication_state_invalid",
                        FindingSeverity.ERROR,
                        RepairClass.MANUAL,
                        ".honeymoney/publication-journal.json",
                        "Restore a complete workspace backup.",
                    ),
                ],
                checked_item_count + 1,
            )
        if winner in {"old", "new"}:
            return _audit_result(
                paths,
                [
                    *findings,
                    DoctorFinding(
                        "publication_recovery_required",
                        FindingSeverity.ERROR,
                        RepairClass.RECOVERY,
                        ".honeymoney/publication-journal.json",
                        "Run doctor --fix to settle the retained publication.",
                    ),
                ],
                checked_item_count + 1,
            )

    if paths.internal.is_dir():
        _audit_private_mode(paths, paths.internal, 0o700, findings)
    _audit_private_mode(paths, paths.root, 0o700, findings)

    if paths.config.is_symlink():
        findings.append(
            DoctorFinding(
                "managed_path_unsafe",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                "config.json",
                "Remove the unsafe path or restore a complete workspace backup.",
            )
        )
    elif not paths.config.is_file():
        findings.append(
            DoctorFinding(
                "workspace_input_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                "config.json",
                "Restore or correct config.json.",
            )
        )
    else:
        _audit_private_mode(paths, paths.config, 0o600, findings)
        try:
            config = _read_json_object(paths.config)
        except OSError, UnicodeError, json.JSONDecodeError, ValueError:
            findings.append(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    "config.json",
                    "Restore or correct config.json.",
                )
            )
        else:
            checked_item_count += 1

    if config is not None:
        checked_item_count += _audit_configured_inputs(paths, config, findings)
    checked_item_count += _audit_legacy_markers(paths, findings)

    if paths.import_records.is_symlink():
        findings.append(
            DoctorFinding(
                "managed_path_unsafe",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                ".honeymoney/import-records",
                "Remove the unsafe path or restore a complete workspace backup.",
            )
        )
    elif not paths.import_records.is_dir():
        findings.append(
            DoctorFinding(
                "managed_metadata_invalid",
                FindingSeverity.ERROR,
                RepairClass.SAFE,
                ".honeymoney/import-records",
                "Run doctor --fix to restore the managed directory.",
            )
        )
    else:
        checked_item_count += 1
        _audit_private_mode(paths, paths.import_records, 0o700, findings)

    if paths.workspace_index.is_symlink():
        findings.append(
            DoctorFinding(
                "managed_path_unsafe",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                ".honeymoney/workspace-index.json",
                "Remove the unsafe path or restore a complete workspace backup.",
            )
        )
    else:
        _audit_private_mode(paths, paths.workspace_index, 0o600, findings)
        try:
            index = load_compatible_workspace_index(paths.workspace_index)
        except WorkspaceIndexError as error:
            code = (
                "newer_honeymoney_required"
                if error.code == "newer_honeymoney_required"
                else "workspace_index_invalid"
            )
            findings.append(
                DoctorFinding(
                    code,
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    ".honeymoney/workspace-index.json",
                    "Restore a complete workspace backup.",
                )
            )
        else:
            checked_item_count += 1

    if paths.import_records.is_dir() and not paths.import_records.is_symlink():
        checked_item_count += _audit_import_record_summaries(
            paths,
            findings,
            record_ids,
            ready_record_ids,
            ready_record_authorities,
        )
    if paths.internal.is_dir():
        checked_item_count += _audit_unknown_internal_entries(paths, findings)
    if index is not None:
        checked_item_count += _audit_input_proofs(
            paths,
            index,
            config,
            findings,
        )
        checked_item_count += _audit_corrections(paths, config, index, findings)
        checked_item_count += _audit_durable_authority_agreement(
            paths,
            index,
            record_ids,
            ready_record_ids,
            ready_record_authorities,
            findings,
        )
        checked_item_count += _audit_registered_views(paths, index, findings)

    return _audit_result(paths, findings, checked_item_count)


def build_repair_plan(
    root: Path | str,
    *,
    audit: AuditResult | None = None,
) -> RepairPlan:
    """Build a complete, read-only repair plan from the current audit result."""
    current = audit or audit_workspace(root)
    hard_codes = {
        "workspace_busy",
        "lock_owner_unknown",
        "publication_state_invalid",
        "workspace_input_invalid",
        "workspace_index_invalid",
        "import_record_invalid",
        "attempt_history_invalid",
        "corrections_invalid",
        "durable_state_conflict",
        "managed_path_unsafe",
        "newer_honeymoney_required",
    }
    blockers = tuple(
        sorted(
            {finding.code for finding in current.findings if finding.code in hard_codes}
        )
    )
    if blockers:
        return RepairPlan(current.root, current, (), True, blockers)

    paths = WorkspacePaths.from_root(current.root)

    if any(
        finding.code == "publication_recovery_required" for finding in current.findings
    ):
        return RepairPlan(
            paths.root,
            current,
            (
                RepairAction(
                    RepairActionKind.SETTLE_RETAINED_PUBLICATION,
                    ".honeymoney/publication-journal.json",
                    "publication_recovery_required",
                ),
            ),
            False,
            (),
        )

    actions: list[RepairAction] = []
    for finding in current.findings:
        action: RepairAction | None
        if finding.path is None:
            continue
        if finding.code == "stale_lock":
            action = RepairAction(
                RepairActionKind.REMOVE_STALE_LOCK,
                finding.path,
                "stale_lock",
            )
        elif finding.code == "summary_invalid":
            action = _summary_repair_action(paths, finding.path)
        elif finding.code == "managed_metadata_invalid":
            action = _mode_repair_action(paths, finding.path)
        else:
            action = None
        if action is not None:
            actions.append(action)
    actions.extend(_generated_view_repair_actions(paths, current.findings))
    return RepairPlan(
        paths.root,
        current,
        tuple(sorted(actions, key=lambda action: (action.kind, action.path))),
        False,
        (),
    )


def fix_workspace(root: Path | str) -> FixResult:
    """Apply only proved repairs and always return the post-fix audit."""
    before = audit_workspace(root)
    plan = build_repair_plan(root, audit=before)
    if plan.blocked or not plan.actions:
        return FixResult(before, plan, (), audit_workspace(root))

    if plan.actions[0].kind == RepairActionKind.SETTLE_RETAINED_PUBLICATION:
        try:
            settle_retained_publication(plan.root)
        except PublicationError:
            return FixResult(before, plan, (), audit_workspace(plan.root))
        after_recovery = audit_workspace(plan.root)
        follow_up = build_repair_plan(plan.root, audit=after_recovery)
        if follow_up.blocked or not follow_up.actions:
            return FixResult(
                before,
                plan,
                plan.actions,
                after_recovery,
            )
        return _apply_repair_plan(before, follow_up, prior_actions=plan.actions)

    return _apply_repair_plan(before, plan)


def _apply_repair_plan(
    before: AuditResult,
    plan: RepairPlan,
    *,
    prior_actions: tuple[RepairAction, ...] = (),
) -> FixResult:
    paths = WorkspacePaths.from_root(plan.root)
    applied_actions = list(prior_actions)
    remaining_actions: list[RepairAction] = []
    for action in plan.actions:
        if action.kind != RepairActionKind.REMOVE_STALE_LOCK:
            remaining_actions.append(action)
            continue
        if not _remove_proved_stale_lock(paths):
            return FixResult(
                before,
                plan,
                tuple(applied_actions),
                audit_workspace(paths.root),
            )
        applied_actions.append(action)
    if not remaining_actions:
        return FixResult(
            before,
            plan,
            tuple(applied_actions),
            audit_workspace(paths.root),
        )
    targets_by_path: dict[str, PublicationTarget] = {}
    for action in remaining_actions:
        if action.content is None:
            continue
        target = PublicationTarget(action.path, action.content)
        if action.kind == RepairActionKind.SET_PRIVATE_MODE:
            targets_by_path.setdefault(action.path, target)
        else:
            targets_by_path[action.path] = target
    targets = list(targets_by_path.values())
    directory_targets = _repair_directory_targets(paths, remaining_actions, targets)
    with WorkspaceLock(paths.root):
        index = load_compatible_workspace_index(paths.workspace_index)
        next_index = cast(dict[str, object], json.loads(json.dumps(index)))
        generation_id = f"gen_{secrets.token_hex(32)}"
        next_index["generation_id"] = generation_id
        if directory_targets:
            publish_generation(
                paths.root,
                generation_id,
                targets,
                workspace_index_document(next_index).encode("utf-8"),
                directory_targets=directory_targets,
            )
        else:
            publish_generation(
                paths.root,
                generation_id,
                targets,
                workspace_index_document(next_index).encode("utf-8"),
            )
    return FixResult(
        before,
        plan,
        (*applied_actions, *remaining_actions),
        audit_workspace(paths.root),
    )


def _audit_result(
    paths: WorkspacePaths,
    findings: list[DoctorFinding],
    checked_item_count: int,
) -> AuditResult:
    ordered = sorted(findings, key=lambda finding: (finding.code, finding.path or ""))
    unique = list(dict.fromkeys(ordered))
    return AuditResult(
        root=paths.root,
        findings=tuple(unique[:_MAX_FINDINGS]),
        checked_item_count=checked_item_count,
        omitted_finding_count=max(0, len(unique) - _MAX_FINDINGS),
    )


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_proved_stale_lock(paths: WorkspacePaths) -> bool:
    if inspect_lock(paths.root) != "stale":
        return False
    if paths.lock.is_symlink():
        return False
    try:
        paths.lock.unlink()
    except OSError:
        return False
    _fsync_directory(paths.internal)
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def _audit_configured_inputs(
    paths: WorkspacePaths,
    config: Mapping[str, object],
    findings: list[DoctorFinding],
) -> int:
    checked = 0
    if "paths" in config:
        findings.append(
            DoctorFinding(
                "workspace_input_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                "config.json",
                "Restore a clean-start workspace configuration.",
            )
        )
        return checked

    fixed_inputs = (
        ("profile_mappings", "mappings", paths.profile_mappings),
        ("rules", "rules", paths.rules),
        ("corrections", "corrections", paths.corrections),
        ("rate_cache", "rates", paths.rates),
    )
    for field, kind, default in fixed_inputs:
        value = config.get(field, default.name)
        if not isinstance(value, str) or not value.strip():
            findings.append(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    "config.json",
                    "Restore or correct the configured input path.",
                )
            )
            continue
        path = _configured_workspace_path(paths, value)
        if path is None:
            findings.append(
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    "config.json",
                    "Remove the unsafe configured path or restore a backup.",
                )
            )
            continue
        if not _path_lexists(path):
            continue
        if not path.is_file():
            findings.append(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, path),
                    "Restore or correct the configured input.",
                )
            )
            continue
        checked += 1
        _audit_private_mode(paths, path, 0o600, findings)
        _audit_input_parent_modes(paths, path, findings)
        if not _valid_input_document(path, kind, config):
            findings.append(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, path),
                    "Restore or correct the configured input.",
                )
            )

    profile_values = config.get("profiles")
    if not isinstance(profile_values, list) or not profile_values:
        findings.append(
            DoctorFinding(
                "workspace_input_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                "config.json",
                "Restore or correct the configured profiles.",
            )
        )
        return checked
    for value in profile_values:
        if not isinstance(value, str) or not value.strip():
            findings.append(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    "config.json",
                    "Restore or correct the configured profile path.",
                )
            )
            continue
        path = _configured_workspace_path(paths, value)
        if path is None:
            findings.append(
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    "config.json",
                    "Remove the unsafe configured path or restore a backup.",
                )
            )
            continue
        if not path.is_file() or not _valid_input_document(path, "profile", config):
            findings.append(
                DoctorFinding(
                    "workspace_input_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, path),
                    "Restore or correct the configured profile.",
                )
            )
            continue
        checked += 1
        _audit_private_mode(paths, path, 0o600, findings)
        _audit_input_parent_modes(paths, path, findings)
    return checked


def _audit_legacy_markers(
    paths: WorkspacePaths,
    findings: list[DoctorFinding],
) -> int:
    checked = 0
    for marker in legacy_workspace_markers(paths):
        if not _path_lexists(marker):
            continue
        checked += 1
        if marker.is_symlink():
            _unsafe_path_finding(_relative(paths, marker), findings)
            continue
        findings.append(
            DoctorFinding(
                "workspace_input_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                _relative(paths, marker),
                "Preserve the legacy workspace and create a fresh workspace.",
            )
        )
    return checked


def _configured_workspace_path(paths: WorkspacePaths, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else paths.root / raw
        normalized = Path(os.path.abspath(candidate))
    except OSError, ValueError:
        return None
    try:
        relative = normalized.relative_to(paths.root)
    except ValueError:
        return None
    current = paths.root
    for part in relative.parts:
        current = current / part
        if _path_lexists(current) and current.is_symlink():
            return None
    return normalized


def _valid_input_document(
    path: Path,
    kind: str,
    config: Mapping[str, object],
) -> bool:
    if kind == "corrections":
        return True
    try:
        value = _read_json_object(path)
    except OSError, UnicodeError, json.JSONDecodeError, ValueError:
        return False
    try:
        if kind == "profile":
            return isinstance(value.get("id"), str) and bool(value["id"])
        if kind == "mappings":
            validate_profile_mappings(value, config)
        elif kind == "rates":
            validate_rate_cache(value)
        elif kind == "rules":
            rules = value.get("rules")
            if not isinstance(rules, list):
                return False
            validate_rules(rules, dict(config))
    except AttributeError, TypeError, ValueError:
        return False
    return True


def _audit_input_parent_modes(
    paths: WorkspacePaths,
    path: Path,
    findings: list[DoctorFinding],
) -> None:
    parent = path.parent
    while parent != paths.root:
        _audit_private_mode(paths, parent, 0o700, findings)
        parent = parent.parent


def _audit_import_record_summaries(
    paths: WorkspacePaths,
    findings: list[DoctorFinding],
    record_ids: set[str],
    ready_record_ids: set[str],
    ready_record_authorities: dict[str, _ReadyRecordAuthority],
) -> int:
    checked = 0
    try:
        records = sorted(paths.import_records.iterdir(), key=lambda path: path.name)
    except OSError:
        findings.append(
            DoctorFinding(
                "managed_metadata_invalid",
                FindingSeverity.ERROR,
                RepairClass.SAFE,
                ".honeymoney/import-records",
                "Run doctor --fix to restore owner-only access.",
            )
        )
        return checked
    for record in records:
        checked += 1
        record_relative = _relative(paths, record)
        if record.is_symlink():
            _unsafe_path_finding(record_relative, findings)
            continue
        if SOURCE_ID_PATTERN.fullmatch(record.name) is None:
            findings.append(
                DoctorFinding(
                    "unknown_managed_entry",
                    FindingSeverity.WARNING,
                    RepairClass.NONE,
                    record_relative,
                    "Leave the unknown managed entry unchanged.",
                )
            )
            continue
        if not record.is_dir():
            findings.append(
                DoctorFinding(
                    "import_record_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    record_relative,
                    "Restore a complete workspace backup.",
                )
            )
            continue
        record_ids.add(record.name)
        _audit_private_mode(paths, record, 0o700, findings)
        _audit_unknown_record_entries(paths, record, findings)
        attempts = record / "attempts"
        snapshot = record / "transactions.csv"
        summary_path = record / "summary.json"
        if attempts.is_symlink():
            _unsafe_path_finding(_relative(paths, attempts), findings)
            continue
        if snapshot.is_symlink():
            _unsafe_path_finding(_relative(paths, snapshot), findings)
            continue
        if summary_path.is_symlink():
            _unsafe_path_finding(_relative(paths, summary_path), findings)
            continue
        if not attempts.is_dir():
            findings.append(
                DoctorFinding(
                    "attempt_history_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    record_relative,
                    "Restore a complete workspace backup.",
                )
            )
            continue
        _audit_private_mode(paths, attempts, 0o700, findings)
        if not _audit_attempt_entries(paths, attempts, findings):
            continue
        try:
            reports = load_attempts(record)
        except ImportRecordError as error:
            reports = []
            valid_history = False
            history_code = (
                "newer_honeymoney_required"
                if error.code == "attempt_schema_unsupported"
                else "attempt_history_invalid"
            )
        else:
            valid_history = all(
                report["source_id"] == record.name for report in reports
            )
            history_code = "attempt_history_invalid"
        if not valid_history:
            findings.append(
                DoctorFinding(
                    history_code,
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    record_relative,
                    "Restore a complete workspace backup.",
                )
            )
            continue
        relative = _relative(paths, summary_path)
        try:
            expected_summary = build_summary(record, record.name)
            expected = summary_document(expected_summary)
        except ImportRecordError as error:
            findings.append(
                DoctorFinding(
                    (
                        "attempt_history_invalid"
                        if error.code.startswith("attempt_")
                        else "import_record_invalid"
                    ),
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, record),
                    "Restore a complete workspace backup.",
                )
            )
            continue
        if expected_summary["ready"]:
            current_number = expected_summary["current_attempt_number"]
            current = next(
                (
                    report
                    for report in reports
                    if report["attempt_number"] == current_number
                ),
                None,
            )
            snapshot_rows = _read_snapshot_rows(snapshot)
            source_revision = current.get("source_revision") if current else None
            parser_contract = current.get("parser_contract") if current else None
            if (
                snapshot_rows is None
                or not isinstance(source_revision, str)
                or not isinstance(parser_contract, str)
            ):
                findings.append(
                    DoctorFinding(
                        "import_record_invalid",
                        FindingSeverity.ERROR,
                        RepairClass.MANUAL,
                        _relative(paths, record),
                        "Restore a complete workspace backup.",
                    )
                )
                continue
            ready_record_ids.add(record.name)
            ready_record_authorities[record.name] = _ReadyRecordAuthority(
                source_revision,
                parser_contract,
                tuple(snapshot_rows),
            )
        _audit_private_mode(paths, snapshot, 0o600, findings)
        _audit_private_mode(paths, summary_path, 0o600, findings)
        try:
            actual = summary_path.read_text(encoding="utf-8")
        except OSError, UnicodeError:
            actual = ""
        if actual != expected:
            findings.append(
                DoctorFinding(
                    "summary_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.SAFE,
                    relative,
                    "Run doctor --fix to rebuild the disposable summary.",
                )
            )
        else:
            checked += 1
    return checked


def _audit_attempt_entries(
    paths: WorkspacePaths,
    attempts: Path,
    findings: list[DoctorFinding],
) -> bool:
    valid = True
    try:
        entries = sorted(attempts.iterdir(), key=lambda path: path.name)
    except OSError:
        findings.append(
            DoctorFinding(
                "attempt_history_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                _relative(paths, attempts),
                "Restore a complete workspace backup.",
            )
        )
        return False
    for entry in entries:
        relative = _relative(paths, entry)
        if entry.is_symlink():
            _unsafe_path_finding(relative, findings)
            valid = False
            continue
        if not entry.is_file() or _ATTEMPT_FILE_NAME.fullmatch(entry.name) is None:
            findings.append(
                DoctorFinding(
                    "attempt_history_invalid",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, attempts),
                    "Restore a complete workspace backup.",
                )
            )
            valid = False
            continue
        _audit_private_mode(paths, entry, 0o600, findings)
    return valid


def _read_snapshot_rows(path: Path) -> list[dict[str, str]] | None:
    from honeymoney.workspace_commands import SNAPSHOT_COLUMNS

    try:
        return read_transaction_snapshot(path, SNAPSHOT_COLUMNS)
    except ImportRecordError:
        return None


def _audit_unknown_record_entries(
    paths: WorkspacePaths,
    record: Path,
    findings: list[DoctorFinding],
) -> None:
    known = {"attempts", "summary.json", "transactions.csv"}
    try:
        entries = record.iterdir()
    except OSError:
        return
    for entry in entries:
        if entry.name in known:
            continue
        relative = _relative(paths, entry)
        if entry.is_symlink():
            _unsafe_path_finding(relative, findings)
            continue
        findings.append(
            DoctorFinding(
                "unknown_managed_entry",
                FindingSeverity.WARNING,
                RepairClass.NONE,
                relative,
                "Leave the unknown managed entry unchanged.",
            )
        )


def _audit_durable_authority_agreement(
    paths: WorkspacePaths,
    index: Mapping[str, object],
    record_ids: set[str],
    ready_record_ids: set[str],
    ready_record_authorities: Mapping[str, _ReadyRecordAuthority],
    findings: list[DoctorFinding],
) -> int:
    evidence_key = _view_proof_key(index)
    sources_by_id: dict[str, Mapping[str, object]] = {}
    for source in _index_sources(index):
        source_id = source.get("source_id")
        if isinstance(source_id, str):
            sources_by_id[source_id] = source
    indexed_ids = set(sources_by_id)
    checked = len(indexed_ids)
    for source_id in sorted(ready_record_ids - indexed_ids):
        findings.append(
            DoctorFinding(
                "durable_state_conflict",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                f".honeymoney/import-records/{source_id}",
                "Restore a complete workspace backup.",
            )
        )
    for source_id in sorted(indexed_ids - ready_record_ids):
        record_path = f".honeymoney/import-records/{source_id}"
        if any(
            finding.path == record_path
            and finding.code in {"import_record_invalid", "attempt_history_invalid"}
            for finding in findings
        ):
            continue
        path = (
            record_path
            if source_id in record_ids
            else ".honeymoney/workspace-index.json"
        )
        findings.append(
            DoctorFinding(
                "durable_state_conflict",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                path,
                "Restore a complete workspace backup.",
            )
        )
    for source_id in sorted(ready_record_ids & indexed_ids):
        authority = ready_record_authorities.get(source_id)
        source = sources_by_id[source_id]
        if (
            authority is None
            or evidence_key is None
            or not _source_matches_record(source, authority, evidence_key)
        ):
            findings.append(
                DoctorFinding(
                    "durable_state_conflict",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    f".honeymoney/import-records/{source_id}",
                    "Restore a complete workspace backup.",
                )
            )
    return checked


def _source_matches_record(
    source: Mapping[str, object],
    authority: _ReadyRecordAuthority,
    evidence_key: bytes,
) -> bool:
    if (
        source.get("source_revision")
        != workspace_source_revision(
            authority.source_revision, evidence_key=evidence_key
        )
        or source.get("extractor_contract_id") != authority.parser_contract
    ):
        return False
    records = source.get("records")
    if not isinstance(records, list):
        return False
    active: dict[str, Mapping[str, object]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            return False
        if record.get("state") != "active":
            continue
        source_record_id = record.get("source_record_id")
        if not isinstance(source_record_id, str):
            return False
        active[source_record_id] = record
    if set(active) != {row["source_record_id"] for row in authority.snapshot_rows}:
        return False
    return all(
        workspace_record_fingerprint(row, evidence_key=evidence_key)
        == active[row["source_record_id"]].get("record_fingerprint")
        for row in authority.snapshot_rows
    )


def _unsafe_path_finding(
    relative: str | None,
    findings: list[DoctorFinding],
) -> None:
    findings.append(
        DoctorFinding(
            "managed_path_unsafe",
            FindingSeverity.ERROR,
            RepairClass.MANUAL,
            relative,
            "Remove the unsafe path or restore a complete workspace backup.",
        )
    )


def _summary_repair_action(
    paths: WorkspacePaths,
    relative: str,
) -> RepairAction | None:
    path = paths.root / relative
    record = path.parent
    if path.name != "summary.json" or SOURCE_ID_PATTERN.fullmatch(record.name) is None:
        return None
    try:
        content = summary_document(build_summary(record, record.name)).encode("utf-8")
    except ImportRecordError:
        return None
    return RepairAction(
        RepairActionKind.REBUILD_SUMMARY, relative, "summary_invalid", content
    )


def _generated_view_repair_actions(
    paths: WorkspacePaths,
    findings: tuple[DoctorFinding, ...],
) -> tuple[RepairAction, ...]:
    periods = {
        parts[1]
        for finding in findings
        if finding.code == "generated_view_invalid" and finding.path is not None
        for parts in (finding.path.split("/"),)
        if len(parts) == 2
        and parts[0] == "views"
        and _VIEW_PERIOD.fullmatch(parts[1]) is not None
    }
    if not periods:
        return ()

    try:
        index = load_compatible_workspace_index(paths.workspace_index)
    except WorkspaceIndexError:
        return ()
    if not derivation_contract_is_rederivable(index):
        return ()

    # Keep the command service import out of read-only audits. Repair derives only
    # normalized snapshots and disables Ollama before it checks the durable proof.
    from honeymoney.workspace_commands import (
        WorkspaceCommandError,
        derive_workspace_for_repair,
    )
    from honeymoney.workspace_derivation import view_report_inputs

    try:
        context, derivation = derive_workspace_for_repair(paths.config)
        key = bytes.fromhex(
            context.index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
        )
        expected_proofs = {
            item["period"]: item["content_proof"]
            for item in context.index["registered_views"]
        }
    except WorkspaceCommandError, KeyError, TypeError, ValueError:
        return ()

    rows_by_period: dict[str, list[Mapping[str, str]]] = {
        period: [] for period in periods
    }
    for row in derivation.rows:
        period = view_period_for_row(row)
        if period in rows_by_period:
            rows_by_period[period].append(row)

    actions: list[RepairAction] = []
    for period in sorted(periods):
        try:
            unit = build_view_unit(
                period,
                rows_by_period[period],
                content_proof_key=key,
                report_inputs=(
                    view_report_inputs(derivation, rows_by_period[period])
                    if rows_by_period[period]
                    else None
                ),
            )
        except WorkspaceViewError:
            continue
        if unit.content_proof != expected_proofs.get(period):
            continue
        actions.extend(
            RepairAction(
                RepairActionKind.REBUILD_GENERATED_VIEW,
                file.path,
                "generated_view_invalid",
                file.content,
            )
            for file in unit.files()
        )
    return tuple(actions)


def _mode_repair_action(paths: WorkspacePaths, relative: str) -> RepairAction | None:
    path = _managed_relative_path(paths, relative)
    expected = _expected_private_mode(paths, relative)
    if path is None or expected is None:
        return None
    if path.is_symlink():
        return None
    if not _path_lexists(path):
        if expected == 0o700 and _managed_parent_is_safe(paths, path):
            return RepairAction(
                RepairActionKind.CREATE_PRIVATE_DIRECTORY,
                relative,
                "managed_metadata_invalid",
                mode=expected,
            )
        return None
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if (expected == 0o700 and not stat.S_ISDIR(mode)) or (
        expected == 0o600 and not stat.S_ISREG(mode)
    ):
        return None
    content: bytes | None = None
    if expected == 0o600:
        if path.lstat().st_nlink != 1:
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
    return RepairAction(
        RepairActionKind.SET_PRIVATE_MODE,
        relative,
        "managed_metadata_invalid",
        content=content,
        mode=expected,
    )


def _managed_parent_is_safe(paths: WorkspacePaths, path: Path) -> bool:
    try:
        relative_parent = path.parent.relative_to(paths.root)
    except ValueError:
        return False
    current = paths.root
    for part in relative_parent.parts:
        current /= part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def _repair_directory_targets(
    paths: WorkspacePaths,
    actions: list[RepairAction],
    targets: list[PublicationTarget],
) -> list[PublicationDirectory]:
    directories: dict[str, PublicationDirectory] = {}

    def add(relative: str) -> None:
        expected = _expected_private_mode(paths, relative)
        path = _managed_relative_path(paths, relative)
        if expected != 0o700 or path is None:
            raise PublicationError("invalid managed directory repair")
        if _path_lexists(path) and (path.is_symlink() or not path.is_dir()):
            raise PublicationError("unsafe managed directory repair")
        directories[relative] = PublicationDirectory(relative)

    for action in actions:
        if action.kind == RepairActionKind.CREATE_PRIVATE_DIRECTORY:
            add(action.path)
        elif action.kind == RepairActionKind.SET_PRIVATE_MODE and action.mode == 0o700:
            add(action.path)

    for target in targets:
        path = _managed_relative_path(paths, target.path)
        if path is None:
            raise PublicationError("invalid managed repair target")
        parent = path.parent
        while parent != paths.root:
            relative = _relative(paths, parent)
            if relative is None:
                raise PublicationError("managed repair target leaves the workspace")
            expected = _expected_private_mode(paths, relative)
            if expected != 0o700:
                break
            if not _path_lexists(parent) or (
                parent.is_dir() and stat.S_IMODE(parent.stat().st_mode) != 0o700
            ):
                add(relative)
            elif parent.is_symlink() or not parent.is_dir():
                raise PublicationError("unsafe managed directory repair")
            parent = parent.parent

    return sorted(
        directories.values(),
        key=lambda item: (len(Path(item.path).parts), item.path),
    )


def _managed_relative_path(paths: WorkspacePaths, relative: str) -> Path | None:
    if relative == ".":
        return paths.root
    parts = relative.split("/")
    if not parts or any(
        part in {"", ".", ".."} or _SAFE_PATH_COMPONENT.fullmatch(part) is None
        for part in parts
    ):
        return None
    return paths.root.joinpath(*parts)


def _expected_private_mode(paths: WorkspacePaths, relative: str) -> int | None:
    if relative == ".":
        return 0o700
    static_directories = {
        ".honeymoney",
        ".honeymoney/import-records",
        "profiles",
        "views",
    }
    static_files = {
        ".honeymoney/report-preview.html",
        ".honeymoney/workspace-index.json",
    }
    if relative in static_directories:
        return 0o700
    if relative in static_files:
        return 0o600

    parts = relative.split("/")
    if (
        len(parts) >= 3
        and parts[:2] == [".honeymoney", "import-records"]
        and SOURCE_ID_PATTERN.fullmatch(parts[2]) is not None
    ):
        if len(parts) == 3 or (len(parts) == 4 and parts[3] == "attempts"):
            return 0o700
        if len(parts) == 4 and parts[3] in {"summary.json", "transactions.csv"}:
            return 0o600
        if (
            len(parts) == 5
            and parts[3] == "attempts"
            and _ATTEMPT_FILE_NAME.fullmatch(parts[4]) is not None
        ):
            return 0o600
    if len(parts) >= 2 and parts[0] == "views" and _VIEW_PERIOD.fullmatch(parts[1]):
        if len(parts) == 2:
            return 0o700
        if len(parts) == 3 and parts[2] in VIEW_FILE_NAMES:
            return 0o600

    return _configured_input_mode(paths, relative)


def _configured_input_mode(paths: WorkspacePaths, relative: str) -> int | None:
    try:
        config = _read_json_object(paths.config)
    except OSError, UnicodeError, json.JSONDecodeError, ValueError:
        return None
    for path in _input_proof_paths(paths, config).values():
        file_relative = _relative(paths, path)
        if file_relative == relative:
            return 0o600
        parent = path.parent
        while parent != paths.root:
            if _relative(paths, parent) == relative:
                return 0o700
            parent = parent.parent
    return None


def _relative(paths: WorkspacePaths, path: Path) -> str | None:
    try:
        relative = path.relative_to(paths.root)
    except ValueError:
        return None
    if not relative.parts:
        return "."
    if not relative.parts or any(
        _SAFE_PATH_COMPONENT.fullmatch(part) is None for part in relative.parts
    ):
        return None
    return relative.as_posix()


def _audit_unknown_internal_entries(
    paths: WorkspacePaths,
    findings: list[DoctorFinding],
) -> int:
    known = {
        "import-records",
        "publication",
        "publication-journal.json",
        "report-preview.html",
        "workspace-index.json",
        "workspace.lock",
    }
    checked = 0
    try:
        entries = paths.internal.iterdir()
    except OSError:
        return checked
    for entry in entries:
        checked += 1
        if entry.name == "publication":
            if entry.is_symlink() or not entry.is_dir():
                _unsafe_path_finding(_relative(paths, entry), findings)
                continue
            try:
                has_retained_bytes = next(entry.iterdir(), None) is not None
            except OSError:
                has_retained_bytes = True
            if has_retained_bytes:
                findings.append(
                    DoctorFinding(
                        "publication_state_invalid",
                        FindingSeverity.ERROR,
                        RepairClass.MANUAL,
                        ".honeymoney/publication",
                        "Restore a complete workspace backup.",
                    )
                )
            continue
        if entry.name == "report-preview.html":
            if entry.is_symlink() or not entry.is_file():
                _unsafe_path_finding(_relative(paths, entry), findings)
            else:
                _audit_private_mode(paths, entry, 0o600, findings)
            continue
        if entry.name in known:
            continue
        if entry.is_symlink():
            _unsafe_path_finding(_relative(paths, entry), findings)
            continue
        findings.append(
            DoctorFinding(
                "unknown_managed_entry",
                FindingSeverity.WARNING,
                RepairClass.NONE,
                _relative(paths, entry),
                "Leave the unknown managed entry unchanged.",
            )
        )
    return checked


def _audit_private_mode(
    paths: WorkspacePaths,
    path: Path,
    expected_mode: int,
    findings: list[DoctorFinding],
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        _unsafe_path_finding(_relative(paths, path), findings)
        return
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode == expected_mode:
        return
    findings.append(
        DoctorFinding(
            "managed_metadata_invalid",
            FindingSeverity.ERROR,
            RepairClass.SAFE,
            _relative(paths, path),
            "Run doctor --fix to restore owner-only access.",
        )
    )


def _audit_input_proofs(
    paths: WorkspacePaths,
    index: Mapping[str, object],
    config: Mapping[str, object] | None,
    findings: list[DoctorFinding],
) -> int:
    checked = 0
    if config is None:
        return checked
    proof_paths = _input_proof_paths(paths, config)
    if not proof_paths:
        return checked
    overlap = index.get("overlap_manifest")
    if not isinstance(overlap, Mapping):
        return checked
    namespace = overlap.get("namespace_key")
    if not isinstance(namespace, str):
        return checked
    try:
        key = bytes.fromhex(namespace.removeprefix("ovns_"))
    except ValueError:
        return checked
    proofs = index.get("input_proofs")
    if not isinstance(proofs, list):
        return checked
    supplied: dict[str, str] = {}
    for proof in proofs:
        if not isinstance(proof, Mapping):
            continue
        name = proof.get("name")
        expected = proof.get("proof")
        if not isinstance(name, str) or not isinstance(expected, str):
            continue
        supplied[name] = expected
    requires_complete_proofs = bool(
        _index_sources(index) or index.get("registered_views")
    )
    if (supplied or requires_complete_proofs) and set(supplied) != set(proof_paths):
        findings.append(
            DoctorFinding(
                "workspace_index_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                ".honeymoney/workspace-index.json",
                "Restore a complete workspace backup.",
            )
        )
    for name, expected in sorted(supplied.items()):
        path = proof_paths.get(name)
        if path is None:
            continue
        actual = _file_proof(path, name, key)
        checked += 1
        if actual == expected:
            continue
        findings.append(
            DoctorFinding(
                "full_rebuild_required",
                FindingSeverity.WARNING,
                RepairClass.FULL_REBUILD,
                _relative(paths, path),
                "Run views rebuild --all to regenerate output from changed inputs.",
            )
        )
    return checked


def _index_sources(index: Mapping[str, object]) -> list[Mapping[str, object]]:
    identity = index.get("identity_manifest")
    if not isinstance(identity, Mapping):
        return []
    sources = identity.get("sources")
    if not isinstance(sources, list):
        return []
    return [source for source in sources if isinstance(source, Mapping)]


def _input_proof_paths(
    paths: WorkspacePaths,
    config: Mapping[str, object],
) -> dict[str, Path]:
    fixed = (
        ("corrections", "corrections", paths.corrections),
        ("mappings", "profile_mappings", paths.profile_mappings),
        ("rates", "rate_cache", paths.rates),
        ("rules", "rules", paths.rules),
    )
    result = {"config": paths.config}
    for proof_name, field, default in fixed:
        path = _configured_workspace_path(paths, config.get(field, default.name))
        if path is None:
            return {}
        result[proof_name] = path
    profiles = config.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        return {}
    for number, value in enumerate(profiles):
        path = _configured_workspace_path(paths, value)
        if path is None:
            return {}
        result[f"profile-{number:04d}"] = path
    return result


def _file_proof(path: Path, name: str, key: bytes) -> str | None:
    if path.is_symlink():
        return None
    if not path.exists():
        content = b"honeymoney-input-missing-v1"
    else:
        if not path.is_file():
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
    return hmac.new(
        key,
        b"honeymoney-input-proof-v1\0" + name.encode("ascii") + b"\0" + content,
        sha256,
    ).hexdigest()


def _audit_corrections(
    paths: WorkspacePaths,
    config: Mapping[str, object] | None,
    index: Mapping[str, object],
    findings: list[DoctorFinding],
) -> int:
    if config is None:
        return 0
    path = _configured_workspace_path(
        paths,
        config.get("corrections", paths.corrections.name),
    )
    if path is None:
        return 0
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError, UnicodeError, csv.Error:
        rows = []
    if not _valid_corrections_rows(rows, config, index):
        findings.append(
            DoctorFinding(
                "corrections_invalid",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                _relative(paths, path),
                "Restore a complete workspace backup.",
            )
        )
    return 1


def _valid_corrections_rows(
    rows: list[list[str]],
    config: Mapping[str, object],
    index: Mapping[str, object],
) -> bool:
    if not rows or rows[0] != CORRECTION_COLUMNS:
        return False
    view_ids = _view_transaction_ids(index)
    seen: set[str] = set()
    for row in rows[1:]:
        if len(row) != len(CORRECTION_COLUMNS):
            return False
        transaction_id = row[0].strip()
        if not transaction_id:
            if any(cell.strip() for cell in row[1:]):
                return False
            continue
        if transaction_id in seen or transaction_id not in view_ids:
            return False
        correction = {
            field: value
            for field, value in zip(CORRECTION_COLUMNS[1:], row[1:], strict=True)
        }
        try:
            validate_correction(transaction_id, correction, config)
        except ValueError:
            return False
        seen.add(transaction_id)
    return True


def _view_transaction_ids(index: Mapping[str, object]) -> set[str]:
    overlap = index.get("overlap_manifest")
    if not isinstance(overlap, Mapping):
        return set()
    groups = overlap.get("groups")
    if not isinstance(groups, list):
        return set()
    result: set[str] = set()
    for group in groups:
        if not isinstance(group, Mapping):
            return set()
        slots = group.get("slots")
        if not isinstance(slots, list):
            return set()
        for slot in slots:
            if not isinstance(slot, Mapping):
                return set()
            identifier = slot.get("transaction_id")
            if not isinstance(identifier, str):
                return set()
            result.add(identifier)
    return result


def _audit_registered_views(
    paths: WorkspacePaths,
    index: Mapping[str, object],
    findings: list[DoctorFinding],
) -> int:
    registered = index.get("registered_views")
    if not isinstance(registered, list):
        return 0
    proofs = {
        item["period"]: item["content_proof"]
        for item in registered
        if isinstance(item, Mapping)
        and isinstance(item.get("period"), str)
        and isinstance(item.get("content_proof"), str)
    }
    if len(proofs) != len(registered):
        return 0
    key = _view_proof_key(index)
    if key is None:
        return 0
    checked = 0
    if _path_lexists(paths.views) and paths.views.is_symlink():
        findings.append(
            DoctorFinding(
                "managed_path_unsafe",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                "views",
                "Remove the unsafe path or restore a complete workspace backup.",
            )
        )
        return checked
    if _path_lexists(paths.views) and not paths.views.is_dir():
        findings.append(
            DoctorFinding(
                "managed_path_unsafe",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                "views",
                "Remove the unsafe path or restore a complete workspace backup.",
            )
        )
        return checked
    expected_units: Mapping[str, ViewUnit | None]
    if derivation_contract_is_rederivable(index):
        rederived_units = _rederive_registered_views(
            paths, index, proofs, key, findings
        )
        if rederived_units is None:
            return checked
        expected_units = rederived_units
    else:
        expected_units = {period: None for period in proofs}
    if paths.views.is_dir():
        checked += 1
        _audit_private_mode(paths, paths.views, 0o700, findings)
    for period, expected_proof in proofs.items():
        checked += _audit_one_registered_view(
            paths,
            period,
            expected_proof,
            key,
            expected_units[period],
            findings,
        )
    if paths.views.is_dir():
        checked += _audit_unknown_view_entries(paths, set(proofs), findings)
    return checked


def _audit_one_registered_view(
    paths: WorkspacePaths,
    period: str,
    expected_proof: str,
    key: bytes,
    expected_unit: ViewUnit | None,
    findings: list[DoctorFinding],
) -> int:
    directory = paths.views / period
    relative = _relative(paths, directory)
    repair_class = (
        RepairClass.SAFE if expected_unit is not None else RepairClass.FULL_REBUILD
    )
    if directory.is_symlink():
        findings.append(
            DoctorFinding(
                "managed_path_unsafe",
                FindingSeverity.ERROR,
                RepairClass.MANUAL,
                relative,
                "Remove the unsafe path or restore a complete workspace backup.",
            )
        )
        return 1
    if not directory.is_dir():
        _generated_view_invalid(relative, findings, repair_class=repair_class)
        return 1
    _audit_private_mode(paths, directory, 0o700, findings)
    _audit_unknown_view_files(paths, directory, findings)
    files: dict[str, bytes] = {}
    for name in VIEW_FILE_NAMES:
        path = directory / name
        if path.is_symlink():
            findings.append(
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, path),
                    "Remove the unsafe path or restore a complete workspace backup.",
                )
            )
            return 1
        if not path.is_file():
            _generated_view_invalid(relative, findings, repair_class=repair_class)
            return 1
        _audit_private_mode(paths, path, 0o600, findings)
        try:
            files[name] = path.read_bytes()
        except OSError:
            _generated_view_invalid(relative, findings, repair_class=repair_class)
            return 1
    try:
        actual_proof = view_content_proof(period, files, content_proof_key=key)
    except WorkspaceViewError:
        _generated_view_invalid(relative, findings, repair_class=repair_class)
        return len(VIEW_FILE_NAMES) + 1
    if actual_proof != expected_proof:
        _generated_view_invalid(relative, findings, repair_class=repair_class)
        return len(VIEW_FILE_NAMES) + 1
    if expected_unit is not None:
        expected_files = {
            file.path.rsplit("/", 1)[-1]: file.content for file in expected_unit.files()
        }
        if any(files[name] != expected_files[name] for name in VIEW_FILE_NAMES):
            _generated_view_invalid(relative, findings, repair_class=repair_class)
    return len(VIEW_FILE_NAMES) + 1


def _generated_view_invalid(
    relative: str | None,
    findings: list[DoctorFinding],
    *,
    repair_class: RepairClass = RepairClass.SAFE,
) -> None:
    next_action = (
        "Run `honeymoney views rebuild --all` to regenerate output."
        if repair_class == RepairClass.FULL_REBUILD
        else "Run doctor --fix to restore the proved view unit."
    )
    findings.append(
        DoctorFinding(
            "generated_view_invalid",
            FindingSeverity.ERROR,
            repair_class,
            relative,
            next_action,
        )
    )


def _rederive_registered_views(
    paths: WorkspacePaths,
    index: Mapping[str, object],
    proofs: Mapping[str, str],
    key: bytes,
    findings: list[DoctorFinding],
) -> dict[str, ViewUnit] | None:
    blocked_codes = {
        "attempt_history_invalid",
        "corrections_invalid",
        "durable_state_conflict",
        "full_rebuild_required",
        "import_record_invalid",
        "managed_path_unsafe",
        "publication_state_invalid",
        "workspace_input_invalid",
    }
    if any(finding.code in blocked_codes for finding in findings):
        return None
    if not proofs and not _index_sources(index):
        return {}
    from honeymoney.workspace_commands import (
        WorkspaceCommandError,
        derive_workspace_for_repair,
    )
    from honeymoney.workspace_derivation import view_report_inputs

    try:
        context, derivation = derive_workspace_for_repair(paths.config)
    except WorkspaceCommandError, ValueError:
        _durable_state_conflict(".honeymoney/workspace-index.json", findings)
        return None
    if derivation.overlap_manifest != context.index["overlap_manifest"]:
        _durable_state_conflict(".honeymoney/workspace-index.json", findings)
        return None
    rows_by_period: dict[str, list[Mapping[str, str]]] = {
        period: [] for period in proofs
    }
    for row in derivation.rows:
        period = view_period_for_row(row)
        if period in rows_by_period:
            rows_by_period[period].append(row)
    units: dict[str, ViewUnit] = {}
    try:
        for period, expected_proof in proofs.items():
            rows = rows_by_period[period]
            unit = build_view_unit(
                period,
                rows,
                content_proof_key=key,
                report_inputs=(view_report_inputs(derivation, rows) if rows else None),
            )
            if unit.content_proof != expected_proof:
                _durable_state_conflict(".honeymoney/workspace-index.json", findings)
                return None
            units[period] = unit
    except WorkspaceViewError:
        _durable_state_conflict(".honeymoney/workspace-index.json", findings)
        return None
    return units


def _durable_state_conflict(
    path: str,
    findings: list[DoctorFinding],
) -> None:
    findings.append(
        DoctorFinding(
            "durable_state_conflict",
            FindingSeverity.ERROR,
            RepairClass.MANUAL,
            path,
            "Restore a complete workspace backup.",
        )
    )


def _audit_unknown_view_entries(
    paths: WorkspacePaths,
    registered: set[str],
    findings: list[DoctorFinding],
) -> int:
    checked = 0
    try:
        entries = paths.views.iterdir()
    except OSError:
        return checked
    for entry in entries:
        checked += 1
        if entry.name in registered:
            continue
        if entry.is_symlink():
            findings.append(
                DoctorFinding(
                    "managed_path_unsafe",
                    FindingSeverity.ERROR,
                    RepairClass.MANUAL,
                    _relative(paths, entry),
                    "Remove the unsafe path or restore a complete workspace backup.",
                )
            )
            continue
        findings.append(
            DoctorFinding(
                "unknown_managed_entry",
                FindingSeverity.WARNING,
                RepairClass.NONE,
                _relative(paths, entry),
                "Leave the unknown managed entry unchanged.",
            )
        )
    return checked


def _audit_unknown_view_files(
    paths: WorkspacePaths,
    directory: Path,
    findings: list[DoctorFinding],
) -> None:
    try:
        entries = directory.iterdir()
    except OSError:
        return
    for entry in entries:
        if entry.name in VIEW_FILE_NAMES:
            continue
        relative = _relative(paths, entry)
        if entry.is_symlink():
            _unsafe_path_finding(relative, findings)
            continue
        findings.append(
            DoctorFinding(
                "unknown_managed_entry",
                FindingSeverity.WARNING,
                RepairClass.NONE,
                relative,
                "Leave the unknown managed entry unchanged.",
            )
        )


def _view_proof_key(index: Mapping[str, object]) -> bytes | None:
    overlap = index.get("overlap_manifest")
    if not isinstance(overlap, Mapping):
        return None
    namespace = overlap.get("namespace_key")
    if not isinstance(namespace, str):
        return None
    try:
        key = bytes.fromhex(namespace.removeprefix("ovns_"))
    except ValueError:
        return None
    return key if len(key) == 32 else None
