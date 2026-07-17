from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fnmatch import fnmatch
from importlib import resources
from pathlib import Path
from typing import Any

from honeymoney.classification_policy import (
    apply_structural_classification,
    validate_category_policies,
)
from honeymoney.corrections import (
    CORRECTION_FIELDS,
    apply_correction_operation,
    apply_corrections,
    ledger_output_documents,
    load_corrections,
    prepare_corrections_document,
    read_ledger,
    to_review_row,
    validate_correction,
)
from honeymoney.ollama import (
    OllamaProgress,
    apply_ollama_fallback,
    list_ollama_models,
    validate_ollama_endpoint,
)
from honeymoney.persistence import persist_generation, recover_generation
from honeymoney.reconciliation import (
    reconcile_ledger,
    reconciliation_date_window,
    transaction_direction,
)
from honeymoney.report import build_report_html
from honeymoney.rules import apply_rules, load_rules
from honeymoney.schema import (
    ALLOWED_ACCOUNT_TYPES,
    ALLOWED_FLOW_TYPES,
    allowed_categories,
    allowed_owners,
    allowed_payment_methods,
)

JSON_SCHEMA_VERSION = 1


def _emit_json(
    command: str,
    status: str,
    *,
    data: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    warnings: list[Any] | None = None,
    errors: list[Any] | None = None,
) -> None:
    print(
        json.dumps(
            {
                "schema_version": JSON_SCHEMA_VERSION,
                "command": command,
                "status": status,
                "data": data or {},
                "artifacts": artifacts or {},
                "warnings": warnings or [],
                "errors": errors or [],
            },
            sort_keys=True,
        )
    )


class _CommandArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, json_errors: bool = False, **kwargs: Any) -> None:
        self._json_errors = json_errors
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        if self._json_errors:
            raise ValueError(f"{self.prog}: {message}")
        super().error(message)


def _command_parser(argv: list[str], **kwargs: Any) -> _CommandArgumentParser:
    return _CommandArgumentParser(json_errors="--json" in argv, **kwargs)


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
    if argv and argv[0] == "pending":
        return _pending_command(argv[1:])
    if argv and argv[0] == "correct":
        return _correct_command(argv[1:])
    if argv and argv[0] == "report":
        return _report_command(argv[1:])
    if argv and argv[0] == "reconcile":
        return _reconcile_command(argv[1:])
    if argv and argv[0] == "review":
        return _review_command(argv[1:])
    if argv and argv[0] == "config":
        return _config_command(argv[1:])
    if argv and argv[0] == "run":
        argv = argv[1:]
    return _run_pipeline(argv)


