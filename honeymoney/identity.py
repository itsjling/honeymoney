"""Pure primitives for the stable transaction identity contract.

This module deliberately does not read or write workspace artifacts. Callers
resolve a batch, validate its manifest, then publish all files together.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import struct
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

IDENTITY_MANIFEST_NAME = ".honeymoney-identity-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MAX_UINT64 = 2**64 - 1

SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{64}$")
SOURCE_NAMESPACE_ID_RE = re.compile(r"^ns_[0-9a-f]{64}$")
SOURCE_REVISION_RE = re.compile(r"^rev_[0-9a-f]{64}$")
SOURCE_RECORD_ID_RE = re.compile(r"^rec_[0-9a-f]{64}$")
EXTRACTOR_CONTRACT_ID_RE = re.compile(r"^ext_[0-9a-f]{64}$")
RECORD_FINGERPRINT_RE = re.compile(r"^fp_[0-9a-f]{64}$")
TRANSACTION_ID_V2_RE = re.compile(r"^txn_[0-9a-f]{32}$")
TRANSACTION_ID_LEGACY_RE = re.compile(r"^txn_[0-9a-f]{16}$")

ID_FIELDS = (
    "source_id",
    "source_namespace_id",
    "source_revision",
    "source_record_id",
)

EXCLUDED_PROFILE_TOP_LEVEL_KEYS = frozenset(
    {
        "account",
        "account_type",
        "category",
        "categories",
        "confidence",
        "country",
        "flags",
        "flow_type",
        "institution",
        "needs_review",
        "notes",
        "owner",
        "payment_method",
        "reason",
        "rules",
    }
)

ADAPTER_COMPONENT_COUNTS = {1: 1, 2: 4, 3: 2, 4: 2}
ADAPTER_VERSIONS = {
    1: "csv-v1",
    2: "pdf-table-v1",
    3: "pdf-word-v1",
    4: "pdf-sectioned-v1",
}


@dataclass(frozen=True)
class SourceResolutionDiagnostic:
    """The safe details callers may report for a source-resolution failure."""

    code: str
    source_display: str
    action: str
    candidate_count: int
    remediation: str


@dataclass(frozen=True)
class IncomingSourceIdentity:
    """One incoming source's stable, non-record identity inputs.

    ``record_data`` deliberately remains opaque to source resolution. It lets a
    caller retain its parsed records alongside this input without letting them
    affect source claims or leak through diagnostics.
    """

    stable_handle: str
    source_display: str
    namespace_id: str
    revision: str
    contract_id: str
    record_data: object | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.stable_handle, str) or not self.stable_handle:
            raise IdentityError("identity_manifest_invalid")
        if not isinstance(self.source_display, str):
            raise IdentityError("identity_manifest_invalid")
        object.__setattr__(
            self, "source_display", unicodedata.normalize("NFC", self.source_display)
        )
        _require_match(SOURCE_NAMESPACE_ID_RE, self.namespace_id)
        _require_match(SOURCE_REVISION_RE, self.revision)
        _require_match(EXTRACTOR_CONTRACT_ID_RE, self.contract_id)

    @property
    def source_namespace_id(self) -> str:
        """Return the persisted-schema name for ``namespace_id``."""
        return self.namespace_id

    @property
    def source_revision(self) -> str:
        """Return the persisted-schema name for ``revision``."""
        return self.revision

    @property
    def extractor_contract_id(self) -> str:
        """Return the persisted-schema name for ``contract_id``."""
        return self.contract_id


@dataclass(frozen=True)
class ResolvedSourceIdentity:
    """The source assignment selected for one incoming stable handle."""

    stable_handle: str
    source_display: str
    source_id: str
    source_namespace_id: str
    source_revision: str
    extractor_contract_id: str
    disposition: str


@dataclass(frozen=True)
class SourceResolutionResult:
    """A complete, immutable source assignment for a successfully resolved batch."""

    assignments: tuple[ResolvedSourceIdentity, ...]
    diagnostics: tuple[SourceResolutionDiagnostic, ...] = ()

    @property
    def resolutions(self) -> tuple[ResolvedSourceIdentity, ...]:
        """Return assignments under the resolver's more general name."""
        return self.assignments


@dataclass(frozen=True)
class IncomingRecordIdentity:
    """One normalized row and its immutable allocation locator."""

    row: Mapping[str, Any]
    locator: AllocationLocator

    def __post_init__(self) -> None:
        if not isinstance(self.row, Mapping) or not isinstance(
            self.locator, AllocationLocator
        ):
            raise IdentityError("identity_manifest_invalid")


@dataclass(frozen=True)
class RecordResolutionDiagnostic:
    """A structural, privacy-safe diagnostic for record resolution."""

    code: str
    source_display: str
    action: str
    affected_count: int
    remediation: str


@dataclass(frozen=True)
class ResolvedRecordIdentity:
    """A resolved incoming row with its persisted ownership identifiers."""

    row: Mapping[str, Any]
    locator: AllocationLocator
    source_record_id: str
    transaction_id: str
    transaction_id_kind: str


@dataclass(frozen=True)
class RecordResolutionResult:
    """Pure result of resolving one assigned source's records."""

    resolved_rows: tuple[ResolvedRecordIdentity, ...]
    source_ownership: Mapping[str, Any]
    retained_legacy_rows: tuple[Mapping[str, Any], ...]
    retired_transaction_ids: tuple[str, ...]
    reset_transaction_ids: tuple[str, ...]
    diagnostics: tuple[RecordResolutionDiagnostic, ...] = ()

    @property
    def incoming_rows(self) -> tuple[ResolvedRecordIdentity, ...]:
        """Return resolved rows under the incoming-facing name."""
        return self.resolved_rows


@dataclass(frozen=True)
class IdentityResolution:
    """Complete, pure identity work for one incoming source batch."""

    resolved_rows: tuple[Mapping[str, Any], ...]
    next_manifest: Mapping[str, Any]
    retained_ledger_rows: tuple[Mapping[str, Any], ...]
    replaced_source_ids: tuple[str, ...]
    reset_transaction_ids: tuple[str, ...]
    diagnostics: tuple[SourceResolutionDiagnostic | RecordResolutionDiagnostic, ...]

    @property
    def incoming_rows(self) -> tuple[Mapping[str, Any], ...]:
        """Return rows ready to append to the retained ledger rows."""
        return self.resolved_rows

    @property
    def manifest(self) -> Mapping[str, Any]:
        """Return the validated manifest to publish with the ledger."""
        return self.next_manifest

    @property
    def ledger_rows(self) -> tuple[Mapping[str, Any], ...]:
        """Return prior ledger rows that remain after this operation."""
        return self.retained_ledger_rows


class IdentityError(ValueError):
    """A privacy-safe identity validation failure."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        diagnostic: SourceResolutionDiagnostic | None = None,
    ) -> None:
        self.code = code
        self.diagnostic = diagnostic
        super().__init__(message or code)


@dataclass(frozen=True, order=True)
class AllocationLocator:
    """The immutable parser coordinate used only for new-record allocation."""

    adapter_tag: int
    components: tuple[int, ...]

    def __post_init__(self) -> None:
        _validate_locator(self.adapter_tag, self.components)

    def as_manifest(self) -> dict[str, Any]:
        return {"adapter_tag": self.adapter_tag, "components": list(self.components)}

    @classmethod
    def from_manifest(cls, value: Any) -> "AllocationLocator":
        if not isinstance(value, dict) or set(value) != {"adapter_tag", "components"}:
            raise IdentityError("identity_manifest_invalid")
        tag = value["adapter_tag"]
        components = value["components"]
        if not isinstance(components, list):
            raise IdentityError("identity_manifest_invalid")
        return cls(tag, tuple(components))


@dataclass(frozen=True)
class AllocationOrigin:
    """Immutable allocation proof retained for a source record."""

    source_revision: str
    extractor_contract_id: str
    locator: AllocationLocator
    occurrence_ordinal: int

    def __post_init__(self) -> None:
        _require_match(SOURCE_REVISION_RE, self.source_revision)
        _require_match(EXTRACTOR_CONTRACT_ID_RE, self.extractor_contract_id)
        _validate_ordinal(self.occurrence_ordinal)

    def as_manifest(self) -> dict[str, Any]:
        return {
            "source_revision": self.source_revision,
            "extractor_contract_id": self.extractor_contract_id,
            "locator": self.locator.as_manifest(),
            "occurrence_ordinal": self.occurrence_ordinal,
        }

    @classmethod
    def from_manifest(cls, value: Any) -> "AllocationOrigin":
        required = {
            "source_revision",
            "extractor_contract_id",
            "locator",
            "occurrence_ordinal",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise IdentityError("identity_manifest_invalid")
        return cls(
            value["source_revision"],
            value["extractor_contract_id"],
            AllocationLocator.from_manifest(value["locator"]),
            value["occurrence_ordinal"],
        )


def digest(domain: str, *components: bytes) -> str:
    """Return the ADR's domain-separated SHA-256 digest framing."""
    if not isinstance(domain, str):
        raise TypeError("Identity digest domain must be text")
    domain_bytes = domain.encode("utf-8")
    framed = bytearray(b"honeymoney.identity\x00")
    framed.extend(struct.pack(">I", len(domain_bytes)))
    framed.extend(domain_bytes)
    framed.extend(struct.pack(">I", len(components)))
    for component in components:
        if not isinstance(component, bytes):
            raise TypeError("Identity digest components must be bytes")
        framed.extend(struct.pack(">Q", len(component)))
        framed.extend(component)
    return hashlib.sha256(framed).hexdigest()


