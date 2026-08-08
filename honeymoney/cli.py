"""Honeymoney 0.2 command-line interface."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import NoReturn, Sequence

from honeymoney import importers
from honeymoney.account_bindings import (
    BoundAccount,
    binding_views,
    remove_binding_pattern,
    replace_binding_pattern,
    upsert_binding,
)
from honeymoney.corrections import load_corrections
from honeymoney.ollama import list_ollama_models
from honeymoney.parser_contracts import Profile
from honeymoney.periods import PeriodSelection, resolve_period_selection
from honeymoney.rate_fetch import fetch_hkma_daily_rates, prepare_hkma_fetch
from honeymoney.rates import parse_hkma_daily_document
from honeymoney.workspace_commands import (
    CommandResult,
    apply_workspace_config,
    apply_workspace_corrections,
    apply_workspace_profile_mappings,
    apply_workspace_rate_observations,
    import_workspace,
    learn_workspace_rules,
    list_imports,
    load_workspace,
    rebuild_views,
    resolve_workspace_duplicate,
    resolve_workspace_source_data,
    review_workspace_transaction,
    show_import,
    workspace_config_document,
    workspace_duplicates,
    workspace_missing_valuations,
    workspace_pending,
    workspace_profile_bindings,
    workspace_public_config,
    workspace_reconciliation_summary,
    workspace_report,
    workspace_review_pair,
    workspace_source_data_inspect,
    workspace_status,
)
from honeymoney.workspace_setup import setup_workspace

JSON_SCHEMA_VERSION = 3


class CliUsageError(ValueError):
    """A command-line usage error that can carry a stable code."""

    code = "usage_error"


class RetiredCliContractError(CliUsageError):
    """A named error for a clean-start contract that cannot carry over."""

    code = "legacy_csv_contract_removed"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(f"{self.prog}: {message}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its public exit status."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"help", "--help", "-h"}:
        print(_help_text())
        return 0
    command, tail = arguments[0], arguments[1:]
    if command == "setup":
        return _setup_command(tail)
    if command == "import":
        return _import_command(tail)
    if command == "imports":
        return _imports_command(tail)
    if command == "views":
        return _views_command(tail)
    if command == "status":
        return _status_command(tail)
    if command == "pending":
        return _pending_command(tail)
    if command == "report":
        return _report_command(tail)
    if command == "valuation":
        return _valuation_command(tail)
    if command == "review":
        return _review_command(tail)
    if command == "correct":
        return _correct_command(tail)
    if command == "rates":
        return _rates_command(tail)
    if command == "duplicates":
        return _duplicates_command(tail)
    if command == "reconcile":
        return _reconcile_command(tail)
    if command == "profile":
        return _profile_command(tail)
    if command == "config":
        return _config_command(tail)
    if command == "learn":
        return _learn_command(tail)
    if command == "source-data":
        return _source_data_command(tail)
    if command == "evaluate":
        raise RetiredCliContractError(
            "The evaluate command was retired: it required old cumulative CSV "
            "inputs, while generated views are not durable source records."
        )
    if command == "doctor":
        return _doctor_command(tail)
    if command == "run":
        raise CliUsageError(
            "The run command was removed; use `honeymoney import PATH`."
        )
    raise CliUsageError(f"Unknown command: {command}")


def run() -> int:
    """Console-script wrapper that emits one bounded public error."""
    try:
        return main()
    except (OSError, ValueError) as error:
        arguments = sys.argv[1:]
        if "--json" in arguments:
            error_code = getattr(error, "code", None)
            _emit_json(
                _error_command(arguments),
                "error",
                errors=[
                    {
                        "type": type(error).__name__,
                        **({"code": error_code} if isinstance(error_code, str) else {}),
                        "message": str(error),
                    }
                ],
            )
        else:
            print(str(error), file=sys.stderr)
        return 2


def _setup_command(argv: list[str]) -> int:
    parser = _parser("honeymoney setup", "Create a clean Honeymoney workspace.")
    parser.add_argument("--root")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.json and not args.root:
        raise CliUsageError("honeymoney setup --json requires --root")
    root = _setup_root(args.root)
    paths = setup_workspace(root)
    if args.json:
        _emit_json(
            "setup",
            "success",
            data={"root": str(paths.root)},
            artifacts={
                "config_json": str(paths.config),
                "corrections_csv": str(paths.corrections),
                "rate_cache_json": str(paths.rates),
                "workspace_index_json": str(paths.workspace_index),
                "import_records_directory": str(paths.import_records),
            },
        )
    else:
        print(f"Created Honeymoney workspace at {paths.root}")
        print("Next: edit config.json if needed, then run honeymoney import PATH")
    return 0


def _import_command(argv: list[str]) -> int:
    parser = _parser("honeymoney import", "Import one statement file or folder.")
    parser.add_argument("path")
    parser.add_argument("--config")
    parser.add_argument("--binding")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--no-interactive", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--replace", action="store_true")
    actions.add_argument("--reset", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    action = "reset" if args.reset else "replace" if args.replace else "import"
    result = import_workspace(
        _clean_pasted_path(args.path),
        config_path=args.config,
        action=action,
        binding_id=args.binding,
        interactive=not (args.no_interactive or args.json),
        strict=args.strict,
    )
    if args.json:
        _emit_result("import", result)
    else:
        print(
            f"Imported {result.data['statement_transaction_count']} statement "
            f"transactions into {result.data['view_transaction_count']} view rows."
        )
    return 1 if args.strict and result.warnings else 0


def _imports_command(argv: list[str]) -> int:
    if not argv or argv[0] not in {"list", "show"}:
        raise CliUsageError("honeymoney imports requires `list` or `show`")
    action, tail = argv[0], argv[1:]
    parser = _parser(
        f"honeymoney imports {action}", "Inspect privacy-safe import history."
    )
    if action == "show":
        parser.add_argument("source_id")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(tail)
    result = (
        list_imports(args.config)
        if action == "list"
        else show_import(args.source_id, args.config)
    )
    if args.json:
        _emit_result(f"imports.{action}", result)
    elif action == "list":
        items = result.data["import_records"]
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise AssertionError("import list result has an invalid shape")
        for item in items:
            state = "ready" if item["ready"] else "not ready"
            print(
                f"{item['source_id']}  {item['source_label']}  {state}  "
                f"{item['statement_transaction_count']} transactions"
            )
    else:
        attempts = result.data["attempts"]
        if not isinstance(attempts, list):
            raise AssertionError("import history result has an invalid shape")
        print(f"Source: {result.data['source_id']}")
        print(f"Label: {result.data['source_label']}")
        print(f"Ready: {'yes' if result.data['ready'] else 'no'}")
        print(f"Attempts: {len(attempts)}")
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise AssertionError("import attempt result has an invalid shape")
            number = attempt.get("attempt_number")
            requested_action = attempt.get("requested_action")
            outcome = attempt.get("outcome")
            counts = attempt.get("counts")
            if (
                not isinstance(number, int)
                or not isinstance(requested_action, str)
                or not isinstance(outcome, str)
                or not isinstance(counts, dict)
            ):
                raise AssertionError("import attempt result has an invalid shape")
            statement_count = counts.get("statement_transaction_count", 0)
            print(
                f"  {number:08d}  {requested_action}  {outcome}  "
                f"statement transactions={statement_count}"
            )
            _print_attempt_codes(attempt, "warnings", "warning")
            _print_attempt_codes(attempt, "error_codes", "error")
    return 0


def _print_attempt_codes(
    attempt: dict[str, object],
    field: str,
    label: str,
) -> None:
    values = attempt.get(field, [])
    omitted = attempt.get(f"omitted_{label}_count", 0)
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise AssertionError("import attempt codes have an invalid shape")
    if not isinstance(omitted, int):
        raise AssertionError("import attempt omitted count has an invalid shape")
    if values or omitted:
        suffix = f" (+{omitted} omitted)" if omitted else ""
        print(f"    {label}s: {', '.join(values)}{suffix}")


def _views_command(argv: list[str]) -> int:
    if not argv or argv[0] != "rebuild":
        raise CliUsageError("honeymoney views requires `rebuild`")
    parser = _period_parser(
        "honeymoney views rebuild", "Rebuild complete managed view units."
    )
    args = parser.parse_args(argv[1:])
    result = rebuild_views(_selection(args), config_path=args.config)
    if args.json:
        _emit_result("views.rebuild", result)
    else:
        print(
            f"Views: {result.data['written_count']} written, "
            f"{result.data['removed_count']} removed, "
            f"{result.data['unchanged_count']} unchanged."
        )
    return 0


def _status_command(argv: list[str]) -> int:
    parser = _period_parser("honeymoney status", "Show selected workspace counts.")
    args = parser.parse_args(argv)
    result = workspace_status(_selection(args), config_path=args.config)
    if args.json:
        _emit_result("status", result)
    else:
        periods = result.data["periods"]
        if not isinstance(periods, list) or not all(
            isinstance(period, str) for period in periods
        ):
            raise AssertionError("status result has an invalid period shape")
        print(f"Period: {', '.join(periods)}")
        print(f"Imports: {result.data['import_count']}")
        print(f"Statement transactions: {result.data['statement_transaction_count']}")
        print(f"View transactions: {result.data['view_transaction_count']}")
        print(f"Needs review: {result.data['needs_review_count']}")
    return 0


def _pending_command(argv: list[str]) -> int:
    parser = _period_parser(
        "honeymoney pending", "Show selected transactions that need review."
    )
    args = parser.parse_args(argv)
    result = workspace_pending(_selection(args), config_path=args.config)
    if args.json:
        _emit_result("pending", result)
    else:
        print(f"Pending review: {result.data['pending_count']}")
        transactions = result.data["transactions"]
        if not isinstance(transactions, list):
            raise AssertionError("pending result has an invalid shape")
        for item in transactions:
            if not isinstance(item, dict):
                raise AssertionError("pending row has an invalid shape")
            print(
                f"  {item.get('transaction_id', '')}  "
                f"{item.get('review_reason_labels', '')}"
            )
    return 0


def _report_command(argv: list[str]) -> int:
    parser = _period_parser(
        "honeymoney report", "Use or export the selected self-contained report."
    )
    parser.add_argument("--export")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    result = workspace_report(
        _selection(args),
        config_path=args.config,
        export_path=args.export,
    )
    if args.json:
        _emit_result("report", result)
    else:
        target = result.artifacts["report_html"]
        print(f"Report: {target}")
    target = result.artifacts["report_html"]
    if not args.no_open and not args.json and isinstance(target, str):
        webbrowser.open(Path(target).resolve().as_uri())
    return 0


def _valuation_command(argv: list[str]) -> int:
    if not argv or argv[0] != "missing":
        raise CliUsageError("honeymoney valuation requires `missing`")
    parser = _period_parser(
        "honeymoney valuation missing", "Show selected missing HKD valuations."
    )
    args = parser.parse_args(argv[1:])
    result = workspace_missing_valuations(_selection(args), config_path=args.config)
    if args.json:
        _emit_result("valuation.missing", result)
    else:
        print(f"Missing HKD values: {result.data['missing_valuation_count']}")
    return 0


def _doctor_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney doctor", "Audit managed state without reading statements."
    )
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.config or "config.json").expanduser().absolute().parent
    from honeymoney.doctor import audit_workspace, fix_workspace

    if args.fix:
        fixed = fix_workspace(root)
        audit, repaired = fixed.after, len(fixed.applied_actions)
    else:
        audit, repaired = audit_workspace(root), 0
    findings = [
        {
            "code": item.code,
            "severity": item.severity.value,
            "repair_class": item.repair_class.value,
            **({"path": item.path} if item.path is not None else {}),
            "next_action": item.next_action,
            "detail_count": item.detail_count,
            "omitted_detail_count": item.omitted_detail_count,
        }
        for item in audit.findings
    ]
    if args.json:
        _emit_json(
            "doctor",
            "success" if audit.healthy else "action_required",
            data={
                "findings": findings,
                "finding_count": len(findings),
                "repaired_count": repaired,
                "checked_item_count": audit.checked_item_count,
            },
        )
    elif audit.healthy:
        print("Workspace is healthy.")
    else:
        for item in findings:
            path = f"  {item['path']}" if "path" in item else ""
            print(f"{item['severity']}: {item['code']}{path}")
            print(f"  {item['next_action']}")
    return audit.exit_code


def _review_command(argv: list[str]) -> int:
    if argv and argv[0] == "pair":
        return _review_pair_command(argv[1:])
    parser = _period_parser(
        "honeymoney review", "Inspect or resolve local review decisions."
    )
    parser.add_argument("--transaction")
    parser.add_argument("--as", dest="decision")
    parser.add_argument("--file", dest="decision_file")
    args = parser.parse_args(argv)
    one_shot = args.transaction is not None or args.decision is not None
    has_selector = any(
        (
            args.period is not None,
            args.month is not None,
            args.start is not None,
            args.end is not None,
            args.undated,
            args.all_periods,
        )
    )
    if one_shot and (args.transaction is None or args.decision is None):
        raise CliUsageError("Review requires both --transaction and --as")
    if one_shot and args.decision_file is not None:
        raise CliUsageError("Review accepts one decision source at a time")
    if (one_shot or args.decision_file is not None) and has_selector:
        raise CliUsageError("Saved review decisions cannot use period filters")
    if one_shot:
        result = review_workspace_transaction(
            args.transaction,
            args.decision,
            config_path=args.config,
        )
    elif args.decision_file is not None:
        result = _apply_correction_file(args.decision_file, args.config)
    else:
        result = workspace_pending(_selection(args), config_path=args.config)
    if args.json:
        _emit_result("review", result)
    elif not one_shot and args.decision_file is None:
        print(f"Pending review: {result.data['pending_count']}")
    else:
        print(f"Saved {result.data['corrected_count']} review decision(s).")
    return 0


def _review_pair_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney review pair",
        "Confirm two same-account cash movements as a manual transfer pair.",
    )
    parser.add_argument("transaction_ids", nargs=2, metavar="VIEW_TRANSACTION_ID")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.yes:
        raise CliUsageError("Manual transfer pairing requires --yes confirmation.")
    result = workspace_review_pair(args.transaction_ids, config_path=args.config)
    if args.json:
        _emit_result("review.pair", result)
    elif result.data["changed"]:
        print(f"Manual transfer pair confirmed: {result.data['pair_id']}.")
    else:
        print(f"Manual transfer pair already exists: {result.data['pair_id']}.")
    return 0


def _correct_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney correct", "Apply saved corrections from a local CSV file."
    )
    parser.add_argument("--file", required=True)
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = _apply_correction_file(args.file, args.config)
    if args.json:
        _emit_result("correct", result)
    else:
        print(f"Saved {result.data['corrected_count']} correction(s).")
    return 0


def _apply_correction_file(value: str, config_path: str | Path | None) -> CommandResult:
    raw_path = Path(_clean_pasted_path(value)).expanduser()
    if raw_path.is_symlink():
        raise CliUsageError("Correction file does not exist or is unsafe")
    path = raw_path.resolve(strict=False)
    if not path.is_file():
        raise CliUsageError("Correction file does not exist or is unsafe")
    context = load_workspace(config_path)
    batch_config = {**context.config, "corrections": str(path)}
    try:
        patches = load_corrections(batch_config)
    except (OSError, UnicodeError, ValueError) as error:
        raise CliUsageError("Correction file is invalid") from error
    if not patches:
        raise CliUsageError("Correction file has no choices")
    return apply_workspace_corrections(patches, config_path=config_path)


def _rates_command(argv: list[str]) -> int:
    if not argv or argv[0] not in {"import", "fetch"}:
        raise CliUsageError("honeymoney rates requires `import` or `fetch`")
    if argv[0] == "import":
        return _rates_import_command(argv[1:])
    return _rates_fetch_command(argv[1:])


def _duplicates_command(argv: list[str]) -> int:
    if argv and argv[0] == "resolve":
        parser = _parser(
            "honeymoney duplicates resolve", "Save one exact duplicate choice."
        )
        parser.add_argument("group_id")
        parser.add_argument("--as", dest="choice", required=True)
        parser.add_argument("--config")
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv[1:])
        result = resolve_workspace_duplicate(
            args.group_id,
            args.choice,
            config_path=args.config,
        )
        command = "duplicates.resolve"
    else:
        parser = _parser(
            "honeymoney duplicates", "List unresolved exact duplicate groups."
        )
        parser.add_argument("--config")
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        result = workspace_duplicates(config_path=args.config)
        command = "duplicates"
    if args.json:
        _emit_result(command, result)
    elif command == "duplicates":
        print(f"Unresolved duplicate groups: {result.data['duplicate_group_count']}")
    else:
        print(
            "Duplicate choice saved; "
            f"{result.data['remaining_duplicate_group_count']} group(s) remain."
        )
    return 0


def _reconcile_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney reconcile",
        "Inspect current transfer and balance reconciliation.",
    )
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = workspace_reconciliation_summary(config_path=args.config)
    result = CommandResult(
        data={**result.data, "dry_run": args.dry_run},
        artifacts=result.artifacts,
        warnings=result.warnings,
    )
    if args.json:
        _emit_result("reconcile", result)
    else:
        print(
            f"Reconciliation: {result.data['view_transaction_count']} view "
            f"transactions, {result.data['paired_groups']} paired group(s), "
            f"{result.data['ambiguous_transactions']} ambiguous."
        )
    return 0


def _learn_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney learn", "Build exact managed rules from saved local reviews."
    )
    parser.add_argument("--config")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = learn_workspace_rules(config_path=args.config, apply=args.yes)
    if args.json:
        _emit_json(
            "learn",
            "success" if args.yes else "dry_run",
            data=result.data,
            artifacts=result.artifacts,
            warnings=list(result.warnings),
        )
    else:
        mode = "Updated" if args.yes else "Dry run"
        print(
            f"{mode}: {result.data['candidates']} candidates, "
            f"{result.data['broad_rules']} broad rules, "
            f"{result.data['amount_specific_rules']} amount-specific rules."
        )
        if not args.yes:
            print("Run again with --yes to update managed rules.")
    return 0


def _source_data_command(argv: list[str]) -> int:
    if not argv or argv[0] not in {"inspect", "resolve"}:
        raise CliUsageError("honeymoney source-data requires `inspect` or `resolve`")
    action, tail = argv[0], argv[1:]
    parser = _parser(
        f"honeymoney source-data {action}",
        "Inspect or clear one stored source-data review state.",
    )
    parser.add_argument("transaction_id", metavar="VIEW_TRANSACTION_ID")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(tail)
    result = (
        workspace_source_data_inspect(args.transaction_id, config_path=args.config)
        if action == "inspect"
        else resolve_workspace_source_data(args.transaction_id, config_path=args.config)
    )
    if args.json:
        _emit_result(f"source-data.{action}", result)
    elif action == "inspect":
        transaction = result.data["transaction"]
        if not isinstance(transaction, dict):
            raise AssertionError("source-data inspection has an invalid shape")
        print(
            f"Source-data evidence: {transaction['evidence_status']} "
            f"({transaction['source_occurrence_count']} source occurrence(s))."
        )
    elif result.data["changed"]:
        print("Cleared the stale source-data review state.")
    else:
        print("Source-data review state is already clear.")
    return 0


def _profile_command(argv: list[str]) -> int:
    if argv and argv[0] == "bind":
        return _profile_bind_command(argv[1:])
    if argv and argv[0] == "bindings":
        return _profile_bindings_command(argv[1:])
    if argv and argv[0] == "replace-pattern":
        return _profile_replace_pattern_command(argv[1:])
    if argv and argv[0] == "remove-pattern":
        return _profile_remove_pattern_command(argv[1:])
    parser = _parser("honeymoney profile", "Validate a local import profile.")
    parser.add_argument("operation", choices=["validate"])
    parser.add_argument("profile_path")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    raw_profile_path = Path(args.profile_path).expanduser()
    if raw_profile_path.is_symlink():
        raise CliUsageError("Profile path does not exist or is unsafe")
    profile_path = raw_profile_path.resolve(strict=False)
    if not profile_path.is_file():
        raise CliUsageError("Profile path does not exist or is unsafe")
    if args.config is not None or Path("config.json").is_file():
        context = load_workspace(args.config)
        config = context.config
    else:
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0, "USD": 7.8},
            "pdf": {"enabled": True, "parser": "pdfplumber"},
        }
    try:
        value: object = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CliUsageError("Profile is not valid JSON") from error
    try:
        profile = importers._validate_profile(value, profile_path, config)
    except (TypeError, ValueError) as error:
        raise CliUsageError("Profile is invalid") from error
    profile_id = str(profile.get("id") or profile["account_id"])
    parsers = [name for name in ("csv", "pdf") if name in profile]
    data: dict[str, object] = {
        "parsers": parsers,
        "profile_id": profile_id,
        "profile_path": str(profile_path),
    }
    if args.json:
        _emit_json("profile.validate", "success", data=data)
    else:
        print(f"Profile {profile_id} is valid ({', '.join(parsers)}).")
    return 0


def _profile_bind_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney profile bind",
        "Save one filename-to-account binding.",
    )
    parser.add_argument("binding_id")
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--profile", dest="profile_id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument(
        "--account",
        action="append",
        required=True,
        metavar="SOURCE_ID=ACCOUNT_ID=ACCOUNT_NAME",
    )
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    binding_id = _required_profile_value("binding id", args.binding_id)
    pattern = _required_profile_value("filename pattern", args.pattern)
    profile_id = _required_profile_value("profile id", args.profile_id)
    owner = _required_profile_value("owner", args.owner)
    context = load_workspace(args.config)
    profiles, mappings = _profile_cli_inputs(context.config)
    try:
        if profile_id not in {
            str(profile.get("id") or profile.get("account_id") or "")
            for profile in profiles
        }:
            raise ValueError(f"Unknown profile for account binding: {profile_id}")
        next_mappings = upsert_binding(
            mappings,
            {
                "id": binding_id,
                "profile": profile_id,
                "owner": owner,
                "accounts": [_parse_bound_account(value) for value in args.account],
            },
            pattern,
        )
    except ValueError as error:
        raise CliUsageError("Account binding values are invalid") from error
    result = apply_workspace_profile_mappings(
        next_mappings,
        config_path=args.config,
        expected_generation_id=context.index["generation_id"],
    )
    binding = next(
        item for item in binding_views(next_mappings) if item.get("id") == binding_id
    )
    result = CommandResult(
        data={**result.data, "binding": binding},
        artifacts={
            **result.artifacts,
            "profile_mappings_json": str(context.config["profile_mappings"]),
        },
        warnings=result.warnings,
    )
    if args.json:
        _emit_result("profile.bind", result)
    else:
        print(f"Saved account binding {binding_id} for {pattern}.")
    return 0


def _profile_bindings_command(argv: list[str]) -> int:
    parser = _parser("honeymoney profile bindings", "List saved account bindings.")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = workspace_profile_bindings(config_path=args.config)
    if args.json:
        _emit_result("profile.bindings", result)
    else:
        bindings = result.data["bindings"]
        if not isinstance(bindings, list):
            raise AssertionError("binding list result has an invalid shape")
        if not bindings:
            print("No account bindings are saved.")
        for binding in bindings:
            if not isinstance(binding, dict):
                raise AssertionError("binding result has an invalid shape")
            print(
                f"{binding['id']}: profile={binding['profile']} "
                f"owner={binding['owner']} patterns={', '.join(binding['patterns'])}"
            )
    return 0


def _profile_replace_pattern_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney profile replace-pattern",
        "Replace one saved binding filename pattern.",
    )
    parser.add_argument("binding_id")
    parser.add_argument("--old-pattern", required=True)
    parser.add_argument("--new-pattern", required=True)
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    binding_id = _required_profile_value("binding id", args.binding_id)
    old_pattern = _required_profile_value("old filename pattern", args.old_pattern)
    new_pattern = _required_profile_value("new filename pattern", args.new_pattern)
    context = load_workspace(args.config)
    _profiles, mappings = _profile_cli_inputs(context.config)
    try:
        next_mappings, changed = replace_binding_pattern(
            mappings,
            binding_id,
            old_pattern,
            new_pattern,
        )
    except ValueError as error:
        raise CliUsageError("Account binding pattern change is invalid") from error
    result = apply_workspace_profile_mappings(
        next_mappings,
        config_path=args.config,
        expected_generation_id=context.index["generation_id"],
    )
    binding = next(
        item for item in binding_views(next_mappings) if item.get("id") == binding_id
    )
    data = {
        **result.data,
        "binding_id": binding_id,
        "changed": changed,
        "new_pattern": new_pattern,
        "old_pattern": old_pattern,
        "profile": binding["profile"],
        "result": "replaced" if changed else "already_replaced",
    }
    result = CommandResult(
        data=data,
        artifacts={
            **result.artifacts,
            "profile_mappings_json": str(context.config["profile_mappings"]),
        },
        warnings=result.warnings,
    )
    if args.json:
        _emit_result("profile.replace-pattern", result)
    elif changed:
        print(f"Replaced {old_pattern} with {new_pattern} for {binding_id}.")
    else:
        print(f"Account binding {binding_id} already uses {new_pattern}.")
    return 0


def _profile_remove_pattern_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney profile remove-pattern",
        "Remove one saved binding filename pattern.",
    )
    parser.add_argument("binding_id")
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    binding_id = _required_profile_value("binding id", args.binding_id)
    pattern = _required_profile_value("filename pattern", args.pattern)
    context = load_workspace(args.config)
    _profiles, mappings = _profile_cli_inputs(context.config)
    try:
        next_mappings, changed, binding_removed, profile = remove_binding_pattern(
            mappings,
            binding_id,
            pattern,
            confirm_final=args.yes,
        )
    except ValueError as error:
        raise CliUsageError("Account binding pattern removal is invalid") from error
    result = apply_workspace_profile_mappings(
        next_mappings,
        config_path=args.config,
        expected_generation_id=context.index["generation_id"],
    )
    result = CommandResult(
        data={
            **result.data,
            "binding_id": binding_id,
            "binding_removed": binding_removed,
            "changed": changed,
            "pattern": pattern,
            "profile": profile,
            "result": "removed" if changed else "already_removed",
        },
        artifacts={
            **result.artifacts,
            "profile_mappings_json": str(context.config["profile_mappings"]),
        },
        warnings=result.warnings,
    )
    if args.json:
        _emit_result("profile.remove-pattern", result)
    elif changed:
        suffix = " and removed the binding" if binding_removed else ""
        print(f"Removed {pattern} from account binding {binding_id}{suffix}.")
    else:
        print(f"Account binding {binding_id} pattern {pattern} is already removed.")
    return 0


def _required_profile_value(label: str, value: str) -> str:
    result = value.strip()
    if not result:
        raise CliUsageError(f"Account {label} must be a non-empty string")
    return result


def _profile_cli_inputs(
    config: dict[str, object],
) -> tuple[list[Profile], dict[str, object]]:
    """Load profile inputs without echoing their values on failure."""
    try:
        return (
            importers._load_profiles(config),
            importers._load_profile_mappings(config),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise CliUsageError("Workspace profiles or mappings are invalid") from error


def _parse_bound_account(value: str) -> BoundAccount:
    parts = value.split("=", 2)
    if len(parts) != 3 or any(not part.strip() for part in parts):
        raise CliUsageError(
            "--account must use SOURCE_ID=ACCOUNT_ID=ACCOUNT_NAME with no empty field"
        )
    return {
        "source_account_id": parts[0].strip(),
        "account_id": parts[1].strip(),
        "account": parts[2].strip(),
    }


def _config_command(argv: list[str]) -> int:
    parser = _parser("honeymoney config", "View or edit the workspace config.")
    parser.add_argument("action", nargs="?", choices=["edit"])
    parser.add_argument("section", nargs="?", choices=["ollama"])
    parser.add_argument("--config")
    parser.add_argument("--model")
    enabled = parser.add_mutually_exclusive_group()
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.action is None:
        if args.section or args.model or args.enable or args.disable:
            raise CliUsageError("Config changes require `honeymoney config edit`")
        result = workspace_public_config(config_path=args.config)
        if args.json:
            _emit_result("config", result)
        else:
            print(json.dumps(result.data["config"], indent=2, sort_keys=True))
        return 0
    if args.section is None:
        if args.json:
            raise CliUsageError("honeymoney config edit does not support --json")
        if args.model or args.enable or args.disable:
            raise CliUsageError(
                "Ollama options require `honeymoney config edit ollama`"
            )
        context, raw_config = workspace_config_document(config_path=args.config)
        edited = _edit_config_in_editor(context.paths.config, raw_config)
        result = apply_workspace_config(
            edited,
            config_path=args.config,
            expected_generation_id=context.index["generation_id"],
        )
        print(f"Updated {context.paths.config}")
        if result.data["written_count"]:
            print(f"Refreshed {result.data['written_count']} view(s).")
        return 0
    if args.disable and args.model:
        raise CliUsageError("Use either --disable or --model, not both")
    if args.model is not None and not args.model.strip():
        raise CliUsageError("--model must be a non-empty Ollama model name")
    if not args.model and not args.enable and not args.disable:
        raise CliUsageError(
            "Use --model, --enable, or --disable with `honeymoney config edit ollama`"
        )
    context, raw_config = workspace_config_document(config_path=args.config)
    ollama = raw_config.get("ollama", {})
    if not isinstance(ollama, dict):
        raise CliUsageError("Config field ollama must be a JSON object")
    updated_ollama = copy.deepcopy(ollama)
    if args.model:
        updated_ollama["model"] = args.model.strip()
        updated_ollama["enabled"] = True
    elif args.enable:
        _require_available_ollama_model(updated_ollama)
        updated_ollama["enabled"] = True
    else:
        updated_ollama["enabled"] = False
    candidate = {**raw_config, "ollama": updated_ollama}
    result = apply_workspace_config(
        candidate,
        config_path=args.config,
        expected_generation_id=context.index["generation_id"],
    )
    data = {
        **result.data,
        "ollama": {
            "enabled": bool(updated_ollama.get("enabled", False)),
            "model": str(updated_ollama.get("model", "")),
        },
    }
    result = CommandResult(
        data=data,
        artifacts=result.artifacts,
        warnings=result.warnings,
    )
    if args.json:
        _emit_result("config", result)
    elif updated_ollama.get("enabled"):
        print(f"Ollama enabled with model {updated_ollama.get('model', '(not set)')}")
    else:
        print("Ollama disabled")
    return 0


def _require_available_ollama_model(ollama: dict[str, object]) -> None:
    model = str(ollama.get("model", "")).strip()
    if not model:
        raise CliUsageError("Set an Ollama model with --model before enabling it")
    models = list_ollama_models(ollama)
    aliases = {
        alias for item in models for alias in (item, item.removesuffix(":latest"))
    }
    if model not in aliases:
        raise CliUsageError(
            "Configured Ollama model is not installed; pass --model or install it first"
        )


def _edit_config_in_editor(
    config_path: Path, raw_config: dict[str, object]
) -> dict[str, object]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise CliUsageError("Set $VISUAL or $EDITOR before running config edit")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.stem}.",
        suffix=".json",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(raw_config, indent=2, sort_keys=True) + "\n")
        completed = subprocess.run(
            [*shlex.split(editor), str(temporary_path)], check=False
        )
        if completed.returncode != 0:
            raise CliUsageError(f"Editor exited with status {completed.returncode}")
        try:
            edited = json.loads(temporary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CliUsageError("Edited config is not valid JSON") from error
        if not isinstance(edited, dict):
            raise CliUsageError("Edited config must be a JSON object")
        return edited
    finally:
        temporary_path.unlink(missing_ok=True)


def _rates_import_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney rates import", "Import a downloaded HKMA daily-rate document."
    )
    parser.add_argument("file")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    context = load_workspace(args.config)
    raw_path = Path(_clean_pasted_path(args.file)).expanduser()
    if raw_path.is_symlink():
        raise CliUsageError("Rate document does not exist or is unsafe")
    path = raw_path.resolve(strict=False)
    if not path.is_file():
        raise CliUsageError("Rate document does not exist")
    observations = parse_hkma_daily_document(
        path.read_bytes(),
        base_currency=str(context.config.get("base_currency", "HKD")),
    )
    result = apply_workspace_rate_observations(
        observations,
        config_path=args.config,
    )
    if args.json:
        _emit_result("rates.import", result)
    else:
        print(
            f"Imported {result.data['imported_observation_count']} observations; "
            f"{result.data['resolved_transaction_date_count']} transaction dates "
            "resolved."
        )
    return 0


def _rates_fetch_command(argv: list[str]) -> int:
    parser = _parser(
        "honeymoney rates fetch", "Fetch approved public HKMA daily-rate fields."
    )
    parser.add_argument("currencies", nargs="+")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    context = load_workspace(args.config)
    request = prepare_hkma_fetch(
        args.currencies,
        start=args.start,
        end=args.end,
        base_currency=str(context.config.get("base_currency", "HKD")),
    )
    if not args.allow_network:
        if args.json or not sys.stdin.isatty():
            raise CliUsageError("Non-interactive rate fetch requires --allow-network.")
        print(f"Requested range: {request.start} to {request.end}")
        print(f"Currencies: {', '.join(request.currencies)}")
        if input("Fetch these public rates now? [y/N] ").strip().casefold() not in {
            "y",
            "yes",
        }:
            print("Rate fetch cancelled.")
            return 0
    fetched = fetch_hkma_daily_rates(request)
    base_result = apply_workspace_rate_observations(
        fetched.observations,
        config_path=args.config,
    )
    result = CommandResult(
        data={
            **base_result.data,
            "network_access": True,
            "requested_currencies": list(request.currencies),
            "requested_range": {"start": request.start, "end": request.end},
            "fetched_page_count": len(fetched.request_urls),
        },
        artifacts=base_result.artifacts,
        warnings=base_result.warnings,
    )
    if args.json:
        _emit_result("rates.fetch", result)
    else:
        print(
            f"Fetched {result.data['imported_observation_count']} observations; "
            f"{result.data['resolved_transaction_date_count']} transaction dates "
            "resolved."
        )
    return 0


def _period_parser(prog: str, description: str) -> _Parser:
    parser = _parser(prog, description)
    parser.add_argument("period", nargs="?")
    parser.add_argument("--month")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--undated", action="store_true")
    parser.add_argument("--all", dest="all_periods", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    return parser


def _selection(args: argparse.Namespace) -> PeriodSelection:
    return resolve_period_selection(
        args.period,
        month=args.month,
        start=args.start,
        end=args.end,
        undated=args.undated,
        all_periods=args.all_periods,
    )


def _parser(prog: str, description: str) -> _Parser:
    return _Parser(prog=prog, description=description, add_help=True)


def _setup_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().absolute()
    try:
        supplied = input("Root folder [./money]: ").strip()
    except EOFError:
        supplied = ""
    return Path(supplied or "./money").expanduser().absolute()


def _clean_pasted_path(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1]
    if not cleaned:
        raise CliUsageError("Import path must not be empty")
    return cleaned


def _emit_result(command: str, result: CommandResult) -> None:
    _emit_json(
        command,
        "success",
        data=result.data,
        artifacts=result.artifacts,
        warnings=list(result.warnings),
    )


def _emit_json(
    command: str,
    status: str,
    *,
    data: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
    warnings: Sequence[object] | None = None,
    errors: Sequence[object] | None = None,
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


def _error_command(argv: Sequence[str]) -> str:
    if len(argv) > 1 and argv[0] in {
        "duplicates",
        "imports",
        "rates",
        "valuation",
        "views",
    }:
        return f"{argv[0]}.{argv[1]}"
    return argv[0] if argv else "help"


def _help_text() -> str:
    return """Honeymoney 0.2.0

