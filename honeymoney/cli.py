from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import logging
import re
import shutil
import sys
import webbrowser
from contextlib import contextmanager
from importlib import resources
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
from honeymoney.report import build_report_html
from honeymoney.rules import apply_rules, load_rules
from honeymoney.ollama import apply_ollama_fallback, OllamaProgress


class _StatusLine:
    """A single terminal line that updates in place; silent when not a TTY."""

    def __init__(self, stream: Any = None, enabled: bool | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = self._stream.isatty() if enabled is None else enabled
        self._last_length = 0

    def update(self, text: str) -> None:
        if not self._enabled:
            return
        width = shutil.get_terminal_size().columns
        if width > 1 and len(text) >= width:
            text = text[: width - 2] + "…"
        padding = " " * max(0, self._last_length - len(text))
        self._stream.write(f"\r{text}{padding}")
        self._stream.flush()
        self._last_length = len(text)

    def clear(self) -> None:
        if not self._enabled or not self._last_length:
            return
        self._stream.write("\r" + " " * self._last_length + "\r")
        self._stream.flush()
        self._last_length = 0


_status = _StatusLine()


def _ollama_progress(progress: OllamaProgress) -> None:
    range_label = (
        str(progress.start_index)
        if progress.start_index == progress.end_index
        else f"{progress.start_index}-{progress.end_index}"
    )
    elapsed = f", {progress.elapsed_seconds:.0f}s" if progress.elapsed_seconds else ""
    _status.update(
        "Categorizing via Ollama... "
        f"batch {progress.batch_number}/{progress.batch_count} "
        f"(transactions {range_label} of {progress.total}{elapsed})"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"help", "--help", "-h"}:
        print(_help_text())
        return 0
    if argv and argv[0] == "setup":
        return _setup_command(argv[1:])
    if argv and argv[0] == "import":
        return _import_command(argv[1:])
    if argv and argv[0] == "status":
        return _status_command(argv[1:])
    if argv and argv[0] == "report":
        return _report_command(argv[1:])
    if argv and argv[0] == "run":
        argv = argv[1:]
    return _run_pipeline(argv)


def _run_pipeline(argv: list[str], print_import_summary: bool = False) -> int:
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
    _status.update("Applying categorization rules...")
    rules = load_rules(config)
    apply_rules(transactions, rules, config)
    _status.update("Checking for duplicates...")
    _annotate_duplicate_suspicions(transactions)
    ollama_report, ollama_warnings = apply_ollama_fallback(
        transactions, config, progress=_ollama_progress
    )
    if ollama_warnings:
        _status.clear()
        for warning in ollama_warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    _status.update("Applying corrections...")
    corrections = _load_corrections(config)
    _apply_corrections(transactions, corrections)
    _status.clear()
    if not args.no_interactive:
        categorized_interactively = _prompt_uncategorized(transactions, config)
        _save_interactive_corrections(categorized_interactively, config)
    review_rows = [row for row in transactions if row["needs_review"] == "true"]

    _status.update("Writing output files...")
    ledger_rows = _merge_into_ledger(categorized_path, transactions)
    _write_csv(categorized_path, CATEGORIZED_COLUMNS, ledger_rows)
    _write_csv(
        review_needed_path,
        REVIEW_NEEDED_COLUMNS,
        [_to_review_row(row) for row in ledger_rows if row.get("needs_review") == "true"],
    )
    report = {
        "status": "partial_success" if import_warnings else "success",
        "input_count": len(input_files),
        "transaction_count": len(transactions),
        "successful_record_count": len(transactions),
        "unsuccessful_record_count": _unsuccessful_record_count(file_reports),
        "review_count": len(review_rows),
        "uncategorized_count": _uncategorized_count(transactions),
        "duplicate_count": _count_flag(transactions, "duplicate_suspected"),
        "strict": args.strict,
        "interactive": not args.no_interactive,
        "output": {
            "categorized_csv": str(categorized_path),
            "review_needed_csv": str(review_needed_path),
            "import_report_json": str(import_report_path),
        },
        "ledger": {
            "transaction_count": len(ledger_rows),
            "review_count": sum(
                1 for row in ledger_rows if row.get("needs_review") == "true"
            ),
            "uncategorized_count": _uncategorized_count(ledger_rows),
        },
        "files": file_reports,
        "transaction_flags": _transaction_flags(transactions),
        "transaction_diagnostics": _transaction_diagnostics(transactions),
        "warnings": import_warnings + ollama_warnings,
        "errors": [],
        "ollama": ollama_report,
    }
    _write_report(import_report_path, report)
    _status.clear()

    if print_import_summary:
        _print_import_summary(report)

    if args.strict and import_warnings:
        return 1
    return 0


def _help_text() -> str:
    return """Honeymoney

Commands:
  honeymoney setup                 Create a local starter workspace
  honeymoney run                   Process configured CSV/PDF exports
  honeymoney import [PATH]         Import a pasted CSV/PDF path
  honeymoney status [MONTH]        Show processed/categorized counts for a period
  honeymoney report                Open a web report of recorded transactions
  honeymoney help                  Show this help

Common run options:
  --config config.json
  --input DIR_OR_FILE
  --output output/categorized.csv
  --strict
  --no-interactive                 Skip categorization and profile prompts

Common status/report options:
  --month june | --month 2026-06
  --start 2026-06-01 --end 2026-06-30
  --no-open                        (report) Write the HTML without opening it
"""


def _setup_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="honeymoney setup",
        description="Create a starter Honeymoney workspace.",
    )
    parser.add_argument("--root")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    root = _setup_root(args.root)
    _write_starter_workspace(root, force=args.force)
    print(f"Created Honeymoney workspace at {root}")
    print("")
    print("Next:")
    print(f"  1. Put exported CSV/PDF files in {root / 'input'}")
    print(f"  2. Edit {root / 'config.json'} and {root / 'rules.json'} as needed")
    print(f"  3. Run cd {root} && honeymoney run")
    return 0


