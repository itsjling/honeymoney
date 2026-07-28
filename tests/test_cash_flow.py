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
from honeymoney.manual_pairs import ManualPairError, validate_manual_pair_facts
from honeymoney.reconciliation import reconcile_ledger, transaction_direction

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

    def test_missing_base_conversion_gives_direction_without_cross_currency_pairing(
        self,
    ) -> None:
        rows = [
            {
                "transaction_id": "txn_synthetic_out",
                "date": "2026-07-01",
                "account_id": "synthetic_one",
                "account_type": "bank",
                "posted_amount": "-10.00",
                "posted_currency": "USD",
                "amount_hkd": "",
                "category": "Other",
            },
            {
                "transaction_id": "txn_synthetic_in",
                "date": "2026-07-01",
                "account_id": "synthetic_two",
                "account_type": "bank",
                "posted_amount": "10.00",
                "posted_currency": "EUR",
                "amount_hkd": "",
                "category": "Other",
            },
        ]

        self.assertEqual(transaction_direction(rows[0]), "outflow")
        self.assertEqual(transaction_direction(rows[1]), "inflow")
        summary = reconcile_ledger(rows, {"reconciliation": {"date_window_days": 3}})

        self.assertEqual(summary["paired_groups"], 0)
        self.assertEqual({row.get("paired_transaction_id", "") for row in rows}, {""})
        self.assertTrue(all(row["flow_type"] == "unresolved" for row in rows))

    def test_report_warns_when_base_currency_rows_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_synthetic_out",
                        "date": "2026-07-01",
                        "account_id": "synthetic_one",
                        "account_type": "bank",
                        "posted_amount": "-10.00",
                        "posted_currency": "USD",
                        "amount_hkd": "",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_synthetic_in",
                        "date": "2026-07-02",
                        "account_id": "synthetic_two",
                        "account_type": "bank",
                        "posted_amount": "10.00",
                        "posted_currency": "EUR",
                        "amount_hkd": "",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(
                ["report", "--month", "2026-07", "--no-open", "--json"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)["data"]["missing_base_currency_count"], 2
            )
            report = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn(
                "2 rows have no HKD valuation.",
                report,
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
            self.assertEqual(rows["txn_out"]["needs_review"], "false")
            self.assertEqual(rows["txn_out"]["flags"], "")
            self.assertEqual(rows["txn_out"]["reason"], "")
            self.assertEqual(rows["txn_unique_in"]["needs_review"], "false")
            self.assertEqual(rows["txn_excluded_in"]["needs_review"], "false")
            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))
            self.assertEqual(review_rows, [])

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
                        "posted_currency": "HKD",
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
                        "posted_currency": "HKD",
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
                        "posted_currency": "HKD",
                        "amount_hkd": "5.00",
                        "source_file": "synthetic.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_opening_only",
                        "date": "2026-06-03",
                        "account_id": "bank_opening_only",
                        "account_type": "bank",
                        "posted_amount": "7.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "7.00",
                        "statement_opening_balance": "1234.56",
                        "source_file": "opening-only.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_closing_only",
                        "date": "2026-06-03",
                        "account_id": "bank_closing_only",
                        "account_type": "bank",
                        "posted_amount": "8.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "8.00",
                        "statement_closing_balance": "6543.21",
                        "source_file": "closing-only.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_balance_difference",
                        "date": "2026-06-04",
                        "account_id": "bank_difference",
                        "account_type": "bank",
                        "posted_amount": "5.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "5.00",
                        "statement_opening_balance": "50.00",
                        "statement_closing_balance": "60.00",
                        "source_file": "synthetic.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_partial_matched",
                        "date": "2026-06-05",
                        "account_id": "bank_partial",
                        "account_type": "bank",
                        "posted_amount": "5.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "5.00",
                        "statement_opening_balance": "50.00",
                        "statement_closing_balance": "55.00",
                        "source_file": "synthetic-a.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_partial_unavailable",
                        "date": "2026-06-06",
                        "account_id": "bank_partial",
                        "account_type": "bank",
                        "posted_amount": "5.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "5.00",
                        "source_file": "synthetic-b.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_balance_conflict_one",
                        "date": "2026-06-07",
                        "account_id": "bank_conflict",
                        "account_type": "bank",
                        "posted_amount": "1.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "1.00",
                        "statement_opening_balance": "7777.77",
                        "statement_closing_balance": "9999.99",
                        "source_file": "conflict.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_balance_conflict_two",
                        "date": "2026-06-08",
                        "account_id": "bank_conflict",
                        "account_type": "bank",
                        "posted_amount": "2.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "2.00",
                        "statement_opening_balance": "8888.88",
                        "source_file": "conflict.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_absent_opening_invalid_closing",
                        "date": "2026-06-09",
                        "account_id": "bank_no_safe_endpoints_a",
                        "account_type": "bank",
                        "posted_amount": "1.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "1.00",
                        "statement_closing_balance": "not-a-balance",
                        "source_file": "invalid-closing.csv",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_invalid_opening_absent_closing",
                        "date": "2026-06-10",
                        "account_id": "bank_no_safe_endpoints_b",
                        "account_type": "bank",
                        "posted_amount": "1.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "1.00",
                        "statement_opening_balance": "not-a-balance",
                        "source_file": "invalid-opening.csv",
                        "category": "Other",
                    },
                ],
            )

            result = self._run_cli(["reconcile", "--dry-run", "--json"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            balances = json.loads(result.stdout)["data"]["balance_reconciliation"]
            self.assertEqual(balances["bank_balanced"]["status"], "reconciled")
            self.assertEqual(balances["bank_balanced"]["result"], "matched")
            self.assertEqual(
                balances["bank_balanced"]["statements"][0]["difference"], "0.00"
            )
            self.assertEqual(
                balances["bank_balanced"]["statements"][0]["result"], "matched"
            )
            self.assertTrue(
                balances["bank_balanced"]["statements"][0]["opening_evidence_found"]
            )
            self.assertTrue(
                balances["bank_balanced"]["statements"][0]["closing_evidence_found"]
            )
            self.assertEqual(balances["bank_unavailable"]["status"], "unavailable")
            unavailable = balances["bank_unavailable"]["statements"][0]
            self.assertEqual(unavailable["result"], "missing_both")
            self.assertFalse(unavailable["opening_evidence_found"])
            self.assertFalse(unavailable["closing_evidence_found"])
            self.assertNotIn("opening_balance", unavailable)
            self.assertNotIn("closing_balance", unavailable)
            self.assertNotIn("calculated_closing_balance", unavailable)
            self.assertNotIn("difference", unavailable)
            self.assertEqual(
                unavailable["reason"],
                "Opening and closing balances are unavailable.",
            )
            opening_only = balances["bank_opening_only"]["statements"][0]
            self.assertEqual(opening_only["status"], "unavailable")
            self.assertEqual(opening_only["result"], "missing_closing")
            self.assertTrue(opening_only["opening_evidence_found"])
            self.assertFalse(opening_only["closing_evidence_found"])
            self.assertNotIn("opening_balance", opening_only)
            self.assertNotIn("difference", opening_only)
            closing_only = balances["bank_closing_only"]["statements"][0]
            self.assertEqual(closing_only["status"], "unavailable")
            self.assertEqual(closing_only["result"], "missing_opening")
            self.assertFalse(closing_only["opening_evidence_found"])
            self.assertTrue(closing_only["closing_evidence_found"])
            self.assertNotIn("closing_balance", closing_only)
            self.assertNotIn("difference", closing_only)
            self.assertEqual(balances["bank_difference"]["status"], "difference")
            self.assertEqual(balances["bank_difference"]["result"], "mismatched")
            self.assertEqual(
                balances["bank_difference"]["statements"][0]["difference"], "5.00"
            )
            self.assertEqual(balances["bank_partial"]["status"], "unavailable")
            self.assertEqual(
                balances["bank_partial"]["reason"],
                "One or more statement balance checks are unavailable.",
            )
            conflict = balances["bank_conflict"]["statements"][0]
            self.assertEqual(conflict["status"], "unavailable")
            self.assertEqual(conflict["result"], "conflicting_evidence")
            self.assertFalse(conflict["opening_evidence_found"])
            self.assertTrue(conflict["closing_evidence_found"])
            self.assertNotIn("opening_balance", conflict)
            self.assertNotIn("closing_balance", conflict)
            self.assertNotIn("difference", conflict)
            for account_id in (
                "bank_no_safe_endpoints_a",
                "bank_no_safe_endpoints_b",
            ):
                no_safe_endpoints = balances[account_id]["statements"][0]
                self.assertEqual(no_safe_endpoints["result"], "missing_both")
                self.assertFalse(no_safe_endpoints["opening_evidence_found"])
                self.assertFalse(no_safe_endpoints["closing_evidence_found"])
                self.assertNotIn("opening_balance", no_safe_endpoints)
                self.assertNotIn("closing_balance", no_safe_endpoints)
                self.assertNotIn("difference", no_safe_endpoints)

            human = self._run_cli(["reconcile", "--dry-run"], cwd=root)
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn(
                "result=missing_opening opening=missing closing=found",
                human.stdout,
            )
            self.assertIn(
                "result=missing_closing opening=found closing=missing",
                human.stdout,
            )
            self.assertIn(
                "result=missing_both opening=missing closing=missing",
                human.stdout,
            )
            self.assertIn("result=conflicting_evidence", human.stdout)
            self.assertIn("result=matched", human.stdout)
            self.assertIn("result=mismatched", human.stdout)
            self.assertNotIn("1234.56", human.stdout)
            self.assertNotIn("6543.21", human.stdout)
            self.assertNotIn("7777.77", human.stdout)
            self.assertNotIn("8888.88", human.stdout)
            self.assertNotIn("9999.99", human.stdout)

            report = self._run_cli(
                ["report", "--month", "2026-06", "--no-open", "--json"],
                cwd=root,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            report_data = json.loads(report.stdout)["data"]
            self.assertIn("balance_reconciliation", report_data)
            report_html = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn("Statement balance coverage", report_html)
            self.assertIn("missing_opening", report_html)
            self.assertIn("missing_closing", report_html)
            self.assertIn("missing_both", report_html)
            self.assertIn("conflicting_evidence", report_html)
            self.assertIn("matched", report_html)
            self.assertIn("mismatched", report_html)
            self.assertNotIn("1234.56", report_html)
            self.assertNotIn("6543.21", report_html)
            self.assertNotIn("7777.77", report_html)
            self.assertNotIn("8888.88", report_html)
            self.assertNotIn("9999.99", report_html)

    def test_balance_reconciliation_reports_unavailable_calculation_inputs(
        self,
    ) -> None:
        rows = [
            {
                "account_id": "bank_missing_posted_currency",
                "account_type": "bank",
                "posted_amount": "10.00",
                "posted_currency": "",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "110.00",
                "source_file": "missing-currency.csv",
            },
            {
                "account_id": "bank_invalid_posted_activity",
                "account_type": "bank",
                "posted_amount": "not-an-amount",
                "posted_currency": "HKD",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "110.00",
                "source_file": "invalid-activity.csv",
            },
        ]

        balances = reconcile_ledger(rows, {})["balance_reconciliation"]

        unavailable_inputs = {
            "bank_missing_posted_currency": "Posted currency is unavailable.",
            "bank_invalid_posted_activity": (
                "One or more posted amounts are unavailable."
            ),
        }
        for account_id, reason in unavailable_inputs.items():
            calculation_unavailable = balances[account_id]["statements"][0]
            self.assertEqual(calculation_unavailable["status"], "unavailable")
            self.assertEqual(calculation_unavailable["result"], "unavailable")
            self.assertTrue(calculation_unavailable["opening_evidence_found"])
            self.assertTrue(calculation_unavailable["closing_evidence_found"])
            self.assertEqual(calculation_unavailable["reason"], reason)
            self.assertNotIn("opening_balance", calculation_unavailable)
            self.assertNotIn("closing_balance", calculation_unavailable)
            self.assertNotIn(
                "calculated_closing_balance",
                calculation_unavailable,
            )
            self.assertNotIn("difference", calculation_unavailable)

    def test_period_report_reconciles_the_complete_represented_statement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(
                tmp,
                [
                    {
                        "transaction_id": "txn_before_period",
                        "date": "2026-05-31",
                        "account_id": "bank_boundary",
                        "account_type": "bank",
                        "posted_amount": "10.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "10.00",
                        "statement_opening_balance": "100.00",
                        "source_file": "boundary.pdf",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_in_period_one",
                        "date": "2026-06-01",
                        "account_id": "bank_boundary",
                        "account_type": "bank",
                        "posted_amount": "20.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "20.00",
                        "source_file": "boundary.pdf",
                        "category": "Other",
                    },
                    {
                        "transaction_id": "txn_in_period_two",
                        "date": "2026-06-02",
                        "account_id": "bank_boundary",
                        "account_type": "bank",
                        "posted_amount": "30.00",
                        "posted_currency": "HKD",
                        "amount_hkd": "30.00",
                        "statement_closing_balance": "160.00",
                        "source_file": "boundary.pdf",
                        "category": "Other",
                    },
                ],
            )

            report = self._run_cli(
                ["report", "--month", "2026-06", "--no-open", "--json"],
                cwd=root,
            )

            self.assertEqual(report.returncode, 0, report.stderr)
            data = json.loads(report.stdout)["data"]
            self.assertEqual(data["transaction_count"], 2)
            balance = data["balance_reconciliation"]["bank_boundary"]
            self.assertEqual(balance["result"], "matched")
            [statement] = balance["statements"]
            self.assertEqual(statement["result"], "matched")
            self.assertTrue(statement["opening_evidence_found"])
            self.assertTrue(statement["closing_evidence_found"])
            html = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn("matched", html)
            self.assertNotIn("missing_opening", html)

    def test_balance_reconciliation_separates_source_identity_and_currency(
        self,
    ) -> None:
        rows = [
            {
                "account_id": "multi",
                "source_id": "source_a",
                "source_file": "bank-a/june.pdf",
                "posted_currency": "HKD",
                "posted_amount": "10.00",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "110.00",
            },
            {
                "account_id": "multi",
                "source_id": "source_a",
                "source_file": "bank-a/june.pdf",
                "posted_currency": "USD",
                "posted_amount": "-5.00",
                "statement_opening_balance": "50.00",
                "statement_closing_balance": "45.00",
            },
            {
                "account_id": "multi",
                "source_id": "source_b",
                "source_file": "bank-b/june.pdf",
                "posted_currency": "HKD",
                "posted_amount": "1.00",
            },
        ]

        result = reconcile_ledger(rows, {})["balance_reconciliation"]["multi"]

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["result"], "unavailable")
        self.assertEqual(
            result["reason"],
            "One or more statement balance checks are unavailable.",
        )
        self.assertEqual(len(result["statements"]), 3)
        self.assertEqual(
            [statement["posted_currency"] for statement in result["statements"]],
            ["HKD", "USD", "HKD"],
        )
        self.assertEqual(
            [statement["status"] for statement in result["statements"]],
            ["reconciled", "reconciled", "unavailable"],
        )
        self.assertEqual(
            [statement["result"] for statement in result["statements"]],
            ["matched", "matched", "missing_both"],
        )
        self.assertEqual(
            [statement["source_file"] for statement in result["statements"]],
            ["bank-a/june.pdf", "bank-a/june.pdf", "bank-b/june.pdf"],
        )
        self.assertEqual(
            [
                (
                    statement["opening_evidence_found"],
                    statement["closing_evidence_found"],
                )
                for statement in result["statements"]
            ],
            [(True, True), (True, True), (False, False)],
        )

    def test_balance_reconciliation_separates_sections_and_statements(
        self,
    ) -> None:
        rows = [
            {
                "account_id": "shared",
                "source_id": source_id,
                "source_file": source_file,
                "statement_section": section,
                "posted_currency": "HKD",
                "posted_amount": "10.00",
                "statement_opening_balance": opening,
                "statement_closing_balance": closing,
            }
            for source_id, source_file, section, opening, closing in (
                (
                    "source_a",
                    "april.pdf",
                    "HKD Savings",
                    "100.00",
                    "110.00",
                ),
                (
                    "source_a",
                    "april.pdf",
                    "HKD Current",
                    "200.00",
                    "210.00",
                ),
                (
                    "source_b",
                    "may.pdf",
                    "HKD Savings",
                    "110.00",
                    "120.00",
                ),
            )
        ]

        result = reconcile_ledger(rows, {})["balance_reconciliation"]["shared"]

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["result"], "matched")
        self.assertEqual(len(result["statements"]), 3)
        self.assertEqual(
            {
                (statement["source_file"], statement["statement_section"])
                for statement in result["statements"]
            },
            {
                ("april.pdf", "HKD Savings"),
                ("april.pdf", "HKD Current"),
                ("may.pdf", "HKD Savings"),
            },
        )
        self.assertEqual(
            {statement["result"] for statement in result["statements"]},
            {"matched"},
        )

    def test_balance_reconciliation_reports_conflicting_values(self) -> None:
        rows = [
            {
                "account_id": "conflict",
                "source_file": "legacy.pdf",
                "posted_currency": "HKD",
                "posted_amount": "1.00",
                "statement_opening_balance": "10.00",
                "statement_closing_balance": "11.00",
            },
            {
                "account_id": "conflict",
                "source_file": "legacy.pdf",
                "posted_currency": "HKD",
                "posted_amount": "2.00",
                "statement_opening_balance": "12.00",
            },
        ]

        statement = reconcile_ledger(rows, {})["balance_reconciliation"]["conflict"][
            "statements"
        ][0]

        self.assertEqual(statement["status"], "unavailable")
        self.assertEqual(statement["result"], "conflicting_evidence")
        self.assertFalse(statement["opening_evidence_found"])
        self.assertTrue(statement["closing_evidence_found"])
        self.assertNotIn("opening_balance", statement)
        self.assertNotIn("closing_balance", statement)
        self.assertNotIn("difference", statement)
        self.assertEqual(statement["reason"], "Opening balances conflict.")

    def test_missing_balance_evidence_does_not_add_source_data_review(self) -> None:
        rows = [
            {
                "transaction_id": "txn_missing_balance_evidence",
                "account_id": "missing",
                "source_file": "missing.csv",
                "posted_currency": "HKD",
                "posted_amount": "1.00",
                "flags": "",
                "review_reasons": "",
                "needs_review": "false",
            }
        ]

        reconcile_ledger(rows, {})

        self.assertEqual(rows[0]["flags"], "")
        self.assertEqual(rows[0]["review_reasons"], "")
        self.assertEqual(rows[0]["needs_review"], "false")

    def test_manual_pair_facts_reject_account_amount_currency_and_owner_conflicts(
        self,
    ) -> None:
        left = {
            "transaction_id": "txn_left",
            "account_id": "cash_account",
            "posted_amount": "-100.00",
            "posted_currency": "HKD",
            "owner": "Justin",
        }
        right = {
            "transaction_id": "txn_right",
            "account_id": "cash_account",
            "posted_amount": "100.00",
            "posted_currency": "HKD",
            "owner": "Justin",
        }
        cases = (
            ("account_id", "other_account", "manual_pair_account_mismatch"),
            ("posted_amount", "99.00", "manual_pair_amount_mismatch"),
            ("posted_currency", "USD", "manual_pair_currency_mismatch"),
            ("owner", "Franchesca", "manual_pair_owner_mismatch"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                candidate = {**right, field: value}
                with self.assertRaises(ManualPairError) as raised:
                    validate_manual_pair_facts(left, candidate)
                self.assertEqual(raised.exception.code, code)

    def test_manual_pair_members_are_not_reused_for_currency_exchange(self) -> None:
        pair_id = "mpair_" + "a" * 32
        rows = [
            {
                "transaction_id": "txn_manual_out",
                "date": "2026-07-01",
                "account_id": "cash_account",
                "account_type": "bank",
                "institution": "Synthetic Bank",
                "posted_amount": "-780.00",
                "posted_currency": "HKD",
                "amount_hkd": "-780.00",
                "merchant": "SYNTHETIC EXCHANGE",
                "owner": "Household",
                "flags": f"manual_transfer_pair:{pair_id}",
            },
            {
                "transaction_id": "txn_manual_in",
                "date": "2026-07-01",
                "account_id": "cash_account",
                "account_type": "bank",
                "institution": "Synthetic Bank",
                "posted_amount": "780.00",
                "posted_currency": "HKD",
                "amount_hkd": "780.00",
                "merchant": "SYNTHETIC REDEPOSIT",
                "owner": "Household",
                "flags": f"manual_transfer_pair:{pair_id}",
            },
            {
                "transaction_id": "txn_foreign",
                "date": "2026-07-01",
                "account_id": "foreign_account",
                "account_type": "bank",
                "institution": "Synthetic Bank",
                "posted_amount": "100.00",
                "posted_currency": "JPY",
                "amount_hkd": "",
                "merchant": "SYNTHETIC DEPOSIT",
                "owner": "Household",
                "flags": "",
            },
        ]

        summary = reconcile_ledger(
            rows,
            {
                "base_currency": "HKD",
                "exchange_rates": {"JPY": 7.8},
            },
        )

        self.assertEqual(summary["paired_groups"], 1)
        self.assertEqual(summary["cross_currency_paired_groups"], 0)
        self.assertEqual(rows[0]["paired_transaction_id"], "txn_manual_in")
        self.assertEqual(rows[1]["paired_transaction_id"], "txn_manual_out")
        self.assertEqual(rows[2]["paired_transaction_id"], "")

    def test_invalid_manual_member_is_reserved_from_automatic_matching(self) -> None:
        pair_id = "mpair_" + "b" * 32
        rows = [
            {
                "transaction_id": "txn_invalid_manual",
                "date": "2026-07-01",
                "account_id": "cash_account",
                "account_type": "bank",
                "institution": "Synthetic Bank",
                "posted_amount": "-780.00",
                "posted_currency": "HKD",
                "amount_hkd": "-780.00",
                "merchant": "SYNTHETIC EXCHANGE",
                "owner": "Household",
                "flags": f"manual_transfer_pair:{pair_id}",
            },
            {
                "transaction_id": "txn_foreign",
                "date": "2026-07-01",
                "account_id": "foreign_account",
                "account_type": "bank",
                "institution": "Synthetic Bank",
                "posted_amount": "100.00",
                "posted_currency": "JPY",
                "amount_hkd": "",
                "merchant": "SYNTHETIC DEPOSIT",
                "owner": "Household",
                "flags": "",
            },
        ]

        summary = reconcile_ledger(
            rows,
            {
                "base_currency": "HKD",
                "exchange_rates": {"JPY": 7.8},
            },
        )

        self.assertEqual(summary["paired_groups"], 0)
        self.assertEqual(summary["cross_currency_paired_groups"], 0)
        self.assertEqual(summary["unmatched_transactions"], 1)
        self.assertEqual(rows[0]["paired_transaction_id"], "")
        self.assertEqual(rows[0]["flow_type"], "unresolved")
        self.assertIn("manual_transfer_pair_invalid", rows[0]["flags"])


if __name__ == "__main__":
    unittest.main()
