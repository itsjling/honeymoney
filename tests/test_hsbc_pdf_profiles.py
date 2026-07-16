import json
import unittest
from pathlib import Path


class HsbcCreditCardPdfProfileConsistencyTest(unittest.TestCase):
    def test_bundled_and_example_profiles_are_word_rows_only(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        profile_paths = [
            repo_root
            / "honeymoney"
            / "data"
            / "profiles"
            / "hsbc_hk_credit_card_pdf.json",
            repo_root / "examples" / "profiles" / "hsbc_hk_credit_card_pdf.json",
        ]

        for profile_path in profile_paths:
            with self.subTest(profile=profile_path):
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertIs(profile["pdf"]["word_rows_only"], True)


class HsbcOnePdfProfileConsistencyTest(unittest.TestCase):
    def test_bundled_and_example_profiles_include_foreign_currency_savings(
        self,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        profile_paths = [
            repo_root / "honeymoney" / "data" / "profiles" / "hsbc_one_pdf.json",
            repo_root / "examples" / "profiles" / "hsbc_one_pdf.json",
        ]

        for profile_path in profile_paths:
            with self.subTest(profile=profile_path):
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                pdf = profile["pdf"]
                sectioned = pdf["sectioned_word_rows"]
                foreign_account = sectioned["accounts"]["Foreign Currency Savings"]
                self.assertEqual(foreign_account["account_id"], "hsbc_one_fcy_savings")
                self.assertIs(foreign_account["currency_from_row"], True)
                self.assertEqual(pdf["columns"]["original_currency"], "Currency")


if __name__ == "__main__":
    unittest.main()
