import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from honeymoney.cli import _report_command

REPO_ROOT = Path(__file__).resolve().parents[1]


class AgentCliTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd or REPO_ROOT,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _json(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertTrue(result.stdout.strip(), result.stderr)
        self.assertEqual(result.stdout.count("\n"), 1)
        payload = json.loads(result.stdout)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "command",
                "status",
                "data",
                "artifacts",
                "warnings",
                "errors",
            },
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertIsInstance(payload["data"], dict)
        self.assertIsInstance(payload["artifacts"], dict)
        self.assertIsInstance(payload["warnings"], list)
        self.assertIsInstance(payload["errors"], list)
        return payload

    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "money"
        result = self._run_cli(["setup", "--root", str(root), "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _write_statement(self, path: Path) -> None:
        path.write_text(
            "Date,Description,Amount,Currency\n2026-05-04,PARKNSHOP,-120.50,HKD\n",
            encoding="utf-8",
        )

    def test_setup_json_returns_one_machine_readable_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"

            result = self._run_cli(["setup", "--root", str(root), "--json"])

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = self._json(result)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["command"], "setup")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["root"], str(root.resolve()))
            self.assertEqual(
                payload["artifacts"]["config_json"], str(root.resolve() / "config.json")
            )
            self.assertEqual(payload["warnings"], [])
            self.assertEqual(payload["errors"], [])
            self.assertEqual(result.stdout.count("\n"), 1)

    def test_import_json_is_non_interactive_and_returns_import_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)

            result = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--config",
                    str(root / "config.json"),
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = self._json(result)
            self.assertEqual(payload["command"], "import")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["transaction_count"], 1)
            self.assertFalse(payload["data"]["interactive"])
            self.assertEqual(payload["data"]["uncategorized_count"], 1)
            self.assertEqual(
                payload["artifacts"]["import_report_json"],
                str(root.resolve() / "output" / "import_report.json"),
            )
            self.assertNotIn("Pick a category", result.stdout)
            self.assertEqual(result.stdout.count("\n"), 1)

    def test_run_status_and_report_have_structured_json_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._write_statement(root / "input" / "may.csv")
            config_path = root / "config.json"

            run_result = self._run_cli(
                ["run", "--config", str(config_path), "--json"], cwd=root
            )
            status_result = self._run_cli(
                [
                    "status",
                    "2026-05",
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=root,
            )
            report_path = root / "output" / "agent-report.html"
            report_result = self._run_cli(
                [
                    "report",
                    "2026-05",
                    "--config",
                    str(config_path),
                    "--output",
                    str(report_path),
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(run_result.returncode, 0, run_result.stderr)
            self.assertEqual(self._json(run_result)["command"], "run")

            self.assertEqual(status_result.returncode, 0, status_result.stderr)
            status = self._json(status_result)
            self.assertEqual(status["command"], "status")
            self.assertEqual(status["data"]["records_processed"], 1)
            self.assertEqual(status["data"]["uncategorized"], 1)
            self.assertEqual(
                status["data"]["period"],
                {"start": "2026-05-01", "end": "2026-05-31"},
            )

            self.assertEqual(report_result.returncode, 0, report_result.stderr)
            report = self._json(report_result)
            self.assertEqual(report["command"], "report")
            self.assertEqual(report["data"]["transaction_count"], 1)
            self.assertEqual(
                report["artifacts"]["report_html"], str(report_path.resolve())
            )
            self.assertTrue(report_path.exists())

    def test_json_mode_returns_structured_validation_errors(self) -> None:
        result = self._run_cli(["import", "--json"])

        self.assertEqual(result.returncode, 2)
        payload = self._json(result)
        self.assertEqual(payload["command"], "import")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["data"], {})
        self.assertIn("requires a path", payload["errors"][0]["message"])
        self.assertEqual(result.stdout.count("\n"), 1)

    def test_pending_json_lists_review_rows_for_the_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            import_result = self._run_cli(
                ["import", str(statement), "--json"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            result = self._run_cli(["pending", "2026-05", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = self._json(result)
            self.assertEqual(payload["command"], "pending")
            self.assertEqual(payload["data"]["count"], 1)
            self.assertEqual(
                payload["data"]["transactions"][0]["merchant"], "PARKNSHOP"
            )
            self.assertEqual(payload["data"]["transactions"][0]["category"], "")
            self.assertEqual(
                payload["data"]["period"],
                {"start": "2026-05-01", "end": "2026-05-31"},
            )

    def test_correct_json_validates_and_applies_a_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            import_result = self._run_cli(
                ["import", str(statement), "--json"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            correction_path = root / "agent-corrections.json"
            correction_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                            "owner": "Household",
                            "confidence": 1,
                            "reason": "Reviewed by the local agent",
                            "needs_review": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )

            result = self._run_cli(
                ["correct", "--file", str(correction_path), "--json"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = self._json(result)
            self.assertEqual(payload["command"], "correct")
            self.assertEqual(payload["data"]["applied_count"], 1)
            self.assertEqual(payload["data"]["remaining_review_count"], 0)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [corrected] = list(csv.DictReader(fh))
            self.assertEqual(corrected["category"], "Groceries")
            self.assertEqual(corrected["owner"], "Household")
            self.assertEqual(corrected["confidence"], "1")
            self.assertEqual(corrected["needs_review"], "false")
            self.assertIn("manual_correction", corrected["flags"])
            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                [saved] = list(csv.DictReader(fh))
            self.assertEqual(saved["transaction_id"], row["transaction_id"])
            self.assertEqual(saved["category"], "Groceries")
            self.assertEqual(saved["needs_review"], "false")

    def test_correct_rejects_the_entire_batch_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            import_result = self._run_cli(
                ["import", str(statement), "--json"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            categorized_path = root / "output" / "categorized.csv"
            review_path = root / "output" / "review_needed.csv"
            corrections_path = root / "corrections.csv"
            with categorized_path.open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            before = {
                path: path.read_bytes()
                for path in [categorized_path, review_path, corrections_path]
            }
            batch_path = root / "invalid-corrections.json"
            batch_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                        },
                        {"transaction_id": "missing-id", "category": "Dining"},
                    ]
                ),
                encoding="utf-8",
            )

            result = self._run_cli(
                ["correct", "--file", str(batch_path), "--json"], cwd=root
            )

            self.assertEqual(result.returncode, 2)
            payload = self._json(result)
            self.assertEqual(payload["command"], "correct")
            self.assertIn("Unknown transaction_id", payload["errors"][0]["message"])
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_correct_rejects_duplicate_transaction_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            self.assertEqual(
                self._run_cli(
                    ["import", str(statement), "--json"], cwd=root
                ).returncode,
                0,
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            duplicate_batch = json.dumps(
                [
                    {"transaction_id": row["transaction_id"], "category": "Dining"},
                    {"transaction_id": row["transaction_id"], "category": "Groceries"},
                ]
            )

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=duplicate_batch,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Duplicate transaction_id", self._json(result)["errors"][0]["message"]
            )

    def test_correct_accepts_a_json_batch_from_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            self.assertEqual(
                self._run_cli(
                    ["import", str(statement), "--json"], cwd=root
                ).returncode,
                0,
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [{"transaction_id": row["transaction_id"], "category": "Dining"}]
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._json(result)["data"]["applied_count"], 1)

    def test_correct_rejects_invalid_domain_values_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            self.assertEqual(
                self._run_cli(
                    ["import", str(statement), "--json"], cwd=root
                ).returncode,
                0,
            )
            categorized_path = root / "output" / "categorized.csv"
            with categorized_path.open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            before = categorized_path.read_bytes()

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Not a real category",
                        }
                    ]
                ),
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Unsupported category", self._json(result)["errors"][0]["message"]
            )
            self.assertEqual(categorized_path.read_bytes(), before)

    def test_correct_json_requires_an_input_file_as_structured_error(self) -> None:
        result = self._run_cli(["correct", "--json"])

        self.assertEqual(result.returncode, 2)
        payload = self._json(result)
        self.assertEqual(payload["command"], "correct")
        self.assertIn("requires --file", payload["errors"][0]["message"])

    def test_setup_json_requires_root_without_prompting(self) -> None:
        result = self._run_cli(["setup", "--json"])

        self.assertEqual(result.returncode, 2)
        payload = self._json(result)
        self.assertEqual(payload["command"], "setup")
        self.assertIn("requires --root", payload["errors"][0]["message"])

    def test_strict_json_partial_success_uses_exit_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["pdf"]["enabled"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            statement = root / "statement.pdf"
            statement.write_bytes(b"synthetic placeholder")

            result = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--config",
                    str(config_path),
                    "--strict",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = self._json(result)
            self.assertEqual(payload["status"], "partial_success")
            self.assertTrue(payload["warnings"])

    def test_json_mode_reports_missing_input_and_correction_paths(self) -> None:
        missing_import = self._run_cli(
            ["import", "/definitely/missing/statement.csv", "--json"]
        )
        self.assertEqual(missing_import.returncode, 2)
        self.assertIn(
            "does not exist", self._json(missing_import)["errors"][0]["message"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            missing_corrections = self._run_cli(
                [
                    "correct",
                    "--file",
                    "/definitely/missing/corrections.json",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(missing_corrections.returncode, 2)
            self.assertIn(
                "No such file",
                self._json(missing_corrections)["errors"][0]["message"],
            )

    def test_malformed_arguments_still_return_json_for_every_machine_command(
        self,
    ) -> None:
        commands = ["setup", "run", "import", "status", "report", "pending", "correct"]

        for command in commands:
            with self.subTest(command=command):
                result = self._run_cli([command, "--bogus", "--json"])

                self.assertEqual(result.returncode, 2)
                payload = self._json(result)
                self.assertEqual(payload["command"], command)
                self.assertEqual(payload["status"], "error")
                self.assertIn("unrecognized arguments", payload["errors"][0]["message"])
                self.assertEqual(result.stdout.count("\n"), 1)

    def test_correct_merges_fields_and_preserves_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            self.assertEqual(
                self._run_cli(
                    ["import", str(statement), "--json"], cwd=root
                ).returncode,
                0,
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            transaction_id = row["transaction_id"]

            initial = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": transaction_id,
                            "category": "Groceries",
                            "owner": "Household",
                            "needs_review": False,
                        }
                    ]
                ),
            )
            self.assertEqual(initial.returncode, 0, initial.stderr)

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [{"transaction_id": transaction_id, "notes": "Keep the receipt"}]
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                [saved] = list(csv.DictReader(fh))
            self.assertEqual(saved["category"], "Groceries")
            self.assertEqual(saved["owner"], "Household")
            self.assertEqual(saved["notes"], "Keep the receipt")
            self.assertEqual(saved["needs_review"], "false")
            rerun = self._run_cli(
                ["import", str(statement), "--replace", "--json"], cwd=root
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [reimported] = list(csv.DictReader(fh))
            self.assertEqual(reimported["category"], "Groceries")
            self.assertEqual(reimported["owner"], "Household")
            self.assertEqual(reimported["notes"], "Keep the receipt")
            self.assertEqual(reimported["needs_review"], "false")

    def test_correction_without_review_field_preserves_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            self.assertEqual(
                self._run_cli(
                    ["import", str(statement), "--json"], cwd=root
                ).returncode,
                0,
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "notes": "Inspect later",
                        }
                    ]
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [corrected] = list(csv.DictReader(fh))
            self.assertEqual(corrected["needs_review"], "true")

    def test_malformed_config_and_ambiguous_profile_return_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed_config = root / "malformed.json"
            malformed_config.write_text('{"paths": []}', encoding="utf-8")
            malformed = self._run_cli(
                ["status", "--config", str(malformed_config), "--json"]
            )

            self.assertEqual(malformed.returncode, 2)
            self.assertIn(
                "paths must be a JSON object",
                self._json(malformed)["errors"][0]["message"],
            )
            malformed_config.write_text('{"paths": {"output": []}}', encoding="utf-8")
            malformed_nested = self._run_cli(
                ["status", "--config", str(malformed_config), "--json"]
            )
            self.assertEqual(malformed_nested.returncode, 2)
            self.assertIn(
                "paths.output must be a non-empty string",
                self._json(malformed_nested)["errors"][0]["message"],
            )

            workspace = self._setup_workspace(tmp)
            ambiguous = workspace / "ambiguous.csv"
            ambiguous.write_text("Something,Else\nA,B\n", encoding="utf-8")
            ambiguous_result = self._run_cli(
                ["import", str(ambiguous), "--json"], cwd=workspace
            )

            self.assertEqual(ambiguous_result.returncode, 2)
            self.assertIn(
                "Could not detect profile",
                self._json(ambiguous_result)["errors"][0]["message"],
            )

    def test_malformed_public_config_sections_fail_with_structured_errors(
        self,
    ) -> None:
        cases = [
            ("paths not an object", {"paths": []}, "paths must be a JSON object"),
            (
                "paths.output not a string",
                {"paths": {"output": []}},
                "paths.output must be a non-empty string",
            ),
            (
                "profiles not an array",
                {"profiles": "profiles/starter.json"},
                "profiles must be a JSON array",
            ),
            (
                "profiles item not a string",
                {"profiles": [123]},
                "profiles[0] must be a non-empty string",
            ),
            (
                "profiles item empty",
                {"profiles": [""]},
                "profiles[0] must be a non-empty string",
            ),
            (
                "profile_mappings not a string",
                {"profile_mappings": {"filename_patterns": []}},
                "profile_mappings must be a string",
            ),
            (
                "rules not a string",
                {"rules": 12},
                "rules must be a string",
            ),
            (
                "corrections not a string",
                {"corrections": ["corrections.csv"]},
                "corrections must be a string",
            ),
            (
                "pdf not an object",
                {"pdf": "enabled"},
                "pdf must be a JSON object",
            ),
            (
                "pdf.enabled not a boolean",
                {"pdf": {"enabled": "true"}},
                "pdf.enabled must be a boolean",
            ),
            (
                "pdf.parser not a string",
                {"pdf": {"parser": 1}},
                "pdf.parser must be a non-empty string",
            ),
            (
                "ollama not an object",
                {"ollama": "enabled"},
                "ollama must be a JSON object",
            ),
            (
                "ollama.enabled not a boolean",
                {"ollama": {"enabled": "yes"}},
                "ollama.enabled must be a boolean",
            ),
            (
                "ollama.url not a string",
                {"ollama": {"url": 11434}},
                "ollama.url must be a non-empty string",
            ),
            (
                "ollama.model not a string",
                {"ollama": {"model": None}},
                "ollama.model must be a non-empty string",
            ),
            (
                "ollama.batch_size is a boolean, not an integer",
                {"ollama": {"batch_size": True}},
                "ollama.batch_size must be an integer",
            ),
            (
                "ollama.batch_size not an integer",
                {"ollama": {"batch_size": 2.5}},
                "ollama.batch_size must be an integer",
            ),
            (
                "ollama.batch_size below minimum",
                {"ollama": {"batch_size": 0}},
                "ollama.batch_size must be at least 1",
            ),
            (
                "ollama.timeout_seconds is a boolean, not a number",
                {"ollama": {"timeout_seconds": False}},
                "ollama.timeout_seconds must be a number",
            ),
            (
                "ollama.timeout_seconds not positive",
                {"ollama": {"timeout_seconds": 0}},
                "ollama.timeout_seconds must be greater than 0",
            ),
            (
                "ollama.timeout_seconds non-finite",
                {"ollama": {"timeout_seconds": float("inf")}},
                "ollama.timeout_seconds must be a finite number",
            ),
            (
                "exchange_rates not an object",
                {"exchange_rates": ["HKD", 1.0]},
                "exchange_rates must be a JSON object",
            ),
            (
                "exchange_rates value not a number",
                {"exchange_rates": {"USD": "7.8"}},
                "exchange_rates.USD must be a number",
            ),
            (
                "exchange_rates value is a boolean, not a number",
                {"exchange_rates": {"USD": True}},
                "exchange_rates.USD must be a number",
            ),
            (
                "exchange_rates value not positive",
                {"exchange_rates": {"USD": 0}},
                "exchange_rates.USD must be greater than 0",
            ),
            (
                "exchange_rates value non-finite",
                {"exchange_rates": {"USD": float("nan")}},
                "exchange_rates.USD must be a finite number",
            ),
            (
                "base_currency not a string",
                {"base_currency": 840},
                "base_currency must be a non-empty string",
            ),
            (
                "review_confidence_threshold is a boolean, not a number",
                {"review_confidence_threshold": True},
                "review_confidence_threshold must be a number",
            ),
            (
                "review_confidence_threshold out of range",
                {"review_confidence_threshold": 1.5},
                "review_confidence_threshold must be at most 1",
            ),
            (
                "review_confidence_threshold negative",
                {"review_confidence_threshold": -0.1},
                "review_confidence_threshold must be at least 0",
            ),
            (
                "review_confidence_threshold non-finite",
                {"review_confidence_threshold": float("nan")},
                "review_confidence_threshold must be a finite number",
            ),
            (
                "categories not an array",
                {"categories": "Groceries"},
                "categories must be a JSON array",
            ),
            (
                "categories item not a string",
                {"categories": [1, 2]},
                "categories[0] must be a non-empty string",
            ),
            (
                "owners not an array",
                {"owners": {"Household": True}},
                "owners must be a JSON array",
            ),
            (
                "owners item empty string",
                {"owners": [""]},
                "owners[0] must be a non-empty string",
            ),
            (
                "payment_methods not an array",
                {"payment_methods": "Cash"},
                "payment_methods must be a JSON array",
            ),
            (
                "payment_methods item not a string",
                {"payment_methods": [None]},
                "payment_methods[0] must be a non-empty string",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for description, overrides, expected_message in cases:
                with self.subTest(description=description):
                    config_path = root / "config.json"
                    config_path.write_text(json.dumps(overrides), encoding="utf-8")

                    result = self._run_cli(
                        ["status", "--config", str(config_path), "--json"]
                    )

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, "")
                    payload = self._json(result)
                    self.assertEqual(payload["command"], "status")
                    self.assertEqual(payload["status"], "error")
                    self.assertIn(expected_message, payload["errors"][0]["message"])

    def test_valid_public_config_sections_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
                        "review_confidence_threshold": 0.8,
                        "categories": ["Income", "Groceries"],
                        "owners": ["Household"],
                        "payment_methods": ["Bank Account"],
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                        "ollama": {
                            "enabled": False,
                            "url": "http://localhost:11434/api/generate",
                            "model": "qwen2.5:7b-instruct",
                            "batch_size": 5,
                            "timeout_seconds": 120,
                            "think": False,
                        },
                        "paths": {
                            "input": str(root / "input"),
                            "output": str(root / "output" / "categorized.csv"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_cli(["status", "--config", str(config_path), "--json"])

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = self._json(result)
            self.assertEqual(payload["command"], "status")
            self.assertEqual(payload["status"], "success")

    def test_report_json_never_opens_a_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            report_path = root / "report.html"
            config_path.write_text(
                json.dumps({"paths": {"output": str(root / "categorized.csv")}}),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with patch("honeymoney.cli.webbrowser.open") as browser_open:
                with redirect_stdout(stdout):
                    result = _report_command(
                        [
                            "2026-05",
                            "--config",
                            str(config_path),
                            "--output",
                            str(report_path),
                            "--json",
                        ]
                    )

            self.assertEqual(result, 0)
            browser_open.assert_not_called()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["command"], "report")
            self.assertTrue(report_path.exists())


if __name__ == "__main__":
    unittest.main()
