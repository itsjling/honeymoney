"""Statement discovery, profile selection, parsing, and identity inputs."""

from __future__ import annotations

import csv
import json
import logging
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

from honeymoney.identity import (
    AllocationLocator,
    IncomingRecordIdentity,
    IncomingSourceIdentity,
    extractor_contract_id,
    logical_locator,
    source_id,
    source_namespace_id,
    source_revision,
)
from honeymoney.normalization import (
    _clean_text,
    _date_format_has_year,
    _default_profile,
    _normalized_row,
    _parse_decimal,
    _parse_profile_date,
)
from honeymoney.schema import (
    ALLOWED_ACCOUNT_TYPES,
    allowed_owners,
    allowed_payment_methods,
)


def _relative_source(path: Path, input_root: Path) -> str:
    root = input_root if input_root.is_dir() else input_root.parent
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


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
    if not isinstance(profile, dict):
        raise ValueError(f"Profile {profile_path.name} must be a JSON object")
    raw_id = profile.get("id")
    if raw_id is not None and (not isinstance(raw_id, str) or not raw_id.strip()):
        raise ValueError(
            f"Profile {profile_path.name} field profile.id must be a non-empty string"
        )
    profile_id = str(raw_id or profile.get("account_id") or profile_path.name)
    if (
        not isinstance(profile.get("account_id"), str)
        or not profile["account_id"].strip()
    ):
        raise ValueError(
            f"Missing required profile fields in profile {profile_id}: account_id"
        )
    for field in (
        "account",
        "institution",
        "country",
        "account_currency",
        "owner",
        "payment_method",
    ):
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Profile {profile_id} field profile.{field} must be a non-empty string"
            )
    if profile.get("owner") and profile["owner"] not in allowed_owners(config):
        raise ValueError(
            f"Unsupported owner in profile {profile_id}: {profile['owner']}"
        )
    if profile.get("payment_method") and profile[
        "payment_method"
    ] not in allowed_payment_methods(config):
        raise ValueError(
            f"Unsupported payment_method in profile {profile_id}: "
            f"{profile['payment_method']}"
        )
    if (
        profile.get("account_type")
        and profile["account_type"] not in ALLOWED_ACCOUNT_TYPES
    ):
        raise ValueError(
            f"Unsupported account_type in profile {profile_id}: "
            f"{profile['account_type']}"
        )
    parser_fields = [field for field in ("csv", "pdf") if field in profile]
    if len(parser_fields) != 1:
        raise ValueError(f"Profile {profile_id} must define exactly one of csv or pdf")
    date_formats = profile.get("date_formats", ["%Y-%m-%d"])
    if not isinstance(date_formats, list) or not date_formats:
        raise ValueError(
            f"Profile {profile_id} field profile.date_formats must be a non-empty JSON array"
        )
    for index, date_format in enumerate(date_formats):
        if not isinstance(date_format, str) or not date_format.strip():
            raise ValueError(
                f"Profile {profile_id} field profile.date_formats[{index}] must be a non-empty string"
            )
        try:
            validation_year = 2024
            rendered = datetime(validation_year, 2, 28).strftime(date_format)
            _parse_profile_date(rendered, date_format, fallback_year=validation_year)
        except ValueError as error:
            raise ValueError(
                f"Profile {profile_id} field profile.date_formats[{index}] must be a valid date format"
            ) from error
        if "%d" not in date_format or not any(
            directive in date_format for directive in ("%m", "%b", "%B")
        ):
            raise ValueError(
                f"Profile {profile_id} field profile.date_formats[{index}] must include day and month"
            )
        if not _date_format_has_year(date_format) and "statement_year" not in profile:
            raise ValueError(
                f"Profile {profile_id} date format {date_format!r} requires profile.statement_year"
            )
    if "statement_year" in profile and (
        isinstance(profile["statement_year"], bool)
        or not isinstance(profile["statement_year"], int)
        or not 1 <= profile["statement_year"] <= 9999
    ):
        raise ValueError(
            f"Profile {profile_id} field profile.statement_year must be an integer from 1 to 9999"
        )

    parser = parser_fields[0]
    settings = profile[parser]
    if not isinstance(settings, dict):
        raise ValueError(
            f"Profile {profile_id} field profile.{parser} must be a JSON object"
        )
    columns = settings.get("columns")
    if not isinstance(columns, dict):
        raise ValueError(
            f"Profile {profile_id} field {parser}.columns must be a JSON object"
        )
    for field, source in columns.items():
        if not isinstance(field, str) or not field.strip():
            raise ValueError(
                f"Profile {profile_id} field {parser}.columns keys must be non-empty strings"
            )
        if (
            not isinstance(source, (str, int))
            or isinstance(source, bool)
            or str(source).strip() == ""
        ):
            raise ValueError(
                f"Profile {profile_id} field {parser}.columns.{field} must be a non-empty string or column index"
            )
    if not any(
        columns.get(field) not in (None, "")
        for field in ("transaction_date", "posting_date")
    ):
        raise ValueError(
            f"Profile {profile_id} field {parser}.columns.transaction_date or posting_date is required"
        )
    if not columns.get("description"):
        raise ValueError(
            f"Profile {profile_id} field {parser}.columns.description is required"
        )
    _validate_profile_amount_strategy(profile_id, parser, settings, columns)
    _validate_profile_sign_settings(profile_id, parser, settings, columns)
    if parser == "csv":
        _validate_csv_profile(profile_id, settings)
    else:
        _validate_pdf_profile(profile_id, settings)


