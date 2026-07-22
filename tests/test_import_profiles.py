import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pdfplumber

from honeymoney.cli import (
    _import_pdf,
    _import_transactions,
    _load_config_document,
)
from honeymoney.identity import (
    IdentityError,
    empty_manifest,
    logical_locator,
    resolve_batch,
    source_namespace_id,
)
from tests.golden_helpers import (
    FIXTURE_DIR,
    assert_import_case,
    assert_pdf_byte_import_case,
    load_json,
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
        assert_pdf_byte_import_case(
            self,
            load_profile("hsbc_one_pdf.json"),
            "accepted_statement",
        )


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


class IdentityParserInputsTest(unittest.TestCase):
    def test_config_root_is_private_and_independent_of_input_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "workspace"
            input_dir = config_dir / "input"
            input_dir.mkdir(parents=True)
            statement = input_dir / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-01-01,Coffee,-1.00,HKD\n",
                encoding="utf-8",
            )
            config_path = config_dir / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            config = _load_config_document(str(config_path), recover=False)

            single = _import_transactions(
                [statement],
                [starter_profile()],
                config,
                statement,
                False,
                {},
                None,
                include_identity_sources=True,
            )
            folder = _import_transactions(
                [statement],
                [starter_profile()],
                config,
                input_dir,
                False,
                {},
                None,
                include_identity_sources=True,
            )

            self.assertEqual(config["_identity_workspace_root"], config_dir.resolve())
            self.assertEqual(config["_identity_config_path"], config_path.resolve())
            self.assertEqual(single[3][0].namespace_id, folder[3][0].namespace_id)
            self.assertEqual(single[3][0].source_display, "statement.csv")
            self.assertNotIn(str(config_dir), json.dumps(single[2]))

    def test_no_config_uses_default_config_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = Path.cwd()
            try:
                os.chdir(root)
                config = _load_config_document(None, recover=False)
            finally:
                os.chdir(previous)
            self.assertEqual(config["_identity_workspace_root"], root.resolve())
            self.assertEqual(
                config["_identity_config_path"], (root / "config.json").resolve()
            )

    def test_external_source_identity_never_enters_rows_or_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "workspace"
            external_dir = root / "outside"
            config_dir.mkdir()
            external_dir.mkdir()
            config_path = config_dir / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            statement = external_dir / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-01-01,Coffee,-1.00,HKD\n",
                encoding="utf-8",
            )
            config = _load_config_document(str(config_path), recover=False)
            result = _import_transactions(
                [statement],
                [starter_profile()],
                config,
                external_dir,
                False,
                {},
                None,
                include_identity_sources=True,
            )
            rows, _, reports, sources = result
            kind, locator = logical_locator(statement, config_dir)

            self.assertEqual(kind, "external")
            self.assertEqual(
                sources[0].namespace_id, source_namespace_id(kind, locator)
            )
            self.assertEqual(rows[0]["source_file"], "statement.csv")
            self.assertEqual(
                [
                    rows[0]["source_id"],
                    rows[0]["source_namespace_id"],
                    rows[0]["source_revision"],
                    rows[0]["source_record_id"],
                ],
                ["", "", "", ""],
            )
            self.assertEqual(
                [name for name in rows[0] if name.startswith("identity_")], []
            )
            self.assertNotIn(str(root), json.dumps(reports))
            self.assertNotIn("locator", json.dumps(reports))

    def test_zero_row_csv_still_has_identity_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.csv"
            statement.write_text("Date,Description,Amount,Currency\n", encoding="utf-8")
            config = _identity_config(root)
            rows, _, reports, sources = _import_transactions(
                [statement],
                [starter_profile()],
                config,
                root,
                False,
                {},
                None,
                include_identity_sources=True,
            )

            self.assertEqual(rows, [])
            self.assertEqual(sources[0].record_data, ())
            self.assertEqual(reports[0]["status"], "processed")
            self.assertEqual(reports[0]["transaction_count"], "0")

    def test_csv_identity_uses_physical_record_starts_after_skipped_multiline_row(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-01-01,Skip this,-1.00,HKD\n"
                '2026-01-02,"Two line\ndescription",-2.00,HKD\n'
                "2026-01-03,Later,-3.00,HKD\n",
                encoding="utf-8",
            )
            profile = starter_profile()
            profile["skip_descriptions"] = ["skip this"]
            config = _identity_config(root)
            rows, _, _, sources = _import_transactions(
                [statement],
                [profile],
                config,
                root,
                False,
                {},
                None,
                include_identity_sources=True,
            )

            self.assertEqual([row["source_row"] for row in rows], ["3", "4"])
            self.assertEqual(
                [record.locator.components for record in sources[0].record_data],
                [(3,), (5,)],
            )

    def test_csv_identity_uses_header_aware_first_physical_record_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-01-01,First,-1.00,HKD\n",
                encoding="utf-8",
            )
            _, _, _, sources = _import_transactions(
                [statement],
                [starter_profile()],
                _identity_config(root),
                root,
                False,
                {},
                None,
                include_identity_sources=True,
            )

            self.assertEqual(sources[0].record_data[0].locator.components, (2,))

    def test_duplicate_parser_locators_are_rejected_by_identity_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-01-01,Coffee,-1.00,HKD\n",
                encoding="utf-8",
            )
            _, _, _, sources = _import_transactions(
                [statement],
                [starter_profile()],
                _identity_config(root),
                root,
                False,
                {},
                None,
                include_identity_sources=True,
            )
            duplicate = replace(sources[0], record_data=sources[0].record_data * 2)

            with self.assertRaisesRegex(
                IdentityError, "identity_allocation_locator_invalid"
            ):
                resolve_batch(
                    ledger_rows=[],
                    manifest=empty_manifest(),
                    sources=[duplicate],
                    intent="import",
                )

    def test_pdf_table_identity_keeps_table_row_and_split_subrow(self) -> None:
        profile = {
            "id": "table",
            "account_id": "table",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "has_header": True,
                "split_multiline_rows": True,
                "split_multiline_row_count_columns": ["Date"],
                "columns": {
                    "transaction_date": "Date",
                    "description": "Description",
                    "amount": "Amount",
                },
            },
        }
        tables = [
            [["Date", "Description", "Amount"], ["2026-01-01", "One", "1.00"]],
            [
                ["Date", "Description", "Amount"],
                ["2026-01-02\n2026-01-03", "Two\nThree", "2.00\n3.00"],
            ],
        ]
        rows, _, records = _import_fake_pdf(profile, tables=tables)

        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [record.locator.components for record in records],
            [(1, 1, 2, 1), (1, 2, 2, 1), (1, 2, 2, 2)],
        )

    def test_pdf_word_identity_uses_original_line_before_filtering(self) -> None:
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "word_rows": True,
                "word_header_markers": ["Post date", "Description", "Amount"],
                "word_columns": {
                    "Post date": [0, 90],
                    "Description": [90, 200],
                    "Amount": [200, 300],
                },
                "columns": {
                    "transaction_date": "Post date",
                    "description": "Description",
                    "amount": "Amount",
                },
            },
        }
        words = [
            {"text": "Post date", "x0": 0, "top": 10},
            {"text": "Description", "x0": 100, "top": 10},
            {"text": "Amount", "x0": 210, "top": 10},
            {"text": "Ignore", "x0": 100, "top": 20},
            {"text": "2026-01-01", "x0": 0, "top": 30},
            {"text": "Coffee", "x0": 100, "top": 30},
            {"text": "-1.00", "x0": 210, "top": 30},
        ]
        _, _, records = _import_fake_pdf(profile, words=words)

        self.assertEqual(records[0].locator.components, (1, 3))

    def test_pdf_sectioned_identity_uses_physical_line(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        fixture = (
            FIXTURE_DIR
            / "import_profiles"
            / "hsbc_one_pdf"
            / "accepted_statement"
            / "words.json"
        )
        words = load_json(fixture)["pages"][0]
        _, _, records = _import_fake_pdf(profile, words=words)

        self.assertEqual(
            [record.locator.adapter_tag for record in records], [4] * len(records)
        )
        self.assertEqual(records[0].locator.components, (1, 8))


def _identity_config(root: Path) -> dict:
    config_path = root / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    return _load_config_document(str(config_path), recover=False)


def _import_fake_pdf(
    profile: dict,
    *,
    tables: list | None = None,
    words: list | None = None,
):
    class Page:
        def extract_tables(self):
            return tables or []

        def extract_table(self):
            return (tables or [None])[0]

        def extract_words(self, **kwargs):
            return words or []

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        statement = root / "statement.pdf"
        statement.write_bytes(b"%PDF-1.4 synthetic")
        fake_pdfplumber = types.SimpleNamespace(open=lambda path: Pdf())
        with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
            return _import_pdf(
                statement,
                profile,
                {"base_currency": "HKD", "exchange_rates": {"HKD": 1}},
                root,
                include_identity_records=True,
            )


if __name__ == "__main__":
    unittest.main()
