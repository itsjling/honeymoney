import unittest

from honeymoney.cli import _assign_transaction_ids
from tests.golden_helpers import (
    FIXTURE_DIR,
    assert_import_case,
    import_profile_case,
    load_profile,
    starter_profile,
)


class StarterCsvProfileTest(unittest.TestCase):
    def test_balances_ignored(self) -> None:
        assert_import_case(self, starter_profile(), "balances_ignored")


class MoxCreditCardCsvProfileTest(unittest.TestCase):
    def test_credit_debit_indicator(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_credit_card.json"),
            "credit_debit_indicator",
        )


class HsbcOnePdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_import_case(
            self,
            load_profile("hsbc_one_pdf.json"),
            "accepted_statement",
        )

    def test_account_identity_produces_stable_transaction_ids(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        case_dir = (
            FIXTURE_DIR / "import_profiles" / "hsbc_one_pdf" / "accepted_statement"
        )
        first_rows, _ = import_profile_case(profile, case_dir)
        second_rows, _ = import_profile_case(profile, case_dir)

        first_ids = [
            row["transaction_id"] for row in _assign_transaction_ids(first_rows)
        ]
        second_ids = [
            row["transaction_id"] for row in _assign_transaction_ids(second_rows)
        ]

        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(set(first_ids)), len(first_ids))
        self.assertNotEqual(first_ids[1], first_ids[2])


class HsbcCreditCardPdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_import_case(
            self,
            load_profile("hsbc_hk_credit_card_pdf.json"),
            "accepted_statement",
        )


class MoxBankPdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_bank_pdf.json"),
            "accepted_statement",
        )

    def test_accepted_statement_transaction_ids_are_stable(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        case_dir = (
            FIXTURE_DIR / "import_profiles" / "mox_bank_pdf" / "accepted_statement"
        )

        first_rows, _ = import_profile_case(profile, case_dir)
        second_rows, _ = import_profile_case(profile, case_dir)
        first_ids = [
            row["transaction_id"] for row in _assign_transaction_ids(first_rows)
        ]
        second_ids = [
            row["transaction_id"] for row in _assign_transaction_ids(second_rows)
        ]

        self.assertEqual(len(first_ids), 5)
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(set(first_ids)), len(first_ids))


class MoxCreditCardPdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_credit_card_pdf.json"),
            "accepted_statement",
        )


class AccountSemanticsTest(unittest.TestCase):
    def test_bundled_bank_and_card_profiles_declare_account_types(self) -> None:
        expected = {
            "hsbc_one_pdf.json": "bank",
            "hsbc_hk_credit_card_pdf.json": "credit_card",
            "mox_bank_pdf.json": "bank",
            "mox_credit_card.json": "credit_card",
            "mox_credit_card_pdf.json": "credit_card",
        }
        for profile_name, account_type in expected.items():
            with self.subTest(profile=profile_name):
                self.assertEqual(
                    load_profile(profile_name)["account_type"], account_type
                )


if __name__ == "__main__":
    unittest.main()
