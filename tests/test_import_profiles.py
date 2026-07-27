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

    def test_foreign_section_survives_a_continuation_page_header(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        first_page = [
            {"text": "Statement", "x0": 20, "top": 10},
            {"text": "Date", "x0": 75, "top": 10},
            {"text": "05", "x0": 105, "top": 10},
            {"text": "January", "x0": 123, "top": 10},
            {"text": "2026", "x0": 153, "top": 10},
            {"text": "Foreign", "x0": 20, "top": 30},
            {"text": "Currency", "x0": 65, "top": 30},
            {"text": "Savings", "x0": 115, "top": 30},
            {"text": "Date", "x0": 10, "top": 50},
            {"text": "Transaction", "x0": 40, "top": 50},
            {"text": "Details", "x0": 110, "top": 50},
            {"text": "Deposit", "x0": 340, "top": 50},
            {"text": "Withdrawal", "x0": 418, "top": 50},
            {"text": "Balance", "x0": 490, "top": 50},
            {"text": "EUR", "x0": 59, "top": 70},
            {"text": "02", "x0": 79, "top": 70},
            {"text": "Jan", "x0": 85, "top": 70},
            {"text": "SYNTHETIC", "x0": 120, "top": 70},
            {"text": "CREDIT", "x0": 210, "top": 70},
            {"text": "10.00", "x0": 350, "top": 70},
        ]
        continuation_page = [
            {"text": "HSBC One HKD Current account summary", "x0": 20, "top": 10},
            {"text": "SERVICE", "x0": 120, "top": 30},
            {"text": "FEE", "x0": 170, "top": 30},
            {"text": "1.00", "x0": 425, "top": 30},
        ]

        rows, warnings, _ = _import_fake_pdf(
            profile,
            page_words=[first_page, continuation_page],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["account_id"] for row in rows},
            {"hsbc_one_fcy_savings"},
        )
        self.assertEqual({row["posted_currency"] for row in rows}, {"EUR"})

    def test_exact_heading_replaces_an_open_continuation_section(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        first_page = _hsbc_one_transaction_words(
            "Foreign Currency Savings", "EUR", "02"
        )
        continuation_page = [
            {"text": "HKD", "x0": 120, "top": 10},
            {"text": "Current", "x0": 170, "top": 10},
            {"text": "Date", "x0": 10, "top": 30},
            {"text": "Transaction", "x0": 40, "top": 30},
            {"text": "Details", "x0": 110, "top": 30},
            {"text": "Deposit", "x0": 340, "top": 30},
            {"text": "Withdrawal", "x0": 418, "top": 30},
            {"text": "Balance", "x0": 490, "top": 30},
            {"text": "03", "x0": 79, "top": 50},
            {"text": "Jan", "x0": 85, "top": 50},
            {"text": "SYNTHETIC", "x0": 120, "top": 50},
            {"text": "CREDIT", "x0": 210, "top": 50},
            {"text": "5.00", "x0": 350, "top": 50},
        ]

        rows, warnings, _ = _import_fake_pdf(
            profile,
            page_words=[first_page, continuation_page],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [row["account_id"] for row in rows],
            ["hsbc_one_fcy_savings", "hsbc_one_hkd_current"],
        )
        self.assertEqual(
            [row["posted_currency"] for row in rows],
            ["EUR", "HKD"],
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

    def test_generic_information_boundary_stops_import_and_preview(self) -> None:
        profile = load_profile("hsbc_hk_credit_card_pdf.json")
        words = [
            {"text": "Post date", "x0": 50, "top": 10},
            {"text": "Trans date", "x0": 95, "top": 10},
            {"text": "Description", "x0": 135, "top": 10},
            {"text": "Amount", "x0": 490, "top": 10},
            {"text": "01JAN", "x0": 50, "top": 20},
            {"text": "01JAN", "x0": 95, "top": 20},
            {"text": "SYNTHETIC MERCHANT", "x0": 135, "top": 20},
            {"text": "10.00", "x0": 490, "top": 20},
            {"text": "For", "x0": 50, "top": 30},
            {"text": "important information", "x0": 135, "top": 30},
            {"text": "02JAN", "x0": 50, "top": 40},
            {"text": "02JAN", "x0": 95, "top": 40},
            {"text": "AFTER BOUNDARY", "x0": 135, "top": 40},
            {"text": "20.00", "x0": 490, "top": 40},
        ]

        imported_rows, imported_warnings, _ = _import_fake_pdf(profile, words=words)
        preview_rows, preview_warnings = _import_fake_pdf(
            profile, words=words, preview=True
        )

        self.assertEqual(imported_warnings, [])
        self.assertEqual(preview_warnings, [])
        self.assertEqual(
            [row["merchant"] for row in imported_rows],
            ["SYNTHETIC MERCHANT"],
        )
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

    def test_foreign_purchase_without_rate_line_settles_in_hkd(self) -> None:
        rows, warnings, _ = _import_fake_pdf(
            load_profile("mox_credit_card_pdf.json"),
            tables=[[["17 May 18 May SYNTHETIC FOREIGN PURCHASE -10.00 USD -79.80"]]],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["original_amount"], "-10.00")
        self.assertEqual(rows[0]["original_currency"], "USD")
        self.assertEqual(rows[0]["posted_amount"], "-79.80")
        self.assertEqual(rows[0]["posted_currency"], "HKD")
        self.assertEqual(rows[0]["amount_hkd"], "-79.80")
        self.assertEqual(rows[0]["valuation_source"], "statement_posted")
        self.assertEqual(rows[0]["valuation_status"], "actual")


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

    def test_dynamic_section_and_account_target_conflict_at_runtime(self) -> None:
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

    def test_static_section_and_account_targets_cannot_collapse(self) -> None:
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

    def test_section_and_account_targets_can_use_distinct_currencies(self) -> None:
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
                "currency": "USD",
                "opening_regex": r"^OPEN (?P<balance>\d+\.\d{2})$",
                "closing_regex": r"^CLOSE (?P<balance>\d+\.\d{2})$",
            },
        ]

        _validate_profile(profile, Path("profile.json"), base_config())


