"""Filesystem boundary for authoritative identity state."""

from __future__ import annotations

import csv
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, cast

from honeymoney.contracts import Config
from honeymoney.csv_artifacts import read_csv_artifact
from honeymoney.identity import (
    ID_FIELDS,
    IDENTITY_MANIFEST_NAME,
    IdentityError,
    empty_manifest,
    manifest_document,
    parse_manifest,
    record_fingerprint,
    validate_ledger_manifest_agreement,
)
from honeymoney.identity_contracts import (
    IdentityManifest,
    IdentityRecordManifest,
    IdentitySourceManifest,
)
from honeymoney.overlap import (
    canonicalize_overlaps,
    empty_overlap_manifest,
    overlap_manifest_document,
    overlap_manifest_path,
    parse_overlap_manifest,
    source_occurrences_path,
    validate_overlap_agreement,
)
from honeymoney.overlap_contracts import OverlapManifest
from honeymoney.persistence import configured_generation_paths, recover_generation
from honeymoney.schema import CATEGORIZED_COLUMNS, SOURCE_OCCURRENCE_COLUMNS

LEGACY_CATEGORIZED_COLUMNS = [
    column for column in SOURCE_OCCURRENCE_COLUMNS if column not in ID_FIELDS
]


@dataclass(frozen=True)
class IdentityState:
    """Validated canonical ledger, source evidence, and hidden manifests."""

    rows: list[dict[str, str]]
    manifest: IdentityManifest
    manifest_document: str
    bootstrap_required: bool = False
    source_rows: list[dict[str, str]] | None = None
    source_evidence_rows: list[dict[str, str]] | None = None
    overlap_manifest: OverlapManifest | None = None
    overlap_manifest_document: str = ""
    canonical_migration_required: bool = False
    overlap_migration_required: bool = False

    def __post_init__(self) -> None:
        if self.source_rows is None:
            object.__setattr__(self, "source_rows", self.rows)
        if self.source_evidence_rows is None:
            object.__setattr__(self, "source_evidence_rows", self.source_rows)
        overlap = self.overlap_manifest
        if overlap is None:
            overlap = empty_overlap_manifest("ovns_" + secrets.token_hex(32))
            object.__setattr__(
                self,
                "overlap_manifest",
                overlap,
            )
        if not self.overlap_manifest_document:
            object.__setattr__(
                self,
                "overlap_manifest_document",
                overlap_manifest_document(overlap),
            )


def identity_manifest_path(categorized_path: Path) -> Path:
    """Return the manifest's fixed sibling path."""
    return Path(categorized_path).parent / str(IDENTITY_MANIFEST_NAME)


