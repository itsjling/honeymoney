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


class HsbcBankCsvProfileTest(unittest.TestCase):
    def test_debit_credit_and_previous_balance(self) -> None:
        assert_import_case(
            self,
            load_profile("hsbc_hk_bank.json"),
            "debit_credit_and_previous_balance",
        )


class MoxCreditCardCsvProfileTest(unittest.TestCase):
    def test_credit_debit_indicator(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_credit_card.json"),
            "credit_debit_indicator",
        )


class HsbcBankPdfProfileTest(unittest.TestCase):
    def test_table_balances_ignored(self) -> None:
        assert_import_case(
            self,
            load_profile("hsbc_hk_bank_pdf.json"),
            "table_balances_ignored",
        )


class HsbcOnePdfProfileTest(unittest.TestCase):
    def test_sectioned_accounts_multiline_rows_summaries_and_artifacts(self) -> None:
        assert_import_case(
            self,
            load_profile("hsbc_one_pdf.json"),
            "sectioned_multiline_transactions",
        )

    def test_account_identity_produces_stable_transaction_ids(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        case_dir = (
            FIXTURE_DIR
            / "import_profiles"
            / "hsbc_one_pdf"
            / "sectioned_multiline_transactions"
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
    def test_word_rows_keep_24_7_fitness_amount_with_merchant(self) -> None:
        assert_import_case(
            self,
            load_profile("hsbc_hk_credit_card_pdf.json"),
            "word_rows_24_7_fitness",
        )


class MoxBankPdfProfileTest(unittest.TestCase):
    def test_headerless_regex_rows(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_bank_pdf.json"),
            "headerless_regex_rows",
        )

    def test_headerless_regex_row_transaction_ids_are_stable(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        case_dir = (
            FIXTURE_DIR / "import_profiles" / "mox_bank_pdf" / "headerless_regex_rows"
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
    def test_foreign_currency_suffix(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_credit_card_pdf.json"),
            "foreign_currency_suffix",
        )

    def test_multiline_regex_percent(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_credit_card_pdf.json"),
            "multiline_regex_percent",
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