def _setup_root(root_arg: str | None) -> Path:
    if root_arg:
        return Path(root_arg).expanduser().resolve()
    try:
        value = input("Root folder [./money]: ").strip()
    except EOFError:
        value = ""
    return Path(value or "./money").expanduser().resolve()


def _import_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="honeymoney import",
        description="Import one pasted CSV/PDF file or folder path.",
    )
    parser.add_argument("path", nargs="?")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--no-interactive", action="store_true")
    args = parser.parse_args(argv)

    input_path = args.path
    if not input_path:
        input_path = input("Paste a CSV/PDF file or folder path: ")
    input_path = _clean_pasted_path(input_path)
    if not input_path:
        raise ValueError("No import path provided")

    run_args = ["--input", input_path]
    if args.config_path:
        run_args.extend(["--config", args.config_path])
    if args.output_path:
        run_args.extend(["--output", args.output_path])
    if args.strict:
        run_args.append("--strict")
    if args.no_interactive:
        run_args.append("--no-interactive")
    return _run_pipeline(run_args, print_import_summary=True)


def _print_import_summary(report: dict[str, Any]) -> None:
    print(
        "Import complete: "
        f"{report['successful_record_count']} successful records, "
        f"{report['unsuccessful_record_count']} unsuccessful records"
    )
    uncategorized = report.get("uncategorized_count", 0)
    if uncategorized:
        print(
            f"{uncategorized} records are still uncategorized; "
            "run `honeymoney status` to see totals or re-run without --no-interactive"
        )
    ledger = report.get("ledger", {})
    if ledger:
        print(
            f"Ledger now has {ledger['transaction_count']} records "
            f"({ledger['uncategorized_count']} uncategorized)"
        )


def _unsuccessful_record_count(file_reports: list[dict[str, str]]) -> int:
    return sum(
        1
        for file_report in file_reports
        if file_report.get("status") in {"failed", "skipped"}
    )


