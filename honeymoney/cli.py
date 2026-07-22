from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import webbrowser
from datetime import date
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from honeymoney import importers, normalization
from honeymoney.categorization_memory import (
    apply_local_categorization_memory,
    build_local_categorization_memory,
)
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
from honeymoney.identity import (
    IdentityError,
    ambiguous_legacy_transaction_ids,
    resolve_batch,
)
from honeymoney.identity_state import load_identity_state
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
    ALLOWED_FLOW_TYPES,
    allowed_categories,
)

JSON_SCHEMA_VERSION = 1
IDENTITY_MIGRATION_AMBIGUITY_FLAG = "identity_migration_ambiguous"
PROFILE_PREVIEW_LIMIT = 10
PROFILE_PREVIEW_FIELDS = [
    "amount_hkd",
    "date",
    "merchant",
    "original_amount",
    "original_currency",
    "source_page",
    "source_row",
]


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
    if argv and argv[0] == "profile":
        return _profile_command(argv[1:])
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

    input_files = importers._discover_input_files(input_path)
    profiles = importers._load_profiles(config)
    profile_mappings = importers._load_profile_mappings(config)
    identity_state = load_identity_state(categorized_path)
    transactions, import_warnings, file_reports, identity_sources = (
        importers._import_transactions(
            input_files,
            profiles,
            config,
            input_path,
            interactive=interactive,
            profile_mappings=profile_mappings,
            profile_mappings_path=config.get("profile_mappings"),
            include_identity_sources=True,
            status=_status.update,
            clear_status=_status.clear,
        )
    )
    if args.reset:
        requested_action = "reset"
    elif args.replace:
        requested_action = "replace"
    else:
        requested_action = "import"
    candidate_source_ids = importers._candidate_source_ids(
        input_files, input_path, config
    )
    for file_report in file_reports:
        file_report["requested_action"] = requested_action
        file_report["source_id"] = candidate_source_ids.get(
            file_report["source_file"], ""
        )
        if file_report.get("status") != "processed":
            file_report["ledger_action"] = "preserved"
        elif args.reset:
            file_report["ledger_action"] = "reset"
        elif args.replace:
            file_report["ledger_action"] = "replaced"
        else:
            file_report["ledger_action"] = "added"
    resolution = resolve_batch(
        ledger_rows=identity_state.rows,
        manifest=identity_state.manifest,
        sources=identity_sources,
        intent=requested_action,
    )
    transactions = [dict(row) for row in resolution.resolved_rows]
    resolved_source_ids = {
        str(source["source_namespace_id"]): str(source["source_id"])
        for source in resolution.next_manifest["sources"]
    }
    for source in identity_sources:
        source_id_value = resolved_source_ids.get(source.namespace_id, "")
        for file_report in file_reports:
            if file_report.get("source_file") == source.source_display:
                file_report["source_id"] = source_id_value
    import_warnings.extend(
        _identity_diagnostic_warning(item) for item in resolution.diagnostics
    )
    reset_ids = set(resolution.reset_transaction_ids)
    corrections = load_corrections(config)
    correction_documents: dict[Path, str] = {}
    if args.reset and config.get("corrections"):
        corrections_path, corrections_content, corrections = (
            prepare_corrections_document(config, removed_transaction_ids=reset_ids)
        )
        correction_documents[corrections_path] = corrections_content
    local_memory = build_local_categorization_memory(
        identity_state.rows, corrections, config
    )
    _status.update("Applying categorization rules...")
    rules = load_rules(config)
    apply_rules(transactions, rules, config)
    _status.update("Applying local categorization memory...")
    apply_local_categorization_memory(transactions, local_memory, config)
    _status.update("Checking for duplicates...")
    normalization._annotate_duplicate_suspicions(
        transactions, resolution.retained_ledger_rows
    )
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
    apply_corrections(transactions, corrections)
    _enforce_identity_review(transactions)
    _status.clear()
    if interactive:
        categorized_interactively = _prompt_uncategorized(transactions, config)
    else:
        categorized_interactively = []
    review_rows = [row for row in transactions if row["needs_review"] == "true"]

    _status.update("Writing output files...")
    ledger_rows = [
        *(dict(row) for row in resolution.retained_ledger_rows),
        *transactions,
    ]
    reconciliation = reconcile_ledger(ledger_rows, config)
    _enforce_identity_review(ledger_rows)
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
    files = ledger_output_documents(
        categorized_path, ledger_rows, identity_manifest=resolution.next_manifest
    )
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
  honeymoney profile validate ... Validate a profile and optionally preview input
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

    categorization_memory = config.get("categorization_memory")
    if categorization_memory is not None:
        if not isinstance(categorization_memory, dict):
            raise ValueError("Config field categorization_memory must be a JSON object")
        if "enabled" in categorization_memory and not isinstance(
            categorization_memory["enabled"], bool
        ):
            raise ValueError(
                "Config field categorization_memory.enabled must be a boolean"
            )
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