def _run_pipeline(
    argv: list[str],
    print_import_summary: bool = False,
    json_command: str = "run",
) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney",
        description="Categorize local household transaction exports.",
    )
    parser.add_argument("--input", dest="input_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--no-interactive", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    interactive = not (args.no_interactive or args.json)

    config = _load_config(args.config_path)
    input_path = Path(args.input_path or config["paths"]["input"])
    if not input_path.exists():
        raise ValueError(f"Input path does not exist: {input_path}")
    categorized_path = Path(args.output_path or config["paths"]["output"])
    output_dir = categorized_path.parent
    review_needed_path = output_dir / "review_needed.csv"
    import_report_path = output_dir / "import_report.json"

    output_dir.mkdir(parents=True, exist_ok=True)

    input_files = _discover_input_files(input_path)
    profiles = _load_profiles(config)
    profile_mappings = _load_profile_mappings(config)
    source_files = {
        _relative_source(input_file, input_path) for input_file in input_files
    }
    existing_ledger_rows = read_ledger(categorized_path)
    existing_source_files = _processed_source_files(existing_ledger_rows, source_files)
    if existing_source_files and not (args.replace or args.reset):
        source_list = ", ".join(sorted(existing_source_files))
        raise ValueError(
            f"Already imported source file(s): {source_list}. "
            "Use --replace to re-import or --reset to re-import and clear corrections."
        )
    transactions, import_warnings, file_reports = _import_transactions(
        input_files,
        profiles,
        config,
        input_path,
        interactive=interactive,
        profile_mappings=profile_mappings,
        profile_mappings_path=config.get("profile_mappings"),
    )
    if args.reset:
        requested_action = "reset"
    elif args.replace:
        requested_action = "replace"
    else:
        requested_action = "import"
    for file_report in file_reports:
        file_report["requested_action"] = requested_action
        if file_report.get("status") != "processed":
            file_report["ledger_action"] = "preserved"
        elif args.reset:
            file_report["ledger_action"] = "reset"
        elif args.replace:
            file_report["ledger_action"] = "replaced"
        else:
            file_report["ledger_action"] = "added"
    replace_sources = (
        {
            file_report["source_file"]
            for file_report in file_reports
            if file_report.get("status") == "processed"
        }
        if args.replace or args.reset
        else None
    )
    reset_ids = (
        {
            row["transaction_id"]
            for row in existing_ledger_rows
            if row.get("source_file") in (replace_sources or set())
            and row.get("transaction_id")
        }
        if args.reset
        else set()
    )
    _status.update("Applying categorization rules...")
    rules = load_rules(config)
    apply_rules(transactions, rules, config)
    _status.update("Checking for duplicates...")
    _annotate_duplicate_suspicions(transactions)
    _status.update("Applying structural classifications...")
    structural_count = apply_structural_classification(transactions, config)
    ollama_report, ollama_warnings = apply_ollama_fallback(
        transactions, config, progress=_ollama_progress
    )
    if ollama_warnings:
        _status.clear()
        for warning in ollama_warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    _status.update("Applying corrections...")
    corrections = load_corrections(config)
    correction_documents: dict[Path, str] = {}
    if args.reset and config.get("corrections"):
        corrections_path, corrections_content, corrections = (
            prepare_corrections_document(config, removed_transaction_ids=reset_ids)
        )
        correction_documents[corrections_path] = corrections_content
    apply_corrections(transactions, corrections)
    _status.clear()
    if interactive:
        categorized_interactively = _prompt_uncategorized(transactions, config)
    else:
        categorized_interactively = []
    review_rows = [row for row in transactions if row["needs_review"] == "true"]

    _status.update("Writing output files...")
    ledger_rows = _merge_into_ledger(categorized_path, transactions, replace_sources)
    reconciliation = reconcile_ledger(ledger_rows, config)
    report = {
        "status": "partial_success" if import_warnings else "success",
        "input_count": len(input_files),
        "transaction_count": len(transactions),
        "successful_record_count": len(transactions),
        "unsuccessful_record_count": _unsuccessful_record_count(file_reports),
        "review_count": len(review_rows),
        "uncategorized_count": _uncategorized_count(transactions),
        "duplicate_count": _count_flag(transactions, "duplicate_suspected"),
        "categorization": {"structural_count": structural_count},
        "strict": args.strict,
        "interactive": interactive,
        "replace": args.replace or args.reset,
        "reset": args.reset,
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
        "reconciliation": reconciliation,
    }
    files = ledger_output_documents(categorized_path, ledger_rows)
    files[import_report_path] = json.dumps(report, indent=2, sort_keys=True) + "\n"
    files.update(correction_documents)
    files.update(
        _interactive_correction_documents(
            categorized_interactively,
            config,
            removed_transaction_ids=reset_ids,
        )
    )
    persist_generation(categorized_path, files)
    _status.clear()

    if print_import_summary:
        _print_import_summary(report)

    if args.json:
        _emit_json(
            json_command,
            report["status"],
            data=report,
            artifacts=report["output"],
            warnings=report["warnings"],
        )

    if args.strict and import_warnings:
        return 1
    return 0


def _help_text() -> str:
    return """Honeymoney

A local-first CLI for importing, categorizing, and reviewing household transactions.

Commands:
  honeymoney setup                 Create a local starter workspace
  honeymoney run                   Process configured CSV/PDF exports
  honeymoney import [PATH]         Import a pasted CSV/PDF path
  honeymoney review [--category CATEGORY]
                                   Review queued or category-matched transactions
  honeymoney review [FILTERS]      Review filtered accounting flow decisions
  honeymoney review --transaction ID --as income
                                   Apply one human accounting decision
  honeymoney status [MONTH]        Show processed/categorized counts for a period
  honeymoney pending [MONTH]       List transactions that need review
  honeymoney correct --file FILE   Apply validated transaction corrections
  honeymoney report [MONTH]        Open a web report for a period
  honeymoney reconcile             Recompute and inspect ledger transfers
  honeymoney config                View or edit config.json
  honeymoney help                  Show this help

Common run options:
  --config config.json
  --input DIR_OR_FILE
  --output output/categorized.csv
  --strict
  --no-interactive                 Skip categorization and profile prompts
  --replace                        Re-import a previously processed source file
  --reset                          Re-import and clear old corrections for the source
  --json                           Emit one machine-readable JSON document

Common status/report options:
  --month june | --month 2026-06
  --start 2026-06-01 --end 2026-06-30
  --no-open                        (report) Write the HTML without opening it

Review filters and decisions:
  --category CATEGORY              Compose with period, flow, and direction filters
  --flow unresolved --direction inflow
  --transaction ID --as DECISION  Non-interactive one-shot review
  --remember --yes                 Save an exact, directional income rule
  --json                           Valid only for a fully specified one-shot review
"""


def _config_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney config",
        description="View or edit the Honeymoney configuration.",
    )
    parser.add_argument("action", nargs="?", choices=["edit"])
    parser.add_argument("section", nargs="?", choices=["ollama"])
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--model")
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config_path = _existing_config_path(args.config_path)
    config = _read_config_document(config_path)
    _recover_config_generation(config)
    artifacts = {"config_json": str(config_path)}

    if args.action is None:
        if args.section or args.model or args.enable or args.disable:
            raise ValueError("Config changes require `honeymoney config edit`")
        if args.json:
            _emit_json(
                "config",
                "success",
                data={"config": config},
                artifacts=artifacts,
            )
        else:
            print(json.dumps(config, indent=2, sort_keys=True))
        return 0

    if args.section is None:
        if args.json:
            raise ValueError("honeymoney config edit does not support --json")
        if args.model or args.enable or args.disable:
            raise ValueError("Ollama options require `honeymoney config edit ollama`")
        _edit_config_in_editor(config_path)
        print(f"Updated {config_path}")
        return 0

    ollama_config = config.setdefault("ollama", {})
    if not isinstance(ollama_config, dict):
        raise ValueError("Config field ollama must be a JSON object")
    if args.disable and args.model:
        raise ValueError("Use either --disable or --model, not both")

    selected_model = args.model.strip() if args.model else None
    if args.model is not None and not selected_model:
        raise ValueError("--model must be a non-empty Ollama model name")
    if not selected_model and not args.enable and not args.disable:
        if args.json:
            raise ValueError(
                "honeymoney config edit ollama --json requires --model, "
                "--enable, or --disable"
            )
        selected_model = _prompt_ollama_model(ollama_config)
        if selected_model is None:
            print("Config unchanged.")
            return 0

    if selected_model:
        ollama_config["model"] = selected_model
        ollama_config["enabled"] = True
    elif args.enable:
        _require_available_ollama_model(ollama_config)
        ollama_config["enabled"] = True
    elif args.disable:
        ollama_config["enabled"] = False

    _write_config_document(config_path, config)
    if args.json:
        _emit_json(
            "config",
            "success",
            data={"ollama": ollama_config},
            artifacts=artifacts,
        )
    elif ollama_config.get("enabled"):
        print(f"Ollama enabled with model {ollama_config.get('model', '(not set)')}")
    else:
        print("Ollama disabled")
    return 0


def _existing_config_path(config_path: str | None) -> Path:
    path = Path(config_path or "config.json").expanduser()
    if not path.exists():
        raise ValueError(
            f"Config file does not exist: {path}. Run `honeymoney setup` first."
        )
    return path.resolve()


