import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pdfplumber

from honeymoney.cli import _load_config_document, _preview_profile_input, main
from honeymoney.identity import (
    IdentityError,
    empty_manifest,
    logical_locator,
    resolve_batch,
    source_namespace_id,
)
from honeymoney.importers import (
    _import_pdf,
    _import_transactions,
    _pdf_balance_lines,
    _pdf_balance_observations,
    _validate_profile,
)
from honeymoney.reconciliation import reconcile_ledger
from tests.golden_helpers import (
    FIXTURE_DIR,
    assert_import_case,
    assert_pdf_byte_import_case,
    base_config,
    import_profile_case,
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

    def test_footer_boundary(self) -> None:
        assert_pdf_byte_import_case(
            self,
            load_profile("hsbc_hk_credit_card_pdf.json"),
            "footer_boundary",
        )

    def test_preview_and_import_share_footer_boundary(self) -> None:
        profile = load_profile("hsbc_hk_credit_card_pdf.json")
        fixture = (
            FIXTURE_DIR
            / "import_profiles"
            / "hsbc_hk_credit_card_pdf"
            / "footer_boundary"
            / "input.pdf"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.pdf"
            statement.write_bytes(fixture.read_bytes())
            config = {"base_currency": "HKD", "exchange_rates": {"HKD": 1}}
            imported_rows, imported_warnings = _import_pdf(
                statement, profile, config, root
            )
            preview_rows, preview_warnings = _preview_profile_input(
                profile, str(profile["id"]), statement, config
            )

        self.assertEqual(preview_warnings, imported_warnings)
        self.assertEqual(preview_rows, imported_rows)


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


class PdfBalanceMappingValidationTest(unittest.TestCase):
    def test_profiles_without_balance_mappings_remain_valid(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        profile["pdf"].pop("balance_mappings", None)

        _validate_profile(profile, Path("profile.json"), base_config())

    def test_balance_mapping_requires_paired_balance_regexes(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "account_id": "mox_bank_main",
                "currency": "HKD",
                "opening_regex": r"OPENING (?P<balance>\d+\.\d{2})",
            }
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf\.balance_mappings\[0\]\.closing_regex must be a non-empty string",
        ):
            _validate_profile(profile, Path("profile.json"), base_config())

    def test_balance_mapping_rejects_duplicate_targets(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        mapping = {
            "account_id": "mox_bank_main",
            "currency": "HKD",
            "opening_regex": r"OPENING (?P<balance>\d+\.\d{2})",
            "closing_regex": r"CLOSING (?P<balance>\d+\.\d{2})",
        }
        profile["pdf"]["balance_mappings"] = [mapping, dict(mapping)]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf\.balance_mappings\[1\] conflicts with mapping 0",
        ):
            _validate_profile(profile, Path("profile.json"), base_config())

    def test_static_balance_mapping_strips_account_and_currency(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        mapping = profile["pdf"]["balance_mappings"][0]
        mapping["account_id"] = "  mox_bank_main  "
        mapping["currency"] = "  hkd  "

        _validate_profile(profile, Path("profile.json"), base_config())

        self.assertEqual(mapping["account_id"], "mox_bank_main")
        self.assertEqual(mapping["currency"], "HKD")

    def test_dynamic_currency_mapping_requires_known_section_and_group(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "section": "Foreign Currency Savings",
                "currency_group": "currency",
                "opening_regex": (
                    r"^(?P<currency>[A-Z]{3}) B/F "
                    r"(?P<balance>\d+\.\d{2})$"
                ),
                "closing_regex": (
                    r"^(?P<currency>[A-Z]{3}) C/F "
                    r"(?P<balance>\d+\.\d{2})$"
                ),
            }
        ]

        _validate_profile(profile, Path("profile.json"), base_config())

    def test_optional_balance_group_fails_with_a_controlled_diagnostic(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "account_id": "mox_bank_main",
                "currency": "HKD",
                "opening_regex": r"^OPENING(?: (?P<balance>\d+\.\d{2}))?$",
                "closing_regex": r"^CLOSING(?: (?P<balance>\d+\.\d{2}))?$",
            }
        ]
        _validate_profile(profile, Path("profile.json"), base_config())

        class Page:
            def extract_words(self, **kwargs):
                return [{"text": "OPENING", "x0": 20, "top": 10}]

            def extract_tables(self):
                return []

        class Pdf:
            pages = [Page()]

        with self.assertRaisesRegex(
            ValueError, "PDF balance mapping captured an invalid balance"
        ):
            _pdf_balance_observations(Pdf(), profile["pdf"])

    def test_dynamic_mappings_conflict_for_one_account_despite_group_names(
        self,
    ) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "section": "Foreign Currency Savings",
                "currency_group": group,
                "opening_regex": (
                    rf"^(?P<{group}>[A-Z]{{3}}) B/F "
                    r"(?P<balance>\d+\.\d{2})$"
                ),
                "closing_regex": (
                    rf"^(?P<{group}>[A-Z]{{3}}) C/F "
                    r"(?P<balance>\d+\.\d{2})$"
                ),
            }
            for group in ("currency", "ccy")
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf\.balance_mappings\[1\] conflicts with mapping 0",
        ):
            _validate_profile(profile, Path("profile.json"), base_config())

    def test_dynamic_mapping_conflicts_with_fixed_target_for_same_account(
        self,
    ) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "section": "Foreign Currency Savings",
                "currency_group": "currency",
                "opening_regex": (
                    r"^(?P<currency>[A-Z]{3}) B/F "
                    r"(?P<balance>\d+\.\d{2})$"
                ),
                "closing_regex": (
                    r"^(?P<currency>[A-Z]{3}) C/F "
                    r"(?P<balance>\d+\.\d{2})$"
                ),
            },
            {
                "account_id": "hsbc_one_fcy_savings",
                "currency": "AUD",
                "opening_regex": r"^AUD OPEN (?P<balance>\d+\.\d{2})$",
                "closing_regex": r"^AUD CLOSE (?P<balance>\d+\.\d{2})$",
            },
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf\.balance_mappings\[1\] conflicts with mapping 0",
        ):
            _validate_profile(profile, Path("profile.json"), base_config())

    def test_static_and_section_targets_share_duplicate_namespace(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "section": "HKD Savings",
                "currency": "HKD",
                "opening_regex": r"^B/F (?P<balance>\d+\.\d{2})$",
                "closing_regex": r"^C/F (?P<balance>\d+\.\d{2})$",
            },
            {
                "account_id": "hsbc_one_hkd_savings",
                "currency": "HKD",
                "opening_regex": r"^OPEN (?P<balance>\d+\.\d{2})$",
                "closing_regex": r"^CLOSE (?P<balance>\d+\.\d{2})$",
            },
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf\.balance_mappings\[1\] conflicts with mapping 0",
        ):
            _validate_profile(profile, Path("profile.json"), base_config())


