from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from honeymoney import cli, importers
from honeymoney.parse_service import FACT_FIELDS, ParseError, parse_statement

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "import_profiles"
CSV = FIXTURES / "mox_credit_card" / "credit_debit_indicator" / "input.csv"
PDF = FIXTURES / "mox_credit_card_pdf" / "accepted_statement" / "input.pdf"


class ParseCliTest(unittest.TestCase):
    def test_csv_public_contract_without_derived_fields_or_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "private-name.csv"
            source.write_bytes(CSV.read_bytes())
            (root / "config.json").write_text("invalid-private-config")
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with (
                patch("honeymoney.cli.load_workspace", side_effect=AssertionError),
                patch("honeymoney.rules.apply_rules", side_effect=AssertionError),
                patch(
                    "honeymoney.ollama.apply_ollama_fallback",
                    side_effect=AssertionError,
                ),
                patch(
                    "honeymoney.importers.value_transaction", side_effect=AssertionError
                ),
                patch("socket.socket", side_effect=AssertionError),
                patch("os.getcwd", return_value=str(root)),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(
                    ["parse", str(source), "--profile", "mox_credit_card", "--json"]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["command"], "parse")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["artifacts"], {})
            self.assertEqual(payload["errors"], [])
            data = payload["data"]
            self.assertEqual(data["parse_schema_version"], 1)
            self.assertEqual(data["engine"], {"name": "honeymoney", "version": "0.2.0"})
            self.assertEqual(len(data["profile_sha256"]), 64)
            self.assertEqual(
                data["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest()
            )
            self.assertIsNone(data["page_count"])
            self.assertEqual(
                [row["posted_amount"] for row in data["rows"]], ["-88.00", "20.00"]
            )
            for row in data["rows"]:
                self.assertEqual(set(row), set(FACT_FIELDS))
                self.assertTrue(all(isinstance(value, str) for value in row.values()))
            self.assertNotIn("private-name", output.getvalue())
            self.assertNotIn(str(root), output.getvalue())
            self.assertEqual(
                {path.name: path.read_bytes() for path in root.iterdir()}, before
            )

    def test_all_bundled_pdf_profiles_include_page_evidence(self) -> None:
        for profile_id in (
            "hsbc_one_pdf",
            "hsbc_hk_credit_card_pdf",
            "mox_bank_pdf",
            "mox_credit_card_pdf",
        ):
            with (
                self.subTest(profile=profile_id),
                patch(
                    "honeymoney.importers.value_transaction", side_effect=AssertionError
                ),
            ):
                result = parse_statement(
                    FIXTURES / profile_id / "accepted_statement" / "input.pdf",
                    profile_id,
                )
                self.assertGreater(result["page_count"], 0)
                self.assertGreater(len(result["rows"]), 0)
                self.assertTrue(result["balance_checks"])
                self.assertTrue(
                    all(
                        row["source_page"] and row["source_row"]
                        for row in result["rows"]
                    )
                )

    def test_source_hash_and_csv_parser_share_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "statement.csv"
            captured = CSV.read_bytes()
            source.write_bytes(captured)
            original = importers.preview_profile_input

            def replace_after_capture(*args, **kwargs):
                source.write_bytes(b"replacement bytes")
                return original(*args, **kwargs)

            with patch.object(
                importers, "preview_profile_input", side_effect=replace_after_capture
            ):
                result = parse_statement(source, "mox_credit_card")
            self.assertEqual(
                result["source_sha256"], hashlib.sha256(captured).hexdigest()
            )
            self.assertEqual(len(result["rows"]), 2)

    def test_source_hash_and_pdf_parser_share_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "statement.pdf"
            captured = PDF.read_bytes()
            source.write_bytes(captured)
            original = importers.preview_profile_input

            def replace_after_capture(*args, **kwargs):
                source.write_bytes(b"replacement bytes")
                return original(*args, **kwargs)

            with patch.object(
                importers, "preview_profile_input", side_effect=replace_after_capture
            ):
                result = parse_statement(source, "mox_credit_card_pdf")
            self.assertEqual(
                result["source_sha256"], hashlib.sha256(captured).hexdigest()
            )
            self.assertGreater(result["page_count"], 0)
            self.assertGreater(len(result["rows"]), 0)

    def test_filename_date_fallback_stays_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "eStatementFile_20311231-private.pdf"
            hsbc_pdf = FIXTURES / "hsbc_one_pdf" / "accepted_statement" / "input.pdf"
            source.write_bytes(hsbc_pdf.read_bytes())
            original_preview = importers.preview_profile_input
            original_statement_date = importers._pdf_sectioned_statement_date

            def record_parser_path(*args, **kwargs):
                self.assertEqual(args[2], source)
                rows, warnings = original_preview(*args, **kwargs)
                warnings.append(f"Parser warning for {source.name}")
                return rows, warnings

            def filename_statement_date(page_lines, pdf_path, settings):
                filename_only = dict(settings)
                filename_only["statement_year_regex"] = ""
                return original_statement_date(page_lines, pdf_path, filename_only)

            with (
                patch.object(
                    importers,
                    "preview_profile_input",
                    side_effect=record_parser_path,
                ),
                patch.object(
                    importers,
                    "_pdf_sectioned_statement_date",
                    side_effect=filename_statement_date,
                ),
            ):
                result = parse_statement(source, "hsbc_one_pdf")
            self.assertTrue(
                all(row["date"].startswith("2031-") for row in result["rows"])
            )
            self.assertIn("Parser warning for statement.pdf", result["warnings"])
            public_labels = json.dumps(
                {
                    "warnings": result["warnings"],
                    "balance_checks": result["balance_checks"],
                }
            )
            self.assertNotIn(source.name, public_labels)

    def test_error_envelopes_hide_source_names_and_parser_details(self) -> None:
        cases = [
            (
                ["parse", "secret.pdf", "--profile", "../private", "--json"],
                "parse_unknown_profile",
            ),
            (
                ["parse", "secret.txt", "--profile", "mox_credit_card", "--json"],
                "parse_unsupported_type",
            ),
            (
                [
                    "parse",
                    "/no-such-private-file.csv",
                    "--profile",
                    "mox_credit_card",
                    "--json",
                ],
                "parse_invalid_source",
            ),
            (
                ["parse", str(CSV), "--profile", "mox_bank_pdf", "--json"],
                "parse_profile_type_mismatch",
            ),
            (["parse", str(CSV), "--json"], "usage_error"),
        ]
        for arguments, expected in cases:
            with (
                self.subTest(code=expected),
                patch.object(sys, "argv", ["honeymoney", *arguments]),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.run(), 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["errors"][0]["code"], expected)
                self.assertNotIn("secret", output.getvalue())
                self.assertNotIn("private", output.getvalue())
        for failure in (
            ValueError("private-source-data"),
            OSError("private-source-path"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(importers, "preview_profile_input", side_effect=failure),
                self.assertRaises(ParseError) as error,
            ):
                parse_statement(CSV, "mox_credit_card")
            self.assertEqual(error.exception.code, "parse_failed")
            self.assertNotIn("private-source", str(error.exception))

    def test_syntax_errors_hide_arguments_in_json_and_human_output(self) -> None:
        cases = (
            [str(CSV), "/synthetic/private-extra.csv", "--profile", "mox_credit_card"],
            [str(CSV), "--profile", "mox_credit_card", "--synthetic-private-option"],
        )
        for arguments in cases:
            with (
                self.subTest(arguments=arguments, output="json"),
                patch.object(
                    sys, "argv", ["honeymoney", "parse", *arguments, "--json"]
                ),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(cli.run(), 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["errors"][0]["code"], "usage_error")
                self.assertEqual(
                    result["errors"][0]["message"], "Invalid parse command arguments"
                )
                self.assertEqual(errors.getvalue(), "")
                self.assertNotIn("private", output.getvalue())

            with (
                self.subTest(arguments=arguments, output="human"),
                patch.object(sys, "argv", ["honeymoney", "parse", *arguments]),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                self.assertEqual(cli.run(), 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), "Invalid parse command arguments\n")
                self.assertNotIn("private", errors.getvalue())

    def test_parse_help_exits_normally(self) -> None:
        with (
            patch.object(sys, "argv", ["honeymoney", "parse", "--help"]),
            redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as exit_status,
        ):
            cli.run()
        self.assertEqual(exit_status.exception.code, 0)
        self.assertIn("usage: honeymoney parse", output.getvalue())

    def test_empty_csv_and_unrecognized_pdf_require_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "empty.csv"
            source.write_bytes(CSV.read_bytes().splitlines(keepends=True)[0])
            with self.assertRaises(ParseError) as error:
                parse_statement(source, "mox_credit_card")
            self.assertEqual(error.exception.code, "parse_no_rows")
        with patch.object(importers, "preview_profile_input", return_value=([], [])):
            with self.assertRaises(ParseError) as error:
                parse_statement(PDF, "mox_credit_card_pdf")
            self.assertEqual(error.exception.code, "parse_no_rows")

    def test_dependency_missing_and_source_warnings(self) -> None:
        with patch.object(
            importers,
            "preview_profile_input",
            side_effect=ModuleNotFoundError("private-dependency"),
        ):
            with self.assertRaises(ParseError) as error:
                parse_statement(PDF, "mox_credit_card_pdf")
            self.assertEqual(error.exception.code, "parse_dependency_missing")
        row = {"flags": "invalid_amount", "posted_amount": "", "date": ""}
        with patch.object(importers, "preview_profile_input", return_value=([row], [])):
            result = parse_statement(CSV, "mox_credit_card")
            self.assertEqual(len(result["warnings"]), 2)
            self.assertEqual(result["rows"][0]["posted_amount"], "")

    def test_invalid_amount_is_blank_and_cannot_reconcile_as_zero(self) -> None:
        rows = [
            {
                "date": "2026-05-01",
                "account_id": "mox_credit_card",
                "account_type": "credit_card",
                "original_amount": "0.00",
                "posted_amount": "0.00",
                "posted_currency": "HKD",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "80.00",
                "source_file": "private.csv",
                "source_row": "1",
                "flags": "uncategorized;invalid_amount",
            },
            {
                "date": "2026-05-02",
                "account_id": "mox_credit_card",
                "account_type": "credit_card",
                "original_amount": "20.00",
                "posted_amount": "20.00",
                "posted_currency": "HKD",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "80.00",
                "source_file": "private.csv",
                "source_row": "2",
                "flags": "uncategorized",
            },
            {
                "date": "2026-05-03",
                "account_id": "mox_credit_card",
                "account_type": "credit_card",
                "original_amount": "0.00",
                "posted_amount": "0.00",
                "posted_currency": "HKD",
                "statement_opening_balance": "100.00",
                "statement_closing_balance": "80.00",
                "source_file": "private.csv",
                "source_row": "3",
                "flags": "uncategorized",
            },
        ]
        with (
            patch.object(importers, "preview_profile_input", return_value=(rows, [])),
            patch.object(
                sys,
                "argv",
                [
                    "honeymoney",
                    "parse",
                    str(CSV),
                    "--profile",
                    "mox_credit_card",
                    "--json",
                ],
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(cli.run(), 0)

        data = json.loads(output.getvalue())["data"]
        self.assertEqual(
            [row["original_amount"] for row in data["rows"]],
            ["", "20.00", "0.00"],
        )
        self.assertEqual(
            [row["posted_amount"] for row in data["rows"]],
            ["", "20.00", "0.00"],
        )
        self.assertEqual(
            data["warnings"], ["One or more rows have invalid source amounts."]
        )
        balance = data["balance_checks"]["mox_credit_card"]
        self.assertEqual(balance["status"], "unavailable")
        self.assertEqual(
            balance["statements"][0]["reason"],
            "One or more posted amounts are unavailable.",
        )

    def test_csv_json_distinguishes_invalid_amounts_from_zero(self) -> None:
        source_text = CSV.read_text()
        header = source_text.splitlines()[0]
        rows = (
            "2026-05-01,2026-05-02,CARD PURCHASE,PRIVATE,HKD,Shop,Debit",
            "2026-05-03,2026-05-04,CARD PURCHASE,,HKD,Shop,Debit",
            "2026-05-05,2026-05-06,CARD PURCHASE,0.00,HKD,Shop,Debit",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "statement.csv"
            source.write_text("\n".join((header, *rows)))
            with redirect_stdout(io.StringIO()) as output:
                code = cli.main(
                    [
                        "parse",
                        str(source),
                        "--profile",
                        "mox_credit_card",
                        "--json",
                    ]
                )

        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())["data"]
        self.assertEqual(
            [row["original_amount"] for row in data["rows"]], ["", "", "0.00"]
        )
        self.assertEqual(
            [row["posted_amount"] for row in data["rows"]], ["", "", "0.00"]
        )
        self.assertEqual(
            data["warnings"], ["One or more rows have invalid source amounts."]
        )

    def test_invalid_dates_and_fixed_year_assumptions_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bad-date.csv"
            source.write_bytes(CSV.read_bytes().replace(b"2026-05-01", b"2026-02-30"))
            result = parse_statement(source, "mox_credit_card")
            self.assertIn(
                "One or more rows have missing or invalid dates.", result["warnings"]
            )
        result = parse_statement(PDF, "mox_credit_card_pdf")
        self.assertTrue(
            any("fixed year 2026" in warning for warning in result["warnings"])
        )

    def test_symlink_and_unknown_bundled_profile_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "link.csv"
            source.symlink_to(CSV.resolve())
            with self.assertRaises(ParseError) as error:
                parse_statement(source, "mox_credit_card")
            self.assertEqual(error.exception.code, "parse_invalid_source")
        with self.assertRaises(ParseError) as error:
            parse_statement(CSV, "not_a_bundled_profile")
        self.assertEqual(error.exception.code, "parse_unknown_profile")

    def test_human_output_is_value_free_and_needs_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "private.csv"
            source.write_bytes(CSV.read_bytes())
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "parse",
                    str(source),
                    "--profile",
                    "mox_credit_card",
                ],
                cwd=temporary,
                env=os.environ
                | {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": str(REPO_ROOT),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Parsed 2 statement rows", result.stdout)
            self.assertNotIn("88.00", result.stdout)
            self.assertNotIn("Mox Cafe", result.stdout)
            self.assertEqual(list(Path(temporary).iterdir()), [source])