def load_identity_state(
    categorized_path: Path,
    *,
    allowed_generation_paths: Iterable[Path] = (),
) -> IdentityState:
    """Recover and validate canonical ledger plus source-occurrence ownership."""
    categorized_path = Path(categorized_path)
    recover_generation(
        categorized_path,
        allowed_generation_paths=allowed_generation_paths,
    )
    manifest_path = identity_manifest_path(categorized_path)
    occurrences_path = source_occurrences_path(categorized_path)
    canonical_manifest_path = overlap_manifest_path(categorized_path)
    ledger_exists = categorized_path.exists()
    manifest_exists = manifest_path.exists()
    occurrences_exist = occurrences_path.exists()
    canonical_manifest_exists = canonical_manifest_path.exists()

    if not ledger_exists:
        if manifest_exists or occurrences_exist or canonical_manifest_exists:
            raise IdentityError("identity_manifest_invalid")
        manifest = empty_manifest()
        overlap = empty_overlap_manifest("ovns_" + secrets.token_hex(32))
        return IdentityState(
            [],
            manifest,
            manifest_document(manifest),
            source_rows=[],
            source_evidence_rows=[],
            overlap_manifest=overlap,
            overlap_manifest_document=overlap_manifest_document(overlap),
        )

    header = _ledger_header(categorized_path)
    if occurrences_exist != canonical_manifest_exists:
        raise IdentityError("identity_manifest_invalid")

    if occurrences_exist:
        if not manifest_exists or header != CATEGORIZED_COLUMNS:
            raise IdentityError("identity_manifest_invalid")
        rows = read_csv_artifact(categorized_path, CATEGORIZED_COLUMNS).rows
        source_evidence_rows = read_csv_artifact(
            occurrences_path, SOURCE_OCCURRENCE_COLUMNS
        ).rows
        document, manifest = _read_identity_manifest(manifest_path)
        active_ids = _active_transaction_ids(manifest)
        active_source_rows = [
            row
            for row in source_evidence_rows
            if row.get("transaction_id") in active_ids
        ]
        legacy_rows = [
            row
            for row in rows
            if not row.get("canonical_group_id")
            and not any(row.get(field) for field in ID_FIELDS)
        ]
        source_rows = [*active_source_rows, *legacy_rows]
        validate_ledger_manifest_agreement(source_rows, manifest)
        validate_source_evidence_manifest_agreement(source_evidence_rows, manifest)
        try:
            overlap_document = canonical_manifest_path.read_text(encoding="utf-8")
            parsed_overlap = parse_overlap_manifest(overlap_document)
            overlap_migration_required = parsed_overlap["schema_version"] == 1
            if overlap_migration_required:
                overlap = canonicalize_overlaps(
                    source_rows, rows, parsed_overlap
                ).manifest
                overlap_document = overlap_manifest_document(overlap)
            else:
                overlap = cast(OverlapManifest, parsed_overlap)
            validate_overlap_agreement(rows, source_rows, overlap)
        except (OSError, UnicodeError, ValueError) as error:
            raise IdentityError("identity_manifest_invalid") from error
        return IdentityState(
            rows,
            manifest,
            document,
            source_rows=source_rows,
            source_evidence_rows=source_evidence_rows,
            overlap_manifest=overlap,
            overlap_manifest_document=overlap_document,
            overlap_migration_required=overlap_migration_required,
        )

    if header == SOURCE_OCCURRENCE_COLUMNS:
        if not manifest_exists:
            raise IdentityError("identity_manifest_missing")
        source_rows = read_csv_artifact(
            categorized_path, SOURCE_OCCURRENCE_COLUMNS
        ).rows
        document, manifest = _read_identity_manifest(manifest_path)
        validate_ledger_manifest_agreement(source_rows, manifest)
        overlap = empty_overlap_manifest("ovns_" + secrets.token_hex(32))
        return IdentityState(
            _canonical_column_order(source_rows),
            manifest,
            document,
            source_rows=source_rows,
            source_evidence_rows=source_rows,
            overlap_manifest=overlap,
            overlap_manifest_document=overlap_manifest_document(overlap),
            canonical_migration_required=True,
        )

    if not manifest_exists:
        if header == LEGACY_CATEGORIZED_COLUMNS:
            source_rows = read_csv_artifact(
                categorized_path, SOURCE_OCCURRENCE_COLUMNS
            ).rows
            rows = _canonical_column_order(source_rows)
            for row in rows:
                for field in ID_FIELDS:
                    row[field] = ""
            for row in source_rows:
                for field in ID_FIELDS:
                    row[field] = ""
            manifest = empty_manifest()
            overlap = empty_overlap_manifest("ovns_" + secrets.token_hex(32))
            return IdentityState(
                rows,
                manifest,
                manifest_document(manifest),
                bootstrap_required=True,
                source_rows=source_rows,
                source_evidence_rows=source_rows,
                overlap_manifest=overlap,
                overlap_manifest_document=overlap_manifest_document(overlap),
                canonical_migration_required=True,
            )
        if (
            header == CATEGORIZED_COLUMNS
            or any(field in header for field in ID_FIELDS)
            or any(
                field in header
                for field in (
                    "canonical_group_id",
                    "canonical_slot",
                    "provenance_status",
                    "source_occurrence_count",
                )
            )
        ):
            raise IdentityError("identity_manifest_missing")
        raise IdentityError("identity_manifest_invalid")

    raise IdentityError("identity_manifest_invalid")


def load_configured_identity_state(
    categorized_path: Path,
    config: Config,
) -> IdentityState:
    """Load state with the exact configured generation members allowed."""
    return load_identity_state(
        categorized_path,
        allowed_generation_paths=configured_generation_paths(config),
    )


def validated_manifest_document(
    ledger_rows: Sequence[Mapping[str, str]], manifest: IdentityManifest
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


def _read_identity_manifest(path: Path) -> tuple[str, IdentityManifest]:
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise IdentityError("identity_manifest_invalid") from error
    return document, parse_manifest(document)


def _canonical_column_order(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    return [
        {column: str(row.get(column, "")) for column in CATEGORIZED_COLUMNS}
        for row in rows
    ]


def _active_transaction_ids(manifest: IdentityManifest) -> set[str]:
    return {
        str(record["transaction_id"])
        for source in manifest["sources"]
        for record in source["records"]
        if record["state"] == "active"
    }


def validate_source_evidence_manifest_agreement(
    rows: Sequence[Mapping[str, str]], manifest: IdentityManifest
) -> None:
    expected: dict[str, tuple[IdentitySourceManifest, IdentityRecordManifest]] = {}
    for source in manifest["sources"]:
        for record in source["records"]:
            identifier = str(record["transaction_id"])
            if identifier in expected:
                raise IdentityError("identity_manifest_invalid")
            expected[identifier] = (source, record)
    seen: set[str] = set()
    for row in rows:
        identifier = str(row.get("transaction_id", ""))
        ownership = expected.get(identifier)
        if ownership is None or identifier in seen:
            raise IdentityError("identity_manifest_invalid")
        seen.add(identifier)
        source, record = ownership
        origin = record["allocation_origin"]
        expected_revision = (
            source["source_revision"]
            if record["state"] == "active"
            else origin["source_revision"]
        )
        if (
            row.get("source_id") != source["source_id"]
            or row.get("source_namespace_id") != source["source_namespace_id"]
            or row.get("source_revision") != expected_revision
            or row.get("source_record_id") != record["source_record_id"]
            or record_fingerprint(row) != record["record_fingerprint"]
        ):
            raise IdentityError("identity_manifest_invalid")
    if seen != set(expected):
        raise IdentityError("identity_manifest_invalid")