def _validate_profile_amount_strategy(
    profile_id: str,
    parser: str,
    settings: dict[str, Any],
    columns: dict[str, Any],
) -> None:
    direct = bool(columns.get("amount"))
    debit = bool(columns.get("debit"))
    credit = bool(columns.get("credit"))
    if not direct and not debit and not credit:
        raise ValueError(
            f"Profile {profile_id} field {parser}.columns must define an amount strategy"
        )
    if direct and (debit or credit):
        raise ValueError(
            f"Profile {profile_id} must define exactly one amount strategy in {parser}.columns"
        )
    if not direct and not (debit and credit):
        raise ValueError(
            f"Profile {profile_id} debit/credit amount strategy requires both {parser}.columns.debit and credit"
        )


def _validate_profile_sign_settings(
    profile_id: str,
    parser: str,
    settings: dict[str, Any],
    columns: dict[str, Any],
) -> None:
    default_sign = settings.get("amount_default_sign")
    if default_sign is not None and default_sign not in {"", "expense", "income"}:
        raise ValueError(
            f"Profile {profile_id} field {parser}.amount_default_sign must be expense or income"
        )
    indicator = columns.get("credit_debit")
    debit_values = settings.get("debit_values", [])
    credit_values = settings.get("credit_values", [])
    for field, values in (
        ("debit_values", debit_values),
        ("credit_values", credit_values),
    ):
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(
                f"Profile {profile_id} field {parser}.{field} must be an array of non-empty strings"
            )
    if indicator and (not debit_values or not credit_values):
        raise ValueError(
            f"Profile {profile_id} field {parser}.columns.credit_debit requires debit_values and credit_values"
        )
    if not indicator and (debit_values or credit_values):
        raise ValueError(
            f"Profile {profile_id} fields {parser}.debit_values and credit_values require columns.credit_debit"
        )


def _validate_csv_profile(profile_id: str, settings: dict[str, Any]) -> None:
    for field, source in settings["columns"].items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError(
                f"Profile {profile_id} field csv.columns.{field} must be a non-empty string"
            )
    headers = settings.get("detect_headers")
    if headers is None:
        return
    if not isinstance(headers, list) or not headers:
        raise ValueError(
            f"Profile {profile_id} field csv.detect_headers must be a non-empty JSON array"
        )
    for index, header in enumerate(headers):
        if not isinstance(header, str) or not header.strip():
            raise ValueError(
                f"Profile {profile_id} field csv.detect_headers[{index}] must be a non-empty string"
            )


def _validate_pdf_profile(profile_id: str, settings: dict[str, Any]) -> None:
    if "parser" in settings and settings.get("parser") != "pdfplumber":
        raise ValueError(f"Profile {profile_id} field pdf.parser must be pdfplumber")
    for field in ("has_header", "word_rows_only", "split_multiline_rows"):
        if field in settings and not isinstance(settings[field], bool):
            raise ValueError(
                f"Profile {profile_id} field pdf.{field} must be a boolean"
            )
    compiled_row_regex: re.Pattern[str] | None = None
    row_regex = settings.get("row_regex")
    if row_regex is not None:
        if not isinstance(row_regex, str) or not row_regex.strip():
            raise ValueError(
                f"Profile {profile_id} field pdf.row_regex must be a non-empty string"
            )
        try:
            compiled_row_regex = re.compile(row_regex)
        except re.error as error:
            raise ValueError(
                f"Profile {profile_id} field pdf.row_regex must be a valid regular expression"
            ) from error
    word_rows = settings.get("word_rows", False)
    if not (
        isinstance(word_rows, bool)
        or isinstance(word_rows, str)
        and word_rows == "sectioned"
    ):
        raise ValueError(
            f"Profile {profile_id} field pdf.word_rows must be a boolean or sectioned"
        )
    if word_rows is True:
        _validate_pdf_bounds_map(
            profile_id, "pdf.word_columns", settings.get("word_columns")
        )
        missing_sources = sorted(
            str(source)
            for source in settings["columns"].values()
            if source not in settings["word_columns"]
        )
        if missing_sources:
            raise ValueError(
                f"Profile {profile_id} pdf.columns map missing word columns: "
                + ", ".join(missing_sources)
            )
        markers = settings.get("word_header_markers")
        if (
            not isinstance(markers, list)
            or not markers
            or any(
                not isinstance(marker, str) or not marker.strip() for marker in markers
            )
        ):
            raise ValueError(
                f"Profile {profile_id} field pdf.word_header_markers must be a non-empty JSON array"
            )
    elif word_rows == "sectioned":
        _validate_sectioned_pdf_profile(profile_id, settings.get("sectioned_word_rows"))
    elif compiled_row_regex is not None:
        join_fields = settings.get("join_fields", {})
        if not isinstance(join_fields, dict):
            raise ValueError(
                f"Profile {profile_id} field pdf.join_fields must be a JSON object"
            )
        for field, sources in join_fields.items():
            if (
                not isinstance(field, str)
                or not field.strip()
                or not isinstance(sources, list)
                or not sources
                or any(not isinstance(source, str) or not source for source in sources)
            ):
                raise ValueError(
                    f"Profile {profile_id} field pdf.join_fields.{field} must be a non-empty string array"
                )
        available_sources = set(compiled_row_regex.groupindex) | set(join_fields)
        missing_sources = sorted(
            str(source)
            for source in settings["columns"].values()
            if isinstance(source, str) and source not in available_sources
        )
        if missing_sources:
            raise ValueError(
                f"Profile {profile_id} pdf.columns map missing row-regex groups: "
                + ", ".join(missing_sources)
            )