def _read_config_document(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")
    _validate_config_document(config)
    return config


def _validate_config_document(config: dict[str, Any]) -> None:
    paths = config.get("paths")
    if paths is not None and not isinstance(paths, dict):
        raise ValueError("Config field paths must be a JSON object")
    for path_field, path_value in (paths or {}).items():
        if path_field not in {"input", "output"}:
            continue
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(
                f"Config field paths.{path_field} must be a non-empty string"
            )

    profiles = config.get("profiles")
    if profiles is not None:
        _validate_non_empty_string_array("profiles", profiles)

    for field in ("profile_mappings", "rules", "corrections"):
        if field in config and (
            not isinstance(config[field], str) or not config[field].strip()
        ):
            raise ValueError(f"Config field {field} must be a non-empty string")

    pdf = config.get("pdf")
    if pdf is not None:
        if not isinstance(pdf, dict):
            raise ValueError("Config field pdf must be a JSON object")
        if "enabled" in pdf and not isinstance(pdf["enabled"], bool):
            raise ValueError("Config field pdf.enabled must be a boolean")
        if "parser" in pdf and (
            not isinstance(pdf["parser"], str) or not pdf["parser"].strip()
        ):
            raise ValueError("Config field pdf.parser must be a non-empty string")

    base_currency = config.get("base_currency")
    if base_currency is not None and (
        not isinstance(base_currency, str) or not base_currency.strip()
    ):
        raise ValueError("Config field base_currency must be a non-empty string")

    exchange_rates = config.get("exchange_rates")
    if exchange_rates is not None:
        if not isinstance(exchange_rates, dict):
            raise ValueError("Config field exchange_rates must be a JSON object")
        for currency, rate in exchange_rates.items():
            if not isinstance(currency, str) or not currency.strip():
                raise ValueError(
                    "Config field exchange_rates keys must be non-empty strings"
                )
            _validate_finite_number(
                f"exchange_rates.{currency}", rate, minimum=Decimal("0"), exclusive=True
            )

    threshold = config.get("review_confidence_threshold")
    if threshold is not None:
        _validate_finite_number(
            "review_confidence_threshold",
            threshold,
            minimum=Decimal("0"),
            maximum=Decimal("1"),
            range_message="must be a number from 0 to 1",
        )

    for field in ("categories", "owners", "payment_methods"):
        if field in config:
            _validate_non_empty_string_array(field, config[field], unique=True)

    ollama = config.get("ollama")
    if ollama is not None:
        if not isinstance(ollama, dict):
            raise ValueError("Config field ollama must be a JSON object")
        if "enabled" in ollama and not isinstance(ollama["enabled"], bool):
            raise ValueError("Config field ollama.enabled must be a boolean")
        for field in ("url", "model"):
            if field in ollama and (
                not isinstance(ollama[field], str) or not ollama[field].strip()
            ):
                raise ValueError(
                    f"Config field ollama.{field} must be a non-empty string"
                )
        if "url" in ollama:
            validate_ollama_endpoint(ollama["url"])
        if "batch_size" in ollama and (
            isinstance(ollama["batch_size"], bool)
            or not isinstance(ollama["batch_size"], int)
            or ollama["batch_size"] < 1
        ):
            raise ValueError(
                "Config field ollama.batch_size must be a positive integer"
            )
        if "timeout_seconds" in ollama:
            _validate_finite_number(
                "ollama.timeout_seconds",
                ollama["timeout_seconds"],
                minimum=Decimal("0"),
                exclusive=True,
            )
        if "think" in ollama and not isinstance(ollama["think"], (bool, str)):
            raise ValueError("Config field ollama.think must be a boolean or string")

    reconciliation = config.get("reconciliation")
    if reconciliation is not None:
        if not isinstance(reconciliation, dict):
            raise ValueError("Config field reconciliation must be a JSON object")
        reconciliation_date_window(config)
    validate_category_policies(config)


def _validate_non_empty_string_array(
    field: str, value: Any, *, unique: bool = False
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"Config field {field} must be a JSON array")
    if not value:
        raise ValueError(f"Config field {field} must not be empty")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"Config field {field}[{index}] must be a non-empty string"
            )
        normalized.append(item.strip())
    if unique and len(set(normalized)) != len(normalized):
        raise ValueError(f"Config field {field} must not contain duplicates")


def _validate_finite_number(
    field: str,
    value: Any,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    exclusive: bool = False,
    range_message: str | None = None,
) -> None:
    message = range_message or "must be a finite number greater than 0"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Config field {field} {message}")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"Config field {field} {message}")
    invalid_minimum = minimum is not None and (
        number <= minimum if exclusive else number < minimum
    )
    if invalid_minimum or (maximum is not None and number > maximum):
        raise ValueError(f"Config field {field} {message}")


def _write_config_document(path: Path, config: dict[str, Any]) -> None:
    _atomic_write_text_files(
        {path: f"{json.dumps(config, indent=2, sort_keys=True)}\n"}
    )


def _prompt_ollama_model(ollama_config: dict[str, Any]) -> str | None:
    models = list_ollama_models(ollama_config)
    if not models:
        raise ValueError("No local Ollama models found; run `ollama pull MODEL` first")
    print("Available Ollama models:")
    for index, model in enumerate(models, start=1):
        print(f"  {index}. {model}")
    while True:
        try:
            choice = input(f"Select model [1-{len(models)}/q]: ").strip().casefold()
        except EOFError as error:
            raise ValueError("No Ollama model selected") from error
        if choice == "q":
            return None
        try:
            selected = int(choice)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(models):
            return models[selected - 1]
        print(f"Enter a number from 1 to {len(models)}, or q to cancel.")


def _require_available_ollama_model(ollama_config: dict[str, Any]) -> None:
    configured = str(ollama_config.get("model", "")).strip()
    if not configured:
        raise ValueError(
            "Set an Ollama model with --model before enabling the fallback"
        )
    available = list_ollama_models(ollama_config)
    aliases = {
        alias for model in available for alias in (model, model.removesuffix(":latest"))
    }
    if configured not in aliases:
        raise ValueError(
            f"Configured Ollama model {configured!r} is not installed; "
            "pass --model or run `honeymoney config edit ollama` to select one"
        )


def _edit_config_in_editor(config_path: Path) -> None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("vi")
    if not editor:
        raise ValueError("Set $VISUAL or $EDITOR before running config edit")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.stem}.",
        suffix=".json",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            fh.write(config_path.read_text(encoding="utf-8"))
        result = subprocess.run(
            [*shlex.split(editor), str(temporary_path)],
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"Editor exited with status {result.returncode}")
        config = _read_config_document(temporary_path)
        _write_config_document(config_path, config)
    finally:
        temporary_path.unlink(missing_ok=True)


def _setup_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney setup",
        description="Create a starter Honeymoney workspace.",
    )
    parser.add_argument("--root")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.json and not args.root:
        raise ValueError("honeymoney setup --json requires --root")
    root = _setup_root(args.root)
    existing_config_path = root / "config.json"
    if existing_config_path.exists():
        _recover_config_generation(_read_config_document(existing_config_path))
    _write_starter_workspace(root, force=args.force)
    if args.json:
        _emit_json(
            "setup",
            "success",
            data={"root": str(root)},
            artifacts={
                "config_json": str(root / "config.json"),
                "corrections_csv": str(root / "corrections.csv"),
                "input_directory": str(root / "input"),
                "output_directory": str(root / "output"),
            },
        )
        return 0
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
    parser = _command_parser(
        argv,
        prog="honeymoney import",
        description="Import one pasted CSV/PDF file or folder path.",
    )
    parser.add_argument("path", nargs="?")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--no-interactive", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    input_path = args.path
    if not input_path:
        if args.json:
            raise ValueError("honeymoney import --json requires a path")
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
    if args.json:
        run_args.append("--json")
    if args.reset:
        run_args.append("--reset")
    elif args.replace:
        run_args.append("--replace")
    return _run_pipeline(
        run_args,
        print_import_summary=not args.json,
        json_command="import",
    )


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
            "run `honeymoney status` to see totals or `honeymoney review` to categorize"
        )
    ledger = report.get("ledger", {})
    if ledger:
        print(
            f"Ledger now has {ledger['transaction_count']} records "
            f"({ledger['uncategorized_count']} uncategorized)"
        )