def _clean_pasted_path(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    return cleaned


def _status_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="honeymoney status",
        description="Show processed and categorized counts for a time period.",
    )
    parser.add_argument("period", nargs="?", help="Month name or YYYY-MM")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--config", dest="config_path")
    args = parser.parse_args(argv)

    start, end = _resolve_period(args.month or args.period, args.start, args.end)
    config = _load_config(args.config_path)
    categorized_path = Path(config["paths"]["output"])
    ledger_rows = _read_ledger(categorized_path)
    if not ledger_rows:
        print(f"No processed records found at {categorized_path}")
        print("Run `honeymoney import` or `honeymoney run` first.")
        return 0

    rows = _rows_in_period(ledger_rows, start, end)
    categorized = [row for row in rows if _is_categorized(row)]
    statements = {row.get("source_file", "") for row in rows if row.get("source_file")}
    review = [row for row in rows if row.get("needs_review") == "true"]

    print(f"Status for {start.isoformat()} to {end.isoformat()}")
    print(f"  Statements processed: {len(statements)}")
    print(f"  Records processed:    {len(rows)}")
    print(f"  Categorized:          {len(categorized)}")
    print(f"  Uncategorized:        {len(rows) - len(categorized)}")
    print(f"  Needs review:         {len(review)}")
    undated = sum(1 for row in ledger_rows if _parse_iso_date(row.get("date", "")) is None)
    outside = len(ledger_rows) - len(rows) - undated
    print(
        f"Ledger total: {len(ledger_rows)} records "
        f"({outside} outside this period, {undated} with unparseable dates)"
    )
    return 0


def _report_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="honeymoney report",
        description="Write and open an HTML report of recorded transactions.",
    )
    parser.add_argument("period", nargs="?", help="Month name or YYYY-MM")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path", help="Report HTML path")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config(args.config_path)
    categorized_path = Path(config["paths"]["output"])
    ledger_rows = _read_ledger(categorized_path)

    month = args.month or args.period
    if month or args.start or args.end:
        start, end = _resolve_period(month, args.start, args.end)
        rows = _rows_in_period(ledger_rows, start, end)
        period_label = f"{start.isoformat()} to {end.isoformat()}"
    else:
        rows = ledger_rows
        period_label = "All recorded transactions"

    report_path = Path(args.output_path or categorized_path.parent / "report.html")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report_html(rows, period_label), encoding="utf-8")
    print(f"Report written to {report_path} ({len(rows)} transactions)")
    if not args.no_open:
        webbrowser.open(report_path.resolve().as_uri())
    return 0


def _resolve_period(
    month: str | None, start: str | None, end: str | None, today: date | None = None
) -> tuple[date, date]:
    today = today or date.today()
    if month and (start or end):
        raise ValueError("Use either a month or --start/--end, not both")
    if month:
        return _month_period(month, today)
    if start or end:
        start_date = date.fromisoformat(start) if start else date.min
        end_date = date.fromisoformat(end) if end else today
        if start_date > end_date:
            raise ValueError(f"Start date {start_date} is after end date {end_date}")
        return start_date, end_date
    return _month_period(f"{today.year}-{today.month:02d}", today)


def _month_period(value: str, today: date) -> tuple[date, date]:
    text = value.strip().casefold()
    numeric = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if numeric:
        year, month = int(numeric.group(1)), int(numeric.group(2))
    else:
        month_names = {
            name.casefold(): index
            for index, name in enumerate(calendar.month_name)
            if name
        }
        month_names.update(
            {
                name.casefold(): index
                for index, name in enumerate(calendar.month_abbr)
                if name
            }
        )
        month = month_names.get(text, 0)
        year = today.year
    if not 1 <= month <= 12:
        raise ValueError(f"Unrecognized month: {value}")
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _read_ledger(categorized_path: Path) -> list[dict[str, str]]:
    if not categorized_path.exists():
        return []
    with categorized_path.open(newline="", encoding="utf-8") as fh:
        return [
            {column: row.get(column) or "" for column in CATEGORIZED_COLUMNS}
            for row in csv.DictReader(fh)
        ]


def _rows_in_period(
    rows: list[dict[str, str]], start: date, end: date
) -> list[dict[str, str]]:
    in_period = []
    for row in rows:
        row_date = _parse_iso_date(row.get("date", ""))
        if row_date is not None and start <= row_date <= end:
            in_period.append(row)
    return in_period


def _is_categorized(row: dict[str, str]) -> bool:
    return row.get("category", "") not in {"", "Unknown"}


