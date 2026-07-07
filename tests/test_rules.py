import unittest

from honeymoney.rules import apply_rules, validate_rules


class RulesTest(unittest.TestCase):
    def test_priority_and_file_order_choose_one_winning_rule(self) -> None:
        transactions = [
            {
                "merchant": "APPLE",
                "original_description": "APPLE",
                "category": "Unknown",
                "owner": "Household",
                "payment_method": "Credit Card",
                "confidence": "0.00",
                "needs_review": "true",
                "reason": "",
                "flags": "uncategorized",
                "notes": "",
            }
        ]
        rules = [
            {
                "id": "apple-shopping",
                "enabled": True,
                "priority": 1,
                "match_type": "keyword",
                "patterns": ["APP"],
                "fields": ["merchant"],
                "category": "Shopping",
                "confidence": 0.95,
            },
            {
                "id": "apple-subscriptions",
                "enabled": True,
                "priority": 10,
                "match_type": "exact",
                "patterns": ["apple"],
                "fields": ["merchant"],
                "category": "Subscriptions",
                "confidence": 0.91,
            },
        ]

        apply_rules(transactions, rules, {"review_confidence_threshold": 0.8})

        self.assertEqual(transactions[0]["category"], "Subscriptions")
        self.assertEqual(transactions[0]["confidence"], "0.91")
        self.assertEqual(transactions[0]["needs_review"], "false")
        self.assertEqual(
            transactions[0]["flags"], "matched_rule:apple-subscriptions"
        )

    def test_disabled_invalid_rule_is_allowed_but_active_invalid_rule_fails(self) -> None:
        validate_rules(
            [
                {
                    "id": "draft-invalid",
                    "enabled": False,
                    "match_type": "regex",
                    "patterns": ["["],
                    "fields": ["merchant"],
                    "category": "Review Needed",
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "Unsupported category"):
            validate_rules(
                [
                    {
                        "id": "active-invalid",
                        "enabled": True,
                        "match_type": "keyword",
                        "patterns": ["X"],
                        "fields": ["merchant"],
                        "category": "Review Needed",
                    }
                ]
            )

    def test_config_can_extend_category_and_owner_vocabulary(self) -> None:
        validate_rules(
            [
                {
                    "id": "pet-supplies",
                    "enabled": True,
                    "match_type": "keyword",
                    "patterns": ["PET"],
                    "fields": ["merchant"],
                    "category": "Pet Care",
                    "owner": "Family",
                }
            ],
            {"categories": ["Unknown", "Pet Care"], "owners": ["Household", "Family"]},
        )

    def test_active_rule_with_invalid_confidence_fails_validation(self) -> None:
        for confidence in ["not-a-number", "NaN", 1.5, -0.1]:
            with self.subTest(confidence=confidence):
                with self.assertRaisesRegex(ValueError, "Unsupported confidence"):
                    validate_rules(
                        [
                            {
                                "id": "bad-confidence",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["APPLE"],
                                "fields": ["merchant"],
                                "category": "Shopping",
                                "confidence": confidence,
                            }
                        ]
                    )

    def test_rule_notes_append_without_erasing_existing_pdf_note(self) -> None:
        transactions = [
            {
                "merchant": "PARKNSHOP",
                "original_description": "PARKNSHOP",
                "category": "Unknown",
                "owner": "Household",
                "payment_method": "Bank Account",
                "confidence": "0.00",
                "needs_review": "true",
                "reason": "",
                "flags": "uncategorized",
                "notes": "Imported from PDF",
            }
        ]

        apply_rules(
            transactions,
            [
                {
                    "id": "parksnshop",
                    "enabled": True,
                    "match_type": "keyword",
                    "patterns": ["PARKNSHOP"],
                    "fields": ["merchant"],
                    "category": "Groceries",
                    "confidence": 0.95,
                    "notes": "Hong Kong supermarket",
                }
            ],
            {"review_confidence_threshold": 0.8},
        )

        self.assertEqual(
            transactions[0]["notes"], "Imported from PDF; Hong Kong supermarket"
        )


if __name__ == "__main__":
    unittest.main()
