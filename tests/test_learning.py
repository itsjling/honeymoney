import unittest

from honeymoney.learning import plan_learned_rules
from honeymoney.rules import apply_rules


class DeterministicRuleLearningTest(unittest.TestCase):
    def _row(
        self,
        transaction_id: str,
        *,
        institution: str = "Synthetic  Bank",
        account_id: str = "synthetic  account",
        description: str = "SYNTHETIC  SHOP",
    ) -> dict[str, str]:
        return {
            "transaction_id": transaction_id,
            "institution": institution,
            "account_id": account_id,
            "original_description": description,
            "posted_amount": "-10.00",
            "posted_currency": "HKD",
            "amount_hkd": "-10.00",
            "category": "Unknown",
            "flow_type": "unresolved",
            "owner": "Household",
            "payment_method": "Bank Account",
            "confidence": "0.00",
            "needs_review": "true",
            "reason": "",
            "flags": "",
        }

    def test_grouping_and_matching_use_the_same_exact_normalization(self) -> None:
        first = self._row("txn_synthetic_first")
        second = self._row(
            "txn_synthetic_second",
            institution="Ｓｙｎｔｈｅｔｉｃ Bank",
            account_id="synthetic account",
            description="SYNTHETIC SHOP",
        )
        corrections = {
            first["transaction_id"]: {
                "category": "Dining",
                "needs_review": "false",
            },
            second["transaction_id"]: {
                "category": "Dining",
                "needs_review": "false",
            },
        }

        plan = plan_learned_rules([first, second], corrections)
        apply_rules([first, second], plan.rules, {})

        self.assertEqual(plan.broad_rule_count, 1)
        self.assertEqual(plan.historical_rows_covered, 2)
        self.assertTrue(all(row["category"] == "Dining" for row in (first, second)))

    def test_generic_identity_fields_are_skipped(self) -> None:
        row = self._row("txn_synthetic_generic", institution="N/A")

        plan = plan_learned_rules(
            [row],
            {
                row["transaction_id"]: {
                    "category": "Dining",
                    "needs_review": "false",
                }
            },
        )

        self.assertEqual(plan.rules, [])
        self.assertEqual(plan.candidate_count, 0)
        self.assertEqual(plan.skipped_count, 1)


if __name__ == "__main__":
    unittest.main()
