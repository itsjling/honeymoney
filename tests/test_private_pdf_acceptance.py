import csv
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.check_private_pdfs import (
    DEFAULT_ROOT,
    PARSER_COLUMNS,
    AcceptanceCase,
    AcceptanceError,
    _accept_snapshot,
    _add_case,
    _compare_snapshots,
    _ensure_private_root,
    _initialize,
    _load_cases,
    _prepare_case,
    _write_snapshot,
    main,
)


class PrivatePdfAcceptanceTest(unittest.TestCase):
    def test_parser_snapshot_columns_are_a_stable_public_contract(self) -> None:
        self.assertEqual(
            PARSER_COLUMNS,
            [
                "date",
                "transaction_date",
                "posting_date",
                "account_id",
                "account",
                "account_type",
                "institution",
                "country",
                "original_amount",
                "original_currency",
                "posted_amount",
                "posted_currency",
                "amount_hkd",
                "statement_opening_balance",
                "statement_closing_balance",
                "merchant",
                "original_description",
                "source_page",
                "source_row",
            ],
        )

    def test_command_exit_codes_distinguish_success_difference_and_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = StringIO()
            errors = StringIO()
            with (
                patch(
                    "scripts.check_private_pdfs._ensure_private_root",
                    return_value=root,
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                self.assertEqual(main(["--root", str(root), "init"]), 0)
                with patch(
                    "scripts.check_private_pdfs._prepare_command", return_value=1
                ):
                    self.assertEqual(main(["--root", str(root), "check"]), 1)
                self.assertEqual(
                    main(
                        [
                            "--root",
                            str(root),
                            "profiles",
                            "--config",
                            str(root / "missing.json"),
                        ]
                    ),
                    2,
                )

    def test_accept_supports_case_option_and_positional_case_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = root / "expected" / "case.csv"
            for arguments in (["--case", "case"], ["case"]):
                with (
                    self.subTest(arguments=arguments),
                    patch(
                        "scripts.check_private_pdfs._ensure_private_root",
                        return_value=root,
                    ),
                    patch(
                        "scripts.check_private_pdfs._load_config_and_profiles",
                        return_value=({}, {}),
                    ),
                    patch(
                        "scripts.check_private_pdfs._accept_snapshot",
                        return_value=accepted,
                    ) as accept_snapshot,
                    redirect_stdout(StringIO()),
                ):
                    self.assertEqual(
                        main(["--root", str(root), "accept", *arguments]), 0
                    )
                    accept_snapshot.assert_called_once_with(root, "case", {}, {})

    def test_acceptance_root_is_confined_to_private_samples(self) -> None:
        self.assertEqual(_ensure_private_root(DEFAULT_ROOT), DEFAULT_ROOT.resolve())
        with self.assertRaisesRegex(AcceptanceError, "must stay inside"):
            _ensure_private_root(Path("/tmp/not-private-pdf-acceptance"))

    def test_initialize_creates_private_workspace_without_overwriting_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private_samples" / "pdf_acceptance"

            _initialize(root)

            manifest = root / "cases.json"
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8")),
                {"version": 1, "cases": []},
            )
            self.assertTrue((root / "statements").is_dir())
            self.assertTrue((root / "actual").is_dir())
            self.assertTrue((root / "expected").is_dir())

            manifest.write_text('{"version": 1, "cases": ["keep-me"]}\n')
            _initialize(root)
            self.assertIn("keep-me", manifest.read_text(encoding="utf-8"))

    def test_add_case_registers_pdf_without_copying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _initialize(root)
            pdf_path = root / "statements" / "May statement.pdf"
            pdf_path.write_bytes(b"%PDF")

            case_name = _add_case(root, pdf_path, "synthetic_pdf", None)

            self.assertEqual(case_name, "May-statement")
            document = json.loads((root / "cases.json").read_text(encoding="utf-8"))
            self.assertEqual(
                document["cases"],
                [
                    {
                        "name": "May-statement",
                        "pdf": "statements/May statement.pdf",
                        "profile": "synthetic_pdf",
                    }
                ],
            )
            with self.assertRaisesRegex(AcceptanceError, "duplicate case name"):
                _add_case(root, pdf_path, "synthetic_pdf", None)

    def test_load_cases_resolves_private_pdf_and_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "statements").mkdir()
            (root / "statements" / "statement.pdf").write_bytes(b"%PDF")
            (root / "cases.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "name": "statement-2026-05",
                                "pdf": "statements/statement.pdf",
                                "profile": "synthetic_pdf",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            [case] = _load_cases(root)

            self.assertEqual(case.name, "statement-2026-05")
            self.assertEqual(
                case.pdf_path, (root / "statements" / "statement.pdf").resolve()
            )
            self.assertEqual(case.profile_id, "synthetic_pdf")

            (root / "cases.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "name": "escape",
                                "pdf": "../statement.pdf",
                                "profile": "synthetic_pdf",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AcceptanceError, "must stay inside"):
                _load_cases(root)

    def test_snapshot_contains_only_parser_fields_and_diff_hides_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual.csv"
            expected = root / "expected.csv"
            row = {
                "transaction_id": "private-id",
                "transaction_date": "2026-05-01",
                "merchant": "SYNTHETIC SHOP",
                "amount_hkd": "-10.00",
                "category": "Dining",
                "reason": "private reason",
            }
            _write_snapshot(actual, [row])

            with actual.open(newline="", encoding="utf-8-sig") as fh:
                [snapshot] = list(csv.DictReader(fh))
            self.assertEqual(snapshot["merchant"], "SYNTHETIC SHOP")
            self.assertNotIn("transaction_id", snapshot)
            self.assertNotIn("category", snapshot)
            self.assertNotIn("reason", snapshot)

            _write_snapshot(expected, [{**row, "amount_hkd": "-11.00"}])
            differences = _compare_snapshots(expected, actual)
            self.assertEqual(differences, ["row 1: amount_hkd changed"])
            self.assertNotIn("-10.00", differences[0])
            self.assertNotIn("-11.00", differences[0])

    def test_accept_requires_warning_free_prepared_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _initialize(root)
            pdf_path = root / "statements" / "statement.pdf"
            pdf_path.write_bytes(b"%PDF")
            (root / "cases.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cases": [
                            {
                                "name": "case",
                                "pdf": "statements/statement.pdf",
                                "profile": "synthetic_pdf",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            case = AcceptanceCase("case", pdf_path, "synthetic_pdf")
            config = {"base_currency": "HKD"}
            profiles = {"synthetic_pdf": {"id": "synthetic_pdf", "pdf": {}}}
            row = {"merchant": "SYNTHETIC SHOP"}

            with patch(
                "scripts.check_private_pdfs._import_pdf", return_value=([row], ["warn"])
            ):
                _prepare_case(root, case, config, profiles)
                with self.assertRaisesRegex(AcceptanceError, "parser warning"):
                    _accept_snapshot(root, "case", config, profiles)

            with patch("scripts.check_private_pdfs._import_pdf", return_value=([], [])):
                _prepare_case(root, case, config, profiles)
                with self.assertRaisesRegex(AcceptanceError, "no parsed transactions"):
                    _accept_snapshot(root, "case", config, profiles)

            with patch(
                "scripts.check_private_pdfs._import_pdf", return_value=([row], [])
            ):
                actual, _ = _prepare_case(root, case, config, profiles)
            destination = _accept_snapshot(root, "case", config, profiles)
            self.assertEqual(destination, root / "expected" / "case.csv")
            self.assertEqual(
                destination.read_text(encoding="utf-8"), actual.read_text()
            )

            actual.write_text("merchant\nCHANGED\n", encoding="utf-8")
            with self.assertRaisesRegex(AcceptanceError, "changed after preparation"):
                _accept_snapshot(root, "case", config, profiles)

    def test_prepare_uses_real_pdf_import_seam_and_records_safe_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "statements" / "statement.pdf"
            pdf_path.parent.mkdir()
            pdf_path.write_bytes(b"%PDF")
            case = AcceptanceCase("case", pdf_path, "synthetic_pdf")
            rows = [
                {
                    "transaction_id": "not-snapshotted",
                    "transaction_date": "2026-05-01",
                    "merchant": "SYNTHETIC SHOP",
                    "amount_hkd": "-10.00",
                }
            ]

            with patch(
                "scripts.check_private_pdfs._import_pdf",
                return_value=(rows, []),
            ) as importer:
                actual_path, warnings = _prepare_case(
                    root,
                    case,
                    {"base_currency": "HKD"},
                    {"synthetic_pdf": {"id": "synthetic_pdf", "pdf": {}}},
                )

            importer.assert_called_once()
            self.assertEqual(warnings, [])
            self.assertTrue(actual_path.is_file())
            status = json.loads(
                (root / "actual" / "case.status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {
                    field: status[field]
                    for field in ("profile", "row_count", "warning_count")
                },
                {"profile": "synthetic_pdf", "row_count": 1, "warning_count": 0},
            )
            self.assertNotIn("SYNTHETIC SHOP", json.dumps(status))

    def test_prepare_parses_actual_synthetic_pdf_bytes(self) -> None:
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "statements" / "statement.pdf"
            pdf_path.parent.mkdir()
            with fitz.open() as document:
                page = document.new_page()
                for x, text in [
                    (50, "Post date"),
                    (100, "Trans date"),
                    (150, "Description"),
                    (500, "Amount"),
                ]:
                    page.insert_text((x, 72), text)
                for x, text in [
                    (50, "02Jun"),
                    (100, "01Jun"),
                    (150, "SYNTHETIC SHOP"),
                    (500, "10.00"),
                ]:
                    page.insert_text((x, 100), text)
                page.insert_text((50, 128), "Note:")
                document.save(pdf_path)

            profile_path = (
                Path(__file__).resolve().parents[1]
                / "honeymoney"
                / "data"
                / "profiles"
                / "hsbc_hk_credit_card_pdf.json"
            )
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            case = AcceptanceCase("actual-pdf", pdf_path, "hsbc_hk_credit_card_pdf")

            actual_path, warnings = _prepare_case(
                root,
                case,
                {"base_currency": "HKD", "exchange_rates": {"HKD": 1.0}},
                {"hsbc_hk_credit_card_pdf": profile},
            )

            self.assertEqual(warnings, [])
            with actual_path.open(newline="", encoding="utf-8-sig") as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["transaction_date"], "2026-06-01")
            self.assertEqual(row["posting_date"], "2026-06-02")
            self.assertEqual(row["merchant"], "SYNTHETIC SHOP")
            self.assertEqual(row["amount_hkd"], "-10.00")


if __name__ == "__main__":
    unittest.main()
