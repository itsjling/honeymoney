"""Public static contracts for stable transaction identity services."""

from pathlib import Path
from typing import Final, Mapping, Sequence

from honeymoney.identity_contracts import IdentityManifest, IdentityRow

IDENTITY_MANIFEST_NAME: Final[str]
ID_FIELDS: Final[tuple[str, ...]]

class IdentityError(ValueError):
    code: str
    diagnostic: object | None

    def __init__(
        self,
        code: str,
        diagnostic: object | None = None,
        message: str | None = None,
    ) -> None: ...

class AllocationLocator:
    adapter_tag: int
    components: tuple[int, ...]

    def __init__(self, adapter_tag: int, components: tuple[int, ...]) -> None: ...
    def as_manifest(self) -> dict[str, object]: ...

class IncomingRecordIdentity:
    row: IdentityRow
    locator: AllocationLocator

    def __init__(self, row: IdentityRow, locator: AllocationLocator) -> None: ...

class IncomingSourceIdentity:
    stable_handle: str
    source_display: str
    namespace_id: str
    revision: str
    contract_id: str
    record_data: object | None

    def __init__(
        self,
        stable_handle: str,
        source_display: str,
        namespace_id: str,
        revision: str,
        contract_id: str,
        record_data: object | None = None,
    ) -> None: ...
    @property
    def source_namespace_id(self) -> str: ...
    @property
    def source_revision(self) -> str: ...
    @property
    def extractor_contract_id(self) -> str: ...

class ResolvedSourceIdentity:
    stable_handle: str
    source_display: str
    source_id: str
    source_namespace_id: str
    source_revision: str
    extractor_contract_id: str
    disposition: str

class SourceResolutionResult:
    assignments: tuple[ResolvedSourceIdentity, ...]

class IdentityResolution:
    resolved_rows: tuple[Mapping[str, str], ...]
    next_manifest: IdentityManifest
    retained_ledger_rows: tuple[Mapping[str, str], ...]
    replaced_source_ids: tuple[str, ...]
    reset_transaction_ids: tuple[str, ...]

def logical_locator(path: Path, workspace_root: Path) -> tuple[str, str]: ...
def logical_locator_from_resolved(
    resolved_path: Path, workspace_root: Path
) -> tuple[str, str]: ...
def source_namespace_id(locator_kind: str, locator: str) -> str: ...
def source_revision(source_bytes: bytes) -> str: ...
def source_id(namespace_id: str) -> str: ...
def extractor_contract_id(adapter_tag: int, profile: Mapping[str, object]) -> str: ...
def record_fingerprint(row: IdentityRow) -> str: ...
def workspace_record_fingerprint(
    row: IdentityRow, *, evidence_key: bytes | None
) -> str: ...
def workspace_source_revision(revision: str, *, evidence_key: bytes) -> str: ...
def workspace_source_identity(
    source: IncomingSourceIdentity, *, evidence_key: bytes
) -> IncomingSourceIdentity: ...
def resolve_sources(
    manifest: Mapping[str, object],
    legacy_rows: Sequence[Mapping[str, object]],
    incoming_sources: Sequence[IncomingSourceIdentity],
    intent: str,
) -> SourceResolutionResult: ...
def normalized_decimal(value: object) -> str: ...
def normalized_record_identity(row: IdentityRow) -> dict[str, str]: ...
def has_stable_v2_identity(row: IdentityRow) -> bool: ...
def ambiguous_legacy_transaction_ids(rows: Sequence[IdentityRow]) -> frozenset[str]: ...
def empty_manifest() -> IdentityManifest: ...
def manifest_document(manifest: Mapping[str, object]) -> str: ...
def parse_manifest(document: str | bytes) -> IdentityManifest: ...
def validate_ledger_manifest_agreement(
    ledger_rows: Sequence[Mapping[str, str]],
    manifest: IdentityManifest,
    *,
    evidence_key: bytes | None = None,
) -> None: ...
def resolve_batch(
    *,
    ledger_rows: Sequence[Mapping[str, str]],
    manifest: Mapping[str, object],
    sources: Sequence[IncomingSourceIdentity],
    intent: str,
    allow_unmatched_reallocation: bool = False,
    allow_parser_upgrade_reallocation: bool = False,
    evidence_key: bytes | None = None,
) -> IdentityResolution: ...