def _review_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney review",
        description="Review category and accounting flow decisions.",
    )
    parser.add_argument("period", nargs="?", help="Month name or YYYY-MM")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        metavar="CATEGORY",
        help=(
            "Review rows currently in CATEGORY regardless of review state; "
            "repeat to select multiple categories"
        ),
    )
    parser.add_argument(
        "--flow",
        action="append",
        dest="flows",
        choices=sorted(ALLOWED_FLOW_TYPES),
        help="Review rows with this accounting flow; repeat to select more than one",
    )
    parser.add_argument("--direction", choices=["inflow", "outflow"])
    parser.add_argument("--transaction", dest="transaction_id")
    parser.add_argument("--as", dest="decision")
    parser.add_argument("--remember", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    has_period = bool(args.month or args.period or args.start or args.end)
    has_filters = bool(args.categories or args.flows or args.direction or has_period)
    one_shot = bool(args.transaction_id or args.decision or args.remember or args.yes)
    if args.json and not (args.transaction_id and args.decision):
        raise ValueError(
            "honeymoney review --json requires --transaction ID and --as DECISION; "
            "JSON mode cannot prompt"
        )
    if bool(args.transaction_id) != bool(args.decision):
        raise ValueError(
            "One-shot review requires both --transaction ID and --as DECISION"
        )
    if one_shot and not args.transaction_id:
        raise ValueError(
            "--remember and --yes require one-shot --transaction ID and --as DECISION"
        )
    if one_shot and has_filters:
        raise ValueError("One-shot review cannot be combined with review filters")
    if args.yes and not args.remember:
        raise ValueError("--yes is valid only with --remember")
    if args.remember and not args.yes:
        raise ValueError("One-shot --remember requires --yes")

    config = _load_config(args.config_path)
    category_filters = args.categories or []
    unsupported_categories = sorted(set(category_filters) - allowed_categories(config))
    if unsupported_categories:
        raise ValueError(
            "Unsupported review category: " + ", ".join(unsupported_categories)
        )
    categorized_path = Path(args.output_path or config["paths"]["output"])
    ledger_rows = read_ledger(categorized_path)
    if not ledger_rows:
        if args.transaction_id:
            raise ValueError(f"Unknown transaction_id: {args.transaction_id}")
        print(f"No processed records found at {categorized_path}")
        print("Run `honeymoney import` or `honeymoney run` first.")
        return 0

    if args.transaction_id:
        return _one_shot_review(
            args,
            config,
            categorized_path,
            ledger_rows,
        )

    if args.flows or args.direction or has_period:
        filter_period = (
            (args.month or args.period, args.start, args.end) if has_period else None
        )
        selected = _filtered_review_rows(
            ledger_rows,
            category_filters=category_filters,
            flow_filters=args.flows or [],
            direction=args.direction,
            period=filter_period,
        )
        if not selected:
            print(
                "No transactions matched the selected review filters; no changes written."
            )
            print(_review_filter_summary(args))
            return 0
        patches, remembered_rules = _prompt_accounting_decisions(selected, ledger_rows)
        if patches:
            result = apply_correction_operation(
                config,
                categorized_path,
                patches,
                remembered_rules=list(remembered_rules.values()),
            )
            resulting_rows = result.ledger_rows
        else:
            resulting_rows = ledger_rows
        remaining_matches = _filtered_review_rows(
            resulting_rows,
            category_filters=category_filters,
            flow_filters=args.flows or [],
            direction=args.direction,
            period=filter_period,
        )
        review_queue_count = sum(
            1 for row in resulting_rows if row.get("needs_review") == "true"
        )
        print(
            f"Review complete: {len(patches)} updated from {len(selected)} matched; "
            f"{len(remaining_matches)} still match these filters; "
            f"{review_queue_count} in review queue"
        )
        return 0

    reviewed = _prompt_review_transactions(ledger_rows, config, category_filters)
    patches = {
        row["transaction_id"]: {
            "category": row["category"],
            "confidence": "1.00",
            "reason": "Categorized interactively",
            "needs_review": "false",
        }
        for row in reviewed
    }
    if patches:
        result = apply_correction_operation(config, categorized_path, patches)
        remaining = result.remaining_review_count
    else:
        remaining = sum(1 for row in ledger_rows if row.get("needs_review") == "true")
    if category_filters:
        print(
            f"Review complete: {len(patches)} updated from selected categories, "
            f"{remaining} still need review"
        )
    else:
        print(f"Review complete: {len(patches)} updated, {remaining} still need review")
    return 0


def _one_shot_review(
    args: argparse.Namespace,
    config: dict[str, Any],
    categorized_path: Path,
    ledger_rows: list[dict[str, str]],
) -> int:
    transaction = next(
        (
            row
            for row in ledger_rows
            if row.get("transaction_id") == args.transaction_id
        ),
        None,
    )
    if transaction is None:
        raise ValueError(f"Unknown transaction_id: {args.transaction_id}")
    decision = _normalize_review_decision(args.decision)
    patch = _accounting_decision_patch(
        transaction, decision, "Accounting flow confirmed by one-shot review"
    )
    remembered_rules: list[dict[str, Any]] = []
    rule_matches = 0
    if args.remember:
        if decision != "income":
            raise ValueError("--remember is supported only with --as income")
        rule = _remembered_income_rule(transaction)
        rule_matches = _remembered_rule_match_count(ledger_rows, transaction)
        remembered_rules.append(rule)

    result = apply_correction_operation(
        config,
        categorized_path,
        {args.transaction_id: patch},
        remembered_rules=remembered_rules,
    )
    data = {
        "applied_count": result.applied_count,
        "remaining_review_count": result.remaining_review_count,
        "transaction_ids": result.transaction_ids,
        "decision": decision,
        "rules_added": result.rules_added,
        "rule_matches": rule_matches,
    }
    artifacts = _correction_artifacts(config, categorized_path)
    if args.remember:
        artifacts["rules_json"] = str(Path(config["rules"]).resolve())
    if args.json:
        _emit_json("review", "success", data=data, artifacts=artifacts)
    else:
        suffix = (
            f" and remembered an exact inflow rule matching {rule_matches} current rows"
            if args.remember
            else ""
        )
        print(f"Reviewed {args.transaction_id} as {decision}{suffix}.")
    return 0


def _filtered_review_rows(
    rows: list[dict[str, str]],
    *,
    category_filters: list[str],
    flow_filters: list[str],
    direction: str | None,
    period: tuple[str | None, str | None, str | None] | None,
) -> list[dict[str, str]]:
    selected = list(rows)
    if period is not None:
        month, start, end = period
        period_start, period_end = _resolve_period(month, start, end)
        selected = _rows_in_period(selected, period_start, period_end)
    if category_filters:
        categories = set(category_filters)
        selected = [row for row in selected if row.get("category") in categories]
    if flow_filters:
        flows = set(flow_filters)
        selected = [row for row in selected if row.get("flow_type") in flows]
    if direction:
        selected = [row for row in selected if transaction_direction(row) == direction]
    return selected


def _prompt_accounting_decisions(
    selected: list[dict[str, str]], ledger_rows: list[dict[str, str]]
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    print(f"\n{len(selected)} transactions matched the selected review filters.")
    print(
        "Choose [i]ncome, [r]efund, internal [t]ransfer, [c]ard payment, "
        "in[v]estment transfer, [e]xpense, [u]nresolved, [s]kip, or [q]uit."
    )
    choices = {
        "i": "income",
        "income": "income",
        "r": "refund",
        "refund": "refund",
        "t": "internal-transfer",
        "internal-transfer": "internal-transfer",
        "c": "credit-card-payment",
        "credit-card-payment": "credit-card-payment",
        "v": "investment-transfer",
        "investment-transfer": "investment-transfer",
        "e": "expense",
        "expense": "expense",
        "u": "unresolved",
        "unresolved": "unresolved",
    }
    patches: dict[str, dict[str, str]] = {}
    remembered: dict[str, dict[str, Any]] = {}
    for position, transaction in enumerate(selected, start=1):
        print(f"\n[{position}/{len(selected)}] {_accounting_review_line(transaction)}")
        while True:
            try:
                raw_choice = (
                    input("Decision [i/r/t/c/v/e/u/Enter/q]: ").strip().casefold()
                )
            except EOFError:
                return patches, remembered
            if raw_choice in {"", "s", "skip"}:
                break
            if raw_choice in {"q", "quit"}:
                return {}, {}
            decision = choices.get(raw_choice)
            if decision is None:
                print("Enter i, r, t, c, v, e, u, Enter to skip, or q to quit.")
                continue
            try:
                patch = _accounting_decision_patch(
                    transaction,
                    decision,
                    "Accounting flow confirmed interactively",
                )
            except ValueError as error:
                print(str(error))
                continue
            patches[transaction["transaction_id"]] = patch
            if decision == "income" and _can_remember_income(transaction):
                match_count = _remembered_rule_match_count(ledger_rows, transaction)
                print(
                    "Rule preview: exact institution, account, and description; "
                    f"inflow direction only; {match_count} current row(s) match."
                )
                try:
                    remember = (
                        input("Remember matching future inflows as income? [y/N]: ")
                        .strip()
                        .casefold()
                    )
                except EOFError:
                    remember = ""
                if remember in {"y", "yes"}:
                    rule = _remembered_income_rule(transaction)
                    remembered[rule["id"]] = rule
            break
    return patches, remembered


def _normalize_review_decision(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    aliases = {
        "leave-unresolved": "unresolved",
        "internal-transfer": "internal-transfer",
    }
    normalized = aliases.get(normalized, normalized)
    supported = {
        "income",
        "refund",
        "internal-transfer",
        "credit-card-payment",
        "investment-transfer",
        "expense",
        "unresolved",
    }
    if normalized not in supported:
        raise ValueError(
            "Unsupported review decision: "
            f"{value}. Choose income, refund, internal-transfer, credit-card-payment, "
            "investment-transfer, expense, or unresolved"
        )
    return normalized


def _accounting_decision_patch(
    transaction: dict[str, str], decision: str, reason: str
) -> dict[str, str]:
    if decision == "income" and transaction_direction(transaction) != "inflow":
        raise ValueError("Income can be confirmed only for a normalized inflow")
    patch = {
        "confidence": "1.00",
        "reason": reason,
        "needs_review": "false",
    }
    mappings = {
        "income": {"category": "Income", "flow_type": "income"},
        "refund": {"flow_type": "refund"},
        "internal-transfer": {
            "category": "Internal Transfer",
            "flow_type": "internal_transfer",
        },
        "credit-card-payment": {
            "category": "Credit Card Payment",
            "flow_type": "credit_card_payment",
        },
        "investment-transfer": {
            "category": "Investments",
            "flow_type": "investment_transfer",
        },
        "expense": {"flow_type": "expense"},
        "unresolved": {"flow_type": "unresolved", "needs_review": "true"},
    }
    patch.update(mappings[decision])
    return patch


def _accounting_review_line(transaction: dict[str, str]) -> str:
    amount = transaction.get("amount_hkd", "")
    posted = " ".join(
        part
        for part in [
            transaction.get("posted_amount", ""),
            transaction.get("posted_currency", ""),
        ]
        if part
    )
    base_currency = "HKD"
    amount_label = f"{amount} {base_currency}"
    if posted and posted != amount_label:
        amount_label = f"{posted} ({amount_label})"
    merchant = transaction.get("merchant", "") or "(no merchant)"
    description = transaction.get("original_description", "")
    merchant_label = merchant
    if description and description != merchant:
        merchant_label += f" / {description}"
    return "  ".join(
        [
            transaction.get("date", ""),
            amount_label,
            transaction.get("account", "") or transaction.get("account_id", ""),
            merchant_label,
            f"category={transaction.get('category', '')}",
            f"flow={transaction.get('flow_type', '')}",
        ]
    )


def _can_remember_income(transaction: dict[str, str]) -> bool:
    return bool(
        transaction.get("institution", "").strip()
        and transaction.get("account_id", "").strip()
        and transaction.get("original_description", "").strip()
        and transaction_direction(transaction) == "inflow"
    )


def _remembered_income_rule(transaction: dict[str, str]) -> dict[str, Any]:
    if not _can_remember_income(transaction):
        raise ValueError(
            "Cannot remember income without institution, account_id, exact description, "
            "and inflow direction"
        )
    identity_values = [
        transaction[field].strip().casefold()
        for field in ["institution", "account_id", "original_description"]
    ]
    identity = "|".join(identity_values)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return {
        "id": f"review_income_{digest}",
        "enabled": True,
        "priority": 100,
        "conditions": [
            {
                "field": "institution",
                "match_type": "exact",
                "patterns": [identity_values[0]],
            },
            {
                "field": "account_id",
                "match_type": "exact",
                "patterns": [identity_values[1]],
            },
            {
                "field": "original_description",
                "match_type": "exact",
                "patterns": [identity_values[2]],
            },
            {"field": "direction", "match_type": "exact", "patterns": ["inflow"]},
        ],
        "category": "Income",
        "flow_type": "income",
        "confidence": 1.0,
        "notes": "Confirmed in human cash-flow review",
    }


def _remembered_rule_match_count(
    rows: list[dict[str, str]], transaction: dict[str, str]
) -> int:
    return sum(
        1
        for row in rows
        if row.get("institution", "").strip().casefold()
        == transaction.get("institution", "").strip().casefold()
        and row.get("account_id", "").strip().casefold()
        == transaction.get("account_id", "").strip().casefold()
        and row.get("original_description", "").strip().casefold()
        == transaction.get("original_description", "").strip().casefold()
        and transaction_direction(row) == "inflow"
    )


def _review_filter_summary(args: argparse.Namespace) -> str:
    parts = []
    if args.month or args.period:
        parts.append(f"period={args.month or args.period}")
    if args.start:
        parts.append(f"start={args.start}")
    if args.end:
        parts.append(f"end={args.end}")
    if args.categories:
        parts.append("category=" + ",".join(args.categories))
    if args.flows:
        parts.append("flow=" + ",".join(args.flows))
    if args.direction:
        parts.append(f"direction={args.direction}")
    return "Matched filters: " + "; ".join(parts)


def _correction_artifacts(
    config: dict[str, Any], categorized_path: Path
) -> dict[str, str]:
    return {
        "corrections_csv": str(Path(config["corrections"]).resolve()),
        "categorized_csv": str(categorized_path.resolve()),
        "review_needed_csv": str(
            (categorized_path.parent / "review_needed.csv").resolve()
        ),
    }


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
    parser = _command_parser(
        argv,
        prog="honeymoney status",
        description="Show processed and categorized counts for a time period.",
    )
    parser.add_argument("period", nargs="?", help="Month name or YYYY-MM")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    start, end = _resolve_period(args.month or args.period, args.start, args.end)
    config = _load_config(args.config_path)
    categorized_path = Path(config["paths"]["output"])
    ledger_rows = read_ledger(categorized_path)
    if not ledger_rows:
        if args.json:
            _emit_json(
                "status",
                "success",
                data={
                    "period": {"start": start.isoformat(), "end": end.isoformat()},
                    "statements_processed": 0,
                    "records_processed": 0,
                    "categorized": 0,
                    "uncategorized": 0,
                    "needs_review": 0,
                    "unresolved_inflows": 0,
                    "unresolved_outflows": 0,
                    "ledger": {
                        "total_records": 0,
                        "outside_period": 0,
                        "unparseable_dates": 0,
                    },
                },
                artifacts={"categorized_csv": str(categorized_path.resolve())},
            )
            return 0
        print(f"No processed records found at {categorized_path}")
        print("Run `honeymoney import` or `honeymoney run` first.")
        return 0

    rows = _rows_in_period(ledger_rows, start, end)
    categorized = [row for row in rows if _is_categorized(row)]
    statements = {row.get("source_file", "") for row in rows if row.get("source_file")}
    review = [row for row in rows if row.get("needs_review") == "true"]
    unresolved_inflows = sum(
        1
        for row in rows
        if row.get("flow_type") == "unresolved"
        and transaction_direction(row) == "inflow"
    )
    unresolved_outflows = sum(
        1
        for row in rows
        if row.get("flow_type") == "unresolved"
        and transaction_direction(row) == "outflow"
    )
    undated = sum(
        1 for row in ledger_rows if _parse_iso_date(row.get("date", "")) is None
    )
    outside = len(ledger_rows) - len(rows) - undated

    if args.json:
        _emit_json(
            "status",
            "success",
            data={
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "statements_processed": len(statements),
                "records_processed": len(rows),
                "categorized": len(categorized),
                "uncategorized": len(rows) - len(categorized),
                "needs_review": len(review),
                "unresolved_inflows": unresolved_inflows,
                "unresolved_outflows": unresolved_outflows,
                "ledger": {
                    "total_records": len(ledger_rows),
                    "outside_period": outside,
                    "unparseable_dates": undated,
                },
            },
            artifacts={"categorized_csv": str(categorized_path.resolve())},
        )
        return 0

    print(f"Status for {start.isoformat()} to {end.isoformat()}")
    print(f"  Statements processed: {len(statements)}")
    print(f"  Records processed:    {len(rows)}")
    print(f"  Categorized:          {len(categorized)}")
    print(f"  Uncategorized:        {len(rows) - len(categorized)}")
    print(f"  Needs review:         {len(review)}")
    print(f"  Unresolved inflows:   {unresolved_inflows}")
    print(f"  Unresolved outflows:  {unresolved_outflows}")
    print(
        "  Review inflows:       honeymoney review --flow unresolved --direction inflow"
    )
    print(
        f"Ledger total: {len(ledger_rows)} records "
        f"({outside} outside this period, {undated} with unparseable dates)"
    )
    return 0


def _report_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney report",
        description="Write and open an HTML report for a time period.",
    )
    parser.add_argument("period", nargs="?", help="Month name or YYYY-MM")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path", help="Report HTML path")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config(args.config_path)
    categorized_path = Path(config["paths"]["output"])
    ledger_rows = read_ledger(categorized_path)
    reconcile_ledger(ledger_rows, config)

    start, end = _resolve_period(args.month or args.period, args.start, args.end)
    rows = _rows_in_period(ledger_rows, start, end)
    period_label = f"{start.isoformat()} to {end.isoformat()}"

    report_path = Path(args.output_path or categorized_path.parent / "report.html")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report_html(rows, period_label), encoding="utf-8")
    if args.json:
        _emit_json(
            "report",
            "success",
            data={
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "transaction_count": len(rows),
            },
            artifacts={"report_html": str(report_path.resolve())},
        )
        return 0
    print(f"Report written to {report_path} ({len(rows)} transactions)")
    if not args.no_open:
        webbrowser.open(report_path.resolve().as_uri())
    return 0


def _reconcile_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney reconcile",
        description="Recompute and inspect cash-flow and transfer reconciliation.",
    )
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config = _load_config(args.config_path)
    categorized_path = Path(args.output_path or config["paths"]["output"])
    rows = read_ledger(categorized_path)
    summary = reconcile_ledger(rows, config)
    if not args.dry_run:
        _write_ledger_outputs(categorized_path, rows)

    artifacts = {"categorized_csv": str(categorized_path.resolve())}
    if args.json:
        _emit_json(
            "reconcile",
            "success",
            data={**summary, "dry_run": args.dry_run},
            artifacts=artifacts,
        )
        return 0
    mode = "Inspected" if args.dry_run else "Reconciled"
    print(
        f"{mode} {summary['transaction_count']} transactions: "
        f"{summary['paired_groups']} paired groups, "
        f"{summary['ambiguous_transactions']} ambiguous, "
        f"{summary['unmatched_transactions']} unmatched, "
        f"{summary['unresolved_transactions']} unresolved"
    )
    return 0


