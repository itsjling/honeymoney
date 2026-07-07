import csv
import io
import json
import os
import pty
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from honeymoney.cli import _report_command, _resolve_period, _StatusLine
from honeymoney.schema import ALLOWED_CATEGORIES


REPO_ROOT = Path(__file__).resolve().parents[1]

PROMPT_CATEGORIES = sorted(ALLOWED_CATEGORIES - {"Unknown"})


def _category_number(category: str) -> str:
    return str(PROMPT_CATEGORIES.index(category) + 1)


class WorkflowTest(unittest.TestCase):
    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "money"
        result = subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", "setup", "--root", str(root)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _run_cli(
        self, args: list[str], cwd: Path, input_text: str | None = None
    ) -> subprocess.CompletedProcess:
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

    def _write_statement(self, path: Path, rows: list[str]) -> None:
        path.write_text(
            "\n".join(["Date,Description,Amount,Currency", *rows]),
            encoding="utf-8",
        )

    def test_import_prompts_to_categorize_and_saves_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])

            result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1 imported records have no category", result.stdout)
            self.assertIn("PARKNSHOP", result.stdout)
            self.assertNotIn("(may.csv)", result.stdout)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["category"], "Groceries")
            self.assertEqual(row["needs_review"], "false")
            self.assertIn("manual_correction", row["flags"])

            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                [correction] = list(csv.DictReader(fh))
            self.assertEqual(correction["transaction_id"], row["transaction_id"])
            self.assertEqual(correction["category"], "Groceries")
            self.assertEqual(correction["reason"], "Categorized interactively")

            rerun = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"], cwd=root
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["category"], "Groceries")

    def test_import_prompt_can_skip_one_and_quit_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                    "2026-05-05,MTR,-8.00,HKD",
                    "2026-05-06,WELLCOME,-60.00,HKD",
                ],
            )

            result = self._run_cli(
                ["import", str(statement)], cwd=root, input_text="\nq\n"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([row["category"] for row in rows], ["Unknown"] * 3)
            corrections = (root / "corrections.csv").read_text(encoding="utf-8")
            self.assertEqual(len(corrections.strip().splitlines()), 1)

    def test_import_prompt_shows_placeholder_for_blank_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,,-120.50,HKD"])

            result = self._run_cli(
                ["import", str(statement)], cwd=root, input_text="\n"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "[1/1] 2026-05-04  -120.50 HKD  (no description)", result.stdout
            )

    def test_no_interactive_flag_disables_categorization_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])

            result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("have no category", result.stdout)
            self.assertIn("1 records are still uncategorized", result.stdout)

    def test_import_rejects_previously_processed_file_without_replace_or_reset(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            second = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )

            self.assertEqual(second.returncode, 2)
            self.assertIn("Already imported source file(s): may.csv", second.stderr)
            self.assertIn("--replace", second.stderr)
            self.assertIn("--reset", second.stderr)

    def test_import_replace_reprocesses_source_and_drops_stale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                    "2026-05-05,WELLCOME,-60.00,HKD",
                ],
            )
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])

            replacement = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"], cwd=root
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([row["merchant"] for row in rows], ["PARKNSHOP"])

    def test_import_reset_reprocesses_source_and_clears_old_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])
            first = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            reset = self._run_cli(
                ["import", str(statement), "--reset", "--replace", "--no-interactive"],
                cwd=root,
            )

            self.assertEqual(reset.returncode, 0, reset.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["category"], "Unknown")
            self.assertEqual(row["needs_review"], "true")
            corrections = (root / "corrections.csv").read_text(encoding="utf-8")
            self.assertEqual(len(corrections.strip().splitlines()), 1)

    def test_review_command_categorizes_transactions_needing_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])
            import_result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            review_result = self._run_cli(
                ["review"],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn("1 records need review", review_result.stdout)
            self.assertIn("Review complete: 1 updated, 0 still need review", review_result.stdout)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["category"], "Groceries")
            self.assertEqual(row["needs_review"], "false")
            self.assertEqual(row["reason"], "Categorized interactively")
            self.assertIn("manual_correction", row["flags"])

            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))
            self.assertEqual(review_rows, [])

            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                [correction] = list(csv.DictReader(fh))
            self.assertEqual(correction["transaction_id"], row["transaction_id"])
            self.assertEqual(correction["category"], "Groceries")

    def test_review_command_reports_when_no_transactions_need_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PARKNSHOP,-120.50,HKD"])
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            review_result = self._run_cli(["review"], cwd=root)

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn("No transactions need review.", review_result.stdout)
            self.assertIn("Review complete: 0 updated, 0 still need review", review_result.stdout)

    def test_hsbc_bank_profile_skips_previous_balance_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "hsbc.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Date,Description,Debit,Credit,Currency",
                        "2026-05-18,PREVIOUS BALANCE,5632.88,,HKD",
                        "2026-05-19,PARKNSHOP,120.50,,HKD",
                        "2026-05-20,SALARY,,20000,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            merchants = {row["merchant"] for row in rows}
            self.assertNotIn("PREVIOUS BALANCE", merchants)
            self.assertEqual(merchants, {"PARKNSHOP", "SALARY"})
            self.assertEqual(len(rows), 2)

    def test_import_skips_opening_closing_and_previous_balance_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-01,Opening Balance,9999.00,HKD",
                    "2026-05-02,PREVIOUS BALANCE,9999.00,HKD",
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                    "2026-05-31,Closing Balance,9878.50,HKD",
                ],
            )

            result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([row["merchant"] for row in rows], ["PARKNSHOP"])

    def test_sequential_imports_accumulate_into_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "may.csv"
            second = root / "june.csv"
            self._write_statement(first, ["2026-05-04,PARKNSHOP,-120.50,HKD"])
            self._write_statement(second, ["2026-06-10,WELLCOME,-60.00,HKD"])

            for statement in [first, second]:
                result = self._run_cli(
                    ["import", str(statement), "--no-interactive"], cwd=root
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            result = self._run_cli(
                ["import", str(second), "--replace", "--no-interactive"], cwd=root
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertIn("Ledger now has 2 records", result.stdout)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {row["merchant"] for row in rows}, {"PARKNSHOP", "WELLCOME"}
            )

    def test_setup_profiles_detect_mox_credit_csv_without_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "mox.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Transaction date,Post date,Description,Billing amount,"
                        "Billing currency,Merchant name,Credit / Debit",
                        "2026-06-01,2026-06-02,CARD PURCHASE,88.00,HKD,Mox Cafe,Debit",
                    ]
                ),
                encoding="utf-8",
            )

            result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["account_id"], "mox_credit_card")
            self.assertEqual(row["payment_method"], "Credit Card")
            self.assertEqual(row["original_amount"], "-88.00")

    def test_packaged_starter_profiles_match_examples(self) -> None:
        packaged_dir = REPO_ROOT / "honeymoney" / "data" / "profiles"
        examples_dir = REPO_ROOT / "examples" / "profiles"
        packaged = sorted(path.name for path in packaged_dir.glob("*.json"))
        self.assertEqual(
            packaged, sorted(path.name for path in examples_dir.glob("*.json"))
        )
        self.assertIn("mox_credit_card_pdf.json", packaged)
        for name in packaged:
            self.assertEqual(
                json.loads((packaged_dir / name).read_text(encoding="utf-8")),
                json.loads((examples_dir / name).read_text(encoding="utf-8")),
                f"{name} differs between honeymoney/data/profiles and examples/profiles",
            )

    def test_status_command_reports_period_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                    "2026-05-05,MTR,-8.00,HKD",
                    "2026-06-01,WELLCOME,-60.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\nq\n",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            result = self._run_cli(["status", "--month", "2026-05"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Status for 2026-05-01 to 2026-05-31", result.stdout)
            self.assertIn("Statements processed: 1", result.stdout)
            self.assertIn("Records processed:    2", result.stdout)
            self.assertIn("Categorized:          1", result.stdout)
            self.assertIn("Uncategorized:        1", result.stdout)
            self.assertIn("Ledger total: 3 records", result.stdout)

    def test_status_command_without_ledger_explains_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)

            result = self._run_cli(["status"], cwd=root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No processed records found", result.stdout)
            self.assertIn("honeymoney import", result.stdout)

    def test_report_command_writes_self_contained_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                    "2026-05-05,SALARY,20000.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            result = self._run_cli(
                ["report", "--month", "2026-05", "--no-open"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Report written to", result.stdout)
            report_path = root / "output" / "report.html"
            self.assertTrue(report_path.exists())
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("Honeymoney Report", html)
            self.assertIn("2026-05-01 to 2026-05-31", html)
            self.assertIn("PARKNSHOP", html)
            for external_reference in ['src="http', "src='http", 'href="http', "url(http", "@import"]:
                self.assertNotIn(external_reference, html)

    def test_report_command_defaults_to_current_calendar_month(self) -> None:
        class FixedDate(date):
            @classmethod
            def today(cls) -> date:
                return cls(2026, 7, 7)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "output"
            output_dir.mkdir()
            ledger_path = output_dir / "categorized.csv"
            with ledger_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "date",
                        "merchant",
                        "original_description",
                        "category",
                        "amount_hkd",
                        "account",
                        "owner",
                        "needs_review",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "date": "2026-07-04",
                        "merchant": "JULY SHOP",
                        "original_description": "JULY SHOP",
                        "category": "Groceries",
                        "amount_hkd": "-10.00",
                    }
                )
                writer.writerow(
                    {
                        "date": "2026-06-30",
                        "merchant": "JUNE SHOP",
                        "original_description": "JUNE SHOP",
                        "category": "Groceries",
                        "amount_hkd": "-20.00",
                    }
                )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"paths": {"output": str(ledger_path)}}), encoding="utf-8"
            )
            report_path = output_dir / "report.html"

            with patch("honeymoney.cli.date", FixedDate), redirect_stdout(io.StringIO()):
                result = _report_command(
                    [
                        "--config",
                        str(config_path),
                        "--output",
                        str(report_path),
                        "--no-open",
                    ]
                )

            self.assertEqual(result, 0)
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("2026-07-01 to 2026-07-31", html)
            self.assertIn("JULY SHOP", html)
            self.assertNotIn("JUNE SHOP", html)

    def test_report_command_can_filter_by_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "mixed.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                    "2026-06-01,WELLCOME,-60.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            result = self._run_cli(
                ["report", "--month", "2026-05", "--no-open"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(1 transactions)", result.stdout)
            html = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn("2026-05-01 to 2026-05-31", html)
            self.assertIn("PARKNSHOP", html)
            self.assertNotIn("WELLCOME", html)


class StatusLineTest(unittest.TestCase):
    def test_updates_in_place_and_pads_over_previous_text(self) -> None:
        stream = io.StringIO()
        status = _StatusLine(stream=stream, enabled=True)

        status.update("longer message")
        status.update("short")

        self.assertEqual(stream.getvalue(), "\rlonger message\rshort" + " " * 9)

    def test_clear_erases_the_line(self) -> None:
        stream = io.StringIO()
        status = _StatusLine(stream=stream, enabled=True)

        status.update("busy")
        status.clear()

        self.assertEqual(stream.getvalue(), "\rbusy\r    \r")
        status.clear()
        self.assertEqual(stream.getvalue(), "\rbusy\r    \r")

    def test_disabled_when_stream_is_not_a_tty(self) -> None:
        stream = io.StringIO()
        status = _StatusLine(stream=stream)

        status.update("busy")
        status.clear()

        self.assertEqual(stream.getvalue(), "")


class StatusLineTtyTest(unittest.TestCase):
    def test_import_shows_status_line_with_ollama_progress_on_tty(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                prompt = json.loads(payload["prompt"])
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": transaction["id"],
                                "category": "Groceries",
                                "owner": "Household",
                                "confidence": 0.9,
                                "reason": "Supermarket merchant",
                            }
                            for transaction in prompt["transactions"]
                        ]
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup_result = subprocess.run(
                [sys.executable, "-m", "honeymoney.cli", "setup", "--root", str(root)],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
            statement = root / "may.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"] = {
                "enabled": True,
                "url": f"http://127.0.0.1:{server.server_address[1]}/api/generate",
                "model": "test",
                "batch_size": 20,
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "honeymoney.cli",
                        "import",
                        str(statement),
                    ],
                    cwd=root,
                    env=env,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    close_fds=True,
                )
                os.close(slave)
                output = b""
                while True:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                self.assertEqual(process.wait(timeout=60), 0, output.decode(errors="replace"))
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()

            text = output.decode(errors="replace")
            self.assertIn("\r", text)
            self.assertIn("Importing statements... (1/1) may.csv", text)
            self.assertIn(
                "Categorizing via Ollama... batch 1/1 (transactions 1 of 1)", text
            )
            self.assertIn("Import complete: 1 successful records", text)


