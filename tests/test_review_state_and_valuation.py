import csv
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from honeymoney.classification_policy import apply_structural_classification
from honeymoney.cli import _accounting_decision_patch
from honeymoney.corrections import (
    apply_corrections,
    load_corrections,
    review_state_correction_updates,
    to_review_row,
)
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.report import build_report_html
from honeymoney.review_state import (
    REVIEW_REASON_ACCOUNTING_FLOW,
    REVIEW_REASON_CATEGORY,
    REVIEW_REASON_IDENTITY,
    review_reason_tokens,
    synchronize_review_state,
)
from honeymoney.rules import apply_rules
from honeymoney.valuation import value_transaction


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "transaction_id": "txn_synthetic",
        "date": "2026-07-01",
        "account_id": "owned_account",
        "account_type": "bank",
        "institution": "Synthetic Bank",
        "original_amount": "-10.00",
        "original_currency": "HKD",
        "posted_amount": "-10.00",
        "posted_currency": "HKD",
        "amount_hkd": "-10.00",
        "valuation_source": "",
        "valuation_status": "",
        "merchant": "Synthetic Merchant",
        "original_description": "SYNTHETIC PURCHASE",
        "category": "Unknown",
        "flow_type": "unresolved",
        "flow_source": "deterministic",
        "owner": "Household",
        "confidence": "0.00",
        "needs_review": "true",
        "review_reasons": (f"{REVIEW_REASON_CATEGORY};{REVIEW_REASON_ACCOUNTING_FLOW}"),
        "reason": "Synthetic provenance",
        "flags": "uncategorized",
    }
    row.update(overrides)
    return row


