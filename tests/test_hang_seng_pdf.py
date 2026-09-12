from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pymupdf

from honeymoney.hang_seng_pdf import Word, bank_rows, card_rows
from honeymoney.importers import (
    _import_pdf,
    _import_transactions,
    _validate_profile,
    preview_profile_input,
)
from tests.golden_helpers import base_config, load_profile

FIXTURES = Path(__file__).parent / "fixtures" / "import_profiles"


def layout(kind):
    return json.loads(
        (FIXTURES / f"hang_seng_{kind}_pdf" / "synthetic_layout.json").read_text()
    )


def pages(lines):
    return [[[Word(text, x) for x, text in line] for line in lines]]


def pdf_bytes(lines):
    with pymupdf.open() as document:
        page = document.new_page(width=612, height=792)
        for index, line in enumerate(lines):
            for x, text in line:
                page.insert_text((x, 30 + index * 14), text, fontsize=5)
        return document.tobytes()


class HangSengPdfTest(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_bundled_and_example_profiles_match(self):
        repo_root = Path(__file__).resolve().parents[1]
        for kind in ("bank", "credit_card"):
            profile_name = f"hang_seng_{kind}_pdf.json"
            bundled = json.loads(
                (repo_root / "honeymoney/data/profiles" / profile_name).read_text()
            )
            example = json.loads(
                (repo_root / "examples/profiles" / profile_name).read_text()
            )
            self.assertEqual(bundled, example)

    def test_pdf_import_preserves_source_facts_and_identity_locators(self):
        for kind, count in (("bank", 2), ("credit_card", 2)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "not-a-statement-date-1999.pdf"
                source.write_bytes(pdf_bytes(layout(kind)))
                profile = load_profile(f"hang_seng_{kind}_pdf.json")

                rows, warnings = preview_profile_input(
                    profile,
                    str(profile["id"]),
                    source,
                    {},
                )
                _, _, identities = _import_pdf(
                    source, profile, {}, source.parent, include_identity_records=True
                )

                self.assertEqual(warnings, [])
                self.assertEqual(len(rows), count)
                self.assertEqual(len(identities), count)
                self.assertEqual(rows[0]["transaction_date"], "2026-12-31")
                self.assertEqual(
                    rows[1]["transaction_date"],
                    "2027-01-02" if kind == "bank" else "2027-01-03",
                )
                self.assertEqual(
                    rows[0]["original_description"],
                    "SYNTHETIC TRANSFER REFERENCE ALPHA"
                    if kind == "bank"
                    else "SYNTHETIC SHOP REFERENCE ALPHA",
                )
                self.assertEqual(
                    [row["original_amount"] for row in rows],
                    ["30.00", "-30.00"] if kind == "bank" else ["-20.00", "30.00"],
                )
                self.assertEqual(
                    [row["source_row"] for row in rows],
                    ["4", "6"] if kind == "bank" else ["5", "7"],
                )
                self.assertEqual({row["source_page"] for row in rows}, {"1"})
                self.assertEqual(rows[0]["statement_closing_balance"], "0.00")
                self.assertEqual(
                    rows[0]["posting_date"], "" if kind == "bank" else "2027-01-02"
                )
                self.assertEqual(
                    [identity.locator.components for identity in identities],
                    [(1, 4), (1, 6)] if kind == "bank" else [(1, 5), (1, 7)],
                )

    def test_profile_validation_rejects_unknown_hang_seng_layout(self):
        profile = load_profile("hang_seng_bank_pdf.json")
        profile["pdf"]["word_rows"] = "hang_seng_unknown"
        with self.assertRaisesRegex(ValueError, "pdf.word_rows"):
            _validate_profile(profile, Path("profile.json"), base_config())

    def test_profile_validation_rejects_unknown_layout_sources(self):
        profile = load_profile("hang_seng_bank_pdf.json")
        profile["pdf"]["columns"]["description"] = "Unknown"
        with self.assertRaisesRegex(ValueError, "unknown Hang Seng sources: Unknown"):
            _validate_profile(profile, Path("profile.json"), base_config())

        profile = load_profile("hang_seng_bank_pdf.json")
        profile["pdf"]["columns"].pop("statement_opening_balance")
        profile["pdf"]["columns"]["merchant"] = "Description"
        _validate_profile(profile, Path("profile.json"), base_config())

    def test_importer_applies_skip_patterns_and_transaction_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "statement.pdf"
            source.write_bytes(pdf_bytes(layout("bank")))
            profile = load_profile("hang_seng_bank_pdf.json")
            profile["skip_descriptions"] = ["SYNTHETIC TRANSFER"]

            rows, _, identities = _import_pdf(
                source,
                profile,
                {},
                source.parent,
                include_identity_records=True,
            )

            self.assertEqual([row["original_amount"] for row in rows], ["-30.00"])
            self.assertEqual(
                [identity.locator.components for identity in identities], [(1, 6)]
            )
            profile.pop("skip_descriptions")
            with patch("honeymoney.importers.MAX_PDF_TRANSACTION_ROWS", 1):
                with self.assertRaisesRegex(ValueError, "transaction rows exceed 1"):
                    _import_pdf(source, profile, {}, source.parent)

    def test_cli_import_creates_ready_records_from_synthetic_pdfs(self):
        for kind in ("bank", "credit_card"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "money"
                setup = self._run("setup", "--root", str(root), "--json")
                self.assertEqual(setup.returncode, 0, setup.stderr)
                source = root / f"synthetic-{kind}.pdf"
                source.write_bytes(pdf_bytes(layout(kind)))
                profile_id = f"hang_seng_{kind}_pdf"
                account_id = f"hang_seng_{kind}"
                binding_id = f"synthetic-{kind}"
                config = root / "config.json"
                bound = self._run(
                    "profile",
                    "bind",
                    binding_id,
                    "--pattern",
                    source.name,
                    "--profile",
                    profile_id,
                    "--owner",
                    "Household",
                    "--account",
                    f"{account_id}={account_id}=Synthetic Hang Seng",
                    "--config",
                    str(config),
                    "--json",
                )
                self.assertEqual(bound.returncode, 0, bound.stderr)

                imported = self._run(
                    "import",
                    str(source),
                    "--binding",
                    binding_id,
                    "--config",
                    str(config),
                    "--no-interactive",
                    "--json",
                )

                self.assertEqual(imported.returncode, 0, imported.stderr)
                payload = json.loads(imported.stdout)
                self.assertEqual(payload["data"]["import_count"], 1)
                self.assertEqual(payload["data"]["statement_transaction_count"], 2)
                records = list((root / ".honeymoney" / "import-records").iterdir())
                self.assertEqual(len(records), 1)
                summary = json.loads((records[0] / "summary.json").read_text())
                self.assertTrue(summary["ready"])
                with (records[0] / "transactions.csv").open(
                    encoding="utf-8", newline=""
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(
                    [row["original_amount"] for row in rows],
                    ["30.00", "-30.00"] if kind == "bank" else ["-20.00", "30.00"],
                )
                self.assertEqual(
                    rows[0]["original_description"],
                    "SYNTHETIC TRANSFER REFERENCE ALPHA"
                    if kind == "bank"
                    else "SYNTHETIC SHOP REFERENCE ALPHA",
                )

    def test_truncated_card_table_fails_reader(self):
        truncated = layout("credit_card")[:-1]
        with self.assertRaisesRegex(ValueError, "card table has no end marker"):
            card_rows(pages(truncated))

    def test_cli_import_rejects_a_truncated_card_table(self):
        truncated = layout("credit_card")[:-1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            setup = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic-truncated-card.pdf"
            source.write_bytes(pdf_bytes(truncated))
            config = root / "config.json"
            bound = self._run(
                "profile",
                "bind",
                "synthetic-truncated-card",
                "--pattern",
                source.name,
                "--profile",
                "hang_seng_credit_card_pdf",
                "--owner",
                "Household",
                "--account",
                "hang_seng_credit_card=hang_seng_credit_card=Synthetic Hang Seng",
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)

            imported = self._run(
                "import",
                str(source),
                "--binding",
                "synthetic-truncated-card",
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )

            self.assertEqual(imported.returncode, 2, imported.stdout)
            self.assertEqual(
                json.loads(imported.stdout)["errors"][0]["code"], "import_failed"
            )
            records = root / ".honeymoney" / "import-records"
            imported_records = list(records.iterdir())
            self.assertEqual(len(imported_records), 1)
            record = imported_records[0]
            summary = json.loads((record / "summary.json").read_text())
            attempt = json.loads((record / "attempts/00000001.json").read_text())
            self.assertFalse(summary["ready"])
            self.assertEqual(summary["statement_transaction_count"], 0)
            self.assertEqual(attempt["outcome"], "failure")

    def test_bank_next_month_and_year_come_from_statement(self):
        lines = layout("bank")
        lines[0] = [[414, "Date :05 February 2028"]]
        lines[3][0][1] = "31 Jan"
        lines[5][0][1] = "02 Feb"
        lines[7][0][1] = "05 Feb"
        result = bank_rows(pages(lines))
        self.assertEqual(
            [row[0]["Date"] for row in result], ["2028-01-31", "2028-02-02"]
        )

    def test_wrapping_and_inherited_date_cross_a_repeated_page_header(self):
        lines = layout("bank")
        lines[5] = lines[5][1:]
        result = bank_rows(pages(lines[:5]) + pages([lines[1]] + lines[5:]))
        self.assertEqual([row[0]["Date"] for row in result], ["2026-12-31"] * 2)
        self.assertEqual(result[1][1:], (2, 2))
        self.assertEqual(
            result[1][0]["Description"], "SYNTHETIC REPAYMENT REFERENCE BETA"
        )

    def test_bank_page_balance_rollover_keeps_statement_endpoints(self):
        lines = layout("bank")
        carried = [[74, "31 Dec"], [108, "C/F Balance"], [495, "30.00"]]
        brought = [[74, "31 Dec"], [108, "B/F Balance"], [495, "30.00"]]
        statement = pages(lines[:5] + [carried]) + pages(
            [lines[1], brought] + lines[5:]
        )

        result = bank_rows(statement)

        self.assertEqual([row[0]["Opening"] for row in result], ["0.00", "0.00"])
        self.assertEqual([row[0]["Closing"] for row in result], ["0.00", "0.00"])

        brought[-1][1] = "31.00"
        with self.assertRaisesRegex(ValueError, "balance carry"):
            bank_rows(
                pages(lines[:5] + [carried]) + pages([lines[1], brought] + lines[5:])
            )

        brought[-1][1] = "30.00"
        brought[0][1] = "30 Dec"
        with self.assertRaisesRegex(ValueError, "balance carry"):
            bank_rows(
                pages(lines[:5] + [carried]) + pages([lines[1], brought] + lines[5:])
            )

    def test_bank_summary_marker_can_follow_the_closed_table(self):
        lines = layout("bank")

        result = bank_rows(pages(lines[:-1]) + pages([lines[-1]]))

        self.assertEqual(len(result), 2)

    def test_bank_requires_summary_after_the_final_closing_balance(self):
        lines = layout("bank")
        important_notes = [[108, "Important Notes"]]
        same_date_partial = lines[:5] + [
            [[74, "05 Jan"], [108, "C/F Balance"], [495, "30.00"]]
        ]

        for statement in (
            lines[:-1],
            lines[:-1] + [important_notes],
            same_date_partial,
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(ValueError, "no transaction summary"):
                    bank_rows(pages(statement))

    def test_bank_summary_before_the_closing_balance_is_invalid(self):
        lines = layout("bank")
        marker = lines.pop()
        lines.insert(-1, marker)

        with self.assertRaisesRegex(ValueError, "table has no closing balance"):
            bank_rows(pages(lines))

    def test_bank_transaction_summary_merchant_text_remains_a_transaction(self):
        lines = layout("bank")
        lines[3][1][1] = "Transaction Summary PURCHASE"

        result = bank_rows(pages(lines))

        self.assertEqual(
            result[0][0]["Description"],
            "Transaction Summary PURCHASE REFERENCE ALPHA",
        )

    def test_bank_rejects_table_data_after_the_summary(self):
        lines = layout("bank")
        forbidden = (
            [[74, "Date Transaction Details Deposit Withdrawal Balance in HKD"]],
            [[74, "05 Jan"], [108, "SYNTHETIC LATE ROW"], [319, "1.00"]],
            [[74, "05 Jan"], [108, "B/F Balance"], [495, "0.00"]],
            [[74, "05 Jan"], [108, "C/F Balance"], [495, "0.00"]],
        )

        for extra_line in forbidden:
            statements = (
                pages(lines + [extra_line]),
                pages(lines) + pages([extra_line]),
            )
            for statement in statements:
                with self.subTest(extra_line=extra_line, page_count=len(statement)):
                    with self.assertRaisesRegex(
                        ValueError, "data after transaction summary"
                    ):
                        bank_rows(statement)

    def test_bank_allows_summary_totals_and_notes_after_the_marker(self):
        lines = layout("bank")
        lines.extend(
            [
                [[108, "TOTAL DEPOSITS"], [319, "30.00"]],
                [[108, "Important Notes"]],
                [[108, "Synthetic summary note"]],
            ]
        )

        result = bank_rows(pages(lines))

        self.assertEqual(len(result), 2)

    def test_bank_terminal_balance_date_must_match_statement_date(self):
        lines = layout("bank")
        carried = [[74, "31 Dec"], [108, "C/F Balance"], [495, "30.00"]]
        with self.assertRaisesRegex(ValueError, "closing balance date"):
            bank_rows(pages(lines[:5] + [carried]))

        for raw_date in ("", "32 Dec"):
            with self.subTest(raw_date=raw_date):
                broken = deepcopy(lines)
                broken[7][0][1] = raw_date
                with self.assertRaisesRegex(
                    ValueError, "Invalid Hang Seng balance date"
                ):
                    bank_rows(pages(broken))

    def test_cli_import_rejects_a_bank_statement_without_its_summary(self):
        lines = layout("bank")
        truncated = lines[:5] + [[[74, "05 Jan"], [108, "C/F Balance"], [495, "30.00"]]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            setup = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic-truncated-bank.pdf"
            source.write_bytes(pdf_bytes(truncated))
            config = root / "config.json"
            bound = self._run(
                "profile",
                "bind",
                "synthetic-truncated-bank",
                "--pattern",
                source.name,
                "--profile",
                "hang_seng_bank_pdf",
                "--owner",
                "Household",
                "--account",
                "hang_seng_bank=hang_seng_bank=Synthetic Hang Seng",
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)

            imported = self._run(
                "import",
                str(source),
                "--binding",
                "synthetic-truncated-bank",
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )

            self.assertEqual(imported.returncode, 2, imported.stdout)
            self.assertEqual(
                json.loads(imported.stdout)["errors"][0]["code"], "import_failed"
            )
            records = list((root / ".honeymoney" / "import-records").iterdir())
            self.assertEqual(len(records), 1)
            summary = json.loads((records[0] / "summary.json").read_text())
            attempt = json.loads((records[0] / "attempts/00000001.json").read_text())
            self.assertFalse(summary["ready"])
            self.assertEqual(summary["statement_transaction_count"], 0)
            self.assertEqual(attempt["outcome"], "failure")

    def test_parse_cli_rejects_a_bank_statement_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic-truncated-bank.pdf"
            lines = layout("bank")
            truncated = lines[:5] + [
                [[74, "05 Jan"], [108, "C/F Balance"], [495, "30.00"]]
            ]
            source.write_bytes(pdf_bytes(truncated))

            parsed = self._run(
                "parse",
                str(source),
                "--profile",
                "hang_seng_bank_pdf",
                "--json",
            )

            self.assertEqual(parsed.returncode, 2, parsed.stdout)
            self.assertEqual(
                json.loads(parsed.stdout)["errors"][0]["code"], "parse_failed"
            )
            self.assertEqual(list(root.iterdir()), [source])

    def test_bank_rejects_data_after_a_closed_table_without_a_balance_pair(self):
        lines = layout("bank")
        carried = [[74, "31 Dec"], [108, "C/F Balance"], [495, "30.00"]]
        first_page = pages(lines[:5] + [carried])
        cases = (
            first_page + pages(lines[5:]),
            first_page + pages([lines[1]] + lines[5:7]),
        )
        for statement in cases:
            with self.subTest(statement=statement):
                with self.assertRaises(ValueError):
                    bank_rows(statement)

        result = bank_rows(pages(lines) + pages([[[108, "Important Notes"]]]))
        self.assertEqual(len(result), 2)

    def test_continuation_page_without_repeated_header_fails(self):
        bank = layout("bank")
        card = layout("credit_card")
        cases = (
            (bank_rows, pages(bank[:5]) + pages(bank[5:])),
            (card_rows, pages(card[:6]) + pages(card[6:])),
        )
        for reader, statement_pages in cases:
            with self.subTest(reader=reader.__name__):
                with self.assertRaisesRegex(ValueError, "continuation page"):
                    reader(statement_pages)

    def test_failed_import_warning_does_not_echo_invalid_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "statement.pdf"
            lines = layout("bank")
            private_date = "31 February 2027"
            lines[0][0][1] = f"Date :{private_date}"
            source.write_bytes(pdf_bytes(lines))

            rows, warnings, reports = _import_transactions(
                [source],
                [load_profile("hang_seng_bank_pdf.json")],
                {},
                source.parent,
                False,
                {},
                None,
            )

            self.assertEqual(rows, [])
            self.assertEqual(reports[0]["status"], "failed")
            failure = json.dumps({"warnings": warnings, "reports": reports})
            self.assertIn("Invalid Hang Seng statement date", failure)
            self.assertNotIn(private_date, failure)

    def test_invalid_transaction_dates_use_fixed_errors(self):
        bank = layout("bank")
        bank[3][0][1] = "32 Dec"
        card_transaction = layout("credit_card")
        card_transaction[4][0][1] = "31 PRIVATE 2026"
        card_posting = layout("credit_card")
        card_posting[4][1][1] = "32 JAN 2027"
        cases = (
            (bank_rows, bank, "Invalid Hang Seng transaction date"),
            (card_rows, card_transaction, "Invalid Hang Seng card date"),
            (card_rows, card_posting, "Invalid Hang Seng card date"),
        )
        for reader, lines, message in cases:
            with self.subTest(reader=reader.__name__, message=message):
                with self.assertRaisesRegex(ValueError, message):
                    reader(pages(lines))

    def test_card_signs_and_negative_liability(self):
        for raw, expected in [
            ("30.00CR", "30.00"),
            ("30.00DR", "-30.00"),
            ("30.00-", "30.00"),
            ("0.00", "0.00"),
        ]:
            with self.subTest(raw=raw):
                lines = layout("credit_card")
                lines[6][-1][1] = raw
                lines[1][0][1] = "$5.00-"
                result = card_rows(pages(lines))
                self.assertEqual(result[1][0]["Amount"], expected)
                self.assertEqual(result[1][0]["Closing"], "-5.00")

    def test_missing_conflicting_or_invalid_facts_fail_closed(self):
        bank = layout("bank")
        card = layout("credit_card")
        cases = []
        for index in [0, 2, 7]:
            cases.append((bank_rows, bank[:index] + bank[index + 1 :]))
        broken = deepcopy(bank)
        broken[3].append([400, "1.00"])
        cases.append((bank_rows, broken))
        broken = deepcopy(bank)
        broken[3][0][1] = "32 Dec"
        cases.append((bank_rows, broken))
        broken = deepcopy(bank)
        broken[3][0][1] = "06 Jan"
        cases.append((bank_rows, broken))
        broken = deepcopy(bank)
        broken[3][2][1] = "unknown"
        cases.append((bank_rows, broken))
        broken = deepcopy(bank)
        broken[3] = broken[3][1:]
        cases.append((bank_rows, broken))
        broken = deepcopy(bank)
        broken[0][0][1] = "Date :05 January 2027"
        broken.insert(1, [[414, "Date :06 January 2027"]])
        cases.append((bank_rows, broken))
        broken = deepcopy(card)
        broken[4][0][1] = "31 DEC"
        cases.append((card_rows, broken))
        broken = deepcopy(card)
        broken[4][1][1] = "30 DEC 2026"
        cases.append((card_rows, broken))
        broken = deepcopy(card)
        broken[4] = broken[4][1:]
        cases.append((card_rows, broken))
        for index in [0, 3]:
            cases.append((card_rows, card[:index] + card[index + 1 :]))
        for reader, lines in cases:
            with self.subTest(reader=reader.__name__, lines=lines):
                with self.assertRaises(ValueError):
                    reader(pages(lines))

    def test_bank_preserves_words_between_old_column_bounds(self):
        lines = layout("bank")
        lines[3].insert(2, [265, "EXTRA"])
        result = bank_rows(pages(lines))
        self.assertEqual(
            result[0][0]["Description"], "SYNTHETIC TRANSFER EXTRA REFERENCE ALPHA"
        )
        for x in (350, 440):
            broken = deepcopy(lines)
            broken[3].append([x, "UNRECOGNIZED"])
            with self.assertRaises(ValueError):
                bank_rows(pages(broken))

    def test_card_rejects_unknown_sign_and_foreign_amount(self):
        for amount in ("-30.00", "USD 30.00 234.00", "30.00X"):
            lines = layout("credit_card")
            lines[6][-1][1] = amount
            with self.assertRaises(ValueError):
                card_rows(pages(lines))

    def test_empty_bank_is_not_a_fabricated_transaction(self):
        lines = layout("bank")
        result = bank_rows(pages(lines[:3] + lines[7:]))
        self.assertEqual(result, [])