def _validate_pdf_bounds_map(profile_id: str, field: str, value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Profile {profile_id} field {field} must be a JSON object")
    for name, bounds in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(bounds, list)
            or len(bounds) != 2
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not Decimal(str(item)).is_finite()
                for item in bounds
            )
            or bounds[0] >= bounds[1]
        ):
            raise ValueError(
                f"Profile {profile_id} field {field}.{name} must be two increasing numbers"
            )


def _validate_sectioned_pdf_profile(profile_id: str, value: Any) -> None:
    field = "pdf.sectioned_word_rows"
    if not isinstance(value, dict):
        raise ValueError(f"Profile {profile_id} field {field} must be a JSON object")
    accounts = value.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError(
            f"Profile {profile_id} field {field}.accounts must be a non-empty JSON object"
        )
    for section, account in accounts.items():
        account_field = f"{field}.accounts.{section}"
        if (
            not isinstance(section, str)
            or not section.strip()
            or not isinstance(account, dict)
        ):
            raise ValueError(
                f"Profile {profile_id} field {account_field} must be a JSON object"
            )
        for account_key in ("account_id", "account"):
            if (
                not isinstance(account.get(account_key), str)
                or not account[account_key].strip()
            ):
                raise ValueError(
                    f"Profile {profile_id} field {account_field}.{account_key} must be a non-empty string"
                )
        currency_from_row = account.get("currency_from_row", False)
        if not isinstance(currency_from_row, bool):
            raise ValueError(
                f"Profile {profile_id} field {account_field}.currency_from_row must be a boolean"
            )
        if not currency_from_row and (
            not isinstance(account.get("currency"), str)
            or not account["currency"].strip()
        ):
            raise ValueError(
                f"Profile {profile_id} field {account_field}.currency must be a non-empty string"
            )
    _validate_pdf_bounds_map(profile_id, f"{field}.columns", value.get("columns"))
    missing_columns = {"description", "deposit", "withdrawal"} - set(value["columns"])
    if missing_columns:
        raise ValueError(
            f"Profile {profile_id} field {field}.columns is missing: "
            + ", ".join(sorted(missing_columns))
        )
    markers = value.get("header_markers")
    if (
        not isinstance(markers, list)
        or not markers
        or any(not isinstance(marker, str) or not marker.strip() for marker in markers)
    ):
        raise ValueError(
            f"Profile {profile_id} field {field}.header_markers must be a non-empty string array"
        )
    compiled: dict[str, re.Pattern[str]] = {}
    for regex_field in (
        "date_regex",
        "currency_regex",
        "statement_year_regex",
        "statement_year_filename_regex",
    ):
        pattern = value.get(regex_field)
        if pattern is None:
            continue
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(
                f"Profile {profile_id} field {field}.{regex_field} must be a non-empty string"
            )
        try:
            compiled[regex_field] = re.compile(pattern)
        except re.error as error:
            raise ValueError(
                f"Profile {profile_id} field {field}.{regex_field} must be a valid regular expression"
            ) from error
    if "date_regex" not in compiled or not {"day", "month"}.issubset(
        compiled["date_regex"].groupindex
    ):
        raise ValueError(
            f"Profile {profile_id} field {field}.date_regex must define day and month groups"
        )
    if not value.get("statement_year_regex") and not value.get(
        "statement_year_filename_regex"
    ):
        raise ValueError(
            f"Profile {profile_id} field {field} requires a statement-year regex"
        )
    for year_field in ("statement_year_regex", "statement_year_filename_regex"):
        if year_field in compiled and "year" not in compiled[year_field].groupindex:
            raise ValueError(
                f"Profile {profile_id} field {field}.{year_field} must define a year group"
            )