def _uncategorized_count(rows: list[dict[str, str]]) -> int:
    return sum(1 for row in rows if not _is_categorized(row))


def _merge_into_ledger(
    categorized_path: Path, transactions: list[dict[str, str]]
) -> list[dict[str, str]]:
    merged = {
        row["transaction_id"]: row
        for row in _read_ledger(categorized_path)
        if row.get("transaction_id")
    }
    for transaction in transactions:
        merged[transaction["transaction_id"]] = transaction
    return list(merged.values())


def _prompt_uncategorized(
    transactions: list[dict[str, str]], config: dict[str, Any]
) -> list[dict[str, str]]:
    pending = [row for row in transactions if not _is_categorized(row)]
    if not pending:
        return []
    categories = sorted(
        category for category in allowed_categories(config) if category != "Unknown"
    )
    print(f"\n{len(pending)} imported records have no category.")
    print("Pick a category number, press Enter to skip one, or enter q to skip the rest.")
    _print_category_menu(categories)
    categorized = []
    for position, transaction in enumerate(pending, start=1):
        print(f"\n[{position}/{len(pending)}] {_transaction_prompt_line(transaction)}")
        while True:
            try:
                choice = input("Category [number/Enter/q]: ").strip().casefold()
            except EOFError:
                return categorized
            if choice == "":
                break
            if choice == "q":
                return categorized
            try:
                selected = int(choice)
            except ValueError:
                selected = 0
            if 1 <= selected <= len(categories):
                _apply_interactive_category(transaction, categories[selected - 1])
                categorized.append(transaction)
                break
            print("Enter a number from the list, Enter to skip, or q to stop.")
    return categorized


def _print_category_menu(categories: list[str], columns: int = 3) -> None:
    if not categories:
        return
    row_count = (len(categories) + columns - 1) // columns
    for row in range(row_count):
        cells = []
        for column in range(columns):
            index = column * row_count + row
            if index < len(categories):
                cells.append(f"{index + 1:>2}. {categories[index]:<22}")
        print("  " + "".join(cells).rstrip())


def _transaction_prompt_line(transaction: dict[str, str]) -> str:
    amount = transaction.get("posted_amount", "")
    currency = transaction.get("posted_currency", "")
    merchant = transaction.get("merchant", "")
    description = transaction.get("original_description", "")
    name = merchant or description or "(no description)"
    parts = [transaction.get("date", ""), f"{amount} {currency}".strip(), name]
    if description and description != name:
        parts.append(description)
    return "  ".join(part for part in parts if part)


def _apply_interactive_category(transaction: dict[str, str], category: str) -> None:
    transaction["category"] = category
    transaction["confidence"] = "1.00"
    transaction["needs_review"] = "false"
    transaction["reason"] = "Categorized interactively"
    transaction["flags"] = _remove_flag(transaction["flags"], "uncategorized")
    transaction["flags"] = _append_flag(transaction["flags"], "manual_correction")


def _save_interactive_corrections(
    categorized: list[dict[str, str]], config: dict[str, Any]
) -> None:
    corrections_path = config.get("corrections")
    if not corrections_path or not categorized:
        return
    path = Path(corrections_path)
    fieldnames = [
        "transaction_id",
        "category",
        "owner",
        "payment_method",
        "confidence",
        "reason",
        "notes",
    ]
    exists = path.exists()
    if exists:
        with path.open(newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), None)
        if header:
            fieldnames = header
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for transaction in categorized:
            writer.writerow(
                {
                    "transaction_id": transaction["transaction_id"],
                    "category": transaction["category"],
                    "confidence": "1.00",
                    "reason": "Categorized interactively",
                }
            )


