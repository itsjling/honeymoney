import copy
import unittest
from unittest.mock import patch

from honeymoney.identity import ADAPTER_VERSIONS, extractor_contract_id
from tests.golden_helpers import REPO_ROOT, load_json, load_profile
from tests.test_import_profiles import _import_fake_pdf


def line(top, *cells):
    return [{"text": text, "x0": x, "top": top} for x, text in cells]


def start(section="HKD Savings"):
    return (
        line(10, (20, "Statement Date 05 January 2026"))
        + line(30, (20, section))
        + line(50, (10, "Date Transaction Details Deposit Withdrawal Balance"))
    )


def opening(top=70, currency="", balance="100.00"):
    return line(
        top, (55, currency), (82, "01 Jan"), (120, "B/F BALANCE"), (490, balance)
    )


def transaction(top=90, currency="", balance="110.00", amount="10.00"):
    cells = [(55, currency), (82, "02 Jan"), (120, "SYNTHETIC CREDIT"), (350, amount)]
    if balance:
        cells.append((490, balance))
    return line(top, *cells)


class HsbcRunningBalanceTest(unittest.TestCase):
    def parse(self, pages, profile=None):
        rows, warnings, identities = _import_fake_pdf(
            profile or load_profile("hsbc_one_pdf.json"), page_words=pages
        )
        self.assertEqual(warnings, [])
        return rows, identities

    def test_changed_section_end_semantics_have_a_new_extractor_contract(self):
        profile = load_profile("hsbc_one_pdf.json")
        with patch.dict(ADAPTER_VERSIONS, {4: "pdf-sectioned-v1"}):
            old_contract = extractor_contract_id(4, profile)
        self.assertNotEqual(extractor_contract_id(4, profile), old_contract)

    def test_example_profile_matches_bundled_profile(self):
        self.assertEqual(
            load_json(REPO_ROOT / "examples/profiles/hsbc_one_pdf.json"),
            load_profile("hsbc_one_pdf.json"),
        )

    def test_deposit_plus_ends_foreign_currency_transactions(self):
        for active in (False, True):
            with self.subTest(active=active):
                words = start("Foreign Currency Savings") + opening(currency="EUR")
                if active:
                    words += transaction(currency="EUR")
                words += (
                    line(130, (73, "Deposit"), (103, "Plus"))
                    + line(150, (120, "SYNTHETIC INVESTMENT"), (350, "700.00"))
                    + line(170, (120, "MATURITY"), (425, "800.00"))
                )
                rows, _ = self.parse([words])
                self.assertEqual(len(rows), int(active))
                if active:
                    self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_notices_end_table_without_erasing_printed_endpoint(self):
        for heading in ("Total Relationship Balance", "Important Notice"):
            with self.subTest(heading=heading):
                rows, _ = self.parse(
                    [
                        start()
                        + opening()
                        + transaction()
                        + line(130, (73, heading))
                        + line(150, (120, "SYNTHETIC FEE NOTICE"), (350, "300.00"))
                    ]
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_end_heading_stops_balance_scanner_until_next_account(self):
        rows, _ = self.parse(
            [
                start()
                + opening()
                + transaction()
                + line(130, (73, "Deposit Plus"))
                + opening(150, balance="900.00")
                + line(170, (120, "C/F BALANCE"), (490, "999.00"))
                + line(190, (20, "HKD Current"))
                + line(210, (10, "Date Transaction Details Deposit Withdrawal Balance"))
                + opening(230)
                + transaction(250)
            ]
        )
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["statement_opening_balance"], "100.00")
            self.assertEqual(row["statement_closing_balance"], "110.00")

    def test_exact_end_heading_in_wrapped_description_keeps_transaction(self):
        for heading in (
            "Deposit Plus",
            "Total Relationship Balance",
            "Important Notice",
        ):
            with self.subTest(heading=heading):
                words = (
                    start()
                    + opening()
                    + line(90, (120, heading))
                    + line(
                        110,
                        (82, "02 Jan"),
                        (120, "CONTINUED"),
                        (350, "10.00"),
                        (490, "110.00"),
                    )
                    + line(130, (120, "C/F BALANCE"), (490, "110.00"))
                )
                rows, _ = self.parse([words])
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    rows[0]["original_description"], heading + " CONTINUED"
                )
                self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
                self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_end_heading_in_pending_transaction_continuation_keeps_transaction(self):
        rows, _ = self.parse(
            [
                start()
                + opening()
                + line(90, (82, "02 Jan"), (120, "CARD PURCHASE"))
                + line(110, (120, "Deposit Plus"))
                + line(130, (120, "MERCHANT"), (350, "10.00"), (490, "110.00"))
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["original_description"], "CARD PURCHASE Deposit Plus MERCHANT"
        )
        self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_wrapped_heading_keeps_explicit_closing_balance_conflicts(self):
        rows, _ = self.parse(
            [
                start()
                + opening()
                + line(90, (120, "Deposit Plus"))
                + transaction(110)
                + line(130, (120, "C/F BALANCE"), (490, "120.00"))
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("statement_closing_balance_conflict", rows[0]["flags"])
        self.assertEqual(rows[0]["statement_closing_balance"], "")

    def test_table_end_heading_does_not_contaminate_word_balances(self):
        rows, warnings, _ = _import_fake_pdf(
            load_profile("hsbc_one_pdf.json"),
            words=start() + opening() + transaction(),
            tables=[
                [
                    ["HKD Savings"],
                    ["Date Transaction Details Deposit Withdrawal Balance"],
                    ["B/F BALANCE 100.00"],
                    ["Deposit Plus"],
                    ["B/F BALANCE 900.00"],
                    ["C/F BALANCE 999.00"],
                ]
            ],
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_table_description_cells_keep_explicit_balance_evidence(self):
        for marker_row in (["", "Deposit Plus"], ["CARD PURCHASE\nDeposit Plus"]):
            with self.subTest(marker_row=marker_row):
                rows, warnings, _ = _import_fake_pdf(
                    load_profile("hsbc_one_pdf.json"),
                    words=start() + opening() + transaction(),
                    tables=[
                        [
                            ["HKD Savings"],
                            ["Date Transaction Details Deposit Withdrawal Balance"],
                            ["B/F BALANCE 100.00"],
                            marker_row,
                            ["C/F BALANCE 120.00"],
                        ]
                    ],
                )
                self.assertEqual(warnings, [])
                self.assertEqual(len(rows), 1)
                self.assertIn("statement_closing_balance_conflict", rows[0]["flags"])
                self.assertEqual(rows[0]["statement_closing_balance"], "")

    def test_heading_words_in_transaction_description_do_not_end_table(self):
        rows, _ = self.parse(
            [
                start()
                + opening()
                + line(
                    90,
                    (82, "02 Jan"),
                    (120, "Deposit Plus"),
                    (350, "10.00"),
                    (490, "110.00"),
                )
                + transaction(110, balance="120.00")
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["statement_closing_balance"], "120.00")

    def test_printed_endpoint_preserves_wrapped_facts_and_physical_refs(self):
        words = (
            start()
            + opening()
            + line(90, (82, "02 Jan"), (120, "WRAPPED DESCRIPTION"))
            + line(110, (120, "CONTINUED"), (350, "10.00"), (490, "110.00"))
        )
        profile = load_profile("hsbc_one_pdf.json")
        old_profile = copy.deepcopy(profile)
        del old_profile["pdf"]["sectioned_word_rows"]["columns"]["balance"]
        before, before_ids = self.parse([words], old_profile)
        after, after_ids = self.parse([words], profile)
        self.assertEqual(after[0]["statement_closing_balance"], "110.00")
        self.assertEqual(
            after[0]["original_description"], "WRAPPED DESCRIPTION CONTINUED"
        )
        after[0]["statement_closing_balance"] = ""
        self.assertEqual(before, after)
        self.assertEqual(before_ids, after_ids)

    def test_missing_or_invalid_final_balance_never_reuses_previous_balance(self):
        for balance in ("", "not-a-balance", "110.00 120.00"):
            with self.subTest(balance=balance):
                rows, _ = self.parse(
                    [
                        start()
                        + opening()
                        + transaction()
                        + transaction(110, balance=balance)
                    ]
                )
                self.assertEqual(len(rows), 2)
                self.assertTrue(
                    all(not row["statement_closing_balance"] for row in rows)
                )

    def test_currency_account_and_inactive_currency_boundaries(self):
        words = (
            start("Foreign Currency Savings")
            + opening(currency="EUR")
            + transaction(currency="EUR")
            + opening(110, "USD", "0.00")
            + line(130, (20, "HKD Current"))
            + line(150, (10, "Date Transaction Details Deposit Withdrawal Balance"))
            + opening(170)
            + transaction(190, balance="120.00", amount="20.00")
        )
        rows, _ = self.parse([words])
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["posted_currency"] for row in rows], ["EUR", "HKD"])
        self.assertEqual(
            [row["statement_closing_balance"] for row in rows], ["110.00", "120.00"]
        )

    def test_only_final_transaction_across_pages_supplies_endpoint(self):
        first = start() + opening() + transaction()
        second = start() + transaction(balance="120.00")
        rows, _ = self.parse([first, second])
        self.assertEqual(
            [row["statement_closing_balance"] for row in rows], ["", "120.00"]
        )
        second = start() + transaction(balance="")
        rows, _ = self.parse([first, second])
        self.assertTrue(all(not row["statement_closing_balance"] for row in rows))

    def test_explicit_endpoint_agreement_and_conflict(self):
        for balance, conflict in (("110.00", False), ("120.00", True)):
            with self.subTest(balance=balance):
                rows, _ = self.parse(
                    [
                        start()
                        + opening()
                        + transaction()
                        + line(110, (120, "C/F BALANCE"), (490, balance))
                    ]
                )
                self.assertEqual(
                    rows[0]["statement_closing_balance"], "" if conflict else "110.00"
                )
                self.assertEqual(
                    "statement_closing_balance_conflict" in rows[0]["flags"], conflict
                )
                if conflict:
                    self.assertIn(
                        "statement_closing_balance_conflict_page_1", rows[0]["flags"]
                    )

    def test_zero_and_debit_balances_are_printed_facts(self):
        for printed, expected in (
            ("0.00", "0.00"),
            ("10.00 DR", "-10.00"),
            ("10.00DR", "-10.00"),
            ("10.00 CR", "10.00"),
        ):
            with self.subTest(printed=printed):
                rows, _ = self.parse(
                    [start() + opening() + transaction(balance=printed)]
                )
                self.assertEqual(rows[0]["statement_closing_balance"], expected)

    def test_no_opening_or_wrong_column_leaves_endpoint_unavailable(self):
        rows, _ = self.parse([start() + transaction()])
        self.assertEqual(rows[0]["statement_closing_balance"], "")
        rows, _ = self.parse(
            [start() + opening() + transaction(balance="") + line(110, (120, "110.00"))]
        )
        self.assertEqual(rows[0]["statement_closing_balance"], "")

    def test_account_only_table_opening_uses_unique_section_running_balance(self):
        profile = load_profile("hsbc_one_pdf.json")
        profile["pdf"]["balance_mappings"] = [
            {
                "account_id": "hsbc_one_hkd_savings",
                "currency": "HKD",
                "opening_regex": r"OPENING (?P<balance>\d+\.\d{2})",
                "closing_regex": r"CLOSING (?P<balance>\d+\.\d{2})",
            }
        ]
        rows, warnings, _ = _import_fake_pdf(
            profile,
            page_words=[start() + transaction()],
            page_tables=[[[["OPENING", "100.00"]]]],
        )
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "110.00")
        accounts = profile["pdf"]["sectioned_word_rows"]["accounts"]
        accounts["HKD Current"]["account_id"] = "hsbc_one_hkd_savings"
        rows, _, _ = _import_fake_pdf(
            profile,
            page_words=[start() + transaction(), start("HKD Current") + transaction()],
            page_tables=[[[["OPENING", "100.00"]]], []],
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row["statement_closing_balance"] for row in rows))
