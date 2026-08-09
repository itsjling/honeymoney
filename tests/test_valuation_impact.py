import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.identity_state import LEGACY_CATEGORIZED_COLUMNS
from honeymoney.report import build_report_html, missing_base_currency_count
from honeymoney.valuation import valuation_summary

REPO_ROOT = Path(__file__).resolve().parents[1]


def _row(
    transaction_id: str,
    flow_type: str,
    *,
    amount_hkd: str = "",
    posted_amount: str = "",
    valuation_source: str = "missing",
    valuation_status: str = "missing",
) -> dict[str, str]:
    return {
        "transaction_id": transaction_id,
        "date": "2026-07-05",
        "posted_amount": posted_amount,
        "posted_currency": "JPY" if not amount_hkd else "HKD",
        "amount_hkd": amount_hkd,
        "valuation_source": valuation_source,
        "valuation_status": valuation_status,
        "flow_type": flow_type,
        "category": "Synthetic",
        "flow_source": "correction",
    }


class ValuationImpactTest(unittest.TestCase):
    def test_html_keeps_exact_decimal_totals_after_client_render(self) -> None:
        browser = shutil.which("google-chrome") or shutil.which("chromium")
        if browser is None:
            chrome_app = Path(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            )
            browser = str(chrome_app) if chrome_app.exists() else None
        if browser is None:
            self.skipTest("Chrome or Chromium is required for the HTML render check")

        large_income = _row(
            "large_income",
            "income",
            amount_hkd="100000000000000.00",
            posted_amount="100000000000000.00",
            valuation_source="statement_posted",
            valuation_status="actual",
        )
        cent_income = _row(
            "cent_income",
            "income",
            amount_hkd="0.01",
            posted_amount="0.01",
            valuation_source="statement_posted",
            valuation_status="actual",
        )
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.html"
            report_path.write_text(
                build_report_html([large_income, cent_income], "Synthetic period"),
                encoding="utf-8",
            )
            rendered = subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--dump-dom",
                    report_path.as_uri(),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout

        match = re.search(r'id="tile-income">([^<]+)</div>', rendered)
        self.assertIsNotNone(match, rendered)
        assert match is not None
        self.assertEqual(match.group(1), "100,000,000,000,000.01")

    def legacy_owner_filters_keep_status_report_and_html_on_the_same_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            ledger_path = output / "categorized.csv"
            rows = [
                self._legacy_row("justin", "2026-07-01", "expense", "-40", "HKD"),
                self._legacy_row("franchesca", "2026-07-02", "expense", "-10", "HKD"),
                self._legacy_row("household", "2026-07-03", "income", "100", "HKD"),
            ]
            for row, owner in zip(
                rows,
                ("Justin", "Franchesca", "Household"),
                strict=True,
            ):
                row["owner"] = owner
                row["merchant"] = f"{owner.upper()} SYNTHETIC"
            with ledger_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEGACY_CATEGORIZED_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "paths": {"output": str(ledger_path)},
                    }
                ),
                encoding="utf-8",
            )

            owner_args = [
                "--owner",
                "Justin",
                "--owner",
                "Franchesca",
                "--owner",
                "Justin",
            ]
            status = self._run_cli(
                ["status", "--month", "2026-07", *owner_args, "--json"],
                cwd=root,
            )
            report = self._run_cli(
                [
                    "report",
                    "--month",
                    "2026-07",
                    *owner_args,
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(report.returncode, 0, report.stderr)
            status_data = json.loads(status.stdout)["data"]
            report_data = json.loads(report.stdout)["data"]
            expected_filters = {"owners": ["Justin", "Franchesca"]}
            self.assertEqual(status_data["filters"], expected_filters)
            self.assertEqual(report_data["filters"], expected_filters)
            self.assertEqual(status_data["records_processed"], 2)
            self.assertEqual(report_data["transaction_count"], 2)
            self.assertEqual(status_data["valuation"], report_data["valuation"])
            self.assertEqual(
                status_data["valuation"]["cash_flow"]["combined_estimate"][
                    "net_cash_flow"
                ],
                "-50.00",
            )

            report_html = (output / "report.html").read_text(encoding="utf-8")
            self.assertIn("JUSTIN SYNTHETIC", report_html)
            self.assertIn("FRANCHESCA SYNTHETIC", report_html)
            self.assertNotIn("HOUSEHOLD SYNTHETIC", report_html)
            self.assertIn('id="owner-filter"', report_html)
            self.assertIn('id="owner-select-all"', report_html)
            self.assertIn('id="valuation-warning"', report_html)
            self.assertIn('id="missing-total"', report_html)
            self.assertIn("function applyOwnerFilter()", report_html)

            combined = self._run_cli(
                ["status", "--month", "2026-07", "--json"],
                cwd=root,
            )
            combined_report = self._run_cli(
                ["report", "--month", "2026-07", "--no-open", "--json"],
                cwd=root,
            )
            self.assertEqual(combined.returncode, 0, combined.stderr)
            self.assertEqual(combined_report.returncode, 0, combined_report.stderr)
            combined_data = json.loads(combined.stdout)["data"]
            combined_report_data = json.loads(combined_report.stdout)["data"]
            self.assertEqual(combined_data["filters"], {"owners": []})
            self.assertEqual(combined_report_data["filters"], {"owners": []})
            self.assertEqual(combined_data["records_processed"], 3)
            self.assertEqual(combined_report_data["transaction_count"], 3)
            self.assertEqual(
                combined_data["valuation"]["cash_flow"]["combined_estimate"][
                    "net_cash_flow"
                ],
                "50.00",
            )

            single_status = self._run_cli(
                ["status", "--month", "2026-07", "--owner", "Justin", "--json"],
                cwd=root,
            )
            single_report = self._run_cli(
                [
                    "report",
                    "--month",
                    "2026-07",
                    "--owner",
                    "Justin",
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(single_status.returncode, 0, single_status.stderr)
            self.assertEqual(single_report.returncode, 0, single_report.stderr)
            single_status_data = json.loads(single_status.stdout)["data"]
            single_report_data = json.loads(single_report.stdout)["data"]
            self.assertEqual(single_status_data["records_processed"], 1)
            self.assertEqual(single_report_data["transaction_count"], 1)
            self.assertEqual(
                single_status_data["valuation"], single_report_data["valuation"]
            )
            self.assertEqual(
                single_report_data["valuation"]["cash_flow"]["combined_estimate"][
                    "net_cash_flow"
                ],
                "-40.00",
            )
            single_html = (output / "report.html").read_text(encoding="utf-8")
            self.assertIn("JUSTIN SYNTHETIC", single_html)
            self.assertNotIn("FRANCHESCA SYNTHETIC", single_html)
            self.assertNotIn("HOUSEHOLD SYNTHETIC", single_html)
            self.assertIn('data-account-id="account_justin"', single_html)
            self.assertIn("renderBalanceCoverage();", single_html)

    def legacy_corrected_owner_keeps_its_canonical_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            statement = root / "synthetic.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-01,SYNTHETIC OWNER CHANGE,-25.00,HKD\n",
                encoding="utf-8",
            )
            imported = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--config",
                    str(root / "config.json"),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                [ledger_row] = list(csv.DictReader(handle))
            corrected = self._run_cli(
                [
                    "correct",
                    "--config",
                    str(root / "config.json"),
                    "--file",
                    "-",
                    "--json",
                ],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": ledger_row["transaction_id"],
                            "owner": "Justin",
                        }
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)

            for command in ("status", "report"):
                with self.subTest(command=command):
                    args = [
                        command,
                        "--month",
                        "2026-07",
                        "--owner",
                        "Justin",
                        "--config",
                        str(root / "config.json"),
                        "--json",
                    ]
                    if command == "report":
                        args.append("--no-open")
                    result = self._run_cli(args, cwd=root)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    data = json.loads(result.stdout)["data"]
                    count_field = (
                        "records_processed"
                        if command == "status"
                        else "transaction_count"
                    )
                    self.assertEqual(data[count_field], 1)
                    self.assertEqual(data["overlap"]["source_occurrence_count"], 1)
                    self.assertEqual(
                        data["overlap"]["consolidated_occurrence_count"], 0
                    )

    def legacy_unknown_owner_filters_fail_before_workspace_files_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            ledger_path = output / "categorized.csv"
            row = self._legacy_row("known", "2026-07-01", "expense", "-40", "HKD")
            row["owner"] = "Justin"
            with ledger_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=LEGACY_CATEGORIZED_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            (root / "config.json").write_text(
                json.dumps({"paths": {"output": str(ledger_path)}}),
                encoding="utf-8",
            )
            before = {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

            for command in ("status", "report"):
                with self.subTest(command=command):
                    result = self._run_cli(
                        [command, "--owner", "Not configured", "--json"],
                        cwd=root,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    error = json.loads(result.stdout)["errors"][0]
                    self.assertIn(
                        "Unsupported owner filter: Not configured", error["message"]
                    )
                    after = {
                        str(path.relative_to(root)): path.read_bytes()
                        for path in sorted(root.rglob("*"))
                        if path.is_file()
                    }
                    self.assertEqual(after, before)

    def legacy_status_and_report_help_describe_repeatable_owner_filter(self) -> None:
        for command in ("status", "report"):
            with self.subTest(command=command):
                result = self._run_cli([command, "--help"], cwd=REPO_ROOT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--owner OWNER", result.stdout)
                self.assertIn(
                    "Filter by owner. Repeat to include more than one", result.stdout
                )

    def test_html_combined_view_keeps_unassigned_owner_rows_selectable(self) -> None:
        unassigned = _row(
            "unassigned",
            "expense",
            amount_hkd="-5",
            posted_amount="-5",
            valuation_source="statement_posted",
            valuation_status="actual",
        )
        unassigned["owner"] = ""
        assigned = _row(
            "assigned",
            "expense",
            amount_hkd="-7",
            posted_amount="-7",
            valuation_source="statement_posted",
            valuation_status="actual",
        )
        assigned["owner"] = "Justin"

        report_html = build_report_html([unassigned, assigned], "Synthetic period")

        self.assertIn('"owner": ""', report_html)
        self.assertIn('name.textContent = owner || "Unassigned";', report_html)
        self.assertIn("return owners.indexOf(owner) === index;", report_html)
        self.assertIn("function applyOwnerFilter()", report_html)
        self.assertIn("input.checked = true;", report_html)

    def test_summary_splits_completeness_and_cash_flow_by_valuation_status(
        self,
    ) -> None:
        rows = [
            _row(
                "actual_income",
                "income",
                amount_hkd="100",
                posted_amount="100",
                valuation_source="statement_posted",
                valuation_status="actual",
            ),
            _row(
                "actual_expense",
                "expense",
                amount_hkd="-40",
                posted_amount="-40",
                valuation_source="statement_posted",
                valuation_status="actual",
            ),
            _row(
                "actual_refund",
                "refund",
                amount_hkd="5",
                posted_amount="5",
                valuation_source="matched_exchange_leg",
                valuation_status="actual",
            ),
            _row(
                "estimated_income",
                "income",
                amount_hkd="20",
                posted_amount="2",
                valuation_source="hkma_daily_reference_rate",
                valuation_status="estimated",
            ),
            _row(
                "estimated_expense",
                "expense",
                amount_hkd="-10",
                posted_amount="-1",
                valuation_source="configured_dated_rate",
                valuation_status="estimated",
            ),
            _row(
                "estimated_refund",
                "refund",
                amount_hkd="2",
                posted_amount="1",
                valuation_source="configured_fixed_rate",
                valuation_status="estimated",
            ),
            _row("missing_income", "income", posted_amount="30"),
            _row("missing_expense", "expense", posted_amount="-4"),
            _row("missing_refund", "refund", posted_amount="3"),
            _row("missing_transfer", "internal_transfer", posted_amount="50"),
            _row(
                "missing_card_payment",
                "credit_card_payment",
                posted_amount="-60",
            ),
            _row(
                "missing_investment",
                "investment_transfer",
                posted_amount="-70",
            ),
            _row("missing_unresolved", "unresolved", posted_amount="-8"),
            _row("missing_zero_income", "income", posted_amount="0"),
            _row("missing_other", "new_flow", posted_amount="-9"),
            _row(
                "actual_zero_expense",
                "expense",
                amount_hkd="0",
                posted_amount="0",
                valuation_source="statement_posted",
                valuation_status="actual",
            ),
        ]

        summary = valuation_summary(rows)

        self.assertEqual(summary["missing_count"], 9)
        self.assertEqual(summary["cash_flow_blocking_missing_count"], 3)
        self.assertEqual(summary["excluded_flow_missing_count"], 3)
        self.assertEqual(summary["unresolved_flow_missing_count"], 1)
        self.assertEqual(summary["zero_amount_missing_count"], 1)
        self.assertEqual(summary["other_flow_missing_count"], 1)
        self.assertFalse(summary["cash_flow_complete"])
        self.assertEqual(
            summary["cash_flow"],
            {
                "currency": "HKD",
                "actual": {
                    "income": "100.00",
                    "spending": "-40.00",
                    "refunds": "5.00",
                    "net_cash_flow": "65.00",
                },
                "estimated": {
                    "income": "20.00",
                    "spending": "-10.00",
                    "refunds": "2.00",
                    "net_cash_flow": "12.00",
                },
                "combined_estimate": {
                    "income": "120.00",
                    "spending": "-50.00",
                    "refunds": "7.00",
                    "net_cash_flow": "77.00",
                },
            },
        )
        self.assertEqual(
            {
                source: summary["sources"][source]
                for source in (
                    "hkma_daily_reference_rate",
                    "configured_dated_rate",
                    "configured_fixed_rate",
                )
            },
            {
                "hkma_daily_reference_rate": 1,
                "configured_dated_rate": 1,
                "configured_fixed_rate": 1,
            },
        )

        html = build_report_html(rows, "Synthetic period", source_occurrence_count=16)
        self.assertIn('Total missing</div><div class="value num">9', html)
        self.assertIn('Cash-flow blockers</div><div class="value num">3', html)
        self.assertIn('Excluded flows</div><div class="value num">3', html)
        self.assertIn('Unresolved flows</div><div class="value num">1', html)
        self.assertIn('Other flows</div><div class="value num">1', html)
        self.assertIn("9 rows have no HKD valuation.", html)
        self.assertNotIn("omitted from period totals", html)
        self.assertIn("<th>Actual</th><th>Estimated</th>", html)
        self.assertIn(
            '<td>Net cash flow</td><td class="num">65.00</td>'
            '<td class="num">12.00</td><td class="num">77.00</td>',
            html,
        )
        self.assertIn("not exact bank conversion costs or tax valuations", html)

    def test_legacy_missing_count_keeps_nonzero_posted_amount_rule(self) -> None:
        rows = [
            _row("nonzero", "expense", posted_amount="-1"),
            _row("zero", "expense", posted_amount="0"),
            _row("blank", "expense", posted_amount=""),
            _row("invalid", "expense", posted_amount="invalid"),
        ]

        self.assertEqual(valuation_summary(rows)["missing_count"], 4)
        self.assertEqual(missing_base_currency_count(rows), 1)

    def legacy_status_and_report_share_period_counts_and_impact_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            output.mkdir()
            ledger_path = output / "categorized.csv"
            rows = [
                self._legacy_row("actual", "2026-07-01", "income", "100", "HKD"),
                self._legacy_row("estimate", "2026-07-02", "expense", "-10", "EUR"),
                self._legacy_row(
                    "excluded",
                    "2026-07-03",
                    "internal_transfer",
                    "5",
                    "JPY",
                ),
                self._legacy_row(
                    "unresolved",
                    "2026-07-04",
                    "unresolved",
                    "-7",
                    "JPY",
                ),
                self._legacy_row(
                    "blocking",
                    "2026-07-05",
                    "expense",
                    "-2",
                    "JPY",
                ),
                self._legacy_row(
                    "outside",
                    "2026-08-01",
                    "expense",
                    "-3",
                    "JPY",
                ),
            ]
            rows[4]["flow_type"] = ""
            rows[4]["flow_source"] = ""
            with ledger_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=LEGACY_CATEGORIZED_COLUMNS,
                )
                writer.writeheader()
                writer.writerows(rows)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"EUR": 9},
                        "paths": {"output": str(ledger_path)},
                    }
                ),
                encoding="utf-8",
            )

            status = self._run_cli(
                ["status", "--month", "2026-07", "--json"],
                cwd=root,
            )
            report = self._run_cli(
                [
                    "report",
                    "--month",
                    "2026-07",
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(report.returncode, 0, report.stderr)
            status_data = json.loads(status.stdout)["data"]
            report_data = json.loads(report.stdout)["data"]
            self.assertEqual(status_data["canonical_occurrence_count"], 5)
            self.assertEqual(report_data["transaction_count"], 5)
            self.assertEqual(
                status_data["source_occurrence_count"],
                report_data["overlap"]["source_occurrence_count"],
            )
            self.assertEqual(status_data["valuation"], report_data["valuation"])
            valuation = status_data["valuation"]
            self.assertEqual(valuation["missing_count"], 3)
            self.assertEqual(valuation["cash_flow_blocking_missing_count"], 1)
            self.assertEqual(valuation["excluded_flow_missing_count"], 1)
            self.assertEqual(valuation["unresolved_flow_missing_count"], 1)
            self.assertEqual(
                valuation["cash_flow"]["combined_estimate"]["net_cash_flow"],
                "10.00",
            )
            report_html = (output / "report.html").read_text(encoding="utf-8")
            self.assertIn(
                'Total missing</div><div class="value num">3',
                report_html,
            )
            self.assertIn(
                'Cash-flow blockers</div><div class="value num">1',
                report_html,
            )

            human = self._run_cli(
                ["status", "--month", "2026-07"],
                cwd=root,
            )
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("Cash-flow blockers: 1", human.stdout)
            self.assertIn(
                "Combined estimate HKD: income=100.00, spending=-90.00, "
                "refunds=0.00, net=10.00",
                human.stdout,
            )

    def _legacy_row(
        self,
        label: str,
        row_date: str,
        flow_type: str,
        posted_amount: str,
        posted_currency: str,
    ) -> dict[str, str]:
        row = {column: "" for column in LEGACY_CATEGORIZED_COLUMNS}
        row.update(
            {
                "transaction_id": "txn_"
                + hashlib.sha256(label.encode()).hexdigest()[:16],
                "date": row_date,
                "transaction_date": row_date,
                "account_id": f"account_{label}",
                "account_type": "bank",
                "institution": "Synthetic Bank",
                "posted_amount": posted_amount,
                "posted_currency": posted_currency,
                "original_amount": posted_amount,
                "original_currency": posted_currency,
                "category": "Synthetic",
                "flow_type": flow_type,
                "flow_source": "correction",
                "needs_review": "false",
                "flags": "",
            }
        )
        return row

    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
