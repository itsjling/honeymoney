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
from unittest.mock import Mock, patch

from honeymoney.csv_artifacts import csv_document
from honeymoney.identity_state import identity_manifest_path, load_identity_state
from honeymoney.manual_pairs import MANUAL_PAIR_FLAG_PREFIX, manual_pair_id
from honeymoney.overlap import overlap_manifest_path, source_occurrences_path
from honeymoney.schema import SOURCE_OCCURRENCE_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_OLLAMA_HOOK = REPO_ROOT / "tests" / "offline_ollama_hook"


class CashFlowReviewTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        ollama_mode: str | None = None,
        filesystem_fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        python_paths = [str(REPO_ROOT)]
        if filesystem_fault is not None:
            python_paths.insert(0, str(REPO_ROOT / "tests" / "fault_injection"))
            env["HONEYMONEY_TEST_FS_FAULT"] = filesystem_fault
        if ollama_mode is not None:
            python_paths.insert(0, str(OFFLINE_OLLAMA_HOOK))
            env["HONEYMONEY_TEST_OLLAMA_MODE"] = ollama_mode
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "synthetic-money"
        result = self._run_cli(["setup", "--root", str(root), "--json"], cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _import_rows(
        self,
        root: Path,
        filename: str,
        rows: list[str],
        *,
        ollama_mode: str | None = None,
    ) -> None:
        statement = root / filename
        statement.write_text(
            "\n".join(["Date,Description,Amount,Currency", *rows]),
            encoding="utf-8",
        )
        result = self._run_cli(
            ["import", str(statement), "--no-interactive"],
            cwd=root,
            ollama_mode=ollama_mode,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _ledger(self, root: Path) -> list[dict[str, str]]:
        with (root / "output" / "categorized.csv").open(
            newline="", encoding="utf-8"
        ) as fh:
            return list(csv.DictReader(fh))

    def _artifacts(
        self, root: Path, *, include_rules: bool = False
    ) -> dict[str, bytes]:
        paths = [
            root / "output" / "categorized.csv",
            root / "output" / "review_needed.csv",
            root / "corrections.csv",
        ]
        if include_rules:
            paths.append(root / "rules.json")
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in paths
            if path.exists()
        }

    def _pair_artifacts(self, root: Path) -> dict[str, bytes]:
        paths = [
            *sorted((root / "output").iterdir()),
            root / "corrections.csv",
        ]
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in paths
            if path.is_file()
        }

    def test_filtered_review_marks_only_unresolved_may_inflow_as_income(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "flows.csv",
                [
                    "2026-05-04,SYNTHETIC CREDIT,800.00,HKD",
                    "2026-05-05,SYNTHETIC DEBIT,-40.00,HKD",
                    "2026-06-04,LATER CREDIT,900.00,HKD",
                ],
            )

            result = self._run_cli(
                [
                    "review",
                    "--month",
                    "2026-05",
                    "--category",
                    "Unknown",
                    "--flow",
                    "unresolved",
                    "--direction",
                    "inflow",
                ],
                cwd=root,
                input_text="i\nn\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SYNTHETIC CREDIT", result.stdout)
            self.assertNotIn("SYNTHETIC DEBIT", result.stdout)
            self.assertNotIn("LATER CREDIT", result.stdout)
            self.assertIn("Rule preview:", result.stdout)
            self.assertIn(
                "Remember matching future inflows as income? [y/N]", result.stdout
            )
            rows = {row["merchant"]: row for row in self._ledger(root)}
            income = rows["SYNTHETIC CREDIT"]
            self.assertEqual(income["category"], "Income")
            self.assertEqual(income["flow_type"], "income")
            self.assertEqual(income["flow_source"], "correction")
            self.assertEqual(income["confidence"], "1.00")
            self.assertEqual(income["needs_review"], "false")
            self.assertIn("interactively", income["reason"])
            self.assertEqual(rows["SYNTHETIC DEBIT"]["flow_type"], "unresolved")
            self.assertEqual(rows["LATER CREDIT"]["flow_type"], "unresolved")

            report = self._run_cli(["report", "2026-05", "--no-open"], cwd=root)
            self.assertEqual(report.returncode, 0, report.stderr)
            html = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn('id="tile-income">800.00</div>', html)

    def test_one_shot_income_json_merges_one_correction_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root, "credit.csv", ["2026-05-04,CONFIRMED CREDIT,700.00,HKD"]
            )
            [row] = self._ledger(root)

            for _ in range(2):
                result = self._run_cli(
                    [
                        "review",
                        "--transaction",
                        row["transaction_id"],
                        "--as",
                        "income",
                        "--json",
                    ],
                    cwd=root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
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
                self.assertEqual(payload["command"], "review")
                self.assertNotIn("CONFIRMED CREDIT", result.stdout)
                self.assertEqual(payload["data"]["applied_count"], 1)
                self.assertEqual(
                    payload["data"]["transaction_ids"], [row["transaction_id"]]
                )

            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                corrections = list(csv.DictReader(fh))
            self.assertEqual(len(corrections), 1)
            self.assertEqual(corrections[0]["category"], "Income")
            self.assertEqual(corrections[0]["flow_type"], "income")
            self.assertEqual(corrections[0]["needs_review"], "false")

            reimported = self._run_cli(
                [
                    "import",
                    str(root / "credit.csv"),
                    "--replace",
                    "--no-interactive",
                ],
                cwd=root,
            )
            self.assertEqual(reimported.returncode, 0, reimported.stderr)
            [persisted] = self._ledger(root)
            self.assertEqual(persisted["category"], "Income")
            self.assertEqual(persisted["flow_type"], "income")
            self.assertEqual(persisted["flow_source"], "correction")

    def test_batch_review_json_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "batch.csv",
                [
                    "2026-05-04,SYNTHETIC BATCH CREDIT,700.00,HKD",
                    "2026-05-05,SYNTHETIC BATCH DEBIT,-20.00,HKD",
                ],
            )
            rows = {row["merchant"]: row for row in self._ledger(root)}
            decisions = root / "decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": rows["SYNTHETIC BATCH CREDIT"][
                                "transaction_id"
                            ],
                            "decision": "income",
                        },
                        {
                            "transaction_id": rows["SYNTHETIC BATCH DEBIT"][
                                "transaction_id"
                            ],
                            "decision": "expense",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            applied = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )

            self.assertEqual(applied.returncode, 0, applied.stderr)
            payload = json.loads(applied.stdout)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["command"], "review")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["applied_count"], 2)
            self.assertEqual(payload["data"]["unchanged_count"], 0)
            self.assertEqual(payload["data"]["rejected_count"], 0)
            self.assertNotIn("SYNTHETIC BATCH", applied.stdout)
            self.assertNotIn("batch.csv", applied.stdout)
            after_rows = {row["merchant"]: row for row in self._ledger(root)}
            income = after_rows["SYNTHETIC BATCH CREDIT"]
            expense = after_rows["SYNTHETIC BATCH DEBIT"]
            self.assertEqual(
                (income["category"], income["flow_type"]), ("Income", "income")
            )
            self.assertEqual(income["review_reasons"], "")
            self.assertEqual(expense["flow_type"], "expense")
            self.assertNotIn("accounting_flow", expense["review_reasons"].split(";"))
            self.assertIn("category_decision", expense["review_reasons"].split(";"))
            first_artifacts = self._artifacts(root)

            repeated = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_data = json.loads(repeated.stdout)["data"]
            self.assertEqual(repeated_data["applied_count"], 0)
            self.assertEqual(repeated_data["unchanged_count"], 2)
            self.assertEqual(repeated_data["rejected_count"], 0)
            self.assertEqual(self._artifacts(root), first_artifacts)

    def test_batch_review_keeps_pre_migration_ids_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "legacy-batch.csv",
                ["2026-05-04,SYNTHETIC LEGACY DEBIT,-20.00,HKD"],
            )
            categorized_path = root / "output" / "categorized.csv"
            state = load_identity_state(categorized_path)
            [source_row] = state.source_rows
            source_transaction_id = source_row["transaction_id"]
            categorized_path.write_text(
                csv_document(SOURCE_OCCURRENCE_COLUMNS, [source_row]),
                encoding="utf-8",
            )
            source_occurrences_path(categorized_path).unlink()
            overlap_manifest_path(categorized_path).unlink()
            decisions = root / "legacy-decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": source_transaction_id,
                            "decision": "expense",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            first = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )
            second = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(first.stdout)["data"]["applied_count"], 1)
            second_data = json.loads(second.stdout)["data"]
            self.assertEqual(second_data["applied_count"], 0)
            self.assertEqual(second_data["unchanged_count"], 1)
            [canonical_row] = self._ledger(root)
            self.assertNotEqual(
                canonical_row["transaction_id"],
                source_transaction_id,
            )
            with (root / "corrections.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                [correction] = csv.DictReader(handle)
            self.assertEqual(
                correction["transaction_id"],
                canonical_row["transaction_id"],
            )

    def test_batch_review_accepts_csv_and_reports_counts_without_row_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "csv-batch.csv",
                ["2026-05-04,PRIVATE-SHAPED SYNTHETIC DEBIT,-10.00,HKD"],
            )
            [row] = self._ledger(root)
            decisions = root / "decisions.csv"
            decisions.write_text(
                f"transaction_id,decision\n{row['transaction_id']},expense\n",
                encoding="utf-8",
            )

            result = self._run_cli(
                ["review", "--file", str(decisions)],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Batch review complete: 1 applied, 0 unchanged, 0 rejected",
                result.stdout,
            )
            self.assertNotIn(row["transaction_id"], result.stdout)
            self.assertNotIn("PRIVATE-SHAPED", result.stdout)
            [updated] = self._ledger(root)
            self.assertEqual(updated["flow_type"], "expense")

    def test_batch_review_rejects_invalid_and_stale_entries_before_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "rejected.csv",
                [
                    "2026-05-04,SYNTHETIC VALID DEBIT,-10.00,HKD",
                    "2026-05-05,SYNTHETIC OTHER DEBIT,-20.00,HKD",
                ],
            )
            rows = self._ledger(root)
            before = self._artifacts(root)
            decisions = root / "invalid.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": rows[0]["transaction_id"],
                            "decision": "expense",
                        },
                        {
                            "transaction_id": rows[1]["transaction_id"],
                            "decision": "not-a-decision",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            invalid = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )

            self.assertEqual(invalid.returncode, 2, invalid.stderr)
            payload = json.loads(invalid.stdout)
            self.assertEqual(
                payload["data"],
                {
                    "applied_count": 0,
                    "unchanged_count": 0,
                    "rejected_count": 2,
                },
            )
            self.assertEqual(payload["errors"][0]["code"], "unsupported_decision")
            self.assertNotIn("SYNTHETIC", invalid.stdout)
            self.assertNotIn("rejected.csv", invalid.stdout)
            self.assertEqual(self._artifacts(root), before)

            invalid_csv = root / "invalid.csv"
            invalid_csv.write_text(
                "transaction_id,decision\n"
                f"{rows[0]['transaction_id']},expense,extra\n"
                f"{rows[1]['transaction_id']},expense\n",
                encoding="utf-8",
            )
            invalid_csv_result = self._run_cli(
                ["review", "--file", str(invalid_csv), "--json"],
                cwd=root,
            )
            self.assertEqual(
                invalid_csv_result.returncode,
                2,
                invalid_csv_result.stderr,
            )
            invalid_csv_payload = json.loads(invalid_csv_result.stdout)
            self.assertEqual(invalid_csv_payload["data"]["rejected_count"], 2)
            self.assertEqual(
                invalid_csv_payload["errors"][0]["code"],
                "invalid_entry",
            )
            self.assertEqual(self._artifacts(root), before)

            resolved = root / "resolved.json"
            resolved.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": rows[0]["transaction_id"],
                            "decision": "expense",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            first = self._run_cli(
                ["review", "--file", str(resolved), "--json"],
                cwd=root,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = self._artifacts(root)
            stale = root / "stale.json"
            stale.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": rows[0]["transaction_id"],
                            "decision": "refund",
                        },
                        {
                            "transaction_id": "stale-synthetic-id",
                            "decision": "expense",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            stale_result = self._run_cli(
                ["review", "--file", str(stale), "--json"],
                cwd=root,
            )

            self.assertEqual(stale_result.returncode, 2, stale_result.stderr)
            stale_payload = json.loads(stale_result.stdout)
            self.assertEqual(stale_payload["data"]["rejected_count"], 2)
            self.assertEqual(
                {error["code"] for error in stale_payload["errors"]},
                {"stale_review_state", "stale_transaction_id"},
            )
            self.assertNotIn("stale-synthetic-id", stale_result.stdout)
            self.assertEqual(self._artifacts(root), after_first)

    def test_batch_review_does_not_overwrite_a_newer_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "overlap.csv",
                ["2026-05-04,SYNTHETIC OVERLAP DEBIT,-10.00,HKD"],
            )
            [row] = self._ledger(root)
            decisions = root / "overlap-decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "decision": "expense",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            import honeymoney.cli as cli

            original_apply = cli.apply_correction_operation
            competing_patch = {
                row["transaction_id"]: {
                    "category": "Internal Transfer",
                    "flow_type": "internal_transfer",
                    "confidence": "1.00",
                    "reason": "Synthetic competing review",
                    "needs_review": "false",
                    "review_reasons": "",
                }
            }
            competed = False

            def apply_after_competing_review(
                config, categorized_path, patches, **kwargs
            ):
                nonlocal competed
                if not competed:
                    competed = True
                    original_apply(config, categorized_path, competing_patch)
                return original_apply(config, categorized_path, patches, **kwargs)

            prior_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with (
                    patch.object(
                        cli,
                        "apply_correction_operation",
                        apply_after_competing_review,
                    ),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "honeymoney",
                            "review",
                            "--file",
                            str(decisions),
                            "--json",
                        ],
                    ),
                    redirect_stdout(output),
                ):
                    return_code = cli.run()
            finally:
                os.chdir(prior_cwd)

            self.assertEqual(return_code, 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["data"]["rejected_count"], 1)
            self.assertIn(
                payload["errors"][0]["code"],
                {"stale_review_state", "stale_batch_generation"},
            )
            [persisted] = self._ledger(root)
            self.assertEqual(persisted["flow_type"], "internal_transfer")
            self.assertEqual(persisted["reason"], "Synthetic competing review")

    def test_batch_review_does_not_overwrite_new_hidden_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "hidden-state.csv",
                ["2026-05-04,SYNTHETIC HIDDEN STATE,-10.00,HKD"],
            )
            [row] = self._ledger(root)
            decisions = root / "hidden-state-decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "decision": "expense",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            empty_statement = root / "empty-source.csv"
            empty_statement.write_text(
                "Date,Description,Amount,Currency\n",
                encoding="utf-8",
            )
            manifest_path = identity_manifest_path(root / "output" / "categorized.csv")
            prior_manifest = manifest_path.read_bytes()

            import honeymoney.cli as cli
            import honeymoney.corrections as corrections

            original_persist = corrections.persist_generation
            competed = False

            def persist_after_empty_import(authoritative_path, files, **kwargs) -> None:
                nonlocal competed
                if not competed:
                    competed = True
                    imported = self._run_cli(
                        [
                            "import",
                            str(empty_statement),
                            "--no-interactive",
                        ],
                        cwd=root,
                    )
                    self.assertEqual(imported.returncode, 0, imported.stderr)
                original_persist(authoritative_path, files, **kwargs)

            prior_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with (
                    patch.object(
                        corrections,
                        "persist_generation",
                        persist_after_empty_import,
                    ),
                    patch.object(
                        sys,
                        "argv",
                        [
                            "honeymoney",
                            "review",
                            "--file",
                            str(decisions),
                            "--json",
                        ],
                    ),
                    redirect_stdout(output),
                ):
                    return_code = cli.run()
            finally:
                os.chdir(prior_cwd)

            self.assertEqual(return_code, 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["errors"][0]["code"],
                "stale_batch_generation",
            )
            self.assertNotEqual(manifest_path.read_bytes(), prior_manifest)
            [persisted] = self._ledger(root)
            self.assertEqual(persisted["flow_type"], "unresolved")

    def test_batch_review_recovery_restores_the_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "recovery.csv",
                ["2026-05-04,SYNTHETIC RECOVERY DEBIT,-10.00,HKD"],
            )
            [row] = self._ledger(root)
            decisions = root / "recovery-decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "decision": "expense",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            before = self._artifacts(root)

            failed = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
                filesystem_fault="replace-before:categorized.csv",
            )

            self.assertEqual(failed.returncode, 2, failed.stderr)
            self.assertEqual(self._artifacts(root), before)
            recovered = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(recovered.stdout)["data"]["applied_count"], 1)

    def test_invalid_review_combinations_and_empty_selection_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root, "debit.csv", ["2026-05-04,SYNTHETIC DEBIT,-10.00,HKD"]
            )
            [row] = self._ledger(root)
            before = self._artifacts(root, include_rules=True)

            invalid_commands = [
                ["review", "--transaction", "missing", "--as", "income"],
                [
                    "review",
                    "--transaction",
                    row["transaction_id"],
                    "--as",
                    "unsupported",
                ],
                [
                    "review",
                    "--transaction",
                    row["transaction_id"],
                    "--as",
                    "refund",
                    "--remember",
                    "--yes",
                ],
                ["review", "--remember", "--yes"],
                ["review", "--json"],
            ]
            for command in invalid_commands:
                with self.subTest(command=command):
                    result = self._run_cli(command, cwd=root)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(self._artifacts(root, include_rules=True), before)

            empty = self._run_cli(
                [
                    "review",
                    "--month",
                    "2026-05",
                    "--flow",
                    "unresolved",
                    "--direction",
                    "inflow",
                ],
                cwd=root,
            )
            self.assertEqual(empty.returncode, 0, empty.stderr)
            self.assertIn("No transactions matched", empty.stdout)
            self.assertEqual(self._artifacts(root, include_rules=True), before)

    def test_one_shot_unknown_id_on_empty_ledger_is_a_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            before = self._artifacts(root, include_rules=True)

            result = self._run_cli(
                [
                    "review",
                    "--transaction",
                    "synthetic-missing-id",
                    "--as",
                    "income",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout.count("\n"), 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["command"], "review")
            self.assertEqual(payload["status"], "error")
            self.assertIn("Unknown transaction_id", payload["errors"][0]["message"])
            self.assertEqual(self._artifacts(root, include_rules=True), before)

    def test_skip_and_quit_leave_all_review_artifacts_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "credits.csv",
                [
                    "2026-05-04,FIRST CREDIT,100.00,HKD",
                    "2026-05-05,SECOND CREDIT,200.00,HKD",
                ],
            )
            before = self._artifacts(root, include_rules=True)

            skipped = self._run_cli(
                ["review", "2026-05", "--flow", "unresolved"],
                cwd=root,
                input_text="i\nn\nq\n",
            )

            self.assertEqual(skipped.returncode, 0, skipped.stderr)
            self.assertIn(
                "Review complete: 0 updated from 2 matched; "
                "2 still match these filters; 2 in review queue",
                skipped.stdout,
            )
            self.assertEqual(self._artifacts(root, include_rules=True), before)

    def test_filtered_review_reports_remaining_matches_after_resolving_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "credits.csv",
                [
                    "2026-05-04,FIRST CREDIT,100.00,HKD",
                    "2026-05-05,SECOND CREDIT,200.00,HKD",
                ],
            )

            result = self._run_cli(
                [
                    "review",
                    "--flow",
                    "unresolved",
                    "--direction",
                    "inflow",
                    "--start",
                    "2026-05-01",
                    "--end",
                    "2026-05-31",
                ],
                cwd=root,
                input_text="i\nn\n\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Review complete: 1 updated from 2 matched; "
                "1 still match these filters; 1 in review queue",
                result.stdout,
            )
            rows = {row["merchant"]: row for row in self._ledger(root)}
            self.assertEqual(rows["FIRST CREDIT"]["flow_type"], "income")
            self.assertEqual(rows["SECOND CREDIT"]["flow_type"], "unresolved")

    def test_non_income_decisions_remain_excluded_from_income(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "flows.csv",
                [
                    "2026-05-04,SYNTHETIC REFUND,100.00,HKD",
                    "2026-05-05,OWNED TRANSFER,200.00,HKD",
                    "2026-05-06,CARD SETTLEMENT,-300.00,HKD",
                    "2026-05-07,BROKER FUNDING,-400.00,HKD",
                    "2026-05-08,HOUSEHOLD PURCHASE,-50.00,HKD",
                ],
            )
            decisions = {
                "SYNTHETIC REFUND": "refund",
                "OWNED TRANSFER": "internal-transfer",
                "CARD SETTLEMENT": "credit-card-payment",
                "BROKER FUNDING": "investment-transfer",
                "HOUSEHOLD PURCHASE": "expense",
            }
            rows = {row["merchant"]: row for row in self._ledger(root)}
            for merchant, decision in decisions.items():
                result = self._run_cli(
                    [
                        "review",
                        "--transaction",
                        rows[merchant]["transaction_id"],
                        "--as",
                        decision,
                        "--json",
                    ],
                    cwd=root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            updated = {row["merchant"]: row for row in self._ledger(root)}
            self.assertEqual(updated["SYNTHETIC REFUND"]["flow_type"], "refund")
            self.assertEqual(
                updated["OWNED TRANSFER"]["flow_type"], "internal_transfer"
            )
            self.assertEqual(updated["OWNED TRANSFER"]["category"], "Internal Transfer")
            self.assertEqual(
                updated["CARD SETTLEMENT"]["flow_type"], "credit_card_payment"
            )
            self.assertEqual(
                updated["BROKER FUNDING"]["flow_type"], "investment_transfer"
            )
            self.assertEqual(updated["HOUSEHOLD PURCHASE"]["flow_type"], "expense")
            self.assertNotIn("income", {row["flow_type"] for row in updated.values()})

    def test_explicit_income_is_protected_from_transfer_pairing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["csv"]["columns"]["account_id"] = "Account ID"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            statement = root / "pair.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency,Account ID",
                        "2026-05-04,CONFIRMED INCOME,500.00,HKD,synthetic_primary",
                        "2026-05-04,EQUAL OUTFLOW,-500.00,HKD,synthetic_secondary",
                    ]
                ),
                encoding="utf-8",
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            [income_row, _] = self._ledger(root)

            result = self._run_cli(
                [
                    "review",
                    "--transaction",
                    income_row["transaction_id"],
                    "--as",
                    "income",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            updated = {row["merchant"]: row for row in self._ledger(root)}
            income = updated["CONFIRMED INCOME"]
            self.assertEqual(income["flow_type"], "income")
            self.assertEqual(income["flow_source"], "correction")
            self.assertEqual(income["reconciliation_status"], "not_applicable")
            self.assertEqual(income["paired_transaction_id"], "")
            self.assertNotEqual(
                updated["EQUAL OUTFLOW"]["flow_type"], "internal_transfer"
            )

    def test_manual_pair_replay_survives_migration_reconcile_and_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "cash-movements.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,SYNTHETIC CASH OUT,-500.00,HKD",
                        "2026-05-04,SYNTHETIC CASH IN,500.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            categorized_path = root / "output" / "categorized.csv"
            state = load_identity_state(categorized_path)
            source_rows = state.source_rows
            self.assertIsNotNone(source_rows)
            assert source_rows is not None
            categorized_path.write_text(
                csv_document(SOURCE_OCCURRENCE_COLUMNS, source_rows),
                encoding="utf-8",
            )
            source_occurrences_path(categorized_path).unlink()
            overlap_manifest_path(categorized_path).unlink()
            rows = {row["merchant"]: row for row in self._ledger(root)}
            nominated_ids = [
                rows["SYNTHETIC CASH OUT"]["transaction_id"],
                rows["SYNTHETIC CASH IN"]["transaction_id"],
            ]

            paired = self._run_cli(
                [
                    "review",
                    "pair",
                    *nominated_ids,
                    "--yes",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(paired.returncode, 0, paired.stderr)
            payload = json.loads(paired.stdout)
            self.assertEqual(payload["command"], "review.pair")
            self.assertEqual(payload["data"]["paired_count"], 2)
            self.assertFalse(payload["data"]["unchanged"])
            self.assertTrue(payload["data"]["changed"])
            self.assertEqual(payload["data"]["result"], "paired")
            self.assertNotIn("SYNTHETIC CASH", paired.stdout)
            self.assertNotIn("cash-movements.csv", paired.stdout)
            linked = {row["merchant"]: row for row in self._ledger(root)}
            outgoing = linked["SYNTHETIC CASH OUT"]
            incoming = linked["SYNTHETIC CASH IN"]
            current_ids = sorted(
                [outgoing["transaction_id"], incoming["transaction_id"]]
            )
            self.assertNotEqual(set(current_ids), set(nominated_ids))
            self.assertEqual(payload["data"]["transaction_ids"], current_ids)
            self.assertEqual(payload["data"]["pair_id"], outgoing["transfer_group_id"])
            self.assertEqual(
                {row["category"] for row in linked.values()},
                {"Internal Transfer"},
            )
            self.assertEqual(
                {row["flow_type"] for row in linked.values()},
                {"internal_transfer"},
            )
            self.assertEqual(
                {row["flow_source"] for row in linked.values()},
                {"correction"},
            )
            self.assertEqual(
                outgoing["paired_transaction_id"], incoming["transaction_id"]
            )
            self.assertEqual(
                incoming["paired_transaction_id"], outgoing["transaction_id"]
            )
            self.assertEqual(
                outgoing["transfer_group_id"], incoming["transfer_group_id"]
            )
            self.assertTrue(outgoing["transfer_group_id"].startswith("mpair_"))
            self.assertEqual(
                {row["reconciliation_status"] for row in linked.values()},
                {"paired"},
            )
            self.assertTrue(
                all("manual_transfer_pair:" in row["flags"] for row in linked.values())
            )
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                corrections = list(csv.DictReader(handle))
            self.assertEqual(
                {row["manual_pair_id"] for row in corrections},
                {outgoing["transfer_group_id"]},
            )

            replay_before = self._pair_artifacts(root)
            repeated = self._run_cli(
                [
                    "review",
                    "pair",
                    *nominated_ids,
                    "--yes",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            repeated_data = json.loads(repeated.stdout)["data"]
            self.assertTrue(repeated_data["unchanged"])
            self.assertFalse(repeated_data["changed"])
            self.assertEqual(repeated_data["result"], "already_paired")
            self.assertEqual(repeated_data["pair_id"], outgoing["transfer_group_id"])
            self.assertEqual(repeated_data["transaction_ids"], current_ids)
            self.assertNotIn("SYNTHETIC CASH", repeated.stdout)
            self.assertNotIn("500.00", repeated.stdout)
            self.assertEqual(self._pair_artifacts(root), replay_before)

            human = self._run_cli(
                [
                    "review",
                    "pair",
                    *nominated_ids,
                    "--yes",
                ],
                cwd=root,
            )
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("already paired", human.stdout)
            self.assertIn(outgoing["transfer_group_id"], human.stdout)
            for transaction_id in current_ids:
                self.assertIn(transaction_id, human.stdout)
            self.assertNotIn("SYNTHETIC CASH", human.stdout)
            self.assertNotIn("500.00", human.stdout)
            self.assertEqual(self._pair_artifacts(root), replay_before)

            reconciled = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            self.assertEqual(json.loads(reconciled.stdout)["data"]["paired_groups"], 1)
            reconciled_artifacts = self._pair_artifacts(root)
            replayed_after_reconcile = self._run_cli(
                ["review", "pair", *nominated_ids, "--yes", "--json"],
                cwd=root,
            )
            self.assertEqual(
                replayed_after_reconcile.returncode,
                0,
                replayed_after_reconcile.stderr,
            )
            self.assertFalse(
                json.loads(replayed_after_reconcile.stdout)["data"]["changed"]
            )
            self.assertEqual(self._pair_artifacts(root), reconciled_artifacts)

            replaced = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            replacement_rows = self._ledger(root)
            self.assertEqual(
                {row["transfer_group_id"] for row in replacement_rows},
                {outgoing["transfer_group_id"]},
            )
            self.assertEqual(
                {row["reconciliation_status"] for row in replacement_rows},
                {"paired"},
            )
            replaced_artifacts = self._pair_artifacts(root)
            replayed_after_replace = self._run_cli(
                ["review", "pair", *nominated_ids, "--yes", "--json"],
                cwd=root,
            )
            self.assertEqual(
                replayed_after_replace.returncode,
                0,
                replayed_after_replace.stderr,
            )
            self.assertFalse(
                json.loads(replayed_after_replace.stdout)["data"]["changed"]
            )
            self.assertEqual(self._pair_artifacts(root), replaced_artifacts)

            report = self._run_cli(
                ["report", "2026-05", "--no-open"],
                cwd=root,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            html = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn('id="tile-income">0.00</div>', html)
            self.assertIn('id="tile-spending">0.00</div>', html)
            report_artifacts = self._pair_artifacts(root)
            replayed_after_report = self._run_cli(
                ["review", "pair", *nominated_ids, "--yes", "--json"],
                cwd=root,
            )
            self.assertEqual(
                replayed_after_report.returncode,
                0,
                replayed_after_report.stderr,
            )
            self.assertFalse(
                json.loads(replayed_after_report.stdout)["data"]["changed"]
            )
            self.assertEqual(self._pair_artifacts(root), report_artifacts)

    def test_review_decision_atomically_supersedes_a_manual_pair(self) -> None:
        cases = (
            ("expense", "SYNTHETIC CASH OUT", "expense"),
            ("income", "SYNTHETIC CASH IN", "income"),
            ("unresolved", "SYNTHETIC CASH OUT", "unresolved"),
        )
        for decision, selected_merchant, expected_flow in cases:
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                self._import_rows(
                    root,
                    "superseded-pair.csv",
                    [
                        "2026-05-04,SYNTHETIC CASH OUT,-500.00,HKD",
                        "2026-05-04,SYNTHETIC CASH IN,500.00,HKD",
                    ],
                )
                rows = {row["merchant"]: row for row in self._ledger(root)}
                paired = self._run_cli(
                    [
                        "review",
                        "pair",
                        rows["SYNTHETIC CASH OUT"]["transaction_id"],
                        rows["SYNTHETIC CASH IN"]["transaction_id"],
                        "--yes",
                    ],
                    cwd=root,
                )
                self.assertEqual(paired.returncode, 0, paired.stderr)

                reviewed = self._run_cli(
                    [
                        "review",
                        "--transaction",
                        rows[selected_merchant]["transaction_id"],
                        "--as",
                        decision,
                        "--json",
                    ],
                    cwd=root,
                )

                self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
                updated = {row["merchant"]: row for row in self._ledger(root)}
                self.assertEqual(
                    updated[selected_merchant]["flow_type"],
                    expected_flow,
                )
                for row in updated.values():
                    self.assertEqual(row["paired_transaction_id"], "")
                    self.assertEqual(row["transfer_group_id"], "")
                    self.assertNotIn("manual_transfer_pair:", row["flags"])
                partner_merchant = (
                    "SYNTHETIC CASH IN"
                    if selected_merchant == "SYNTHETIC CASH OUT"
                    else "SYNTHETIC CASH OUT"
                )
                self.assertEqual(updated[partner_merchant]["flow_type"], "unresolved")
                self.assertEqual(updated[partner_merchant]["needs_review"], "true")
                with (root / "corrections.csv").open(
                    newline="",
                    encoding="utf-8",
                ) as handle:
                    corrections = list(csv.DictReader(handle))
                self.assertEqual(
                    {row["manual_pair_id"] for row in corrections},
                    {""},
                )
                reconciled = self._run_cli(["reconcile", "--json"], cwd=root)
                self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
                self.assertEqual(
                    json.loads(reconciled.stdout)["data"]["paired_groups"],
                    0,
                )

    def test_manual_pair_can_resolve_transfer_candidate_ambiguity(self) -> None:
        import honeymoney.cli as cli

        left = {
            "transaction_id": "txn_ambiguous_out",
            "account_id": "cash_account",
            "posted_amount": "-100.00",
            "posted_currency": "HKD",
            "owner": "Household",
            "reconciliation_status": "ambiguous",
            "flags": "reconciliation_ambiguous",
            "review_reasons": "accounting_flow",
        }
        right = {
            "transaction_id": "txn_same_account_in",
            "account_id": "cash_account",
            "posted_amount": "100.00",
            "posted_currency": "HKD",
            "owner": "Household",
            "reconciliation_status": "not_applicable",
            "flags": "",
            "review_reasons": "accounting_flow",
        }
        result = Mock(remaining_review_count=0, ledger_rows=[left, right])

        with (
            patch.object(
                cli,
                "_load_config",
                return_value={
                    "corrections": "corrections.csv",
                    "paths": {"output": "categorized.csv"},
                },
            ),
            patch.object(cli, "read_ledger", return_value=[left, right]),
            patch.object(
                cli,
                "apply_correction_operation",
                return_value=result,
            ) as apply_operation,
            redirect_stdout(io.StringIO()),
        ):
            return_code = cli._manual_pair_review(
                [
                    left["transaction_id"],
                    right["transaction_id"],
                    "--yes",
                ]
            )

        self.assertEqual(return_code, 0)
        patches = apply_operation.call_args.args[2]
        self.assertEqual(
            set(patches), {left["transaction_id"], right["transaction_id"]}
        )
        pair_ids = {patch["manual_pair_id"] for patch in patches.values()}
        self.assertEqual(len(pair_ids), 1)
        self.assertTrue(next(iter(pair_ids)).startswith("mpair_"))

    def test_manual_pair_fails_closed_when_membership_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "membership.csv"
            original = [
                "Date,Description,Amount,Currency",
                "2026-05-04,SYNTHETIC WITHDRAWAL,-100.00,HKD",
                "2026-05-04,SYNTHETIC REDEPOSIT,100.00,HKD",
            ]
            statement.write_text("\n".join(original), encoding="utf-8")
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            first, second = self._ledger(root)
            confirmed = self._run_cli(
                [
                    "review",
                    "pair",
                    first["transaction_id"],
                    second["transaction_id"],
                    "--yes",
                ],
                cwd=root,
            )
            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)

            statement.write_text("\n".join(original[:2]), encoding="utf-8")
            replaced = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )

            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            [remaining] = self._ledger(root)
            self.assertEqual(remaining["paired_transaction_id"], "")
            self.assertEqual(remaining["transfer_group_id"], "")
            self.assertEqual(remaining["flow_type"], "unresolved")
            self.assertEqual(remaining["reconciliation_status"], "unmatched")
            self.assertIn("manual_transfer_pair_invalid", remaining["flags"])
            self.assertIn("accounting_flow", remaining["review_reasons"].split(";"))
            missing_before = self._pair_artifacts(root)
            replayed_missing = self._run_cli(
                [
                    "review",
                    "pair",
                    first["transaction_id"],
                    second["transaction_id"],
                    "--yes",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(replayed_missing.returncode, 2, replayed_missing.stderr)
            self.assertEqual(
                json.loads(replayed_missing.stdout)["errors"][0]["code"],
                "manual_pair_stale_transaction",
            )
            self.assertNotIn("SYNTHETIC", replayed_missing.stdout)
            self.assertNotIn("100.00", replayed_missing.stdout)
            self.assertEqual(self._pair_artifacts(root), missing_before)

            statement.write_text("\n".join(original), encoding="utf-8")
            restored = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            restored_rows = self._ledger(root)
            self.assertEqual(
                {row["reconciliation_status"] for row in restored_rows},
                {"paired"},
            )
            self.assertTrue(
                all(
                    "manual_transfer_pair_invalid" not in row["flags"]
                    for row in restored_rows
                )
            )

    def test_manual_pair_replay_rejects_extra_stored_members_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "extra-pair-member.csv",
                [
                    "2026-05-04,SYNTHETIC PAIR OUT,-100.00,HKD",
                    "2026-05-04,SYNTHETIC PAIR IN,100.00,HKD",
                    "2026-05-05,SYNTHETIC EXTRA,-30.00,HKD",
                ],
            )
            rows = {row["merchant"]: row for row in self._ledger(root)}
            pair_ids = [
                rows["SYNTHETIC PAIR OUT"]["transaction_id"],
                rows["SYNTHETIC PAIR IN"]["transaction_id"],
            ]
            paired = self._run_cli(
                ["review", "pair", *pair_ids, "--yes", "--json"],
                cwd=root,
            )
            self.assertEqual(paired.returncode, 0, paired.stderr)
            pair_id = json.loads(paired.stdout)["data"]["pair_id"]
            correction_path = root / "corrections.csv"
            with correction_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = list(reader.fieldnames or [])
                corrections = list(reader)
            corrections.append(
                {
                    **{field: "" for field in fieldnames},
                    "transaction_id": rows["SYNTHETIC EXTRA"]["transaction_id"],
                    "manual_pair_id": pair_id,
                }
            )
            with correction_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(corrections)
            before = self._pair_artifacts(root)

            replayed = self._run_cli(
                ["review", "pair", *pair_ids, "--yes", "--json"],
                cwd=root,
            )

            self.assertEqual(replayed.returncode, 2, replayed.stderr)
            self.assertEqual(
                json.loads(replayed.stdout)["errors"][0]["code"],
                "manual_pair_conflict",
            )
            self.assertNotIn("SYNTHETIC", replayed.stdout)
            self.assertNotIn("100.00", replayed.stdout)
            self.assertEqual(self._pair_artifacts(root), before)

    def test_manual_pair_rejects_correction_only_membership_without_writing(
        self,
    ) -> None:
        cases = ("missing_member", "conflicting_pair")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                self._import_rows(
                    root,
                    "correction-only-pair.csv",
                    [
                        "2026-05-04,SYNTHETIC PAIR OUT,-100.00,HKD",
                        "2026-05-04,SYNTHETIC PAIR IN,100.00,HKD",
                    ],
                )
                rows = {row["merchant"]: row for row in self._ledger(root)}
                pair_ids = [
                    rows["SYNTHETIC PAIR OUT"]["transaction_id"],
                    rows["SYNTHETIC PAIR IN"]["transaction_id"],
                ]
                stored_pair_id = (
                    manual_pair_id(pair_ids)
                    if case == "missing_member"
                    else manual_pair_id([pair_ids[0], "retired-transaction"])
                )
                correction_path = root / "corrections.csv"
                with correction_path.open(newline="", encoding="utf-8") as handle:
                    fieldnames = list(csv.DictReader(handle).fieldnames or [])
                correction = {field: "" for field in fieldnames}
                correction["transaction_id"] = pair_ids[0]
                correction["manual_pair_id"] = stored_pair_id
                with correction_path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerow(correction)
                before = self._pair_artifacts(root)

                replayed = self._run_cli(
                    ["review", "pair", *pair_ids, "--yes", "--json"],
                    cwd=root,
                )

                self.assertEqual(replayed.returncode, 2, replayed.stderr)
                self.assertEqual(
                    json.loads(replayed.stdout)["errors"][0]["code"],
                    "manual_pair_conflict",
                )
                self.assertNotIn("SYNTHETIC", replayed.stdout)
                self.assertNotIn("100.00", replayed.stdout)
                self.assertEqual(self._pair_artifacts(root), before)

    def test_manual_pair_rejects_ledger_only_membership_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "ledger-only-pair.csv",
                [
                    "2026-05-04,SYNTHETIC PAIR OUT,-100.00,HKD",
                    "2026-05-04,SYNTHETIC PAIR IN,100.00,HKD",
                    "2026-05-05,SYNTHETIC EXTRA,-30.00,HKD",
                ],
            )
            ledger_path = root / "output" / "categorized.csv"
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fieldnames = list(reader.fieldnames or [])
                rows = list(reader)
            rows_by_merchant = {row["merchant"]: row for row in rows}
            pair_ids = [
                rows_by_merchant["SYNTHETIC PAIR OUT"]["transaction_id"],
                rows_by_merchant["SYNTHETIC PAIR IN"]["transaction_id"],
            ]
            pair_id = manual_pair_id(pair_ids)
            rows_by_merchant["SYNTHETIC EXTRA"]["flags"] = (
                f"{MANUAL_PAIR_FLAG_PREFIX}{pair_id}"
            )
            with ledger_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            before = self._pair_artifacts(root)

            rejected = self._run_cli(
                ["review", "pair", *pair_ids, "--yes", "--json"],
                cwd=root,
            )

            self.assertEqual(rejected.returncode, 2, rejected.stderr)
            self.assertEqual(
                json.loads(rejected.stdout)["errors"][0]["code"],
                "manual_pair_conflict",
            )
            self.assertNotIn("SYNTHETIC", rejected.stdout)
            self.assertNotIn("100.00", rejected.stdout)
            self.assertEqual(self._pair_artifacts(root), before)

    def test_manual_pair_rejects_stale_same_sign_and_conflicting_rows_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "invalid-pairs.csv",
                [
                    "2026-05-04,SYNTHETIC POSITIVE A,100.00,HKD",
                    "2026-05-04,SYNTHETIC POSITIVE B,100.00,HKD",
                    "2026-05-04,SYNTHETIC NEGATIVE A,-100.00,HKD",
                    "2026-05-04,SYNTHETIC NEGATIVE B,-100.00,HKD",
                ],
            )
            rows = {row["merchant"]: row for row in self._ledger(root)}
            before = self._artifacts(root)

            for other_id, code in (
                (
                    rows["SYNTHETIC POSITIVE B"]["transaction_id"],
                    "manual_pair_same_sign",
                ),
                ("stale-synthetic-id", "manual_pair_stale_transaction"),
            ):
                with self.subTest(code=code):
                    rejected = self._run_cli(
                        [
                            "review",
                            "pair",
                            rows["SYNTHETIC POSITIVE A"]["transaction_id"],
                            other_id,
                            "--yes",
                            "--json",
                        ],
                        cwd=root,
                    )
                    self.assertEqual(rejected.returncode, 2, rejected.stderr)
                    error = json.loads(rejected.stdout)["errors"][0]
                    self.assertEqual(error["code"], code)
                    self.assertNotIn("SYNTHETIC", rejected.stdout)
                    self.assertNotIn("stale-synthetic-id", rejected.stdout)
                    self.assertEqual(self._artifacts(root), before)

            first_pair = self._run_cli(
                [
                    "review",
                    "pair",
                    rows["SYNTHETIC POSITIVE A"]["transaction_id"],
                    rows["SYNTHETIC NEGATIVE A"]["transaction_id"],
                    "--yes",
                ],
                cwd=root,
            )
            self.assertEqual(first_pair.returncode, 0, first_pair.stderr)
            paired_artifacts = self._artifacts(root)
            conflict = self._run_cli(
                [
                    "review",
                    "pair",
                    rows["SYNTHETIC POSITIVE A"]["transaction_id"],
                    rows["SYNTHETIC NEGATIVE B"]["transaction_id"],
                    "--yes",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(conflict.returncode, 2, conflict.stderr)
            self.assertEqual(
                json.loads(conflict.stdout)["errors"][0]["code"],
                "manual_pair_conflict",
            )
            self.assertEqual(self._artifacts(root), paired_artifacts)

    def test_manual_pair_persistence_failure_restores_all_review_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "pair-recovery.csv",
                [
                    "2026-05-04,SYNTHETIC PAIR OUT,-40.00,HKD",
                    "2026-05-04,SYNTHETIC PAIR IN,40.00,HKD",
                ],
            )
            left, right = self._ledger(root)
            before = self._artifacts(root)

            failed = self._run_cli(
                [
                    "review",
                    "pair",
                    left["transaction_id"],
                    right["transaction_id"],
                    "--yes",
                    "--json",
                ],
                cwd=root,
                filesystem_fault="replace-before:categorized.csv",
            )

            self.assertEqual(failed.returncode, 2, failed.stderr)
            self.assertEqual(self._artifacts(root), before)

    def test_ollama_income_category_cannot_establish_income_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root, "credit.csv", ["2026-05-04,MODEL-LABELLED CREDIT,500.00,HKD"]
            )
            ledger_path = root / "output" / "categorized.csv"
            [row] = self._ledger(root)
            row["category"] = "Income"
            row["flow_type"] = ""
            row["flow_source"] = ""
            row["flags"] = "ollama_categorized"
            with ledger_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            [updated] = self._ledger(root)
            self.assertEqual(updated["category"], "Income")
            self.assertEqual(updated["flow_type"], "unresolved")
            self.assertNotEqual(updated["flow_source"], "correction")

    def test_remembered_income_rule_is_exact_directional_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root, "first.csv", ["2026-05-04,RECURRING CREDIT,600.00,HKD"]
            )
            [first] = self._ledger(root)

            for _ in range(2):
                result = self._run_cli(
                    [
                        "review",
                        "--transaction",
                        first["transaction_id"],
                        "--as",
                        "income",
                        "--remember",
                        "--yes",
                        "--json",
                    ],
                    cwd=root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["data"]["rule_matches"], 1)

            rules = json.loads((root / "rules.json").read_text(encoding="utf-8"))[
                "rules"
            ]
            remembered = [rule for rule in rules if rule["id"].startswith("review_")]
            self.assertEqual(len(remembered), 1)
            conditions = {
                condition["field"]: condition
                for condition in remembered[0]["conditions"]
            }
            self.assertEqual(
                set(conditions),
                {"institution", "account_id", "original_description", "direction"},
            )
            self.assertTrue(
                all(
                    condition["match_type"] == "exact"
                    for condition in conditions.values()
                )
            )

            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"].update(
                {
                    "enabled": True,
                    "url": "http://127.0.0.1:1/api/generate",
                    "timeout_seconds": 1,
                }
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")

            self._import_rows(
                root,
                "future.csv",
                [
                    "2026-06-04,RECURRING CREDIT,650.00,HKD",
                    "2026-06-05,RECURRING CREDIT,-20.00,HKD",
                    "2026-06-06,OTHER CREDIT,650.00,HKD",
                ],
                ollama_mode="unavailable",
            )
            rows = {(row["date"], row["merchant"]): row for row in self._ledger(root)}
            future = rows[("2026-06-04", "RECURRING CREDIT")]
            self.assertEqual(future["category"], "Income")
            self.assertEqual(future["flow_type"], "income")
            self.assertEqual(future["flow_source"], "rule")
            self.assertEqual(
                rows[("2026-06-05", "RECURRING CREDIT")]["flow_type"], "unresolved"
            )
            self.assertEqual(
                rows[("2026-06-06", "OTHER CREDIT")]["flow_type"], "unresolved"
            )

    def test_remember_rejects_missing_identity_fields_without_partial_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(root, "credit.csv", ["2026-05-04,,300.00,HKD"])
            [row] = self._ledger(root)
            self.assertEqual(row["original_description"], "")
            before = self._artifacts(root, include_rules=True)

            result = self._run_cli(
                [
                    "review",
                    "--transaction",
                    row["transaction_id"],
                    "--as",
                    "income",
                    "--remember",
                    "--yes",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Cannot remember income",
                json.loads(result.stdout)["errors"][0]["message"],
            )
            self.assertEqual(self._artifacts(root, include_rules=True), before)

    def test_status_reports_unresolved_direction_counts_and_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            self._import_rows(
                root,
                "flows.csv",
                [
                    "2026-05-04,UNRESOLVED CREDIT,500.00,HKD",
                    "2026-05-05,UNRESOLVED DEBIT,-30.00,HKD",
                ],
            )

            human = self._run_cli(["status", "2026-05"], cwd=root)
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("Unresolved inflows:   1", human.stdout)
            self.assertIn("Unresolved outflows:  1", human.stdout)
            self.assertIn(
                "honeymoney review --flow unresolved --direction inflow", human.stdout
            )

            machine = self._run_cli(["status", "2026-05", "--json"], cwd=root)
            self.assertEqual(machine.returncode, 0, machine.stderr)
            data = json.loads(machine.stdout)["data"]
            self.assertEqual(data["unresolved_inflows"], 1)
            self.assertEqual(data["unresolved_outflows"], 1)


if __name__ == "__main__":
    unittest.main()
