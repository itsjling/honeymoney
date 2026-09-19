import hashlib
import unittest

from tests.golden_helpers import _import_pdf_case, load_profile


def _words(*lines: str) -> list[dict[str, object]]:
    return [
        {"text": line, "x0": 70, "top": index * 20}
        for index, line in enumerate(lines, start=1)
    ]


def _time_deposit_account_id(reference: str) -> str:
    digest = hashlib.sha256(
        f"honeymoney:mox-time-deposit:v1:{reference}".encode()
    ).hexdigest()
    return f"mox_time_deposit_{digest}"


class MoxBankPdfSectionsTest(unittest.TestCase):
    def test_cross_year_multi_account_statement_keeps_source_sections(self) -> None:
        first_reference = "100000000001"
        second_reference = "100000000002"
        page_lines = [
            ["Statement Period: 15 Dec 2025 - 14 Jan 2026"],
            [
                "HKD Mox Account transaction details",
                "Transaction date Settlement date Description Corresponding amount",
                "31 Dec 31 Dec Opening Balance 100.00",
                "31 Dec 31 Dec SYNTHETIC HKD CREDIT +5.00",
                "14 Jan 14 Jan Closing Balance 105.00",
            ],
            [
                "USD Mox Account transaction details",
                "Transaction date Settlement date Description Corresponding amount (USD)",
                "31 Dec 31 Dec Opening Balance 200.00",
                "02 Jan 02 Jan Time Deposit -80.00",
                "02 Jan 02 Jan Time Deposit Upfront Interest +2.00",
                "14 Jan 14 Jan Closing Balance 122.00",
            ],
            [
                f"Time Deposit - {first_reference}",
                "Principal Amount: 80.00 USD",
                "Transaction date Settlement date Description Corresponding amount (USD)",
                "02 Jan 02 Jan Opening Balance 0.00",
                "02 Jan 02 Jan USD Mox Account 80.00",
                "14 Jan 14 Jan Time Deposit Interest 0.00",
                "14 Jan 14 Jan Closing Balance 80.00",
                f"Time Deposit - {second_reference}",
                "Principal Amount: 40.00 USD",
                "Transaction date Settlement date Description Corresponding amount (USD)",
                "03 Jan 03 Jan USD Mox Account 40.00",
                "14 Jan 14 Jan Closing Balance 40.00",
            ],
        ]
        tables = [
            [[page_lines[0]]],
            [[[line] for line in page_lines[1][2:]]],
            [[[line] for line in page_lines[2][2:]]],
            [[[line] for line in page_lines[3] if line[:1].isdigit()]],
        ]

        rows, warnings = _import_pdf_case(
            load_profile("mox_bank_pdf.json"),
            tables=tables,
            words_pages=[_words(*lines) for lines in page_lines],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            [row["account_id"] for row in rows],
            [
                "mox_bank_main",
                "mox_bank_usd",
                "mox_bank_usd",
                _time_deposit_account_id(first_reference),
                _time_deposit_account_id(second_reference),
            ],
        )
        self.assertEqual(
            [row["posted_currency"] for row in rows],
            ["HKD", "USD", "USD", "USD", "USD"],
        )
        self.assertEqual(
            [row["transaction_date"] for row in rows],
            [
                "2025-12-31",
                "2026-01-02",
                "2026-01-02",
                "2026-01-02",
                "2026-01-03",
            ],
        )
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "105.00")
        self.assertEqual(rows[1]["statement_opening_balance"], "200.00")
        self.assertEqual(rows[2]["statement_closing_balance"], "122.00")
        self.assertEqual(rows[3]["statement_opening_balance"], "0.00")
        self.assertEqual(rows[3]["statement_closing_balance"], "80.00")
        self.assertEqual(rows[4]["statement_opening_balance"], "")
        self.assertEqual(rows[4]["statement_closing_balance"], "40.00")
        self.assertEqual(rows[3]["original_description"], "USD Mox Account")
        self.assertNotIn(
            "Time Deposit Interest",
            [row["original_description"] for row in rows],
        )
        encoded = repr(rows)
        self.assertNotIn(first_reference, encoded)
        self.assertNotIn(second_reference, encoded)

    def test_single_hkd_account_keeps_the_existing_account_id(self) -> None:
        lines = [
            "Statement Period: 01 Apr 2026 - 30 Apr 2026",
            "HKD Mox Account transaction details",
            "Transaction date Settlement date Description Corresponding amount",
            "01 Apr 01 Apr Opening Balance 100.00",
            "02 Apr 02 Apr SYNTHETIC CREDIT +10.00",
            "30 Apr 30 Apr Closing Balance 110.00",
        ]

        rows, warnings = _import_pdf_case(
            load_profile("mox_bank_pdf.json"),
            tables=[[[[line] for line in lines[3:]]]],
            words_pages=[_words(*lines)],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_id"], "mox_bank_main")
        self.assertEqual(rows[0]["posted_currency"], "HKD")
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "110.00")

    def test_doubled_header_and_wrapped_interest_keep_all_activity(self) -> None:
        lines = [
            "Statement Period: 01 Aug 2026 - 31 Aug 2026",
            "USD Mox Account transaction details",
            "AAccttiivviittyy SSeettttlleemmeenntt DDeessccrriippttiioonn "
            "CCoorrrreessppoonnddiinngg AAmmoouunntt ((UUSSDD))",
            "01 Aug 01 Aug Opening balance synthetic 100.00",
            "02 Aug 02 Aug Time Deposit Upfront Interest",
            "+2.00",
            "31 Aug 31 Aug Closing balance synthetic 102.00",
        ]

        rows, warnings = _import_pdf_case(
            load_profile("mox_bank_pdf.json"),
            tables=[],
            words_pages=[_words(*lines)],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["original_description"], "Time Deposit Upfront Interest"
        )
        self.assertEqual(rows[0]["posted_currency"], "USD")
        self.assertEqual(rows[0]["statement_opening_balance"], "100.00")
        self.assertEqual(rows[0]["statement_closing_balance"], "102.00")

    def test_corresponding_currency_and_header_words_remain_transaction_facts(
        self,
    ) -> None:
        rows, warnings = _import_pdf_case(
            load_profile("mox_bank_pdf.json"),
            tables=[],
            words_pages=[
                _words(
                    "Statement Period: 01 Aug 2026 - 31 Aug 2026",
                    "HKD Mox Account transaction details",
                    "Activity Settlement Description Corresponding amount (HKD)",
                    "02 Aug 02 Aug ACTIVITY SETTLEMENT AMOUNT (USD) -10.00 USD -78.00",
                )
            ],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["original_amount"], "-10.00")
        self.assertEqual(rows[0]["original_currency"], "USD")
        self.assertEqual(rows[0]["posted_amount"], "-78.00")
        self.assertEqual(rows[0]["posted_currency"], "HKD")
        self.assertEqual(
            rows[0]["original_description"], "ACTIVITY SETTLEMENT AMOUNT (USD)"
        )

    def test_unknown_or_malformed_section_fails_closed(self) -> None:
        cases = (
            "EUR Mox Account transaction details",
            "Time Deposit - missing-reference",
        )
        for heading in cases:
            with (
                self.subTest(heading=heading),
                self.assertRaisesRegex(ValueError, "Unsupported Mox"),
            ):
                _import_pdf_case(
                    load_profile("mox_bank_pdf.json"),
                    tables=[],
                    words_pages=[
                        _words(
                            "Statement Period: 01 Aug 2026 - 31 Aug 2026",
                            "HKD Mox Account transaction details",
                            "Activity Settlement Description Corresponding amount",
                            "01 Aug 01 Aug SYNTHETIC CREDIT +1.00",
                            heading,
                            "Activity Settlement Description Corresponding amount",
                            "02 Aug 02 Aug SYNTHETIC CREDIT +2.00",
                        )
                    ],
                )

    def test_dormant_deposit_does_not_fabricate_a_transaction(self) -> None:
        reference = "100000000009"
        rows, warnings = _import_pdf_case(
            load_profile("mox_bank_pdf.json"),
            tables=[],
            words_pages=[
                _words(
                    "Statement Period: 01 Jun 2026 - 30 Jun 2026",
                    f"Time Deposit - {reference}",
                    "Principal Amount: 80.00 USD",
                    "Activity Settlement Description Corresponding amount (USD)",
                    "01 Jun 01 Jun Opening balance 80.00",
                    "30 Jun 30 Jun Closing balance 80.00",
                )
            ],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(rows, [])


class MoxCreditCardPdfPeriodTest(unittest.TestCase):
    def test_cross_year_dates_and_balance_before_label_use_printed_facts(self) -> None:
        rows, warnings = _import_pdf_case(
            load_profile("mox_credit_card_pdf.json"),
            tables=[
                [
                    [
                        [
                            "Activity date Settlement date Description "
                            "Foreign currency amount Amount (HKD)"
                        ],
                        ["31 Dec 31 Dec SYNTHETIC DECEMBER PURCHASE -10.00"],
                        ["02 Jan 03 Jan SYNTHETIC JANUARY PURCHASE -20.00"],
                    ]
                ]
            ],
            words_pages=[
                _words(
                    "Statement Period: 15 Dec 2025 - 14 Jan 2026",
                    "30.00 HKD",
                    "Statement Balance",
                )
            ],
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            [row["transaction_date"] for row in rows],
            ["2025-12-31", "2026-01-02"],
        )
        self.assertEqual(
            [row["posting_date"] for row in rows],
            ["2025-12-31", "2026-01-03"],
        )
        self.assertEqual(rows[0]["statement_opening_balance"], "")
        self.assertEqual(rows[-1]["statement_closing_balance"], "30.00")

    def test_signed_closing_balance_is_preserved(self) -> None:
        rows, _ = _import_pdf_case(
            load_profile("mox_credit_card_pdf.json"),
            tables=[[[["02 Jan 02 Jan SYNTHETIC PURCHASE -20.00"]]]],
            words_pages=[
                _words(
                    "Statement Period: 01 Jan 2026 - 31 Jan 2026",
                    "-20.00 HKD",
                    "Statement Balance",
                )
            ],
        )

        self.assertEqual(rows[-1]["statement_closing_balance"], "-20.00")

    def test_posting_date_after_period_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "period"):
            _import_pdf_case(
                load_profile("mox_credit_card_pdf.json"),
                tables=[[[["31 Jan 02 Feb SYNTHETIC PURCHASE -20.00"]]]],
                words_pages=[_words("Statement Period: 02 Jan 2026 - 01 Feb 2026")],
            )

    def test_transaction_text_cannot_supply_period_or_closing_balance(self) -> None:
        with self.assertRaisesRegex(ValueError, "statement period"):
            _import_pdf_case(
                load_profile("mox_credit_card_pdf.json"),
                tables=[
                    [
                        [
                            [
                                "02 Jan 02 Jan SYNTHETIC Statement period: "
                                "01 Jan 2026 - 31 Jan 2026 Statement Balance -20.00"
                            ]
                        ]
                    ]
                ],
                words_pages=[
                    _words(
                        "02 Jan 02 Jan SYNTHETIC Statement period: "
                        "01 Jan 2026 - 31 Jan 2026"
                    )
                ],
            )

        rows, _ = _import_pdf_case(
            load_profile("mox_credit_card_pdf.json"),
            tables=[[[["02 Jan 02 Jan SYNTHETIC Statement Balance -20.00"]]]],
            words_pages=[
                _words(
                    "Statement Period: 01 Jan 2026 - 31 Jan 2026",
                    "02 Jan 02 Jan SYNTHETIC Statement Balance -20.00",
                )
            ],
        )
        self.assertEqual(rows[-1]["statement_closing_balance"], "")


if __name__ == "__main__":
    unittest.main()
