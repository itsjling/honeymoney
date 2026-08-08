"""Fixed clean-start workspace paths and path safety checks."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

INTERNAL_DIRECTORY = ".honeymoney"
WORKSPACE_INDEX_NAME = "workspace-index.json"
IMPORT_RECORDS_DIRECTORY = "import-records"
VIEWS_DIRECTORY = "views"
REPORT_PREVIEW_NAME = "report-preview.html"

_SYSTEM_ROOT_ALIASES = {
    "var": Path("/private/var"),
    "tmp": Path("/private/tmp"),
    "etc": Path("/private/etc"),
}

_LEGACY_ROOT_MARKERS = (
    "categorized.csv",
    ".honeymoney-identity-manifest.json",
    ".honeymoney-overlap-manifest.json",
    ".honeymoney-source-occurrences.csv",
)
_LEGACY_OUTPUT_MARKERS = (
    "categorized.csv",
    "review_needed.csv",
    "import_report.json",
    ".honeymoney-identity-manifest.json",
    ".honeymoney-overlap-manifest.json",
    ".honeymoney-source-occurrences.csv",
    ".categorized.csv.honeymoney-state.json",
    ".categorized.csv.honeymoney-lock",
)


class WorkspacePathError(ValueError):
    """A stable workspace-path validation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def existing_path_components(path: Path) -> tuple[Path, ...]:
    """Return existing lexical components after proven system-alias handling."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = _normalize_system_root_alias(candidate)
    current = Path(candidate.anchor)
    components: list[Path] = []
    for part in candidate.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError, NotADirectoryError:
            break
        if current.is_symlink():
            components.append(current)
            break
        components.append(current)
    return tuple(components)


def _normalize_system_root_alias(path: Path) -> Path:
    """Replace a verified macOS root alias with its fixed real target."""
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.anchor != "/":
        return candidate
    parts = candidate.parts
    if len(parts) < 2:
        return candidate
    expected = _SYSTEM_ROOT_ALIASES.get(parts[1])
    alias = Path("/") / parts[1]
    if expected is None or not _matches_system_root_alias(alias, expected):
        return candidate
    return expected.joinpath(*parts[2:])


def _matches_system_root_alias(alias: Path, expected: Path) -> bool:
    try:
        return alias.is_symlink() and alias.resolve(strict=True) == expected
    except OSError:
        return False


def reject_existing_symlink_components(path: Path) -> None:
    """Reject a path whose existing lexical components include a link."""
    try:
        unsafe = next(
            (
                component
                for component in existing_path_components(path)
                if component.is_symlink()
            ),
            None,
        )
    except OSError as error:
        raise WorkspacePathError(
            "workspace_input_invalid", "Workspace path is not accessible."
        ) from error
    if unsafe is not None:
        raise WorkspacePathError(
            "managed_path_unsafe", "Workspace path must not contain symbolic links."
        )


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    config: Path
    internal: Path
    workspace_index: Path
    import_records: Path
    views: Path
    report_preview: Path
    profiles: Path
    corrections: Path
    rules: Path
    rates: Path
    profile_mappings: Path
    lock: Path
    journal: Path

    @classmethod
    def from_config(cls, config_path: Path) -> WorkspacePaths:
        expanded_config = config_path.expanduser()
        reject_existing_symlink_components(expanded_config)
        resolved_config = expanded_config.resolve(strict=True)
        return cls.from_root(resolved_config.parent, config=resolved_config)

    @classmethod
    def from_root(cls, root: Path, *, config: Path | None = None) -> WorkspacePaths:
        expanded_root = root.expanduser()
        reject_existing_symlink_components(expanded_root)
        resolved_root = expanded_root.resolve()
        internal = resolved_root / INTERNAL_DIRECTORY
        return cls(
            root=resolved_root,
            config=config or resolved_root / "config.json",
            internal=internal,
            workspace_index=internal / WORKSPACE_INDEX_NAME,
            import_records=internal / IMPORT_RECORDS_DIRECTORY,
            views=resolved_root / VIEWS_DIRECTORY,
            report_preview=internal / REPORT_PREVIEW_NAME,
            profiles=resolved_root / "profiles",
            corrections=resolved_root / "corrections.csv",
            rules=resolved_root / "rules.json",
            rates=resolved_root / "rates.json",
            profile_mappings=resolved_root / "profile_mappings.json",
            lock=internal / "workspace.lock",
            journal=internal / "publication-journal.json",
        )

    def relative(self, path: Path) -> str:
        candidate = Path(path).expanduser()
        reject_existing_symlink_components(candidate)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as error:
            raise WorkspacePathError(
                "workspace_input_invalid", "Workspace path is not accessible."
            ) from error
        if not resolved.is_relative_to(self.root):
            raise WorkspacePathError(
                "managed_path_unsafe", "Managed path leaves the workspace root"
            )
        return resolved.relative_to(self.root).as_posix()


def reject_legacy_workspace(
    paths: WorkspacePaths, config: Mapping[str, object] | None = None
) -> None:
    """Reject 0.1 storage without changing it."""
    if config is not None and "paths" in config:
        raise WorkspacePathError(
            "legacy_workspace_reset_required",
            "This is a legacy Honeymoney workspace; preserve it and create a fresh "
            "0.2.0 workspace.",
        )
    if any(
        path.is_symlink() or path.exists() for path in legacy_workspace_markers(paths)
    ):
        raise WorkspacePathError(
            "legacy_workspace_reset_required",
            "This is a legacy Honeymoney workspace; preserve it and create a fresh "
            "0.2.0 workspace.",
        )


def legacy_workspace_markers(paths: WorkspacePaths) -> tuple[Path, ...]:
    """Return old state markers that must block clean-start setup."""
    output = paths.root / "output"
    return (
        *(paths.root / name for name in _LEGACY_ROOT_MARKERS),
        *(output / name for name in _LEGACY_OUTPUT_MARKERS),
    )


def checked_workspace_path(
    paths: WorkspacePaths,
    value: str,
    *,
    must_exist: bool = True,
    require_regular_file: bool = False,
) -> Path:
    """Resolve a configured workspace path and reject escapes and symbolic links."""
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else paths.root / raw
    reject_existing_symlink_components(candidate)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise WorkspacePathError(
            "workspace_input_invalid", "Workspace input is unavailable."
        ) from error
    if not resolved.is_relative_to(paths.root):
        raise WorkspacePathError(
            "managed_path_unsafe", "Workspace input leaves the workspace root."
        )
    if require_regular_file:
        try:
            mode = resolved.stat().st_mode
            if not stat.S_ISREG(mode):
                raise WorkspacePathError(
                    "workspace_input_invalid", "Workspace input is not a regular file."
                )
            with resolved.open("rb"):
                pass
        except WorkspacePathError:
            raise
        except OSError as error:
            raise WorkspacePathError(
                "workspace_input_invalid", "Workspace input is unavailable."
            ) from error
    return resolved