class PdfBalanceReconciliationTest(unittest.TestCase):
    def test_mox_balance_rows_allow_account_text_between_label_and_amount(
        self,
    ) -> None:
        profile = load_profile("mox_bank_pdf.json")
        observations = _balance_observations_from_table_pages(
            profile,
            [
                [
                    "01 Apr 01 Apr OPENING BALANCE MAIN ACCOUNT HKD 100.00",
                    "30 Apr 30 Apr CLOSING BALANCE MAIN ACCOUNT HKD 110.00",
                ]
            ],
        )

        self.assertEqual(
            observations[("mox_bank_main", "", "HKD")],
            {
                "opening": [Decimal("100.00")],
                "closing": [Decimal("110.00")],
            },
        )
        rows, warnings, _ = _import_fake_pdf(
            profile,
            tables=[
                [
                    ["01 Apr 01 Apr OPENING BALANCE MAIN ACCOUNT HKD 100.00"],
                    ["02 Apr 02 Apr SYNTHETIC CREDIT +10.00"],
                    ["30 Apr 30 Apr CLOSING BALANCE MAIN ACCOUNT HKD 110.00"],
                ]
            ],
        )
        statement = reconcile_ledger(rows, {})["balance_reconciliation"][
            "mox_bank_main"
        ]["statements"][0]

        self.assertEqual(warnings, [])
        self.assertEqual(statement["result"], "matched")

    def test_hsbc_one_section_currency_endpoints_ignore_portfolio_total(
        self,
    ) -> None:
        observations = _balance_observations_from_table_pages(
            load_profile("hsbc_one_pdf.json"),
            [
                [
                    "Foreign Currency Savings",
                    "EUR 01 Apr B/F BALANCE 100.00",
                    "TOTAL BALANCE 110.00 EUR",
                    "TOTAL BALANCE 999.00 FOREIGN CURRENCY",
                ]
            ],
        )

        self.assertEqual(
            observations,
            {
                ("hsbc_one_fcy_savings", "Foreign Currency Savings", "EUR"): {
                    "opening": [Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                }
            },
        )

    def test_hsbc_one_total_balances_reconcile_by_section_and_currency(
        self,
    ) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        cases = (
            (
                "HKD Savings",
                "HKD",
                "hsbc_one_hkd_savings",
                [
                    ["HKD Savings"],
                    ["01 Apr B/F BALANCE 100.00"],
                    ["TOTAL BALANCE 110.00 HKD"],
                ],
            ),
            (
                "Foreign Currency Savings",
                "EUR",
                "hsbc_one_fcy_savings",
                [
                    ["Foreign Currency Savings"],
                    ["EUR 01 Apr B/F BALANCE 100.00"],
                    ["TOTAL BALANCE 110.00 EUR"],
                    ["TOTAL BALANCE 999.00 FOREIGN CURRENCY"],
                ],
            ),
        )
        for section, currency, account_id, table in cases:
            with self.subTest(section=section):
                rows, warnings, _ = _import_fake_pdf(
                    profile,
                    tables=[table],
                    words=_hsbc_one_transaction_words(section, currency, "02"),
                )
                statement = reconcile_ledger(rows, {})["balance_reconciliation"][
                    account_id
                ]["statements"][0]

                self.assertEqual(warnings, [])
                self.assertEqual(statement["result"], "matched")
                self.assertEqual(statement["opening_balance"], "100.00")
                self.assertEqual(statement["closing_balance"], "110.00")

    def test_hsbc_one_sections_with_one_account_keep_distinct_balances(
        self,
    ) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        accounts = profile["pdf"]["sectioned_word_rows"]["accounts"]
        accounts["HKD Savings"]["account_id"] = "hsbc_shared_hkd"
        accounts["HKD Current"]["account_id"] = "hsbc_shared_hkd"
        _validate_profile(profile, Path("profile.json"), base_config())
        page_tables = [
            [
                [
                    ["HKD Savings"],
                    ["B/F BALANCE 100.00"],
                    ["C/F BALANCE 110.00"],
                ]
            ],
            [
                [
                    ["HKD Current"],
                    ["B/F BALANCE 200.00"],
                    ["C/F BALANCE 210.00"],
                ]
            ],
        ]
        page_words = [
            _hsbc_one_transaction_words("HKD Savings", "HKD", "02"),
            _hsbc_one_transaction_words("HKD Current", "HKD", "03"),
        ]

        rows, warnings, _ = _import_fake_pdf(
            profile,
            page_tables=page_tables,
            page_words=page_words,
        )
        preview_rows, preview_warnings = _import_fake_pdf(
            profile,
            page_tables=page_tables,
            page_words=page_words,
            preview=True,
        )
        statements = reconcile_ledger(rows, {})["balance_reconciliation"][
            "hsbc_shared_hkd"
        ]["statements"]

        self.assertEqual(warnings, [])
        self.assertEqual(preview_warnings, warnings)
        self.assertEqual(
            [
                (
                    row["account_id"],
                    row["statement_section"],
                    row["statement_opening_balance"],
                    row["statement_closing_balance"],
                )
                for row in preview_rows
            ],
            [
                (
                    row["account_id"],
                    row["statement_section"],
                    row["statement_opening_balance"],
                    row["statement_closing_balance"],
                )
                for row in rows
            ],
        )
        self.assertEqual(
            [row["statement_section"] for row in rows],
            ["HKD Savings", "HKD Current"],
        )
        self.assertEqual(
            [
                (
                    row["statement_opening_balance"],
                    row["statement_closing_balance"],
                )
                for row in rows
            ],
            [("100.00", "110.00"), ("200.00", "210.00")],
        )
        self.assertEqual(
            [statement["statement_section"] for statement in statements],
            ["HKD Current", "HKD Savings"],
        )
        self.assertEqual(
            [statement["result"] for statement in statements],
            ["matched", "matched"],
        )

    def test_sectioned_profile_keeps_account_targeted_balances(self) -> None:
        profile = load_profile("hsbc_one_pdf.json")
        mapping = profile["pdf"]["balance_mappings"][0]
        mapping["account_id"] = "hsbc_one_hkd_savings"
        mapping.pop("section")
        _validate_profile(profile, Path("profile.json"), base_config())

        rows, warnings, _ = _import_fake_pdf(
            profile,
            tables=[
                [
                    ["HKD Savings"],
                    ["B/F BALANCE 100.00"],
                    ["C/F BALANCE 110.00"],
                ]
            ],
            words=_hsbc_one_transaction_words("HKD Savings", "HKD", "02"),
        )

        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["statement_section"], "HKD Savings")
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_positive_credit_liability_balances_reconcile_against_signed_rows(
        self,
    ) -> None:
        rows = [
            {
                "transaction_id": "txn_liability",
                "source_file": "synthetic.pdf",
                "account_id": "synthetic_card",
                "account_type": "credit_card",
                "posted_currency": "HKD",
                "posted_amount": "-25.00",
                "amount_hkd": "-25.00",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "125.00",
                "flags": "",
            }
        ]

        statement = reconcile_ledger(rows, {})["balance_reconciliation"][
            "synthetic_card"
        ]["statements"][0]

        self.assertEqual(statement["result"], "matched")
        self.assertEqual(statement["calculated_closing_balance"], "125.00")

    def test_one_statement_balance_endpoint_remains_unavailable(self) -> None:
        rows = [
            {
                "transaction_id": "txn_one_endpoint",
                "source_file": "synthetic.pdf",
                "account_id": "synthetic_bank",
                "account_type": "bank",
                "posted_currency": "HKD",
                "posted_amount": "10.00",
                "amount_hkd": "10.00",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "",
                "flags": "",
            }
        ]

        statement = reconcile_ledger(rows, {})["balance_reconciliation"][
            "synthetic_bank"
        ]["statements"][0]

        self.assertEqual(statement["result"], "unavailable")
        self.assertEqual(statement["reason"], "Closing balance is unavailable.")

    def test_multi_page_balance_rollovers_keep_statement_endpoints(self) -> None:
        cases = (
            (
                "mox_bank_pdf",
                "mox_bank_main",
                "Opening Balance 100.00",
                "Closing Balance 110.00",
                "Opening Balance 110.00",
                "Closing Balance 120.00",
                Decimal("100.00"),
                Decimal("120.00"),
            ),
            (
                "mox_credit_card_pdf",
                "mox_credit_card",
                "Opening Balance 100.00 DR",
                "Closing Balance 110.00 DR",
                "Opening Balance 110.00 DR",
                "Statement Balance 120.00 DR",
                Decimal("-100.00"),
                Decimal("-120.00"),
            ),
            (
                "hsbc_hk_credit_card_pdf",
                "hsbc_hk_credit_card",
                "Previous Balance 100.00",
                "Closing Balance 110.00",
                "Opening Balance 110.00",
                "Statement Balance 120.00",
                Decimal("100.00"),
                Decimal("120.00"),
            ),
        )
        for (
            profile_id,
            account_id,
            first_open,
            first_close,
            second_open,
            second_close,
            expected_open,
            expected_close,
        ) in cases:
            with self.subTest(profile=profile_id):
                observations = _balance_observations_from_table_pages(
                    load_profile(f"{profile_id}.json"),
                    [
                        [first_open, first_close],
                        [second_open, second_close],
                    ],
                )
                self.assertEqual(
                    observations[(account_id, "", "HKD")],
                    {
                        "opening": [expected_open],
                        "closing": [expected_close],
                    },
                )

    def test_multi_page_import_attaches_one_opening_and_closing_balance(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        rows, warnings, _ = _import_fake_pdf(
            profile,
            page_tables=[
                [
                    [
                        ["Opening Balance 100.00"],
                        ["01 Apr 01 Apr SYNTHETIC CREDIT +10.00"],
                        ["Closing Balance 110.00"],
                    ]
                ],
                [
                    [
                        ["Opening Balance 110.00"],
                        ["02 Apr 02 Apr SYNTHETIC CREDIT +10.00"],
                        ["Closing Balance 120.00"],
                    ]
                ],
            ],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "")
        self.assertEqual(rows[-1]["statement_opening_balance"], "")
        self.assertEqual(rows[-1]["statement_closing_balance"], "120.00")
        statement = reconcile_ledger(rows, {})["balance_reconciliation"][
            "mox_bank_main"
        ]["statements"][0]
        self.assertEqual(statement["result"], "matched")

    def test_broken_rollover_and_missing_final_close_remain_unavailable(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        broken = _balance_observations_from_table_pages(
            profile,
            [
                ["Opening Balance 100.00", "Closing Balance 110.00"],
                ["Opening Balance 111.00", "Closing Balance 120.00"],
            ],
        )
        missing_close = _balance_observations_from_table_pages(
            profile,
            [
                ["Opening Balance 100.00", "Closing Balance 110.00"],
                ["Opening Balance 110.00"],
            ],
        )

        self.assertEqual(
            broken[("mox_bank_main", "", "HKD")]["opening"],
            [Decimal("100.00"), Decimal("111.00")],
        )
        self.assertEqual(
            missing_close[("mox_bank_main", "", "HKD")]["opening"],
            [Decimal("100.00"), Decimal("110.00")],
        )
        self.assertEqual(
            missing_close[("mox_bank_main", "", "HKD")]["closing"],
            [Decimal("110.00")],
        )

    def test_hsbc_one_accepts_balance_first_multi_currency_labels(self) -> None:
        observations = _balance_observations_from_table_pages(
            load_profile("hsbc_one_pdf.json"),
            [
                [
                    "Foreign Currency Savings",
                    "AUD Balance B/F 100.00",
                    "AUD Balance C/F 110.00",
                    "EUR B/F Balance 50.00",
                    "EUR C/F Balance 55.00",
                ]
            ],
        )

        self.assertEqual(
            observations,
            {
                ("hsbc_one_fcy_savings", "Foreign Currency Savings", "AUD"): {
                    "opening": [Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
                ("hsbc_one_fcy_savings", "Foreign Currency Savings", "EUR"): {
                    "opening": [Decimal("50.00")],
                    "closing": [Decimal("55.00")],
                },
            },
        )

    def test_table_section_context_does_not_leak_across_pages(self) -> None:
        observations = _hsbc_one_split_word_table_observations()

        self.assertEqual(
            observations,
            {
                ("hsbc_one_hkd_savings", "HKD Savings", "HKD"): {
                    "opening": [Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
                ("hsbc_one_hkd_current", "HKD Current", "HKD"): {
                    "opening": [Decimal("200.00")],
                    "closing": [Decimal("210.00")],
                },
            },
        )

    def test_exact_section_heading_recovers_after_missing_prior_close(self) -> None:
        observations = _balance_observations_from_table_pages(
            load_profile("hsbc_one_pdf.json"),
            [
                [
                    "HKD Savings",
                    "BALANCE B/F 100.00",
                    "HKD Current",
                    "BALANCE B/F 200.00",
                    "BALANCE C/F 210.00",
                ]
            ],
        )

        self.assertEqual(
            observations,
            {
                ("hsbc_one_hkd_savings", "HKD Savings", "HKD"): {
                    "opening": [Decimal("100.00")],
                    "closing": [],
                },
                ("hsbc_one_hkd_current", "HKD Current", "HKD"): {
                    "opening": [Decimal("200.00")],
                    "closing": [Decimal("210.00")],
                },
            },
        )

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
        page_tables = [
            [
                [
                    ["01 Apr 01 Apr OPENING BALANCE +100.00"],
                    ["01 Apr 02 Apr SYNTHETIC CREDIT +10.00"],
                    ["15 Apr 15 Apr CLOSING BALANCE +110.00"],
                ]
            ],
            [
                [
                    ["16 Apr 16 Apr OPENING BALANCE +101.00"],
                    ["16 Apr 17 Apr SYNTHETIC CREDIT +10.00"],
                    ["30 Apr 30 Apr CLOSING BALANCE +110.00"],
                ]
            ],
        ]

        rows, warnings, _ = _import_fake_pdf(profile, page_tables=page_tables)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["statement_opening_balance"], "")
        self.assertIn("statement_opening_balance_conflict", rows[0]["flags"])
        report = reconcile_ledger(rows, {})["balance_reconciliation"]["mox_bank_main"]
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["result"], "unavailable")
        self.assertEqual(
            report["statements"][0]["reason"], "Opening balances conflict."
        )
        self.assertEqual(
            report["statements"][0]["conflicts"],
            [
                {
                    "source_file": "statement.pdf",
                    "source_page": "1",
                    "statement_section": "",
                    "field": "statement_opening_balance",
                },
                {
                    "source_file": "statement.pdf",
                    "source_page": "2",
                    "statement_section": "",
                    "field": "statement_opening_balance",
                },
            ],
        )
        self.assertNotIn(
            "100.00",
            json.dumps(report["statements"][0]["conflicts"], sort_keys=True),
        )
        self.assertNotIn(
            "101.00",
            json.dumps(report["statements"][0]["conflicts"], sort_keys=True),
        )

    def test_mapped_endpoint_conflicts_include_safe_row_context(self) -> None:
        rows = [
            {
                "transaction_id": f"txn_{index}",
                "source_file": "private/statements/synthetic.csv",
                "source_page": str(index),
                "account_id": "synthetic_bank",
                "account_type": "bank",
                "posted_currency": "HKD",
                "posted_amount": "10.00",
                "amount_hkd": "10.00",
                "statement_opening_balance": opening,
                "statement_closing_balance": "120.00",
                "flags": "",
            }
            for index, opening in ((4, "100.00"), (5, "101.00"))
        ]

        statement = reconcile_ledger(rows, {})["balance_reconciliation"][
            "synthetic_bank"
        ]["statements"][0]

        self.assertEqual(statement["reason"], "Opening balances conflict.")
        self.assertEqual(
            statement["conflicts"],
            [
                {
                    "source_file": "synthetic.csv",
                    "source_page": "4",
                    "statement_section": "",
                    "field": "statement_opening_balance",
                },
                {
                    "source_file": "synthetic.csv",
                    "source_page": "5",
                    "statement_section": "",
                    "field": "statement_opening_balance",
                },
            ],
        )
        self.assertNotIn("private/", json.dumps(statement["conflicts"]))

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
                ("hsbc_one_hkd_savings", "HKD Savings", "HKD"): {
                    "opening": [Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
                ("hsbc_one_hkd_current", "HKD Current", "HKD"): {
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
                ("hsbc_one_hkd_savings", "HKD Savings", "HKD"): {
                    "opening": [Decimal("100.00"), Decimal("100.00")],
                    "closing": [Decimal("110.00")],
                },
                ("hsbc_one_hkd_current", "HKD Current", "HKD"): {
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

    def test_section_end_marker_in_description_keeps_account_context(self) -> None:
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
            {"text": "30", "x0": 79, "top": 90},
            {"text": "Dec", "x0": 85, "top": 90},
            {"text": "SYNTHETIC", "x0": 120, "top": 90},
            {"text": "END", "x0": 185, "top": 90},
            {"text": "SECTION", "x0": 220, "top": 90},
            {"text": "DEBIT", "x0": 275, "top": 90},
            {"text": "5.00", "x0": 425, "top": 90},
            {"text": "AUD", "x0": 59, "top": 110},
            {"text": "31", "x0": 79, "top": 110},
            {"text": "Dec", "x0": 85, "top": 110},
            {"text": "SYNTHETIC", "x0": 120, "top": 110},
            {"text": "LATER", "x0": 185, "top": 110},
            {"text": "DEBIT", "x0": 230, "top": 110},
            {"text": "2.00", "x0": 425, "top": 110},
            {"text": "AUD", "x0": 59, "top": 130},
            {"text": "C/F", "x0": 120, "top": 130},
            {"text": "BALANCE", "x0": 145, "top": 130},
            {"text": "93.00", "x0": 500, "top": 130},
        ]

        rows, warnings, _ = _import_fake_pdf(profile, words=words)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row["original_description"] for row in rows],
            ["SYNTHETIC END SECTION DEBIT", "SYNTHETIC LATER DEBIT"],
        )
        self.assertEqual({row["account_id"] for row in rows}, {"hsbc_one_fcy_savings"})
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[-1]["statement_closing_balance"], "93.00")

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


def _hsbc_one_transaction_words(
    section: str, currency: str, day: str
) -> list[dict[str, object]]:
    words: list[dict[str, object]] = [
        {"text": "Statement", "x0": 20, "top": 10},
        {"text": "Date", "x0": 75, "top": 10},
        {"text": "05", "x0": 105, "top": 10},
        {"text": "January", "x0": 123, "top": 10},
        {"text": "2026", "x0": 153, "top": 10},
    ]
    x0 = 20
    for part in section.split():
        words.append({"text": part, "x0": x0, "top": 30})
        x0 += len(part) * 7 + 5
    words.extend(
        [
            {"text": "Date", "x0": 10, "top": 50},
            {"text": "Transaction", "x0": 40, "top": 50},
            {"text": "Details", "x0": 110, "top": 50},
            {"text": "Deposit", "x0": 340, "top": 50},
            {"text": "Withdrawal", "x0": 418, "top": 50},
            {"text": "Balance", "x0": 490, "top": 50},
        ]
    )
    if currency != "HKD":
        words.append({"text": currency, "x0": 59, "top": 70})
    words.extend(
        [
            {"text": day, "x0": 79, "top": 70},
            {"text": "Jan", "x0": 85, "top": 70},
            {"text": "SYNTHETIC", "x0": 120, "top": 70},
            {"text": "CREDIT", "x0": 210, "top": 70},
            {"text": "10.00", "x0": 350, "top": 70},
        ]
    )
    return words


def _balance_observations_from_table_pages(
    profile: dict,
    pages: list[list[str]],
) -> dict[tuple[str, str, str], dict[str, list[Decimal]]]:
    class Page:
        def __init__(self, lines: list[str]):
            self.lines = lines

        def extract_words(self, **kwargs):
            return []

        def extract_tables(self):
            return [[[line] for line in self.lines]]

    class Pdf:
        def __init__(self):
            self.pages = [Page(lines) for lines in pages]

    return _pdf_balance_observations(Pdf(), profile["pdf"])


def _hsbc_one_split_word_table_observations() -> dict[
    tuple[str, str, str], dict[str, list[Decimal]]
]:
    def words(lines: list[str]) -> list[dict[str, object]]:
        return [
            {"text": line, "x0": 20, "top": index * 20}
            for index, line in enumerate(lines, start=1)
        ]

    class Page:
        def __init__(self, word_lines: list[str], table_lines: list[str]):
            self.word_lines = word_lines
            self.table_lines = table_lines

        def extract_words(self, **kwargs):
            return words(self.word_lines)

        def extract_tables(self):
            return [[[line] for line in self.table_lines]]

    class Pdf:
        pages = [
            Page(
                [
                    "HKD Savings",
                    "B/F Balance 100.00",
                    "C/F Balance 110.00",
                ],
                [
                    "HKD Savings",
                    "B/F Balance 100.00",
                    "C/F Balance 110.00",
                ],
            ),
            Page(
                [
                    "HKD Current",
                    "B/F Balance 200.00",
                    "C/F Balance 210.00",
                ],
                [
                    "B/F Balance 200.00",
                    "C/F Balance 210.00",
                ],
            ),
        ]

    profile = load_profile("hsbc_one_pdf.json")
    return _pdf_balance_observations(Pdf(), profile["pdf"])


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
    page_tables: list[list] | None = None,
    page_words: list[list] | None = None,
    preview: bool = False,
):
    class Page:
        def __init__(
            self,
            page_tables_value: list | None = None,
            page_words_value: list | None = None,
        ):
            self.page_tables = page_tables_value
            self.page_words = page_words_value

        def extract_tables(self):
            return self.page_tables if self.page_tables is not None else tables or []

        def extract_table(self):
            extracted = self.extract_tables()
            return (extracted or [None])[0]

        def extract_words(self, **kwargs):
            return self.page_words if self.page_words is not None else words or []

    class Pdf:
        pages = (
            [
                Page(
                    page_tables_value=(
                        page_tables[index] if page_tables is not None else []
                    ),
                    page_words_value=(
                        page_words[index] if page_words is not None else []
                    ),
                )
                for index in range(
                    max(
                        len(page_tables) if page_tables is not None else 0,
                        len(page_words) if page_words is not None else 0,
                    )
                )
            ]
            if page_tables is not None or page_words is not None
            else [Page()]
        )

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