class ReviewStateTest(unittest.TestCase):
    def test_stale_saved_category_correction_migrates_to_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            correction_path = Path(tmp) / "corrections.csv"
            with correction_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "transaction_id",
                        "category",
                        "flow_type",
                        "owner",
                        "payment_method",
                        "confidence",
                        "reason",
                        "notes",
                        "needs_review",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "transaction_id": "txn_saved",
                        "category": "Dining",
                        "confidence": "1.00",
                        "reason": "Reviewed choice",
                        "needs_review": "true",
                    }
                )

            corrections = load_corrections({"corrections": str(correction_path)})
            transaction = _row(
                transaction_id="txn_saved",
                flow_type="expense",
            )
            apply_corrections([transaction], corrections)

            self.assertEqual(corrections["txn_saved"]["needs_review"], "true")
            self.assertEqual(transaction["needs_review"], "false")
            self.assertEqual(transaction["review_reasons"], "")
            self.assertEqual(
                review_state_correction_updates(corrections, [transaction]),
                {
                    "txn_saved": {
                        "needs_review": "false",
                        "review_reasons": "",
                    }
                },
            )

    def test_full_confidence_rule_and_human_choice_clear_category_review(self) -> None:
        transaction = _row()
        apply_rules(
            [transaction],
            [
                {
                    "id": "synthetic-rule",
                    "enabled": True,
                    "category": "Dining",
                    "confidence": 1.0,
                    "patterns": ["Synthetic"],
                }
            ],
            {"review_confidence_threshold": 0.8},
        )
        reconcile_ledger([transaction], {"base_currency": "HKD"})

        self.assertEqual(transaction["needs_review"], "false")
        self.assertEqual(transaction["review_reasons"], "")
        self.assertNotIn("uncategorized", transaction["flags"].split(";"))

        conflicted = _row(
            transaction_id="txn_conflicted",
            flags="uncategorized;duplicate_suspected",
        )
        apply_corrections(
            [conflicted],
            {
                "txn_conflicted": {
                    "category": "Dining",
                    "flow_type": "expense",
                    "needs_review": "false",
                }
            },
        )
        synchronize_review_state(conflicted)

        self.assertEqual(conflicted["needs_review"], "true")
        self.assertEqual(
            review_reason_tokens(conflicted["review_reasons"]),
            [REVIEW_REASON_IDENTITY],
        )

    def test_multiple_current_reasons_are_explicit_and_stale_flags_clear(self) -> None:
        pending = _row(flags="uncategorized;duplicate_suspected")
        synchronize_review_state(pending)
        self.assertEqual(
            set(review_reason_tokens(pending["review_reasons"])),
            {
                REVIEW_REASON_CATEGORY,
                REVIEW_REASON_ACCOUNTING_FLOW,
                REVIEW_REASON_IDENTITY,
            },
        )

        stale = _row(
            category="Dining",
            flow_type="expense",
            flow_source="correction",
            flags="uncategorized;manual_correction",
            review_reasons="",
        )
        synchronize_review_state(stale, legacy=True)
        self.assertEqual(stale["needs_review"], "false")
        self.assertEqual(stale["review_reasons"], "")
        self.assertNotIn("uncategorized", stale["flags"].split(";"))

        inconsistent = _row(
            category="Dining",
            flow_type="expense",
            needs_review="false",
            review_reasons=REVIEW_REASON_IDENTITY,
            flags="duplicate_suspected",
        )
        synchronize_review_state(inconsistent)
        self.assertEqual(inconsistent["needs_review"], "true")
        self.assertEqual(inconsistent["review_reasons"], REVIEW_REASON_IDENTITY)

        old_reviewable_model = _row(
            category="Dining",
            flow_type="expense",
            flags="ollama_categorized",
            review_reasons="",
        )
        synchronize_review_state(old_reviewable_model, legacy=True)
        self.assertEqual(old_reviewable_model["review_reasons"], "category_suggestion")

        old_accepted_model = deepcopy(old_reviewable_model)
        old_accepted_model["needs_review"] = "false"
        old_accepted_model["review_reasons"] = ""
        synchronize_review_state(old_accepted_model, legacy=True)
        self.assertEqual(old_accepted_model["review_reasons"], "")

        with self.assertRaisesRegex(ValueError, "Unsupported review reasons"):
            synchronize_review_state(
                _row(review_reasons="future_reason", needs_review="true")
            )

    def test_accounting_decision_keeps_an_unrelated_human_choice(self) -> None:
        transaction = _row(
            category="Dining",
            flow_type="unresolved",
            review_reasons="accounting_flow;other_decision",
        )

        patch = _accounting_decision_patch(
            transaction,
            "expense",
            "Synthetic accounting decision",
        )

        self.assertEqual(patch["review_reasons"], "other_decision")
        self.assertEqual(patch["needs_review"], "true")
        review_row = to_review_row({**transaction, **patch})
        self.assertEqual(
            review_row["review_reason_labels"],
            "Make another recorded decision",
        )