def _profile_command(argv: list[str]) -> int:
    parser = _command_parser(
        argv,
        prog="honeymoney profile",
        description="Validate a local import profile and optionally preview one input.",
    )
    parser.add_argument("operation", choices=["validate"])
    parser.add_argument("profile_path", metavar="PROFILE")
    parser.add_argument("--input", dest="input_path", metavar="FILE")
    parser.add_argument("--config", dest="config_path", metavar="CONFIG")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    profile_path = Path(args.profile_path).expanduser().resolve()
    if not profile_path.exists():
        raise ValueError(f"Profile path does not exist: {profile_path}")
    if not profile_path.is_file():
        raise ValueError(f"Profile path is not a file: {profile_path}")

    config = _load_config_read_only(args.config_path)
    try:
        with profile_path.open(encoding="utf-8") as fh:
            profile = json.load(fh)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON in profile {profile_path}: {error.msg}"
        ) from error
    _validate_profile(profile, profile_path, config)

    profile_id = str(profile.get("id") or profile.get("account_id"))
    parsers = [parser_name for parser_name in ("csv", "pdf") if parser_name in profile]
    data: dict[str, Any] = {
        "mode": "validation",
        "parsers": parsers,
        "profile_id": profile_id,
        "profile_path": str(profile_path),
    }
    warnings: list[str] = []

    if args.input_path:
        input_path = Path(args.input_path).expanduser().resolve()
        if not input_path.exists():
            raise ValueError(f"Input path does not exist: {input_path}")
        if not input_path.is_file():
            raise ValueError(f"Input path is not a file: {input_path}")
        rows, parser_warnings = _preview_profile_input(
            profile, profile_id, input_path, config
        )
        preview_rows = [
            {field: row.get(field, "") for field in PROFILE_PREVIEW_FIELDS}
            for row in rows[:PROFILE_PREVIEW_LIMIT]
        ]
        warnings = [
            "Preview output contains normalized local statement data; keep it private.",
            *parser_warnings,
        ]
        if len(rows) > PROFILE_PREVIEW_LIMIT:
            warnings.append(
                f"Preview limited to the first {PROFILE_PREVIEW_LIMIT} of "
                f"{len(rows)} normalized rows."
            )
        data.update(
            {
                "base_currency": str(config.get("base_currency", "HKD")).upper(),
                "input_path": str(input_path),
                "mode": "preview",
                "preview_count": len(preview_rows),
                "preview_limit": PROFILE_PREVIEW_LIMIT,
                "rows": preview_rows,
                "transaction_count": len(rows),
            }
        )

    if args.json:
        _emit_json("profile.validate", "success", data=data, warnings=warnings)
        return 0

    parser_label = ", ".join(parsers)
    print(f"Profile {profile_id} is valid ({parser_label}).")
    if data["mode"] == "preview":
        print(
            f"Preview: {data['transaction_count']} normalized rows; "
            f"showing {data['preview_count']}."
        )
        for row in data["rows"]:
            print(
                f"  {row['date']} | {row['original_amount']} "
                f"{row['original_currency']} | {row['merchant']} | "
                f"base {row['amount_hkd']} {data['base_currency']}"
            )
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
    return 0


