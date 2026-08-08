from __future__ import annotations

import unittest

from honeymoney.periods import resolve_period_selection
from honeymoney.report import build_report_html
from honeymoney.workspace_queries import query_workspace_rows


class WorkspaceQueriesTest(unittest.TestCase):
    def _row(
        self,
        transaction_id: str,
        *,
        posting_date: str = "",
        transaction_date: str = "",
        needs_review: str = "false",
        amount_hkd: str = "1",
        valuation_status: str = "actual",
    ) -> dict[str, str]:
        return {
            "transaction_id": transaction_id,
            "posting_date": posting_date,
            "transaction_date": transaction_date,
            "account_id": "account-synthetic",
            "merchant": f"Synthetic {transaction_id}",
            "original_description": "Synthetic description",
            "original_amount": "1",
            "original_currency": "HKD",
            "posted_amount": "1",
            "posted_currency": "HKD",
            "amount_hkd": amount_hkd,
            "canonical_group_id": f"group-{transaction_id}",
            "canonical_slot": "1",
            "category": "Synthetic",
            "flow_type": "expense",
            "needs_review": needs_review,
            "valuation_status": valuation_status,
        }

    def test_month_and_range_use_output_period_dates(self) -> None:
        rows = [
            self._row(
                "may-posting",
                posting_date="2026-05-02",
                transaction_date="2026-04-30",
            ),
            self._row(
                "may-fallback",
                posting_date="not-a-date",
                transaction_date="2026-05-20",
            ),
            self._row(
                "june",
                posting_date="2026-06-01",
                transaction_date="2026-06-01",
            ),
            self._row(
                "july",
                posting_date="2026-07-01",
                transaction_date="2026-07-01",
            ),
        ]

        month = query_workspace_rows(
            rows,
            resolve_period_selection(month="2026-05"),
        )
        period_range = query_workspace_rows(
            rows,
            resolve_period_selection(start="2026-05-20", end="2026-06-01"),
        )

        self.assertEqual(month.periods, ("2026-05",))
        self.assertEqual(
            [row["transaction_id"] for row in month.rows],
            ["may-posting", "may-fallback"],
        )
        self.assertEqual(period_range.periods, ("2026-05", "2026-06"))
        self.assertEqual(
            [row["transaction_id"] for row in period_range.rows],
            ["may-fallback", "june"],
        )
        self.assertEqual(period_range.report_label, "2026-05-20 to 2026-06-01")

    def test_undated_and_all_use_the_shared_view_date_rules(self) -> None:
        rows = [
            self._row(
                "undated",
                posting_date="not-a-date",
                transaction_date="2026-02-30",
            ),
            self._row(
                "may",
                posting_date="2026-05-01",
                transaction_date="2026-05-01",
            ),
            self._row(
                "june",
                posting_date="2026-06-01",
                transaction_date="2026-06-01",
            ),
        ]

        undated = query_workspace_rows(rows, resolve_period_selection(undated=True))
        all_periods = query_workspace_rows(
            rows,
            resolve_period_selection(all_periods=True),
        )

        self.assertEqual(undated.periods, ("undated",))
        self.assertEqual(
            [row["transaction_id"] for row in undated.rows],
            ["undated"],
        )
        self.assertEqual(undated.report_label, "undated")
        self.assertEqual(all_periods.periods, ("2026-05", "2026-06", "undated"))
        self.assertEqual(
            [row["transaction_id"] for row in all_periods.rows],
            ["undated", "may", "june"],
        )
        self.assertEqual(all_periods.report_label, "All periods")

    def test_pending_and_missing_valuation_rows_keep_the_selected_order(self) -> None:
        rows = [
            self._row(
                "pending-missing",
                posting_date="2026-05-02",
                needs_review="true",
                amount_hkd="",
                valuation_status="missing",
            ),
            self._row(
                "complete",
                posting_date="2026-05-03",
            ),
            self._row(
                "pending-empty-status",
                posting_date="2026-05-04",
                needs_review="true",
                amount_hkd="",
                valuation_status="",
            ),
            self._row(
                "outside-period",
                posting_date="2026-06-01",
                needs_review="true",
                amount_hkd="",
                valuation_status="missing",
            ),
        ]

        result = query_workspace_rows(
            rows,
            resolve_period_selection(month="2026-05"),
        )

        self.assertEqual(result.view_transaction_count, 3)
        self.assertEqual(result.pending_count, 2)
        self.assertEqual(
            [row["transaction_id"] for row in result.pending_rows],
            ["pending-missing", "pending-empty-status"],
        )
        self.assertEqual(result.missing_valuation_count, 2)
        self.assertEqual(
            [row["transaction_id"] for row in result.missing_valuation_rows],
            ["pending-missing", "pending-empty-status"],
        )

    def test_report_bytes_match_the_selected_rows_and_label(self) -> None:
        result = query_workspace_rows(
            [
                self._row("june", posting_date="2026-06-01"),
                self._row("may", posting_date="2026-05-01"),
            ],
            resolve_period_selection(month="2026-05"),
        )

        self.assertEqual(result.report_label, "2026-05")
        self.assertEqual(
            result.report_html,
            build_report_html(list(result.rows), result.report_label).encode("utf-8"),
        )
        self.assertIn(b"Honeymoney", result.report_html)


if __name__ == "__main__":
    unittest.main()