class ValuationAndCrossCurrencyTest(unittest.TestCase):
    def test_statement_posted_dated_fixed_and_missing_valuations_are_distinct(
        self,
    ) -> None:
        statement = _row(
            original_amount="-10.00",
            original_currency="USD",
            posted_amount="-78.50",
            posted_currency="HKD",
        )
        dated = _row(
            posted_amount="-10.00",
            posted_currency="EUR",
            date="2026-07-02",
        )
        fixed = _row(posted_amount="-10.00", posted_currency="USD")
        missing = _row(
            posted_amount="-10.00",
            posted_currency="JPY",
            category="Travel",
            flow_type="expense",
            needs_review="false",
            review_reasons="",
            flags="",
        )
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"EUR": 9.0, "USD": 7.8},
            "dated_exchange_rates": {"EUR": {"2026-07-02": 8.9}},
        }
        for row in (statement, dated, fixed, missing):
            value_transaction(row, config)

        self.assertEqual(
            (statement["amount_hkd"], statement["valuation_source"]),
            ("-78.50", "statement_posted"),
        )
        self.assertEqual(
            (dated["amount_hkd"], dated["valuation_source"], dated["valuation_status"]),
            ("-89.00", "configured_dated_rate", "estimated"),
        )
        self.assertEqual(
            (fixed["amount_hkd"], fixed["valuation_source"], fixed["valuation_status"]),
            ("-78.00", "configured_fixed_rate", "estimated"),
        )
        fixed["amount_hkd"] = "-79.00"
        fixed["valuation_source"] = "matched_exchange_leg"
        fixed["valuation_status"] = "actual"
        value_transaction(fixed, config)
        self.assertEqual(
            (fixed["amount_hkd"], fixed["valuation_source"], fixed["valuation_status"]),
            ("-79.00", "matched_exchange_leg", "actual"),
        )
        value_transaction(fixed, config, preserve_matched=False)
        self.assertEqual(
            (fixed["amount_hkd"], fixed["valuation_source"], fixed["valuation_status"]),
            ("-78.00", "configured_fixed_rate", "estimated"),
        )
        self.assertEqual(
            (
                missing["amount_hkd"],
                missing["valuation_source"],
                missing["valuation_status"],
            ),
            ("", "missing", "missing"),
        )
        synchronize_review_state(missing)
        self.assertEqual(missing["needs_review"], "false")

    def test_separate_exchange_legs_pair_value_and_do_not_count_as_spending(
        self,
    ) -> None:
        base = _row(
            transaction_id="txn_exchange_debit",
            account_id="owned_hkd",
            posted_amount="-850.00",
            posted_currency="HKD",
            amount_hkd="-850.00",
            category="Household",
            flow_type="expense",
            flow_source="correction",
            original_description="CURRENCY EXCHANGE DEBIT",
            flags="manual_correction",
            needs_review="false",
            review_reasons="",
        )
        foreign = _row(
            transaction_id="txn_foreign_deposit",
            account_id="owned_foreign",
            posted_amount="100.00",
            posted_currency="EUR",
            original_amount="100.00",
            original_currency="EUR",
            amount_hkd="",
            category="Savings",
            flow_type="investment_transfer",
            flow_source="correction",
            original_description="FOREIGN CURRENCY DEPOSIT",
            flags="manual_correction;missing_exchange_rate",
            needs_review="false",
            review_reasons="",
        )
        rows = [base, foreign]
        original_ids = [row["transaction_id"] for row in rows]

        config = {
            "base_currency": "HKD",
            "exchange_rates": {"EUR": 8.5},
        }
        first = reconcile_ledger(rows, config)
        first_rows = deepcopy(rows)
        second = reconcile_ledger(rows, config)

        self.assertEqual(first["cross_currency_paired_groups"], 1)
        self.assertEqual(first["matched_exchange_valuations"], 1)
        self.assertEqual(second["cross_currency_paired_groups"], 1)
        self.assertEqual(rows, first_rows)
        self.assertEqual([row["transaction_id"] for row in rows], original_ids)
        self.assertEqual({row["flow_type"] for row in rows}, {"internal_transfer"})
        self.assertEqual({row["category"] for row in rows}, {"Internal Transfer"})
        self.assertEqual(foreign["amount_hkd"], "850.00")
        self.assertEqual(foreign["valuation_source"], "matched_exchange_leg")
        self.assertEqual(foreign["valuation_status"], "actual")
        self.assertEqual({row["needs_review"] for row in rows}, {"false"})

        report = build_report_html(rows, "Synthetic period")
        self.assertIn('id="tile-spending">0.00<', report)
        self.assertIn('"valuation_source": "matched_exchange_leg"', report)

    def test_foreign_spend_refund_and_unmatched_deposit_keep_their_roles(self) -> None:
        spend = _row(
            transaction_id="txn_foreign_spend",
            posted_amount="-10.00",
            posted_currency="USD",
            original_amount="-10.00",
            original_currency="USD",
            category="Travel",
            needs_review="false",
            review_reasons="",
            flags="",
        )
        refund = _row(
            transaction_id="txn_foreign_refund",
            account_type="bank",
            posted_amount="5.00",
            posted_currency="USD",
            original_amount="5.00",
            original_currency="USD",
            original_description="SYNTHETIC MERCHANT REFUND",
            category="Travel",
            needs_review="false",
            review_reasons="",
            flags="",
        )
        rebate = _row(
            transaction_id="txn_foreign_rebate",
            posted_amount="1.00",
            posted_currency="EUR",
            amount_hkd="",
            original_amount="1.00",
            original_currency="EUR",
            original_description="SYNTHETIC CASH REBATE",
        )
        unmatched = _row(
            transaction_id="txn_unmatched_deposit",
            account_id="owned_foreign",
            posted_amount="100.00",
            posted_currency="EUR",
            original_amount="100.00",
            original_currency="EUR",
            original_description="FOREIGN CURRENCY DEPOSIT",
            category="Savings",
            needs_review="false",
            review_reasons="",
            flags="",
        )
        apply_structural_classification([rebate], {})
        rows = [spend, refund, rebate, unmatched]

        summary = reconcile_ledger(
            rows,
            {
                "base_currency": "HKD",
                "exchange_rates": {"USD": 7.8},
            },
        )

        self.assertEqual(spend["flow_type"], "expense")
        self.assertEqual(refund["flow_type"], "refund")
        self.assertEqual(rebate["flow_type"], "refund")
        self.assertEqual(rebate["valuation_status"], "missing")
        self.assertEqual(unmatched["reconciliation_status"], "unmatched")
        self.assertEqual(unmatched["valuation_status"], "missing")
        self.assertEqual(summary["cross_currency_paired_groups"], 0)

    def test_ambiguous_cross_currency_assignments_do_not_pair(self) -> None:
        rows = [
            _row(
                transaction_id=f"txn_base_{index}",
                account_id=f"base_{index}",
                posted_amount="-850.00",
                posted_currency="HKD",
                amount_hkd="-850.00",
                original_description="CURRENCY EXCHANGE DEBIT",
                category="Household",
                flow_type="expense",
                review_reasons="",
                needs_review="false",
                flags="",
            )
            for index in range(2)
        ]
        rows.extend(
            _row(
                transaction_id=f"txn_foreign_{index}",
                account_id=f"foreign_{index}",
                posted_amount="100.00",
                posted_currency="EUR",
                amount_hkd="",
                original_description="FOREIGN CURRENCY DEPOSIT",
                category="Savings",
                flow_type="investment_transfer",
                review_reasons="",
                needs_review="false",
                flags="",
            )
            for index in range(2)
        )

        summary = reconcile_ledger(
            rows,
            {
                "base_currency": "HKD",
                "exchange_rates": {"EUR": 8.5},
            },
        )

        self.assertEqual(summary["cross_currency_paired_groups"], 0)
        self.assertTrue(
            all("cross_currency_exchange" not in row["flags"] for row in rows)
        )

    def test_single_exchange_pair_must_fit_a_configured_rate(self) -> None:
        base = _row(
            transaction_id="txn_implausible_base",
            account_id="owned_hkd",
            posted_amount="-8500.00",
            posted_currency="HKD",
            amount_hkd="-8500.00",
            original_description="CURRENCY EXCHANGE DEBIT",
            category="Household",
            flow_type="expense",
            review_reasons="",
            needs_review="false",
            flags="",
        )
        foreign = _row(
            transaction_id="txn_implausible_foreign",
            account_id="owned_eur",
            posted_amount="100.00",
            posted_currency="EUR",
            amount_hkd="",
            original_description="FOREIGN CURRENCY DEPOSIT",
            category="Savings",
            flow_type="investment_transfer",
            review_reasons="",
            needs_review="false",
            flags="",
        )

        summary = reconcile_ledger(
            [base, foreign],
            {
                "base_currency": "HKD",
                "exchange_rates": {"EUR": 8.5},
            },
        )

        self.assertEqual(summary["cross_currency_paired_groups"], 0)
        self.assertEqual(base["flow_type"], "expense")
        self.assertNotEqual(foreign["valuation_source"], "matched_exchange_leg")

    def test_exchange_pair_requires_named_institution_and_accounts(self) -> None:
        for missing_field in ("institution", "account_id"):
            with self.subTest(missing_field=missing_field):
                base = _row(
                    transaction_id=f"txn_missing_{missing_field}_base",
                    account_id="owned_hkd",
                    posted_amount="-850.00",
                    posted_currency="HKD",
                    amount_hkd="-850.00",
                    original_description="CURRENCY EXCHANGE DEBIT",
                    category="Household",
                    flow_type="expense",
                    review_reasons="",
                    needs_review="false",
                    flags="",
                )
                foreign = _row(
                    transaction_id=f"txn_missing_{missing_field}_foreign",
                    account_id="owned_eur",
                    posted_amount="100.00",
                    posted_currency="EUR",
                    amount_hkd="",
                    original_description="FOREIGN CURRENCY DEPOSIT",
                    category="Savings",
                    flow_type="investment_transfer",
                    review_reasons="",
                    needs_review="false",
                    flags="",
                )
                base[missing_field] = ""
                foreign[missing_field] = ""

                summary = reconcile_ledger(
                    [base, foreign],
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"EUR": 8.5},
                    },
                )

                self.assertEqual(summary["cross_currency_paired_groups"], 0)

    def test_consistent_exchange_events_supply_a_rate_check(self) -> None:
        rows: list[dict[str, str]] = []
        for index, (transaction_date, hkd_amount) in enumerate(
            (("2026-07-01", "-850.00"), ("2026-07-02", "-860.00"))
        ):
            rows.extend(
                [
                    _row(
                        transaction_id=f"txn_cohort_base_{index}",
                        date=transaction_date,
                        account_id="owned_hkd",
                        posted_amount=hkd_amount,
                        posted_currency="HKD",
                        amount_hkd=hkd_amount,
                        original_description="CURRENCY EXCHANGE DEBIT",
                        category="Household",
                        flow_type="expense",
                        review_reasons="",
                        needs_review="false",
                        flags="",
                    ),
                    _row(
                        transaction_id=f"txn_cohort_foreign_{index}",
                        date=transaction_date,
                        account_id="owned_eur",
                        posted_amount="100.00",
                        posted_currency="EUR",
                        amount_hkd="",
                        original_description="FOREIGN CURRENCY DEPOSIT",
                        category="Savings",
                        flow_type="investment_transfer",
                        review_reasons="",
                        needs_review="false",
                        flags="",
                    ),
                ]
            )

        summary = reconcile_ledger(rows, {"base_currency": "HKD"})

        self.assertEqual(summary["cross_currency_paired_groups"], 2)
        self.assertEqual(
            {row["flow_type"] for row in rows},
            {"internal_transfer"},
        )

    def test_large_unique_exchange_group_has_no_hidden_size_limit(self) -> None:
        rows: list[dict[str, str]] = []
        for index in range(9):
            foreign_amount = 100 * (2**index)
            base_amount = Decimal("8.5") * foreign_amount
            rows.extend(
                [
                    _row(
                        transaction_id=f"txn_large_base_{index}",
                        account_id="owned_hkd",
                        posted_amount=f"-{base_amount:.2f}",
                        posted_currency="HKD",
                        amount_hkd=f"-{base_amount:.2f}",
                        original_description="CURRENCY EXCHANGE DEBIT",
                        category="Household",
                        flow_type="expense",
                        review_reasons="",
                        needs_review="false",
                        flags="",
                    ),
                    _row(
                        transaction_id=f"txn_large_foreign_{index}",
                        account_id="owned_eur",
                        posted_amount=f"{foreign_amount:.2f}",
                        posted_currency="EUR",
                        amount_hkd="",
                        original_description="FOREIGN CURRENCY DEPOSIT",
                        category="Savings",
                        flow_type="investment_transfer",
                        review_reasons="",
                        needs_review="false",
                        flags="",
                    ),
                ]
            )

        summary = reconcile_ledger(
            rows,
            {
                "base_currency": "HKD",
                "exchange_rates": {"EUR": 8.5},
                "reconciliation": {"exchange_rate_spread_tolerance": 0.01},
            },
        )

        self.assertEqual(summary["cross_currency_paired_groups"], 9)
        self.assertEqual(
            {row["flow_type"] for row in rows},
            {"internal_transfer"},
        )


if __name__ == "__main__":
    unittest.main()
