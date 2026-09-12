"""Read-only statement facts for local clients that own their own records."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from honeymoney import importers
from honeymoney.reconciliation import statement_balance_reconciliation
from honeymoney.workspace_index import HONEYMONEY_VERSION

PARSE_SCHEMA_VERSION = 1
FACT_FIELDS = (
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "account_type",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "statement_opening_balance",
    "statement_closing_balance",
    "statement_section",
    "merchant",
    "original_description",
    "source_page",
    "source_row",
)


class ParseError(ValueError):
    """A bounded failure that never echoes source data or paths."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_statement(input_path: Path, profile_id: str) -> dict[str, object]:
    """Return source facts from exactly one captured file; create no workspace."""
    profiles_root = Path(__file__).parent / "data" / "profiles"
    if not re.fullmatch(r"[a-z0-9_]+", profile_id):
        raise ParseError("parse_unknown_profile", "Choose a bundled profile ID.")
    profile_path = profiles_root / f"{profile_id}.json"
    suffix = input_path.suffix.casefold()
    if suffix not in {".csv", ".pdf"}:
        raise ParseError("parse_unsupported_type", "Expected a CSV or PDF file.")

    try:
        if not profile_path.is_file():
            raise ParseError("parse_unknown_profile", "Choose a bundled profile ID.")
        if input_path.is_symlink() or not input_path.is_file():
            raise ParseError("parse_invalid_source", "Source must be a regular file.")
        profile = importers._validate_profile(
            json.loads(profile_path.read_text(encoding="utf-8")), profile_path, {}
        )
        if suffix[1:] not in profile:
            raise ParseError(
                "parse_profile_type_mismatch",
                "Profile does not support this file type.",
            )
        snapshot = importers._capture_input_source(input_path, {})
        expected_path = input_path.parent.resolve(strict=True) / input_path.name
        if snapshot.resolved_path != expected_path:
            raise ParseError("parse_invalid_source", "Source must be a regular file.")
        metadata: dict[str, int] = {}
        rows, warnings = importers.preview_profile_input(
            profile,
            profile_id,
            input_path,
            {},
            source_snapshot=snapshot,
            metadata=metadata,
            value_rows=False,
        )
    except ParseError:
        raise
    except ModuleNotFoundError as error:
        raise ParseError(
            "parse_dependency_missing", "Install Honeymoney with its PDF dependencies."
        ) from error
    except Exception as error:
        raise ParseError(
            "parse_failed", "Could not parse source with the selected profile."
        ) from error

    if not rows:
        raise ParseError(
            "parse_no_rows",
            "No transaction rows found; check the profile or review the source manually.",
        )
    fact_rows = [dict(row) for row in rows]
    for row in fact_rows:
        if "invalid_amount" in row.get("flags", "").split(";"):
            row["original_amount"] = ""
            row["posted_amount"] = ""
    display_name = f"statement{suffix}"
    warnings = [warning.replace(input_path.name, display_name) for warning in warnings]
    for row in fact_rows:
        row["source_file"] = display_name
    if any("invalid_amount" in row.get("flags", "").split(";") for row in fact_rows):
        warnings.append("One or more rows have invalid source amounts.")
    if any(
        not _is_iso_date(row.get("date", ""))
        or any(
            row.get(field) and not _is_iso_date(row[field])
            for field in ("transaction_date", "posting_date")
        )
        for row in fact_rows
    ):
        warnings.append("One or more rows have missing or invalid dates.")
    if isinstance(profile.get("statement_year"), int):
        warnings.append(
            f"Profile uses fixed year {profile['statement_year']} when dates omit the year; "
            "check all dates against the statement."
        )
    return {
        "parse_schema_version": PARSE_SCHEMA_VERSION,
        "engine": {"name": "honeymoney", "version": HONEYMONEY_VERSION},
        "profile_id": profile_id,
        "profile_sha256": hashlib.sha256(
            json.dumps(
                profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest(),
        "source_sha256": hashlib.sha256(snapshot.source_bytes).hexdigest(),
        "page_count": metadata.get("page_count"),
        "rows": [
            {field: row.get(field, "") for field in FACT_FIELDS} for row in fact_rows
        ],
        "warnings": warnings,
        "balance_checks": statement_balance_reconciliation(fact_rows),
    }


def _is_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