def _write_starter_workspace(root: Path, force: bool) -> None:
    input_dir = root / "input"
    output_dir = root / "output"
    profiles_dir = root / "profiles"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir.mkdir(parents=True, exist_ok=True)

    profile_path = profiles_dir / "starter_csv.json"
    rules_path = root / "rules.json"
    corrections_path = root / "corrections.csv"
    profile_mappings_path = root / "profile_mappings.json"
    config_path = root / "config.json"

    _write_json_file(
        profile_path,
        {
            "id": "starter_csv",
            "account_id": "starter_csv",
            "account": "Starter CSV",
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
        },
        force,
    )
    starter_profile_paths = _copy_starter_profiles(profiles_dir, force)
    _write_json_file(profile_mappings_path, {"filename_patterns": []}, force)
    _write_json_file(rules_path, {"version": 1, "rules": []}, force)
    _write_text_file(
        corrections_path,
        "transaction_id,category,owner,payment_method,confidence,reason,notes\n",
        force,
    )
    _write_json_file(
        config_path,
        {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0, "USD": 7.8},
            "review_confidence_threshold": 0.8,
            "profiles": [str(profile_path)]
            + [str(path) for path in starter_profile_paths],
            "profile_mappings": str(profile_mappings_path),
            "rules": str(rules_path),
            "corrections": str(corrections_path),
            "pdf": {"enabled": True, "parser": "pdfplumber"},
            "ollama": {
                "enabled": False,
                "url": "http://localhost:11434/api/generate",
                "model": "qwen2.5:7b-instruct",
                "batch_size": 5,
                "timeout_seconds": 120,
            },
            "paths": {
                "input": str(input_dir),
                "output": str(output_dir / "categorized.csv"),
            },
        },
        force,
    )


def _copy_starter_profiles(profiles_dir: Path, force: bool) -> list[Path]:
    copied = []
    profile_resources = resources.files("honeymoney").joinpath("data/profiles")
    for resource in sorted(profile_resources.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".json"):
            continue
        destination = profiles_dir / resource.name
        _write_text_file(destination, resource.read_text(encoding="utf-8"), force)
        copied.append(destination)
    return copied


def _write_json_file(path: Path, data: dict[str, Any], force: bool) -> None:
    if path.exists() and not force:
        raise ValueError(f"Refusing to overwrite {path}; pass --force to replace it")
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _write_text_file(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        raise ValueError(f"Refusing to overwrite {path}; pass --force to replace it")
    path.write_text(content, encoding="utf-8")


def _load_config(config_path: str | None) -> dict[str, Any]:
    if config_path is None:
        default_config = Path("config.json")
        if default_config.exists():
            config_path = str(default_config)
        else:
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
    for file_number, input_file in enumerate(input_files, start=1):
        _status.update(
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
    _status.clear()
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


def _row_is_skipped(row: dict[str, str], skip_patterns: list[str]) -> bool:
    if not skip_patterns:
        return False
    haystacks = [
        row.get("original_description", "").casefold(),
        row.get("merchant", "").casefold(),
    ]
    return any(
        pattern in haystack for pattern in skip_patterns for haystack in haystacks if haystack
    )


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
    skip_patterns = _skip_descriptions(profile)
    rows: list[dict[str, str]] = []

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row_number, source_row in enumerate(reader, start=2):
            normalized = _normalized_row(
                source_row=source_row,
                row_number=row_number,
                profile=profile,
                config=config,
                input_path=csv_path,
                input_root=input_root,
                columns=columns,
            )
            if _row_is_skipped(normalized, skip_patterns):
                continue
            rows.append(normalized)

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
    skip_patterns = _skip_descriptions(profile)
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    with _quiet_pdfminer_font_warnings():
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
                        for expanded_index, expanded_cells in enumerate(
                            expanded_rows, start=1
                        ):
                            row_number = (
                                f"{table_row_number}.{expanded_index}"
                                if len(expanded_rows) > 1
                                else table_row_number
                            )
                            source_row = _pdf_source_row(expanded_cells, header, has_header)
                            source_row = _apply_pdf_row_regex(source_row, pdf_settings)
                            if source_row is None:
                                continue
                            normalized = _normalized_row(
                                source_row=source_row,
                                row_number=row_number,
                                profile=profile,
                                config=config,
                                input_path=pdf_path,
                                input_root=input_root,
                                columns=columns,
                                source_page=str(page_number),
                            )
                            if _row_is_skipped(normalized, skip_patterns):
                                continue
                            rows.append(normalized)
    return rows, warnings


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
        _status.clear()
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
