#!/usr/bin/env python3
"""Verify that built distributions expose only the intended public metadata."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement

EXPECTED_PDF_RANGES = {
    "pdfplumber": ">=0.11.10,<0.12",
    "pymupdf": ">=1.28,<1.29",
}


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

    _assert_pdf_metadata(_wheel_metadata(wheels[0]), wheels[0])
    _assert_pdf_metadata(_sdist_metadata(sdists[0]), sdists[0])
    print("Distribution metadata verified: bounded PDF extras, no constraints shipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