def _pending_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney pending",
        description="List transactions that need review for a time period.",
    )
    parser.add_argument("period", nargs="?", help="Month name or YYYY-MM")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    start, end = _resolve_period(args.month or args.period, args.start, args.end)
    config = _load_config(args.config_path)
    categorized_path = Path(config["paths"]["output"])
    ledger_rows = read_ledger(categorized_path)
    pending_rows = [
        to_review_row(row)
        for row in _rows_in_period(ledger_rows, start, end)
        if row.get("needs_review") == "true"
    ]

    if args.json:
        _emit_json(
            "pending",
            "success",
            data={
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "count": len(pending_rows),
                "transactions": pending_rows,
            },
            artifacts={
                "categorized_csv": str(categorized_path.resolve()),
                "review_needed_csv": str(
                    (categorized_path.parent / "review_needed.csv").resolve()
                ),
            },
        )
        return 0

    print(f"Pending review for {start.isoformat()} to {end.isoformat()}")
    print(f"  Transactions: {len(pending_rows)}")
    for row in pending_rows:
        print(f"  {row['transaction_id']}  {row['date']}  {row['merchant']}")
    return 0


def _correct_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney correct",
        description="Apply a validated JSON batch of transaction corrections.",
    )
    parser.add_argument("--file", dest="correction_file")
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--output", dest="output_path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.correction_file:
        raise ValueError(
            "honeymoney correct requires --file PATH (or --file - for stdin)"
        )

    config = _load_config(args.config_path)
    categorized_path = Path(args.output_path or config["paths"]["output"])
    ledger_rows = read_ledger(categorized_path)
    ledger_ids = {
        row["transaction_id"] for row in ledger_rows if row.get("transaction_id")
    }
    correction_batch = _load_json_correction_batch(
        args.correction_file, config, ledger_ids
    )
    result = apply_correction_operation(config, categorized_path, correction_batch)

    data = {
        "applied_count": result.applied_count,
        "remaining_review_count": result.remaining_review_count,
        "transaction_ids": result.transaction_ids,
    }
    artifacts = _correction_artifacts(config, categorized_path)
    if args.json:
        _emit_json("correct", "success", data=data, artifacts=artifacts)
    else:
        print(
            f"Applied {result.applied_count} corrections; "
            f"{result.remaining_review_count} transactions still need review"
        )
    return 0