def logical_locator(path: Path, workspace_root: Path) -> tuple[str, str]:
    """Return the in-memory locator kind and normalized locator text.

    Callers must not store the second value in reports, diagnostics, or manifests.
    """
    resolved_path = Path(path).resolve(strict=True)
    resolved_root = Path(workspace_root).resolve(strict=True)
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError:
        return "external", _normalize_locator_text(resolved_path)
    return "workspace", _normalize_locator_text(relative)


def source_namespace_id(locator_kind: str, locator: str) -> str:
    if locator_kind not in {"workspace", "external"}:
        raise IdentityError("identity_manifest_invalid")
    locator_bytes = locator.encode("utf-8")
    return "ns_" + digest(
        "source-namespace-v1", locator_kind.encode("utf-8"), locator_bytes
    )


def source_revision(source_bytes: bytes) -> str:
    if not isinstance(source_bytes, bytes):
        raise TypeError("Source revision requires exact source bytes")
    return "rev_" + digest("source-revision-v1", source_bytes)


def source_id(namespace_id: str) -> str:
    _require_match(SOURCE_NAMESPACE_ID_RE, namespace_id)
    return "src_" + digest("source-id-v2", namespace_id.encode("ascii"))


def resolve_sources(
    manifest: Mapping[str, Any],
    legacy_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    incoming_sources: list[IncomingSourceIdentity] | tuple[IncomingSourceIdentity, ...],
    intent: str,
) -> SourceResolutionResult:
    """Resolve a full incoming source batch without changing its inputs.

    This is the ADR's source table only. Record matching and manifest updates
    happen after this function returns a complete assignment.
    """
    action = _source_resolution_action(intent)
    incoming = tuple(incoming_sources)
    if not all(isinstance(source, IncomingSourceIdentity) for source in incoming):
        raise IdentityError("identity_manifest_invalid")
    if len({source.stable_handle for source in incoming}) != len(incoming):
        raise IdentityError("identity_manifest_invalid")
    ordered_incoming = tuple(sorted(incoming, key=lambda source: source.stable_handle))
    prior_sources = _resolution_manifest_sources(manifest)

    legacy_displays = _legacy_source_displays(legacy_rows)
    legacy_claims = {
        source.stable_handle: source
        for source in ordered_incoming
        if source.source_display in legacy_displays
    }
    legacy_conflicts = _legacy_claim_conflicts(legacy_claims, action)
    if legacy_conflicts:
        _raise_first_resolution_error(legacy_conflicts)

    namespace_conflicts = _incoming_namespace_conflicts(ordered_incoming, action)
    if namespace_conflicts:
        _raise_first_resolution_error(namespace_conflicts)

    by_namespace: dict[str, list[dict[str, str]]] = {}
    for prior in prior_sources:
        by_namespace.setdefault(prior["source_namespace_id"], []).append(prior)
    legacy_v2_conflicts = _legacy_v2_claim_conflicts(
        legacy_claims, by_namespace, action
    )
    if legacy_v2_conflicts:
        _raise_first_resolution_error(legacy_v2_conflicts)

    if action == "import" and legacy_claims:
        _raise_first_resolution_error(
            [
                _source_diagnostic(
                    "identity_source_already_imported", source, action, 1
                )
                for source in legacy_claims.values()
            ]
        )

    assignments: dict[str, tuple[str, str]] = {}
    for source in legacy_claims.values():
        assignments[source.stable_handle] = (source_id(source.namespace_id), "legacy")

    remaining = tuple(
        source
        for source in ordered_incoming
        if source.stable_handle not in legacy_claims
    )

    exact_candidates = {
        source.stable_handle: tuple(by_namespace.get(source.namespace_id, ()))
        for source in remaining
    }
    namespace_errors = [
        _source_diagnostic(
            "identity_source_namespace_ambiguous", source, action, len(candidates)
        )
        for source in remaining
        if len(candidates := exact_candidates[source.stable_handle]) > 1
    ]
    exact_claimants: dict[str, list[IncomingSourceIdentity]] = {}
    for source in remaining:
        candidates = exact_candidates[source.stable_handle]
        if len(candidates) == 1:
            exact_claimants.setdefault(candidates[0]["source_id"], []).append(source)
    for claimants in exact_claimants.values():
        if len(claimants) > 1:
            namespace_errors.extend(
                _source_diagnostic(
                    "identity_source_namespace_ambiguous",
                    source,
                    action,
                    len(claimants),
                )
                for source in claimants
            )
    if namespace_errors:
        _raise_first_resolution_error(namespace_errors)

    exact_claimed_ids = set(exact_claimants)
    exact_sources = {
        source.stable_handle: exact_candidates[source.stable_handle][0]
        for source in remaining
        if len(exact_candidates[source.stable_handle]) == 1
    }
    if action == "import" and exact_sources:
        _raise_first_resolution_error(
            [
                _source_diagnostic(
                    "identity_source_already_imported", source, action, 1
                )
                for source in remaining
                if source.stable_handle in exact_sources
            ]
        )

    for source in remaining:
        exact = exact_sources.get(source.stable_handle)
        if exact is not None:
            assignments[source.stable_handle] = (exact["source_id"], "reused")

    unmatched = tuple(
        source for source in remaining if source.stable_handle not in exact_sources
    )
    if action == "import":
        for source in unmatched:
            assignments[source.stable_handle] = (source_id(source.namespace_id), "new")
        return _source_resolution_result(ordered_incoming, assignments)

    revision_candidates = {
        source.stable_handle: tuple(
            prior
            for prior in prior_sources
            if prior["source_revision"] == source.revision
            and prior["source_id"] not in exact_claimed_ids
        )
        for source in unmatched
    }
    revision_errors = [
        _source_diagnostic(
            "identity_source_revision_ambiguous", source, action, len(candidates)
        )
        for source in unmatched
        if len(candidates := revision_candidates[source.stable_handle]) > 1
    ]
    revision_claimants: dict[str, list[IncomingSourceIdentity]] = {}
    for source in unmatched:
        candidates = revision_candidates[source.stable_handle]
        if len(candidates) == 1:
            revision_claimants.setdefault(candidates[0]["source_id"], []).append(source)
    for claimants in revision_claimants.values():
        if len(claimants) > 1:
            revision_errors.extend(
                _source_diagnostic(
                    "identity_source_revision_ambiguous", source, action, len(claimants)
                )
                for source in claimants
            )
    if revision_errors:
        _raise_first_resolution_error(revision_errors)

    missing = [
        source for source in unmatched if not revision_candidates[source.stable_handle]
    ]
    if missing:
        _raise_first_resolution_error(
            [
                _source_diagnostic(
                    "identity_source_target_not_found", source, action, 0
                )
                for source in missing
            ]
        )

    for source in unmatched:
        assignments[source.stable_handle] = (
            revision_candidates[source.stable_handle][0]["source_id"],
            "reused",
        )
    return _source_resolution_result(ordered_incoming, assignments)


