"""Filesystem boundary for authoritative identity state."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from honeymoney.csv_artifacts import read_csv_artifact
from honeymoney.identity import (
    ID_FIELDS,
    IDENTITY_MANIFEST_NAME,
    IdentityError,
    empty_manifest,
    manifest_document,
    parse_manifest,
    validate_ledger_manifest_agreement,
)
from honeymoney.persistence import recover_generation
from honeymoney.schema import CATEGORIZED_COLUMNS

LEGACY_CATEGORIZED_COLUMNS = [
    column for column in CATEGORIZED_COLUMNS if column not in ID_FIELDS
]


@dataclass(frozen=True)
class IdentityState:
    """Validated ledger rows plus the exact canonical manifest document."""

    rows: list[dict[str, str]]
    manifest: dict[str, object]
    manifest_document: str
    bootstrap_required: bool = False


def identity_manifest_path(categorized_path: Path) -> Path:
    """Return the manifest's fixed sibling path."""
    return Path(categorized_path).parent / IDENTITY_MANIFEST_NAME


def load_identity_state(categorized_path: Path) -> IdentityState:
    """Recover and validate identity state without reconstructing v2 ownership."""
    categorized_path = Path(categorized_path)
    recover_generation(categorized_path)
    manifest_path = identity_manifest_path(categorized_path)
    ledger_exists = categorized_path.exists()
    manifest_exists = manifest_path.exists()

    if not ledger_exists:
        if manifest_exists:
            raise IdentityError("identity_manifest_invalid")
        manifest = empty_manifest()
        return IdentityState([], manifest, manifest_document(manifest))

    header = _ledger_header(categorized_path)
    rows = read_csv_artifact(categorized_path, CATEGORIZED_COLUMNS).rows
    if not manifest_exists:
        if header == LEGACY_CATEGORIZED_COLUMNS:
            for row in rows:
                for field in ID_FIELDS:
                    row[field] = ""
            manifest = empty_manifest()
            return IdentityState(
                rows,
                manifest,
                manifest_document(manifest),
                bootstrap_required=True,
            )
        if header == CATEGORIZED_COLUMNS or any(field in header for field in ID_FIELDS):
            raise IdentityError("identity_manifest_missing")
        raise IdentityError("identity_manifest_invalid")

    try:
        document = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise IdentityError("identity_manifest_invalid") from error
    manifest = parse_manifest(document)
    validate_ledger_manifest_agreement(rows, manifest)
    return IdentityState(rows, manifest, document)


def validated_manifest_document(
    ledger_rows: list[Mapping[str, str]], manifest: Mapping[str, object]
) -> str:
    """Validate an output ledger and return its canonical manifest document."""
    validate_ledger_manifest_agreement(ledger_rows, manifest)
    return manifest_document(manifest)


def _ledger_header(path: Path) -> list[str]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return next(csv.reader(handle), [])
    except (OSError, UnicodeError, csv.Error) as error:
        raise IdentityError("identity_manifest_invalid") from error