def _load_json_correction_batch(
    source: str,
    config: dict[str, Any],
    ledger_ids: set[str],
) -> dict[str, dict[str, str]]:
    if source == "-":
        payload = json.load(sys.stdin)
    else:
        with Path(source).open(encoding="utf-8") as fh:
            payload = json.load(fh)
    if not isinstance(payload, list):
        raise ValueError("Correction input must be a JSON array")

    corrections: dict[str, dict[str, str]] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"Correction at index {index} must be a JSON object")
        unknown_fields = set(item) - {"transaction_id", *CORRECTION_FIELDS}
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ValueError(
                f"Unsupported correction fields at index {index}: {fields}"
            )
        transaction_id = item.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            raise ValueError(f"Correction at index {index} requires transaction_id")
        transaction_id = transaction_id.strip()
        if transaction_id in corrections:
            raise ValueError(
                f"Duplicate transaction_id in correction batch: {transaction_id}"
            )
        if transaction_id not in ledger_ids:
            raise ValueError(
                f"Unknown transaction_id in correction batch: {transaction_id}"
            )

        correction = _normalize_json_correction(index, item)
        if not correction:
            raise ValueError(
                f"Correction for {transaction_id} must set at least one correction field"
            )
        validate_correction(transaction_id, correction, config)
        corrections[transaction_id] = correction
    return corrections


