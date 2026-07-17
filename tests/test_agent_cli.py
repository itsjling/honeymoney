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
        filesystem_fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        python_paths = []
        if filesystem_fault is not None:
            python_paths.append(REPO_ROOT / "tests" / "fault_injection")
            env["HONEYMONEY_TEST_FS_FAULT"] = filesystem_fault
        python_paths.append(REPO_ROOT)
        env["PYTHONPATH"] = os.pathsep.join(map(str, python_paths))
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

    def test_interactive_import_explains_when_ollama_is_disabled(self) -> None:
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
                ],
                cwd=root,
                input_text="q\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Ollama fallback is disabled; set ollama.enabled to true in "
                "config.json to enable it.",
                result.stdout,
            )
            self.assertIn("1 imported records have no category.", result.stdout)

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

    def test_correct_failure_restores_all_existing_artifacts(self) -> None:
        faults = [
            "file-fsync",
            "replace-before:review_needed.csv",
            "replace-before:corrections.csv",
            "replace-before:categorized.csv",
            "directory-fsync-after:categorized.csv",
        ]
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statement = root / "may.csv"
                self._write_statement(statement)
                imported = self._run_cli(
                    ["import", str(statement), "--json"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                categorized = root / "output" / "categorized.csv"
                review = root / "output" / "review_needed.csv"
                corrections = root / "corrections.csv"
                with categorized.open(newline="", encoding="utf-8") as fh:
                    [row] = list(csv.DictReader(fh))
                before = {
                    path: path.read_bytes()
                    for path in (categorized, review, corrections)
                }
                correction = json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                            "needs_review": False,
                        }
                    ]
                )

                result = self._run_cli(
                    ["correct", "--file", "-", "--json"],
                    cwd=root,
                    input_text=correction,
                    filesystem_fault=fault,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(
                    {path: path.read_bytes() for path in before}, before
                )

    def test_correct_retained_generation_is_completed_by_the_next_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            imported = self._run_cli(["import", str(statement), "--json"], cwd=root)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            categorized = root / "output" / "categorized.csv"
            with categorized.open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            correction = json.dumps(
                [
                    {
                        "transaction_id": row["transaction_id"],
                        "category": "Groceries",
                        "needs_review": False,
                    }
                ]
            )

            interrupted = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=correction,
                filesystem_fault="replace-after:categorized.csv",
            )
            self.assertEqual(interrupted.returncode, 75, interrupted.stderr)

            recovered = self._run_cli(["status", "--json"], cwd=root)

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            with categorized.open(newline="", encoding="utf-8") as fh:
                [corrected] = list(csv.DictReader(fh))
            self.assertEqual(corrected["category"], "Groceries")
            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [saved] = list(csv.DictReader(fh))
            self.assertEqual(saved["category"], "Groceries")

    def test_successful_correction_preserves_existing_artifact_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement)
            imported = self._run_cli(["import", str(statement), "--json"], cwd=root)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            categorized = root / "output" / "categorized.csv"
            review = root / "output" / "review_needed.csv"
            corrections = root / "corrections.csv"
            modes = {categorized: 0o640, review: 0o600, corrections: 0o644}
            for path, mode in modes.items():
                path.chmod(mode)
            with categorized.open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                            "needs_review": False,
                        }
                    ]
                ),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {path: path.stat().st_mode & 0o777 for path in modes}, modes
            )

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

    def test_correct_rejects_empty_non_note_fields_from_file_and_stdin(self) -> None:
        fields = [
            "category",
            "flow_type",
            "owner",
            "payment_method",
            "confidence",
            "reason",
            "needs_review",
        ]
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
            review_path = root / "output" / "review_needed.csv"
            corrections_path = root / "corrections.csv"
            with categorized_path.open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            before = {
                path: path.read_bytes()
                for path in (categorized_path, review_path, corrections_path)
            }
            for index, field in enumerate(fields):
                with self.subTest(field=field):
                    batch = json.dumps(
                        [{"transaction_id": row["transaction_id"], field: " \t "}]
                    )
                    if index % 2:
                        batch_path = root / "invalid-correction.json"
                        batch_path.write_text(batch, encoding="utf-8")
                        result = self._run_cli(
                            ["correct", "--file", str(batch_path), "--json"],
                            cwd=root,
                        )
                    else:
                        result = self._run_cli(
                            ["correct", "--file", "-", "--json"],
                            cwd=root,
                            input_text=batch,
                        )

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(
                        f"Correction field {field}",
                        self._json(result)["errors"][0]["message"],
                    )
                    for path, content in before.items():
                        self.assertEqual(path.read_bytes(), content)

    def test_empty_notes_clear_persists_across_correction_reload_and_import(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            rules_path = root / "rules.json"
            rules = json.loads(rules_path.read_text(encoding="utf-8"))
            rules["rules"].append(
                {
                    "id": "synthetic-note",
                    "enabled": True,
                    "priority": 100,
                    "fields": ["original_description"],
                    "match_type": "exact",
                    "patterns": ["PARKNSHOP"],
                    "category": "Groceries",
                    "confidence": 1,
                    "notes": "Rule-generated note",
                }
            )
            rules_path.write_text(json.dumps(rules), encoding="utf-8")
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
            self.assertEqual(row["notes"], "Rule-generated note")

            omitted = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [{"transaction_id": row["transaction_id"], "owner": "Household"}]
                ),
            )
            self.assertEqual(omitted.returncode, 0, omitted.stderr)
            omitted_rerun = self._run_cli(
                ["import", str(statement), "--replace", "--json"], cwd=root
            )
            self.assertEqual(omitted_rerun.returncode, 0, omitted_rerun.stderr)
            with categorized_path.open(newline="", encoding="utf-8") as fh:
                [unchanged] = list(csv.DictReader(fh))
            self.assertEqual(unchanged["notes"], "Rule-generated note")

            clear = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [{"transaction_id": row["transaction_id"], "notes": ""}]
                ),
            )

            self.assertEqual(clear.returncode, 0, clear.stderr)
            with categorized_path.open(newline="", encoding="utf-8") as fh:
                [cleared] = list(csv.DictReader(fh))
            self.assertEqual(cleared["notes"], "")
            rerun = self._run_cli(
                ["import", str(statement), "--replace", "--json"], cwd=root
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            with categorized_path.open(newline="", encoding="utf-8") as fh:
                [reimported] = list(csv.DictReader(fh))
            self.assertEqual(reimported["notes"], "")

    def test_correction_cannot_resolve_unknown_category_without_a_flow_decision(
        self,
    ) -> None:
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

            invalid = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "needs_review": False,
                        }
                    ]
                ),
            )

            self.assertEqual(invalid.returncode, 2)
            self.assertIn(
                "Unknown category cannot be marked resolved",
                self._json(invalid)["errors"][0]["message"],
            )
            self.assertEqual(categorized_path.read_bytes(), before)
            explicit_flow = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "flow_type": "expense",
                            "needs_review": False,
                        }
                    ]
                ),
            )
            self.assertEqual(explicit_flow.returncode, 0, explicit_flow.stderr)

    def test_correction_cannot_trust_unproven_existing_flow_to_resolve_unknown(
        self,
    ) -> None:
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
            row["flow_type"] = "expense"
            row["flow_source"] = "deterministic"
            with categorized_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            before = categorized_path.read_bytes()

            result = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "needs_review": False,
                        }
                    ]
                ),
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "explicit accounting flow decision",
                self._json(result)["errors"][0]["message"],
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
        commands = [
            "setup",
            "run",
            "import",
            "status",
            "report",
            "pending",
            "correct",
            "config",
        ]

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

    def test_import_rejects_incomplete_profiles_before_artifacts_change(self) -> None:
        cases = [
            ("identity", {"account": "Synthetic"}, "account_id"),
            (
                "parser mode",
                {
                    "id": "synthetic",
                    "account_id": "synthetic",
                    "account": "Synthetic",
                    "account_type": "bank",
                    "institution": "Local",
                    "country": "HK",
                    "account_currency": "HKD",
                    "owner": "Household",
                    "payment_method": "Bank Account",
                },
                "exactly one of csv or pdf",
            ),
            (
                "date source",
                {"columns": {"description": "Description", "amount": "Amount"}},
                "columns.transaction_date",
            ),
            (
                "amount source",
                {
                    "columns": {
                        "transaction_date": "Date",
                        "description": "Description",
                    }
                },
                "amount strategy",
            ),
            (
                "conflicting amount sources",
                {
                    "columns": {
                        "transaction_date": "Date",
                        "description": "Description",
                        "amount": "Amount",
                        "debit": "Debit",
                        "credit": "Credit",
                    }
                },
                "exactly one amount strategy",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "synthetic.csv"
            self._write_statement(statement)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            base_profile = json.loads(
                (root / "profiles" / "starter_csv.json").read_text(encoding="utf-8")
            )
            protected = [
                root / "corrections.csv",
                root / "rules.json",
                root / "profile_mappings.json",
            ]
            before = {path: path.read_bytes() for path in protected}
            for label, replacement, message in cases:
                with self.subTest(label=label):
                    profile = dict(base_profile)
                    if label == "identity":
                        profile = replacement
                    elif label == "parser mode":
                        profile = replacement
                    else:
                        profile["csv"] = replacement
                    profile_path = root / "profiles" / "invalid.json"
                    profile_path.write_text(json.dumps(profile), encoding="utf-8")
                    config["profiles"] = [str(profile_path)]
                    config_path.write_text(json.dumps(config), encoding="utf-8")

                    result = self._run_cli(
                        [
                            "import",
                            str(statement),
                            "--config",
                            str(config_path),
                            "--json",
                        ],
                        cwd=root,
                    )

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(message, self._json(result)["errors"][0]["message"])
                    for path, content in before.items():
                        self.assertEqual(path.read_bytes(), content)
                    self.assertFalse((root / "output" / "categorized.csv").exists())

    def test_import_rejects_missing_selected_csv_headers_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "synthetic.csv"
            self._write_statement(statement)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["csv"]["columns"]["amount"] = "Missing Amount"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["profiles"] = [str(profile_path)]
            config_path.write_text(json.dumps(config), encoding="utf-8")

            result = self._run_cli(
                ["import", str(statement), "--config", str(config_path), "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            message = self._json(result)["errors"][0]["message"]
            self.assertIn("starter_csv", message)
            self.assertIn("csv.columns.amount", message)
            self.assertIn("Missing Amount", message)
            self.assertFalse((root / "output" / "categorized.csv").exists())

    def test_invalid_prompted_profile_does_not_save_a_filename_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "synthetic.csv"
            self._write_statement(statement)
            template_path = root / "profiles" / "starter_csv.json"
            template = json.loads(template_path.read_text(encoding="utf-8"))
            profile_paths = []
            for index in (1, 2):
                profile = json.loads(json.dumps(template))
                profile["id"] = f"candidate_{index}"
                profile["account_id"] = f"candidate_{index}"
                if index == 1:
                    profile["csv"]["columns"]["amount"] = "Missing Amount"
                path = root / "profiles" / f"candidate_{index}.json"
                path.write_text(json.dumps(profile), encoding="utf-8")
                profile_paths.append(str(path))
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["profiles"] = profile_paths
            config_path.write_text(json.dumps(config), encoding="utf-8")
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()

            result = self._run_cli(
                ["import", str(statement), "--config", str(config_path)],
                cwd=root,
                input_text="1\n",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("csv.columns.amount", result.stderr)
            self.assertEqual(mappings_path.read_bytes(), before)

    def test_import_rejects_malformed_pdf_profile_and_mapping_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            profile_path = root / "profiles" / "mox_bank_pdf.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["pdf"]["row_regex"] = "("
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            config["profiles"] = [str(profile_path)]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            statement = root / "synthetic.pdf"
            statement.write_bytes(b"%PDF-1.4 synthetic")

            malformed_profile = self._run_cli(
                ["import", str(statement), "--config", str(config_path), "--json"],
                cwd=root,
            )

            self.assertEqual(malformed_profile.returncode, 2, malformed_profile.stderr)
            self.assertIn(
                "pdf.row_regex must be a valid regular expression",
                self._json(malformed_profile)["errors"][0]["message"],
            )
            self.assertFalse((root / "output" / "categorized.csv").exists())

            profile["pdf"]["row_regex"] = (
                "^(?P<transaction_date>\\S+) (?P<posting_date>\\S+) "
                "(?P<description>.+?) "
                "(?P<amount>-?\\d+\\.\\d{2})$"
            )
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            mappings_path = root / "profile_mappings.json"
            mappings_path.write_text("[]", encoding="utf-8")
            malformed_mapping = self._run_cli(
                ["import", str(statement), "--config", str(config_path), "--json"],
                cwd=root,
            )

            self.assertEqual(malformed_mapping.returncode, 2, malformed_mapping.stderr)
            self.assertIn(
                "Profile mappings document must be a JSON object",
                self._json(malformed_mapping)["errors"][0]["message"],
            )
            self.assertFalse((root / "output" / "categorized.csv").exists())

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