def _load_profile_mappings(config: dict[str, Any]) -> dict[str, Any]:
    mapping_path = config.get("profile_mappings")
    if not mapping_path:
        return {}
    if not Path(mapping_path).exists():
        return {}
    with Path(mapping_path).open(encoding="utf-8") as fh:
        mappings = json.load(fh)
    if not isinstance(mappings, dict):
        raise ValueError("Profile mappings document must be a JSON object")
    patterns = mappings.get("filename_patterns", [])
    if not isinstance(patterns, list):
        raise ValueError(
            "Profile mappings field filename_patterns must be a JSON array"
        )
    for index, mapping in enumerate(patterns):
        if not isinstance(mapping, dict):
            raise ValueError(
                f"Profile mappings field filename_patterns[{index}] must be a JSON object"
            )
        for field in ("pattern", "profile"):
            if not isinstance(mapping.get(field), str) or not mapping[field].strip():
                raise ValueError(
                    f"Profile mappings field filename_patterns[{index}].{field} must be a non-empty string"
                )
    return mappings


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
    *,
    include_identity_sources: bool = False,
    status: Callable[[str], None] | None = None,
    clear_status: Callable[[], None] | None = None,
) -> (
    tuple[list[dict[str, str]], list[str], list[dict[str, str]]]
    | tuple[
        list[dict[str, str]],
        list[str],
        list[dict[str, str]],
        tuple[IncomingSourceIdentity, ...],
    ]
):
    """Import rows and, when requested, their private identity inputs."""
    status = status or (lambda _message: None)
    clear_status = clear_status or (lambda: None)
    transactions: list[dict[str, str]] = []
    warnings: list[str] = []
    file_reports: list[dict[str, str]] = []
    identity_sources: list[IncomingSourceIdentity] = []
    for file_number, input_file in enumerate(input_files, start=1):
        status(
            f"Importing statements... ({file_number}/{len(input_files)}) {input_file.name}"
        )
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
                    clear_status,
                )
                if include_identity_sources:
                    imported, pdf_warnings, records = _import_pdf(
                        input_file,
                        profile,
                        config,
                        input_root,
                        include_identity_records=True,
                    )
                else:
                    imported, pdf_warnings = _import_pdf(
                        input_file, profile, config, input_root
                    )
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
                warning = f"PDF parsing failed for {_relative_source(input_file, input_root)}: {error}"
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
            if include_identity_sources:
                identity_sources.append(
                    _incoming_source_identity(
                        input_file,
                        profile,
                        config,
                        _pdf_adapter_tag(profile),
                        records,
                        input_root,
                    )
                )
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
        profile, prompted_for_profile = _select_csv_profile(
            input_file,
            profiles,
            interactive,
            profile_mappings,
            clear_status,
        )
        if include_identity_sources:
            imported, records = _import_csv(
                input_file,
                profile,
                config,
                input_root,
                include_identity_records=True,
            )
        else:
            imported = _import_csv(input_file, profile, config, input_root)
        if prompted_for_profile:
            _maybe_save_profile_mapping(input_file, profile, profile_mappings_path)
        transactions.extend(imported)
        if include_identity_sources:
            identity_sources.append(
                _incoming_source_identity(
                    input_file, profile, config, 1, records, input_root
                )
            )
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
    if include_identity_sources:
        return transactions, warnings, file_reports, tuple(identity_sources)
    return transactions, warnings, file_reports


def _incoming_source_identity(
    input_file: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
    adapter_tag: int,
    records: tuple[IncomingRecordIdentity, ...],
    input_root: Path,
) -> IncomingSourceIdentity:
    """Build private, immutable identity inputs for one processed source."""
    workspace_root = config.get("_identity_workspace_root")
    if not isinstance(workspace_root, Path):
        workspace_root = Path("config.json").resolve().parent
    locator_kind, locator = logical_locator(input_file, workspace_root)
    return IncomingSourceIdentity(
        stable_handle=str(input_file.resolve(strict=True)),
        source_display=_relative_source(input_file, input_root),
        namespace_id=source_namespace_id(locator_kind, locator),
        revision=source_revision(input_file.read_bytes()),
        contract_id=extractor_contract_id(adapter_tag, profile),
        record_data=records,
    )


def _candidate_source_ids(
    input_files: list[Path], input_root: Path, config: dict[str, Any]
) -> dict[str, str]:
    """Return report-safe candidate IDs without retaining private locators."""
    workspace_root = config.get("_identity_workspace_root")
    if not isinstance(workspace_root, Path):
        workspace_root = Path("config.json").resolve().parent
    candidates: dict[str, str] = {}
    for input_file in input_files:
        locator_kind, locator = logical_locator(input_file, workspace_root)
        candidates[_relative_source(input_file, input_root)] = source_id(
            source_namespace_id(locator_kind, locator)
        )
    return candidates


def _identity_diagnostic_warning(diagnostic: Any) -> str:
    """Format the resolver's safe diagnostic without exposing identity inputs."""
    count = getattr(diagnostic, "affected_count", None)
    if count is None:
        count = getattr(diagnostic, "candidate_count", 0)
    return (
        f"{diagnostic.code}: {diagnostic.source_display}; "
        f"action={diagnostic.action}; count={count}; {diagnostic.remediation}"
    )


def _pdf_adapter_tag(profile: dict[str, Any]) -> int:
    pdf_settings = profile.get("pdf", {})
    if pdf_settings.get("word_rows") == "sectioned":
        return 4
    if pdf_settings.get("word_rows"):
        return 3
    return 2


def _select_pdf_profile(
    pdf_path: Path,
    profiles: list[dict[str, Any]],
    interactive: bool,
    profile_mappings: dict[str, Any],
    profile_mappings_path: str | None,
    clear_status: Callable[[], None],
) -> dict[str, Any]:
    if not profiles:
        return _default_profile()

    mapped_profile = _mapped_profile(pdf_path, profiles, profile_mappings)
    if mapped_profile is not None:
        return mapped_profile

    if len(profiles) > 1:
        if not interactive:
            raise ValueError(f"Could not detect profile for {pdf_path.name}")
        return _prompt_for_profile(
            pdf_path, profiles, profile_mappings_path, clear_status
        )

    return profiles[0]


def _select_csv_profile(
    csv_path: Path,
    profiles: list[dict[str, Any]],
    interactive: bool,
    profile_mappings: dict[str, Any],
    clear_status: Callable[[], None],
) -> tuple[dict[str, Any], bool]:
    if not profiles:
        return _default_profile(), False

    mapped_profile = _mapped_profile(csv_path, profiles, profile_mappings)
    if mapped_profile is not None:
        return mapped_profile, False

    headers = _csv_headers(csv_path)
    matching_profiles = []
    for profile in profiles:
        required_headers = profile.get("csv", {}).get("detect_headers", [])
        if required_headers and set(required_headers).issubset(headers):
            matching_profiles.append(profile)

    if len(matching_profiles) == 1:
        return matching_profiles[0], False
    if len(matching_profiles) > 1:
        if not interactive:
            labels = ", ".join(
                str(profile.get("id") or profile.get("account_id") or "unknown")
                for profile in matching_profiles
            )
            raise ValueError(
                f"Ambiguous profile detection for {csv_path.name}: {labels}"
            )
        return (
            _prompt_for_profile(csv_path, matching_profiles, None, clear_status),
            True,
        )

    if len(profiles) > 1:
        if not interactive:
            raise ValueError(f"Could not detect profile for {csv_path.name}")
        return _prompt_for_profile(csv_path, profiles, None, clear_status), True

    return profiles[0], False


