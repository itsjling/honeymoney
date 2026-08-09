"""Idempotent clean-start workspace setup."""

from __future__ import annotations

import hashlib
import json
import secrets
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Mapping

from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.rates import empty_rate_cache, rate_cache_document
from honeymoney.workspace_index import (
    WorkspaceIndex,
    WorkspaceIndexError,
    empty_workspace_index,
    load_compatible_workspace_index,
    workspace_index_document,
)
from honeymoney.workspace_paths import (
    WorkspacePathError,
    WorkspacePaths,
    reject_existing_symlink_components,
    reject_legacy_workspace,
)
from honeymoney.workspace_publication import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    PublicationDirectory,
    PublicationError,
    PublicationTarget,
    WorkspaceBusyError,
    WorkspaceLock,
    publish_generation,
)


@dataclass
class _SetupPlan:
    paths: WorkspacePaths
    planned: dict[Path, str]
    index: WorkspaceIndex
    has_existing_index: bool


def setup_workspace(root: Path) -> WorkspacePaths:
    """Create or prove the exact starter workspace without replacing bytes."""
    plan = _build_setup_plan(root)
    if _setup_is_current(plan.paths, plan.planned):
        return plan.paths
    before_lock = _planned_file_state(plan.planned)
    with WorkspaceLock(plan.paths.root):
        if _planned_file_state(plan.planned) != before_lock:
            return _settle_changed_setup_plan(plan)
        if _setup_is_current(plan.paths, plan.planned):
            return plan.paths
        _publish_setup_plan(plan)
    return plan.paths


def _build_setup_plan(root: Path) -> _SetupPlan:
    paths = WorkspacePaths.from_root(root)
    for path in _managed_setup_paths(paths):
        reject_existing_symlink_components(path)
    if paths.journal.exists():
        raise PublicationError("publication recovery is required")
    _validate_setup_directories(paths)
    existing = _read_existing_config(paths.config)
    reject_legacy_workspace(paths, existing)
    profiles = _installed_profile_documents(paths)
    config: dict[str, object] = {
        "base_currency": "HKD",
        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
        "review_confidence_threshold": 0.8,
        "reconciliation": {"date_window_days": 3},
        "categorization_memory": {"enabled": False},
        "profiles": [path.relative_to(paths.root).as_posix() for path in profiles],
        "profile_mappings": "profile_mappings.json",
        "rules": "rules.json",
        "corrections": "corrections.csv",
        "rate_cache": "rates.json",
        "pdf": {"enabled": True, "parser": "pdfplumber"},
        "ollama": {
            "enabled": False,
            "url": "http://localhost:11434/api/generate",
            "model": "qwen2.5:7b-instruct",
            "batch_size": 5,
            "timeout_seconds": 120,
        },
    }
    planned = {
        **profiles,
        paths.profile_mappings: json.dumps(
            {"account_bindings": [], "filename_patterns": []},
            indent=2,
            sort_keys=True,
        ),
        paths.rules: json.dumps(_starter_rules(), indent=2, sort_keys=True),
        paths.corrections: ",".join(CORRECTION_COLUMNS) + "\n",
        paths.rates: rate_cache_document(empty_rate_cache()),
        paths.config: json.dumps(config, indent=2, sort_keys=True),
    }
    index, has_existing_index = _planned_workspace_index(paths)
    planned[paths.workspace_index] = workspace_index_document(index)
    for path in planned:
        reject_existing_symlink_components(path)
    for path, content in planned.items():
        if path.exists() and _read_text(path) != content:
            raise WorkspacePathError(
                "workspace_input_invalid",
                f"Refusing to replace existing workspace file: {path.name}",
            )
    return _SetupPlan(paths, planned, index, has_existing_index)


def _settle_changed_setup_plan(plan: _SetupPlan) -> WorkspacePaths:
    try:
        current = _build_setup_plan(plan.paths.root)
    except WorkspacePathError as error:
        raise WorkspaceBusyError("workspace changed; retry setup") from error
    if _setup_is_current(current.paths, current.planned):
        return current.paths
    raise WorkspaceBusyError("workspace changed; retry setup")


def _publish_setup_plan(plan: _SetupPlan) -> None:
    paths = plan.paths
    if plan.has_existing_index:
        plan.index["generation_id"] = f"gen_{secrets.token_hex(32)}"
        plan.planned[paths.workspace_index] = workspace_index_document(plan.index)
    generation_id = plan.index["generation_id"]
    targets = [
        PublicationTarget(paths.relative(path), content.encode("utf-8"))
        for path, content in plan.planned.items()
        if path != paths.workspace_index
    ]
    publish_generation(
        paths.root,
        generation_id,
        targets,
        plan.planned[paths.workspace_index].encode("utf-8"),
        directory_targets=[
            PublicationDirectory("."),
            PublicationDirectory(paths.relative(paths.internal)),
            PublicationDirectory(paths.relative(paths.profiles)),
            PublicationDirectory(paths.relative(paths.import_records)),
        ],
    )