class ResolvePeriodTest(unittest.TestCase):
    TODAY = date(2026, 7, 7)

    def test_defaults_to_current_calendar_month(self) -> None:
        self.assertEqual(
            _resolve_period(None, None, None, today=self.TODAY),
            (date(2026, 7, 1), date(2026, 7, 31)),
        )

    def test_month_name_uses_current_year(self) -> None:
        for value in ["may", "May", "MAY", "may "]:
            self.assertEqual(
                _resolve_period(value, None, None, today=self.TODAY),
                (date(2026, 5, 1), date(2026, 5, 31)),
            )

    def test_month_abbreviation_and_numeric_month(self) -> None:
        self.assertEqual(
            _resolve_period("feb", None, None, today=self.TODAY),
            (date(2026, 2, 1), date(2026, 2, 28)),
        )
        self.assertEqual(
            _resolve_period("2024-02", None, None, today=self.TODAY),
            (date(2024, 2, 1), date(2024, 2, 29)),
        )

    def test_start_and_end_dates(self) -> None:
        self.assertEqual(
            _resolve_period(None, "2026-01-15", "2026-03-01", today=self.TODAY),
            (date(2026, 1, 15), date(2026, 3, 1)),
        )
        self.assertEqual(
            _resolve_period(None, "2026-06-15", None, today=self.TODAY),
            (date(2026, 6, 15), self.TODAY),
        )

    def test_rejects_month_combined_with_start_or_end(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_period("may", "2026-05-01", None, today=self.TODAY)

    def test_rejects_unknown_month(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_period("not-a-month", None, None, today=self.TODAY)

    def test_rejects_start_after_end(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_period(None, "2026-06-01", "2026-05-01", today=self.TODAY)


if __name__ == "__main__":
    unittest.main()
