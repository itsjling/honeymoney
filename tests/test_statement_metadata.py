from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from honeymoney import cli
from honeymoney.statement_metadata import (
    StatementMetadataError,
    extract_statement_metadata,
    statement_metadata_document,
)
from tests.fixtures.import_profiles.generate_pdf_byte_goldens import (
    _pdf_document,
    _text_command,
)


class StatementMetadataExtractionTest(unittest.TestCase):
    def test_supported_header_layouts(self) -> None:
        cases = {
            "hsbc_premier": (
                "HSBC Premier Page 1 of 7\nSynthetic address\n"
                "Other address\nAccount address 10 September 2026",
                ("2026-09-10", None, None),
            ),
            "hsbc_card": (
                "Statement date Statement balance\n17 AUG 2026 HKD 1,234.56",
                ("2026-08-17", None, None),
            ),
            "mox_bank": (
                "Mox Bank Statement\nStatement period Statement date\n"
                "17 Jul 2026 - 16 Aug 2026 17 Aug 2026",
                ("2026-08-17", "2026-07-17", "2026-08-16"),
            ),
            "mox_credit": (
                "Mox Credit Card Statement 信用卡月結單\n17 Jul 2026 - 16 Aug 2026",
                (None, "2026-07-17", "2026-08-16"),
            ),
            "hang_seng_card": (
                "ACCOUNT NO. CREDIT LIMIT CLOSING DATE PAYMENT DUE DATE\n"
                "123456789012 100,000.00 19 AUG 26 7 SEP 26",
                ("2026-08-19", None, None),
            ),
            "hang_seng_bank": (
                "Hang Seng Bank\nDate :29 August 2026",
                ("2026-08-29", None, None),
            ),
            "hsbc_savings": (
                "Consolidated statement for savings account\nM Date: 29 August 2026",
                ("2026-08-29", None, None),
            ),
            "hsbc_investment": (
                "PRIVATEPREFIXINVSTM0011SUFFIX\nDate : 10SEP2026",
                ("2026-09-10", None, None),
            ),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(case=name):
                metadata = extract_statement_metadata([text])
                self.assertEqual(
                    (
                        metadata["statement_date"],
                        metadata["period_start"],
                        metadata["period_end"],
                    ),
                    expected,
                )
                self.assertEqual(metadata["source_page"], 1)

    def test_period_end_does_not_become_mox_credit_statement_date(self) -> None:
        metadata = extract_statement_metadata(
            ["信用卡月結單 Credit Card Statement\n17 Jul 2026 - 16 Aug 2026"]
        )

        self.assertIsNone(metadata["statement_date"])
        self.assertEqual(metadata["period_end"], "2026-08-16")

    def test_unlabeled_and_transaction_dates_are_not_metadata(self) -> None:
        metadata = extract_statement_metadata(
            [
                "Payment due date 19 August 2026\n"
                "17 Aug 18 Aug Statement date purchase -100.00\n"
                "29 August 2026"
            ]
        )

        self.assertEqual(
            metadata,
            {
                "statement_date": None,
                "period_start": None,
                "period_end": None,
                "source_page": None,
            },
        )

    def test_conflicting_explicit_dates_are_null(self) -> None:
        metadata = extract_statement_metadata(
            ["Statement date: 17 August 2026\nStatement date: 18 August 2026"]
        )

        self.assertIsNone(metadata["statement_date"])
        self.assertIsNone(metadata["source_page"])

    def test_source_page_requires_common_evidence_for_all_fields(self) -> None:
        metadata = extract_statement_metadata(
            [
                "Mox Bank Statement\nStatement period\n"
                "17 Jul 2026 - 16 Aug 2026\nStatement date",
                "Statement date: 17 August 2026",
            ]
        )

        self.assertEqual(metadata["statement_date"], "2026-08-17")
        self.assertEqual(metadata["period_start"], "2026-07-17")
        self.assertIsNone(metadata["source_page"])

    def test_conflicting_ranges_on_one_page_are_unknown(self) -> None:
        for header in (
            "Mox Credit Card Statement",
            "Mox Bank Statement\nStatement period\nStatement date: 17 August 2026",
        ):
            with self.subTest(header=header):
                metadata = extract_statement_metadata(
                    [header + "\n17 Jul 2026 - 16 Aug 2026\n17 Jul 2026 - 15 Aug 2026"]
                )
                self.assertIsNone(metadata["period_start"])
                self.assertIsNone(metadata["period_end"])

    def test_discarded_period_does_not_hide_statement_date_evidence(self) -> None:
        metadata = extract_statement_metadata(
            [
                "Statement date: 17 August 2026",
                "Mox Credit Card Statement\n17 Jul 2026 - 16 Aug 2026",
                "Mox Credit Card Statement\n17 Jul 2026 - 15 Aug 2026",
            ]
        )
        self.assertEqual(
            metadata,
            {
                "statement_date": "2026-08-17",
                "period_start": None,
                "period_end": None,
                "source_page": 1,
            },
        )


class StatementMetadataCliTest(unittest.TestCase):
    def test_metadata_only_command_handles_an_unsupported_zero_row_pdf(self) -> None:
        pdf_bytes = _synthetic_text_pdf(
            "Consolidated statement for savings account",
            "Date: 29 August 2026",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "private-statement.pdf"
            source.write_bytes(pdf_bytes)
            with (
                patch("honeymoney.cli.parse_statement", side_effect=AssertionError),
                patch("honeymoney.cli.load_workspace", side_effect=AssertionError),
                patch("socket.socket", side_effect=AssertionError),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = cli.main(["statement-metadata", str(source), "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["command"], "statement-metadata")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["artifacts"], {})
        self.assertEqual(
            payload["data"],
            {
                "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                "page_count": 1,
                "engine": {"name": "honeymoney", "version": "0.2.4"},
                "statement_metadata": {
                    "statement_date": "2026-08-29",
                    "period_start": None,
                    "period_end": None,
                    "source_page": 1,
                },
            },
        )
        self.assertNotIn("private-statement", output.getvalue())

    def test_metadata_only_command_rejects_non_pdf_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "private.csv"
            csv_path.write_text("synthetic")
            with self.assertRaises(StatementMetadataError) as unsupported:
                statement_metadata_document(csv_path)
            self.assertEqual(
                unsupported.exception.code, "statement_metadata_unsupported_type"
            )

            pdf_path = root / "statement.pdf"
            pdf_path.write_bytes(_synthetic_text_pdf("Statement summary"))
            link = root / "private-link.pdf"
            link.symlink_to(pdf_path)
            with self.assertRaises(StatementMetadataError) as invalid:
                statement_metadata_document(link)
            self.assertEqual(
                invalid.exception.code, "statement_metadata_invalid_source"
            )


def _synthetic_text_pdf(*lines: str) -> bytes:
    stream = b"\n".join(
        _text_command(36, 48 + index * 20, line, size=10)
        for index, line in enumerate(lines)
    )
    return _pdf_document([stream + b"\n"])