def _managed_setup_paths(paths: WorkspacePaths) -> tuple[Path, ...]:
    return (
        paths.config,
        paths.internal,
        paths.workspace_index,
        paths.import_records,
        paths.report_preview,
        paths.views,
        paths.profiles,
        paths.corrections,
        paths.rules,
        paths.rates,
        paths.profile_mappings,
        paths.lock,
        paths.journal,
    )


def _validate_setup_directories(paths: WorkspacePaths) -> None:
    for directory in (
        paths.root,
        paths.internal,
        paths.import_records,
        paths.profiles,
    ):
        if directory.exists() and not directory.is_dir():
            raise WorkspacePathError(
                "workspace_input_invalid",
                f"Workspace directory is not usable: {directory.name}",
            )


def _planned_workspace_index(paths: WorkspacePaths) -> tuple[WorkspaceIndex, bool]:
    if not paths.workspace_index.exists():
        return empty_workspace_index(), False
    else:
        try:
            index = load_compatible_workspace_index(paths.workspace_index)
        except WorkspaceIndexError as error:
            code = (
                "newer_honeymoney_required"
                if error.code == "newer_honeymoney_required"
                else "workspace_input_invalid"
            )
            raise WorkspacePathError(
                code, "Workspace index is not a supported canonical document."
            ) from error
    return index, True


def _planned_file_state(
    planned: Mapping[Path, str],
) -> tuple[tuple[Path, str, int | None, str | None], ...]:
    states: list[tuple[Path, str, int | None, str | None]] = []
    for path in planned:
        kind, mode, digest = _managed_file_state(path)
        states.append((path, kind, mode, digest))
    return tuple(states)


def _managed_file_state(path: Path) -> tuple[str, int | None, str | None]:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return "missing", None, None
    except OSError:
        return "unavailable", None, None
    mode = stat.S_IMODE(details.st_mode)
    if stat.S_ISLNK(details.st_mode):
        return "symlink", mode, None
    if not stat.S_ISREG(details.st_mode):
        return "other", mode, None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError:
        return "unavailable", None, None
    return "file", mode, digest.hexdigest()


def _setup_is_current(paths: WorkspacePaths, planned: Mapping[Path, str]) -> bool:
    directories = (
        paths.root,
        paths.internal,
        paths.import_records,
        paths.profiles,
    )
    return all(
        _has_mode(directory, PRIVATE_DIRECTORY_MODE, directory=True)
        for directory in directories
    ) and all(_has_mode(path, PRIVATE_FILE_MODE, directory=False) for path in planned)


def _has_mode(path: Path, expected: int, *, directory: bool) -> bool:
    try:
        is_expected_type = path.is_dir() if directory else path.is_file()
        return is_expected_type and stat.S_IMODE(path.stat().st_mode) == expected
    except OSError:
        return False


def _read_existing_config(path: Path) -> Mapping[str, object] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspacePathError(
            "workspace_input_invalid", "Workspace config is not valid JSON."
        ) from error
    if not isinstance(value, dict):
        raise WorkspacePathError(
            "workspace_input_invalid", "Workspace config must be an object."
        )
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise WorkspacePathError(
            "workspace_input_invalid", f"Managed file is unreadable: {path.name}"
        ) from error


def _installed_profile_documents(paths: WorkspacePaths) -> dict[Path, str]:
    documents = {
        paths.profiles / "starter_csv.json": json.dumps(
            _starter_csv_profile(), indent=2, sort_keys=True
        )
    }
    profile_resources = resources.files("honeymoney").joinpath("data/profiles")
    for resource in sorted(profile_resources.iterdir(), key=lambda item: item.name):
        if resource.name.endswith(".json"):
            documents[paths.profiles / resource.name] = resource.read_text(
                encoding="utf-8"
            )
    return documents


def _starter_csv_profile() -> dict[str, object]:
    return {
        "id": "starter_csv",
        "account_id": "starter_csv",
        "account": "Starter CSV",
        "account_type": "bank",
        "institution": "Local",
        "country": "HK",
        "account_currency": "HKD",
        "owner": "Household",
        "payment_method": "Bank Account",
        "csv": {
            "detect_headers": ["Date", "Description", "Amount", "Currency"],
            "columns": {
                "transaction_date": "Date",
                "description": "Description",
                "amount": "Amount",
                "original_currency": "Currency",
            },
        },
    }


def _starter_rules() -> dict[str, object]:
    return {
        "version": 1,
        "rules": [
            {
                "id": "mox-credit-card-payment",
                "enabled": True,
                "priority": 20,
                "conditions": [
                    {
                        "field": "institution",
                        "match_type": "exact",
                        "patterns": ["Mox"],
                    },
                    {
                        "field": "original_description",
                        "match_type": "regex",
                        "patterns": [
                            "^(?:PAYMENT TO MOX CREDIT CARD|MOX CREDIT CARD PAYMENT)$"
                        ],
                    },
                ],
                "category": "Credit Card Payment",
                "flow_type": "credit_card_payment",
                "owner": "Household",
                "confidence": 0.99,
                "notes": "Institution-specific payment treatment runs before Ollama",
            }
        ],
    }