def _mapped_profile(
    source_path: Path, profiles: list[dict[str, Any]], mappings: dict[str, Any]
) -> dict[str, Any] | None:
    profiles_by_id = {
        str(profile.get("id") or profile.get("account_id")): profile
        for profile in profiles
    }
    for mapping in mappings.get("filename_patterns", []):
        if fnmatch(source_path.name, str(mapping.get("pattern", ""))):
            return profiles_by_id.get(str(mapping.get("profile", "")))
    return None


def _prompt_for_profile(
    csv_path: Path,
    profiles: list[dict[str, Any]],
    profile_mappings_path: str | None,
    clear_status: Callable[[], None],
) -> dict[str, Any]:
    clear_status()
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


def _skip_descriptions(profile: dict[str, Any]) -> list[str]:
    patterns = profile.get("skip_descriptions", [])
    return [str(pattern).casefold() for pattern in patterns if str(pattern).strip()]


def _is_balance_transaction_row(row: dict[str, str]) -> bool:
    haystacks = [
        row.get("original_description", ""),
        row.get("merchant", ""),
    ]
    return any(
        re.search(
            r"\b(?:opening|closing|previous)\s+balance\b",
            haystack,
            flags=re.IGNORECASE,
        )
        for haystack in haystacks
        if haystack
    )


def _row_is_skipped(row: dict[str, str], skip_patterns: list[str]) -> bool:
    if _is_balance_transaction_row(row):
        return True
    if not skip_patterns:
        return False
    haystacks = [
        row.get("original_description", "").casefold(),
        row.get("merchant", "").casefold(),
    ]
    return any(
        pattern in haystack
        for pattern in skip_patterns
        for haystack in haystacks
        if haystack
    )


def _import_csv(
    csv_path: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
    input_root: Path,
    *,
    include_identity_records: bool = False,
) -> (
    list[dict[str, str]]
    | tuple[list[dict[str, str]], tuple[IncomingRecordIdentity, ...]]
):
    csv_settings = profile.get("csv", {})
    _validate_selected_csv_headers(csv_path, profile, csv_settings)
    columns = dict(csv_settings.get("columns", {}))
    columns["debit_values"] = csv_settings.get("debit_values", [])
    columns["credit_values"] = csv_settings.get("credit_values", [])
    columns["amount_default_sign"] = csv_settings.get("amount_default_sign", "")
    skip_patterns = _skip_descriptions(profile)
    rows: list[dict[str, str]] = []
    identity_records: list[IncomingRecordIdentity] = []

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        # DictReader consumes its header lazily. Read it before deriving each
        # record's start so the first data record correctly starts on line 2.
        if reader.fieldnames is None:
            return (rows, tuple(identity_records)) if include_identity_records else rows
        row_number = 2
        while True:
            physical_row = reader.line_num + 1
            try:
                source_row = next(reader)
            except StopIteration:
                break
            normalized = _normalized_row(
                source_row=source_row,
                row_number=row_number,
                profile=profile,
                config=config,
                columns=columns,
                source_file=_relative_source(csv_path, input_root),
            )
            if _row_is_skipped(normalized, skip_patterns):
                row_number += 1
                continue
            rows.append(normalized)
            if include_identity_records:
                identity_records.append(
                    IncomingRecordIdentity(
                        normalized, AllocationLocator(1, (physical_row,))
                    )
                )
            row_number += 1

    if include_identity_records:
        return rows, tuple(identity_records)
    return rows


def _validate_selected_csv_headers(
    csv_path: Path, profile: dict[str, Any], settings: dict[str, Any]
) -> None:
    headers = _csv_headers(csv_path)
    profile_id = str(profile.get("id") or profile.get("account_id") or "unknown")
    for field, mapped_header in settings.get("columns", {}).items():
        if str(mapped_header) not in headers:
            raise ValueError(
                f"Profile {profile_id} field csv.columns.{field} maps to missing "
                f"header {mapped_header!r} in {csv_path.name}"
            )