def resolve_records(
    assignment: ResolvedSourceIdentity,
    incoming_records: list[IncomingRecordIdentity] | tuple[IncomingRecordIdentity, ...],
    prior_source: Mapping[str, Any] | None,
    prior_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    intent: str,
    *,
    record_id_factory: Callable[[str, AllocationOrigin, str], str] | None = None,
    transaction_id_factory: Callable[[str, str], str] | None = None,
) -> RecordResolutionResult:
    """Resolve records for one already-resolved source without mutating inputs.

    ``prior_rows`` is the target source's ledger slice. It can contain active
    v2 rows and all-empty legacy rows. The caller keeps unrelated ledger rows
    outside this bounded operation.
    """
    action = _source_resolution_action(intent)
    if not isinstance(assignment, ResolvedSourceIdentity):
        raise IdentityError("identity_manifest_invalid")
    incoming = tuple(incoming_records)
    if not all(isinstance(item, IncomingRecordIdentity) for item in incoming):
        raise IdentityError("identity_manifest_invalid")
    record_id_factory = record_id_factory or source_record_id
    transaction_id_factory = transaction_id_factory or transaction_id
    if not callable(record_id_factory) or not callable(transaction_id_factory):
        raise IdentityError("identity_manifest_invalid")

    rows = tuple(prior_rows)
    if not all(isinstance(row, Mapping) for row in rows):
        raise IdentityError("identity_manifest_invalid")
    _validate_v2_row_states((*rows, *(item.row for item in incoming)))
    _validate_incoming_locators(incoming)

    source = _prior_source_for_assignment(prior_source, assignment)
    records = tuple(copy.deepcopy(source["records"])) if source else ()
    active = tuple(record for record in records if record["state"] == "active")
    retired = tuple(record for record in records if record["state"] == "retired")
    _validate_ledger_manifest_agreement(rows, source, assignment, active)

    fingerprints = tuple(record_fingerprint(item.row) for item in incoming)
    origins = _incoming_origins(assignment, incoming, fingerprints)
    exact_current = source is not None and (
        source["source_revision"] == assignment.source_revision
        and source["extractor_contract_id"] == assignment.extractor_contract_id
    )
    if exact_current:
        resolved = _resolve_exact_records(assignment, incoming, fingerprints, active)
        return _record_result(
            assignment,
            records,
            resolved,
            (),
            (),
            (),
        )

    recurrence: dict[int, dict[str, Any]] = {}
    retired_by_origin = {_ownership_origin_key(record): record for record in retired}
    for index, (origin, fingerprint) in enumerate(zip(origins, fingerprints)):
        record = retired_by_origin.get(_origin_key(origin, fingerprint))
        if record is not None:
            recurrence[index] = record

    legacy_rows = tuple(copy.deepcopy(row) for row in rows if _v2_state(row) == "empty")
    duplicate_legacy_ids = _duplicate_legacy_ids(legacy_rows)
    legacy_diagnostics: list[RecordResolutionDiagnostic] = []
    if duplicate_legacy_ids:
        if action in {"replace", "reset"}:
            raise IdentityError("identity_legacy_transaction_id_ambiguous")
        legacy_diagnostics.append(
            _record_diagnostic(
                "identity_legacy_transaction_id_ambiguous",
                assignment,
                action,
                len(duplicate_legacy_ids),
            )
        )
        return _resolve_with_protected_legacy(
            assignment,
            incoming,
            fingerprints,
            origins,
            records,
            legacy_rows,
            legacy_diagnostics,
            record_id_factory,
            transaction_id_factory,
        )

    remaining_indexes = tuple(
        index for index in range(len(incoming)) if index not in recurrence
    )
    candidates: list[tuple[str, int, dict[str, Any] | Mapping[str, Any]]] = []
    candidates.extend(
        (record["record_fingerprint"], -index - 1, record)
        for index, record in enumerate(active)
        if record not in recurrence.values()
    )
    candidates.extend(
        (record_fingerprint(row), index, row) for index, row in enumerate(legacy_rows)
    )
    matches, ambiguous = _unique_fingerprint_matches(
        candidates, remaining_indexes, fingerprints
    )
    if ambiguous:
        if any(candidate_index >= 0 for _, candidate_index, _ in candidates):
            if action in {"replace", "reset"}:
                raise IdentityError("identity_legacy_transaction_id_ambiguous")
            legacy_diagnostics.append(
                _record_diagnostic(
                    "identity_legacy_transaction_id_ambiguous",
                    assignment,
                    action,
                    len(legacy_rows),
                )
            )
            return _resolve_with_protected_legacy(
                assignment,
                incoming,
                fingerprints,
                origins,
                records,
                legacy_rows,
                legacy_diagnostics,
                record_id_factory,
                transaction_id_factory,
            )
        raise IdentityError("identity_record_match_ambiguous")

    resolved_owners: dict[int, dict[str, Any]] = dict(recurrence)
    matched_active = {record["source_record_id"] for record in recurrence.values()}
    retained_legacy: tuple[Mapping[str, Any], ...] = ()
    for incoming_index, candidate in matches.items():
        _, candidate_index, value = candidate
        if candidate_index < 0:
            owner = copy.deepcopy(value)
            matched_active.add(value["source_record_id"])
        else:
            legacy_row = value
            owner = _legacy_owner(
                assignment,
                origins[incoming_index],
                fingerprints[incoming_index],
                _manifest_text(
                    legacy_row.get("transaction_id"), TRANSACTION_ID_LEGACY_RE
                ),
            )
        resolved_owners[incoming_index] = owner

    updated: list[dict[str, Any]] = []
    retired_ids: list[str] = []
    for record in records:
        if record["source_record_id"] in matched_active:
            continue
        if record["state"] == "active":
            retired_record = copy.deepcopy(record)
            retired_record["state"] = "retired"
            retired_record["current_locator"] = None
            updated.append(retired_record)
            retired_ids.append(retired_record["transaction_id"])
        else:
            updated.append(copy.deepcopy(record))

    for index, item in enumerate(incoming):
        owner = resolved_owners.get(index)
        if owner is None:
            owner = _new_owner(
                assignment,
                origins[index],
                fingerprints[index],
                record_id_factory,
                transaction_id_factory,
            )
        owner = copy.deepcopy(owner)
        owner["state"] = "active"
        owner["current_locator"] = item.locator.as_manifest()
        resolved_owners[index] = owner
        updated.append(owner)

    return _record_result(
        assignment,
        updated,
        _resolved_rows(
            assignment,
            incoming,
            resolved_owners,
            origins,
            fingerprints,
            record_id_factory,
            transaction_id_factory,
        ),
        retained_legacy,
        tuple(sorted(retired_ids)),
        tuple(legacy_diagnostics),
    )


def resolve_batch(
    *,
    ledger_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    manifest: Mapping[str, Any],
    sources: list[IncomingSourceIdentity] | tuple[IncomingSourceIdentity, ...],
    intent: str,
) -> IdentityResolution:
    """Resolve an import batch without changing the ledger or manifest inputs."""
    action = _source_resolution_action(intent)
    rows = tuple(ledger_rows)
    incoming_sources = tuple(sources)
    if not all(isinstance(row, Mapping) for row in rows) or not all(
        isinstance(source, IncomingSourceIdentity) for source in incoming_sources
    ):
        raise IdentityError("identity_manifest_invalid")
    for source in incoming_sources:
        if not isinstance(source.record_data, tuple) or not all(
            isinstance(record, IncomingRecordIdentity) for record in source.record_data
        ):
            raise IdentityError("identity_manifest_invalid")

    prior_manifest = copy.deepcopy(dict(manifest))
    validate_manifest(prior_manifest, require_canonical_order=False)
    _validate_v2_row_states(rows)
    _validate_global_ledger_manifest_agreement(rows, prior_manifest)

    v2_rows_by_source: dict[str, list[Mapping[str, Any]]] = {}
    legacy_rows_by_display: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if _v2_state(row) == "full":
            v2_rows_by_source.setdefault(_text(row.get("source_id")), []).append(row)
        else:
            legacy_rows_by_display.setdefault(_legacy_display(row), []).append(row)

    legacy_id_counts = _legacy_transaction_id_counts(rows)
    ambiguous_legacy_displays = frozenset(
        display
        for display, group in legacy_rows_by_display.items()
        if any(legacy_id_counts[_text(row.get("transaction_id"))] > 1 for row in group)
    )
    incoming_displays = {source.source_display for source in incoming_sources}
    targeted_ambiguous_displays = ambiguous_legacy_displays & incoming_displays
    if action in {"replace", "reset"} and targeted_ambiguous_displays:
        raise IdentityError("identity_legacy_transaction_id_ambiguous")

    source_legacy_rows = rows
    if action == "import" and targeted_ambiguous_displays:
        source_legacy_rows = tuple(
            row
            for row in rows
            if _v2_state(row) != "empty"
            or _legacy_display(row) not in targeted_ambiguous_displays
        )
    source_result = resolve_sources(
        prior_manifest, source_legacy_rows, incoming_sources, intent
    )

    prior_sources = {
        source["source_id"]: source for source in prior_manifest["sources"]
    }
    record_results: dict[str, RecordResolutionResult] = {}
    protected_legacy: dict[str, tuple[Mapping[str, Any], ...]] = {}
    diagnostics: list[SourceResolutionDiagnostic | RecordResolutionDiagnostic] = []
    replaced_source_ids: set[str] = set()

    for assignment in source_result.assignments:
        incoming = next(
            source
            for source in incoming_sources
            if source.stable_handle == assignment.stable_handle
        )
        legacy_group = tuple(legacy_rows_by_display.get(assignment.source_display, ()))
        prior_source = prior_sources.get(assignment.source_id)
        target_legacy = assignment.disposition == "legacy" or (
            action == "import"
            and assignment.source_display in targeted_ambiguous_displays
        )
        prior_rows = tuple(v2_rows_by_source.get(assignment.source_id, ()))

        if assignment.source_display in targeted_ambiguous_displays:
            result = resolve_records(
                assignment, incoming.record_data, prior_source, prior_rows, intent
            )
            protected_legacy[assignment.source_display] = tuple(
                _protect_legacy_row(row) for row in legacy_group
            )
            diagnostics.append(
                _record_diagnostic(
                    "identity_legacy_transaction_id_ambiguous",
                    assignment,
                    action,
                    len(legacy_group),
                )
            )
        else:
            result = resolve_records(
                assignment,
                incoming.record_data,
                prior_source,
                (*prior_rows, *(legacy_group if target_legacy else ())),
                intent,
            )
            if result.retained_legacy_rows:
                protected_legacy[assignment.source_display] = tuple(
                    _protect_legacy_row(row) for row in result.retained_legacy_rows
                )
            elif target_legacy and legacy_group:
                migrated_ids = {
                    _text(resolved.row.get("transaction_id"))
                    for resolved in result.resolved_rows
                }
                if any(
                    _text(row.get("transaction_id")) not in migrated_ids
                    for row in legacy_group
                ):
                    if action in {"replace", "reset"}:
                        raise IdentityError("identity_legacy_transaction_id_ambiguous")
                    protected_legacy[assignment.source_display] = tuple(
                        _protect_legacy_row(row) for row in legacy_group
                    )
                    diagnostics.append(
                        _record_diagnostic(
                            "identity_legacy_transaction_id_ambiguous",
                            assignment,
                            action,
                            len(legacy_group),
                        )
                    )
            diagnostics.extend(result.diagnostics)
        record_results[assignment.stable_handle] = result
        if action in {"replace", "reset"}:
            replaced_source_ids.add(assignment.source_id)

    target_legacy_displays = {
        assignment.source_display
        for assignment in source_result.assignments
        if assignment.disposition == "legacy"
        or assignment.source_display in targeted_ambiguous_displays
    }
    retained: list[Mapping[str, Any]] = []
    for row in rows:
        state = _v2_state(row)
        if state == "full" and _text(row.get("source_id")) in replaced_source_ids:
            continue
        if state == "empty" and _legacy_display(row) in target_legacy_displays:
            continue
        retained.append(copy.deepcopy(dict(row)))
    for display in sorted(protected_legacy):
        retained.extend(copy.deepcopy(dict(row)) for row in protected_legacy[display])

    resolved_rows: list[Mapping[str, Any]] = []
    next_sources = [
        copy.deepcopy(source)
        for source in prior_manifest["sources"]
        if source["source_id"] not in replaced_source_ids
    ]
    for assignment in source_result.assignments:
        result = record_results[assignment.stable_handle]
        resolved_rows.extend(
            copy.deepcopy(dict(row.row)) for row in result.resolved_rows
        )
        next_sources.append(copy.deepcopy(dict(result.source_ownership)))
    _validate_batch_ownership_collisions(next_sources)
    next_manifest = _sorted_manifest(
        {"schema_version": MANIFEST_SCHEMA_VERSION, "sources": next_sources}
    )
    validate_manifest(next_manifest)

    _validate_global_output_collisions((*retained, *resolved_rows))
    reset_ids = (
        tuple(
            sorted(
                {
                    identifier
                    for result in record_results.values()
                    for identifier in result.reset_transaction_ids
                }
            )
        )
        if action == "reset"
        else ()
    )
    diagnostics.sort(key=lambda item: (item.code, item.source_display))
    return IdentityResolution(
        resolved_rows=tuple(resolved_rows),
        next_manifest=next_manifest,
        retained_ledger_rows=tuple(retained),
        replaced_source_ids=tuple(sorted(replaced_source_ids)),
        reset_transaction_ids=reset_ids,
        diagnostics=tuple(diagnostics),
    )


