#!/usr/bin/env python3
"""Verify built release distributions and their offline installed CLI flow."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile
from email.parser import Parser
from importlib import metadata
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet

EXPECTED_PDF_RANGES = {
    "pdfplumber": ">=0.11.10,<0.12",
    "pymupdf": ">=1.28,<1.29",
}
EXPECTED_BASE_RANGES = {
    "certifi": ">=2026.7.22,<2027",
}
EXPECTED_BUNDLED_PROFILES = frozenset(
    {
        "hsbc_hk_credit_card_pdf.json",
        "hsbc_one_pdf.json",
        "mox_bank_pdf.json",
        "mox_credit_card.json",
        "mox_credit_card_pdf.json",
    }
)
EXPECTED_CONSOLE_SCRIPT = "honeymoney.cli:run"
EXPECTED_RELEASE_VERSION = "0.2.0"
EXPECTED_RELEASE_PYTHON_VERSION = "3.14.6"
EXPECTED_REQUIRES_PYTHON = ">=3.14,<3.15"
EXPECTED_JSON_SCHEMA_VERSION = 3
EXPECTED_EMPTY_WORKSPACE_ENTRIES = (
    ".honeymoney",
    "config.json",
    "corrections.csv",
    "profile_mappings.json",
    "profiles",
    "rates.json",
    "rules.json",
)


def _metadata_requirements(text: str) -> list[Requirement]:
    metadata = Parser().parsestr(text)
    return [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]


def _assert_release_metadata(text: str, artifact: Path) -> None:
    metadata = Parser().parsestr(text)
    actual_version = metadata.get("Version", "")
    if actual_version != EXPECTED_RELEASE_VERSION:
        raise ValueError(
            f"{artifact.name} release version is {actual_version!r}, "
            f"expected {EXPECTED_RELEASE_VERSION!r}"
        )
    actual_python = metadata.get("Requires-Python", "")
    try:
        matches_release_range = SpecifierSet(actual_python) == SpecifierSet(
            EXPECTED_REQUIRES_PYTHON
        )
    except InvalidSpecifier:
        matches_release_range = False
    if not matches_release_range:
        raise ValueError(
            f"{artifact.name} requires-python is {actual_python!r}, "
            f"expected {EXPECTED_REQUIRES_PYTHON!r}"
        )


def _assert_pdf_metadata(text: str, artifact: Path) -> None:
    requirements = _metadata_requirements(text)
    pdf_requirements = {
        requirement.name.casefold(): requirement
        for requirement in requirements
        if requirement.marker is not None
        and requirement.marker.evaluate({"extra": "pdf"})
    }
    if set(pdf_requirements) != set(EXPECTED_PDF_RANGES):
        raise ValueError(
            f"{artifact.name} PDF extra mismatch: {sorted(pdf_requirements)}"
        )
    for name, expected_range in EXPECTED_PDF_RANGES.items():
        actual_range = str(pdf_requirements[name].specifier)
        expected = str(Requirement(f"{name}{expected_range}").specifier)
        if actual_range != expected:
            raise ValueError(
                f"{artifact.name} {name} range is {actual_range!r}, expected {expected!r}"
            )
        if "==" in actual_range:
            raise ValueError(f"{artifact.name} unexpectedly hard-pins {name}")


def _assert_base_metadata(text: str, artifact: Path) -> None:
    requirements = _metadata_requirements(text)
    base_requirements = {
        requirement.name.casefold(): requirement
        for requirement in requirements
        if requirement.marker is None
    }
    if set(base_requirements) != set(EXPECTED_BASE_RANGES):
        raise ValueError(
            f"{artifact.name} base dependency mismatch: {sorted(base_requirements)}"
        )
    for name, expected_range in EXPECTED_BASE_RANGES.items():
        actual_range = str(base_requirements[name].specifier)
        expected = str(Requirement(f"{name}{expected_range}").specifier)
        if actual_range != expected:
            raise ValueError(
                f"{artifact.name} {name} range is {actual_range!r}, expected {expected!r}"
            )
        if "==" in actual_range:
            raise ValueError(f"{artifact.name} unexpectedly hard-pins {name}")


def _wheel_metadata(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any("constraints/" in name for name in names):
            raise ValueError(f"{path.name} contains development constraints")
        metadata_paths = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError(f"{path.name} has {len(metadata_paths)} METADATA files")
        return archive.read(metadata_paths[0]).decode("utf-8")


def _assert_wheel_payload(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        profile_names = {
            Path(name).name
            for name in names
            if name.startswith("honeymoney/data/profiles/") and name.endswith(".json")
        }
        if profile_names != EXPECTED_BUNDLED_PROFILES:
            raise ValueError(
                f"{path.name} bundled profiles mismatch: {sorted(profile_names)}"
            )
        entry_point_paths = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(entry_point_paths) != 1:
            raise ValueError(
                f"{path.name} has {len(entry_point_paths)} entry point files"
            )
        entry_points = configparser.ConfigParser()
        entry_points.read_string(archive.read(entry_point_paths[0]).decode("utf-8"))
        actual = entry_points.get("console_scripts", "honeymoney", fallback="")
        if actual.strip() != EXPECTED_CONSOLE_SCRIPT:
            raise ValueError(
                f"{path.name} honeymoney console script is {actual!r}, "
                f"expected {EXPECTED_CONSOLE_SCRIPT!r}"
            )


def _completed_command(
    command: list[str],
    *,
    environment: dict[str, str],
    working_directory: Path,
) -> str:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        cwd=working_directory,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()[-2000:]
        raise ValueError(f"Command failed with exit code {result.returncode}: {detail}")
    return result.stdout


def _copy_installed_distribution(name: str, destination_root: Path) -> None:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError as error:
        raise ValueError(
            f"Distribution verification requires installed {name}"
        ) from error
    for package_file in distribution.files or ():
        relative_path = Path(package_file)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            continue
        source = Path(distribution.locate_file(package_file))
        if not source.is_file():
            continue
        destination = destination_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _offline_environment(
    child_python: Path,
    *,
    temporary_path: Path,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    child_site_packages = Path(
        _completed_command(
            [
                str(child_python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            environment=environment,
            working_directory=temporary_path,
        ).strip()
    )
    dependency_root = temporary_path / "runtime-dependencies"
    dependency_root.mkdir()
    for name in EXPECTED_BASE_RANGES:
        _copy_installed_distribution(name, dependency_root)
    (child_site_packages / "honeymoney-smoke-dependencies.pth").write_text(
        f"{dependency_root}\n",
        encoding="utf-8",
    )
    for name in ("packaging", "setuptools", "wheel"):
        _copy_installed_distribution(name, child_site_packages)
    return environment


def _successful_json_command(
    command: list[str],
    *,
    environment: dict[str, str],
    working_directory: Path,
    label: str,
) -> dict[str, object]:
    stdout = _completed_command(
        command,
        environment=environment,
        working_directory=working_directory,
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} did not return JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} returned a JSON value other than an object")
    if payload.get("schema_version") != EXPECTED_JSON_SCHEMA_VERSION:
        raise ValueError(
            f"{label} schema version is {payload.get('schema_version')!r}, "
            f"expected {EXPECTED_JSON_SCHEMA_VERSION}"
        )
    if payload.get("status") != "success":
        raise ValueError(f"{label} did not report success")
    return payload


def _successful_command_data(
    payload: dict[str, object],
    *,
    label: str,
) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{label} did not return a data object")
    return data


def _assert_empty_workspace(workspace: Path) -> None:
    entries = tuple(sorted(path.name for path in workspace.iterdir()))
    if entries != EXPECTED_EMPTY_WORKSPACE_ENTRIES:
        raise ValueError(
            f"Installed distribution created workspace entries {entries!r}, "
            f"expected {EXPECTED_EMPTY_WORKSPACE_ENTRIES!r}"
        )
    internal = workspace / ".honeymoney"
    internal_entries = tuple(sorted(path.name for path in internal.iterdir()))
    if internal_entries != ("import-records", "workspace-index.json"):
        raise ValueError(
            f"Installed distribution created internal entries {internal_entries!r}"
        )
    if os.name == "nt":
        return
    for path, expected_mode in (
        (workspace, 0o700),
        (internal, 0o700),
        (workspace / "config.json", 0o600),
    ):
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        if actual_mode != expected_mode:
            raise ValueError(
                f"Installed distribution set {path.name} mode {actual_mode:o}, "
                f"expected {expected_mode:o}"
            )


def _smoke_installed_cli(
    command_path: Path,
    child_python: Path,
    environment_path: Path,
    *,
    temporary_path: Path,
    environment: dict[str, str],
) -> None:
    installed_package = Path(
        _completed_command(
            [
                str(child_python),
                "-c",
                "import honeymoney; print(honeymoney.__file__)",
            ],
            environment=environment,
            working_directory=temporary_path,
        ).strip()
    ).resolve()
    if not installed_package.is_relative_to(environment_path.resolve()):
        raise ValueError(
            "Installed distribution loaded honeymoney outside its environment: "
            f"{installed_package}"
        )

    workspace = temporary_path / "workspace"
    _successful_json_command(
        [str(command_path), "setup", "--root", str(workspace), "--json"],
        environment=environment,
        working_directory=temporary_path,
        label="Installed distribution setup",
    )
    _assert_empty_workspace(workspace)

    source = temporary_path / "synthetic.csv"
    source.write_text(
        "Date,Description,Amount,Currency\n2026-08-08,Synthetic Grocer,-12.00,HKD\n",
        encoding="utf-8",
    )
    imported = _successful_json_command(
        [
            str(command_path),
            "import",
            str(source),
            "--no-interactive",
            "--json",
        ],
        environment=environment,
        working_directory=workspace,
        label="Installed distribution import",
    )
    import_data = _successful_command_data(
        imported,
        label="Installed distribution import",
    )
    if import_data.get("import_count") != 1:
        raise ValueError("Installed distribution import did not report one import")
    if import_data.get("statement_transaction_count") != 1:
        raise ValueError(
            "Installed distribution import did not report one statement transaction"
        )
    if import_data.get("view_transaction_count") != 1:
        raise ValueError(
            "Installed distribution import did not report one view transaction"
        )
    records = sorted(
        path
        for path in (workspace / ".honeymoney" / "import-records").iterdir()
        if path.is_dir()
    )
    if len(records) != 1:
        raise ValueError(
            f"Installed distribution import created {len(records)} import records"
        )

    listed = _successful_json_command(
        [str(command_path), "imports", "list", "--json"],
        environment=environment,
        working_directory=workspace,
        label="Installed distribution imports list",
    )
    list_data = _successful_command_data(
        listed,
        label="Installed distribution imports list",
    )
    if list_data.get("import_count") != 1:
        raise ValueError(
            "Installed distribution imports list did not report one import"
        )
    _successful_json_command(
        [str(command_path), "imports", "show", records[0].name, "--json"],
        environment=environment,
        working_directory=workspace,
        label="Installed distribution imports show",
    )
    _successful_json_command(
        [
            str(command_path),
            "views",
            "rebuild",
            "--month",
            "2026-08",
            "--json",
        ],
        environment=environment,
        working_directory=workspace,
        label="Installed distribution views rebuild",
    )
    view_entries = tuple(
        sorted(path.name for path in (workspace / "views" / "2026-08").iterdir())
    )
    if view_entries != ("report.html", "review_needed.csv", "transactions.csv"):
        raise ValueError(
            f"Installed distribution created view entries {view_entries!r}"
        )
    _successful_json_command(
        [str(command_path), "status", "--month", "2026-08", "--json"],
        environment=environment,
        working_directory=workspace,
        label="Installed distribution status",
    )
    _successful_json_command(
        [str(command_path), "doctor", "--json"],
        environment=environment,
        working_directory=workspace,
        label="Installed distribution doctor",
    )


def _install_and_smoke_distribution(path: Path, *, source_archive: bool) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        environment_path = temporary_path / "venv"
        venv.EnvBuilder(with_pip=True).create(environment_path)
        scripts_path = environment_path / ("Scripts" if os.name == "nt" else "bin")
        python_name = "python.exe" if os.name == "nt" else "python"
        command_name = "honeymoney.exe" if os.name == "nt" else "honeymoney"
        child_python = scripts_path / python_name
        environment = _offline_environment(
            child_python,
            temporary_path=temporary_path,
        )
        actual_version = _completed_command(
            [
                str(child_python),
                "-c",
                "import sys; print('.'.join(map(str, sys.version_info[:3])))",
            ],
            environment=environment,
            working_directory=temporary_path,
        ).strip()
        if actual_version != EXPECTED_RELEASE_PYTHON_VERSION:
            raise ValueError(
                "Distribution smoke used Python "
                f"{actual_version}, expected {EXPECTED_RELEASE_PYTHON_VERSION}"
            )
        install_command = [
            str(child_python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--no-deps",
            "--no-index",
        ]
        if source_archive:
            install_command.append("--no-build-isolation")
        install_command.append(str(path.resolve()))
        _completed_command(
            install_command,
            environment=environment,
            working_directory=temporary_path,
        )
        _smoke_installed_cli(
            scripts_path / command_name,
            child_python,
            environment_path,
            temporary_path=temporary_path,
            environment=environment,
        )


def _sdist_metadata(path: Path) -> str:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        if any("/constraints/" in name for name in names):
            raise ValueError(f"{path.name} contains development constraints")
        metadata_paths = [
            name
            for name in names
            if name.endswith("/PKG-INFO") and len(Path(name).parts) == 2
        ]
        if len(metadata_paths) != 1:
            raise ValueError(f"{path.name} has {len(metadata_paths)} PKG-INFO files")
        metadata_file = archive.extractfile(metadata_paths[0])
        if metadata_file is None:
            raise ValueError(f"Could not read metadata from {path.name}")
        return metadata_file.read().decode("utf-8")


def _assert_sdist_payload(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
        pyproject_paths = [
            name
            for name in names
            if name.endswith("/pyproject.toml") and len(PurePosixPath(name).parts) == 2
        ]
        if len(pyproject_paths) != 1:
            raise ValueError(
                f"{path.name} has {len(pyproject_paths)} top-level pyproject files"
            )
        pyproject_path = pyproject_paths[0]
        source_root = PurePosixPath(pyproject_path).parts[0]
        profile_prefix = f"{source_root}/honeymoney/data/profiles/"
        profile_names = {
            PurePosixPath(name).name
            for name in names
            if name.startswith(profile_prefix) and name.endswith(".json")
        }
        if profile_names != EXPECTED_BUNDLED_PROFILES:
            raise ValueError(
                f"{path.name} bundled profiles mismatch: {sorted(profile_names)}"
            )
        if f"{source_root}/honeymoney/cli.py" not in names:
            raise ValueError(f"{path.name} does not contain honeymoney/cli.py")
        pyproject_file = archive.extractfile(pyproject_path)
        if pyproject_file is None:
            raise ValueError(f"Could not read pyproject.toml from {path.name}")
        try:
            document = tomllib.loads(pyproject_file.read().decode("utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"{path.name} has invalid pyproject.toml") from error
        project = document.get("project")
        scripts = project.get("scripts") if isinstance(project, dict) else None
        actual = scripts.get("honeymoney") if isinstance(scripts, dict) else None
        if actual != EXPECTED_CONSOLE_SCRIPT:
            raise ValueError(
                f"{path.name} honeymoney console script is {actual!r}, "
                f"expected {EXPECTED_CONSOLE_SCRIPT!r}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", default="dist", type=Path)
    args = parser.parse_args()

    wheels = sorted(args.dist.glob("*.whl"))
    sdists = sorted(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"Expected one wheel and one sdist in {args.dist}, "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )

    wheel_metadata = _wheel_metadata(wheels[0])
    sdist_metadata = _sdist_metadata(sdists[0])
    _assert_release_metadata(wheel_metadata, wheels[0])
    _assert_release_metadata(sdist_metadata, sdists[0])
    _assert_base_metadata(wheel_metadata, wheels[0])
    _assert_base_metadata(sdist_metadata, sdists[0])
    _assert_pdf_metadata(wheel_metadata, wheels[0])
    _assert_pdf_metadata(sdist_metadata, sdists[0])
    _assert_wheel_payload(wheels[0])
    _assert_sdist_payload(sdists[0])
    _install_and_smoke_distribution(wheels[0], source_archive=False)
    _install_and_smoke_distribution(sdists[0], source_archive=True)
    print(
        "Distribution verified: release metadata, wheel and source payloads, "
        "offline installed CLI flows, and no shipped constraints"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
