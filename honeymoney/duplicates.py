"""Pure duplicate-candidate evaluation from stable identity evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence, TypedDict

from honeymoney.identity import has_stable_v2_identity, record_fingerprint
from honeymoney.identity_contracts import IdentityRow
from honeymoney.review_state import (
    REVIEW_REASON_IDENTITY,
    has_identity_review_evidence,
    set_review_reason,
)

DUPLICATE_FLAG = "duplicate_suspected"
DUPLICATE_REVIEW_PROMOTED_FLAG = "duplicate_review_promoted"
DUPLICATE_MATCH_TYPE = "exact_same_account_event_v2"
DUPLICATE_REASON_PREFIX = "Duplicate candidate ["
LEGACY_DUPLICATE_REASON = "Possible duplicate transaction"


class DuplicateGroupDiagnostic(TypedDict):
    match_type: str
    occurrence_ids: list[str]


class DuplicateOccurrenceDiagnostic(DuplicateGroupDiagnostic):
    counterpart_occurrence_ids: list[str]


@dataclass(frozen=True)
class DuplicateCandidateGroup:
    """One stable set of matching occurrences."""

    match_type: str
    occurrence_ids: tuple[str, ...]

    def as_diagnostic(self) -> DuplicateGroupDiagnostic:
        return {
            "match_type": self.match_type,
            "occurrence_ids": list(self.occurrence_ids),
        }


@dataclass(frozen=True)
class DuplicateEvaluation:
    """The complete duplicate-candidate result for one prospective ledger."""

    groups: tuple[DuplicateCandidateGroup, ...]
    _diagnostic_groups_by_occurrence_id: Mapping[str, DuplicateCandidateGroup] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        diagnostic_groups: dict[str, DuplicateCandidateGroup] = {}
        for group in self.groups:
            for occurrence_id in group.occurrence_ids:
                diagnostic_groups[occurrence_id] = group
        object.__setattr__(
            self,
            "_diagnostic_groups_by_occurrence_id",
            MappingProxyType(diagnostic_groups),
        )

    @property
    def occurrence_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                occurrence_id
                for group in self.groups
                for occurrence_id in group.occurrence_ids
            )
        )

    @property
    def occurrence_count(self) -> int:
        return sum(len(group.occurrence_ids) for group in self.groups)

    def diagnostic_for(
        self, occurrence_id: str
    ) -> DuplicateOccurrenceDiagnostic | None:
        group = self._diagnostic_groups_by_occurrence_id.get(occurrence_id)
        if group is None:
            return None
        return {
            "match_type": group.match_type,
            "occurrence_ids": list(group.occurrence_ids),
            "counterpart_occurrence_ids": [
                candidate
                for candidate in group.occurrence_ids
                if candidate != occurrence_id
            ],
        }

    def as_diagnostic(self) -> dict[str, object]:
        return {
            "group_count": len(self.groups),
            "occurrence_count": self.occurrence_count,
            "groups": [group.as_diagnostic() for group in self.groups],
        }

    def restricted_to(self, occurrence_ids: set[str]) -> "DuplicateEvaluation":
        return DuplicateEvaluation(
            tuple(
                group
                for group in self.groups
                if any(item in occurrence_ids for item in group.occurrence_ids)
            )
        )


def evaluate_duplicate_candidates(
    rows: Sequence[IdentityRow],
    *,
    operation_counts: dict[str, int] | None = None,
) -> DuplicateEvaluation:
    """Return groups proven by account, fingerprint, and distinct source identity."""
    buckets: dict[str, list[IdentityRow]] = {}
    fingerprints_calculated = 0
    for row in rows:
        if not has_stable_v2_identity(row):
            continue
        if not str(row.get("account_id", "")).strip():
            continue
        fingerprint = record_fingerprint(row)
        fingerprints_calculated += 1
        buckets.setdefault(fingerprint, []).append(row)

    groups = []
    for bucket in buckets.values():
        if len({str(row["source_id"]) for row in bucket}) < 2:
            continue
        occurrence_ids = tuple(sorted(str(row["transaction_id"]) for row in bucket))
        groups.append(DuplicateCandidateGroup(DUPLICATE_MATCH_TYPE, occurrence_ids))
    groups.sort(key=lambda group: group.occurrence_ids)

    if operation_counts is not None:
        operation_counts.update(
            {
                "rows_examined": len(rows),
                "fingerprints_calculated": fingerprints_calculated,
                "candidate_buckets": len(buckets),
            }
        )
    return DuplicateEvaluation(tuple(groups))


def apply_duplicate_candidates(
    rows: list[dict[str, str]],
    evaluation: DuplicateEvaluation,
    *,
    final_review_ids: set[str] | None = None,
) -> None:
    """Replace all legacy/current duplicate annotations with ``evaluation``."""
    final_review_ids = final_review_ids or set()
    for row in rows:
        _clear_duplicate_state(row)

    rows_by_id = {
        row["transaction_id"]: row
        for row in rows
        if has_stable_v2_identity(row) and row.get("transaction_id")
    }
    for group in evaluation.groups:
        for occurrence_id in group.occurrence_ids:
            row = rows_by_id[occurrence_id]
            if (
                occurrence_id not in final_review_ids
                and row.get("needs_review") != "true"
            ):
                row["flags"] = _append_flag(
                    row.get("flags", ""), DUPLICATE_REVIEW_PROMOTED_FLAG
                )
            if occurrence_id not in final_review_ids:
                set_review_reason(row, REVIEW_REASON_IDENTITY, True)
            row["flags"] = _append_flag(row.get("flags", ""), DUPLICATE_FLAG)
            diagnostic = evaluation.diagnostic_for(occurrence_id)
            if diagnostic is None:
                raise AssertionError("duplicate occurrence lost its candidate group")
            row["reason"] = _append_reason(
                row.get("reason", ""), _duplicate_reason(diagnostic)
            )


def refresh_duplicate_candidates(
    rows: list[dict[str, str]], *, final_review_ids: set[str] | None = None
) -> DuplicateEvaluation:
    """Evaluate and apply duplicate state for a complete prospective ledger."""
    evaluation = evaluate_duplicate_candidates(rows)
    apply_duplicate_candidates(rows, evaluation, final_review_ids=final_review_ids)
    return evaluation


def release_duplicate_review_ownership(row: dict[str, str]) -> None:
    """Keep review active after another check takes ownership of it."""
    flags = [item for item in row.get("flags", "").split(";") if item]
    row["flags"] = ";".join(
        item for item in flags if item != DUPLICATE_REVIEW_PROMOTED_FLAG
    )


def _clear_duplicate_state(row: dict[str, str]) -> None:
    flags = [item for item in row.get("flags", "").split(";") if item]
    row["flags"] = ";".join(
        item
        for item in flags
        if item not in {DUPLICATE_FLAG, DUPLICATE_REVIEW_PROMOTED_FLAG}
    )
    reasons = [
        item
        for item in row.get("reason", "").split("; ")
        if item
        and item != LEGACY_DUPLICATE_REASON
        and not item.startswith(DUPLICATE_REASON_PREFIX)
    ]
    row["reason"] = "; ".join(reasons)
    set_review_reason(
        row,
        REVIEW_REASON_IDENTITY,
        has_identity_review_evidence(row),
    )


def _duplicate_reason(diagnostic: DuplicateOccurrenceDiagnostic) -> str:
    occurrence_ids = ",".join(str(item) for item in diagnostic["occurrence_ids"])
    counterpart_ids = ",".join(
        str(item) for item in diagnostic["counterpart_occurrence_ids"]
    )
    return (
        f"{DUPLICATE_REASON_PREFIX}"
        f"match_type={diagnostic['match_type']}, "
        f"occurrence_ids={occurrence_ids}, "
        f"counterpart_occurrence_ids={counterpart_ids}]"
    )


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing.split("; "):
        return existing
    return f"{existing}; {reason}"