def _normalize_json_correction(index: int, item: dict[str, Any]) -> dict[str, str]:
    correction: dict[str, str] = {}
    for field in CORRECTION_FIELDS:
        if field not in item:
            continue
        value = item[field]
        if field == "needs_review":
            if isinstance(value, bool):
                correction[field] = str(value).lower()
                continue
            if isinstance(value, str):
                normalized = value.strip().casefold()
                if not normalized:
                    raise ValueError(
                        f"Correction field {field} at index {index} must not be empty"
                    )
                if normalized in {"true", "false"}:
                    correction[field] = normalized
                    continue
            raise ValueError(
                f"Correction field needs_review at index {index} must be boolean"
            )
        if (
            field == "confidence"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            correction[field] = str(value)
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"Correction field {field} at index {index} must be a string"
            )
        normalized = value.strip()
        if field != "notes" and not normalized:
            raise ValueError(
                f"Correction field {field} at index {index} must not be empty"
            )
        correction[field] = normalized
    return correction


def _atomic_write_text_files(files: dict[Path, str]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for target, content in files.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            existing_mode = (
                stat.S_IMODE(target.stat().st_mode) if target.exists() else None
            )
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            staged.append((temporary_path, target))
        for temporary_path, target in staged:
            os.replace(temporary_path, target)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)


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


def _processed_source_files(
    ledger_rows: list[dict[str, str]], source_files: set[str]
) -> set[str]:
    return {
        row.get("source_file", "")
        for row in ledger_rows
        if row.get("source_file", "") in source_files
    }


def _merge_into_ledger(
    categorized_path: Path,
    transactions: list[dict[str, str]],
    replace_sources: set[str] | None = None,
) -> list[dict[str, str]]:
    merged = {
        row["transaction_id"]: row
        for row in read_ledger(categorized_path)
        if row.get("transaction_id")
        and (replace_sources is None or row.get("source_file") not in replace_sources)
    }
    for transaction in transactions:
        merged[transaction["transaction_id"]] = transaction
    return list(merged.values())


def _write_ledger_outputs(
    categorized_path: Path, ledger_rows: list[dict[str, str]]
) -> None:
    persist_generation(
        categorized_path, ledger_output_documents(categorized_path, ledger_rows)
    )


def _prompt_uncategorized(
    transactions: list[dict[str, str]], config: dict[str, Any]
) -> list[dict[str, str]]:
    pending = [row for row in transactions if not _is_categorized(row)]
    if pending and not config.get("ollama", {}).get("enabled", False):
        print(
            "\nOllama fallback is disabled; set ollama.enabled to true in "
            "config.json to enable it."
        )
    return _prompt_category_assignments(
        pending,
        config,
        f"\n{len(pending)} imported records have no category.",
    )


def _prompt_review_transactions(
    transactions: list[dict[str, str]],
    config: dict[str, Any],
    category_filters: list[str] | None = None,
) -> list[dict[str, str]]:
    if category_filters:
        selected_categories = set(category_filters)
        selected_rows = [
            row for row in transactions if row.get("category") in selected_categories
        ]
        category_label = ", ".join(sorted(selected_categories))
        return _prompt_category_assignments(
            selected_rows,
            config,
            f"\n{len(selected_rows)} records in selected categories ({category_label}).",
            empty_message=(
                "No transactions found in selected categories: " + category_label
            ),
        )
    pending = [row for row in transactions if row.get("needs_review") == "true"]
    return _prompt_category_assignments(
        pending,
        config,
        f"\n{len(pending)} records need review.",
        empty_message="No transactions need review.",
    )


def _prompt_category_assignments(
    pending: list[dict[str, str]],
    config: dict[str, Any],
    heading: str,
    empty_message: str | None = None,
) -> list[dict[str, str]]:
    if not pending:
        if empty_message:
            print(empty_message)
        return []
    categories = sorted(
        category for category in allowed_categories(config) if category != "Unknown"
    )
    print(heading)
    print(
        "Pick a category number, press Enter to skip one, or enter q to skip the rest."
    )
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


