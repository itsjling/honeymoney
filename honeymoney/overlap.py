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
from typing import Any, Mapping

from honeymoney.identity import (
    IdentityError,
    has_stable_v2_identity,
    normalized_decimal,
    normalized_record_identity,
    record_fingerprint,
)
from honeymoney.schema import CATEGORIZED_COLUMNS

OVERLAP_MANIFEST_SCHEMA_VERSION = 1
OVERLAP_MANIFEST_NAME = ".honeymoney-overlap-manifest.json"
SOURCE_OCCURRENCES_NAME = ".honeymoney-source-occurrences.csv"

SINGLE_SOURCE_STATUS = "single_source"
EXACT_ONE_TO_ONE_STATUS = "exact_one_to_one"
EQUAL_POOL_STATUS = "pooled_equal_count"
AMBIGUOUS_COUNT_STATUS = "ambiguous_count_mismatch"
PROVENANCE_STATUSES = {
    SINGLE_SOURCE_STATUS,
    EXACT_ONE_TO_ONE_STATUS,
    EQUAL_POOL_STATUS,
    AMBIGUOUS_COUNT_STATUS,
}

OVERLAP_AMBIGUITY_FLAG = "overlap_count_ambiguous"
OVERLAP_AMBIGUITY_REASON = (
    "Exact overlap has different source occurrence counts; provenance remains pooled"
)
OVERLAP_HISTORY_FLAG = "overlap_history_ambiguous"
OVERLAP_HISTORY_REASON = (
    "Repeated exact occurrences have conflicting review history; no assignment was made"
)

_NAMESPACE_RE = re.compile(r"^ovns_[0-9a-f]{64}$")
_GROUP_RE = re.compile(r"^ovg_[0-9a-f]{64}$")
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
    "reason",
    "flags",
    "notes",
)


@dataclass(frozen=True)
class CanonicalizationResult:
    """Complete canonical ledger and overlap-manifest result."""

    rows: list[dict[str, str]]
    manifest: dict[str, Any]
    diagnostic: dict[str, Any]
    source_occurrence_count: int
    canonical_occurrence_count: int
    consolidated_occurrence_count: int
    ambiguous_group_count: int


@dataclass(frozen=True)
class CorrectionProjection:
    """Canonical correction bindings plus rows that cannot be assigned."""

    corrections: dict[str, dict[str, str]]
    ambiguous_transaction_ids: tuple[str, ...]


def empty_overlap_manifest(namespace_key: str) -> dict[str, Any]:
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


