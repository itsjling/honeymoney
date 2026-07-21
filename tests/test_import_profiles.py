import hashlib
import subprocess
import sys
import unittest

import pdfplumber

from honeymoney.cli import _reconcile_transaction_identities
from tests.golden_helpers import (
    FIXTURE_DIR,
    assert_import_case,
    assert_pdf_byte_import_case,
    import_profile_case,
    load_json,
    load_profile,
    starter_profile,
)


def _assign_transaction_ids(
    rows: list[dict[str, str]], case_dir
) -> list[dict[str, str]]:
    """Assign identity-v2 transaction IDs against an empty ledger."""
    _reconcile_transaction_identities(
        rows,
        [],
        input_path=case_dir,
        replace_requested=False,
    )
    return rows


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
        assert_pdf_byte_import_case(
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
            row["transaction_id"]
            for row in _assign_transaction_ids(first_rows, case_dir)
        ]
        second_ids = [
            row["transaction_id"]
            for row in _assign_transaction_ids(second_rows, case_dir)
        ]

        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(set(first_ids)), len(first_ids))
        self.assertNotEqual(first_ids[1], first_ids[2])


class HsbcCreditCardPdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_pdf_byte_import_case(
            self,
            load_profile("hsbc_hk_credit_card_pdf.json"),
            "accepted_statement",
        )


class MoxBankPdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_pdf_byte_import_case(
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
            row["transaction_id"]
            for row in _assign_transaction_ids(first_rows, case_dir)
        ]
        second_ids = [
            row["transaction_id"]
            for row in _assign_transaction_ids(second_rows, case_dir)
        ]

        self.assertEqual(len(first_ids), 5)
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(set(first_ids)), len(first_ids))


class MoxCreditCardPdfProfileTest(unittest.TestCase):
    def test_accepted_statement(self) -> None:
        assert_pdf_byte_import_case(
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


class PdfByteFixtureReviewTest(unittest.TestCase):
    def test_pdf_byte_goldens_are_reproducible_and_privacy_reviewed(self) -> None:
        fixture_root = FIXTURE_DIR / "import_profiles"
        generator = fixture_root / "generate_pdf_byte_goldens.py"
        result = subprocess.run(
            [sys.executable, str(generator), "--check"],
            cwd=fixture_root.parents[2],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        review_path = fixture_root / "pdf_byte_privacy_review.json"
        self.assertTrue(review_path.is_file(), f"Missing review: {review_path}")
        review = load_json(review_path)
        self.assertEqual(
            set(review["fixtures"]),
            {
                "hsbc_one_pdf",
                "hsbc_hk_credit_card_pdf",
                "mox_bank_pdf",
                "mox_credit_card_pdf",
            },
        )
        prohibited_objects = [
            b"/EmbeddedFile",
            b"/EmbeddedFiles",
            b"/Filespec",
            b"/JavaScript",
            b"/JS",
            b"/Launch",
            b"/OpenAction",
            b"/AA",
            b"/AcroForm",
            b"/XFA",
            b"/RichMedia",
            b"/Subtype /Image",
            b"/Encrypt",
        ]
        for profile_id, expected in review["fixtures"].items():
            with self.subTest(profile=profile_id):
                fixture_path = (
                    fixture_root / profile_id / "accepted_statement/input.pdf"
                )
                fixture_bytes = fixture_path.read_bytes()
                self.assertEqual(
                    hashlib.sha256(fixture_bytes).hexdigest(), expected["sha256"]
                )
                for marker in prohibited_objects:
                    self.assertNotIn(marker, fixture_bytes)

                with pdfplumber.open(fixture_path) as pdf:
                    visible_text = "\n\f\n".join(
                        page.extract_text() or "" for page in pdf.pages
                    )
                    self.assertEqual(len(pdf.pages), expected["page_count"])
                    self.assertEqual(pdf.metadata, {})
                self.assertEqual(
                    hashlib.sha256(visible_text.encode()).hexdigest(),
                    expected["visible_text_sha256"],
                )
                self.assertTrue(expected["visible_text_reviewed"])
                self.assertTrue(expected["metadata_reviewed"])
                self.assertTrue(expected["embedded_content_reviewed"])
                self.assertFalse(expected["contains_private_data"])


if __name__ == "__main__":
    unittest.main()