def _interactive_correction_documents(
    categorized: list[dict[str, str]],
    config: dict[str, Any],
    *,
    removed_transaction_ids: set[str] | None = None,
) -> dict[Path, str]:
    if not categorized or not config.get("corrections"):
        return {}
    path, content, _ = prepare_corrections_document(
        config,
        {
            transaction["transaction_id"]: {
                "category": transaction["category"],
                "confidence": "1.00",
                "reason": "Categorized interactively",
                "needs_review": "false",
            }
            for transaction in categorized
        },
        removed_transaction_ids=removed_transaction_ids,
    )
    return {path: content}


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
        _starter_csv_profile(),
        force,
    )
    starter_profile_paths = _copy_starter_profiles(profiles_dir, force)
    _write_json_file(profile_mappings_path, {"filename_patterns": []}, force)
    _write_json_file(rules_path, _starter_rules(), force)
    _write_text_file(
        corrections_path,
        "transaction_id,category,flow_type,owner,payment_method,confidence,reason,notes,needs_review\n",
        force,
    )
    _write_json_file(
        config_path,
        {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0, "USD": 7.8},
            "review_confidence_threshold": 0.8,
            "reconciliation": {"date_window_days": 3},
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


def _starter_csv_profile() -> dict[str, Any]:
    return {
        "id": "starter_csv",
        "account_id": "starter_csv",
        "account": "Starter CSV",
        "account_type": "bank",
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
    }


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


def _starter_rules() -> dict[str, Any]:
    return {
        "version": 1,
        "rules": [
            {
                "id": "mox-credit-card-payment",
                "enabled": True,
                "priority": 20,
                "conditions": [
                    {
                        "field": "institution",
                        "match_type": "exact",
                        "patterns": ["Mox"],
                    },
                    {
                        "field": "original_description",
                        "match_type": "regex",
                        "patterns": [
                            "^(?:PAYMENT TO MOX CREDIT CARD|MOX CREDIT CARD PAYMENT)$"
                        ],
                    },
                ],
                "category": "Credit Card Payment",
                "flow_type": "credit_card_payment",
                "owner": "Household",
                "confidence": 0.99,
                "notes": "Institution-specific payment treatment runs before Ollama",
            }
        ],
    }


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

    config = _read_config_document(Path(config_path))

    config.setdefault("paths", {})
    config["paths"].setdefault("input", "./input")
    config["paths"].setdefault("output", "./output/categorized.csv")
    _recover_config_generation(config)
    return config


def _recover_config_generation(config: dict[str, Any]) -> None:
    paths = config.get("paths", {})
    output = paths.get("output") if isinstance(paths, dict) else None
    if isinstance(output, str) and output.strip():
        recover_generation(Path(output))


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
            rendered = datetime(2023, 2, 28).strftime(date_format)
            datetime.strptime(rendered, date_format)
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
        if (
            "%Y" not in date_format
            and "%y" not in date_format
            and "statement_year" not in profile
        ):
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
        )
        imported = _import_csv(input_file, profile, config, input_root)
        if prompted_for_profile:
            _maybe_save_profile_mapping(input_file, profile, profile_mappings_path)
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
            _prompt_for_profile(csv_path, matching_profiles, None),
            True,
        )

    if len(profiles) > 1:
        if not interactive:
            raise ValueError(f"Could not detect profile for {csv_path.name}")
        return _prompt_for_profile(csv_path, profiles, None), True

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
) -> list[dict[str, str]]:
    csv_settings = profile.get("csv", {})
    _validate_selected_csv_headers(csv_path, profile, csv_settings)
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
                        input_path=pdf_path,
                        input_root=input_root,
                        columns=columns,
                        source_page=str(page_number),
                    )
                    if _row_is_skipped(normalized, skip_patterns):
                        continue
                    rows.append(normalized)
                return rows, warnings

            for page_number, page in enumerate(pdf.pages, start=1):
                word_rows = _pdf_word_source_rows(page, pdf_settings)
                if word_rows is not None:
                    for row_number, source_row in enumerate(word_rows, start=1):
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
                for table in tables:
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
                                input_path=pdf_path,
                                input_root=input_root,
                                columns=columns,
                                source_page=str(page_number),
                            )
                            if _row_is_skipped(normalized, skip_patterns):
                                continue
                            rows.append(normalized)
    if pdf_settings.get("word_rows_only", False) and not rows:
        warnings.append(f"No word transaction table found in {pdf_path.name}")
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
    page: Any, pdf_settings: dict[str, Any]
) -> list[dict[str, str]] | None:
    if not pdf_settings.get("word_rows", False) or not hasattr(page, "extract_words"):
        return None

    word_columns = pdf_settings.get("word_columns", {})
    if not isinstance(word_columns, dict):
        return None

    words = page.extract_words(x_tolerance=1, y_tolerance=3) or []
    lines = _pdf_word_lines(words, float(pdf_settings.get("word_y_tolerance", 3)))
    if not lines:
        return None

    rows: list[dict[str, str]] = []
    in_table = False
    for line in lines:
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
    posting_date = _normalize_date(
        _value(source_row, columns.get("posting_date")), profile
    )
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
    statement_opening_balance = _optional_decimal_value(
        source_row, columns.get("statement_opening_balance")
    )
    statement_closing_balance = _optional_decimal_value(
        source_row, columns.get("statement_closing_balance")
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
        "account_id": _value(source_row, columns.get("account_id"))
        or str(profile.get("account_id", "")),
        "account": _value(source_row, columns.get("account"))
        or str(profile.get("account", "")),
        "account_type": str(
            profile.get("account_type")
            or _account_type_for_payment_method(str(profile.get("payment_method", "")))
        ),
        "institution": str(profile.get("institution", "")),
        "country": str(profile.get("country", "")),
        "original_amount": _format_decimal(original_amount),
        "original_currency": original_currency,
        "posted_amount": _format_decimal(posted_amount),
        "posted_currency": posted_currency,
        "amount_hkd": _format_decimal(amount_hkd) if amount_hkd is not None else "",
        "statement_opening_balance": statement_opening_balance,
        "statement_closing_balance": statement_closing_balance,
        "merchant": merchant,
        "original_description": description,
        "category": "Unknown",
        "flow_type": "unresolved",
        "flow_source": "deterministic",
        "transfer_group_id": "",
        "paired_transaction_id": "",
        "reconciliation_status": "not_applicable",
        "reconciliation_confidence": "",
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
    return "|".join(
        _normalize_identity_part(transaction.get(field, "")) for field in fields
    )


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
    transactions: list[dict[str, str]],
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
        "account_type": "unknown",
        "institution": "",
        "country": "",
        "account_currency": "",
        "owner": "Household",
        "payment_method": "Unknown",
    }


def _account_type_for_payment_method(payment_method: str) -> str:
    return {
        "Bank Account": "bank",
        "Credit Card": "credit_card",
        "Brokerage": "investment",
    }.get(payment_method, "unknown")


def _value(row: dict[str, str], column: str | None) -> str:
    if column is None or column == "":
        return ""
    return _clean_text(row.get(str(column)))


def _optional_decimal_value(row: dict[str, str], column: str | None) -> str:
    value = _value(row, column)
    if not value:
        return ""
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return ""
    return _format_decimal(parsed) if parsed.is_finite() else ""


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
        near_date_groups.setdefault(
            _duplicate_key_without_date(transaction), []
        ).append(transaction)

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
    return "|".join(
        _normalize_identity_part(transaction.get(field, "")) for field in fields
    )


def _duplicate_key_without_date(transaction: dict[str, str]) -> str:
    fields = [
        "amount_hkd",
        "original_amount",
        "original_currency",
        "merchant",
        "original_description",
    ]
    return "|".join(
        _normalize_identity_part(transaction.get(field, "")) for field in fields
    )


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
    except (OSError, ValueError) as error:
        _status.clear()
        argv = sys.argv[1:]
        if "--json" in argv:
            command = argv[0] if argv and not argv[0].startswith("-") else "run"
            _emit_json(
                command,
                "error",
                errors=[{"type": type(error).__name__, "message": str(error)}],
            )
            return 2
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
