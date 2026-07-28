"""Pure canonical-ledger planning for exact source-occurrence overlaps."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, cast

from honeymoney.identity import (
    IdentityError,
    has_stable_v2_identity,
    normalized_decimal,
    normalized_record_identity,
    record_fingerprint,
)
from honeymoney.overlap_contracts import (
    DuplicateGroupListing,
    DuplicateOccurrenceEvidence,
    LegacyOverlapManifest,
    OverlapDecision,
    OverlapDiagnostic,
    OverlapGroup,
    OverlapGroupDiagnostic,
    OverlapManifest,
    OverlapMembership,
    OverlapProvenanceStatus,
    OverlapSlot,
    OverlapWarning,
)
from honeymoney.reconciliation import derive_flow_type
from honeymoney.review_state import (
    REVIEW_REASON_ACCOUNTING_FLOW,
    REVIEW_REASON_CATEGORY,
    REVIEW_REASON_IDENTITY,
    set_review_reason,
    synchronize_review_state,
)
from honeymoney.schema import CATEGORIZED_COLUMNS
from honeymoney.valuation import VALUATION_SOURCE_MATCHED_EXCHANGE

OVERLAP_MANIFEST_SCHEMA_VERSION = 2
OVERLAP_MANIFEST_NAME = ".honeymoney-overlap-manifest.json"
SOURCE_OCCURRENCES_NAME = ".honeymoney-source-occurrences.csv"

SINGLE_SOURCE_STATUS: OverlapProvenanceStatus = "single_source"
EXACT_ONE_TO_ONE_STATUS: OverlapProvenanceStatus = "exact_one_to_one"
EQUAL_POOL_STATUS: OverlapProvenanceStatus = "pooled_equal_count"
AMBIGUOUS_COUNT_STATUS: OverlapProvenanceStatus = "ambiguous_count_mismatch"
PROVENANCE_STATUSES: set[OverlapProvenanceStatus] = {
    SINGLE_SOURCE_STATUS,
    EXACT_ONE_TO_ONE_STATUS,
    EQUAL_POOL_STATUS,
    AMBIGUOUS_COUNT_STATUS,
}

OVERLAP_AMBIGUITY_FLAG = "overlap_count_ambiguous"
OVERLAP_PRIOR_REVIEW_FLAG = "overlap_count_prior_review"
OVERLAP_AMBIGUITY_REASON = (
    "Exact overlap has different source occurrence counts; provenance remains pooled"
)
OVERLAP_HISTORY_FLAG = "overlap_history_ambiguous"
OVERLAP_HISTORY_REASON = (
    "Repeated exact occurrences have conflicting review history; no assignment was made"
)

_NAMESPACE_RE = re.compile(r"^ovns_[0-9a-f]{64}$")
_GROUP_RE = re.compile(r"^ovg_[0-9a-f]{64}$")
_REVIEW_GROUP_RE = re.compile(r"^ovr_[0-9a-f]{64}$")
_MEMBERSHIP_RE = re.compile(r"^ovm_[0-9a-f]{64}$")
_TRANSACTION_RE = re.compile(r"^txn_[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^fp_[0-9a-f]{64}$")

_SOURCE_ID_FIELDS = (
    "source_id",
    "source_namespace_id",
    "source_revision",
    "source_record_id",
)
_SOURCE_DISPLAY_FIELDS = ("source_file", "source_page", "source_row")
_STATEMENT_BALANCE_FIELDS = (
    "statement_opening_balance",
    "statement_closing_balance",
    "statement_section",
)
_DUPLICATE_FLAGS = {"duplicate_suspected", "duplicate_review_promoted"}
_FINANCIAL_FIELDS = (
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "account_type",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "merchant",
    "original_description",
)
_IMMUTABLE_SOURCE_FIELDS = (
    "transaction_id",
    *_FINANCIAL_FIELDS,
    *_SOURCE_ID_FIELDS,
    *_SOURCE_DISPLAY_FIELDS,
    *_STATEMENT_BALANCE_FIELDS,
)
_DISPLAY_FIELDS = ("account", "account_type", "institution", "country")
_MUTABLE_FIELDS = (
    "category",
    "flow_type",
    "flow_source",
    "transfer_group_id",
    "paired_transaction_id",
    "reconciliation_status",
    "reconciliation_confidence",
    "owner",
    "payment_method",
    "confidence",
    "needs_review",
    "review_reasons",
    "reason",
    "flags",
    "notes",
)


@dataclass(frozen=True)
class CanonicalizationResult:
    """Complete canonical ledger and overlap-manifest result."""

    rows: list[dict[str, str]]
    manifest: OverlapManifest
    diagnostic: OverlapDiagnostic
    source_occurrence_count: int
    canonical_occurrence_count: int
    consolidated_occurrence_count: int
    ambiguous_group_count: int


@dataclass(frozen=True)
class CorrectionProjection:
    """Canonical correction bindings plus rows that cannot be assigned."""

    corrections: dict[str, dict[str, str]]
    ambiguous_transaction_ids: tuple[str, ...]


@dataclass(frozen=True)
class MigrationCorrectionProjection:
    """Final canonical corrections after a proven source-record rekey."""

    corrections: dict[str, dict[str, str]]
    ambiguous_transaction_ids: tuple[str, ...]
    removed_transaction_ids: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateResolution:
    """One validated duplicate decision and its canonical result."""

    result: CanonicalizationResult
    removed_correction_ids: tuple[str, ...]
    correction_updates: dict[str, dict[str, str]]
    idempotent: bool
    old_group_canonical_count: int
    new_group_canonical_count: int
    remaining_unresolved_count: int


class DuplicateResolutionError(ValueError):
    """Stable duplicate-resolution failure safe for CLI output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def empty_overlap_manifest(namespace_key: str) -> OverlapManifest:
    """Return an empty manifest for one persisted workspace namespace."""
    if _NAMESPACE_RE.fullmatch(namespace_key) is None:
        raise ValueError("overlap_manifest_invalid")
    return {
        "schema_version": OVERLAP_MANIFEST_SCHEMA_VERSION,
        "namespace_key": namespace_key,
        "groups": [],
    }


def source_occurrences_path(categorized_path: Path) -> Path:
    return Path(categorized_path).parent / SOURCE_OCCURRENCES_NAME


def overlap_manifest_path(categorized_path: Path) -> Path:
    return Path(categorized_path).parent / OVERLAP_MANIFEST_NAME