def _import_pdf(
    pdf_path: Path,
    profile: dict[str, Any],
    config: dict[str, Any],
    input_root: Path,
    *,
    include_identity_records: bool = False,
) -> (
    tuple[list[dict[str, str]], list[str]]
    | tuple[list[dict[str, str]], list[str], tuple[IncomingRecordIdentity, ...]]
):
    import pdfplumber

    pdf_settings = profile.get("pdf", {})
    columns = dict(pdf_settings.get("columns", {}))
    columns["debit_values"] = pdf_settings.get("debit_values", [])
    columns["credit_values"] = pdf_settings.get("credit_values", [])
    columns["amount_default_sign"] = pdf_settings.get("amount_default_sign", "")
    has_header = pdf_settings.get("has_header", True)
    required_columns = set(pdf_settings.get("required_columns", []))
    skip_patterns = _skip_descriptions(profile)
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    identity_records: list[IncomingRecordIdentity] = []
    with _quiet_pdfminer_font_warnings():
        with pdfplumber.open(str(pdf_path)) as pdf:
            if pdf_settings.get("word_rows") == "sectioned":
                source_rows = _pdf_sectioned_word_source_rows(
                    pdf, pdf_path, pdf_settings
                )
                for source_row, page_number, row_number in source_rows:
                    normalized = _normalized_row(
                        source_row=source_row,
                        row_number=row_number,
                        profile=profile,
                        config=config,
                        columns=columns,
                        source_file=_relative_source(pdf_path, input_root),
                        source_page=str(page_number),
                    )
                    if _row_is_skipped(normalized, skip_patterns):
                        continue
                    rows.append(normalized)
                    if include_identity_records:
                        identity_records.append(
                            IncomingRecordIdentity(
                                normalized,
                                AllocationLocator(4, (page_number, row_number)),
                            )
                        )
                if include_identity_records:
                    return rows, warnings, tuple(identity_records)
                return rows, warnings

            for page_number, page in enumerate(pdf.pages, start=1):
                word_rows = _pdf_word_source_rows(
                    page, pdf_settings, include_physical_lines=include_identity_records
                )
                if word_rows is not None:
                    for row_number, item in enumerate(word_rows, start=1):
                        if include_identity_records:
                            source_row, physical_line = item
                        else:
                            source_row = item
                        normalized = _normalized_row(
                            source_row=source_row,
                            row_number=row_number,
                            profile=profile,
                            config=config,
                            columns=columns,
                            source_file=_relative_source(pdf_path, input_root),
                            source_page=str(page_number),
                        )
                        if _row_is_skipped(normalized, skip_patterns):
                            continue
                        rows.append(normalized)
                        if include_identity_records:
                            identity_records.append(
                                IncomingRecordIdentity(
                                    normalized,
                                    AllocationLocator(3, (page_number, physical_line)),
                                )
                            )
                    continue

                if pdf_settings.get("word_rows_only", False) and hasattr(
                    page, "extract_words"
                ):
                    continue

                tables = _pdf_tables(page)
                if not tables:
                    warnings.append(
                        f"No table found on {pdf_path.name} page {page_number}"
                    )
                    text_length = _pymupdf_page_text_length(pdf_path, page_number)
                    if text_length is not None:
                        warnings.append(
                            "PyMuPDF text fallback found "
                            f"{text_length} characters on {pdf_path.name} page {page_number}"
                        )
                    continue
                for table_number, table in enumerate(tables, start=1):
                    header = (
                        [str(cell or "").strip() for cell in table[0]]
                        if has_header
                        else []
                    )
                    if required_columns and not required_columns.issubset(set(header)):
                        warnings.append(
                            "Skipped table on "
                            f"{pdf_path.name} page {page_number} because required columns were missing"
                        )
                        continue
                    data_rows = table[1:] if has_header else table
                    start_row = 2 if has_header else 1
                    for table_row_number, cells in enumerate(
                        data_rows, start=start_row
                    ):
                        expanded_rows = _expand_pdf_cells(
                            cells, header, has_header, pdf_settings
                        )
                        for expanded_index, expanded_cells in enumerate(
                            expanded_rows, start=1
                        ):
                            row_number = (
                                f"{table_row_number}.{expanded_index}"
                                if len(expanded_rows) > 1
                                else table_row_number
                            )
                            source_row = _pdf_source_row(
                                expanded_cells, header, has_header
                            )
                            source_row = _apply_pdf_row_regex(source_row, pdf_settings)
                            if source_row is None:
                                continue
                            normalized = _normalized_row(
                                source_row=source_row,
                                row_number=row_number,
                                profile=profile,
                                config=config,
                                columns=columns,
                                source_file=_relative_source(pdf_path, input_root),
                                source_page=str(page_number),
                            )
                            if _row_is_skipped(normalized, skip_patterns):
                                continue
                            rows.append(normalized)
                            if include_identity_records:
                                identity_records.append(
                                    IncomingRecordIdentity(
                                        normalized,
                                        AllocationLocator(
                                            2,
                                            (
                                                page_number,
                                                table_number,
                                                table_row_number,
                                                expanded_index,
                                            ),
                                        ),
                                    )
                                )
    if pdf_settings.get("word_rows_only", False) and not rows:
        warnings.append(f"No word transaction table found in {pdf_path.name}")
    if include_identity_records:
        return rows, warnings, tuple(identity_records)
    return rows, warnings


