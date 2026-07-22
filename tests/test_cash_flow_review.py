import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class CashFlowReviewTest(unittest.TestCase):
    def _run_cli(
        self, args: list[str], *, cwd: Path, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
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

    def _import_rows(self, root: Path, filename: str, rows: list[str]) -> None:
        statement = root / filename
        statement.write_text(
            "\n".join(["Date,Description,Amount,Currency", *rows]),
            encoding="utf-8",
        )
        result = self._run_cli(["import", str(statement), "--no-interactive"], cwd=root)
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
            self._import_rows(
                root, "credit.csv", ["2026-05-04,IDENTITY CREDIT,300.00,HKD"]
            )
            ledger_path = root / "output" / "categorized.csv"
            [row] = self._ledger(root)
            row["institution"] = ""
            with ledger_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
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
