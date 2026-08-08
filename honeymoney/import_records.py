"""Durable, private import-record storage.

This module owns normalized source snapshots and immutable attempt history.  It
does not parse statements or publish a multi-file workspace generation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Literal, Mapping, Sequence, TypedDict, cast

from honeymoney.persistence import ensure_private_directory, private_atomic_write_text
from honeymoney.workspace_paths import (
    WorkspacePathError,
    reject_existing_symlink_components,
)

ATTEMPT_SCHEMA_VERSION = 1
IMPORT_RECORD_SCHEMA_VERSION = 1
TRANSACTION_SNAPSHOT_SCHEMA_VERSION = 1
MAX_ATTEMPT_BYTES = 64 * 1024
MAX_SOURCE_LABEL_CHARS = 32
SOURCE_ID_PATTERN = re.compile(r"src_[0-9a-f]{64}")
SOURCE_RECORD_ID_PATTERN = re.compile(r"rec_[0-9a-f]{64}")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_NAME_PATTERN = re.compile(r"[0-9]{8}\.json")
_SOURCE_LABEL_PATTERN = re.compile(r"(?:CSV|PDF|Source) source [0-9a-f]{12}\Z")

AttemptOutcome = Literal["success", "failure"]
AttemptAction = Literal["import", "replace", "reset"]


class ImportRecordError(ValueError):
    """Stable import-record failure safe for CLI output."""

    def __init__(self, code: str, *, unsafe_path: bool = False) -> None:
        self.code = code
        self.unsafe_path = unsafe_path
        super().__init__(code)


class AttemptReport(TypedDict, total=False):
    schema_version: int
    honeymoney_version: str
    source_id: str
    source_label: str
    attempt_number: int
    requested_action: AttemptAction
    started_at: str
    finished_at: str
    outcome: AttemptOutcome
    source_revision: str
    parser_contract: str
    counts: dict[str, int]
    warnings: list[str]
    warning_count: int
    omitted_warning_count: int
    error_codes: list[str]
    error_count: int
    omitted_error_count: int
    transactions_schema_version: int
    transactions_digest: str


class ImportRecordSummary(TypedDict):
    schema_version: int
    source_id: str
    source_label: str
    ready: bool
    current_attempt_number: int | None
    statement_transaction_count: int


def import_record_path(records_root: Path, source_id: str) -> Path:
    _validate_source_id(source_id)
    return Path(records_root) / source_id


def safe_source_label(source_id: str, source_kind: str) -> str:
    """Return a bounded public source label without source-path text."""
    _validate_source_id(source_id)
    kind = source_kind.casefold().lstrip(".")
    prefix = {"csv": "CSV", "pdf": "PDF"}.get(kind, "Source")
    label = f"{prefix} source {source_id[4:16]}"
    if len(label) > MAX_SOURCE_LABEL_CHARS:
        raise ImportRecordError("source_label_invalid")
    return label


def attempt_path(record: Path, attempt_number: int) -> Path:
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        raise ImportRecordError("attempt_number_invalid")
    if not 1 <= attempt_number <= 99_999_999:
        raise ImportRecordError("attempt_number_invalid")
    return Path(record) / "attempts" / f"{attempt_number:08d}.json"


def next_attempt_number(record: Path) -> int:
    attempts = _attempt_files(record)
    if not attempts:
        return 1
    number = int(attempts[-1].stem) + 1
    if number > 99_999_999:
        raise ImportRecordError("attempt_number_exhausted")
    return number


def attempt_document(report: Mapping[str, object]) -> str:
    checked = validate_attempt_report(report)
    document = _canonical_json(checked)
    if len(document.encode("utf-8")) > MAX_ATTEMPT_BYTES:
        raise ImportRecordError("attempt_report_too_large")
    return document


def validate_attempt_report(report: Mapping[str, object]) -> AttemptReport:
    required = {
        "schema_version",
        "honeymoney_version",
        "source_id",
        "source_label",
        "attempt_number",
        "requested_action",
        "started_at",
        "finished_at",
        "outcome",
        "source_revision",
        "parser_contract",
        "counts",
        "warnings",
        "warning_count",
        "omitted_warning_count",
        "error_codes",
        "error_count",
        "omitted_error_count",
    }
    success = {"transactions_schema_version", "transactions_digest"}
    outcome = report.get("outcome")
    allowed = required | (success if outcome == "success" else set())
    if set(report) != allowed or not required.issubset(report):
        raise ImportRecordError("attempt_report_invalid")
    if report.get("schema_version") != ATTEMPT_SCHEMA_VERSION:
        raise ImportRecordError("attempt_schema_unsupported")
    source_id = report.get("source_id")
    _validate_source_id(source_id)
    assert isinstance(source_id, str)
    _require_string(report, "honeymoney_version")
    _validate_source_label(report.get("source_label"), source_id)
    _require_string(report, "started_at")
    _require_string(report, "finished_at")
    _require_string(report, "source_revision")
    _require_string(report, "parser_contract")
    number = report.get("attempt_number")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or not 1 <= number <= 99_999_999
    ):
        raise ImportRecordError("attempt_report_invalid")
    if report.get("requested_action") not in {"import", "replace", "reset"}:
        raise ImportRecordError("attempt_report_invalid")
    if outcome not in {"success", "failure"}:
        raise ImportRecordError("attempt_report_invalid")
    _validate_counts(report.get("counts"))
    warnings = _validate_string_list(report.get("warnings"))
    errors = _validate_string_list(report.get("error_codes"))
    _validate_bounded_count(
        report, "warning_count", "omitted_warning_count", len(warnings)
    )
    _validate_bounded_count(report, "error_count", "omitted_error_count", len(errors))
    if outcome == "success":
        if (
            report.get("transactions_schema_version")
            != TRANSACTION_SNAPSHOT_SCHEMA_VERSION
        ):
            raise ImportRecordError("attempt_report_invalid")
        digest = report.get("transactions_digest")
        if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
            raise ImportRecordError("attempt_report_invalid")
    return cast(AttemptReport, _json_copy(report))


def write_attempt(record: Path, report: Mapping[str, object]) -> Path:
    checked = validate_attempt_report(report)
    target = attempt_path(record, checked["attempt_number"])
    _require_safe_record_path(target, error_code="attempt_immutable_conflict")
    document = attempt_document(checked)
    try:
        private_atomic_write_text(target, document, overwrite=False)
    except FileExistsError as error:
        try:
            _require_safe_record_path(target, error_code="attempt_immutable_conflict")
            existing = target.read_text(encoding="utf-8")
        except OSError, UnicodeError:
            raise ImportRecordError("attempt_immutable_conflict") from error
        if existing != document:
            raise ImportRecordError("attempt_immutable_conflict") from error
    return target


def load_attempts(record: Path) -> list[AttemptReport]:
    _require_safe_record_path(record, error_code="attempt_history_invalid")
    reports: list[AttemptReport] = []
    expected = 1
    for path in _attempt_files(record):
        if int(path.stem) != expected:
            raise ImportRecordError("attempt_history_invalid")
        try:
            document = path.read_text(encoding="utf-8")
            value = json.loads(document)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ImportRecordError("attempt_history_invalid") from error
        if not isinstance(value, dict):
            raise ImportRecordError("attempt_history_invalid")
        report = validate_attempt_report(value)
        if report["attempt_number"] != expected or attempt_document(report) != document:
            raise ImportRecordError("attempt_history_invalid")
        reports.append(report)
        expected += 1
    return reports


def transaction_snapshot_document(
    columns: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> str:
    fields = list(columns)
    if (
        not fields
        or len(fields) != len(set(fields))
        or "source_record_id" not in fields
    ):
        raise ImportRecordError("transaction_snapshot_invalid")
    if any(not isinstance(field, str) or not field for field in fields):
        raise ImportRecordError("transaction_snapshot_invalid")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    seen: set[str] = set()
    for row in rows:
        if set(row) != set(fields) or any(
            not isinstance(value, str) for value in row.values()
        ):
            raise ImportRecordError("transaction_snapshot_invalid")
        identifier = row["source_record_id"]
        if SOURCE_RECORD_ID_PATTERN.fullmatch(identifier) is None or identifier in seen:
            raise ImportRecordError("transaction_snapshot_invalid")
        seen.add(identifier)
        writer.writerow({field: row[field] for field in fields})
    return output.getvalue()


def write_transaction_snapshot(
    record: Path, columns: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> tuple[Path, str]:
    document = transaction_snapshot_document(columns, rows)
    target = Path(record) / "transactions.csv"
    _require_safe_record_path(target, error_code="transaction_snapshot_invalid")
    private_atomic_write_text(target, document)
    return target, hashlib.sha256(document.encode("utf-8")).hexdigest()


def read_transaction_snapshot(
    path: Path, columns: Sequence[str]
) -> list[dict[str, str]]:
    """Read one exact canonical normalized statement-transaction snapshot."""
    try:
        target = _require_safe_record_path(
            Path(path), error_code="transaction_snapshot_invalid"
        )
        with target.open(encoding="utf-8", newline="") as handle:
            document = handle.read()
        reader = csv.DictReader(io.StringIO(document, newline=""))
        if tuple(reader.fieldnames or ()) != tuple(columns):
            raise ImportRecordError("transaction_snapshot_invalid")
        rows: list[dict[str, str]] = []
        for row in reader:
            if row.get(None) is not None or set(row) != set(columns):
                raise ImportRecordError("transaction_snapshot_invalid")
            rows.append({column: row.get(column) or "" for column in columns})
        if transaction_snapshot_document(columns, rows) != document:
            raise ImportRecordError("transaction_snapshot_invalid")
        return rows
    except ImportRecordError:
        raise
    except (OSError, UnicodeError, csv.Error) as error:
        raise ImportRecordError("transaction_snapshot_invalid") from error


def build_summary(record: Path, source_id: str) -> ImportRecordSummary:
    _validate_source_id(source_id)
    _require_safe_record_path(record, error_code="import_record_disagreement")
    reports = load_attempts(record)
    if any(report["source_id"] != source_id for report in reports):
        raise ImportRecordError("import_record_disagreement")
    successful = [report for report in reports if report["outcome"] == "success"]
    current = successful[-1] if successful else None
    label = str(
        current["source_label"]
        if current
        else reports[0]["source_label"]
        if reports
        else ""
    )
    count = 0
    snapshot = Path(record) / "transactions.csv"
    _require_safe_record_path(snapshot, error_code="import_record_disagreement")
    if current is not None:
        count, digest = _snapshot_count_and_digest(snapshot)
        if digest != current["transactions_digest"]:
            raise ImportRecordError("import_record_disagreement")
    elif snapshot.exists():
        raise ImportRecordError("import_record_disagreement")
    return {
        "schema_version": IMPORT_RECORD_SCHEMA_VERSION,
        "source_id": source_id,
        "source_label": label,
        "ready": current is not None,
        "current_attempt_number": current["attempt_number"] if current else None,
        "statement_transaction_count": count,
    }


def summary_document(summary: Mapping[str, object]) -> str:
    expected = {
        "schema_version",
        "source_id",
        "source_label",
        "ready",
        "current_attempt_number",
        "statement_transaction_count",
    }
    if (
        set(summary) != expected
        or summary.get("schema_version") != IMPORT_RECORD_SCHEMA_VERSION
    ):
        raise ImportRecordError("import_record_summary_invalid")
    source_id = summary.get("source_id")
    _validate_source_id(source_id)
    assert isinstance(source_id, str)
    _validate_source_label(summary.get("source_label"), source_id)
    if not isinstance(summary.get("ready"), bool):
        raise ImportRecordError("import_record_summary_invalid")
    number, count = (
        summary.get("current_attempt_number"),
        summary.get("statement_transaction_count"),
    )
    if number is not None and (
        not isinstance(number, int) or isinstance(number, bool) or number < 1
    ):
        raise ImportRecordError("import_record_summary_invalid")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ImportRecordError("import_record_summary_invalid")
    if bool(summary["ready"]) != (number is not None):
        raise ImportRecordError("import_record_summary_invalid")
    return _canonical_json(summary)


def write_summary(record: Path, source_id: str) -> Path:
    target = Path(record) / "summary.json"
    _require_safe_record_path(target, error_code="import_record_summary_invalid")
    private_atomic_write_text(
        target, summary_document(build_summary(record, source_id))
    )
    return target


def initialize_record(records_root: Path, source_id: str) -> Path:
    root = Path(records_root)
    try:
        reject_existing_symlink_components(root)
    except WorkspacePathError as error:
        raise OSError("Import-record path is unsafe.") from error
    target = import_record_path(root, source_id)
    try:
        reject_existing_symlink_components(target)
    except WorkspacePathError as error:
        raise OSError("Import-record path is unsafe.") from error
    ensure_private_directory(target / "attempts")
    return target


def _attempt_files(record: Path) -> list[Path]:
    directory = Path(record) / "attempts"
    try:
        _require_safe_record_path(directory, error_code="attempt_history_invalid")
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ImportRecordError("attempt_history_invalid")
        result: list[Path] = []
        for path in directory.iterdir():
            _require_safe_record_path(path, error_code="attempt_history_invalid")
            if (
                path.is_symlink()
                or not path.is_file()
                or _ATTEMPT_NAME_PATTERN.fullmatch(path.name) is None
            ):
                raise ImportRecordError("attempt_history_invalid")
            result.append(path)
        return sorted(result)
    except ImportRecordError:
        raise
    except OSError as error:
        raise ImportRecordError("attempt_history_invalid") from error


def _snapshot_count_and_digest(path: Path) -> tuple[int, str]:
    try:
        target = _require_safe_record_path(
            path, error_code="import_record_disagreement"
        )
        document = target.read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(document)))
    except (OSError, UnicodeError, csv.Error) as error:
        raise ImportRecordError("import_record_disagreement") from error
    return len(rows), hashlib.sha256(document.encode("utf-8")).hexdigest()


def _require_safe_record_path(path: Path, *, error_code: str) -> Path:
    target = Path(path)
    try:
        reject_existing_symlink_components(target)
    except WorkspacePathError as error:
        raise ImportRecordError(error_code, unsafe_path=True) from error
    return target


def _validate_source_id(value: object) -> None:
    if not isinstance(value, str) or SOURCE_ID_PATTERN.fullmatch(value) is None:
        raise ImportRecordError("source_id_invalid")


def _validate_source_label(value: object, source_id: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > MAX_SOURCE_LABEL_CHARS
        or not value.isascii()
        or _SOURCE_LABEL_PATTERN.fullmatch(value) is None
        or value[-12:] != source_id[4:16]
    ):
        raise ImportRecordError("source_label_invalid")


def _require_string(value: Mapping[str, object], key: str) -> None:
    if not isinstance(value.get(key), str) or not value[key]:
        raise ImportRecordError("attempt_report_invalid")


def _validate_counts(value: object) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, int)
        or isinstance(item, bool)
        or item < 0
        for key, item in value.items()
    ):
        raise ImportRecordError("attempt_report_invalid")


def _validate_string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ImportRecordError("attempt_report_invalid")
    return value


def _validate_bounded_count(
    report: Mapping[str, object], total_key: str, omitted_key: str, retained: int
) -> None:
    total, omitted = report.get(total_key), report.get(omitted_key)
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(omitted, int)
        or isinstance(omitted, bool)
        or total < 0
        or omitted < 0
        or total != retained + omitted
    ):
        raise ImportRecordError("attempt_report_invalid")


def _canonical_json(value: Mapping[str, object]) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(json.dumps(value, ensure_ascii=False))
    if not isinstance(copied, dict):  # pragma: no cover - Mapping always encodes so
        raise ImportRecordError("attempt_report_invalid")
    return copied