def _pdf_sectioned_word_source_rows(
    pdf: Any, pdf_path: Path, pdf_settings: dict[str, Any]
) -> list[tuple[dict[str, str], int, int]]:
    settings = pdf_settings.get("sectioned_word_rows", {})
    if not isinstance(settings, dict):
        raise ValueError("PDF sectioned_word_rows settings must be an object")

    page_lines = [
        _pdf_word_lines(
            page.extract_words(x_tolerance=1, y_tolerance=3) or [],
            float(pdf_settings.get("word_y_tolerance", 3)),
        )
        for page in pdf.pages
    ]
    statement_date = _pdf_sectioned_statement_date(page_lines, pdf_path, settings)
    accounts = settings.get("accounts", {})
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("PDF sectioned_word_rows requires account sections")

    rows: list[tuple[dict[str, str], int, int]] = []
    current_date = ""
    account_dates: dict[str, str] = {}
    current_currency = ""
    account_currencies: dict[str, str] = {}
    current_account: dict[str, Any] | None = None
    description_parts: list[str] = []
    in_table = False
    date_pattern = re.compile(str(settings.get("date_regex", "")))
    amount_pattern = re.compile(
        str(settings.get("amount_regex", r"^-?\d[\d,]*\.\d{2}$"))
    )
    currency_pattern = re.compile(str(settings.get("currency_regex", r"^[A-Z]{3}$")))
    columns = settings.get("columns", {})
    if not isinstance(columns, dict):
        raise ValueError("PDF sectioned word columns must be an object")
    skip_descriptions = [
        str(marker).casefold()
        for marker in settings.get("skip_descriptions", [])
        if str(marker).strip()
    ]
    table_end_descriptions = [
        str(marker).casefold()
        for marker in settings.get("table_end_descriptions", [])
        if str(marker).strip()
    ]

    for page_number, lines in enumerate(page_lines, start=1):
        for line_number, line in enumerate(lines, start=1):
            text = " ".join(str(word.get("text", "")) for word in line).strip()
            folded = " ".join(text.casefold().split())

            matched_account = next(
                (
                    account
                    for marker, account in accounts.items()
                    if str(marker).casefold() in folded
                ),
                None,
            )
            if isinstance(matched_account, dict):
                current_account = {
                    "account_id": str(matched_account.get("account_id", "")),
                    "account": str(matched_account.get("account", "")),
                    "currency": str(matched_account.get("currency", "")),
                    "currency_from_row": bool(
                        matched_account.get("currency_from_row", False)
                    ),
                }
                current_date = account_dates.get(current_account["account_id"], "")
                current_currency = account_currencies.get(
                    current_account["account_id"], current_account["currency"]
                )
                in_table = False
                description_parts = []
                continue

            if _pdf_line_has_marker(folded, settings.get("section_end_markers", [])):
                current_account = None
                current_currency = ""
                in_table = False
                description_parts = []
                continue

            if current_account is None:
                continue
            if _pdf_line_has_all_markers(folded, settings.get("header_markers", [])):
                in_table = True
                description_parts = []
                continue
            if not in_table:
                continue

            currencies = [
                currency.upper()
                for currency in _pdf_word_texts_in_bounds(line, columns.get("currency"))
                if currency_pattern.fullmatch(currency)
            ]
            if currencies:
                current_currency = currencies[0]
                account_currencies[current_account["account_id"]] = current_currency

            date_match = date_pattern.match(text)
            if date_match is not None:
                current_date = _pdf_sectioned_date(date_match, statement_date)
                account_dates[current_account["account_id"]] = current_date

            description = _pdf_words_in_bounds(line, columns.get("description"))
            if description:
                description_parts.append(description)
            joined_description = " ".join(description_parts).strip()
            folded_description = joined_description.casefold()
            if any(marker in folded_description for marker in skip_descriptions):
                description_parts = []
                if any(
                    marker in folded_description for marker in table_end_descriptions
                ):
                    in_table = False
                continue

            deposits = _pdf_amounts_in_bounds(
                line, columns.get("deposit"), amount_pattern
            )
            withdrawals = _pdf_amounts_in_bounds(
                line, columns.get("withdrawal"), amount_pattern
            )
            if not deposits and not withdrawals:
                continue
            if deposits and withdrawals:
                raise ValueError(
                    "Both deposit and withdrawal found on "
                    f"{pdf_path.name} page {page_number} row {line_number}"
                )
            amount = deposits[0] if deposits else withdrawals[0]
            if _parse_decimal(amount) == Decimal("0"):
                description_parts = []
                continue
            if not current_date:
                raise ValueError(
                    "Amount found before a transaction date on "
                    f"{pdf_path.name} page {page_number} row {line_number}"
                )
            if current_account["currency_from_row"] and not current_currency:
                raise ValueError(
                    "Amount found before a transaction currency on "
                    f"{pdf_path.name} page {page_number} row {line_number}"
                )

            rows.append(
                (
                    {
                        "Date": current_date,
                        "Description": joined_description or "Bank transaction",
                        "Deposit": deposits[0] if deposits else "",
                        "Withdrawal": withdrawals[0] if withdrawals else "",
                        "Account ID": current_account["account_id"],
                        "Account": current_account["account"],
                        "Currency": current_currency,
                    },
                    page_number,
                    line_number,
                )
            )
            description_parts = []

    return rows


def _pdf_sectioned_statement_date(
    page_lines: list[list[list[dict[str, Any]]]],
    pdf_path: Path,
    settings: dict[str, Any],
) -> date:
    text_pattern = settings.get("statement_year_regex")
    if text_pattern:
        for lines in page_lines:
            for line in lines:
                text = " ".join(str(word.get("text", "")) for word in line)
                match = re.search(str(text_pattern), text)
                if match is not None:
                    return _pdf_statement_date_from_match(match)

    filename_pattern = settings.get("statement_year_filename_regex")
    if filename_pattern:
        match = re.search(str(filename_pattern), pdf_path.name)
        if match is not None:
            return _pdf_statement_date_from_match(match)

    raise ValueError(f"Could not determine statement year for {pdf_path.name}")


def _pdf_statement_date_from_match(match: re.Match[str]) -> date:
    groups = match.groupdict()
    year = int(groups["year"])
    day = int(groups.get("statement_day") or 31)
    month_value = groups.get("statement_month")
    if not month_value:
        return date(year, 12, day)
    if month_value.isdigit():
        return date(year, int(month_value), day)
    for date_format in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(f"{day} {month_value} {year}", date_format).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported statement month: {month_value}")


