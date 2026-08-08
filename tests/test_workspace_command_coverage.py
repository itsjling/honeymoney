from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from honeymoney import cli
from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.csv_artifacts import csv_document
from honeymoney.periods import resolve_period_selection
from honeymoney.workspace_commands import (
    WorkspaceCommandError,
    apply_workspace_corrections,
    apply_workspace_rate_observations,
    import_workspace,
    list_imports,
    load_workspace,
    rebuild_views,
    resolve_workspace_duplicate,
    review_workspace_transaction,
    show_import,
    workspace_duplicates,
    workspace_missing_valuations,
    workspace_pending,
    workspace_report,
    workspace_status,
)
from honeymoney.workspace_index import (
    WORKSPACE_INDEX_SCHEMA_VERSION,
    load_workspace_index,
    write_workspace_index,
)
from honeymoney.workspace_setup import setup_workspace


class WorkspaceCommandCoverageTest(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        return setup_workspace(root).config

    def _source(self, root: Path, name: str, rows: str) -> Path:
        source = root / name
        source.write_text(
            "Date,Description,Amount,Currency\n" + rows,
            encoding="utf-8",
        )
        return source

    def _transaction_id(self, root: Path, period: str = "2026-08") -> str:
        with (root / "views" / period / "transactions.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            row = next(csv.DictReader(handle))
        return row["transaction_id"]

    def _run_main(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["honeymoney", *arguments]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = cli.run()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_load_and_import_input_errors_keep_a_clean_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(root / "missing" / "config.json")
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

            malformed = root / "malformed"
            malformed.mkdir()
            (malformed / "config.json").write_text("{", encoding="utf-8")
            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(malformed / "config.json")
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

            scalar = root / "scalar"
            scalar.mkdir()
            (scalar / "config.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(scalar / "config.json")
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

            invalid_index = root / "invalid-index"
            index_config = self._workspace(invalid_index)
            (invalid_index / ".honeymoney/workspace-index.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(index_config)
            self.assertEqual(raised.exception.code, "workspace_index_invalid")

            guarded = root / "guarded"
            guarded_config = self._workspace(guarded)
            guarded_paths = setup_workspace(guarded)
            guarded_paths.journal.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(guarded_config)
            self.assertEqual(raised.exception.code, "publication_recovery_required")

            clean = root / "clean"
            config = self._workspace(clean)
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    clean,
                    config_path=config,
                    action="unsupported",
                    interactive=False,
                )
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    clean / "missing.csv",
                    config_path=config,
                    interactive=False,
                )
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

            empty = clean / "empty"
            empty.mkdir()
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(empty, config_path=config, interactive=False)
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

            target = self._source(
                clean,
                "target.csv",
                "2026-08-08,Synthetic target,-1,HKD\n",
            )
            source_link = clean / "source-link.csv"
            os.symlink(target, source_link)
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(source_link, config_path=config, interactive=False)
            self.assertEqual(raised.exception.code, "managed_path_unsafe")

            linked_directory = clean / "linked-directory"
            linked_directory.mkdir()
            os.symlink(target, linked_directory / "linked.csv")
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    linked_directory, config_path=config, interactive=False
                )
            self.assertEqual(raised.exception.code, "managed_path_unsafe")

            directory = clean / "sources"
            directory.mkdir()
            self._source(
                directory, "synthetic.csv", "2026-08-08,Synthetic item,-1,HKD\n"
            )
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    directory,
                    config_path=config,
                    binding_id="synthetic-binding",
                    interactive=False,
                )
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

    def test_load_rejects_every_newer_workspace_contract(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("honeymoney_version", "0.2.1"),
            ("import_record_schema_version", 2),
            ("attempt_schema_version", 2),
            ("transaction_schema_version", 2),
            ("derivation_contract", "f" * 64),
        )
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            for name, value in cases:
                with self.subTest(contract=name):
                    root = temporary_path / name
                    config = self._workspace(root)
                    index_path = root / ".honeymoney" / "workspace-index.json"
                    index = load_workspace_index(index_path)
                    contracts = dict(index["contracts"])
                    contracts[name] = value
                    index["contracts"] = contracts  # type: ignore[typeddict-item]
                    if name == "derivation_contract":
                        index["registered_views"] = [
                            {"period": "2026-08", "content_proof": "a" * 64}
                        ]
                    write_workspace_index(index_path, index)

                    with self.assertRaises(WorkspaceCommandError) as raised:
                        load_workspace(config)

                    self.assertEqual(raised.exception.code, "newer_honeymoney_required")

            root = temporary_path / "workspace-index-schema"
            config = self._workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            document = json.loads(index_path.read_text(encoding="utf-8"))
            document["schema_version"] = WORKSPACE_INDEX_SCHEMA_VERSION + 1
            document["future_contract"] = {"synthetic": True}
            index_path.write_text(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(config)

            self.assertEqual(raised.exception.code, "newer_honeymoney_required")

    def test_unmatched_profile_rejects_before_a_plain_import_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            source = root / "synthetic.csv"
            source.write_text("Wrong,Header\nnot,a,statement\n", encoding="utf-8")

            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(source, config_path=config, interactive=False)
            self.assertEqual(raised.exception.code, "workspace_input_invalid")

            listed = list_imports(config)
            self.assertEqual(listed.data["import_count"], 0)
            self.assertFalse((root / ".honeymoney/publication-journal.json").exists())

            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic retry,-12.00,HKD\n",
                encoding="utf-8",
            )
            retried = import_workspace(source, config_path=config, interactive=False)
            self.assertEqual(retried.data["import_count"], 1)
            source_id = str(list_imports(config).data["import_records"][0]["source_id"])
            shown = show_import(source_id, config)
            self.assertTrue(shown.data["ready"])
            self.assertEqual(
                [item["attempt_number"] for item in shown.data["attempts"]], [1]
            )

            with self.assertRaises(WorkspaceCommandError) as raised:
                show_import("src_" + "0" * 64, config)
            self.assertEqual(raised.exception.code, "import_record_not_found")
            with self.assertRaises(WorkspaceCommandError) as raised:
                show_import("not-a-source-id", config)
            self.assertEqual(raised.exception.code, "import_record_invalid")

    def test_replace_reset_and_full_rebuild_use_workspace_generation_rules(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            source = self._source(
                root,
                "synthetic.csv",
                "2026-08-08,Synthetic first,-12.00,HKD\n",
            )
            import_workspace(source, config_path=config, interactive=False)

            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic replacement,-13.00,HKD\n",
                encoding="utf-8",
            )
            replaced = import_workspace(
                source,
                config_path=config,
                action="replace",
                interactive=False,
            )
            self.assertEqual(replaced.data["import_count"], 1)
            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(source, config_path=config, interactive=False)
            self.assertEqual(raised.exception.code, "source_already_imported")

            transaction_id = self._transaction_id(root)
            apply_workspace_corrections(
                {transaction_id: {"notes": "Synthetic saved choice"}},
                config_path=config,
            )
            self.assertIn(transaction_id, (root / "corrections.csv").read_text())

            reset = import_workspace(
                source,
                config_path=config,
                action="reset",
                interactive=False,
            )
            self.assertEqual(reset.data["import_count"], 1)
            self.assertNotIn(transaction_id, (root / "corrections.csv").read_text())

            rules = root / "rules.json"
            rules.write_text(rules.read_text(encoding="utf-8") + " ", encoding="utf-8")
            month = resolve_period_selection(month="2026-08")
            with self.assertRaises(WorkspaceCommandError) as raised:
                rebuild_views(month, config_path=config)
            self.assertEqual(raised.exception.code, "full_rebuild_required")

            all_periods = resolve_period_selection(all_periods=True)
            rebuilt = rebuild_views(all_periods, config_path=config)
            self.assertEqual(rebuilt.data["removed_count"], 0)
            unchanged = rebuild_views(month, config_path=config)
            self.assertEqual(unchanged.data["written_count"], 0)

            source.write_text("Date,Description,Amount,Currency\n", encoding="utf-8")
            emptied = import_workspace(
                source,
                config_path=config,
                action="replace",
                interactive=False,
            )
            self.assertEqual(emptied.data["statement_transaction_count"], 0)
            cleaned = rebuild_views(all_periods, config_path=config)
            self.assertEqual(cleaned.data["removed_count"], 1)
            self.assertFalse((root / "views" / "2026-08").exists())

    def test_queries_reports_corrections_reviews_and_rates_use_public_services(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            source = self._source(
                root,
                "synthetic.csv",
                "2026-08-08,Synthetic local,-12.00,HKD\n"
                "2026-08-09,Synthetic foreign,-2.00,EUR\n"
                "2026-09-01,Synthetic next,-3.00,HKD\n",
            )
            import_workspace(source, config_path=config, interactive=False)
            august = resolve_period_selection(month="2026-08")
            period_range = resolve_period_selection(
                start="2026-08-08", end="2026-09-01"
            )

            status = workspace_status(august, config_path=config)
            self.assertEqual(status.data["import_count"], 1)
            self.assertEqual(status.data["view_transaction_count"], 2)
            pending = workspace_pending(august, config_path=config)
            self.assertEqual(pending.data["pending_count"], 2)
            missing = workspace_missing_valuations(august, config_path=config)
            self.assertEqual(missing.data["missing_valuation_count"], 1)

            managed = workspace_report(august, config_path=config)
            self.assertTrue(Path(str(managed.artifacts["report_html"])).is_file())
            preview = workspace_report(period_range, config_path=config)
            self.assertTrue(Path(str(preview.artifacts["report_html"])).is_file())
            exported = root / "exported.html"
            workspace_report(august, config_path=config, export_path=exported)
            self.assertTrue(exported.is_file())
            with self.assertRaises(WorkspaceCommandError) as raised:
                workspace_report(
                    august,
                    config_path=config,
                    export_path=root / "views" / "unsafe.html",
                )
            self.assertEqual(raised.exception.code, "managed_path_unsafe")

            report = root / "views" / "2026-08" / "report.html"
            original_report = report.read_text(encoding="utf-8")
            report.write_text("changed", encoding="utf-8")
            with self.assertRaises(WorkspaceCommandError) as raised:
                workspace_report(august, config_path=config)
            self.assertEqual(raised.exception.code, "generated_view_invalid")
            report.write_text(original_report, encoding="utf-8")

            transaction_id = self._transaction_id(root)
            with self.assertRaises(WorkspaceCommandError) as raised:
                apply_workspace_corrections({}, config_path=config)
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            with self.assertRaises(WorkspaceCommandError) as raised:
                apply_workspace_corrections(
                    {"unknown": {"notes": "Synthetic"}}, config_path=config
                )
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            with self.assertRaises(WorkspaceCommandError) as raised:
                apply_workspace_corrections(
                    {transaction_id: {"unknown_field": "Synthetic"}},
                    config_path=config,
                )
            self.assertEqual(raised.exception.code, "corrections_invalid")
            with self.assertRaises(WorkspaceCommandError) as raised:
                apply_workspace_corrections(
                    {transaction_id: {"notes": "Synthetic"}},
                    config_path=config,
                    expected_generation_id="stale-generation",
                )
            self.assertEqual(raised.exception.code, "workspace_busy")

            with self.assertRaises(WorkspaceCommandError) as raised:
                review_workspace_transaction("unknown", "expense", config_path=config)
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            with self.assertRaises(WorkspaceCommandError) as raised:
                review_workspace_transaction(
                    transaction_id, "not-a-decision", config_path=config
                )
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            reviewed = review_workspace_transaction(
                transaction_id, "expense", config_path=config
            )
            self.assertEqual(reviewed.data["corrected_count"], 1)

            with self.assertRaises(WorkspaceCommandError) as raised:
                apply_workspace_rate_observations([{}], config_path=config)
            self.assertEqual(raised.exception.code, "rate_cache_invalid")
            first_rates = apply_workspace_rate_observations([], config_path=config)
            self.assertEqual(first_rates.data["imported_observation_count"], 0)
            second_rates = apply_workspace_rate_observations([], config_path=config)
            self.assertEqual(second_rates.data["written_count"], 0)

            report.unlink()
            with self.assertRaises(WorkspaceCommandError) as raised:
                workspace_report(august, config_path=config)
            self.assertEqual(raised.exception.code, "generated_view_invalid")

    def test_duplicate_service_resolution_handles_errors_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            sources = root / "sources"
            sources.mkdir()
            self._source(
                sources,
                "one.csv",
                "2026-08-08,Synthetic equal,-12.00,HKD\n"
                "2026-08-08,Synthetic equal,-12.00,HKD\n",
            )
            self._source(
                sources,
                "two.csv",
                "2026-08-08,Synthetic equal,-12.00,HKD\n",
            )
            import_workspace(sources, config_path=config, interactive=False)

            groups = workspace_duplicates(config_path=config).data["groups"]
            self.assertEqual(len(groups), 1)
            group_id = str(groups[0]["group_id"])
            with self.assertRaises(WorkspaceCommandError) as raised:
                resolve_workspace_duplicate("unknown", "same-event", config_path=config)
            self.assertEqual(raised.exception.code, "duplicate_group_unknown")

            code, output, errors = self._run_main(
                "duplicates",
                "resolve",
                group_id,
                "--as",
                "same-event",
                "--config",
                str(config),
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Duplicate choice saved", output)
            repeated = resolve_workspace_duplicate(
                group_id, "same-event", config_path=config
            )
            self.assertTrue(repeated.data["idempotent"])
            with self.assertRaises(WorkspaceCommandError) as raised:
                resolve_workspace_duplicate(group_id, "keep-all", config_path=config)
            self.assertEqual(raised.exception.code, "duplicate_resolution_conflict")

    def test_cli_human_output_runs_each_workspace_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            code, output, errors = self._run_main("setup", "--root", str(root))
            self.assertEqual(code, 0, errors)
            self.assertIn("Created Honeymoney workspace", output)
            config = root / "config.json"
            source = self._source(
                root,
                "synthetic.csv",
                "2026-08-08,Synthetic CLI,-12.00,HKD\n",
            )

            code, output, errors = self._run_main(
                "import",
                f'"{source}"',
                "--config",
                str(config),
                "--no-interactive",
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Imported 1 statement transactions", output)
            source_id = next((root / ".honeymoney/import-records").iterdir()).name

            for command, expected in (
                (("imports", "list", "--config", str(config)), "ready"),
                (
                    ("imports", "show", source_id, "--config", str(config)),
                    "00000001  import  success  statement transactions=1",
                ),
                (
                    ("views", "rebuild", "--month", "2026-08", "--config", str(config)),
                    "Views:",
                ),
                (
                    ("status", "--month", "2026-08", "--config", str(config)),
                    "Needs review:",
                ),
                (
                    ("pending", "--month", "2026-08", "--config", str(config)),
                    "Pending review:",
                ),
                (
                    (
                        "valuation",
                        "missing",
                        "--month",
                        "2026-08",
                        "--config",
                        str(config),
                    ),
                    "Missing HKD values:",
                ),
                (
                    ("duplicates", "--config", str(config)),
                    "Unresolved duplicate groups:",
                ),
            ):
                code, output, errors = self._run_main(*command)
                self.assertEqual(code, 0, errors)
                self.assertIn(expected, output)

            for command in (
                (
                    "views",
                    "rebuild",
                    "--month",
                    "2026-08",
                    "--config",
                    str(config),
                    "--json",
                ),
                ("status", "--month", "2026-08", "--config", str(config), "--json"),
                ("pending", "--month", "2026-08", "--config", str(config), "--json"),
                ("report", "--month", "2026-08", "--config", str(config), "--json"),
                (
                    "valuation",
                    "missing",
                    "--month",
                    "2026-08",
                    "--config",
                    str(config),
                    "--json",
                ),
            ):
                code, output, errors = self._run_main(*command)
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output)["status"], "success")

            with patch("honeymoney.cli.webbrowser.open") as opened:
                code, output, errors = self._run_main(
                    "report", "--month", "2026-08", "--config", str(config)
                )
            self.assertEqual(code, 0, errors)
            self.assertIn("Report:", output)
            opened.assert_called_once()

            transaction_id = self._transaction_id(root)
            code, output, errors = self._run_main(
                "review",
                "--transaction",
                transaction_id,
                "--as",
                "expense",
                "--config",
                str(config),
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Saved 1 review decision", output)

            corrections = root / "batch.csv"
            corrections.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [{"transaction_id": transaction_id, "notes": "Synthetic CLI note"}],
                ),
                encoding="utf-8",
            )
            code, output, errors = self._run_main(
                "correct", "--file", str(corrections), "--config", str(config)
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Saved 1 correction", output)

            profile = root / "profiles" / "starter_csv.json"
            code, output, errors = self._run_main(
                "profile",
                "validate",
                str(profile),
                "--config",
                str(config),
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Profile starter_csv is valid", output)

            rates = root / "rates-download.json"
            rates.write_text(
                json.dumps(
                    {
                        "header": {
                            "success": True,
                            "err_code": "0000",
                            "err_msg": "No error found",
                        },
                        "result": {
                            "datasize": 1,
                            "records": [{"end_of_day": "2026-08-08", "usd": 8}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            code, output, errors = self._run_main(
                "rates", "import", str(rates), "--config", str(config)
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Imported 1 observations", output)
            code, output, errors = self._run_main(
                "rates", "import", str(rates), "--config", str(config), "--json"
            )
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["command"], "rates.import")

            code, output, errors = self._run_main("doctor", "--config", str(config))
            self.assertEqual(code, 0, errors)
            self.assertIn("Workspace is healthy.", output)
            code, output, errors = self._run_main(
                "doctor", "--fix", "--config", str(config), "--json"
            )
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["command"], "doctor")

    def test_cli_errors_help_and_rate_fetch_are_bounded(self) -> None:
        code, output, errors = self._run_main()
        self.assertEqual(code, 0, errors)
        self.assertIn("Honeymoney 0.2.0", output)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            interactive_root = Path(temporary) / "interactive"
            with patch("builtins.input", return_value=str(interactive_root)):
                code, output, errors = self._run_main("setup")
            self.assertEqual(code, 0, errors)
            self.assertIn("Created Honeymoney workspace", output)

            source = self._source(
                root,
                "synthetic.csv",
                "2026-08-08,Synthetic error path,-12.00,HKD\n",
            )
            import_workspace(source, config_path=config, interactive=False)
            transaction_id = self._transaction_id(root)
            corrections = root / "batch.csv"
            corrections.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [{"transaction_id": transaction_id, "notes": "Synthetic"}],
                ),
                encoding="utf-8",
            )

            code, output, errors = self._run("unknown")
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertIn("Unknown command", errors)
            code, output, errors = self._run("run", "--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["errors"][0]["code"], "usage_error")
            self.assertEqual(errors, "")
            code, output, errors = self._run("setup", "--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["command"], "setup")
            code, output, errors = self._run("views", "wrong", "--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["command"], "views.wrong")
            code, output, errors = self._run("imports", "list", "--bad", "--json")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["errors"][0]["code"], "usage_error")
            code, output, errors = self._run(
                "import", " ", "--config", str(config), "--json"
            )
            self.assertEqual(code, 2)
            self.assertIn(
                "must not be empty", json.loads(output)["errors"][0]["message"]
            )
            code, output, errors = self._run(
                "rates",
                "fetch",
                "USD",
                "--start",
                "2026-08-08",
                "--end",
                "2026-08-08",
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(code, 2)
            self.assertIn("requires --allow-network", output)

            for command, expected in (
                (("valuation", "other", "--json"), "valuation.other"),
                (("review", "--transaction", transaction_id, "--json"), "review"),
                (
                    (
                        "review",
                        "--transaction",
                        transaction_id,
                        "--as",
                        "expense",
                        "--file",
                        str(corrections),
                        "--json",
                    ),
                    "review",
                ),
                (
                    (
                        "review",
                        "--file",
                        str(corrections),
                        "--month",
                        "2026-08",
                        "--config",
                        str(config),
                        "--json",
                    ),
                    "review",
                ),
                (("rates", "other", "--json"), "rates.other"),
            ):
                code, output, errors = self._run(*command)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output)["command"], expected)

            code, output, errors = self._run_main(
                "review", "--month", "2026-08", "--config", str(config)
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Pending review:", output)
            code, output, errors = self._run_main(
                "review", "--file", str(corrections), "--config", str(config)
            )
            self.assertEqual(code, 0, errors)
            self.assertIn("Saved 1 review decision", output)

            missing = root / "missing.csv"
            code, output, errors = self._run(
                "correct", "--file", str(missing), "--config", str(config), "--json"
            )
            self.assertEqual(code, 2)
            self.assertIn("unsafe", output)
            empty_corrections = root / "empty.csv"
            empty_corrections.write_text(
                csv_document(CORRECTION_COLUMNS, []), encoding="utf-8"
            )
            code, output, errors = self._run(
                "correct",
                "--file",
                str(empty_corrections),
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(code, 2)
            self.assertIn("has no choices", output)
            correction_link = root / "correction-link.csv"
            os.symlink(corrections, correction_link)
            code, output, errors = self._run(
                "correct",
                "--file",
                str(correction_link),
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(code, 2)
            self.assertIn("unsafe", output)

            missing_profile = root / "missing-profile.json"
            code, output, errors = self._run(
                "profile",
                "validate",
                str(missing_profile),
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(code, 2)
            self.assertIn("unsafe", output)
            invalid_profile = root / "invalid-profile.json"
            invalid_profile.write_text("{", encoding="utf-8")
            code, output, errors = self._run(
                "profile",
                "validate",
                str(invalid_profile),
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(code, 2)
            self.assertIn("not valid JSON", output)

            code, output, errors = self._run(
                "rates", "import", str(missing), "--config", str(config), "--json"
            )
            self.assertEqual(code, 2)
            self.assertIn("Rate document does not exist", output)
            rate_document = root / "rate-document.json"
            rate_document.write_text("{}", encoding="utf-8")
            rate_link = root / "rate-link.json"
            os.symlink(rate_document, rate_link)
            code, output, errors = self._run(
                "rates", "import", str(rate_link), "--config", str(config), "--json"
            )
            self.assertEqual(code, 2)
            self.assertIn("unsafe", output)

            missing_root = Path(temporary) / "missing-root" / "config.json"
            code, output, errors = self._run_main(
                "doctor", "--config", str(missing_root)
            )
            self.assertEqual(code, 2, errors)
            self.assertIn("workspace_input_invalid", output)

            with (
                patch.object(sys, "stdin") as stdin,
                patch("builtins.input", return_value="no"),
            ):
                stdin.isatty.return_value = True
                code, output, errors = self._run_main(
                    "rates",
                    "fetch",
                    "USD",
                    "--start",
                    "2026-08-08",
                    "--end",
                    "2026-08-08",
                    "--config",
                    str(config),
                )
            self.assertEqual(code, 0, errors)
            self.assertIn("Rate fetch cancelled.", output)

            fetched = SimpleNamespace(observations=[], request_urls=("synthetic",))
            with patch("honeymoney.cli.fetch_hkma_daily_rates", return_value=fetched):
                code, output, errors = self._run_main(
                    "rates",
                    "fetch",
                    "USD",
                    "--start",
                    "2026-08-08",
                    "--end",
                    "2026-08-08",
                    "--allow-network",
                    "--config",
                    str(config),
                )
            self.assertEqual(code, 0, errors)
            self.assertIn("Fetched 0 observations", output)
            with patch("honeymoney.cli.fetch_hkma_daily_rates", return_value=fetched):
                code, output, errors = self._run_main(
                    "rates",
                    "fetch",
                    "USD",
                    "--start",
                    "2026-08-08",
                    "--end",
                    "2026-08-08",
                    "--allow-network",
                    "--config",
                    str(config),
                    "--json",
                )
            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["command"], "rates.fetch")


if __name__ == "__main__":
    unittest.main()
