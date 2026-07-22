import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.identity_state import LEGACY_CATEGORIZED_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[1]


class CashFlowWorkflowTest(unittest.TestCase):
    def _legacy_id(self, label: str) -> str:
        return "txn_" + hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]

    def _aliases(self, root: Path) -> dict[str, str]:
        return getattr(self, "_identity_aliases", {}).get(root, {})

    def _run_cli(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        aliases = self._aliases(cwd)
        corrections_path = cwd / "corrections.csv"
        if aliases and corrections_path.exists():
            with corrections_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                fieldnames = reader.fieldnames or []
                correction_rows = list(reader)
            if correction_rows:
                for row in correction_rows:
                    row["transaction_id"] = aliases.get(
                        row["transaction_id"], row["transaction_id"]
                    )
                with corrections_path.open("w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(correction_rows)
        if aliases and "--file" in args:
            correction_path = Path(args[args.index("--file") + 1])
            corrections = json.loads(correction_path.read_text(encoding="utf-8"))
            for correction in corrections:
                correction["transaction_id"] = aliases.get(
                    correction["transaction_id"], correction["transaction_id"]
                )
            correction_path.write_text(json.dumps(corrections), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _workspace(self, tmp: str, rows: list[dict[str, str]]) -> Path:
        root = Path(tmp)
        output = root / "output"
        output.mkdir()
        ledger = output / "categorized.csv"
        aliases = {
            row["transaction_id"]: self._legacy_id(row["transaction_id"])
            for row in rows
        }
        self._identity_aliases = getattr(self, "_identity_aliases", {})
        self._identity_aliases[root] = aliases
        ledger_rows = [
            {**row, "transaction_id": aliases[row["transaction_id"]]} for row in rows
        ]
        with ledger.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEGACY_CATEGORIZED_COLUMNS)
            writer.writeheader()
            writer.writerows(ledger_rows)
        (root / "config.json").write_text(
            json.dumps(
                {
                    "base_currency": "HKD",
                    "reconciliation": {"date_window_days": 3},
                    "corrections": str(root / "corrections.csv"),
                    "paths": {"output": str(ledger)},
                }
            ),
            encoding="utf-8",
        )
        (root / "corrections.csv").write_text(
            "transaction_id,category,flow_type,owner,payment_method,confidence,reason,notes,needs_review\n",
            encoding="utf-8",
        )
        return root

    def _ledger_rows(self, root: Path) -> list[dict[str, str]]:
        with (root / "output" / "categorized.csv").open(
            newline="", encoding="utf-8"
        ) as fh:
            rows = list(csv.DictReader(fh))
        reverse_aliases = {value: key for key, value in self._aliases(root).items()}
        for row in rows:
            row["transaction_id"] = reverse_aliases.get(
                row["transaction_id"], row["transaction_id"]
            )
            row["paired_transaction_id"] = reverse_aliases.get(
                row["paired_transaction_id"], row["paired_transaction_id"]
            )
        return rows

    def test_reconcile_pairs_bank_debit_and_card_credit_across_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_bank",
                        "date": "2026-05-30",
                        "account_id": "bank_main",
                        "payment_method": "Bank Account",
                        "amount_hkd": "-500.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_card",
                        "date": "2026-05-31",
                        "account_id": "card_main",
                        "payment_method": "Credit Card",
                        "amount_hkd": "500.00",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["command"], "reconcile")
            self.assertEqual(payload["data"]["paired_groups"], 1)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(
                {row["flow_type"] for row in rows.values()},
                {"credit_card_payment"},
            )
            self.assertEqual(
                {row["reconciliation_status"] for row in rows.values()}, {"paired"}
            )
            self.assertEqual(rows["txn_bank"]["paired_transaction_id"], "txn_card")
            self.assertEqual(rows["txn_card"]["paired_transaction_id"], "txn_bank")
            self.assertEqual(
                rows["txn_bank"]["transfer_group_id"],
                rows["txn_card"]["transfer_group_id"],
            )

    def test_reconcile_classifies_owned_account_transfer_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_bank_out",
                        "date": "2026-06-01",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-100.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_bank_in",
                        "date": "2026-06-01",
                        "account_id": "bank_savings",
                        "account_type": "bank",
                        "amount_hkd": "100.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_invest_out",
                        "date": "2026-06-02",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-250.00",
                        "category": "Shopping",
                    },
                    {
                        "transaction_id": "txn_invest_in",
                        "date": "2026-06-02",
                        "account_id": "brokerage",
                        "account_type": "investment",
                        "amount_hkd": "250.00",
                        "category": "Investments",
                    },
                    {
                        "transaction_id": "txn_card_same_day_bank",
                        "date": "2026-06-03",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-75.00",
                        "category": "Internal Transfer",
                    },
                    {
                        "transaction_id": "txn_card_same_day_card",
                        "date": "2026-06-03",
                        "account_id": "card_primary",
                        "account_type": "credit_card",
                        "amount_hkd": "75.00",
                        "category": "Internal Transfer",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["txn_bank_out"]["flow_type"], "internal_transfer")
            self.assertEqual(rows["txn_invest_out"]["flow_type"], "investment_transfer")
            self.assertEqual(
                rows["txn_card_same_day_card"]["flow_type"],
                "credit_card_payment",
            )
            self.assertEqual(
                rows["txn_card_same_day_bank"]["category"], "Internal Transfer"
            )

    def test_ambiguous_candidates_remain_unpaired_and_repeat_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_out",
                        "date": "2026-05-31",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-300.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_in_one",
                        "date": "2026-05-31",
                        "account_id": "bank_one",
                        "account_type": "bank",
                        "amount_hkd": "300.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_in_two",
                        "date": "2026-05-31",
                        "account_id": "bank_two",
                        "account_type": "bank",
                        "amount_hkd": "300.00",
                        "category": "Other",
                    },
                ],
            )

            first = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_text = (root / "output" / "categorized.csv").read_text(
                encoding="utf-8"
            )
            second = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                (root / "output" / "categorized.csv").read_text(encoding="utf-8"),
                first_text,
            )
            rows = self._ledger_rows(root)
            self.assertEqual(
                {row["reconciliation_status"] for row in rows}, {"ambiguous"}
            )
            self.assertEqual({row["paired_transaction_id"] for row in rows}, {""})
            self.assertEqual(json.loads(second.stdout)["data"]["paired_groups"], 0)

    def test_ambiguous_candidate_does_not_keep_confirmed_expense_treatment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_possible_expense",
                        "date": "2026-05-31",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-325.00",
                        "category": "Groceries",
                    },
                    {
                        "transaction_id": "txn_possible_transfer_one",
                        "date": "2026-05-31",
                        "account_id": "bank_one",
                        "account_type": "bank",
                        "amount_hkd": "325.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_possible_transfer_two",
                        "date": "2026-05-31",
                        "account_id": "bank_two",
                        "account_type": "bank",
                        "amount_hkd": "325.00",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = self._ledger_rows(root)
            self.assertEqual(
                {row["reconciliation_status"] for row in rows}, {"ambiguous"}
            )
            self.assertEqual({row["flow_type"] for row in rows}, {"unresolved"})
            self.assertEqual({row["needs_review"] for row in rows}, {"true"})
            self.assertTrue(
                all("reconciliation_ambiguous" in row["flags"] for row in rows)
            )

    def test_resolved_ambiguity_removes_only_generated_review_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_out",
                        "date": "2026-05-31",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-350.00",
                        "category": "Other",
                        "needs_review": "true",
                        "flags": "duplicate_suspected",
                        "reason": "Possible duplicate transaction",
                    },
                    {
                        "transaction_id": "txn_unique_in",
                        "date": "2026-05-31",
                        "account_id": "bank_one",
                        "account_type": "bank",
                        "amount_hkd": "350.00",
                        "category": "Other",
                        "needs_review": "false",
                        "flags": "",
                        "reason": "",
                    },
                    {
                        "transaction_id": "txn_excluded_in",
                        "date": "2026-05-31",
                        "account_id": "bank_two",
                        "account_type": "bank",
                        "amount_hkd": "350.00",
                        "category": "Other",
                        "needs_review": "false",
                        "flags": "",
                        "reason": "",
                    },
                ],
            )

            ambiguous = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(ambiguous.returncode, 0, ambiguous.stderr)
            self.assertEqual(
                {row["reconciliation_status"] for row in self._ledger_rows(root)},
                {"ambiguous"},
            )

            correction_path = root / "resolve-ambiguity.json"
            correction_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": "txn_excluded_in",
                            "flow_type": "income",
                            "needs_review": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            resolved = self._run_cli(
                ["correct", "--file", str(correction_path), "--json"], cwd=root
            )

            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["txn_out"]["reconciliation_status"], "paired")
            self.assertEqual(rows["txn_unique_in"]["reconciliation_status"], "paired")
            self.assertEqual(
                rows["txn_excluded_in"]["reconciliation_status"],
                "not_applicable",
            )
            for row in rows.values():
                self.assertNotIn("reconciliation_ambiguous", row["flags"])
                self.assertNotIn("Ambiguous transfer candidates", row["reason"])
            self.assertEqual(rows["txn_out"]["needs_review"], "true")
            self.assertEqual(rows["txn_out"]["flags"], "duplicate_suspected")
            self.assertEqual(
                rows["txn_out"]["reason"], "Possible duplicate transaction"
            )
            self.assertEqual(rows["txn_unique_in"]["needs_review"], "false")
            self.assertEqual(rows["txn_excluded_in"]["needs_review"], "false")
            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))
            self.assertEqual(
                [row["transaction_id"] for row in review_rows],
                [self._legacy_id("txn_out")],
            )

    def test_equal_salary_and_expense_are_not_hidden_as_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_salary",
                        "date": "2026-06-01",
                        "account_id": "bank_income",
                        "account_type": "bank",
                        "amount_hkd": "600.00",
                        "category": "Income",
                    },
                    {
                        "transaction_id": "txn_expense",
                        "date": "2026-06-01",
                        "account_id": "bank_spending",
                        "account_type": "bank",
                        "amount_hkd": "-600.00",
                        "category": "Groceries",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["txn_salary"]["flow_type"], "income")
            self.assertEqual(rows["txn_expense"]["flow_type"], "expense")
            self.assertEqual(
                {row["reconciliation_status"] for row in rows.values()},
                {"not_applicable"},
            )
            self.assertEqual(json.loads(result.stdout)["data"]["paired_groups"], 0)

    def test_protected_transfer_must_match_account_inferred_pair_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_conflicting_bank",
                        "date": "2026-06-01",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-410.00",
                        "category": "Other",
                        "flow_type": "internal_transfer",
                        "flow_source": "correction",
                    },
                    {
                        "transaction_id": "txn_conflicting_card",
                        "date": "2026-06-01",
                        "account_id": "card_primary",
                        "account_type": "credit_card",
                        "amount_hkd": "410.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_matching_bank",
                        "date": "2026-06-02",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-420.00",
                        "category": "Other",
                        "flow_type": "credit_card_payment",
                        "flow_source": "rule",
                    },
                    {
                        "transaction_id": "txn_matching_card",
                        "date": "2026-06-02",
                        "account_id": "card_primary",
                        "account_type": "credit_card",
                        "amount_hkd": "420.00",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(json.loads(result.stdout)["data"]["paired_groups"], 1)
            self.assertEqual(
                rows["txn_conflicting_bank"]["flow_type"], "internal_transfer"
            )
            self.assertEqual(
                rows["txn_conflicting_bank"]["reconciliation_status"], "unmatched"
            )
            self.assertEqual(
                rows["txn_conflicting_card"]["reconciliation_status"],
                "not_applicable",
            )
            self.assertEqual(
                {
                    rows["txn_matching_bank"]["flow_type"],
                    rows["txn_matching_card"]["flow_type"],
                },
                {"credit_card_payment"},
            )
            self.assertEqual(
                {
                    rows["txn_matching_bank"]["reconciliation_status"],
                    rows["txn_matching_card"]["reconciliation_status"],
                },
                {"paired"},
            )

    def test_strong_unmatched_payment_and_external_flow_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_payment",
                        "date": "2026-05-31",
                        "account_id": "card_primary",
                        "account_type": "credit_card",
                        "amount_hkd": "450.00",
                        "category": "Credit Card Payment",
                    },
                    {
                        "transaction_id": "txn_deposit",
                        "date": "2026-06-01",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "700.00",
                        "category": "Cash",
                    },
                    {
                        "transaction_id": "txn_other",
                        "date": "2026-06-02",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "800.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_income",
                        "date": "2026-06-03",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "900.00",
                        "category": "Income",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["txn_payment"]["flow_type"], "credit_card_payment")
            self.assertEqual(rows["txn_payment"]["reconciliation_status"], "unmatched")
            self.assertEqual(rows["txn_payment"]["paired_transaction_id"], "")
            self.assertEqual(rows["txn_deposit"]["flow_type"], "unresolved")
            self.assertEqual(rows["txn_other"]["flow_type"], "unresolved")
            self.assertEqual(rows["txn_income"]["flow_type"], "income")

    def test_model_provenance_cannot_establish_any_protected_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": f"txn_{category.lower().replace(' ', '_')}",
                        "date": "2026-06-01",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "100.00",
                        "category": category,
                        "flags": "ollama_categorized",
                    }
                    for category in [
                        "Income",
                        "Credit Card Payment",
                        "Internal Transfer",
                        "Savings",
                        "Investments",
                    ]
                ],
            )
            result = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {row["flow_type"] for row in self._ledger_rows(root)}, {"unresolved"}
            )

    def test_report_headlines_net_refunds_and_show_unresolved_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_expense",
                        "date": "2026-06-01",
                        "account_id": "card_primary",
                        "account_type": "credit_card",
                        "amount_hkd": "-1000.00",
                        "category": "Groceries",
                    },
                    {
                        "transaction_id": "txn_refund",
                        "date": "2026-06-02",
                        "account_id": "card_primary",
                        "account_type": "credit_card",
                        "amount_hkd": "200.00",
                        "category": "Groceries",
                    },
                    {
                        "transaction_id": "txn_salary",
                        "date": "2026-06-03",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "5000.00",
                        "category": "Income",
                    },
                    {
                        "transaction_id": "txn_savings",
                        "date": "2026-06-04",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-3000.00",
                        "category": "Savings",
                    },
                    {
                        "transaction_id": "txn_unresolved_in",
                        "date": "2026-06-05",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "700.00",
                        "category": "Cash",
                    },
                    {
                        "transaction_id": "txn_unresolved_out",
                        "date": "2026-06-06",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "-400.00",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(
                ["report", "--month", "2026-06", "--no-open"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn('id="tile-spending">-800.00<', report)
            self.assertIn('id="tile-income">5,000.00<', report)
            self.assertIn('id="tile-net">4,200.00<', report)
            self.assertIn('id="tile-unresolved-inflow">700.00<', report)
            self.assertIn('id="tile-unresolved-outflow">-400.00<', report)
            self.assertIn('"flow_type": "refund"', report)
            self.assertIn('"flow_type": "investment_transfer"', report)

    def test_manual_flow_correction_survives_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_confirmed_income",
                        "date": "2026-06-01",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "125.00",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_equal_outflow",
                        "date": "2026-06-01",
                        "account_id": "bank_secondary",
                        "account_type": "bank",
                        "amount_hkd": "-125.00",
                        "category": "Other",
                    },
                ],
            )
            correction_path = root / "flow-correction.json"
            correction_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": "txn_confirmed_income",
                            "flow_type": "income",
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
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["txn_confirmed_income"]["flow_type"], "income")
            self.assertEqual(rows["txn_confirmed_income"]["flow_source"], "correction")
            self.assertEqual(
                rows["txn_confirmed_income"]["reconciliation_status"],
                "not_applicable",
            )
            rerun = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            rows = {row["transaction_id"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["txn_confirmed_income"]["flow_type"], "income")
            self.assertEqual(rows["txn_equal_outflow"]["flow_type"], "unresolved")

    def test_pending_exposes_suggested_flow_without_freezing_it_as_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_pending",
                        "date": "2026-06-01",
                        "account_id": "bank_primary",
                        "account_type": "bank",
                        "amount_hkd": "90.00",
                        "category": "Other",
                        "needs_review": "true",
                    }
                ],
            )

            reconcile = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
            result = self._run_cli(
                ["pending", "--month", "2026-06", "--json"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            [row] = json.loads(result.stdout)["data"]["transactions"]
            self.assertEqual(row["suggested_flow_type"], "unresolved")
            self.assertEqual(row["flow_type"], "")

    def test_balance_reconciliation_reports_result_or_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_balance_one",
                        "date": "2026-06-01",
                        "account_id": "bank_balanced",
                        "account_type": "bank",
                        "posted_amount": "10.00",
                        "amount_hkd": "10.00",
                        "statement_opening_balance": "100.00",
                        "source_file": "synthetic.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_balance_two",
                        "date": "2026-06-02",
                        "account_id": "bank_balanced",
                        "account_type": "bank",
                        "posted_amount": "20.00",
                        "amount_hkd": "20.00",
                        "statement_closing_balance": "130.00",
                        "source_file": "synthetic.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_no_balances",
                        "date": "2026-06-03",
                        "account_id": "bank_unavailable",
                        "account_type": "bank",
                        "posted_amount": "5.00",
                        "amount_hkd": "5.00",
                        "source_file": "synthetic.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_balance_difference",
                        "date": "2026-06-04",
                        "account_id": "bank_difference",
                        "account_type": "bank",
                        "posted_amount": "5.00",
                        "amount_hkd": "5.00",
                        "statement_opening_balance": "50.00",
                        "statement_closing_balance": "60.00",
                        "source_file": "synthetic.csv",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--dry-run", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            balances = json.loads(result.stdout)["data"]["balance_reconciliation"]
            self.assertEqual(balances["bank_balanced"]["status"], "reconciled")
            self.assertEqual(
                balances["bank_balanced"]["statements"][0]["difference"], "0.00"
            )
            self.assertEqual(balances["bank_unavailable"]["status"], "unavailable")
            self.assertEqual(balances["bank_difference"]["status"], "difference")
            self.assertEqual(
                balances["bank_difference"]["statements"][0]["difference"], "5.00"
            )


if __name__ == "__main__":
    unittest.main()
