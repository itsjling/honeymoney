#!/usr/bin/env python3
"""Verify that built distributions expose only the intended public metadata."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import venv
import zipfile
from email.parser import Parser
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

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


def _metadata_requirements(text: str) -> list[Requirement]:
    metadata = Parser().parsestr(text)
    return [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]


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


def _install_and_smoke_wheel(path: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        environment_path = temporary_path / "venv"
        venv.EnvBuilder(with_pip=True).create(environment_path)
        scripts_path = environment_path / ("Scripts" if os.name == "nt" else "bin")
        python_name = "python.exe" if os.name == "nt" else "python"
        command_name = "honeymoney.exe" if os.name == "nt" else "honeymoney"
        environment = os.environ.copy()
        environment.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INDEX": "1",
            }
        )
        child_site_packages = Path(
            _completed_command(
                [
                    str(scripts_path / python_name),
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
            distribution = metadata.distribution(name)
            for package_file in distribution.files or ():
                relative_path = Path(package_file)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    continue
                source = Path(distribution.locate_file(package_file))
                if not source.is_file():
                    continue
                destination = dependency_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        (child_site_packages / "honeymoney-smoke-dependencies.pth").write_text(
            f"{dependency_root}\n",
            encoding="utf-8",
        )
        _completed_command(
            [
                str(scripts_path / python_name),
                "-m",
                "pip",
                "install",
                "--no-deps",
                str(path.resolve()),
            ],
            environment=environment,
            working_directory=temporary_path,
        )
        installed_package = Path(
            _completed_command(
                [
                    str(scripts_path / python_name),
                    "-c",
                    "import honeymoney; print(honeymoney.__file__)",
                ],
                environment=environment,
                working_directory=temporary_path,
            ).strip()
        ).resolve()
        if not installed_package.is_relative_to(environment_path.resolve()):
            raise ValueError(
                f"Wheel smoke loaded honeymoney outside its environment: "
                f"{installed_package}"
            )
        workspace = temporary_path / "workspace"
        stdout = _completed_command(
            [
                str(scripts_path / command_name),
                "setup",
                "--root",
                str(workspace),
                "--json",
            ],
            environment=environment,
            working_directory=temporary_path,
        )
        try:
            json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ValueError("Installed wheel setup did not return JSON") from error
        if not (workspace / "config.json").is_file():
            raise ValueError("Installed wheel setup did not create config.json")
        installed_profiles = {
            path.name for path in (workspace / "profiles").glob("*.json")
        }
        if not EXPECTED_BUNDLED_PROFILES.issubset(installed_profiles):
            raise ValueError(
                "Installed wheel setup did not publish all bundled profiles"
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
    _assert_base_metadata(wheel_metadata, wheels[0])
    _assert_base_metadata(sdist_metadata, sdists[0])
    _assert_pdf_metadata(wheel_metadata, wheels[0])
    _assert_pdf_metadata(sdist_metadata, sdists[0])
    _assert_wheel_payload(wheels[0])
    _install_and_smoke_wheel(wheels[0])
    print(
        "Distribution verified: metadata, wheel payload, installed entry point, "
        "bundled profiles, and no shipped constraints"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
