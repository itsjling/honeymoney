from __future__ import annotations

import unittest
from datetime import date

from honeymoney.periods import (
    PeriodSelectionError,
    ordered_view_rows,
    resolve_period_selection,
    view_period_for_row,
)


class PeriodSelectionTest(unittest.TestCase):
    def test_default_selects_the_current_calendar_month(self) -> None:
        selection = resolve_period_selection(today=date(2026, 8, 8))

        self.assertEqual(selection.selected_periods(), ("2026-08",))

    def test_month_name_uses_the_current_year(self) -> None:
        selection = resolve_period_selection("May", today=date(2026, 8, 8))

        self.assertEqual(selection.selected_periods(), ("2026-05",))

    def test_month_flag_accepts_a_strict_year_month(self) -> None:
        selection = resolve_period_selection(month="2025-02", today=date(2026, 8, 8))

        self.assertEqual(selection.selected_periods(), ("2025-02",))

    def test_rejects_combined_selectors(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_selector_conflict"):
            resolve_period_selection("May", month="2026-05")

    def test_rejects_undated_combined_with_all(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_selector_conflict"):
            resolve_period_selection(undated=True, all_periods=True)

    def test_rejects_noncanonical_numeric_month(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_month_invalid"):
            resolve_period_selection(month="2026-2")

    def test_rejects_an_empty_month_value(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_month_invalid"):
            resolve_period_selection(month="")

    def test_rejects_year_zero(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_month_invalid"):
            resolve_period_selection(month="0000-01")

    def test_date_range_selects_each_calendar_month_it_touches(self) -> None:
        selection = resolve_period_selection(start="2026-01-15", end="2026-03-01")

        self.assertEqual(
            selection.selected_periods(),
            ("2026-01", "2026-02", "2026-03"),
        )
        self.assertEqual(selection.start, date(2026, 1, 15))
        self.assertEqual(selection.end, date(2026, 3, 1))

    def test_rejects_a_date_range_with_reversed_endpoints(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_range_invalid"):
            resolve_period_selection(start="2026-03-01", end="2026-01-31")

    def test_rejects_noncanonical_range_dates(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_date_invalid"):
            resolve_period_selection(start="2026-1-01", end="2026-01-31")

    def test_range_requires_both_endpoints(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_range_incomplete"):
            resolve_period_selection(start="2026-01-15")

    def test_rejects_an_empty_range_endpoint(self) -> None:
        with self.assertRaisesRegex(PeriodSelectionError, "period_date_invalid"):
            resolve_period_selection(start="", end="2026-01-15")

    def test_undated_selector_selects_the_explicit_undated_view(self) -> None:
        selection = resolve_period_selection(undated=True)

        self.assertEqual(selection.selected_periods(), ("undated",))

    def test_all_selector_includes_known_months_and_undated(self) -> None:
        selection = resolve_period_selection(all_periods=True)

        self.assertEqual(
            selection.selected_periods(("undated", "2026-02", "2026-01")),
            ("2026-01", "2026-02", "undated"),
        )

    def test_posting_date_chooses_the_output_period_before_transaction_date(
        self,
    ) -> None:
        period = view_period_for_row(
            {"posting_date": "2026-08-01", "transaction_date": "2026-07-31"}
        )

        self.assertEqual(period, "2026-08")

    def test_unrecognized_posted_date_does_not_override_transaction_date(self) -> None:
        period = view_period_for_row(
            {"posted_date": "2026-08-01", "transaction_date": "2026-07-31"}
        )

        self.assertEqual(period, "2026-07")

    def test_invalid_posting_date_falls_back_to_a_valid_transaction_date(self) -> None:
        period = view_period_for_row(
            {"posting_date": "2026-02-30", "transaction_date": "2026-01-31"}
        )

        self.assertEqual(period, "2026-01")

    def test_rows_without_a_valid_date_belong_to_undated(self) -> None:
        period = view_period_for_row(
            {"posting_date": "not-a-date", "transaction_date": "2026-02-30"}
        )

        self.assertEqual(period, "undated")

    def test_orders_rows_by_the_published_view_order(self) -> None:
        common = {
            "posting_date": "2026-05-02",
            "transaction_date": "2026-05-02",
            "account_id": "account-a",
            "merchant": "Market",
            "original_description": "Description",
            "original_amount": "1",
            "original_currency": "HKD",
            "posted_amount": "1",
            "posted_currency": "HKD",
            "amount_hkd": "1",
            "canonical_group_id": "group-a",
            "canonical_slot": "1",
        }
        rows = [
            {**common, "transaction_id": "txn-z"},
            {**common, "transaction_id": "txn-a"},
            {**common, "canonical_slot": "2", "transaction_id": "slot"},
            {**common, "canonical_group_id": "group-b", "transaction_id": "group"},
            {**common, "original_amount": "2", "transaction_id": "amount"},
            {
                **common,
                "original_description": "Later",
                "transaction_id": "description",
            },
            {**common, "merchant": "Zoo", "transaction_id": "merchant"},
            {**common, "account_id": "account-z", "transaction_id": "account"},
            {
                **common,
                "transaction_date": "2026-05-03",
                "transaction_id": "transaction-date",
            },
            {
                **common,
                "posting_date": "2026-05-03",
                "transaction_id": "view-date",
            },
        ]

        self.assertEqual(
            [row["transaction_id"] for row in ordered_view_rows(rows)],
            [
                "txn-a",
                "txn-z",
                "slot",
                "group",
                "amount",
                "description",
                "merchant",
                "account",
                "transaction-date",
                "view-date",
            ],
        )


if __name__ == "__main__":
    unittest.main()
