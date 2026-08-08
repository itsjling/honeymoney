from __future__ import annotations

import csv
import io
import unittest

from honeymoney.csv_artifacts import HONEYMONEY_CSV_ESCAPE_V1
from honeymoney.periods import resolve_period_selection
from honeymoney.report import build_report_html
from honeymoney.schema import CATEGORIZED_COLUMNS, REVIEW_NEEDED_COLUMNS
from honeymoney.workspace_views import (
    ViewReportInputs,
    WorkspaceViewError,
    plan_automatic_view_refresh,
    plan_workspace_views,
    view_content_proof,
    view_relative_path,
)


class WorkspaceViewsTest(unittest.TestCase):
    def _row(self, transaction_id: str, posting_date: str) -> dict[str, str]:
        return {
            "transaction_id": transaction_id,
            "posting_date": posting_date,
            "transaction_date": posting_date,
            "account_id": "account-a",
            "merchant": "Synthetic merchant",
            "original_description": "Synthetic description",
            "original_amount": "1",
            "original_currency": "HKD",
            "posted_amount": "1",
            "posted_currency": "HKD",
            "amount_hkd": "1",
            "canonical_group_id": "group-a",
            "canonical_slot": "1",
            "category": "Other",
            "flow_type": "expense",
            "needs_review": "false",
        }

    def test_selected_empty_month_has_complete_deterministic_artifacts(self) -> None:
        plan = plan_workspace_views(
            [],
            resolve_period_selection(month="2026-05"),
            (),
            content_proof_key=b"p" * 32,
        )

        self.assertEqual(plan.selected_periods, ("2026-05",))
        self.assertEqual(len(plan.units), 1)
        [unit] = plan.units
        self.assertEqual(unit.period, "2026-05")
        self.assertEqual(
            unit.transactions_csv,
            (",".join(CATEGORIZED_COLUMNS) + "\r\n").encode(),
        )
        self.assertEqual(
            unit.review_needed_csv,
            (",".join(REVIEW_NEEDED_COLUMNS) + "\r\n").encode(),
        )
        self.assertEqual(
            unit.report_html,
            build_report_html([], "2026-05").encode(),
        )
        self.assertEqual(tuple(item.period for item in plan.writes), ("2026-05",))
        self.assertEqual(plan.removals, ())

    def test_automatic_refresh_keeps_a_newly_empty_view_and_ignores_unrelated_edits(
        self,
    ) -> None:
        key = b"p" * 32
        previous_rows = [
            self._row("txn-april", "2026-04-10"),
            self._row("txn-moved", "2026-05-10"),
        ]
        next_rows = [
            self._row("txn-april", "2026-04-10"),
            self._row("txn-moved", "2026-06-10"),
        ]
        prior = plan_workspace_views(
            previous_rows,
            resolve_period_selection(all_periods=True),
            (),
            content_proof_key=key,
        )
        installed_files = {
            file.path: file.content for unit in prior.units for file in unit.files()
        }
        installed_files["views/2026-04/transactions.csv"] = b"edited"

        plan = plan_automatic_view_refresh(
            previous_rows,
            next_rows,
            prior.next_registered_views,
            content_proof_key=key,
            installed_files=installed_files,
        )

        self.assertEqual(
            tuple(unit.period for unit in plan.writes), ("2026-05", "2026-06")
        )
        self.assertEqual(plan.removals, ())
        self.assertEqual(
            tuple(item["period"] for item in plan.next_registered_views),
            ("2026-04", "2026-05", "2026-06"),
        )
        self.assertNotIn(
            "views/2026-04/transactions.csv",
            tuple(file.path for file in plan.publication_files()),
        )
        empty_unit = next(unit for unit in plan.units if unit.period == "2026-05")
        self.assertEqual(
            empty_unit.transactions_csv,
            (",".join(CATEGORIZED_COLUMNS) + "\r\n").encode(),
        )

    def test_selected_rebuild_repairs_an_edited_view_unit(self) -> None:
        key = b"p" * 32
        rows = [self._row("txn-may", "2026-05-10")]
        selection = resolve_period_selection(month="2026-05")
        initial = plan_workspace_views(
            rows,
            selection,
            (),
            content_proof_key=key,
        )
        installed_files = {
            file.path: file.content for unit in initial.units for file in unit.files()
        }
        installed_files["views/2026-05/report.html"] = b"edited"

        repair = plan_workspace_views(
            rows,
            selection,
            initial.next_registered_views,
            content_proof_key=key,
            installed_files=installed_files,
        )

        self.assertEqual(tuple(unit.period for unit in repair.writes), ("2026-05",))
        self.assertEqual(
            tuple(file.path for file in repair.publication_files()),
            (
                "views/2026-05/transactions.csv",
                "views/2026-05/review_needed.csv",
                "views/2026-05/report.html",
            ),
        )

    def test_matching_registered_and_installed_unit_needs_no_write(self) -> None:
        key = b"p" * 32
        rows = [self._row("txn-may", "2026-05-10")]
        selection = resolve_period_selection(month="2026-05")
        initial = plan_workspace_views(
            rows,
            selection,
            (),
            content_proof_key=key,
        )
        installed_files = {
            file.path: file.content for unit in initial.units for file in unit.files()
        }

        plan = plan_workspace_views(
            rows,
            selection,
            initial.next_registered_views,
            content_proof_key=key,
            installed_files=installed_files,
        )

        self.assertEqual(plan.writes, ())
        self.assertEqual(plan.unchanged, ("2026-05",))
        self.assertEqual(plan.publication_files(), ())

    def test_view_rows_and_review_rows_have_the_required_order_and_public_schema(
        self,
    ) -> None:
        review_row = self._row("txn-first", "2026-05-10")
        review_row["needs_review"] = "true"
        review_row["review_reasons"] = "category_decision"
        plan = plan_workspace_views(
            [self._row("txn-later", "2026-05-11"), review_row],
            resolve_period_selection(month="2026-05"),
            (),
            content_proof_key=b"p" * 32,
        )
        [unit] = plan.units
        transactions = list(
            csv.DictReader(io.StringIO(unit.transactions_csv.decode(), newline=""))
        )
        review_needed = list(
            csv.DictReader(io.StringIO(unit.review_needed_csv.decode(), newline=""))
        )

        self.assertEqual(list(transactions[0]), CATEGORIZED_COLUMNS)
        self.assertEqual(
            [row["transaction_id"] for row in transactions],
            ["txn-first", "txn-later"],
        )
        self.assertEqual(list(review_needed[0]), REVIEW_NEEDED_COLUMNS)
        self.assertEqual(
            [row["transaction_id"] for row in review_needed],
            ["txn-first"],
        )

    def test_view_csv_uses_the_existing_spreadsheet_safety_encoding(self) -> None:
        row = self._row("txn-formula", "2026-05-10")
        row["merchant"] = "=unsafe"
        plan = plan_workspace_views(
            [row],
            resolve_period_selection(month="2026-05"),
            (),
            content_proof_key=b"p" * 32,
        )
        [unit] = plan.units
        [written] = list(
            csv.DictReader(io.StringIO(unit.transactions_csv.decode(), newline=""))
        )

        self.assertEqual(written["merchant"], HONEYMONEY_CSV_ESCAPE_V1 + "=unsafe")

    def test_content_proof_covers_all_three_artifacts(self) -> None:
        files = {
            "transactions.csv": b"transactions",
            "review_needed.csv": b"review",
            "report.html": b"report",
        }

        proof = view_content_proof(
            "2026-05",
            files,
            content_proof_key=b"p" * 32,
        )
        changed = view_content_proof(
            "2026-05",
            {**files, "report.html": b"changed report"},
            content_proof_key=b"p" * 32,
        )

        self.assertRegex(proof, r"^[0-9a-f]{64}$")
        self.assertNotEqual(proof, changed)

    def test_view_report_uses_complete_contributing_source_balance_checks(
        self,
    ) -> None:
        plan = plan_workspace_views(
            [self._row("txn-may", "2026-05-10")],
            resolve_period_selection(month="2026-05"),
            (),
            content_proof_key=b"p" * 32,
            report_inputs={
                "2026-05": ViewReportInputs(
                    source_occurrence_count=2,
                    balance_reconciliation={
                        "account-a": {
                            "status": "reconciled",
                            "result": "matched",
                            "statements": [
                                {
                                    "source_file": "synthetic.csv",
                                    "statement_section": "main",
                                    "posted_currency": "HKD",
                                    "status": "reconciled",
                                    "result": "matched",
                                    "opening_evidence_found": True,
                                    "closing_evidence_found": True,
                                }
                            ],
                        }
                    },
                )
            },
        )

        [unit] = plan.units
        report = unit.report_html.decode()
        self.assertIn(">2 source occurrences<", report)
        self.assertIn("synthetic.csv", report)
        self.assertNotIn("No statement sections found.", report)

    def test_content_proof_rejects_a_non_key_value(self) -> None:
        with self.assertRaisesRegex(
            WorkspaceViewError, "view_content_proof_key_invalid"
        ):
            view_content_proof(
                "2026-05",
                {
                    "transactions.csv": b"transactions",
                    "review_needed.csv": b"review",
                    "report.html": b"report",
                },
                content_proof_key="p" * 32,  # type: ignore[arg-type]
            )

    def test_view_paths_reject_a_non_calendar_year_month(self) -> None:
        with self.assertRaisesRegex(WorkspaceViewError, "view_period_invalid"):
            view_relative_path("0000-01", "transactions.csv")

    def test_all_rebuild_removes_only_registered_views_no_longer_implied(self) -> None:
        key = b"p" * 32
        registered = plan_workspace_views(
            [],
            resolve_period_selection(month="2026-04"),
            (),
            content_proof_key=key,
        ).next_registered_views

        plan = plan_workspace_views(
            [self._row("txn-may", "2026-05-10")],
            resolve_period_selection(all_periods=True),
            registered,
            content_proof_key=key,
        )

        self.assertEqual(tuple(unit.period for unit in plan.writes), ("2026-05",))
        self.assertEqual(plan.removals, ("2026-04",))
        self.assertEqual(
            tuple(item["period"] for item in plan.next_registered_views),
            ("2026-05",),
        )
        self.assertEqual(
            tuple(file.path for file in plan.publication_files()[-3:]),
            (
                "views/2026-04/transactions.csv",
                "views/2026-04/review_needed.csv",
                "views/2026-04/report.html",
            ),
        )


if __name__ == "__main__":
    unittest.main()