Local, clean-start household statement storage and monthly views.

Commands:
  honeymoney setup [--root DIR]
  honeymoney import PATH [--replace | --reset] [--binding ID]
  honeymoney imports list
  honeymoney imports show SOURCE_ID
  honeymoney views rebuild [PERIOD | --month MONTH | --start DATE --end DATE | --undated | --all]
  honeymoney status [PERIOD | --month MONTH | --start DATE --end DATE | --undated | --all]
  honeymoney pending [PERIOD | --month MONTH | --start DATE --end DATE | --undated | --all]
  honeymoney report [PERIOD | --month MONTH | --start DATE --end DATE | --undated | --all] [--export PATH]
  honeymoney valuation missing [PERIOD | --month MONTH | --start DATE --end DATE | --undated | --all]
  honeymoney review --transaction VIEW_TRANSACTION_ID --as DECISION
  honeymoney review pair VIEW_TRANSACTION_ID VIEW_TRANSACTION_ID --yes
  honeymoney correct --file corrections.csv
  honeymoney learn [--yes]
  honeymoney source-data inspect VIEW_TRANSACTION_ID
  honeymoney source-data resolve VIEW_TRANSACTION_ID
  honeymoney rates import hkma-rates.json
  honeymoney rates fetch USD [EUR ...] --start DATE --end DATE --allow-network
  honeymoney duplicates [resolve GROUP_ID --as same-event|keep-all]
  honeymoney profile validate PROFILE
  honeymoney profile bind ID --pattern PATTERN --profile PROFILE --owner OWNER --account SOURCE_ID=ACCOUNT_ID=ACCOUNT_NAME
  honeymoney profile bindings
  honeymoney profile replace-pattern ID --old-pattern PATTERN --new-pattern PATTERN
  honeymoney profile remove-pattern ID --pattern PATTERN --yes
  honeymoney config [edit [ollama --model MODEL | --enable | --disable]]
  honeymoney reconcile [--dry-run]
  honeymoney doctor [--fix]

Common options:
  --config config.json
  --json

Python 3.14.6 is required. Version 0.1 workspaces need a fresh setup; Honeymoney
does not migrate or change them.

The former `evaluate` CSV comparison command is retired because its cumulative
CSV inputs are not part of the clean-start workspace contract.
"""


if __name__ == "__main__":
    raise SystemExit(run())