class PdfBalanceReconciliationTest(unittest.TestCase):
    def test_supported_synthetic_statements_reconcile_by_account_and_currency(
        self,
    ) -> None:
        for profile_id in (
            "hsbc_one_pdf",
            "hsbc_hk_credit_card_pdf",
            "mox_bank_pdf",
            "mox_credit_card_pdf",
        ):
            with self.subTest(profile=profile_id):
                case_dir = (
                    FIXTURE_DIR / "import_profiles" / profile_id / "accepted_statement"
                )
                rows, warnings = import_profile_case(
                    load_profile(f"{profile_id}.json"), case_dir
                )

                self.assertEqual(warnings, [])
                self.assertFalse(
                    any(
                        "balance" in row["original_description"].casefold()
                        for row in rows
                    )
                )
                report = reconcile_ledger(rows, {})["balance_reconciliation"]
                self.assertTrue(report)
                self.assertEqual(
                    {
                        statement["result"]
                        for account in report.values()
                        for statement in account["statements"]
                    },
                    {"matched"},
                )

    def test_conflicting_extracted_balances_mark_rows_and_do_not_fail(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        tables = [
            [
                ["01 Apr 02 Apr SYNTHETIC CREDIT +10.00"],
                ["01 Apr 01 Apr OPENING BALANCE +100.00"],
                ["02 Apr 02 Apr OPENING BALANCE +101.00"],
                ["30 Apr 30 Apr CLOSING BALANCE +110.00"],
            ]
        ]

        rows, warnings, _ = _import_fake_pdf(profile, tables=tables)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["statement_opening_balance"], "")
        self.assertIn("statement_opening_balance_conflict", rows[0]["flags"])
        report = reconcile_ledger(rows, {})["balance_reconciliation"]["mox_bank_main"]
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["result"], "unavailable")
        self.assertEqual(
            report["statements"][0]["reason"], "Opening balances conflict."
        )

    def test_balance_scanner_reads_table_rows_alongside_words(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        words = [
            {"text": "Page", "x0": 20, "top": 10},
            {"text": "1", "x0": 55, "top": 10},
            {"text": "of", "x0": 70, "top": 10},
            {"text": "1", "x0": 90, "top": 10},
            {"text": "OPENING", "x0": 20, "top": 30},
            {"text": "BALANCE", "x0": 90, "top": 30},
            {"text": "+100.00", "x0": 170, "top": 30},
        ]
        tables = [
            [
                ["01 Apr 02 Apr SYNTHETIC CREDIT +10.00"],
                ["01 Apr 01 Apr OPENING BALANCE +100.00"],
                ["30 Apr 30 Apr CLOSING BALANCE +110.00"],
            ]
        ]

        rows, warnings, _ = _import_fake_pdf(profile, tables=tables, words=words)

        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_balance_scanner_deduplicates_word_and_table_rows(self) -> None:
        class Page:
            def extract_words(self, **kwargs):
                return [
                    {"text": "OPENING", "x0": 20, "top": 10},
                    {"text": "BALANCE", "x0": 90, "top": 10},
                    {"text": "+100.00", "x0": 170, "top": 10},
                ]

            def extract_tables(self):
                return [[["OPENING BALANCE +100.00"]]]

        self.assertEqual(_pdf_balance_lines(Page(), {}), ["OPENING BALANCE +100.00"])

    def test_balance_scanner_keeps_identical_lines_in_separate_sections(self) -> None:
        class Page:
            def extract_words(self, **kwargs):
                return [
                    {"text": "HKD", "x0": 20, "top": 10},
                    {"text": "Savings", "x0": 50, "top": 10},
                    {"text": "B/F", "x0": 20, "top": 30},
                    {"text": "BALANCE", "x0": 50, "top": 30},
                    {"text": "+100.00", "x0": 120, "top": 30},
                    {"text": "C/F", "x0": 20, "top": 50},
                    {"text": "BALANCE", "x0": 50, "top": 50},
                    {"text": "+110.00", "x0": 120, "top": 50},
                    {"text": "HKD", "x0": 20, "top": 70},
                    {"text": "Current", "x0": 50, "top": 70},
                    {"text": "B/F", "x0": 20, "top": 90},
                    {"text": "BALANCE", "x0": 50, "top": 90},
                    {"text": "+100.00", "x0": 120, "top": 90},
                    {"text": "C/F", "x0": 20, "top": 110},
                    {"text": "BALANCE", "x0": 50, "top": 110},
                    {"text": "+110.00", "x0": 120, "top": 110},
                ]

            def extract_tables(self):
                return [
                    [
                        ["HKD Savings"],
                        ["B/F BALANCE +100.00"],
                        ["C/F BALANCE +110.00"],
                        ["HKD Current"],
                        ["B/F BALANCE +100.00"],
                        ["C/F BALANCE +110.00"],
                    ]
                ]

        class Pdf:
            pages = [Page()]

        profile = load_profile("hsbc_one_pdf.json")
        observations = _pdf_balance_observations(Pdf(), profile["pdf"])

        self.assertEqual(
            observations,
            {
                ("hsbc_one_hkd_savings", "HKD"): {
                    "opening": [Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
                ("hsbc_one_hkd_current", "HKD"): {
                    "opening": [Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
            },
        )

    def test_balance_scanner_keeps_table_section_context_and_repeated_rows(
        self,
    ) -> None:
        class Page:
            def extract_words(self, **kwargs):
                return [
                    {"text": "Account:", "x0": 20, "top": 10},
                    {"text": "HKD", "x0": 80, "top": 10},
                    {"text": "Savings", "x0": 110, "top": 10},
                    {"text": "Account:", "x0": 20, "top": 70},
                    {"text": "HKD", "x0": 80, "top": 70},
                    {"text": "Current", "x0": 110, "top": 70},
                ]

            def extract_tables(self):
                return [
                    [
                        ["Account: HKD Savings"],
                        ["B/F BALANCE +100.00"],
                        ["B/F BALANCE +100.00"],
                        ["C/F BALANCE +110.00"],
                        ["Account: HKD Current"],
                        ["B/F BALANCE +200.00"],
                        ["C/F BALANCE +210.00"],
                    ]
                ]

        class Pdf:
            pages = [Page()]

        observations = _pdf_balance_observations(
            Pdf(), load_profile("hsbc_one_pdf.json")["pdf"]
        )

        self.assertEqual(
            observations,
            {
                ("hsbc_one_hkd_savings", "HKD"): {
                    "opening": [Decimal("100.00"), Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
                ("hsbc_one_hkd_current", "HKD"): {
                    "opening": [Decimal("200.00")],
                    "closing": [Decimal("210.00")],
                },
            },
        )

    def test_section_end_stops_balance_scanner_and_transaction_parser(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        profile["pdf"]["sectioned_word_rows"]["section_end_markers"] = ["END SECTION"]
        words = [
            {"text": "Statement", "x0": 20, "top": 10},
            {"text": "Date", "x0": 75, "top": 10},
            {"text": "05", "x0": 105, "top": 10},
            {"text": "January", "x0": 123, "top": 10},
            {"text": "2026", "x0": 153, "top": 10},
            {"text": "Foreign", "x0": 20, "top": 30},
            {"text": "Currency", "x0": 65, "top": 30},
            {"text": "Savings", "x0": 115, "top": 30},
            {"text": "Date", "x0": 82, "top": 50},
            {"text": "Transaction", "x0": 118, "top": 50},
            {"text": "Details", "x0": 190, "top": 50},
            {"text": "Deposit", "x0": 350, "top": 50},
            {"text": "Withdrawal", "x0": 418, "top": 50},
            {"text": "Balance", "x0": 500, "top": 50},
            {"text": "AUD", "x0": 59, "top": 70},
            {"text": "B/F", "x0": 120, "top": 70},
            {"text": "BALANCE", "x0": 145, "top": 70},
            {"text": "100.00", "x0": 500, "top": 70},
            {"text": "AUD", "x0": 59, "top": 90},
            {"text": "31", "x0": 79, "top": 90},
            {"text": "Dec", "x0": 85, "top": 90},
            {"text": "SYNTHETIC", "x0": 120, "top": 90},
            {"text": "DEBIT", "x0": 185, "top": 90},
            {"text": "5.00", "x0": 425, "top": 90},
            {"text": "AUD", "x0": 59, "top": 110},
            {"text": "C/F", "x0": 120, "top": 110},
            {"text": "BALANCE", "x0": 145, "top": 110},
            {"text": "95.00", "x0": 500, "top": 110},
            {"text": "END", "x0": 20, "top": 130},
            {"text": "SECTION", "x0": 50, "top": 130},
            {"text": "AUD", "x0": 59, "top": 150},
            {"text": "B/F", "x0": 120, "top": 150},
            {"text": "BALANCE", "x0": 145, "top": 150},
            {"text": "999.00", "x0": 500, "top": 150},
        ]

        rows, warnings, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "95.00")
        self.assertNotIn("balance_conflict", rows[0]["flags"])

    def test_section_label_in_description_does_not_change_balance_section(
        self,
    ) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        words = [
            {"text": "Statement", "x0": 20, "top": 10},
            {"text": "Date", "x0": 75, "top": 10},
            {"text": "05", "x0": 105, "top": 10},
            {"text": "January", "x0": 123, "top": 10},
            {"text": "2026", "x0": 153, "top": 10},
            {"text": "Account:", "x0": 5, "top": 30},
            {"text": "Foreign", "x0": 20, "top": 30},
            {"text": "Currency", "x0": 65, "top": 30},
            {"text": "Savings", "x0": 115, "top": 30},
            {"text": "Date", "x0": 82, "top": 50},
            {"text": "Transaction", "x0": 118, "top": 50},
            {"text": "Details", "x0": 190, "top": 50},
            {"text": "Deposit", "x0": 350, "top": 50},
            {"text": "Withdrawal", "x0": 418, "top": 50},
            {"text": "Balance", "x0": 500, "top": 50},
            {"text": "AUD", "x0": 59, "top": 70},
            {"text": "B/F", "x0": 120, "top": 70},
            {"text": "BALANCE", "x0": 145, "top": 70},
            {"text": "100.00", "x0": 500, "top": 70},
            {"text": "AUD", "x0": 59, "top": 90},
            {"text": "31", "x0": 79, "top": 90},
            {"text": "Dec", "x0": 85, "top": 90},
            {"text": "SYNTHETIC", "x0": 120, "top": 90},
            {"text": "TRANSFER", "x0": 185, "top": 90},
            {"text": "TO", "x0": 245, "top": 90},
            {"text": "HKD", "x0": 270, "top": 90},
            {"text": "Savings", "x0": 300, "top": 90},
            {"text": "5.00", "x0": 425, "top": 90},
            {"text": "AUD", "x0": 59, "top": 110},
            {"text": "C/F", "x0": 120, "top": 110},
            {"text": "BALANCE", "x0": 145, "top": 110},
            {"text": "95.00", "x0": 500, "top": 110},
        ]

        rows, warnings, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_id"], "hsbc_one_fcy_savings")
        self.assertEqual(rows[0]["posted_currency"], "AUD")
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "95.00")

    def test_section_label_in_wrapped_description_keeps_table_balance_section(
        self,
    ) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        words = [
            {"text": "Statement", "x0": 20, "top": 10},
            {"text": "Date", "x0": 75, "top": 10},
            {"text": "05", "x0": 105, "top": 10},
            {"text": "January", "x0": 123, "top": 10},
            {"text": "2026", "x0": 153, "top": 10},
            {"text": "Account:", "x0": 5, "top": 30},
            {"text": "Foreign", "x0": 20, "top": 30},
            {"text": "Currency", "x0": 65, "top": 30},
            {"text": "Savings", "x0": 115, "top": 30},
            {"text": "Date", "x0": 82, "top": 50},
            {"text": "Transaction", "x0": 118, "top": 50},
            {"text": "Details", "x0": 190, "top": 50},
            {"text": "Deposit", "x0": 350, "top": 50},
            {"text": "Withdrawal", "x0": 418, "top": 50},
            {"text": "Balance", "x0": 500, "top": 50},
            {"text": "AUD", "x0": 59, "top": 70},
            {"text": "31", "x0": 79, "top": 70},
            {"text": "Dec", "x0": 85, "top": 70},
            {"text": "SYNTHETIC", "x0": 120, "top": 70},
            {"text": "TRANSFER", "x0": 120, "top": 90},
            {"text": "TO", "x0": 185, "top": 90},
            {"text": "HKD", "x0": 215, "top": 90},
            {"text": "Savings", "x0": 250, "top": 90},
            {"text": "5.00", "x0": 425, "top": 110},
        ]
        tables = [
            [
                ["Account: Foreign Currency Savings"],
                ["AUD B/F BALANCE 100.00"],
                ["31 Dec SYNTHETIC\nTRANSFER TO HKD Savings", "", "5.00"],
                ["AUD C/F BALANCE 95.00"],
            ]
        ]

        rows, warnings, _ = _import_fake_pdf(profile, tables=tables, words=words)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_id"], "hsbc_one_fcy_savings")
        self.assertEqual(
            rows[0]["original_description"], "SYNTHETIC TRANSFER TO HKD Savings"
        )
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "95.00")


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
        fixtures = _pdf_byte_fixtures(generator)
        self.assertEqual(set(review["fixtures"]), set(fixtures))
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
        for fixture_id, expected in review["fixtures"].items():
            with self.subTest(fixture=fixture_id):
                fixture_path = fixtures[fixture_id]
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

    def test_pdf_word_rows_reject_invalid_required_dates_before_identity(self) -> None:
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
            {"text": "not-a-date", "x0": 0, "top": 20},
            {"text": "Synthetic footer", "x0": 100, "top": 20},
            {"text": "1.00", "x0": 210, "top": 20},
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf_word_row_invalid_date: source=statement\.pdf; "
            r"page=1; row=2; field=transaction_date",
        ) as raised:
            _import_fake_pdf(profile, words=words)
        self.assertNotIn("profile=", str(raised.exception))
        self.assertNotIn("Post date", str(raised.exception))

    def test_pdf_word_rows_skip_footer_and_balance_before_date_validation(
        self,
    ) -> None:
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "skip_descriptions": ["Synthetic footer"],
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
            {"text": "2026-01-01", "x0": 0, "top": 20},
            {"text": "Coffee", "x0": 100, "top": 20},
            {"text": "-1.00", "x0": 210, "top": 20},
            {"text": "not-a-date", "x0": 0, "top": 30},
            {"text": "Synthetic footer", "x0": 100, "top": 30},
            {"text": "1.00", "x0": 210, "top": 30},
            {"text": "not-a-date", "x0": 0, "top": 40},
            {"text": "Closing balance", "x0": 100, "top": 40},
            {"text": "1.00", "x0": 210, "top": 40},
        ]

        rows, _, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual([row["merchant"] for row in rows], ["Coffee"])

    def test_pdf_word_end_marker_does_not_end_dated_transaction_row(self) -> None:
        legal_name = "The Hongkong and Shanghai Banking Corporation Limited"
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "word_rows": True,
                "word_header_markers": ["Post date", "Description", "Amount"],
                "word_table_end_markers": [legal_name, "Note:"],
                "word_columns": {
                    "Post date": [0, 90],
                    "Description": [90, 500],
                    "Amount": [500, 600],
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
            {"text": "Amount", "x0": 510, "top": 10},
            {"text": "2026-01-01", "x0": 0, "top": 20},
            {"text": legal_name, "x0": 100, "top": 20},
            {"text": "-1.00", "x0": 510, "top": 20},
        ]

        rows, _, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["merchant"], legal_name)

    def test_pdf_word_end_marker_matches_within_longer_footer_line(self) -> None:
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "word_rows": True,
                "word_header_markers": ["Post date", "Description", "Amount"],
                "word_table_end_markers": ["REWARDCASH"],
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
            {"text": "2026-01-01", "x0": 0, "top": 20},
            {"text": "Coffee", "x0": 100, "top": 20},
            {"text": "-1.00", "x0": 210, "top": 20},
            {"text": "REWARDCASH", "x0": 0, "top": 30},
            {"text": "SUMMARY", "x0": 100, "top": 30},
            {"text": "not-a-date", "x0": 0, "top": 40},
            {"text": "After footer", "x0": 100, "top": 40},
            {"text": "-2.00", "x0": 210, "top": 40},
        ]

        rows, _, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual([row["merchant"] for row in rows], ["Coffee"])

    def test_pdf_word_marker_description_with_malformed_date_is_rejected(
        self,
    ) -> None:
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "word_rows": True,
                "word_header_markers": ["Post date", "Description", "Amount"],
                "word_table_end_markers": ["REWARDCASH"],
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
            {"text": "not-a-date", "x0": 0, "top": 20},
            {"text": "REWARDCASH PURCHASE", "x0": 100, "top": 20},
            {"text": "-1.00", "x0": 210, "top": 20},
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf_word_row_invalid_date: source=statement\.pdf; "
            r"page=1; row=2; field=transaction_date",
        ):
            _import_fake_pdf(profile, words=words)

    def test_pdf_word_rows_allow_one_blank_mapped_date(self) -> None:
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "word_rows": True,
                "word_header_markers": [
                    "Post date",
                    "Trans date",
                    "Description",
                    "Amount",
                ],
                "word_columns": {
                    "Post date": [0, 90],
                    "Trans date": [90, 180],
                    "Description": [180, 290],
                    "Amount": [290, 380],
                },
                "columns": {
                    "transaction_date": "Trans date",
                    "posting_date": "Post date",
                    "description": "Description",
                    "amount": "Amount",
                },
            },
        }
        words = [
            {"text": "Post date", "x0": 0, "top": 10},
            {"text": "Trans date", "x0": 100, "top": 10},
            {"text": "Description", "x0": 190, "top": 10},
            {"text": "Amount", "x0": 300, "top": 10},
            {"text": "2026-01-01", "x0": 100, "top": 20},
            {"text": "Coffee", "x0": 190, "top": 20},
            {"text": "-1.00", "x0": 300, "top": 20},
        ]

        rows, _, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual(rows[0]["transaction_date"], "2026-01-01")
        self.assertEqual(rows[0]["posting_date"], "")

    def test_pdf_word_rows_reject_nonempty_malformed_date_when_other_is_valid(
        self,
    ) -> None:
        profile = {
            "id": "word",
            "account_id": "word",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "pdf": {
                "word_rows": True,
                "word_header_markers": [
                    "Post date",
                    "Trans date",
                    "Description",
                    "Amount",
                ],
                "word_columns": {
                    "Post date": [0, 90],
                    "Trans date": [90, 180],
                    "Description": [180, 290],
                    "Amount": [290, 380],
                },
                "columns": {
                    "transaction_date": "Trans date",
                    "posting_date": "Post date",
                    "description": "Description",
                    "amount": "Amount",
                },
            },
        }
        words = [
            {"text": "Post date", "x0": 0, "top": 10},
            {"text": "Trans date", "x0": 100, "top": 10},
            {"text": "Description", "x0": 190, "top": 10},
            {"text": "Amount", "x0": 300, "top": 10},
            {"text": "2026-01-02", "x0": 0, "top": 20},
            {"text": "not-a-date", "x0": 100, "top": 20},
            {"text": "Coffee", "x0": 190, "top": 20},
            {"text": "-1.00", "x0": 300, "top": 20},
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"pdf_word_row_invalid_date: source=statement\.pdf; "
            r"page=1; row=2; field=transaction_date",
        ):
            _import_fake_pdf(profile, words=words)

    def test_malformed_pdf_word_date_surfaces_parser_code_before_identity_persistence(
        self,
    ) -> None:
        profile = load_profile("hsbc_hk_credit_card_pdf.json")
        profile["id"] = "synthetic_word_profile"
        profile["pdf"] = dict(profile["pdf"])
        profile["pdf"]["word_table_end_markers"] = []
        fixture = (
            FIXTURE_DIR
            / "import_profiles"
            / "hsbc_hk_credit_card_pdf"
            / "footer_boundary"
            / "input.pdf"
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.pdf"
            statement.write_bytes(fixture.read_bytes())
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            output = root / "output" / "categorized.csv"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "paths": {"input": str(statement), "output": str(output)},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                main(["--config", str(config_path), "--no-interactive"]), 0
            )
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (root / "output" / ".honeymoney-identity-manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        warning = report["warnings"][0]
        self.assertIn("pdf_word_row_invalid_date", warning)
        self.assertNotIn("identity_manifest_invalid", warning)
        self.assertEqual(report["files"][0]["status"], "failed")
        self.assertEqual(manifest["sources"], [])

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


def _pdf_byte_fixtures(generator: Path) -> dict[str, Path]:
    namespace = runpy.run_path(str(generator))
    return {
        fixture.review_key: fixture.output_path for fixture in namespace["FIXTURES"]
    }


def _import_fake_pdf(
    profile: dict,
    *,
    tables: list | None = None,
    words: list | None = None,
    preview: bool = False,
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
            config = {"base_currency": "HKD", "exchange_rates": {"HKD": 1}}
            if preview:
                return _preview_profile_input(
                    profile, str(profile["id"]), statement, config
                )
            return _import_pdf(
                statement,
                profile,
                config,
                root,
                include_identity_records=True,
            )


if __name__ == "__main__":
    unittest.main()