def _pdf_sectioned_date(match: re.Match[str], statement_date: date) -> str:
    parsed = datetime.strptime(
        f"{match.group('day')} {match.group('month')} {statement_date.year}",
        "%d %b %Y",
    ).date()
    if parsed.month > statement_date.month:
        parsed = parsed.replace(year=parsed.year - 1)
    return parsed.isoformat()


def _pdf_line_has_marker(text: str, markers: Any) -> bool:
    return any(str(marker).casefold() in text for marker in markers)


def _pdf_line_has_all_markers(text: str, markers: Any) -> bool:
    normalized_markers = [str(marker).casefold() for marker in markers]
    return bool(normalized_markers) and all(
        marker in text for marker in normalized_markers
    )


def _pdf_words_in_bounds(words: list[dict[str, Any]], bounds: Any) -> str:
    return " ".join(_pdf_word_texts_in_bounds(words, bounds)).strip()


def _pdf_word_texts_in_bounds(words: list[dict[str, Any]], bounds: Any) -> list[str]:
    if not isinstance(bounds, list) or len(bounds) != 2:
        return []
    left, right = float(bounds[0]), float(bounds[1])
    return [
        str(word.get("text", ""))
        for word in words
        if left <= float(word.get("x0", 0)) < right
    ]


def _pdf_amounts_in_bounds(
    words: list[dict[str, Any]], bounds: Any, pattern: re.Pattern[str]
) -> list[str]:
    return [
        text
        for text in _pdf_word_texts_in_bounds(words, bounds)
        if pattern.fullmatch(text)
    ]


def _pdf_word_source_rows(
    page: Any,
    pdf_settings: dict[str, Any],
    *,
    include_physical_lines: bool = False,
) -> list[dict[str, str]] | list[tuple[dict[str, str], int]] | None:
    if not pdf_settings.get("word_rows", False) or not hasattr(page, "extract_words"):
        return None

    word_columns = pdf_settings.get("word_columns", {})
    if not isinstance(word_columns, dict):
        return None

    words = page.extract_words(x_tolerance=1, y_tolerance=3) or []
    lines = _pdf_word_lines(words, float(pdf_settings.get("word_y_tolerance", 3)))
    if not lines:
        return None

    rows: list[dict[str, str]] | list[tuple[dict[str, str], int]] = []
    in_table = False
    for physical_line, line in enumerate(lines, start=1):
        text = " ".join(str(word.get("text", "")) for word in line).strip()
        if not in_table:
            in_table = _pdf_word_header_seen(text, pdf_settings)
            continue
        if _pdf_word_table_end_seen(text, pdf_settings):
            break

        source_row = _pdf_word_row(line, word_columns)
        if not any(source_row.values()):
            continue
        if not (
            source_row.get("Post date", "").strip()
            or source_row.get("Trans date", "").strip()
        ):
            continue
        if include_physical_lines:
            rows.append((source_row, physical_line))
        else:
            rows.append(source_row)
    return rows if in_table else None


def _pdf_word_lines(
    words: list[dict[str, Any]], y_tolerance: float
) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(
        words, key=lambda item: (float(item.get("top", 0)), float(item.get("x0", 0)))
    ):
        top = float(word.get("top", 0))
        if lines and abs(top - float(lines[-1][0].get("top", 0))) <= y_tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda item: float(item.get("x0", 0))) for line in lines]


def _pdf_word_header_seen(text: str, pdf_settings: dict[str, Any]) -> bool:
    markers = pdf_settings.get(
        "word_header_markers",
        ["Post date", "Trans date", "Description", "Amount"],
    )
    folded = " ".join(text.casefold().split())
    return all(str(marker).casefold() in folded for marker in markers)


def _pdf_word_table_end_seen(text: str, pdf_settings: dict[str, Any]) -> bool:
    markers = pdf_settings.get("word_table_end_markers", [])
    folded = text.casefold()
    return any(str(marker).casefold() in folded for marker in markers)


def _pdf_word_row(
    words: list[dict[str, Any]], word_columns: dict[str, Any]
) -> dict[str, str]:
    row: dict[str, str] = {}
    for column, bounds in word_columns.items():
        if not isinstance(bounds, list) or len(bounds) != 2:
            continue
        left, right = float(bounds[0]), float(bounds[1])
        row[str(column)] = " ".join(
            str(word.get("text", ""))
            for word in words
            if left <= float(word.get("x0", 0)) < right
        ).strip()
    return row


@contextmanager
def _quiet_pdfminer_font_warnings() -> Any:
    logger = logging.getLogger("pdfminer.pdffont")
    previous_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


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
    match = re.search(str(row_regex), row_text, flags=re.DOTALL)
    if match is None:
        return None
    row = {key: _clean_text(value) for key, value in match.groupdict().items()}
    return _join_pdf_regex_fields(row, pdf_settings)


def _join_pdf_regex_fields(
    source_row: dict[str, str], pdf_settings: dict[str, Any]
) -> dict[str, str]:
    join_fields = pdf_settings.get("join_fields", {})
    if not isinstance(join_fields, dict):
        return source_row

    for target, fields in join_fields.items():
        if not isinstance(fields, list):
            continue
        joined = " ".join(
            source_row.get(str(field), "").strip()
            for field in fields
            if source_row.get(str(field), "").strip()
        )
        if joined:
            source_row[str(target)] = " ".join(_clean_text(joined).split())
    return source_row


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