def record_fingerprint(row: Mapping[str, Any]) -> str:
    """Calculate the normalized immutable financial record fingerprint."""
    fields = (
        _normalize_folded_text(row.get("account_id", "")),
        _normalize_iso_date(row.get("date", "")),
        _normalize_iso_date(row.get("transaction_date", "")),
        _normalize_iso_date(row.get("posting_date", "")),
        _normalize_decimal(row.get("original_amount", "")),
        _normalize_currency(row.get("original_currency", "")),
        _normalize_decimal(row.get("posted_amount", "")),
        _normalize_currency(row.get("posted_currency", "")),
        _normalize_folded_text(row.get("merchant", "")),
        _normalize_folded_text(row.get("original_description", "")),
    )
    return "fp_" + digest(
        "record-fingerprint-v2", *(field.encode("utf-8") for field in fields)
    )


def allocation_locator_bytes(locator: AllocationLocator) -> bytes:
    _validate_locator(locator.adapter_tag, locator.components)
    output = bytearray(b"honeymoney.record-locator-v1\x00")
    output.extend(struct.pack(">B", locator.adapter_tag))
    output.extend(struct.pack(">B", len(locator.components)))
    for component in locator.components:
        output.extend(struct.pack(">Q", component))
    return bytes(output)


def source_record_id(
    source_id_value: str,
    origin: AllocationOrigin,
    fingerprint: str,
) -> str:
    _require_match(SOURCE_ID_RE, source_id_value)
    _require_match(RECORD_FINGERPRINT_RE, fingerprint)
    return "rec_" + digest(
        "source-record-v2",
        source_id_value.encode("ascii"),
        origin.source_revision.encode("ascii"),
        origin.extractor_contract_id.encode("ascii"),
        fingerprint.encode("ascii"),
        allocation_locator_bytes(origin.locator),
        struct.pack(">Q", origin.occurrence_ordinal),
    )


def transaction_id(source_id_value: str, source_record_id_value: str) -> str:
    _require_match(SOURCE_ID_RE, source_id_value)
    _require_match(SOURCE_RECORD_ID_RE, source_record_id_value)
    return (
        "txn_"
        + digest(
            "transaction-id-v2",
            b"2",
            source_id_value.encode("ascii"),
            source_record_id_value.encode("ascii"),
        )[:32]
    )


def _validate_v2_row_states(rows: tuple[Mapping[str, Any], ...]) -> None:
    for row in rows:
        if _v2_state(row) == "partial":
            raise IdentityError("identity_partial_v2_metadata")


