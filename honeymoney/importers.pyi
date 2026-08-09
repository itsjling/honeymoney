"""Static contracts for statement import services.

The clean-start workspace service needs these parser seams while the legacy CLI
continues to use the same implementation.  They deliberately expose only
validated parser inputs and normalized output rows.
"""

from pathlib import Path
from typing import Callable, Literal, Mapping, TypeAlias, overload

from honeymoney.account_bindings import AccountBinding
from honeymoney.identity import IncomingSourceIdentity
from honeymoney.parser_contracts import Profile

ProfileMappings: TypeAlias = dict[str, object]
ImportedRows: TypeAlias = list[dict[str, str]]
ImportWarnings: TypeAlias = list[str]
ImportReports: TypeAlias = list[dict[str, str]]
IdentityImportResult: TypeAlias = tuple[
    ImportedRows,
    ImportWarnings,
    ImportReports,
    tuple[IncomingSourceIdentity, ...],
]
PlainImportResult: TypeAlias = tuple[ImportedRows, ImportWarnings, ImportReports]

class InputSourceSnapshot:
    source_bytes: bytes
    resolved_path: Path
    locator_kind: str
    locator: str

    def __init__(
        self,
        source_bytes: bytes,
        resolved_path: Path,
        locator_kind: str,
        locator: str,
    ) -> None: ...

def _load_profiles(config: Mapping[str, object]) -> list[Profile]: ...
def _validate_profile(
    profile: object,
    profile_path: Path,
    config: Mapping[str, object],
) -> Profile: ...
def _load_profile_mappings(config: Mapping[str, object]) -> ProfileMappings: ...
def _discover_input_files(input_path: Path) -> list[Path]: ...
def _capture_input_source(
    input_file: Path, config: Mapping[str, object]
) -> InputSourceSnapshot: ...
def _relative_source(path: Path, input_root: Path) -> str: ...
def _pdf_adapter_tag(profile: Profile) -> int: ...
def _select_pdf_profile(
    pdf_path: Path,
    profiles: list[Profile],
    interactive: bool,
    profile_mappings: ProfileMappings,
    profile_mappings_path: str | None,
    clear_status: Callable[[], None],
) -> Profile: ...
def _select_csv_profile(
    csv_path: Path,
    profiles: list[Profile],
    interactive: bool,
    profile_mappings: ProfileMappings,
    clear_status: Callable[[], None],
    *,
    source_bytes: bytes | None = None,
) -> tuple[Profile, bool]: ...
def _explicit_binding_profile(
    input_file: Path,
    profiles: list[Profile],
    binding: AccountBinding,
) -> Profile: ...
def _candidate_source_ids(
    input_files: list[Path],
    input_root: Path,
    config: Mapping[str, object],
) -> dict[str, str]: ...
def preview_profile_input(
    profile: Mapping[str, object],
    profile_id: str,
    input_path: Path,
    config: Mapping[str, object],
) -> tuple[ImportedRows, ImportWarnings]: ...
@overload
def _import_transactions(
    input_files: list[Path],
    profiles: list[Profile],
    config: Mapping[str, object],
    input_root: Path,
    interactive: bool,
    profile_mappings: ProfileMappings,
    profile_mappings_path: str | None,
    *,
    explicit_binding: AccountBinding | None = None,
    include_identity_sources: Literal[True],
    source_snapshots: Mapping[Path, InputSourceSnapshot] | None = None,
    preselected_profiles: Mapping[Path, Profile] | None = None,
    status: Callable[[str], None] | None = None,
    clear_status: Callable[[], None] | None = None,
) -> IdentityImportResult: ...
@overload
def _import_transactions(
    input_files: list[Path],
    profiles: list[Profile],
    config: Mapping[str, object],
    input_root: Path,
    interactive: bool,
    profile_mappings: ProfileMappings,
    profile_mappings_path: str | None,
    *,
    explicit_binding: AccountBinding | None = None,
    include_identity_sources: Literal[False] = False,
    source_snapshots: Mapping[Path, InputSourceSnapshot] | None = None,
    preselected_profiles: Mapping[Path, Profile] | None = None,
    status: Callable[[str], None] | None = None,
    clear_status: Callable[[], None] | None = None,
) -> PlainImportResult: ...
