import unittest

from tests.golden_helpers import assert_import_case, load_profile, starter_profile


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


class MoxCreditCardPdfProfileTest(unittest.TestCase):
    def test_multiline_regex_percent(self) -> None:
        assert_import_case(
            self,
            load_profile("mox_credit_card_pdf.json"),
            "multiline_regex_percent",
        )


if __name__ == "__main__":
    unittest.main()