def _validate_global_ledger_manifest_agreement(
    rows: tuple[Mapping[str, Any], ...], manifest: Mapping[str, Any]
) -> None:
    """Require every active ownership record and v2 row to agree globally."""
    expected: dict[tuple[str, str], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for source in manifest["sources"]:
        for record in source["records"]:
            if record["state"] != "active":
                continue
            key = (record["source_record_id"], record["transaction_id"])
            expected[key] = (source, record)
    actual: set[tuple[str, str]] = set()
    for row in rows:
        if _v2_state(row) != "full":
            continue
        key = (_text(row.get("source_record_id")), _text(row.get("transaction_id")))
        if key in actual or key not in expected:
            raise IdentityError("identity_manifest_invalid")
        source, record = expected[key]
        if (
            any(
                _text(row.get(field)) != source[field]
                for field in ("source_id", "source_namespace_id", "source_revision")
            )
            or record_fingerprint(row) != record["record_fingerprint"]
        ):
            raise IdentityError("identity_manifest_invalid")
        actual.add(key)
    if actual != set(expected):
        raise IdentityError("identity_manifest_invalid")


def validate_ledger_manifest_agreement(
    ledger_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    manifest: Mapping[str, Any],
) -> None:
    """Validate the complete authoritative ledger and manifest together."""
    rows = tuple(ledger_rows)
    if not all(isinstance(row, Mapping) for row in rows):
        raise IdentityError("identity_manifest_invalid")
    validate_manifest(manifest)
    _validate_v2_row_states(rows)
    for row in rows:
        if _v2_state(row) != "empty":
            continue
        identifier = _text(row.get("transaction_id"))
        if identifier:
            _require_match(TRANSACTION_ID_LEGACY_RE, identifier)
    _validate_global_ledger_manifest_agreement(rows, manifest)


def has_stable_v2_identity(row: Mapping[str, Any]) -> bool:
    """Return whether a row carries a complete, current v2 identity.

    This predicate is for consumers of validated ledger data that need to
    reject legacy or partial identity rows without reaching into resolver
    internals. It does not replace ledger/manifest validation.
    """
    if not isinstance(row, Mapping):
        return False
    return (
        all(
            pattern.fullmatch(_text(row.get(field))) is not None
            for field, pattern in (
                ("source_id", SOURCE_ID_RE),
                ("source_namespace_id", SOURCE_NAMESPACE_ID_RE),
                ("source_revision", SOURCE_REVISION_RE),
                ("source_record_id", SOURCE_RECORD_ID_RE),
            )
        )
        and TRANSACTION_ID_V2_RE.fullmatch(_text(row.get("transaction_id"))) is not None
    )


def ambiguous_legacy_transaction_ids(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> frozenset[str]:
    """Return all-empty legacy IDs claimed by more than one ledger row."""
    counts: dict[str, int] = {}
    for row in rows:
        if _v2_state(row) != "empty":
            continue
        identifier = _text(row.get("transaction_id"))
        if identifier:
            counts[identifier] = counts.get(identifier, 0) + 1
    return frozenset(identifier for identifier, count in counts.items() if count > 1)


def _legacy_display(row: Mapping[str, Any]) -> str:
    value = row.get("source_file", "")
    if not isinstance(value, str):
        raise IdentityError("identity_manifest_invalid")
    return unicodedata.normalize("NFC", value)


def _legacy_transaction_id_counts(
    rows: tuple[Mapping[str, Any], ...],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if _v2_state(row) != "empty":
            continue
        identifier = _text(row.get("transaction_id"))
        _require_match(TRANSACTION_ID_LEGACY_RE, identifier)
        counts[identifier] = counts.get(identifier, 0) + 1
    return counts


def _protect_legacy_row(row: Mapping[str, Any]) -> Mapping[str, Any]:
    protected = copy.deepcopy(dict(row))
    flags = [part.strip() for part in _text(protected.get("flags")).split(";")]
    flags = [flag for flag in flags if flag]
    if "identity_migration_ambiguous" not in flags:
        flags.append("identity_migration_ambiguous")
    protected["flags"] = ";".join(flags)
    protected["reason"] = (
        "Identity migration is ambiguous; explicit resolution is required"
    )
    protected["needs_review"] = "true"
    return protected


def _validate_global_output_collisions(rows: tuple[Mapping[str, Any], ...]) -> None:
    source_record_ids: set[str] = set()
    owned_transaction_ids: set[str] = set()
    legacy_transaction_ids: set[str] = set()
    for row in rows:
        state = _v2_state(row)
        identifier = _text(row.get("transaction_id"))
        if state == "empty":
            _require_match(TRANSACTION_ID_LEGACY_RE, identifier)
            legacy_transaction_ids.add(identifier)
            continue
        source_record = _text(row.get("source_record_id"))
        _require_match(SOURCE_ID_RE, _text(row.get("source_id")))
        _require_match(SOURCE_NAMESPACE_ID_RE, _text(row.get("source_namespace_id")))
        _require_match(SOURCE_REVISION_RE, _text(row.get("source_revision")))
        _require_match(SOURCE_RECORD_ID_RE, source_record)
        if TRANSACTION_ID_V2_RE.fullmatch(identifier) is None:
            _require_match(TRANSACTION_ID_LEGACY_RE, identifier)
        if source_record in source_record_ids or identifier in owned_transaction_ids:
            raise IdentityError("identity_hash_conflict")
        source_record_ids.add(source_record)
        owned_transaction_ids.add(identifier)
    if owned_transaction_ids & legacy_transaction_ids:
        raise IdentityError("identity_hash_conflict")


def _validate_batch_ownership_collisions(sources: list[Mapping[str, Any]]) -> None:
    source_ids: set[str] = set()
    record_inputs: dict[str, tuple[str, str, str, str, AllocationLocator, int]] = {}
    transaction_inputs: dict[str, tuple[str, str]] = {}
    for source in sources:
        source_id_value = _text(source.get("source_id"))
        if source_id_value in source_ids:
            raise IdentityError("identity_hash_conflict")
        source_ids.add(source_id_value)
        for record in source.get("records", []):
            origin = AllocationOrigin.from_manifest(record["allocation_origin"])
            record_id = record["source_record_id"]
            record_input = (
                source_id_value,
                *_origin_key(origin, record["record_fingerprint"]),
            )
            if record_id in record_inputs and record_inputs[record_id] != record_input:
                raise IdentityError("identity_hash_conflict")
            record_inputs[record_id] = record_input
            identifier = record["transaction_id"]
            transaction_input = (source_id_value, record_id)
            if (
                identifier in transaction_inputs
                and transaction_inputs[identifier] != transaction_input
            ):
                raise IdentityError("identity_hash_conflict")
            transaction_inputs[identifier] = transaction_input


def _v2_state(row: Mapping[str, Any]) -> str:
    populated = tuple(_text(row.get(field, "")) != "" for field in ID_FIELDS)
    if all(populated):
        return "full"
    if not any(populated):
        return "empty"
    return "partial"


def _validate_incoming_locators(incoming: tuple[IncomingRecordIdentity, ...]) -> None:
    if len({item.locator for item in incoming}) != len(incoming):
        raise IdentityError("identity_allocation_locator_invalid")


def _prior_source_for_assignment(
    prior_source: Mapping[str, Any] | None, assignment: ResolvedSourceIdentity
) -> dict[str, Any] | None:
    if prior_source is None:
        return None
    copied = copy.deepcopy(dict(prior_source))
    _validate_source_ownership(copied, set(), set(), set(), set(), False)
    if copied["source_id"] != assignment.source_id:
        raise IdentityError("identity_manifest_invalid")
    return copied


def _validate_ledger_manifest_agreement(
    rows: tuple[Mapping[str, Any], ...],
    source: Mapping[str, Any] | None,
    assignment: ResolvedSourceIdentity,
    active: tuple[dict[str, Any], ...],
) -> None:
    if source is None:
        if any(_v2_state(row) == "full" for row in rows):
            raise IdentityError("identity_manifest_invalid")
        return
    expected = {
        (record["source_record_id"], record["transaction_id"]): record
        for record in active
    }
    actual: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows:
        if _v2_state(row) != "full":
            continue
        if _text(row.get("source_id")) != assignment.source_id:
            raise IdentityError("identity_manifest_invalid")
        key = (_text(row.get("source_record_id")), _text(row.get("transaction_id")))
        if key in actual or key not in expected:
            raise IdentityError("identity_manifest_invalid")
        if (
            _text(row.get("source_namespace_id")) != source["source_namespace_id"]
            or _text(row.get("source_revision")) != source["source_revision"]
        ):
            raise IdentityError("identity_manifest_invalid")
        actual[key] = row
    if set(actual) != set(expected):
        raise IdentityError("identity_manifest_invalid")


def _incoming_origins(
    assignment: ResolvedSourceIdentity,
    incoming: tuple[IncomingRecordIdentity, ...],
    fingerprints: tuple[str, ...],
) -> tuple[AllocationOrigin, ...]:
    counts: dict[str, int] = {}
    origins: list[AllocationOrigin | None] = [None] * len(incoming)
    for index in sorted(range(len(incoming)), key=lambda item: incoming[item].locator):
        fingerprint = fingerprints[index]
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        origins[index] = AllocationOrigin(
            assignment.source_revision,
            assignment.extractor_contract_id,
            incoming[index].locator,
            counts[fingerprint],
        )
    return tuple(origin for origin in origins if origin is not None)


def _origin_key(
    origin: AllocationOrigin, fingerprint: str
) -> tuple[str, str, str, AllocationLocator, int]:
    return (
        origin.source_revision,
        origin.extractor_contract_id,
        fingerprint,
        origin.locator,
        origin.occurrence_ordinal,
    )


def _ownership_origin_key(
    record: Mapping[str, Any],
) -> tuple[str, str, str, AllocationLocator, int]:
    origin = AllocationOrigin.from_manifest(record["allocation_origin"])
    return _origin_key(origin, record["record_fingerprint"])


def _resolve_exact_records(
    assignment: ResolvedSourceIdentity,
    incoming: tuple[IncomingRecordIdentity, ...],
    fingerprints: tuple[str, ...],
    active: tuple[dict[str, Any], ...],
) -> tuple[ResolvedRecordIdentity, ...]:
    expected = {ownership_exact_state_key(record): record for record in active}
    if len(expected) != len(active):
        raise IdentityError("identity_manifest_invalid")
    incoming_keys = tuple(
        exact_state_key(item.locator, fingerprint)
        for item, fingerprint in zip(incoming, fingerprints)
    )
    if len(set(incoming_keys)) != len(incoming_keys) or set(incoming_keys) != set(
        expected
    ):
        raise IdentityError("identity_exact_state_mismatch")
    return tuple(
        _resolved_row(assignment, item, expected[key])
        for item, key in zip(incoming, incoming_keys)
    )


def _unique_fingerprint_matches(
    candidates: list[tuple[str, int, dict[str, Any] | Mapping[str, Any]]],
    incoming_indexes: tuple[int, ...],
    fingerprints: tuple[str, ...],
) -> tuple[dict[int, tuple[str, int, dict[str, Any] | Mapping[str, Any]]], bool]:
    by_fingerprint: dict[
        str, list[tuple[str, int, dict[str, Any] | Mapping[str, Any]]]
    ] = {}
    for candidate in candidates:
        by_fingerprint.setdefault(candidate[0], []).append(candidate)
    incoming_by_fingerprint: dict[str, list[int]] = {}
    for index in incoming_indexes:
        incoming_by_fingerprint.setdefault(fingerprints[index], []).append(index)
    matches: dict[int, tuple[str, int, dict[str, Any] | Mapping[str, Any]]] = {}
    for fingerprint in set(by_fingerprint) | set(incoming_by_fingerprint):
        left = by_fingerprint.get(fingerprint, [])
        right = incoming_by_fingerprint.get(fingerprint, [])
        if left and right and (len(left) != 1 or len(right) != 1):
            return {}, True
        if left and right:
            matches[right[0]] = left[0]
    return matches, False


def _duplicate_legacy_ids(rows: tuple[Mapping[str, Any], ...]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        identifier = _text(row.get("transaction_id"))
        _require_match(TRANSACTION_ID_LEGACY_RE, identifier)
        counts[identifier] = counts.get(identifier, 0) + 1
    return tuple(
        sorted(identifier for identifier, count in counts.items() if count > 1)
    )


def _legacy_owner(
    assignment: ResolvedSourceIdentity,
    origin: AllocationOrigin,
    fingerprint: str,
    identifier: str,
) -> dict[str, Any]:
    return ownership_record(
        source_id_value=assignment.source_id,
        fingerprint=fingerprint,
        origin=origin,
        transaction_id_kind="preserved_legacy",
        preserved_transaction_id=identifier,
    )


def _new_owner(
    assignment: ResolvedSourceIdentity,
    origin: AllocationOrigin,
    fingerprint: str,
    record_id_factory: Callable[[str, AllocationOrigin, str], str],
    transaction_id_factory: Callable[[str, str], str],
) -> dict[str, Any]:
    record_id = record_id_factory(assignment.source_id, origin, fingerprint)
    identifier = transaction_id_factory(assignment.source_id, record_id)
    _require_match(SOURCE_RECORD_ID_RE, record_id)
    _require_match(TRANSACTION_ID_V2_RE, identifier)
    return {
        "source_record_id": record_id,
        "transaction_id": identifier,
        "transaction_id_kind": "v2",
        "record_fingerprint": fingerprint,
        "state": "active",
        "current_locator": origin.locator.as_manifest(),
        "allocation_origin": origin.as_manifest(),
    }


def _resolved_rows(
    assignment: ResolvedSourceIdentity,
    incoming: tuple[IncomingRecordIdentity, ...],
    owners: Mapping[int, Mapping[str, Any]],
    origins: tuple[AllocationOrigin, ...],
    fingerprints: tuple[str, ...],
    record_id_factory: Callable[[str, AllocationOrigin, str], str],
    transaction_id_factory: Callable[[str, str], str],
) -> tuple[ResolvedRecordIdentity, ...]:
    return tuple(
        _resolved_row(
            assignment,
            item,
            owners.get(index)
            or _new_owner(
                assignment,
                origins[index],
                fingerprints[index],
                record_id_factory,
                transaction_id_factory,
            ),
        )
        for index, item in enumerate(incoming)
    )


def _resolved_row(
    assignment: ResolvedSourceIdentity,
    item: IncomingRecordIdentity,
    owner: Mapping[str, Any],
) -> ResolvedRecordIdentity:
    row = copy.deepcopy(dict(item.row))
    row.update(
        {
            "source_id": assignment.source_id,
            "source_namespace_id": assignment.source_namespace_id,
            "source_revision": assignment.source_revision,
            "source_record_id": owner["source_record_id"],
            "transaction_id": owner["transaction_id"],
        }
    )
    return ResolvedRecordIdentity(
        row=row,
        locator=item.locator,
        source_record_id=owner["source_record_id"],
        transaction_id=owner["transaction_id"],
        transaction_id_kind=owner["transaction_id_kind"],
    )


def _record_result(
    assignment: ResolvedSourceIdentity,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    resolved: tuple[ResolvedRecordIdentity, ...],
    retained_legacy_rows: tuple[Mapping[str, Any], ...],
    retired_ids: tuple[str, ...],
    diagnostics: tuple[RecordResolutionDiagnostic, ...],
) -> RecordResolutionResult:
    copied_records = [copy.deepcopy(record) for record in records]
    _validate_record_collisions(copied_records)
    copied_records.sort(key=lambda record: record["source_record_id"])
    ownership = source_ownership(
        source_id_value=assignment.source_id,
        namespace_id=assignment.source_namespace_id,
        revision=assignment.source_revision,
        contract_id=assignment.extractor_contract_id,
        records=copied_records,
    )
    return RecordResolutionResult(
        resolved_rows=resolved,
        source_ownership=ownership,
        retained_legacy_rows=tuple(copy.deepcopy(row) for row in retained_legacy_rows),
        retired_transaction_ids=retired_ids,
        reset_transaction_ids=tuple(
            sorted(record["transaction_id"] for record in copied_records)
        ),
        diagnostics=diagnostics,
    )


def _validate_record_collisions(records: list[dict[str, Any]]) -> None:
    record_inputs: dict[str, tuple[str, str, str, AllocationLocator, int]] = {}
    transaction_inputs: dict[str, tuple[str, str]] = {}
    for record in records:
        origin = AllocationOrigin.from_manifest(record["allocation_origin"])
        record_input = _origin_key(origin, record["record_fingerprint"])
        record_id = record["source_record_id"]
        if record_id in record_inputs and record_inputs[record_id] != record_input:
            raise IdentityError("identity_hash_conflict")
        record_inputs[record_id] = record_input
        transaction_input = (record_id, record["transaction_id_kind"])
        identifier = record["transaction_id"]
        if (
            identifier in transaction_inputs
            and transaction_inputs[identifier] != transaction_input
        ):
            raise IdentityError("identity_hash_conflict")
        transaction_inputs[identifier] = transaction_input


def _record_diagnostic(
    code: str,
    assignment: ResolvedSourceIdentity,
    action: str,
    affected_count: int,
) -> RecordResolutionDiagnostic:
    return RecordResolutionDiagnostic(
        code=code,
        source_display=assignment.source_display,
        action=action,
        affected_count=affected_count,
        remediation="identity is ambiguous; retain the source and request explicit resolution.",
    )


def _resolve_with_protected_legacy(
    assignment: ResolvedSourceIdentity,
    incoming: tuple[IncomingRecordIdentity, ...],
    fingerprints: tuple[str, ...],
    origins: tuple[AllocationOrigin, ...],
    records: tuple[dict[str, Any], ...],
    legacy_rows: tuple[Mapping[str, Any], ...],
    diagnostics: list[RecordResolutionDiagnostic],
    record_id_factory: Callable[[str, AllocationOrigin, str], str],
    transaction_id_factory: Callable[[str, str], str],
) -> RecordResolutionResult:
    owners = {
        index: _new_owner(
            assignment,
            origins[index],
            fingerprints[index],
            record_id_factory,
            transaction_id_factory,
        )
        for index in range(len(incoming))
    }
    updated = [copy.deepcopy(record) for record in records] + list(owners.values())
    return _record_result(
        assignment,
        updated,
        _resolved_rows(
            assignment,
            incoming,
            owners,
            origins,
            fingerprints,
            record_id_factory,
            transaction_id_factory,
        ),
        legacy_rows,
        (),
        tuple(diagnostics),
    )


def canonical_profile_json(profile: Mapping[str, Any]) -> bytes:
    """Return the normalized RFC-8785-style profile bytes for extractor IDs."""
    if not isinstance(profile, Mapping):
        raise IdentityError("identity_manifest_invalid")
    normalized = _normalize_json_value(dict(profile))
    if not isinstance(normalized, dict):
        raise IdentityError("identity_manifest_invalid")
    retained = {
        key: value
        for key, value in normalized.items()
        if key not in EXCLUDED_PROFILE_TOP_LEVEL_KEYS
    }
    return _canonical_json(retained).encode("utf-8")


def extractor_contract_id(adapter_tag: int, profile: Mapping[str, Any]) -> str:
    version = ADAPTER_VERSIONS.get(adapter_tag)
    if version is None:
        raise IdentityError("identity_allocation_locator_invalid")
    return "ext_" + digest(
        "extractor-contract-v1",
        version.encode("ascii"),
        canonical_profile_json(profile),
    )


def exact_state_key(
    locator: AllocationLocator, fingerprint: str
) -> tuple[AllocationLocator, str]:
    _require_match(RECORD_FINGERPRINT_RE, fingerprint)
    return locator, fingerprint


def ownership_exact_state_key(
    record: Mapping[str, Any],
) -> tuple[AllocationLocator, str]:
    if not isinstance(record, Mapping):
        raise IdentityError("identity_manifest_invalid")
    locator_value = record.get("current_locator")
    if locator_value is None:
        raise IdentityError("identity_manifest_invalid")
    return exact_state_key(
        AllocationLocator.from_manifest(locator_value),
        _manifest_text(record.get("record_fingerprint"), RECORD_FINGERPRINT_RE),
    )


def ownership_record(
    *,
    source_id_value: str,
    fingerprint: str,
    origin: AllocationOrigin,
    state: str = "active",
    transaction_id_kind: str = "v2",
    preserved_transaction_id: str | None = None,
    current_locator: AllocationLocator | None = None,
) -> dict[str, Any]:
    """Build one self-validating active or retired ownership record."""
    if state not in {"active", "retired"}:
        raise IdentityError("identity_manifest_invalid")
    _require_match(RECORD_FINGERPRINT_RE, fingerprint)
    record_id = source_record_id(source_id_value, origin, fingerprint)
    if transaction_id_kind == "v2":
        if preserved_transaction_id is not None:
            raise IdentityError("identity_manifest_invalid")
        identifier = transaction_id(source_id_value, record_id)
    elif transaction_id_kind == "preserved_legacy":
        if preserved_transaction_id is None:
            raise IdentityError("identity_manifest_invalid")
        _require_match(TRANSACTION_ID_LEGACY_RE, preserved_transaction_id)
        identifier = preserved_transaction_id
    else:
        raise IdentityError("identity_manifest_invalid")
    locator = current_locator if current_locator is not None else origin.locator
    return {
        "source_record_id": record_id,
        "transaction_id": identifier,
        "transaction_id_kind": transaction_id_kind,
        "record_fingerprint": fingerprint,
        "state": state,
        "current_locator": locator.as_manifest() if state == "active" else None,
        "allocation_origin": origin.as_manifest(),
    }


def source_ownership(
    *,
    source_id_value: str,
    namespace_id: str,
    revision: str,
    contract_id: str,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one manifest source entry without source text or paths."""
    _require_match(SOURCE_ID_RE, source_id_value)
    _require_match(SOURCE_NAMESPACE_ID_RE, namespace_id)
    _require_match(SOURCE_REVISION_RE, revision)
    _require_match(EXTRACTOR_CONTRACT_ID_RE, contract_id)
    return {
        "source_id": source_id_value,
        "source_namespace_id": namespace_id,
        "source_revision": revision,
        "extractor_contract_id": contract_id,
        "records": list(records or []),
    }


def empty_manifest() -> dict[str, Any]:
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "sources": []}


def manifest_path(categorized_path: Path) -> Path:
    return Path(categorized_path).parent / IDENTITY_MANIFEST_NAME


def manifest_document(manifest: Mapping[str, Any]) -> str:
    """Validate and serialize a manifest in its required canonical order."""
    normalized = _sorted_manifest(manifest)
    validate_manifest(normalized, require_canonical_order=True)
    return _canonical_json(normalized) + "\n"


def parse_manifest(document: str | bytes) -> dict[str, Any]:
    """Parse only canonical manifest JSON with no duplicate object keys."""
    if isinstance(document, bytes):
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IdentityError("identity_manifest_invalid") from error
    elif isinstance(document, str):
        text = document
    else:
        raise IdentityError("identity_manifest_invalid")
    if text.startswith("\ufeff"):
        raise IdentityError("identity_manifest_invalid")
    try:
        parsed = json.loads(text, object_pairs_hook=_json_object_without_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise IdentityError("identity_manifest_invalid") from error
    if not isinstance(parsed, dict):
        raise IdentityError("identity_manifest_invalid")
    validate_manifest(parsed, require_canonical_order=True)
    if manifest_document(parsed) != text:
        raise IdentityError("identity_manifest_invalid")
    return parsed


def validate_manifest(
    manifest: Mapping[str, Any], *, require_canonical_order: bool = True
) -> None:
    """Validate manifest structure, ownership uniqueness, and derived IDs.

    Ledger-to-manifest agreement intentionally belongs to the resolver layer.
    """
    if not isinstance(manifest, Mapping) or set(manifest) != {
        "schema_version",
        "sources",
    }:
        raise IdentityError("identity_manifest_invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise IdentityError("identity_manifest_invalid")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise IdentityError("identity_manifest_invalid")

    seen_source_ids: set[str] = set()
    seen_namespaces: set[str] = set()
    seen_record_ids: set[str] = set()
    seen_transaction_ids: set[str] = set()
    previous_source = ""
    for source in sources:
        _validate_source_ownership(
            source,
            seen_source_ids,
            seen_namespaces,
            seen_record_ids,
            seen_transaction_ids,
            require_canonical_order,
        )
        source_id_value = source["source_id"]
        if require_canonical_order and previous_source >= source_id_value:
            raise IdentityError("identity_manifest_invalid")
        previous_source = source_id_value


def _validate_source_ownership(
    source: Any,
    seen_source_ids: set[str],
    seen_namespaces: set[str],
    seen_record_ids: set[str],
    seen_transaction_ids: set[str],
    require_canonical_order: bool,
) -> None:
    required = {
        "source_id",
        "source_namespace_id",
        "source_revision",
        "extractor_contract_id",
        "records",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise IdentityError("identity_manifest_invalid")
    source_id_value = _manifest_text(source.get("source_id"), SOURCE_ID_RE)
    namespace = _manifest_text(
        source.get("source_namespace_id"), SOURCE_NAMESPACE_ID_RE
    )
    _manifest_text(source.get("source_revision"), SOURCE_REVISION_RE)
    _manifest_text(source.get("extractor_contract_id"), EXTRACTOR_CONTRACT_ID_RE)
    if source_id_value in seen_source_ids or namespace in seen_namespaces:
        raise IdentityError("identity_manifest_invalid")
    seen_source_ids.add(source_id_value)
    seen_namespaces.add(namespace)
    records = source.get("records")
    if not isinstance(records, list):
        raise IdentityError("identity_manifest_invalid")
    active_locators: set[AllocationLocator] = set()
    allocation_origins: set[tuple[str, str, str, AllocationLocator, int]] = set()
    previous_record = ""
    for record in records:
        record_id = _validate_ownership_record(
            source_id_value,
            record,
            active_locators,
            allocation_origins,
        )
        if (
            record_id in seen_record_ids
            or record["transaction_id"] in seen_transaction_ids
        ):
            raise IdentityError("identity_manifest_invalid")
        seen_record_ids.add(record_id)
        seen_transaction_ids.add(record["transaction_id"])
        if require_canonical_order and previous_record >= record_id:
            raise IdentityError("identity_manifest_invalid")
        previous_record = record_id


def _validate_ownership_record(
    source_id_value: str,
    record: Any,
    active_locators: set[AllocationLocator],
    allocation_origins: set[tuple[str, str, str, AllocationLocator, int]],
) -> str:
    required = {
        "source_record_id",
        "transaction_id",
        "transaction_id_kind",
        "record_fingerprint",
        "state",
        "current_locator",
        "allocation_origin",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise IdentityError("identity_manifest_invalid")
    record_id = _manifest_text(record.get("source_record_id"), SOURCE_RECORD_ID_RE)
    fingerprint = _manifest_text(
        record.get("record_fingerprint"), RECORD_FINGERPRINT_RE
    )
    origin = AllocationOrigin.from_manifest(record.get("allocation_origin"))
    if source_record_id(source_id_value, origin, fingerprint) != record_id:
        raise IdentityError("identity_manifest_invalid")

    kind = record.get("transaction_id_kind")
    identifier = record.get("transaction_id")
    if kind == "v2":
        _manifest_text(identifier, TRANSACTION_ID_V2_RE)
        if transaction_id(source_id_value, record_id) != identifier:
            raise IdentityError("identity_manifest_invalid")
    elif kind == "preserved_legacy":
        _manifest_text(identifier, TRANSACTION_ID_LEGACY_RE)
    else:
        raise IdentityError("identity_manifest_invalid")

    state = record.get("state")
    current_value = record.get("current_locator")
    if state == "active":
        locator = AllocationLocator.from_manifest(current_value)
        if locator in active_locators:
            raise IdentityError("identity_manifest_invalid")
        active_locators.add(locator)
    elif state == "retired":
        if current_value is not None:
            raise IdentityError("identity_manifest_invalid")
    else:
        raise IdentityError("identity_manifest_invalid")

    origin_key = (
        origin.source_revision,
        origin.extractor_contract_id,
        fingerprint,
        origin.locator,
        origin.occurrence_ordinal,
    )
    if origin_key in allocation_origins:
        raise IdentityError("identity_manifest_invalid")
    allocation_origins.add(origin_key)
    return record_id


def _sorted_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise IdentityError("identity_manifest_invalid")
    copied = copy.deepcopy(dict(manifest))
    sources = copied.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("records"), list):
                source["records"].sort(
                    key=lambda record: str(record.get("source_record_id", ""))
                )
        sources.sort(key=lambda source: str(source.get("source_id", "")))
    return copied


def _source_resolution_action(intent: str) -> str:
    if intent in {"import", "ordinary", "ordinary_import"}:
        return "import"
    if intent in {"replace", "reset"}:
        return intent
    raise IdentityError("identity_manifest_invalid")


def _resolution_manifest_sources(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    """Return just the manifest fields needed by the source table.

    Source candidates may deliberately contain duplicate namespaces here: the
    resolver reports that table row as a source ambiguity before a caller
    publishes a manifest. Full manifest validation remains the persistence
    boundary.
    """
    if not isinstance(manifest, Mapping) or set(manifest) != {
        "schema_version",
        "sources",
    }:
        raise IdentityError("identity_manifest_invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise IdentityError("identity_manifest_invalid")
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise IdentityError("identity_manifest_invalid")
    required = {
        "source_id",
        "source_namespace_id",
        "source_revision",
        "extractor_contract_id",
        "records",
    }
    resolved: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != required:
            raise IdentityError("identity_manifest_invalid")
        resolved.append(
            {
                "source_id": _manifest_text(source.get("source_id"), SOURCE_ID_RE),
                "source_namespace_id": _manifest_text(
                    source.get("source_namespace_id"), SOURCE_NAMESPACE_ID_RE
                ),
                "source_revision": _manifest_text(
                    source.get("source_revision"), SOURCE_REVISION_RE
                ),
                "extractor_contract_id": _manifest_text(
                    source.get("extractor_contract_id"), EXTRACTOR_CONTRACT_ID_RE
                ),
            }
        )
    return tuple(sorted(resolved, key=lambda source: source["source_id"]))


def _legacy_source_displays(
    legacy_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> frozenset[str]:
    displays: set[str] = set()
    for row in legacy_rows:
        if not isinstance(row, Mapping):
            raise IdentityError("identity_manifest_invalid")
        if not all(_text(row.get(field, "")) == "" for field in ID_FIELDS):
            continue
        display = row.get("source_file", "")
        if isinstance(display, str):
            displays.add(unicodedata.normalize("NFC", display))
    return frozenset(displays)


def _legacy_claim_conflicts(
    legacy_claims: Mapping[str, IncomingSourceIdentity], action: str
) -> list[SourceResolutionDiagnostic]:
    by_display: dict[str, list[IncomingSourceIdentity]] = {}
    for source in legacy_claims.values():
        by_display.setdefault(source.source_display, []).append(source)
    diagnostics: list[SourceResolutionDiagnostic] = []
    for claimants in by_display.values():
        if len(claimants) > 1:
            diagnostics.extend(
                _source_diagnostic(
                    "identity_legacy_source_ambiguous", source, action, len(claimants)
                )
                for source in claimants
            )
    return diagnostics


def _incoming_namespace_conflicts(
    incoming: tuple[IncomingSourceIdentity, ...], action: str
) -> list[SourceResolutionDiagnostic]:
    by_namespace: dict[str, list[IncomingSourceIdentity]] = {}
    for source in incoming:
        by_namespace.setdefault(source.namespace_id, []).append(source)
    diagnostics: list[SourceResolutionDiagnostic] = []
    for claimants in by_namespace.values():
        if len(claimants) > 1:
            diagnostics.extend(
                _source_diagnostic(
                    "identity_source_namespace_ambiguous",
                    source,
                    action,
                    len(claimants),
                )
                for source in claimants
            )
    return diagnostics


def _legacy_v2_claim_conflicts(
    legacy_claims: Mapping[str, IncomingSourceIdentity],
    by_namespace: Mapping[str, list[dict[str, str]]],
    action: str,
) -> list[SourceResolutionDiagnostic]:
    diagnostics: list[SourceResolutionDiagnostic] = []
    for source in legacy_claims.values():
        candidates = by_namespace.get(source.namespace_id, [])
        if candidates:
            diagnostics.append(
                _source_diagnostic(
                    "identity_source_namespace_ambiguous",
                    source,
                    action,
                    len(candidates) + 1,
                )
            )
    return diagnostics


def _source_diagnostic(
    code: str,
    source: IncomingSourceIdentity,
    action: str,
    candidate_count: int,
) -> SourceResolutionDiagnostic:
    if code in {
        "identity_source_namespace_ambiguous",
        "identity_source_revision_ambiguous",
        "identity_legacy_source_ambiguous",
    }:
        remediation = (
            "identity is ambiguous; retain the source and request explicit resolution."
        )
    elif code == "identity_source_target_not_found":
        remediation = "target source not found; run an ordinary import to create it."
    elif code == "identity_source_already_imported":
        remediation = "source already imported; use replace or reset."
    else:
        raise IdentityError("identity_manifest_invalid")
    return SourceResolutionDiagnostic(
        code=code,
        source_display=source.source_display,
        action=action,
        candidate_count=candidate_count,
        remediation=remediation,
    )


def _raise_first_resolution_error(
    diagnostics: list[SourceResolutionDiagnostic],
) -> None:
    diagnostic = min(diagnostics, key=lambda item: (item.code, item.source_display))
    raise IdentityError(diagnostic.code, diagnostic=diagnostic)


def _source_resolution_result(
    incoming: tuple[IncomingSourceIdentity, ...],
    assignments: Mapping[str, tuple[str, str]],
) -> SourceResolutionResult:
    resolved: list[ResolvedSourceIdentity] = []
    for source in incoming:
        source_id_value, disposition = assignments[source.stable_handle]
        resolved.append(
            ResolvedSourceIdentity(
                stable_handle=source.stable_handle,
                source_display=source.source_display,
                source_id=source_id_value,
                source_namespace_id=source.namespace_id,
                source_revision=source.revision,
                extractor_contract_id=source.contract_id,
                disposition=disposition,
            )
        )
    return SourceResolutionResult(tuple(resolved))


def _normalize_locator_text(path: Path) -> str:
    parts = path.parts
    if any("\x00" in part for part in parts):
        raise IdentityError("identity_manifest_invalid")
    normalized = [unicodedata.normalize("NFC", part) for part in parts]
    text = Path(*normalized).as_posix()
    if text.endswith("/"):
        text = text.rstrip("/")
    if not text:
        raise IdentityError("identity_manifest_invalid")
    return text


def _normalize_folded_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", _text(value)).split()).casefold()


def _normalize_iso_date(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise IdentityError("identity_manifest_invalid") from error


def _normalize_decimal(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        return ""
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise IdentityError("identity_manifest_invalid") from error
    if not number.is_finite():
        raise IdentityError("identity_manifest_invalid")
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _normalize_currency(value: Any) -> str:
    text = unicodedata.normalize("NFC", _text(value)).strip()
    return "".join(
        character.upper() if "a" <= character <= "z" else character
        for character in text
    )


def _normalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise IdentityError("identity_manifest_invalid")
        if isinstance(value, Decimal) and not value.is_finite():
            raise IdentityError("identity_manifest_invalid")
        return value
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise IdentityError("identity_manifest_invalid")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise IdentityError("identity_manifest_invalid")
            normalized[key] = _normalize_json_value(raw_value)
        return normalized
    raise IdentityError("identity_manifest_invalid")


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (float, Decimal)) and not isinstance(value, bool):
        return _canonical_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = list(value)
        if not all(isinstance(key, str) for key in keys):
            raise IdentityError("identity_manifest_invalid")
        ordered = sorted(keys, key=lambda key: key.encode("utf-16-be"))
        return (
            "{"
            + ",".join(
                _canonical_json(key) + ":" + _canonical_json(value[key])
                for key in ordered
            )
            + "}"
        )
    raise IdentityError("identity_manifest_invalid")


def _canonical_number(value: float | Decimal) -> str:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IdentityError("identity_manifest_invalid")
        if value == 0:
            return "0"
        absolute = abs(value)
        if 1e-6 <= absolute < 1e21:
            return _decimal_plain(Decimal(repr(value)))
        return _scientific_number(repr(value))
    if not value.is_finite():
        raise IdentityError("identity_manifest_invalid")
    if value == 0:
        return "0"
    return _decimal_plain(value)


def _decimal_plain(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _scientific_number(value: str) -> str:
    mantissa, exponent = value.lower().split("e")
    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    return f"{mantissa}e{int(exponent):+d}"


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise IdentityError("identity_manifest_invalid")
        output[key] = value
    return output


def _validate_locator(adapter_tag: Any, components: tuple[Any, ...]) -> None:
    if isinstance(adapter_tag, bool) or not isinstance(adapter_tag, int):
        raise IdentityError("identity_allocation_locator_invalid")
    expected = ADAPTER_COMPONENT_COUNTS.get(adapter_tag)
    if expected is None or len(components) != expected:
        raise IdentityError("identity_allocation_locator_invalid")
    for component in components:
        if (
            isinstance(component, bool)
            or not isinstance(component, int)
            or component < 1
            or component > MAX_UINT64
        ):
            raise IdentityError("identity_allocation_locator_invalid")


def _validate_ordinal(value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_UINT64
    ):
        raise IdentityError("identity_manifest_invalid")


def _manifest_text(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise IdentityError("identity_manifest_invalid")
    return value


def _require_match(pattern: re.Pattern[str], value: Any) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise IdentityError("identity_manifest_invalid")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


__all__ = [
    "ADAPTER_COMPONENT_COUNTS",
    "ADAPTER_VERSIONS",
    "AllocationLocator",
    "AllocationOrigin",
    "EXCLUDED_PROFILE_TOP_LEVEL_KEYS",
    "EXTRACTOR_CONTRACT_ID_RE",
    "IDENTITY_MANIFEST_NAME",
    "ID_FIELDS",
    "IdentityError",
    "IdentityResolution",
    "IncomingRecordIdentity",
    "MANIFEST_SCHEMA_VERSION",
    "RECORD_FINGERPRINT_RE",
    "RecordResolutionDiagnostic",
    "RecordResolutionResult",
    "ResolvedRecordIdentity",
    "ResolvedSourceIdentity",
    "SOURCE_ID_RE",
    "SOURCE_NAMESPACE_ID_RE",
    "SOURCE_RECORD_ID_RE",
    "SOURCE_REVISION_RE",
    "TRANSACTION_ID_LEGACY_RE",
    "TRANSACTION_ID_V2_RE",
    "allocation_locator_bytes",
    "canonical_profile_json",
    "ambiguous_legacy_transaction_ids",
    "digest",
    "empty_manifest",
    "exact_state_key",
    "extractor_contract_id",
    "has_stable_v2_identity",
    "logical_locator",
    "manifest_document",
    "manifest_path",
    "ownership_exact_state_key",
    "ownership_record",
    "parse_manifest",
    "record_fingerprint",
    "resolve_batch",
    "resolve_records",
    "source_id",
    "source_namespace_id",
    "source_ownership",
    "source_record_id",
    "source_revision",
    "transaction_id",
    "validate_manifest",
    "validate_ledger_manifest_agreement",
]
