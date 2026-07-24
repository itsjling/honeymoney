"""Public static contracts at the identity boundary."""

from __future__ import annotations

from typing import Literal, Mapping, TypeAlias, TypedDict

IdentityRow: TypeAlias = Mapping[str, object]
IdentityRecordState: TypeAlias = Literal["active", "retired"]
TransactionIdKind: TypeAlias = Literal["v2", "preserved_legacy"]


class AllocationLocatorManifest(TypedDict):
    adapter_tag: int
    components: list[int]


class AllocationOriginManifest(TypedDict):
    source_revision: str
    extractor_contract_id: str
    locator: AllocationLocatorManifest
    occurrence_ordinal: int


class IdentityRecordManifest(TypedDict):
    transaction_id: str
    transaction_id_kind: TransactionIdKind
    source_record_id: str
    record_fingerprint: str
    state: IdentityRecordState
    current_locator: AllocationLocatorManifest | None
    allocation_origin: AllocationOriginManifest


class IdentitySourceManifest(TypedDict):
    source_id: str
    source_namespace_id: str
    source_revision: str
    extractor_contract_id: str
    records: list[IdentityRecordManifest]


class IdentityManifest(TypedDict):
    schema_version: int
    sources: list[IdentitySourceManifest]
