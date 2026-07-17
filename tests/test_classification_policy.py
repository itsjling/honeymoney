import unittest
from decimal import Decimal

from honeymoney.classification_policy import (
    apply_structural_classification,
    category_policies,
    evaluate_model_suggestion,
    model_category_descriptions,
    trusted_accounting_provenance,
    validate_category_policies,
)


def row(description: str, amount: str, **overrides: str) -> dict[str, str]:
    value = {
        "transaction_id": "txn_test",
        "merchant": description,
        "original_description": description,
        "amount_hkd": amount,
        "account_type": "bank",
        "category": "Unknown",
        "owner": "Household",
        "confidence": "0.00",
        "needs_review": "true",
        "flow_type": "unresolved",
        "flow_source": "deterministic",
        "reason": "Unresolved",
        "flags": "uncategorized",
    }
    value.update(overrides)
    return value


class ClassificationPolicyTest(unittest.TestCase):
    def test_builtin_and_custom_category_kinds(self) -> None:
        config = {"categories": ["Dining", "Income", "Other", "Custom"]}
        policies = category_policies(config)
        self.assertEqual(policies["Dining"].kind, "spending")
        self.assertEqual(policies["Income"].kind, "accounting")
        self.assertEqual(policies["Other"].kind, "manual_only")
        self.assertEqual(policies["Custom"].kind, "manual_only")
        self.assertEqual(set(model_category_descriptions(config)), {"Dining"})

    def test_config_policy_validation_and_protected_boundary(self) -> None:
        for config, error in [
            ({"category_policies": []}, "must be a JSON object"),
            ({"category_policies": {"Dining": []}}, "must be a JSON object"),
            (
                {
                    "category_policies": {
                        "Dining": {"kind": "unsafe", "description": "Meals"}
                    }
                },
                "must be spending, accounting, or manual_only",
            ),
            (
                {
                    "category_policies": {
                        "Dining": {"kind": "spending", "description": ""}
                    }
                },
                "must be a non-empty string",
            ),
            (
                {
                    "category_policies": {
                        "Dining": {"kind": "spending", "description": 3}
                    }
                },
                "must be a non-empty string",
            ),
        ]:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, error):
                    validate_category_policies(config)
        with self.assertRaisesRegex(ValueError, "unknown category"):
            validate_category_policies({"category_policies": {"Nope": {}}})
        with self.assertRaisesRegex(ValueError, "cannot relax"):
            validate_category_policies(
                {
                    "category_policies": {
                        "Income": {"kind": "spending", "description": "No"}
                    }
                }
            )
        config = {
            "category_policies": {
                "Dining": {"kind": "spending", "description": "Meals."}
            }
        }
        self.assertEqual(category_policies(config)["Dining"].description, "Meals.")

    def test_structural_predicates_require_phrase_sign_and_no_duplicate(self) -> None:
        cases = [
            (row("MONTHLY CASH REBATE", "10.00"), "Other", "refund"),
            (row("SAVINGS INTEREST", "10.00"), "Income", "income"),
            (row("ATM WITHDRAWAL CENTRAL", "-10.00"), "Cash", "expense"),
            (
                row("CREDIT CARD PAYMENT", "10.00", account_type="credit_card"),
                "Credit Card Payment",
                "credit_card_payment",
            ),
        ]
        for transaction, category, flow in cases:
            with self.subTest(category=category):
                self.assertEqual(apply_structural_classification([transaction], {}), 1)
                self.assertEqual(transaction["category"], category)
                self.assertEqual(transaction["flow_type"], flow)
                self.assertEqual(
                    transaction["needs_review"],
                    "true" if category == "Other" else "false",
                )
        unknown_owner_interest = row("SAVINGS INTEREST", "10.00", owner="Unknown")
        self.assertEqual(
            apply_structural_classification([unknown_owner_interest], {}), 1
        )
        self.assertEqual(unknown_owner_interest["needs_review"], "true")
        for transaction in [
            row("cash", "10.00"),
            row("ATM WITHDRAWAL", "10.00"),
            row("INTEREST", "-10.00"),
            row("CASHBACK", "10.00", flags="duplicate_suspected"),
        ]:
            self.assertEqual(apply_structural_classification([transaction], {}), 0)
            self.assertEqual(transaction["category"], "Unknown")

    def test_model_review_and_trusted_provenance(self) -> None:
        self.assertEqual(
            evaluate_model_suggestion(
                row("SHOP", "-10.00"), "Dining", Decimal("0.9"), {}
            ).outcome,
            "accepted",
        )
        self.assertEqual(
            evaluate_model_suggestion(
                row("SHOP", "-10.00", owner="Unknown"), "Dining", Decimal("0.9"), {}
            ).outcome,
            "reviewable",
        )
        self.assertEqual(
            evaluate_model_suggestion(
                row("SHOP", "10.00"), "Income", Decimal("1"), {}
            ).outcome,
            "rejected",
        )
        model_income = row(
            "INTEREST", "10.00", category="Income", flags="ollama_categorized"
        )
        self.assertFalse(trusted_accounting_provenance(model_income))
        model_income["flow_source"] = "structural"
        self.assertTrue(trusted_accounting_provenance(model_income))


if __name__ == "__main__":
    unittest.main()
