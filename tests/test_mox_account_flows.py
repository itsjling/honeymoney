import json
import unittest

from honeymoney.classification_policy import apply_structural_classification
from honeymoney.reconciliation import reconcile_ledger, statement_balance_reconciliation
from tests.golden_helpers import _import_pdf_case, base_config, load_profile


def _statement(deposit_order: tuple[str, str] = ("100000000031", "100000000032")):
    lines = [
        "Mox Bank statement",
        "Statement period: 01 Aug 2026 - 31 Aug 2026",
        "HKD Mox Account",
        "Activity Settlement Description Amount (HKD)",
        "01 Aug 01 Aug Opening balance 100.00",
        "02 Aug 02 Aug SYNTHETIC CREDIT +5.00",
        "31 Aug 31 Aug Closing balance 105.00",
        "USD Mox Account",
        "Activity Settlement Description Amount (USD)",
        "01 Aug 01 Aug Opening balance 200.00",
        "03 Aug 03 Aug Time Deposit -80.00",
        "03 Aug 03 Aug Time Deposit Upfront Interest +2.00",
        "04 Aug 04 Aug Time Deposit -40.00",
        "04 Aug 04 Aug Time Deposit Upfront Interest +1.00",
        "31 Aug 31 Aug Closing balance 83.00",
    ]
    for reference in deposit_order:
        principal, interest, day = (
            ("80.00", "2.00", "03")
            if reference == "100000000031"
            else ("40.00", "1.00", "04")
        )
        lines.extend(
            [
                f"Time Deposit - {reference}",
                f"Creation date: {day} Aug 2026",
                f"Principal Amount: {principal} USD",
                f"Interest paid upfront: {interest} USD",
                "Maturity date: 03 Nov 2026",
                "Activity Settlement Description Amount (USD)",
                "01 Aug 01 Aug Opening balance 0.00",
                f"{day} Aug {day} Aug USD Mox Account +{principal}",
                f"{day} Aug {day} Aug Time Deposit Interest 0.00",
                f"31 Aug 31 Aug Closing balance {principal}",
            ]
        )
    words = [
        {"text": line, "x0": 70, "top": index * 20} for index, line in enumerate(lines)
    ]
    return _import_pdf_case(
        load_profile("mox_bank_pdf.json"), tables=[], words_pages=[words]
    )


class MoxAccountFlowTest(unittest.TestCase):
    def test_principal_pairs_do_not_turn_upfront_interest_into_transfers(self) -> None:
        rows, warnings = _statement()
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 7)
        self.assertEqual(len({row["account_id"] for row in rows}), 4)
        for index, row in enumerate(rows):
            row["transaction_id"] = f"synthetic-mox-{index}"

        apply_structural_classification(rows, base_config())
        reconcile_ledger(rows, base_config())

        interest = [
            row for row in rows if "Upfront Interest" in row["original_description"]
        ]
        principal = [row for row in rows if row["flow_type"] == "internal_transfer"]
        self.assertEqual(len(interest), 2)
        self.assertTrue(all(row["category"] == "Income" for row in interest))
        self.assertTrue(all(row["flow_type"] == "income" for row in interest))
        self.assertTrue(all(not row["paired_transaction_id"] for row in interest))
        self.assertEqual(len(principal), 4)
        self.assertTrue(all(row["posted_currency"] == "USD" for row in principal))
        self.assertTrue(
            all(row["reconciliation_status"] == "paired" for row in principal)
        )
        self.assertEqual(len({row["transfer_group_id"] for row in principal}), 2)
        self.assertFalse(any(row["flow_type"] == "expense" for row in rows))
        checks = statement_balance_reconciliation(rows)
        self.assertEqual(len(checks), 4)
        self.assertTrue(all(check["result"] == "matched" for check in checks.values()))

    def test_deposit_identity_survives_section_order_without_exposing_references(
        self,
    ) -> None:
        references = ("100000000031", "100000000032")
        original, _ = _statement(references)
        reordered, _ = _statement(tuple(reversed(references)))

        def deposits(rows):
            return {
                row["posted_amount"]: (row["account_id"], row["statement_section"])
                for row in rows
                if row["account_id"].startswith("mox_time_deposit_")
            }

        self.assertEqual(deposits(original), deposits(reordered))
        self.assertEqual(len(set(deposits(original).values())), 2)
        encoded = json.dumps([original, reordered])
        for reference in references:
            self.assertNotIn(reference, encoded)


if __name__ == "__main__":
    unittest.main()