def overlap_manifest_document(manifest: Mapping[str, object]) -> str:
    copied = _validated_manifest_copy(manifest)
    return (
        json.dumps(copied, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )


def parse_overlap_manifest(document: str) -> OverlapManifest | LegacyOverlapManifest:
    try:
        value = json.loads(document)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("overlap_manifest_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("overlap_manifest_invalid")
    if value.get("schema_version") == 1:
        _validate_v1_manifest(value)
        canonical = (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
    else:
        _validate_manifest(value)
        canonical = overlap_manifest_document(value)
    if canonical != document:
        raise ValueError("overlap_manifest_invalid")
    return cast(OverlapManifest | LegacyOverlapManifest, value)


def validate_overlap_agreement(
    canonical_rows: Sequence[Mapping[str, object]],
    source_occurrences: Sequence[Mapping[str, object]],
    manifest: Mapping[str, object],
) -> None:
    """Require canonical rows to equal the manifest's active multiset slots."""
    expected = canonicalize_overlaps(source_occurrences, canonical_rows, manifest)
    if expected.manifest != manifest:
        raise ValueError("overlap_manifest_invalid")
    expected_by_id = {row["transaction_id"]: row for row in expected.rows}
    actual_by_id = {str(row.get("transaction_id", "")): row for row in canonical_rows}
    if len(actual_by_id) != len(canonical_rows) or set(actual_by_id) != set(
        expected_by_id
    ):
        raise ValueError("overlap_manifest_invalid")
    fields = (
        "canonical_group_id",
        "canonical_slot",
        "provenance_status",
        "source_occurrence_count",
    )
    for identifier, actual in actual_by_id.items():
        expected_row = expected_by_id[identifier]
        if set(actual) != set(CATEGORIZED_COLUMNS):
            raise ValueError("overlap_manifest_invalid")
        if not str(actual.get("canonical_group_id", "")):
            if any(str(actual.get(field, "")) for field in fields):
                raise ValueError("overlap_manifest_invalid")
            if any(
                str(actual.get(field, "")) != expected_row[field]
                for field in _IMMUTABLE_SOURCE_FIELDS
            ):
                raise ValueError("overlap_manifest_invalid")
            continue
        if any(str(actual.get(field, "")) != expected_row[field] for field in fields):
            raise ValueError("overlap_manifest_invalid")
        if any(
            str(actual.get(field, "")) != expected_row[field]
            for field in _IMMUTABLE_SOURCE_FIELDS
            if field != "amount_hkd"
        ):
            raise ValueError("overlap_manifest_invalid")
        if any(
            str(actual.get(field, ""))
            for field in (
                *_SOURCE_ID_FIELDS,
                *_SOURCE_DISPLAY_FIELDS,
                *_STATEMENT_BALANCE_FIELDS,
            )
        ):
            raise ValueError("overlap_manifest_invalid")
        group_fingerprint = next(
            group["record_fingerprint"]
            for group in manifest["groups"]
            if group["overlap_group_id"] == actual["canonical_group_id"]
        )
        if record_fingerprint(actual) != group_fingerprint:
            raise ValueError("overlap_manifest_invalid")
        _validate_canonical_amount_hkd(actual, source_occurrences, group_fingerprint)


def project_corrections(
    result: CanonicalizationResult,
    corrections: Mapping[str, Mapping[str, str]],
) -> CorrectionProjection:
    """Project source aliases only when the canonical target is proven."""
    projected = {
        str(identifier): {str(field): str(value) for field, value in patch.items()}
        for identifier, patch in corrections.items()
        if any(row["transaction_id"] == str(identifier) for row in result.rows)
    }
    ambiguous: set[str] = set()
    for group in result.diagnostic["groups"]:
        canonical_ids = [str(item) for item in group["canonical_transaction_ids"]]
        unprojected_ids = [
            identifier for identifier in canonical_ids if identifier not in projected
        ]
        if not unprojected_ids:
            continue
        occurrence_ids = [
            str(item) for pool in group["source_occurrence_pools"] for item in pool
        ]
        aliases = [
            {str(field): str(value) for field, value in corrections[identifier].items()}
            for identifier in occurrence_ids
            if identifier in corrections
        ]
        if not aliases:
            continue
        unique = {tuple(sorted(patch.items())): patch for patch in aliases}
        if len(canonical_ids) == 1 and len(unique) == 1:
            projected[unprojected_ids[0]] = next(iter(unique.values()))
            continue
        if len(unique) == 1 and len(aliases) == len(occurrence_ids):
            patch = next(iter(unique.values()))
            for identifier in unprojected_ids:
                projected[identifier] = dict(patch)
            continue
        ambiguous.update(unprojected_ids)
    return CorrectionProjection(projected, tuple(sorted(ambiguous)))


def project_migration_corrections(
    result: CanonicalizationResult,
    prior_source_rows: Sequence[Mapping[str, object]],
    next_source_rows: Sequence[Mapping[str, object]],
    source_corrections: Mapping[str, Mapping[str, str]],
    current_corrections: Mapping[str, Mapping[str, str]],
) -> MigrationCorrectionProjection:
    """Carry review history across a unique parser-driven source rekey."""
    prior_by_key = _migration_rows_by_key(prior_source_rows)
    next_by_key = _migration_rows_by_key(next_source_rows)
    next_aliases: dict[str, dict[str, str]] = {}
    ambiguous_occurrence_ids: set[str] = set()

    for key in sorted(set(prior_by_key) & set(next_by_key)):
        prior_rows = prior_by_key[key]
        next_rows = next_by_key[key]
        patches = [
            {
                str(field): str(value)
                for field, value in source_corrections[identifier].items()
            }
            for row in prior_rows
            if (identifier := str(row.get("transaction_id", ""))) in source_corrections
        ]
        if not patches:
            continue
        unique_patches = {tuple(sorted(patch.items())): patch for patch in patches}
        if (
            len(prior_rows) == len(next_rows)
            and len(patches) == len(prior_rows)
            and len(unique_patches) == 1
        ):
            patch = next(iter(unique_patches.values()))
            for row in next_rows:
                next_aliases[str(row.get("transaction_id", ""))] = dict(patch)
            continue
        ambiguous_occurrence_ids.update(
            str(row.get("transaction_id", "")) for row in next_rows
        )

    projected = project_corrections(
        result,
        {**current_corrections, **next_aliases},
    )
    ambiguous_canonical_ids = set(projected.ambiguous_transaction_ids)
    for group in result.diagnostic["groups"]:
        occurrence_ids = {
            str(identifier)
            for pool in group["source_occurrence_pools"]
            for identifier in pool
        }
        if occurrence_ids & ambiguous_occurrence_ids:
            ambiguous_canonical_ids.update(
                str(identifier) for identifier in group["canonical_transaction_ids"]
            )
    prior_ids = {
        str(row.get("transaction_id", ""))
        for row in prior_source_rows
        if row.get("transaction_id")
    }
    return MigrationCorrectionProjection(
        projected.corrections,
        tuple(sorted(ambiguous_canonical_ids)),
        tuple(sorted(prior_ids & set(source_corrections))),
    )


def project_replacement_corrections(
    prior_result: CanonicalizationResult,
    next_result: CanonicalizationResult,
    prior_source_rows: Sequence[Mapping[str, object]],
    next_source_rows: Sequence[Mapping[str, object]],
    corrections: Mapping[str, Mapping[str, str]],
    replaced_source_ids: set[str],
) -> MigrationCorrectionProjection:
    """Carry canonical review history across a parser-driven source rekey."""
    prior_by_id = {
        str(row.get("transaction_id", "")): row
        for row in prior_source_rows
        if row.get("transaction_id")
    }
    targeted_prior_keys = {
        _migration_row_key(row)
        for row in prior_source_rows
        if str(row.get("source_id", "")) in replaced_source_ids
        and has_stable_v2_identity(row)
    }
    targeted_next_rows = [
        row
        for row in next_source_rows
        if str(row.get("source_id", "")) in replaced_source_ids
        and has_stable_v2_identity(row)
    ]
    next_by_key: dict[tuple[str, ...], list[Mapping[str, object]]] = {}
    for row in targeted_next_rows:
        next_by_key.setdefault(_migration_row_key(row), []).append(row)

    safe_by_key: dict[tuple[str, ...], dict[str, str]] = {}
    ambiguous_keys: set[tuple[str, ...]] = set()
    removed_ids: set[str] = set()
    next_canonical_ids = {row["transaction_id"] for row in next_result.rows}

    for group in prior_result.diagnostic["groups"]:
        canonical_ids = [
            str(identifier) for identifier in group["canonical_transaction_ids"]
        ]
        patches = [
            {str(field): str(value) for field, value in corrections[identifier].items()}
            for identifier in canonical_ids
            if identifier in corrections
        ]
        if not patches:
            continue
        occurrence_ids = {
            str(identifier)
            for pool in group["source_occurrence_pools"]
            for identifier in pool
        }
        keys: set[tuple[str, ...]] = set()
        for identifier in occurrence_ids:
            prior_row = prior_by_id.get(identifier)
            if prior_row is None:
                continue
            key = _migration_row_key(prior_row)
            if (
                str(prior_row.get("source_id", "")) in replaced_source_ids
                and key in targeted_prior_keys
            ):
                keys.add(key)
        if not keys:
            continue
        removed_ids.update(
            identifier
            for identifier in canonical_ids
            if identifier in corrections and identifier not in next_canonical_ids
        )
        unique = {tuple(sorted(patch.items())): patch for patch in patches}
        fully_agreed = len(patches) == len(canonical_ids) and len(unique) == 1
        if not fully_agreed:
            ambiguous_keys.update(keys)
            continue
        agreed_patch = next(iter(unique.values()))
        for key in keys:
            existing = safe_by_key.get(key)
            if existing is not None and existing != agreed_patch:
                ambiguous_keys.add(key)
                safe_by_key.pop(key, None)
            elif key not in ambiguous_keys:
                safe_by_key[key] = dict(agreed_patch)

    aliases: dict[str, dict[str, str]] = {}
    ambiguous_occurrence_ids: set[str] = set()
    prior_counts: dict[tuple[str, ...], int] = {}
    for row in prior_source_rows:
        if str(
            row.get("source_id", "")
        ) in replaced_source_ids and has_stable_v2_identity(row):
            key = _migration_row_key(row)
            prior_counts[key] = prior_counts.get(key, 0) + 1
    for key, rows in next_by_key.items():
        if key in ambiguous_keys or (
            key in safe_by_key and prior_counts.get(key, 0) != len(rows)
        ):
            ambiguous_occurrence_ids.update(
                str(row.get("transaction_id", "")) for row in rows
            )
            continue
        candidate_patch = safe_by_key.get(key)
        if candidate_patch is None:
            continue
        for row in rows:
            aliases[str(row.get("transaction_id", ""))] = dict(candidate_patch)

    projected = project_corrections(next_result, {**corrections, **aliases})
    ambiguous_canonical_ids = set(projected.ambiguous_transaction_ids)
    for group in next_result.diagnostic["groups"]:
        occurrence_ids = {
            str(identifier)
            for pool in group["source_occurrence_pools"]
            for identifier in pool
        }
        if occurrence_ids & ambiguous_occurrence_ids:
            ambiguous_canonical_ids.update(
                str(identifier) for identifier in group["canonical_transaction_ids"]
            )
    return MigrationCorrectionProjection(
        projected.corrections,
        tuple(sorted(ambiguous_canonical_ids)),
        tuple(sorted(removed_ids)),
    )


def _migration_rows_by_key(
    rows: Sequence[Mapping[str, object]],
) -> dict[tuple[str, ...], list[Mapping[str, object]]]:
    grouped: dict[tuple[str, ...], list[Mapping[str, object]]] = {}
    for row in rows:
        if not has_stable_v2_identity(row):
            continue
        key = _migration_row_key(row)
        grouped.setdefault(key, []).append(row)
    return grouped


def _migration_row_key(row: Mapping[str, object]) -> tuple[str, ...]:
    identity = normalized_record_identity(row)
    return (
        str(row.get("source_id", "")),
        identity["date"],
        identity["transaction_date"],
        identity["posting_date"],
        identity["original_amount"],
        identity["posted_amount"],
        identity["merchant"],
        identity["original_description"],
    )


def apply_history_ambiguity(
    rows: list[dict[str, str]], transaction_ids: set[str] | tuple[str, ...]
) -> None:
    """Force pooled correction history to review without choosing a slot."""
    selected = set(transaction_ids)
    for row in rows:
        if row.get("transaction_id") not in selected:
            continue
        flags = [item for item in row.get("flags", "").split(";") if item]
        if OVERLAP_HISTORY_FLAG not in flags:
            flags.append(OVERLAP_HISTORY_FLAG)
        row["flags"] = ";".join(flags)
        row["reason"] = _append_reason(row.get("reason", ""), OVERLAP_HISTORY_REASON)
        set_review_reason(row, REVIEW_REASON_IDENTITY, True)


def clear_history_ambiguity(
    rows: list[dict[str, str]], transaction_ids: set[str] | tuple[str, ...]
) -> None:
    """Clear protected history review after an explicit canonical correction."""
    selected = set(transaction_ids)
    for row in rows:
        if row.get("transaction_id") not in selected:
            continue
        row["flags"] = ";".join(
            item
            for item in row.get("flags", "").split(";")
            if item and item != OVERLAP_HISTORY_FLAG
        )
        row["reason"] = _remove_reason(row.get("reason", ""), OVERLAP_HISTORY_REASON)
        set_review_reason(row, REVIEW_REASON_IDENTITY, False)


def enforce_overlap_review(
    rows: list[dict[str, str]],
    result: CanonicalizationResult | None = None,
) -> None:
    """Reapply protected count/history review after mutable processing."""
    resolved_group_ids = {
        diagnostic["canonical_group_id"]
        for diagnostic in (result.diagnostic["groups"] if result is not None else ())
        if diagnostic["decision"] is not None
    }
    for row in rows:
        if not row.get("canonical_group_id"):
            _clear_duplicate_state(row)
        _apply_overlap_review(
            row,
            row.get("provenance_status", ""),
            resolved=row.get("canonical_group_id") in resolved_group_ids,
        )
        if OVERLAP_HISTORY_FLAG in row.get("flags", "").split(";"):
            set_review_reason(row, REVIEW_REASON_IDENTITY, True)


def release_overlap_review_ownership(rows: list[dict[str, str]]) -> None:
    """Restore review state from before overlap review for categorization."""
    for row in rows:
        if OVERLAP_AMBIGUITY_FLAG in row.get("flags", "").split(";"):
            _apply_overlap_review(
                row,
                row.get("provenance_status", ""),
                resolved=True,
            )


def list_duplicate_groups(
    result: CanonicalizationResult,
    source_occurrences: Sequence[Mapping[str, object]],
) -> list[DuplicateGroupListing]:
    """Return bounded local evidence for unresolved count mismatches."""
    by_id = {
        str(row.get("transaction_id", "")): row
        for row in source_occurrences
        if row.get("transaction_id")
    }
    groups: list[DuplicateGroupListing] = []
    for diagnostic in result.diagnostic["groups"]:
        if (
            diagnostic["provenance_status"] != AMBIGUOUS_COUNT_STATUS
            or diagnostic["decision"] is not None
        ):
            continue
        occurrence_ids = sorted(
            str(identifier)
            for pool in diagnostic["source_occurrence_pools"]
            for identifier in pool
        )
        occurrences: list[DuplicateOccurrenceEvidence] = []
        for identifier in occurrence_ids:
            row = by_id[identifier]
            occurrences.append(
                {
                    "occurrence_id": identifier,
                    "account_id": _bounded(row.get("account_id", "")),
                    "account": _bounded(row.get("account", "")),
                    "institution": _bounded(row.get("institution", "")),
                    "date": _bounded(
                        row.get("date")
                        or row.get("transaction_date")
                        or row.get("posting_date")
                        or ""
                    ),
                    "merchant": _bounded(row.get("merchant", "")),
                    "amount": _bounded(
                        row.get("posted_amount") or row.get("amount_hkd", "")
                    ),
                    "currency": _bounded(
                        row.get("posted_currency")
                        or ("HKD" if row.get("amount_hkd") else "")
                    ),
                    "source_display": _safe_source_display(row.get("source_file", "")),
                    "source_page": _bounded(row.get("source_page", "")),
                    "source_row": _bounded(row.get("source_row", "")),
                }
            )
        groups.append(
            {
                "group_id": diagnostic["review_group_id"],
                "match_basis": "exact_normalized_financial_identity",
                "source_counts": list(diagnostic["source_counts"]),
                "keep_all_count": diagnostic["keep_all_count"],
                "same_event_count": diagnostic["same_event_count"],
                "canonical_occurrence_ids": sorted(
                    diagnostic["canonical_transaction_ids"]
                ),
                "occurrences": occurrences,
            }
        )
    return groups


def resolve_duplicate_group(
    source_occurrences: Sequence[Mapping[str, object]],
    prior_canonical_rows: Sequence[Mapping[str, object]],
    prior_manifest: Mapping[str, object],
    review_group_id: str,
    choice: str,
    corrections: Mapping[str, Mapping[str, str]],
) -> DuplicateResolution:
    """Apply one decision to the exact current membership or fail closed."""
    if choice not in {"same-event", "keep-all"}:
        raise DuplicateResolutionError("duplicate_choice_invalid")
    decision = cast(OverlapDecision, choice)
    current = canonicalize_overlaps(
        source_occurrences, prior_canonical_rows, prior_manifest
    )
    diagnostic = next(
        (
            item
            for item in current.diagnostic["groups"]
            if item["review_group_id"] == review_group_id
            and item["provenance_status"] == AMBIGUOUS_COUNT_STATUS
        ),
        None,
    )
    if diagnostic is None:
        if any(
            membership["group_id"] == review_group_id
            for group in current.manifest["groups"]
            for membership in group["memberships"]
        ):
            raise DuplicateResolutionError("duplicate_group_stale")
        raise DuplicateResolutionError("duplicate_group_unknown")
    existing_resolution = diagnostic["decision"]
    if existing_resolution is not None:
        if existing_resolution != decision:
            raise DuplicateResolutionError("duplicate_resolution_conflict")
        count = len(diagnostic["canonical_transaction_ids"])
        return DuplicateResolution(
            current,
            (),
            {},
            True,
            count,
            count,
            current.ambiguous_group_count,
        )

    removed_correction_ids: tuple[str, ...] = ()
    correction_updates: dict[str, dict[str, str]] = {}
    if decision == "same-event":
        removed_correction_ids, correction_updates = _safe_tail_corrections(
            current, diagnostic, corrections
        )

    manifest = copy.deepcopy(current.manifest)
    group = next(
        item
        for item in manifest["groups"]
        if item["overlap_group_id"] == diagnostic["canonical_group_id"]
    )
    membership = next(
        item for item in group["memberships"] if item["group_id"] == review_group_id
    )
    membership["resolution"] = decision
    resolved = canonicalize_overlaps(source_occurrences, current.rows, manifest)
    for row in resolved.rows:
        correction = corrections.get(row["transaction_id"])
        if correction is not None and "needs_review" in correction:
            row["needs_review"] = str(correction["needs_review"]).casefold()
    return DuplicateResolution(
        resolved,
        removed_correction_ids,
        correction_updates,
        False,
        len(diagnostic["canonical_transaction_ids"]),
        sum(
            row.get("canonical_group_id") == diagnostic["canonical_group_id"]
            for row in resolved.rows
        ),
        resolved.ambiguous_group_count,
    )


def canonicalize_overlaps(
    source_occurrences: Sequence[Mapping[str, object]],
    prior_canonical_rows: Sequence[Mapping[str, object]],
    prior_manifest: Mapping[str, object],
) -> CanonicalizationResult:
    """Return the canonical multiset without assigning repeated source rows."""
    occurrences = tuple(source_occurrences)
    prior_rows = tuple(prior_canonical_rows)
    manifest = _validated_manifest_copy(prior_manifest)
    namespace_key = manifest["namespace_key"]

    buckets: dict[str, list[Mapping[str, object]]] = {}
    legacy_rows: list[dict[str, str]] = []
    for occurrence in occurrences:
        if not has_stable_v2_identity(occurrence):
            legacy_rows.append(
                {
                    column: str(occurrence.get(column, ""))
                    for column in CATEGORIZED_COLUMNS
                }
            )
            continue
        fingerprint = record_fingerprint(occurrence)
        buckets.setdefault(fingerprint, []).append(occurrence)

    prior_groups = {
        str(group["record_fingerprint"]): group for group in manifest["groups"]
    }
    fingerprints = sorted(set(prior_groups) | set(buckets))
    prior_by_id = {
        str(row.get("transaction_id", "")): row
        for row in prior_rows
        if row.get("transaction_id")
    }

    next_groups: list[OverlapGroup] = []
    canonical_rows: list[dict[str, str]] = []
    diagnostics: list[OverlapGroupDiagnostic] = []
    ambiguous_groups = 0
    changed_review_group_ids: set[str] = set()
    source_transaction_ids = {
        str(row.get("transaction_id", ""))
        for row in occurrences
        if row.get("transaction_id")
    }

    manifest_transaction_ids = {
        str(slot["transaction_id"])
        for group in manifest["groups"]
        for slot in group["slots"]
    }
    if manifest_transaction_ids & source_transaction_ids:
        raise ValueError("overlap_identity_hash_conflict")

    for fingerprint in fingerprints:
        bucket = buckets.get(fingerprint, [])
        source_pools = _source_pools(bucket)
        source_counts = sorted(len(pool) for pool in source_pools)
        keep_all_count = max(source_counts, default=0)
        same_event_count = (
            source_counts[-2] if len(source_counts) >= 2 else keep_all_count
        )
        prior_group = prior_groups.get(fingerprint)
        group_id = (
            str(prior_group["overlap_group_id"])
            if prior_group is not None
            else _group_id(namespace_key, fingerprint)
        )
        prior_slots = {
            int(slot["slot"]): slot
            for slot in (prior_group["slots"] if prior_group is not None else [])
        }
        slot_count = max(keep_all_count, max(prior_slots, default=0))
        slot_identifiers = [
            (
                str(prior_slots[slot_number]["transaction_id"])
                if slot_number in prior_slots
                else _canonical_transaction_id(namespace_key, group_id, slot_number)
            )
            for slot_number in range(1, slot_count + 1)
        ]
        membership_digest = _membership_digest(
            namespace_key,
            bucket,
            source_pools,
            slot_identifiers[:keep_all_count],
            slot_identifiers[same_event_count:keep_all_count],
        )
        review_group_id = _review_group_id(namespace_key, group_id, membership_digest)
        memberships: list[OverlapMembership] = copy.deepcopy(
            prior_group["memberships"] if prior_group is not None else []
        )
        status = _provenance_status(source_counts)
        matching_membership = next(
            (
                membership
                for membership in memberships
                if membership["membership_digest"] == membership_digest
            ),
            None,
        )
        if status == AMBIGUOUS_COUNT_STATUS:
            if (
                matching_membership is None
                or matching_membership["resolution"] == "unresolved"
            ) and (
                _prior_group_has_resolved_review(prior_slots, prior_by_id)
                or (
                    matching_membership is None
                    and _prior_group_has_resolved_membership(prior_group)
                )
            ):
                changed_review_group_ids.add(review_group_id)
            if matching_membership is None:
                matching_membership = {
                    "overlap_group_id": group_id,
                    "group_id": review_group_id,
                    "membership_digest": membership_digest,
                    "resolution": "unresolved",
                }
                memberships.append(matching_membership)
                memberships.sort(key=lambda item: item["membership_digest"])
        decision_choice: OverlapDecision | None = None
        if (
            matching_membership is not None
            and matching_membership["resolution"] != "unresolved"
        ):
            decision_choice = matching_membership["resolution"]
        active_count = (
            same_event_count if decision_choice == "same-event" else keep_all_count
        )
        slots: list[OverlapSlot] = []
        for slot_number, identifier in enumerate(slot_identifiers, start=1):
            slots.append(
                {
                    "slot": slot_number,
                    "transaction_id": identifier,
                    "state": "active" if slot_number <= active_count else "retired",
                }
            )
        next_groups.append(
            {
                "overlap_group_id": group_id,
                "record_fingerprint": fingerprint,
                "memberships": memberships,
                "slots": slots,
            }
        )
        if not bucket:
            continue

        if status == AMBIGUOUS_COUNT_STATUS and decision_choice is None:
            ambiguous_groups += 1
        group_diagnostic: OverlapGroupDiagnostic = {
            "group_id": group_id,
            "canonical_group_id": group_id,
            "review_group_id": review_group_id,
            "canonical_transaction_ids": [
                slot["transaction_id"] for slot in slots if slot["state"] == "active"
            ],
            "provenance_status": status,
            "decision": decision_choice,
            "source_counts": source_counts,
            "keep_all_count": keep_all_count,
            "same_event_count": same_event_count,
            "slot_support_counts": [
                sum(count >= slot for count in source_counts)
                for slot in range(1, active_count + 1)
            ],
            "source_occurrence_pools": [
                sorted(str(row["transaction_id"]) for row in pool)
                for pool in source_pools
            ],
        }
        diagnostics.append(group_diagnostic)

        agreed_template, history_ambiguous = _agreed_canonical_template(bucket)
        for slot in slots[:active_count]:
            identifier = str(slot["transaction_id"])
            if identifier in source_transaction_ids:
                raise ValueError("overlap_identity_hash_conflict")
            prior = prior_by_id.get(identifier)
            canonical_row: dict[str, str] = copy.deepcopy(agreed_template)
            if prior is not None:
                for field in _MUTABLE_FIELDS:
                    canonical_row[field] = str(prior.get(field, ""))
            canonical_row.update(
                {
                    "transaction_id": identifier,
                    "canonical_group_id": group_id,
                    "canonical_slot": str(slot["slot"]),
                    "provenance_status": status,
                    "source_occurrence_count": str(len(bucket)),
                }
            )
            _clear_source_provenance(canonical_row)
            _clear_duplicate_state(canonical_row)
            _apply_overlap_review(
                canonical_row, status, resolved=decision_choice is not None
            )
            if history_ambiguous and prior is None:
                apply_history_ambiguity([canonical_row], {identifier})
            canonical_rows.append(canonical_row)

    next_groups.sort(key=lambda group: group["overlap_group_id"])
    canonical_rows.sort(
        key=lambda row: (
            row.get("date", ""),
            row.get("transaction_date", ""),
            row.get("posting_date", ""),
            row.get("account_id", ""),
            row.get("merchant", ""),
            row.get("original_description", ""),
            row.get("original_amount", ""),
            row.get("posted_amount", ""),
            row.get("canonical_group_id", ""),
            int(row.get("canonical_slot", "0")),
        )
    )
    canonical_rows.extend(legacy_rows)
    diagnostics.sort(key=lambda item: item["group_id"])
    next_manifest: OverlapManifest = {
        "schema_version": OVERLAP_MANIFEST_SCHEMA_VERSION,
        "namespace_key": namespace_key,
        "groups": next_groups,
    }
    _validate_manifest(next_manifest)
    warnings: list[OverlapWarning] = [
        {
            "code": "duplicate_membership_changed",
            "group_id": review_group_id,
        }
        for review_group_id in sorted(changed_review_group_ids)
    ]
    overlap_diagnostic: OverlapDiagnostic = {
        "group_count": len(diagnostics),
        "ambiguous_group_count": ambiguous_groups,
        "warnings": warnings,
        "source_occurrence_count": len(occurrences),
        "canonical_occurrence_count": len(canonical_rows),
        "consolidated_occurrence_count": len(occurrences) - len(canonical_rows),
        "provenance_counts": {
            status: sum(
                row.get("provenance_status") == status for row in canonical_rows
            )
            for status in sorted(PROVENANCE_STATUSES)
        },
        "groups": diagnostics,
    }
    return CanonicalizationResult(
        rows=canonical_rows,
        manifest=next_manifest,
        diagnostic=overlap_diagnostic,
        source_occurrence_count=len(occurrences),
        canonical_occurrence_count=len(canonical_rows),
        consolidated_occurrence_count=len(occurrences) - len(canonical_rows),
        ambiguous_group_count=ambiguous_groups,
    )


def _source_pools(
    bucket: list[Mapping[str, object]],
) -> list[list[Mapping[str, object]]]:
    by_source: dict[str, list[Mapping[str, object]]] = {}
    for row in bucket:
        by_source.setdefault(str(row["source_id"]), []).append(row)
    pools = [
        sorted(rows, key=lambda row: str(row["transaction_id"]))
        for rows in by_source.values()
    ]
    pools.sort(key=lambda pool: str(pool[0]["transaction_id"]))
    return pools


def _prior_group_has_resolved_review(
    prior_slots: Mapping[int, Mapping[str, object]],
    prior_rows: Mapping[str, Mapping[str, object]],
) -> bool:
    for slot in prior_slots.values():
        if slot.get("state") != "active":
            continue
        row = prior_rows.get(str(slot.get("transaction_id", "")))
        if row is None or row.get("provenance_status") != AMBIGUOUS_COUNT_STATUS:
            continue
        flags = set(filter(None, str(row.get("flags", "")).split(";")))
        if OVERLAP_AMBIGUITY_FLAG not in flags:
            return True
    return False


def _prior_group_has_resolved_membership(
    prior_group: OverlapGroup | None,
) -> bool:
    if prior_group is None:
        return False
    return any(
        membership.get("resolution") in {"same-event", "keep-all"}
        for membership in prior_group["memberships"]
    )


def _provenance_status(source_counts: list[int]) -> OverlapProvenanceStatus:
    if len(source_counts) == 1:
        return SINGLE_SOURCE_STATUS
    if source_counts == [1] * len(source_counts):
        return EXACT_ONE_TO_ONE_STATUS
    if len(set(source_counts)) == 1:
        return EQUAL_POOL_STATUS
    return AMBIGUOUS_COUNT_STATUS


def _agreed_canonical_template(
    bucket: list[Mapping[str, object]],
) -> tuple[dict[str, str], bool]:
    row = {column: "" for column in CATEGORIZED_COLUMNS}
    identities = [normalized_record_identity(item) for item in bucket]
    if len({tuple(identity.items()) for identity in identities}) != 1:
        raise ValueError("overlap_immutable_conflict")
    for field, normalized_value in identities[0].items():
        row[field] = _agreed_value(bucket, field, normalized_value)
    try:
        amount_hkd_values = {
            normalized_decimal(item.get("amount_hkd", "")) for item in bucket
        }
    except IdentityError as error:
        raise ValueError("overlap_immutable_conflict") from error
    if len(amount_hkd_values) != 1:
        raise ValueError("overlap_immutable_conflict")
    row["amount_hkd"] = _agreed_value(
        bucket, "amount_hkd", next(iter(amount_hkd_values))
    )
    row["valuation_source"] = _agreed_value(bucket, "valuation_source", "")
    row["valuation_status"] = _agreed_value(bucket, "valuation_status", "")
    row["valuation_rate_date"] = _agreed_value(bucket, "valuation_rate_date", "")
    row["valuation_provider"] = _agreed_value(bucket, "valuation_provider", "")
    for field in _DISPLAY_FIELDS:
        row[field] = _agreed_value(bucket, field, "")

    history_bucket = [item for item in bucket if _has_processed_history(item)]
    mutable_bucket = history_bucket or bucket
    mutable_projections = {
        tuple((field, _source_mutable_value(item, field)) for field in _MUTABLE_FIELDS)
        for item in mutable_bucket
    }
    history_ambiguous = bool(history_bucket) and len(mutable_projections) > 1
    if len(mutable_projections) == 1:
        for field, value in next(iter(mutable_projections)):
            row[field] = value
    else:
        row.update(
            {
                "category": "Unknown",
                "flow_type": "unresolved",
                "flow_source": "deterministic",
                "owner": _agreed_value(bucket, "owner", "Unknown"),
                "payment_method": _agreed_value(bucket, "payment_method", "Unknown"),
                "confidence": "0.00",
                "needs_review": "true",
                "review_reasons": (
                    f"{REVIEW_REASON_CATEGORY};{REVIEW_REASON_ACCOUNTING_FLOW}"
                ),
                "reason": "Canonical occurrence requires review",
                "flags": "uncategorized",
            }
        )
    return row, history_ambiguous


def _has_processed_history(row: Mapping[str, object]) -> bool:
    flags = str(row.get("flags", "")).split(";")
    return (
        str(row.get("category", "")) not in {"", "Unknown"}
        or str(row.get("flow_source", "")) == "correction"
        or "manual_correction" in flags
        or str(row.get("confidence", "")) not in {"", "0", "0.00"}
        or str(row.get("needs_review", "")) == "false"
    )


def _source_mutable_value(row: Mapping[str, object], field: str) -> str:
    value = str(row.get(field, ""))
    if field == "flags":
        return ";".join(
            item for item in value.split(";") if item and item not in _DUPLICATE_FLAGS
        )
    if field == "reason":
        return "; ".join(
            item
            for item in value.split("; ")
            if item
            and item != "Possible duplicate transaction"
            and not item.startswith("Duplicate candidate [")
        )
    return value


def _agreed_value(bucket: list[Mapping[str, object]], field: str, fallback: str) -> str:
    values = {str(item.get(field, "")) for item in bucket}
    return next(iter(values)) if len(values) == 1 else fallback


def _clear_source_provenance(row: dict[str, str]) -> None:
    for field in (
        *_SOURCE_ID_FIELDS,
        *_SOURCE_DISPLAY_FIELDS,
        *_STATEMENT_BALANCE_FIELDS,
    ):
        row[field] = ""


def _clear_duplicate_state(row: dict[str, str]) -> None:
    flags = [
        item
        for item in row.get("flags", "").split(";")
        if item and item not in _DUPLICATE_FLAGS
    ]
    row["flags"] = ";".join(flags)
    reasons = [
        item
        for item in row.get("reason", "").split("; ")
        if item
        and item != "Possible duplicate transaction"
        and not item.startswith("Duplicate candidate [")
    ]
    row["reason"] = "; ".join(reasons)


def _apply_overlap_review(
    row: dict[str, str], status: str, *, resolved: bool = False
) -> None:
    flags = [item for item in row.get("flags", "").split(";") if item]
    reason = _remove_reason(row.get("reason", ""), OVERLAP_AMBIGUITY_REASON)
    had_ambiguity = OVERLAP_AMBIGUITY_FLAG in flags
    prior_review = OVERLAP_PRIOR_REVIEW_FLAG in flags
    flags = [item for item in flags if item != OVERLAP_AMBIGUITY_FLAG]
    flags = [item for item in flags if item != OVERLAP_PRIOR_REVIEW_FLAG]
    if status == AMBIGUOUS_COUNT_STATUS and not resolved:
        if not had_ambiguity and row.get("needs_review") == "true":
            flags.append(OVERLAP_PRIOR_REVIEW_FLAG)
        elif prior_review:
            flags.append(OVERLAP_PRIOR_REVIEW_FLAG)
        flags.append(OVERLAP_AMBIGUITY_FLAG)
        reason = _append_reason(reason, OVERLAP_AMBIGUITY_REASON)
        set_review_reason(row, REVIEW_REASON_IDENTITY, True)
    elif had_ambiguity and "manual_correction" not in flags:
        set_review_reason(
            row,
            REVIEW_REASON_IDENTITY,
            prior_review or OVERLAP_HISTORY_FLAG in flags,
        )
    row["flags"] = ";".join(dict.fromkeys(flags))
    row["reason"] = reason


def _append_reason(reasons: str, reason: str) -> str:
    if reason in reasons:
        return reasons
    return f"{reasons}; {reason}" if reasons else reason


def _remove_reason(reasons: str, reason: str) -> str:
    if reasons == reason:
        return ""
    if reasons.startswith(reason + "; "):
        return reasons[len(reason) + 2 :]
    if reasons.endswith("; " + reason):
        return reasons[: -len(reason) - 2]
    return reasons.replace("; " + reason + "; ", "; ")


def _validate_canonical_amount_hkd(
    canonical_row: Mapping[str, object],
    source_occurrences: Sequence[Mapping[str, object]],
    group_fingerprint: str,
) -> None:
    """Require each canonical total to match its active source evidence."""
    try:
        source_amounts = {
            normalized_decimal(row.get("amount_hkd", ""))
            for row in source_occurrences
            if has_stable_v2_identity(row)
            and record_fingerprint(row) == group_fingerprint
        }
        canonical_amount = normalized_decimal(canonical_row.get("amount_hkd", ""))
    except IdentityError as error:
        raise ValueError("overlap_manifest_invalid") from error
    matched_exchange = (
        canonical_row.get("valuation_source") == VALUATION_SOURCE_MATCHED_EXCHANGE
        and bool(canonical_amount)
        and all(
            str(row.get("valuation_source", ""))
            in {
                "",
                "missing",
                "configured_dated_rate",
                "hkma_daily_reference_rate",
                "configured_fixed_rate",
            }
            for row in source_occurrences
            if has_stable_v2_identity(row)
            and record_fingerprint(row) == group_fingerprint
        )
    )
    if not matched_exchange and (
        len(source_amounts) != 1 or canonical_amount not in source_amounts
    ):
        raise ValueError("overlap_manifest_invalid")


def _group_id(namespace_key: str, fingerprint: str) -> str:
    return "ovg_" + _keyed_digest(
        namespace_key, "canonical-overlap-group-v1", fingerprint.encode("ascii")
    )


def _canonical_transaction_id(namespace_key: str, group_id: str, slot: int) -> str:
    return (
        "txn_"
        + _keyed_digest(
            namespace_key,
            "canonical-overlap-slot-v1",
            group_id.encode("ascii"),
            struct.pack(">Q", slot),
        )[:32]
    )


def _membership_digest(
    namespace_key: str,
    bucket: list[Mapping[str, object]],
    source_pools: list[list[Mapping[str, object]]],
    canonical_slot_ids: list[str],
    ambiguous_tail_ids: list[str],
) -> str:
    members = sorted(
        (
            str(row["source_id"]),
            str(row["source_record_id"]),
        )
        for row in bucket
    )
    source_counts = sorted(
        (str(pool[0]["source_id"]), len(pool)) for pool in source_pools if pool
    )
    components = [
        b"active-source-record-membership-v1",
        struct.pack(">Q", len(members)),
    ]
    for source_id, source_record_id in members:
        components.extend((source_id.encode("ascii"), source_record_id.encode("ascii")))
    components.extend((b"per-source-counts-v1", struct.pack(">Q", len(source_counts))))
    for source_id, count in source_counts:
        components.extend((source_id.encode("ascii"), struct.pack(">Q", count)))
    components.extend(
        (b"active-canonical-slots-v1", struct.pack(">Q", len(canonical_slot_ids)))
    )
    components.extend(identifier.encode("ascii") for identifier in canonical_slot_ids)
    components.extend(
        (b"ambiguous-tail-slots-v1", struct.pack(">Q", len(ambiguous_tail_ids)))
    )
    components.extend(identifier.encode("ascii") for identifier in ambiguous_tail_ids)
    return "ovm_" + _keyed_digest(
        namespace_key,
        "duplicate-membership-v1",
        *components,
    )


def _review_group_id(
    namespace_key: str, overlap_group_id: str, membership_digest: str
) -> str:
    return "ovr_" + _keyed_digest(
        namespace_key,
        "duplicate-review-group-v1",
        overlap_group_id.encode("ascii"),
        membership_digest.encode("ascii"),
    )


def _keyed_digest(namespace_key: str, domain: str, *components: bytes) -> str:
    if _NAMESPACE_RE.fullmatch(namespace_key) is None:
        raise ValueError("overlap_manifest_invalid")
    key = bytes.fromhex(namespace_key.removeprefix("ovns_"))
    framed = bytearray(b"honeymoney.overlap\x00")
    domain_bytes = domain.encode("ascii")
    framed.extend(struct.pack(">I", len(domain_bytes)))
    framed.extend(domain_bytes)
    framed.extend(struct.pack(">I", len(components)))
    for component in components:
        framed.extend(struct.pack(">Q", len(component)))
        framed.extend(component)
    return hmac.new(key, bytes(framed), hashlib.sha256).hexdigest()


def _validated_manifest_copy(manifest: Mapping[str, object]) -> OverlapManifest:
    copied = copy.deepcopy(dict(manifest))
    if copied.get("schema_version") == 1:
        _validate_v1_manifest(copied)
        legacy = cast(LegacyOverlapManifest, copied)
        return {
            "schema_version": OVERLAP_MANIFEST_SCHEMA_VERSION,
            "namespace_key": legacy["namespace_key"],
            "groups": [
                {
                    "overlap_group_id": group["group_id"],
                    "record_fingerprint": group["record_fingerprint"],
                    "memberships": [],
                    "slots": group["slots"],
                }
                for group in legacy["groups"]
            ],
        }
    _validate_manifest(copied)
    return cast(OverlapManifest, copied)


def _validate_manifest(manifest: Mapping[str, object]) -> None:
    if set(manifest) != {"schema_version", "namespace_key", "groups"}:
        raise ValueError("overlap_manifest_invalid")
    if manifest.get("schema_version") != OVERLAP_MANIFEST_SCHEMA_VERSION:
        raise ValueError("overlap_manifest_invalid")
    namespace_key = manifest.get("namespace_key")
    if (
        not isinstance(namespace_key, str)
        or _NAMESPACE_RE.fullmatch(namespace_key) is None
    ):
        raise ValueError("overlap_manifest_invalid")
    groups = manifest.get("groups")
    if not isinstance(groups, list):
        raise ValueError("overlap_manifest_invalid")
    prior_group = ""
    seen_transactions: set[str] = set()
    seen_fingerprints: set[str] = set()
    seen_review_groups: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "overlap_group_id",
            "record_fingerprint",
            "memberships",
            "slots",
        }:
            raise ValueError("overlap_manifest_invalid")
        overlap_group_id = group.get("overlap_group_id")
        fingerprint = group.get("record_fingerprint")
        if (
            not isinstance(overlap_group_id, str)
            or _GROUP_RE.fullmatch(overlap_group_id) is None
            or not isinstance(fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(fingerprint) is None
            or overlap_group_id != _group_id(namespace_key, fingerprint)
            or overlap_group_id <= prior_group
            or fingerprint in seen_fingerprints
        ):
            raise ValueError("overlap_manifest_invalid")
        prior_group = overlap_group_id
        seen_fingerprints.add(fingerprint)
        memberships = group.get("memberships")
        if not isinstance(memberships, list):
            raise ValueError("overlap_manifest_invalid")
        prior_membership = ""
        for membership in memberships:
            if (
                not isinstance(membership, dict)
                or set(membership)
                != {
                    "overlap_group_id",
                    "group_id",
                    "membership_digest",
                    "resolution",
                }
                or membership.get("overlap_group_id") != overlap_group_id
                or membership.get("resolution")
                not in {"unresolved", "same-event", "keep-all"}
                or not isinstance(membership.get("membership_digest"), str)
                or _MEMBERSHIP_RE.fullmatch(membership["membership_digest"]) is None
                or membership["membership_digest"] <= prior_membership
                or not isinstance(membership.get("group_id"), str)
                or _REVIEW_GROUP_RE.fullmatch(membership["group_id"]) is None
                or membership["group_id"] in seen_review_groups
                or membership.get("group_id")
                != _review_group_id(
                    namespace_key,
                    overlap_group_id,
                    membership["membership_digest"],
                )
            ):
                raise ValueError("overlap_manifest_invalid")
            prior_membership = membership["membership_digest"]
            seen_review_groups.add(membership["group_id"])
        slots = group.get("slots")
        if not isinstance(slots, list):
            raise ValueError("overlap_manifest_invalid")
        for expected_slot, slot in enumerate(slots, start=1):
            if (
                not isinstance(slot, dict)
                or set(slot) != {"slot", "transaction_id", "state"}
                or slot.get("slot") != expected_slot
                or slot.get("state") not in {"active", "retired"}
            ):
                raise ValueError("overlap_manifest_invalid")
            identifier = slot.get("transaction_id")
            if (
                not isinstance(identifier, str)
                or _TRANSACTION_RE.fullmatch(identifier) is None
                or identifier
                != _canonical_transaction_id(
                    namespace_key, overlap_group_id, expected_slot
                )
                or identifier in seen_transactions
            ):
                raise ValueError("overlap_manifest_invalid")
            seen_transactions.add(identifier)


def _validate_v1_manifest(manifest: Mapping[str, object]) -> None:
    if set(manifest) != {"schema_version", "namespace_key", "groups"}:
        raise ValueError("overlap_manifest_invalid")
    namespace_key = manifest.get("namespace_key")
    groups = manifest.get("groups")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(namespace_key, str)
        or _NAMESPACE_RE.fullmatch(namespace_key) is None
        or not isinstance(groups, list)
    ):
        raise ValueError("overlap_manifest_invalid")
    prior_group = ""
    seen_transactions: set[str] = set()
    seen_fingerprints: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {
            "group_id",
            "record_fingerprint",
            "slots",
        }:
            raise ValueError("overlap_manifest_invalid")
        group_id = group.get("group_id")
        fingerprint = group.get("record_fingerprint")
        if (
            not isinstance(group_id, str)
            or _GROUP_RE.fullmatch(group_id) is None
            or not isinstance(fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(fingerprint) is None
            or group_id != _group_id(namespace_key, fingerprint)
            or group_id <= prior_group
            or fingerprint in seen_fingerprints
        ):
            raise ValueError("overlap_manifest_invalid")
        prior_group = group_id
        seen_fingerprints.add(fingerprint)
        slots = group.get("slots")
        if not isinstance(slots, list):
            raise ValueError("overlap_manifest_invalid")
        for expected_slot, slot in enumerate(slots, start=1):
            if (
                not isinstance(slot, dict)
                or set(slot) != {"slot", "transaction_id", "state"}
                or slot.get("slot") != expected_slot
                or slot.get("state") not in {"active", "retired"}
            ):
                raise ValueError("overlap_manifest_invalid")
            identifier = slot.get("transaction_id")
            if (
                not isinstance(identifier, str)
                or _TRANSACTION_RE.fullmatch(identifier) is None
                or identifier
                != _canonical_transaction_id(namespace_key, group_id, expected_slot)
                or identifier in seen_transactions
            ):
                raise ValueError("overlap_manifest_invalid")
            seen_transactions.add(identifier)


def _safe_tail_corrections(
    result: CanonicalizationResult,
    diagnostic: OverlapGroupDiagnostic,
    corrections: Mapping[str, Mapping[str, str]],
) -> tuple[tuple[str, ...], dict[str, dict[str, str]]]:
    rows = [
        row
        for row in result.rows
        if row.get("canonical_group_id") == diagnostic["canonical_group_id"]
    ]
    rows.sort(key=lambda row: int(row["canonical_slot"]))
    keep_count = int(diagnostic["same_event_count"])
    retained_rows = rows[:keep_count]
    tail_rows = rows[keep_count:]
    tail_ids = tuple(row["transaction_id"] for row in rows[keep_count:])
    patches = {
        row["transaction_id"]: dict(corrections[row["transaction_id"]])
        for row in rows
        if row["transaction_id"] in corrections
    }
    unique_patches = {tuple(sorted(patch.items())): patch for patch in patches.values()}
    removed = tuple(identifier for identifier in tail_ids if identifier in patches)
    updates: dict[str, dict[str, str]] = {}
    if keep_count == 1 and patches:
        if len(removed) > 1:
            raise DuplicateResolutionError("duplicate_history_conflict")
        if len(unique_patches) != 1:
            raise DuplicateResolutionError("duplicate_history_conflict")
        patch = dict(next(iter(unique_patches.values())))
        retained_id = retained_rows[0]["transaction_id"]
        retained_patch = patches.get(retained_id)
        if retained_patch is not None and retained_patch != patch:
            raise DuplicateResolutionError("duplicate_history_conflict")
        if retained_patch is None:
            updates[retained_id] = patch
        histories = {
            _protected_history(_project_correction(row, patch)) for row in rows
        }
        if len(histories) != 1:
            raise DuplicateResolutionError("duplicate_history_conflict")
        return removed, updates

    if any(identifier in patches for identifier in tail_ids):
        if len(patches) != len(rows) or len(unique_patches) != 1:
            raise DuplicateResolutionError("duplicate_history_conflict")
    retained_histories = {_protected_history(row) for row in retained_rows}
    if any(_protected_history(row) not in retained_histories for row in tail_rows):
        raise DuplicateResolutionError("duplicate_history_conflict")
    return removed, updates


def _project_correction(
    row: Mapping[str, object], patch: Mapping[str, str]
) -> dict[str, str]:
    projected = {str(key): str(value) for key, value in row.items()}
    for field in (
        "category",
        "flow_type",
        "owner",
        "payment_method",
        "confidence",
        "reason",
        "notes",
        "needs_review",
        "review_reasons",
    ):
        if field in patch:
            projected[field] = str(patch[field])
    if "flow_type" in patch:
        projected["flow_source"] = "correction"
    elif "category" in patch and projected.get("flow_source", "") in {
        "",
        "deterministic",
    }:
        derive_flow_type(projected)
    flags = [item for item in projected.get("flags", "").split(";") if item]
    if "manual_correction" not in flags:
        flags.append("manual_correction")
    projected["flags"] = ";".join(flags)
    synchronize_review_state(projected)
    return projected


def _protected_history(row: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    values = {field: str(row.get(field, "")) for field in _MUTABLE_FIELDS}
    values["flags"] = ";".join(
        item
        for item in values["flags"].split(";")
        if item and item not in {OVERLAP_AMBIGUITY_FLAG, OVERLAP_PRIOR_REVIEW_FLAG}
    )
    values["reason"] = _without_owned_reason(values["reason"], OVERLAP_AMBIGUITY_REASON)
    return tuple((field, values[field]) for field in _MUTABLE_FIELDS)


def _without_owned_reason(value: str, owned_reason: str) -> str:
    if owned_reason not in value:
        return value
    return "; ".join(
        item for item in value.replace(owned_reason, "").strip(" ;").split("; ") if item
    )


def _bounded(value: object, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_source_display(value: object) -> str:
    parts = re.split(r"[/\\\\]+", str(value))
    return _bounded(parts[-1] if parts else "")
