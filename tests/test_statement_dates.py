import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.importers import (
    _import_transactions,
    parse_statement,
    preview_profile_input,
)
from tests.golden_helpers import FIXTURE_DIR, base_config, load_profile, starter_profile

_BUNDLED_PDF_STATEMENT_LINES = {
    "hsbc_one_pdf.json": ("Statement Date 05 January 2026", "2026-01-05"),
    "hsbc_hk_credit_card_pdf.json": (
        "Statement Date 05 January 2026",
        "2026-01-05",
    ),
    "mox_bank_pdf.json": ("Statement date: 5 January 2026", "2026-01-05"),
    "mox_credit_card_pdf.json": (
        "Statement date: 5 January 2026",
        "2026-01-05",
    ),
}


class StatementDateSourceReportTest(unittest.TestCase):
    def test_all_bundled_pdf_profiles_extract_their_printed_statement_date(
        self,
    ) -> None:
        for profile_name, (
            statement_line,
            expected_date,
        ) in _BUNDLED_PDF_STATEMENT_LINES.items():
            with self.subTest(profile=profile_name):
                report = _fake_pdf_source_report(
                    load_profile(profile_name), [[statement_line]]
                )

                self.assertEqual(report["statement_date"], expected_date)
                self.assertEqual(report["statement_date_source_pages"], [1])

    def test_repeated_equal_dates_keep_all_source_pages(self) -> None:
        report = _fake_pdf_source_report(
            _profile_with_test_statement_date_rule(),
            [
                ["Statement Date: 05 January 2026"],
                ["Supplemental information"],
                [
                    "Statement Date: 05 January 2026",
                    "Statement Date: 05 January 2026",
                ],
            ],
        )

        self.assertEqual(report["statement_date"], "2026-01-05")
        self.assertEqual(report["statement_date_source_pages"], [1, 3])

    def test_missing_partial_invalid_and_conflicting_dates_are_null(self) -> None:
        cases = {
            "missing": [["Statement summary"]],
            "partial": [["Statement Date: 05 January"]],
            "invalid": [["Statement Date: 31 February 2026"]],
            "same_page_conflict": [
                [
                    "Statement Date: 05 January 2026",
                    "Statement Date: 06 January 2026",
                ]
            ],
            "different_page_conflict": [
                ["Statement Date: 05 January 2026"],
                ["Statement Date: 06 January 2026"],
            ],
            "valid_and_partial": [
                [
                    "Statement Date: 05 January 2026",
                    "Statement Date: 05 January",
                ]
            ],
            "valid_and_invalid": [
                [
                    "Statement Date: 05 January 2026",
                    "Statement Date: 31 February 2026",
                ]
            ],
        }
        for name, page_lines in cases.items():
            with self.subTest(case=name):
                report = _fake_pdf_source_report(
                    _profile_with_test_statement_date_rule(), page_lines
                )

                self.assertIsNone(report["statement_date"])
                self.assertEqual(report["statement_date_source_pages"], [])
                self.assertIsNone(json.loads(json.dumps(report))["statement_date"])

    def test_date_text_ambiguous_across_formats_is_null(self) -> None:
        profile = _profile_with_test_statement_date_rule(
            date_formats=["%d/%m/%Y", "%m/%d/%Y"]
        )

        report = _fake_pdf_source_report(profile, [["Statement Date: 01/02/2026"]])

        self.assertIsNone(report["statement_date"])
        self.assertEqual(report["statement_date_source_pages"], [])

    def test_other_dates_and_statement_year_do_not_supply_statement_date(self) -> None:
        profile = _profile_with_test_statement_date_rule()
        profile["statement_year"] = 2026
        report = _fake_pdf_source_report(
            profile,
            [
                [
                    "Payment Due Date: 05 January 2026",
                    "01 Jan 02 Jan SYNTHETIC PURCHASE -10.00",
                ]
            ],
            filename="statement-2026-01-05.pdf",
        )

        self.assertIsNone(report["statement_date"])
        self.assertEqual(report["statement_date_source_pages"], [])

    def test_profile_without_statement_date_rule_returns_null(self) -> None:
        profile = load_profile("mox_bank_pdf.json")
        profile["pdf"].pop("statement_date", None)

        report = _fake_pdf_source_report(profile, [["Statement Date: 05 January 2026"]])

        self.assertIsNone(report["statement_date"])
        self.assertEqual(report["statement_date_source_pages"], [])

    def test_statement_date_phrase_in_transaction_description_is_not_evidence(
        self,
    ) -> None:
        for profile_name in _BUNDLED_PDF_STATEMENT_LINES:
            with self.subTest(profile=profile_name):
                report = _fake_pdf_source_report(
                    load_profile(profile_name),
                    [["01 Jan 02 Jan MERCHANT Statement Date: 05 January 2026"]],
                    filename="eStatementFile_20260105.pdf",
                )
                self.assertIsNone(report["statement_date"])
                self.assertEqual(report["statement_date_source_pages"], [])

    def test_bundled_profiles_do_not_infer_missing_statement_dates(self) -> None:
        cases = {
            "hsbc_one_pdf.json": ([], "eStatementFile_20260105.pdf"),
            "hsbc_hk_credit_card_pdf.json": (
                ["Payment Due Date 05 January 2026"],
                "statement.pdf",
            ),
            "mox_bank_pdf.json": (
                ["01 Jan 02 Jan SYNTHETIC PURCHASE -10.00"],
                "statement-2026-01-05.pdf",
            ),
            "mox_credit_card_pdf.json": (
                ["Payment due date: 5 January 2026"],
                "statement.pdf",
            ),
        }
        for profile_name, (page_lines, filename) in cases.items():
            with self.subTest(profile=profile_name):
                report = _fake_pdf_source_report(
                    load_profile(profile_name), [page_lines], filename=filename
                )

                self.assertIsNone(report["statement_date"])
                self.assertEqual(report["statement_date_source_pages"], [])

    def test_csv_source_report_uses_json_null_and_empty_pages(self) -> None:
        fixture = (
            FIXTURE_DIR
            / "import_profiles"
            / "starter_csv"
            / "balances_ignored"
            / "input.csv"
        )

        _, _, reports, identity_sources = _import_transactions(
            [fixture],
            [starter_profile()],
            base_config(),
            fixture.parent,
            False,
            {},
            None,
            include_identity_sources=True,
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(len(identity_sources), 1)
        self.assertIsNone(reports[0]["statement_date"])
        self.assertEqual(reports[0]["statement_date_source_pages"], [])
        encoded = json.dumps(reports[0], sort_keys=True)
        self.assertIn('"statement_date": null', encoded)
        self.assertEqual(json.loads(encoded), reports[0])

    def test_skipped_and_failed_pdf_reports_are_null(self) -> None:
        profile = _profile_with_test_statement_date_rule()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "statement.pdf"
            statement.write_bytes(b"%PDF-1.4 synthetic")

            disabled_config = base_config()
            disabled_config["pdf"]["enabled"] = False
            _, _, skipped_reports = _import_transactions(
                [statement],
                [profile],
                disabled_config,
                root,
                False,
                {},
                None,
            )

            fake_pdfplumber = types.SimpleNamespace(
                open=lambda path: (_ for _ in ()).throw(ValueError("synthetic failure"))
            )
            with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
                _, _, failed_reports = _import_transactions(
                    [statement],
                    [profile],
                    base_config(),
                    root,
                    False,
                    {},
                    None,
                )

        for report in [*skipped_reports, *failed_reports]:
            self.assertIsNone(report["statement_date"])
            self.assertEqual(report["statement_date_source_pages"], [])

    def test_pdf_failure_after_date_extraction_discards_statement_date(self) -> None:
        profile = _profile_with_test_statement_date_rule()
        profile["pdf"]["row_regex"] = "("
        with _fake_pdf([["Statement Date: 05 January 2026"]]) as statement:
            _, _, reports = _import_transactions(
                [statement],
                [profile],
                base_config(),
                statement.parent,
                False,
                {},
                None,
            )

        self.assertEqual(reports[0]["status"], "failed")
        self.assertIsNone(reports[0]["statement_date"])
        self.assertEqual(reports[0]["statement_date_source_pages"], [])

    def test_real_pdf_bytes_feed_the_source_report(self) -> None:
        fixture = (
            FIXTURE_DIR
            / "import_profiles"
            / "hsbc_one_pdf"
            / "accepted_statement"
            / "input.pdf"
        )
        profile = load_profile("hsbc_one_pdf.json")

        _, _, reports, identity_sources = _import_transactions(
            [fixture],
            [profile],
            base_config(),
            fixture.parent,
            False,
            {},
            None,
            include_identity_sources=True,
        )

        self.assertEqual(len(identity_sources), 1)
        self.assertEqual(reports[0]["status"], "processed")
        self.assertEqual(reports[0]["statement_date"], "2026-01-05")
        self.assertEqual(reports[0]["statement_date_source_pages"], [1])


class ParseStatementResultTest(unittest.TestCase):
    def test_pdf_result_is_json_ready(self) -> None:
        profile = _profile_with_test_statement_date_rule()
        with _fake_pdf([["Statement Date: 05 January 2026"]]) as statement:
            result = parse_statement(
                profile,
                str(profile["id"]),
                statement,
                base_config(),
            )
            preview = preview_profile_input(
                profile,
                str(profile["id"]),
                statement,
                base_config(),
            )

        self.assertEqual(result["statement_date"], "2026-01-05")
        self.assertEqual(result["statement_date_source_pages"], [1])
        self.assertEqual(
            (result["transactions"], result["warnings"]),
            preview,
        )
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_csv_result_is_json_ready_with_null_statement_date(self) -> None:
        fixture = (
            FIXTURE_DIR
            / "import_profiles"
            / "starter_csv"
            / "balances_ignored"
            / "input.csv"
        )
        profile = starter_profile()

        result = parse_statement(
            profile,
            str(profile["id"]),
            fixture,
            base_config(),
        )
        preview = preview_profile_input(
            profile,
            str(profile["id"]),
            fixture,
            base_config(),
        )

        self.assertIsNone(result["statement_date"])
        self.assertEqual(result["statement_date_source_pages"], [])
        self.assertEqual(
            (result["transactions"], result["warnings"]),
            preview,
        )
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_incomplete_statement_date_settings_are_rejected(self) -> None:
        cases = {
            "missing_formats": {
                "regex": r"^Statement Date: (?P<date>.+)$",
            },
            "missing_regex": {
                "date_formats": ["%d %B %Y"],
            },
            "format_without_year": {
                "regex": r"^Statement Date: (?P<date>.+)$",
                "date_formats": ["%d %B"],
            },
        }
        for name, settings in cases.items():
            with self.subTest(case=name):
                profile = load_profile("mox_bank_pdf.json")
                profile["pdf"]["statement_date"] = settings
                with _fake_pdf([["Statement Date: 05 January 2026"]]) as statement:
                    with self.assertRaises(ValueError):
                        parse_statement(
                            profile,
                            str(profile["id"]),
                            statement,
                            base_config(),
                        )


def _profile_with_test_statement_date_rule(
    *, date_formats: list[str] | None = None
) -> dict:
    profile = load_profile("mox_bank_pdf.json")
    profile["pdf"].pop("mox_statement", None)
    profile["pdf"]["has_header"] = False
    profile["pdf"]["statement_date"] = {
        "regex": r"^Statement Date: (?P<date>.+)$",
        "date_formats": date_formats or ["%d %B %Y"],
    }
    return profile


def _fake_pdf_source_report(
    profile: dict,
    page_lines: list[list[str]],
    *,
    filename: str = "statement.pdf",
) -> dict:
    profile = json.loads(json.dumps(profile))
    profile.get("pdf", {}).pop("mox_statement", None)
    with _fake_pdf(page_lines, filename=filename) as statement:
        _, _, reports = _import_transactions(
            [statement],
            [profile],
            base_config(),
            statement.parent,
            False,
            {},
            None,
        )
    if len(reports) != 1:
        raise AssertionError(f"Expected one source report, got {reports!r}")
    if reports[0].get("status") != "processed":
        raise AssertionError(f"Expected a processed source report, got {reports[0]!r}")
    return reports[0]


class _fake_pdf:
    def __init__(self, page_lines: list[list[str]], filename: str = "statement.pdf"):
        self.page_lines = page_lines
        self.filename = filename
        self._temporary_directory = None
        self._module_patch = None

    def __enter__(self) -> Path:
        class Page:
            def __init__(self, lines: list[str]):
                self.lines = lines

            def extract_words(self, **kwargs):
                return [
                    {"text": line, "x0": 20, "top": line_number * 20}
                    for line_number, line in enumerate(self.lines, start=1)
                ]

            def extract_tables(self):
                return [[["SYNTHETIC NON-TRANSACTION"]]]

            def extract_table(self):
                return self.extract_tables()[0]

        class Pdf:
            def __init__(self, page_lines: list[list[str]]):
                self.pages = [Page(lines) for lines in page_lines]

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        statement = root / self.filename
        statement.write_bytes(b"%PDF-1.4 synthetic")
        fake_pdfplumber = types.SimpleNamespace(open=lambda path: Pdf(self.page_lines))
        self._module_patch = patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber})
        self._module_patch.start()
        return statement

    def __exit__(self, exc_type, exc, tb):
        if self._module_patch is not None:
            self._module_patch.stop()
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
        return False


if __name__ == "__main__":
    unittest.main()