def overlap_manifest_document(manifest: Mapping[str, Any]) -> str:
    copied = _validated_manifest_copy(manifest)
    return (
        json.dumps(copied, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )


def parse_overlap_manifest(document: str) -> dict[str, Any]:
    try:
        value = json.loads(document)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("overlap_manifest_invalid") from error
    if not isinstance(value, dict):
        raise ValueError("overlap_manifest_invalid")
    _validate_manifest(value)
    if overlap_manifest_document(value) != document:
        raise ValueError("overlap_manifest_invalid")
    return value


def validate_overlap_agreement(
    canonical_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    source_occurrences: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    manifest: Mapping[str, Any],
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
            if any(
                str(actual.get(field, "")) != expected_row[field]
                for field in (
                    "transaction_id",
                    *_FINANCIAL_FIELDS,
                    *_SOURCE_ID_FIELDS,
                    *_SOURCE_DISPLAY_FIELDS,
                    *_STATEMENT_BALANCE_FIELDS,
                )
            ):
                raise ValueError("overlap_manifest_invalid")
            continue
        if any(str(actual.get(field, "")) != expected_row[field] for field in fields):
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
            if group["group_id"] == actual["canonical_group_id"]
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


def apply_history_ambiguity(
    rows: list[dict[str, str]], transaction_ids: set[str] | tuple[str, ...]
) -> None:
    """Force pooled correction history to review without choosing a slot."""
    selected = set(transaction_ids)
    for row in rows:
        if row.get("transaction_id") not in selected:
            continue
        flags = [item for item in row.get("flags", "").split(";") if item]
        reasons = [item for item in row.get("reason", "").split("; ") if item]
        if OVERLAP_HISTORY_FLAG not in flags:
            flags.append(OVERLAP_HISTORY_FLAG)
        if OVERLAP_HISTORY_REASON not in reasons:
            reasons.append(OVERLAP_HISTORY_REASON)
        row["flags"] = ";".join(flags)
        row["reason"] = "; ".join(reasons)
        row["needs_review"] = "true"


def enforce_overlap_review(rows: list[dict[str, str]]) -> None:
    """Reapply protected count/history review after mutable processing."""
    for row in rows:
        if not row.get("canonical_group_id"):
            _clear_duplicate_state(row)
        _apply_overlap_review(row, row.get("provenance_status", ""))
        if OVERLAP_HISTORY_FLAG in row.get("flags", "").split(";"):
            row["needs_review"] = "true"


def canonicalize_overlaps(
    source_occurrences: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    prior_canonical_rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    prior_manifest: Mapping[str, Any],
) -> CanonicalizationResult:
    """Return the canonical multiset without assigning repeated source rows."""
    occurrences = tuple(source_occurrences)
    prior_rows = tuple(prior_canonical_rows)
    manifest = _validated_manifest_copy(prior_manifest)
    namespace_key = manifest["namespace_key"]

    buckets: dict[str, list[Mapping[str, Any]]] = {}
    legacy_rows: list[dict[str, str]] = []
    for row in occurrences:
        if not has_stable_v2_identity(row):
            legacy_rows.append(
                {column: str(row.get(column, "")) for column in CATEGORIZED_COLUMNS}
            )
            continue
        fingerprint = record_fingerprint(row)
        buckets.setdefault(fingerprint, []).append(row)

    prior_groups = {
        str(group["record_fingerprint"]): group for group in manifest["groups"]
    }
    fingerprints = sorted(set(prior_groups) | set(buckets))
    prior_by_id = {
        str(row.get("transaction_id", "")): row
        for row in prior_rows
        if row.get("transaction_id")
    }

    next_groups: list[dict[str, Any]] = []
    canonical_rows: list[dict[str, str]] = []
    diagnostics: list[dict[str, Any]] = []
    ambiguous_groups = 0
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
        active_count = max(source_counts, default=0)
        prior_group = prior_groups.get(fingerprint)
        group_id = (
            str(prior_group["group_id"])
            if prior_group is not None
            else _group_id(namespace_key, fingerprint)
        )
        prior_slots = {
            int(slot["slot"]): slot for slot in (prior_group or {}).get("slots", [])
        }
        slot_count = max(active_count, max(prior_slots, default=0))
        slots = []
        for slot_number in range(1, slot_count + 1):
            identifier = (
                str(prior_slots[slot_number]["transaction_id"])
                if slot_number in prior_slots
                else _canonical_transaction_id(namespace_key, group_id, slot_number)
            )
            slots.append(
                {
                    "slot": slot_number,
                    "transaction_id": identifier,
                    "state": "active" if slot_number <= active_count else "retired",
                }
            )
        next_groups.append(
            {
                "group_id": group_id,
                "record_fingerprint": fingerprint,
                "slots": slots,
            }
        )
        if not bucket:
            continue

        status = _provenance_status(source_counts)
        if status == AMBIGUOUS_COUNT_STATUS:
            ambiguous_groups += 1
        diagnostic = {
            "group_id": group_id,
            "canonical_transaction_ids": [
                slot["transaction_id"] for slot in slots if slot["state"] == "active"
            ],
            "provenance_status": status,
            "source_counts": source_counts,
            "slot_support_counts": [
                sum(count >= slot for count in source_counts)
                for slot in range(1, active_count + 1)
            ],
            "source_occurrence_pools": [
                sorted(str(row["transaction_id"]) for row in pool)
                for pool in source_pools
            ],
        }
        diagnostics.append(diagnostic)

        agreed_template, history_ambiguous = _agreed_canonical_template(bucket)
        for slot in slots[:active_count]:
            identifier = str(slot["transaction_id"])
            if identifier in source_transaction_ids:
                raise ValueError("overlap_identity_hash_conflict")
            prior = prior_by_id.get(identifier)
            row = (
                copy.deepcopy(dict(prior))
                if prior is not None
                else copy.deepcopy(agreed_template)
            )
            row.update(
                {
                    "transaction_id": identifier,
                    "canonical_group_id": group_id,
                    "canonical_slot": str(slot["slot"]),
                    "provenance_status": status,
                    "source_occurrence_count": str(len(bucket)),
                }
            )
            _clear_source_provenance(row)
            _clear_duplicate_state(row)
            _apply_overlap_review(row, status)
            if history_ambiguous and prior is None:
                apply_history_ambiguity([row], {identifier})
            canonical_rows.append(row)

    next_groups.sort(key=lambda group: group["group_id"])
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
    next_manifest = {
        "schema_version": OVERLAP_MANIFEST_SCHEMA_VERSION,
        "namespace_key": namespace_key,
        "groups": next_groups,
    }
    _validate_manifest(next_manifest)
    diagnostic = {
        "group_count": len(diagnostics),
        "ambiguous_group_count": ambiguous_groups,
        "source_occurrence_count": len(occurrences),
        "canonical_occurrence_count": len(canonical_rows),
        "consolidated_occurrence_count": len(occurrences) - len(canonical_rows),
        "groups": diagnostics,
    }
    return CanonicalizationResult(
        rows=canonical_rows,
        manifest=next_manifest,
        diagnostic=diagnostic,
        source_occurrence_count=len(occurrences),
        canonical_occurrence_count=len(canonical_rows),
        consolidated_occurrence_count=len(occurrences) - len(canonical_rows),
        ambiguous_group_count=ambiguous_groups,
    )


def _source_pools(
    bucket: list[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    for row in bucket:
        by_source.setdefault(str(row["source_id"]), []).append(row)
    pools = [
        sorted(rows, key=lambda row: str(row["transaction_id"]))
        for rows in by_source.values()
    ]
    pools.sort(key=lambda pool: str(pool[0]["transaction_id"]))
    return pools


def _provenance_status(source_counts: list[int]) -> str:
    if len(source_counts) == 1:
        return SINGLE_SOURCE_STATUS
    if source_counts == [1] * len(source_counts):
        return EXACT_ONE_TO_ONE_STATUS
    if len(set(source_counts)) == 1:
        return EQUAL_POOL_STATUS
    return AMBIGUOUS_COUNT_STATUS


def _agreed_canonical_template(
    bucket: list[Mapping[str, Any]],
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
    for field in _DISPLAY_FIELDS:
        row[field] = _agreed_value(bucket, field, "")

    mutable_projections = {
        tuple((field, _source_mutable_value(item, field)) for field in _MUTABLE_FIELDS)
        for item in bucket
    }
    history_ambiguous = len(mutable_projections) > 1
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
                "reason": "Canonical occurrence requires review",
                "flags": "uncategorized",
            }
        )
    return row, history_ambiguous


def _source_mutable_value(row: Mapping[str, Any], field: str) -> str:
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


def _agreed_value(bucket: list[Mapping[str, Any]], field: str, fallback: str) -> str:
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


def _apply_overlap_review(row: dict[str, str], status: str) -> None:
    flags = [item for item in row.get("flags", "").split(";") if item]
    reason = _remove_reason(row.get("reason", ""), OVERLAP_AMBIGUITY_REASON)
    had_ambiguity = OVERLAP_AMBIGUITY_FLAG in flags
    flags = [item for item in flags if item != OVERLAP_AMBIGUITY_FLAG]
    if status == AMBIGUOUS_COUNT_STATUS:
        flags.append(OVERLAP_AMBIGUITY_FLAG)
        reason = _append_reason(reason, OVERLAP_AMBIGUITY_REASON)
        row["needs_review"] = "true"
    elif had_ambiguity and not flags and not reason:
        row["needs_review"] = "false"
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
    canonical_row: Mapping[str, Any],
    source_occurrences: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
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
    if len(source_amounts) != 1 or canonical_amount not in source_amounts:
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


def _validated_manifest_copy(manifest: Mapping[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(dict(manifest))
    _validate_manifest(copied)
    return copied


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
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
