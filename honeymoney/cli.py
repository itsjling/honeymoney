from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime
from datetime import date
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from honeymoney.schema import (
    CATEGORIZED_COLUMNS,
    REVIEW_NEEDED_COLUMNS,
    allowed_categories,
    allowed_owners,
    allowed_payment_methods,
)
from honeymoney.rules import apply_rules, load_rules
from honeymoney.ollama import apply_ollama_fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="honeymoney",
        description="Categorize local household transaction exports.",
    )
    parser.add_argument("--input", dest="input_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--no-interactive", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config(args.config_path)
    input_path = Path(args.input_path or config["paths"]["input"])
    categorized_path = Path(args.output_path or config["paths"]["output"])
    output_dir = categorized_path.parent
    review_needed_path = output_dir / "review_needed.csv"
    import_report_path = output_dir / "import_report.json"

    output_dir.mkdir(parents=True, exist_ok=True)

    input_files = _discover_input_files(input_path)
    profiles = _load_profiles(config)
    profile_mappings = _load_profile_mappings(config)
    transactions, import_warnings, file_reports = _import_transactions(
        input_files,
        profiles,
        config,
        input_path,
        interactive=not args.no_interactive,
        profile_mappings=profile_mappings,
        profile_mappings_path=config.get("profile_mappings"),
    )
    rules = load_rules(config)
    apply_rules(transactions, rules, config)
    _annotate_duplicate_suspicions(transactions)
    ollama_report, ollama_warnings = apply_ollama_fallback(transactions, config)
    corrections = _load_corrections(config)
    _apply_corrections(transactions, corrections)
    review_rows = [row for row in transactions if row["needs_review"] == "true"]

    _write_csv(categorized_path, CATEGORIZED_COLUMNS, transactions)
    _write_csv(
        review_needed_path,
        REVIEW_NEEDED_COLUMNS,
        [_to_review_row(row) for row in review_rows],
    )
    _write_report(
        import_report_path,
        {
            "status": "partial_success" if import_warnings else "success",
            "input_count": len(input_files),
            "transaction_count": len(transactions),
            "review_count": len(review_rows),
            "duplicate_count": _count_flag(transactions, "duplicate_suspected"),
            "strict": args.strict,
            "interactive": not args.no_interactive,
            "output": {
                "categorized_csv": str(categorized_path),
                "review_needed_csv": str(review_needed_path),
                "import_report_json": str(import_report_path),
            },
            "files": file_reports,
            "transaction_flags": _transaction_flags(transactions),
            "transaction_diagnostics": _transaction_diagnostics(transactions),
            "warnings": import_warnings + ollama_warnings,
            "errors": [],
            "ollama": ollama_report,
        },
    )

    if args.strict and import_warnings:
        return 1
    return 0


def _load_config(config_path: str | None) -> dict[str, Any]:
    if config_path is None:
        return {"paths": {"input": "./input", "output": "./output/categorized.csv"}}

    with Path(config_path).open(encoding="utf-8") as fh:
        config = json.load(fh)

    config.setdefault("paths", {})
    config["paths"].setdefault("input", "./input")
    config["paths"].setdefault("output", "./output/categorized.csv")
    return config


def _load_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = []
    for profile_path in config.get("profiles", []):
        with Path(profile_path).open(encoding="utf-8") as fh:
            profile = json.load(fh)
            _validate_profile(profile, Path(profile_path), config)
            profiles.append(profile)
    return profiles


def _validate_profile(
    profile: dict[str, Any], profile_path: Path, config: dict[str, Any]
) -> None:
    profile_id = profile.get("id") or profile.get("account_id") or profile_path.name
    if not str(profile.get("account_id", "")).strip():
        raise ValueError(
            f"Missing required profile fields in profile {profile_id}: account_id"
        )
    if profile.get("owner") and profile["owner"] not in allowed_owners(config):
        raise ValueError(f"Unsupported owner in profile {profile_id}: {profile['owner']}")
    if (
        profile.get("payment_method")
        and profile["payment_method"] not in allowed_payment_methods(config)
    ):
        raise ValueError(
            f"Unsupported payment_method in profile {profile_id}: "
            f"{profile['payment_method']}"
        )


def _load_profile_mappings(config: dict[str, Any]) -> dict[str, Any]:
    mapping_path = config.get("profile_mappings")
    if not mapping_path:
        return {}
    if not Path(mapping_path).exists():
        return {}
    with Path(mapping_path).open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_corrections(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    corrections_path = config.get("corrections")
    if not corrections_path:
        return {}

    corrections: dict[str, dict[str, str]] = {}
    with Path(corrections_path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            transaction_id = (row.get("transaction_id") or "").strip()
            if not transaction_id:
                continue
            meaningful = {
                field: (row.get(field) or "").strip()
                for field in [
                    "category",
                    "owner",
                    "payment_method",
                    "confidence",
                    "reason",
                    "notes",
                    "needs_review",
                ]
                if (row.get(field) or "").strip()
            }
            if meaningful:
                _validate_correction(transaction_id, meaningful, config)
                corrections[transaction_id] = meaningful
    return corrections


def _validate_correction(
    transaction_id: str, correction: dict[str, str], config: dict[str, Any]
) -> None:
    if correction.get("category") and correction["category"] not in allowed_categories(config):
        raise ValueError(
            f"Unsupported category in correction {transaction_id}: {correction['category']}"
        )
    if correction.get("owner") and correction["owner"] not in allowed_owners(config):
        raise ValueError(
            f"Unsupported owner in correction {transaction_id}: {correction['owner']}"
        )
    if (
        correction.get("payment_method")
        and correction["payment_method"] not in allowed_payment_methods(config)
    ):
        raise ValueError(
            "Unsupported payment_method in correction "
            f"{transaction_id}: {correction['payment_method']}"
        )
    if correction.get("confidence"):
        try:
            confidence = Decimal(correction["confidence"])
        except InvalidOperation:
            raise ValueError(
                f"Unsupported confidence in correction {transaction_id}: "
                f"{correction['confidence']}"
            )
        if not confidence.is_finite() or confidence < Decimal("0") or confidence > Decimal("1"):
            raise ValueError(
                f"Unsupported confidence in correction {transaction_id}: "
                f"{correction['confidence']}"
            )
    if correction.get("needs_review") and correction["needs_review"].casefold() not in {
        "true",
        "false",
    }:
        raise ValueError(
            f"Unsupported needs_review in correction {transaction_id}: "
            f"{correction['needs_review']}"
        )


def _discover_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(
            path
            for path in input_path.iterdir()
            if path.is_file() and path.suffix.lower() in {".csv", ".pdf"}
        )
    return []


def _import_transactions(
    input_files: list[Path],
    profiles: list[dict[str, Any]],
    config: dict[str, Any],
    input_root: Path,
    interactive: bool,
    profile_mappings: dict[str, Any],
    profile_mappings_path: str | None,
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
    transactions: list[dict[str, str]] = []
    warnings: list[str] = []
    file_reports: list[dict[str, str]] = []
    for input_file in input_files:
        suffix = input_file.suffix.lower()
        if suffix == ".pdf":
            if config.get("pdf", {}).get("enabled") is False:
                warning = (
                    "PDF parsing disabled; skipped "
                    f"{_relative_source(input_file, input_root)}"
                )
                warnings.append(warning)
                file_reports.append(
                    {
                        "source_file": _relative_source(input_file, input_root),
                        "status": "skipped",
                        "reason": warning,
                    }
                )
                continue
            try:
                profile = _select_pdf_profile(
                    input_file,
                    profiles,
                    interactive,
                    profile_mappings,
                    profile_mappings_path,
                )
                imported, pdf_warnings = _import_pdf(input_file, profile, config, input_root)
                warnings.extend(pdf_warnings)
            except ImportError:
                warning = (
                    "PDF parsing requires pdfplumber; skipped "
                    f"{_relative_source(input_file, input_root)}"
                )
                warnings.append(warning)
                file_reports.append(
                    {
                        "source_file": _relative_source(input_file, input_root),
                        "status": "failed",
                        "reason": warning,
                    }
                )
                continue
            except Exception as error:
                warning = (
                    f"PDF parsing failed for {_relative_source(input_file, input_root)}: {error}"
                )
                warnings.append(warning)
                file_reports.append(
                    {
                        "source_file": _relative_source(input_file, input_root),
                        "status": "failed",
                        "reason": warning,
                    }
                )
                continue

            transactions.extend(imported)
            file_reports.append(
                {
                    "source_file": _relative_source(input_file, input_root),
                    "status": "processed",
                    "transaction_count": str(len(imported)),
                    "profile_id": str(
                        profile.get("id") or profile.get("account_id") or "default"
                    ),
                    "parser": "pdfplumber",
                }
            )
            continue
        if suffix != ".csv":
            continue
        profile = _select_csv_profile(
            input_file,
            profiles,
            interactive,
            profile_mappings,
            profile_mappings_path,
        )
        imported = _import_csv(input_file, profile, config, input_root)
        transactions.extend(imported)
        file_reports.append(
            {
                "source_file": _relative_source(input_file, input_root),
                "status": "processed",
                "transaction_count": str(len(imported)),
                "profile_id": str(
                    profile.get("id") or profile.get("account_id") or "default"
                ),
            }
        )
    return _assign_transaction_ids(transactions), warnings, file_reports


def _select_pdf_profile(
    pdf_path: Path,
    profiles: list[dict[str, Any]],
    interactive: bool,
    profile_mappings: dict[str, Any],
    profile_mappings_path: str | None,
) -> dict[str, Any]:
    if not profiles:
        return _default_profile()

    mapped_profile = _mapped_profile(pdf_path, profiles, profile_mappings)
    if mapped_profile is not None:
        return mapped_profile

    if len(profiles) > 1:
        if not interactive:
            raise ValueError(f"Could not detect profile for {pdf_path.name}")
        return _prompt_for_profile(pdf_path, profiles, profile_mappings_path)

    return profiles[0]


def _select_csv_profile(
    csv_path: Path,
    profiles: list[dict[str, Any]],
    interactive: bool,
    profile_mappings: dict[str, Any],
    profile_mappings_path: str | None,
) -> dict[str, Any]:
    if not profiles:
        return _default_profile()

    mapped_profile = _mapped_profile(csv_path, profiles, profile_mappings)
    if mapped_profile is not None:
        return mapped_profile

    headers = _csv_headers(csv_path)
    matching_profiles = []
    for profile in profiles:
        required_headers = profile.get("csv", {}).get("detect_headers", [])
        if required_headers and set(required_headers).issubset(headers):
            matching_profiles.append(profile)

    if len(matching_profiles) == 1:
        return matching_profiles[0]
    if len(matching_profiles) > 1:
        if not interactive:
            labels = ", ".join(
                str(profile.get("id") or profile.get("account_id") or "unknown")
                for profile in matching_profiles
            )
            raise ValueError(f"Ambiguous profile detection for {csv_path.name}: {labels}")
        return _prompt_for_profile(csv_path, matching_profiles, profile_mappings_path)

    if len(profiles) > 1:
        if not interactive:
            raise ValueError(f"Could not detect profile for {csv_path.name}")
        return _prompt_for_profile(csv_path, profiles, profile_mappings_path)

    return profiles[0]


def _mapped_profile(
    source_path: Path, profiles: list[dict[str, Any]], mappings: dict[str, Any]
) -> dict[str, Any] | None:
    profiles_by_id = {
        str(profile.get("id") or profile.get("account_id")): profile for profile in profiles
    }
    for mapping in mappings.get("filename_patterns", []):
        if fnmatch(source_path.name, str(mapping.get("pattern", ""))):
            return profiles_by_id.get(str(mapping.get("profile", "")))
    return None


def _prompt_for_profile(
    csv_path: Path, profiles: list[dict[str, Any]], profile_mappings_path: str | None
) -> dict[str, Any]:
    print(f"Select profile for {csv_path.name}:")
    for index, profile in enumerate(profiles, start=1):
        label = profile.get("id") or profile.get("account_id") or "unknown"
        print(f"{index}. {label}")

    while True:
        choice = input("Profile number: ").strip()
        try:
            selected = int(choice)
        except ValueError:
            print("Enter a number from the list.")
            continue
        if 1 <= selected <= len(profiles):
            profile = profiles[selected - 1]
            _maybe_save_profile_mapping(csv_path, profile, profile_mappings_path)
            return profile
        print("Enter a number from the list.")


def _maybe_save_profile_mapping(
    csv_path: Path, profile: dict[str, Any], profile_mappings_path: str | None
) -> None:
    if not profile_mappings_path:
        return
    choice = input(f"Remember profile for {csv_path.name}? [y/N]: ").strip().casefold()
    if choice not in {"y", "yes"}:
        return

    path = Path(profile_mappings_path)
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            mappings = json.load(fh)
    else:
        mappings = {}
    mappings.setdefault("filename_patterns", [])
    mappings["filename_patterns"].append(
        {
            "pattern": csv_path.name,
            "profile": str(profile.get("id") or profile.get("account_id")),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mappings, indent=2, sort_keys=True), encoding="utf-8")


def _csv_headers(csv_path: Path) -> set[str]:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            return {header.strip() for header in next(reader)}
        except StopIteration:
            return set()


def _import_csv(
    csv_path: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
    input_root: Path,
) -> list[dict[str, str]]:
    csv_settings = profile.get("csv", {})
    columns = dict(csv_settings.get("columns", {}))
    columns["debit_values"] = csv_settings.get("debit_values", [])
    columns["credit_values"] = csv_settings.get("credit_values", [])
    columns["amount_default_sign"] = csv_settings.get("amount_default_sign", "")
    rows: list[dict[str, str]] = []

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_number, source_row in enumerate(reader, start=2):
            rows.append(
                _normalized_row(
                    source_row=source_row,
                    row_number=row_number,
                    profile=profile,
                    config=config,
                    input_path=csv_path,
                    input_root=input_root,
                    columns=columns,
                )
            )

    return rows


def _import_pdf(
    pdf_path: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
    input_root: Path,
) -> tuple[list[dict[str, str]], list[str]]:
    import pdfplumber

    pdf_settings = profile.get("pdf", {})
    columns = dict(pdf_settings.get("columns", {}))
    columns["debit_values"] = pdf_settings.get("debit_values", [])
    columns["credit_values"] = pdf_settings.get("credit_values", [])
    columns["amount_default_sign"] = pdf_settings.get("amount_default_sign", "")
    has_header = pdf_settings.get("has_header", True)
    required_columns = set(pdf_settings.get("required_columns", []))
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            tables = _pdf_tables(page)
            if not tables:
                warnings.append(f"No table found on {pdf_path.name} page {page_number}")
                text_length = _pymupdf_page_text_length(pdf_path, page_number)
                if text_length is not None:
                    warnings.append(
                        "PyMuPDF text fallback found "
                        f"{text_length} characters on {pdf_path.name} page {page_number}"
                    )
                continue
            for table in tables:
                header = [str(cell or "").strip() for cell in table[0]] if has_header else []
                if required_columns and not required_columns.issubset(set(header)):
                    warnings.append(
                        "Skipped table on "
                        f"{pdf_path.name} page {page_number} because required columns were missing"
                    )
                    continue
                data_rows = table[1:] if has_header else table
                start_row = 2 if has_header else 1
                for table_row_number, cells in enumerate(data_rows, start=start_row):
                    expanded_rows = _expand_pdf_cells(
                        cells, header, has_header, pdf_settings
                    )
                    for expanded_index, expanded_cells in enumerate(expanded_rows, start=1):
                        row_number = (
                            f"{table_row_number}.{expanded_index}"
                            if len(expanded_rows) > 1
                            else table_row_number
                        )
                        source_row = _pdf_source_row(expanded_cells, header, has_header)
                        source_row = _apply_pdf_row_regex(source_row, pdf_settings)
                        if source_row is None:
                            continue
                        rows.append(
                            _normalized_row(
                                source_row=source_row,
                                row_number=row_number,
                                profile=profile,
                                config=config,
                                input_path=pdf_path,
                                input_root=input_root,
                                columns=columns,
                                source_page=str(page_number),
                            )
                        )
    return rows, warnings


def _pymupdf_page_text_length(pdf_path: Path, page_number: int) -> int | None:
    try:
        import fitz
    except ImportError:
        return None

    try:
        with fitz.open(str(pdf_path)) as document:
            page = document[page_number - 1]
            return len(_clean_text(page.get_text()))
    except Exception:
        return None


def _apply_pdf_row_regex(
    source_row: dict[str, str], pdf_settings: dict[str, Any]
) -> dict[str, str] | None:
    row_regex = pdf_settings.get("row_regex")
    if not row_regex:
        return source_row

    row_text = " ".join(value for value in source_row.values() if value).strip()
    match = re.search(str(row_regex), row_text)
    if match is None:
        return None
    return {key: _clean_text(value) for key, value in match.groupdict().items()}


def _expand_pdf_cells(
    cells: list[Any],
    header: list[str],
    has_header: bool,
    pdf_settings: dict[str, Any],
) -> list[list[Any]]:
    if not pdf_settings.get("split_multiline_rows", False):
        return [cells]

    split_cells = [str(cell or "").splitlines() for cell in cells]
    row_count_columns = pdf_settings.get("split_multiline_row_count_columns", [])
    row_count_indexes = _pdf_row_count_indexes(
        row_count_columns, header, has_header, len(split_cells)
    )
    row_count_source = (
        [split_cells[index] for index in row_count_indexes]
        if row_count_indexes
        else split_cells
    )
    row_count = max((len(lines) for lines in row_count_source), default=0)
    if row_count <= 1:
        return [cells]

    expanded_rows = []
    for row_index in range(row_count):
        expanded_rows.append(
            [
                _clean_text(lines[row_index]) if row_index < len(lines) else ""
                for lines in split_cells
            ]
        )
    return expanded_rows


def _pdf_row_count_indexes(
    row_count_columns: list[Any],
    header: list[str],
    has_header: bool,
    cell_count: int,
) -> list[int]:
    indexes = []
    for column in row_count_columns:
        if has_header and isinstance(column, str):
            try:
                index = header.index(column)
            except ValueError:
                continue
        else:
            try:
                index = int(column)
            except (TypeError, ValueError):
                continue
        if 0 <= index < cell_count:
            indexes.append(index)
    return indexes


def _pdf_tables(page: Any) -> list[list[list[Any]]]:
    if hasattr(page, "extract_tables"):
        tables = page.extract_tables()
        return [table for table in tables if table]
    table = page.extract_table()
    return [table] if table else []


def _pdf_source_row(
    cells: list[Any], header: list[str], has_header: bool
) -> dict[str, str]:
    if has_header:
        return {
            header[index]: _clean_text(cell)
            for index, cell in enumerate(cells)
            if index < len(header)
        }
    return {str(index): _clean_text(cell) for index, cell in enumerate(cells)}


def _normalized_row(
    source_row: dict[str, str],
    row_number: int | str,
    profile: dict[str, Any],
    config: dict[str, Any],
    input_path: Path,
    input_root: Path,
    columns: dict[str, str],
    source_page: str = "",
) -> dict[str, str]:
    transaction_date = _normalize_date(
        _value(source_row, columns.get("transaction_date")), profile
    )
    posting_date = _normalize_date(_value(source_row, columns.get("posting_date")), profile)
    canonical_date = transaction_date or posting_date
    description = _value(source_row, columns.get("description"))
    merchant = _value(source_row, columns.get("merchant")) or description
    original_currency = (
        _value(source_row, columns.get("original_currency"))
        or profile.get("account_currency", "")
    ).upper()
    invalid_amount_columns: list[str] = []
    original_amount = _signed_amount(source_row, columns, invalid_amount_columns)
    posted_currency = (
        _value(source_row, columns.get("posted_currency"))
        or original_currency
        or profile.get("account_currency", "")
    ).upper()
    posted_amount = _posted_amount(
        source_row, columns, original_amount, invalid_amount_columns
    )
    amount_hkd, amount_flags, amount_reason = _amount_hkd(
        posted_amount, posted_currency, config
    )

    flags = ["uncategorized"]
    if amount_flags:
        flags.extend(amount_flags)
    if invalid_amount_columns:
        flags.append("invalid_amount")
        amount_reason = _append_reason(
            amount_reason,
            f"Invalid amount in {', '.join(_unique(invalid_amount_columns))}",
        )

    return {
        "transaction_id": "",
        "date": canonical_date,
        "transaction_date": transaction_date,
        "posting_date": posting_date,
        "account_id": str(profile.get("account_id", "")),
        "account": str(profile.get("account", "")),
        "institution": str(profile.get("institution", "")),
        "country": str(profile.get("country", "")),
        "original_amount": _format_decimal(original_amount),
        "original_currency": original_currency,
        "posted_amount": _format_decimal(posted_amount),
        "posted_currency": posted_currency,
        "amount_hkd": _format_decimal(amount_hkd) if amount_hkd is not None else "",
        "merchant": merchant,
        "original_description": description,
        "category": "Unknown",
        "owner": str(profile.get("owner", "Household")),
        "payment_method": str(profile.get("payment_method", "Unknown")),
        "confidence": "0.00",
        "needs_review": "true",
        "reason": amount_reason or "No categorization rules have been applied",
        "flags": ";".join(flags),
        "notes": "Imported from PDF" if source_page else "",
        "source_file": _relative_source(input_path, input_root),
        "source_page": source_page,
        "source_row": str(row_number),
    }


def _assign_transaction_ids(transactions: list[dict[str, str]]) -> list[dict[str, str]]:
    base_counts: dict[str, int] = {}
    for transaction in transactions:
        base = _transaction_identity_base(transaction)
        base_counts[base] = base_counts.get(base, 0) + 1

    seen: dict[str, int] = {}
    for transaction in transactions:
        base = _transaction_identity_base(transaction)
        seen[base] = seen.get(base, 0) + 1
        suffix = f":{seen[base]}" if base_counts[base] > 1 else ""
        digest = hashlib.sha256(f"{base}{suffix}".encode("utf-8")).hexdigest()[:16]
        transaction["transaction_id"] = f"txn_{digest}"
        if base_counts[base] > 1:
            transaction["flags"] = _append_flag(
                transaction["flags"], "duplicate_identity_collision"
            )
    return transactions


def _transaction_identity_base(transaction: dict[str, str]) -> str:
    fields = [
        "account_id",
        "date",
        "transaction_date",
        "posting_date",
        "original_amount",
        "original_currency",
        "posted_amount",
        "posted_currency",
        "merchant",
        "original_description",
    ]
    return "|".join(_normalize_identity_part(transaction.get(field, "")) for field in fields)


def _normalize_identity_part(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _count_flag(transactions: list[dict[str, str]], flag: str) -> int:
    return sum(
        1
        for transaction in transactions
        if flag in [item for item in transaction.get("flags", "").split(";") if item]
    )


def _transaction_flags(transactions: list[dict[str, str]]) -> dict[str, list[str]]:
    flagged: dict[str, list[str]] = {}
    for transaction in transactions:
        flags = sorted(item for item in transaction.get("flags", "").split(";") if item)
        if flags:
            flagged[transaction["transaction_id"]] = flags
    return flagged


def _transaction_diagnostics(
    transactions: list[dict[str, str]]
) -> dict[str, dict[str, str | bool]]:
    diagnostics: dict[str, dict[str, str | bool]] = {}
    for transaction in transactions:
        if transaction.get("needs_review") != "true" and not transaction.get("reason"):
            continue
        diagnostics[transaction["transaction_id"]] = {
            "needs_review": transaction.get("needs_review") == "true",
            "reason": transaction.get("reason", ""),
            "category": transaction.get("category", ""),
            "owner": transaction.get("owner", ""),
        }
    return diagnostics


def _default_profile() -> dict[str, Any]:
    return {
        "account_id": "",
        "account": "",
        "institution": "",
        "country": "",
        "account_currency": "",
        "owner": "Household",
        "payment_method": "Unknown",
    }


def _value(row: dict[str, str], column: str | None) -> str:
    if column is None or column == "":
        return ""
    return _clean_text(row.get(str(column)))


def _clean_text(value: Any) -> str:
    text = str(value or "")
    cleaned = "".join(
        character
        for character in text
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    return cleaned.strip()


def _normalize_date(value: str, profile: dict[str, Any]) -> str:
    if not value:
        return ""

    date_formats = profile.get("date_formats", ["%Y-%m-%d"])
    for date_format in date_formats:
        try:
            parsed = datetime.strptime(value, date_format).date()
        except ValueError:
            continue
        if "%Y" not in date_format and "%y" not in date_format:
            statement_year = profile.get("statement_year")
            if statement_year:
                parsed = parsed.replace(year=int(statement_year))
        return parsed.isoformat()
    return value


def _signed_amount(
    row: dict[str, str], columns: dict[str, str], invalid_columns: list[str]
) -> Decimal:
    amount_column = columns.get("amount")
    if amount_column:
        raw_amount = _value(row, amount_column)
        amount = _parse_decimal(raw_amount, invalid_columns, amount_column)
        return _apply_amount_sign(raw_amount, amount, row, columns)

    debit_column = columns.get("debit")
    credit_column = columns.get("credit")
    debit = _parse_decimal(_value(row, debit_column), invalid_columns, debit_column)
    credit = _parse_decimal(_value(row, credit_column), invalid_columns, credit_column)
    if debit != Decimal("0"):
        return -abs(debit)
    if credit != Decimal("0"):
        return abs(credit)
    return Decimal("0")


def _posted_amount(
    row: dict[str, str],
    columns: dict[str, str],
    fallback: Decimal,
    invalid_columns: list[str],
) -> Decimal:
    posted_column = columns.get("posted_amount")
    if posted_column:
        raw_amount = _value(row, posted_column)
        amount = _parse_decimal(raw_amount, invalid_columns, posted_column)
        return _apply_amount_sign(raw_amount, amount, row, columns)
    return fallback


def _apply_amount_sign(
    raw_amount: str, amount: Decimal, row: dict[str, str], columns: dict[str, str]
) -> Decimal:
    indicator = _normalize_identity_part(_value(row, columns.get("credit_debit")))
    debit_values = {
        _normalize_identity_part(value) for value in columns.get("debit_values", [])
    }
    credit_values = {
        _normalize_identity_part(value) for value in columns.get("credit_values", [])
    }
    if indicator and indicator in debit_values:
        return -abs(amount)
    if indicator and indicator in credit_values:
        return abs(amount)
    if _amount_has_sign_suffix(raw_amount):
        return amount
    if columns.get("amount_default_sign") == "expense":
        return -abs(amount)
    if columns.get("amount_default_sign") == "income":
        return abs(amount)
    return amount


def _amount_hkd(
    amount: Decimal, currency: str, config: dict[str, Any]
) -> tuple[Decimal | None, list[str], str]:
    base_currency = str(config.get("base_currency", "HKD")).upper()
    if currency == base_currency:
        return amount, [], ""

    rate = config.get("exchange_rates", {}).get(currency)
    if rate is None:
        return None, ["missing_exchange_rate"], f"Missing exchange rate for {currency}"

    return amount * Decimal(str(rate)), [], ""


def _parse_decimal(
    value: str, invalid_columns: list[str] | None = None, column: str | None = None
) -> Decimal:
    if not value:
        return Decimal("0")
    cleaned = value.replace(",", "").strip()
    upper_cleaned = cleaned.upper()
    if upper_cleaned.endswith("CR"):
        return abs(_parse_decimal(cleaned[:-2], invalid_columns, column))
    if upper_cleaned.endswith("DR"):
        return -abs(_parse_decimal(cleaned[:-2], invalid_columns, column))
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        if invalid_columns is not None and column:
            invalid_columns.append(str(column))
        return Decimal("0")
    if not parsed.is_finite():
        if invalid_columns is not None and column:
            invalid_columns.append(str(column))
        return Decimal("0")
    return parsed


def _amount_has_sign_suffix(value: str) -> bool:
    upper_value = value.replace(",", "").strip().upper()
    return upper_value.endswith("CR") or upper_value.endswith("DR")


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _relative_source(path: Path, input_root: Path) -> str:
    root = input_root if input_root.is_dir() else input_root.parent
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _to_review_row(row: dict[str, str]) -> dict[str, str]:
    review_row = {column: row.get(column, "") for column in REVIEW_NEEDED_COLUMNS}
    review_row["suggested_category"] = row.get("category", "")
    review_row["suggested_owner"] = row.get("owner", "")
    review_row["suggested_payment_method"] = row.get("payment_method", "")
    review_row["category"] = ""
    review_row["owner"] = ""
    review_row["payment_method"] = ""
    return review_row


def _apply_corrections(
    transactions: list[dict[str, str]], corrections: dict[str, dict[str, str]]
) -> None:
    for transaction in transactions:
        correction = corrections.get(transaction["transaction_id"])
        if not correction:
            continue

        for field in ["category", "owner", "payment_method", "confidence", "reason", "notes"]:
            if field in correction:
                transaction[field] = correction[field]

        transaction["needs_review"] = correction.get("needs_review", "false").casefold()
        transaction["flags"] = _append_flag(transaction["flags"], "manual_correction")


def _annotate_duplicate_suspicions(transactions: list[dict[str, str]]) -> None:
    duplicate_keys: dict[str, int] = {}
    for transaction in transactions:
        key = _duplicate_key(transaction)
        duplicate_keys[key] = duplicate_keys.get(key, 0) + 1

    for transaction in transactions:
        if duplicate_keys[_duplicate_key(transaction)] > 1:
            _mark_duplicate(transaction)

    near_date_groups: dict[str, list[dict[str, str]]] = {}
    for transaction in transactions:
        near_date_groups.setdefault(_duplicate_key_without_date(transaction), []).append(
            transaction
        )

    for group in near_date_groups.values():
        for index, transaction in enumerate(group):
            transaction_date = _parse_iso_date(transaction.get("date", ""))
            if transaction_date is None:
                continue
            for other in group[index + 1 :]:
                other_date = _parse_iso_date(other.get("date", ""))
                if other_date is None:
                    continue
                if abs((transaction_date - other_date).days) <= 1:
                    _mark_duplicate(transaction)
                    _mark_duplicate(other)


def _duplicate_key(transaction: dict[str, str]) -> str:
    fields = [
        "date",
        "amount_hkd",
        "original_amount",
        "original_currency",
        "merchant",
        "original_description",
    ]
    return "|".join(_normalize_identity_part(transaction.get(field, "")) for field in fields)


def _duplicate_key_without_date(transaction: dict[str, str]) -> str:
    fields = [
        "amount_hkd",
        "original_amount",
        "original_currency",
        "merchant",
        "original_description",
    ]
    return "|".join(_normalize_identity_part(transaction.get(field, "")) for field in fields)


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _mark_duplicate(transaction: dict[str, str]) -> None:
    transaction["needs_review"] = "true"
    transaction["flags"] = _append_flag(transaction["flags"], "duplicate_suspected")
    transaction["reason"] = _append_reason(
        transaction["reason"], "Possible duplicate transaction"
    )


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing}; {reason}"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(item for item in existing.split(";") if item and item != flag)


def _write_csv(
    path: Path, columns: list[str], rows: list[dict[str, str]] | None = None
) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows or [])


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def run() -> int:
    try:
        return main()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