def _preview_profile_input(
    profile: dict[str, Any],
    profile_id: str,
    input_path: Path,
    config: dict[str, Any],
) -> tuple[list[dict[str, str]], list[str]]:
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        if "csv" not in profile:
            raise ValueError(
                f"Profile {profile_id} does not define csv parser settings "
                f"required for {input_path.name}"
            )
        return importers._import_csv(input_path, profile, config, input_path.parent), []
    if suffix == ".pdf":
        if "pdf" not in profile:
            raise ValueError(
                f"Profile {profile_id} does not define pdf parser settings "
                f"required for {input_path.name}"
            )
        return importers._import_pdf(input_path, profile, config, input_path.parent)
    raise ValueError(
        f"Unsupported preview input type for {input_path.name}; expected .csv or .pdf"
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
    _reject_ambiguous_legacy_transaction_ids(ledger_rows)
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
    _reject_ambiguous_legacy_transaction_ids(ledger_rows)
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


def _reject_ambiguous_legacy_transaction_ids(
    ledger_rows: list[dict[str, str]],
) -> None:
    if ambiguous_legacy_transaction_ids(ledger_rows):
        raise IdentityError("identity_legacy_transaction_id_ambiguous")


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
            "categorization_memory": {"enabled": False},
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
    return _load_config_document(config_path, recover=True)


def _load_config_read_only(config_path: str | None) -> dict[str, Any]:
    """Load validated configuration without recovering workspace artifacts."""
    return _load_config_document(config_path, recover=False)


def _load_config_document(config_path: str | None, *, recover: bool) -> dict[str, Any]:
    if config_path is None:
        default_config = Path("config.json")
        if default_config.exists():
            config_path = str(default_config)
        else:
            resolved_config_path = default_config.resolve()
            return {
                "paths": {"input": "./input", "output": "./output/categorized.csv"},
                "_identity_config_path": resolved_config_path,
                "_identity_workspace_root": resolved_config_path.parent,
            }

    resolved_config_path = Path(config_path).resolve(strict=True)
    config = _read_config_document(resolved_config_path)

    config.setdefault("paths", {})
    config["paths"].setdefault("input", "./input")
    config["paths"].setdefault("output", "./output/categorized.csv")
    config["_identity_config_path"] = resolved_config_path
    config["_identity_workspace_root"] = resolved_config_path.parent
    if recover:
        _recover_config_generation(config)
    return config


def _recover_config_generation(config: dict[str, Any]) -> None:
    paths = config.get("paths", {})
    output = paths.get("output") if isinstance(paths, dict) else None
    if isinstance(output, str) and output.strip():
        recover_generation(Path(output))


def _identity_diagnostic_warning(diagnostic: Any) -> str:
    """Format the resolver's safe diagnostic without exposing identity inputs."""
    count = getattr(diagnostic, "affected_count", None)
    if count is None:
        count = getattr(diagnostic, "candidate_count", 0)
    return (
        f"{diagnostic.code}: {diagnostic.source_display}; "
        f"action={diagnostic.action}; count={count}; {diagnostic.remediation}"
    )


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


def _enforce_identity_review(transactions: list[dict[str, str]]) -> None:
    for transaction in transactions:
        flags = {flag for flag in transaction.get("flags", "").split(";") if flag}
        if IDENTITY_MIGRATION_AMBIGUITY_FLAG not in flags:
            continue
        transaction["needs_review"] = "true"
        transaction["reason"] = (
            "Identity migration is ambiguous; explicit resolution is required"
        )


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def run() -> int:
    try:
        return main()
    except (OSError, ValueError) as error:
        _status.clear()
        argv = sys.argv[1:]
        identity_error = error if isinstance(error, IdentityError) else None
        identity_details = (
            _identity_error_details(identity_error) if identity_error else None
        )
        if "--json" in argv:
            command = _json_error_command(argv)
            _emit_json(
                command,
                "error",
                errors=[
                    identity_details
                    if identity_details is not None
                    else {"type": type(error).__name__, "message": str(error)}
                ],
            )
            return 2
        print(
            identity_details["message"] if identity_details is not None else str(error),
            file=sys.stderr,
        )
        return 2


def _identity_error_details(error: IdentityError) -> dict[str, Any]:
    diagnostic = error.diagnostic
    details: dict[str, Any] = {
        "type": "IdentityError",
        "code": error.code,
        "message": error.code,
    }
    if diagnostic is not None:
        count = getattr(diagnostic, "affected_count", None)
        if count is None:
            count = getattr(diagnostic, "candidate_count", 0)
        details.update(
            {
                "display": diagnostic.source_display,
                "action": diagnostic.action,
                "count": count,
                "remediation": diagnostic.remediation,
                "message": _identity_diagnostic_warning(diagnostic),
            }
        )
    return details


def _json_error_command(argv: list[str]) -> str:
    if len(argv) > 1 and argv[:2] == ["profile", "validate"]:
        return "profile.validate"
    return argv[0] if argv and not argv[0].startswith("-") else "run"


# Compatibility aliases keep existing private test seams while parsing lives in
# its own module. Importer internals always resolve their own collaborators.
_discover_input_files = importers._discover_input_files
_import_transactions = importers._import_transactions
_import_csv = importers._import_csv
_import_pdf = importers._import_pdf
_load_profiles = importers._load_profiles
_load_profile_mappings = importers._load_profile_mappings
_validate_profile = importers._validate_profile
_annotate_duplicate_suspicions = normalization._annotate_duplicate_suspicions
_normalized_row = normalization._normalized_row
_append_flag = normalization._append_flag
_append_reason = normalization._append_reason
_remove_flag = normalization._remove_flag
_parse_iso_date = normalization._parse_iso_date


if __name__ == "__main__":
    raise SystemExit(run())
