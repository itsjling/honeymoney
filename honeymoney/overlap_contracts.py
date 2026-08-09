"""Static contracts for canonical overlap state and diagnostics."""

from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict

OverlapSlotState: TypeAlias = Literal["active", "retired"]
OverlapResolution: TypeAlias = Literal["unresolved", "same-event", "keep-all"]
OverlapDecision: TypeAlias = Literal["same-event", "keep-all"]
OverlapProvenanceStatus: TypeAlias = Literal[
    "single_source",
    "exact_one_to_one",
    "pooled_equal_count",
    "ambiguous_count_mismatch",
]


class OverlapSupportPool(TypedDict):
    """Exact active source-record support without a repeated-row pairing."""

    source_id: str
    source_record_ids: list[str]


class OverlapSlot(TypedDict):
    slot: int
    transaction_id: str
    state: OverlapSlotState
    supporting_source_ids: list[str]


class OverlapMembership(TypedDict):
    overlap_group_id: str
    group_id: str
    membership_digest: str
    resolution: OverlapResolution


class OverlapGroup(TypedDict):
    overlap_group_id: str
    record_fingerprint: str
    support_pools: list[OverlapSupportPool]
    memberships: list[OverlapMembership]
    slots: list[OverlapSlot]


class OverlapManifest(TypedDict):
    schema_version: int
    namespace_key: str
    groups: list[OverlapGroup]


class OverlapGroupDiagnostic(TypedDict):
    group_id: str
    canonical_group_id: str
    review_group_id: str
    canonical_transaction_ids: list[str]
    provenance_status: OverlapProvenanceStatus
    decision: OverlapDecision | None
    source_counts: list[int]
    keep_all_count: int
    same_event_count: int
    slot_support_counts: list[int]
    source_occurrence_pools: list[list[str]]


class OverlapWarning(TypedDict):
    code: str
    group_id: str


class OverlapDiagnostic(TypedDict):
    group_count: int
    ambiguous_group_count: int
    warnings: list[OverlapWarning]
    source_occurrence_count: int
    canonical_occurrence_count: int
    consolidated_occurrence_count: int
    provenance_counts: dict[OverlapProvenanceStatus, int]
    groups: list[OverlapGroupDiagnostic]


class DuplicateOccurrenceEvidence(TypedDict):
    occurrence_id: str
    account_id: str
    account: str
    institution: str
    date: str
    merchant: str
    amount: str
    currency: str
    source_display: str
    source_page: str
    source_row: str


class DuplicateGroupListing(TypedDict):
    group_id: str
    match_basis: str
    source_counts: list[int]
    keep_all_count: int
    same_event_count: int
    canonical_occurrence_ids: list[str]
    occurrences: list[DuplicateOccurrenceEvidence]
