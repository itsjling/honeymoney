import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

EXPECTED_CATEGORIZED_COLUMNS = [
    "transaction_id",
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "merchant",
    "original_description",
    "category",
    "owner",
    "payment_method",
    "confidence",
    "needs_review",
    "reason",
    "flags",
    "notes",
    "source_file",
    "source_page",
    "source_row",
]


class CliBootstrapTest(unittest.TestCase):
    def test_help_command_prints_simple_commands(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", "help"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("honeymoney setup", result.stdout)
        self.assertIn("honeymoney run", result.stdout)
        self.assertIn("honeymoney import", result.stdout)
        self.assertIn("honeymoney help", result.stdout)

    def test_setup_command_creates_starter_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            resolved_root = root.resolve()

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "setup",
                    "--root",
                    str(root),
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Created Honeymoney workspace", result.stdout)
            self.assertTrue((root / "input").is_dir())
            self.assertTrue((root / "output").is_dir())
            self.assertTrue((root / "profiles" / "starter_csv.json").exists())
            self.assertTrue((root / "rules.json").exists())
            self.assertTrue((root / "corrections.csv").exists())
            self.assertTrue((root / "profile_mappings.json").exists())

            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["paths"]["input"], str(resolved_root / "input"))
            self.assertEqual(
                config["paths"]["output"],
                str(resolved_root / "output" / "categorized.csv"),
            )
            self.assertEqual(
                config["profiles"],
                [str(resolved_root / "profiles" / "starter_csv.json")]
                + [
                    str(resolved_root / "profiles" / name)
                    for name in [
                        "hsbc_hk_bank.json",
                        "hsbc_hk_bank_pdf.json",
                        "hsbc_hk_credit_card_pdf.json",
                        "mox_bank_pdf.json",
                        "mox_credit_card.json",
                        "mox_credit_card_pdf.json",
                    ]
                ],
            )
            self.assertTrue((root / "profiles" / "mox_credit_card_pdf.json").exists())
            self.assertEqual(config["rules"], str(resolved_root / "rules.json"))
            self.assertEqual(
                config["corrections"], str(resolved_root / "corrections.csv")
            )

            run_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "run",
                    "--config",
                    str(root / "config.json"),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(run_result.returncode, 0, run_result.stderr)
            self.assertTrue((root / "output" / "categorized.csv").exists())
            self.assertTrue((root / "output" / "review_needed.csv").exists())
            self.assertTrue((root / "output" / "import_report.json").exists())

    def test_setup_command_prompts_for_root_when_not_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "prompted-money"

            result = subprocess.run(
                [sys.executable, "-m", "honeymoney.cli", "setup"],
                cwd=Path(__file__).resolve().parents[1],
                input=f"{root}\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Root folder", result.stdout)
            self.assertTrue((root / "config.json").exists())
            self.assertTrue((root / "input").is_dir())

    def test_run_command_uses_config_in_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            repo_root = Path(__file__).resolve().parents[1]
            setup_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "setup",
                    "--root",
                    str(root),
                ],
                cwd=repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
            (root / "input" / "transactions.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo_root)
            run_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "run",
                    "--no-interactive",
                ],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(run_result.returncode, 0, run_result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["merchant"], "PARKNSHOP")
            self.assertEqual(row["amount_hkd"], "-120.50")

    def test_import_command_prompts_for_pasted_filepath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            repo_root = Path(__file__).resolve().parents[1]
            setup_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "setup",
                    "--root",
                    str(root),
                ],
                cwd=repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
            csv_path = root / "pasted.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo_root)
            import_result = subprocess.run(
                [sys.executable, "-m", "honeymoney.cli", "import"],
                cwd=root,
                env=env,
                input=f'"{csv_path}"\n',
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertIn("Paste a CSV/PDF file or folder path", import_result.stdout)
            self.assertIn(
                "Import complete: 1 successful records, 0 unsuccessful records",
                import_result.stdout,
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["merchant"], "PARKNSHOP")
            report = json.loads((root / "output" / "import_report.json").read_text())
            self.assertEqual(report["successful_record_count"], 1)
            self.assertEqual(report["unsuccessful_record_count"], 0)

    def test_import_command_summarizes_unsuccessful_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            repo_root = Path(__file__).resolve().parents[1]
            setup_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "setup",
                    "--root",
                    str(root),
                ],
                cwd=repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
            pdf_path = root / "statement.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% synthetic placeholder\n")
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["pdf"]["enabled"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")

            env = dict(os.environ)
            env["PYTHONPATH"] = str(repo_root)
            import_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "import",
                    str(pdf_path),
                    "--no-interactive",
                ],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertIn(
                "Import complete: 0 successful records, 1 unsuccessful records",
                import_result.stdout,
            )
            report = json.loads((root / "output" / "import_report.json").read_text())
            self.assertEqual(report["successful_record_count"], 0)
            self.assertEqual(report["unsuccessful_record_count"], 1)

    def test_empty_input_run_writes_output_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
                        "review_confidence_threshold": 0.8,
                        "paths": {
                            "input": str(input_dir),
                            "output": str(output_dir / "categorized.csv"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            categorized_path = output_dir / "categorized.csv"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(categorized_path),
                    "--config",
                    str(config_path),
                    "--strict",
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(categorized_path.exists())
            self.assertTrue((output_dir / "review_needed.csv").exists())
            self.assertTrue((output_dir / "import_report.json").exists())

            with categorized_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.reader(fh)
                self.assertEqual(next(reader), EXPECTED_CATEGORIZED_COLUMNS)
                self.assertEqual(list(reader), [])

            with (output_dir / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                reader = csv.reader(fh)
                review_header = next(reader)
                self.assertIn("transaction_id", review_header)
                self.assertIn("category", review_header)
                self.assertIn("notes", review_header)
                self.assertEqual(list(reader), [])

            report = json.loads((output_dir / "import_report.json").read_text())
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["input_count"], 0)
            self.assertEqual(report["output"]["categorized_csv"], str(categorized_path))
            self.assertEqual(
                report["output"]["review_needed_csv"],
                str(output_dir / "review_needed.csv"),
            )
            self.assertEqual(report["warnings"], [])

    def test_csv_input_is_normalized_with_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            profiles_dir = root / "profiles"
            input_dir.mkdir()
            profiles_dir.mkdir()

            csv_path = input_dir / "transactions.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Debit,Credit,Currency",
                        "2026-05-01,PARKNSHOP,120.50,,HKD",
                        "2026-05-02,SALARY,,20000,HKD",
                        "2026-05-03,USD SUBSCRIPTION,10,,USD",
                    ]
                ),
                encoding="utf-8",
            )

            profile_path = profiles_dir / "hsbc_bank.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "debit": "Debit",
                                "credit": "Credit",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
                        "review_confidence_threshold": 0.8,
                        "profiles": [str(profile_path)],
                        "paths": {
                            "input": str(input_dir),
                            "output": str(output_dir / "categorized.csv"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            categorized_path = output_dir / "categorized.csv"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(categorized_path),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            with categorized_path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["date"], "2026-05-01")
            self.assertEqual(rows[0]["transaction_date"], "2026-05-01")
            self.assertEqual(rows[0]["posting_date"], "")
            self.assertEqual(rows[0]["account_id"], "hsbc_hk_checking")
            self.assertEqual(rows[0]["account"], "HSBC HK Checking")
            self.assertEqual(rows[0]["institution"], "HSBC HK")
            self.assertEqual(rows[0]["country"], "HK")
            self.assertEqual(rows[0]["original_amount"], "-120.50")
            self.assertEqual(rows[0]["original_currency"], "HKD")
            self.assertEqual(rows[0]["posted_amount"], "-120.50")
            self.assertEqual(rows[0]["posted_currency"], "HKD")
            self.assertEqual(rows[0]["amount_hkd"], "-120.50")
            self.assertEqual(rows[0]["merchant"], "PARKNSHOP")
            self.assertEqual(rows[0]["original_description"], "PARKNSHOP")
            self.assertEqual(rows[0]["category"], "Unknown")
            self.assertEqual(rows[0]["owner"], "Household")
            self.assertEqual(rows[0]["payment_method"], "Bank Account")
            self.assertEqual(rows[0]["needs_review"], "true")
            self.assertEqual(rows[0]["source_file"], "transactions.csv")
            self.assertEqual(rows[0]["source_row"], "2")

            self.assertEqual(rows[1]["original_amount"], "20000.00")
            self.assertEqual(rows[1]["amount_hkd"], "20000.00")
            self.assertEqual(rows[2]["original_currency"], "USD")
            self.assertEqual(rows[2]["posted_currency"], "USD")
            self.assertEqual(rows[2]["amount_hkd"], "-78.00")

            report = json.loads((output_dir / "import_report.json").read_text())
            self.assertEqual(report["input_count"], 1)
            self.assertEqual(report["transaction_count"], 3)
            self.assertEqual(report["review_count"], 3)
            self.assertEqual(
                report["transaction_diagnostics"][rows[0]["transaction_id"]]["reason"],
                "No categorization rules have been applied",
            )
            self.assertTrue(
                report["transaction_diagnostics"][rows[0]["transaction_id"]][
                    "needs_review"
                ]
            )

    def test_csv_profile_is_selected_by_required_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            profiles_dir = root / "profiles"
            input_dir.mkdir()
            profiles_dir.mkdir()

            csv_path = input_dir / "mox.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Transaction Date,Details,Amount,CCY",
                        "2026-05-10,Mox Cafe,-88.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            hsbc_profile = profiles_dir / "hsbc.json"
            hsbc_profile.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "detect_headers": ["Date", "Description"],
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            mox_profile = profiles_dir / "mox.json"
            mox_profile.write_text(
                json.dumps(
                    {
                        "account_id": "mox_bank_main",
                        "account": "Mox Main",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "detect_headers": ["Transaction Date", "Details", "CCY"],
                            "columns": {
                                "transaction_date": "Transaction Date",
                                "description": "Details",
                                "amount": "Amount",
                                "original_currency": "CCY",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(hsbc_profile), str(mox_profile)],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["account_id"], "mox_bank_main")
            self.assertEqual(row["institution"], "Mox")
            self.assertEqual(row["merchant"], "Mox Cafe")
            self.assertEqual(report["files"][0]["profile_id"], "mox_bank_main")

    def test_non_interactive_ambiguous_profile_detection_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            input_dir.mkdir()
            profiles_dir.mkdir()
            (input_dir / "ambiguous.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-10,Something,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            profile_paths = []
            for name in ["first", "second"]:
                profile_path = profiles_dir / f"{name}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": name,
                            "account_id": name,
                            "account": name,
                            "institution": name,
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "csv": {
                                "detect_headers": ["Date", "Description"],
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                    "original_currency": "Currency",
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profile_paths.append(str(profile_path))

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"profiles": profile_paths}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Ambiguous profile detection", result.stderr)

    def test_non_interactive_undetected_profile_fails_when_multiple_profiles_exist(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            input_dir.mkdir()
            profiles_dir.mkdir()
            (input_dir / "unknown.csv").write_text(
                "\n".join(
                    [
                        "Unexpected,Headers,Amount",
                        "2026-05-10,Something,-10.00",
                    ]
                ),
                encoding="utf-8",
            )

            profile_paths = []
            for name in ["hsbc", "mox"]:
                profile_path = profiles_dir / f"{name}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": name,
                            "account_id": name,
                            "account": name,
                            "institution": name,
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "csv": {
                                "detect_headers": ["Date", "Description"],
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profile_paths.append(str(profile_path))

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"profiles": profile_paths}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not detect profile", result.stderr)

    def test_interactive_ambiguous_profile_detection_prompts_for_selection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            output_dir = root / "output"
            input_dir.mkdir()
            profiles_dir.mkdir()
            (input_dir / "ambiguous.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-10,Something,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            profile_paths = []
            for name in ["first", "second"]:
                profile_path = profiles_dir / f"{name}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": name,
                            "account_id": name,
                            "account": name,
                            "institution": name,
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "csv": {
                                "detect_headers": ["Date", "Description"],
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                    "original_currency": "Currency",
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profile_paths.append(str(profile_path))

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"profiles": profile_paths}), encoding="utf-8"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                input="2\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Select profile for ambiguous.csv", result.stdout)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["account_id"], "second")

    def test_interactive_profile_selection_can_save_filename_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            output_dir = root / "output"
            input_dir.mkdir()
            profiles_dir.mkdir()
            (input_dir / "ambiguous.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-10,Something,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            profile_paths = []
            for name in ["first", "second"]:
                profile_path = profiles_dir / f"{name}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": name,
                            "account_id": name,
                            "account": name,
                            "institution": name,
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "csv": {
                                "detect_headers": ["Date", "Description"],
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                    "original_currency": "Currency",
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profile_paths.append(str(profile_path))

            mapping_path = root / "profile_mappings.json"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": profile_paths,
                        "profile_mappings": str(mapping_path),
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                input="2\ny\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            mapping = json.loads(mapping_path.read_text())
            self.assertEqual(
                mapping,
                {
                    "filename_patterns": [
                        {"pattern": "ambiguous.csv", "profile": "second"}
                    ]
                },
            )

    def test_profile_mapping_file_can_select_profile_by_filename_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            output_dir = root / "output"
            input_dir.mkdir()
            profiles_dir.mkdir()
            (input_dir / "mox-may.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-10,Something,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )

            profile_paths = []
            for name in ["hsbc", "mox"]:
                profile_path = profiles_dir / f"{name}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": name,
                            "account_id": name,
                            "account": name,
                            "institution": name,
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "csv": {
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                    "original_currency": "Currency",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profile_paths.append(str(profile_path))

            mapping_path = root / "profile_mappings.json"
            mapping_path.write_text(
                json.dumps(
                    {"filename_patterns": [{"pattern": "mox-*.csv", "profile": "mox"}]}
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": profile_paths,
                        "profile_mappings": str(mapping_path),
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["account_id"], "mox")

    def test_csv_file_input_flags_missing_exchange_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "output"
            csv_path = root / "single.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,JPY PURCHASE,-1000,JPY",
                    ]
                ),
                encoding="utf-8",
            )

            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "mox_bank_main",
                        "account": "Mox Main",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["source_file"], "single.csv")
            self.assertEqual(row["original_amount"], "-1000.00")
            self.assertEqual(row["original_currency"], "JPY")
            self.assertEqual(row["amount_hkd"], "")
            self.assertIn("missing_exchange_rate", row["flags"])
            self.assertEqual(row["reason"], "Missing exchange rate for JPY")
            self.assertEqual(row["needs_review"], "true")

    def test_csv_file_input_flags_invalid_amount_for_review(self) -> None:
        for invalid_amount in ["not-a-number", "NaN", "Infinity", "BADCR"]:
            with self.subTest(invalid_amount=invalid_amount):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    output_dir = root / "output"
                    csv_path = root / "single.csv"
                    csv_path.write_text(
                        "\n".join(
                            [
                                "Date,Description,Amount,Currency",
                                f"2026-05-04,BROKEN AMOUNT,{invalid_amount},HKD",
                            ]
                        ),
                        encoding="utf-8",
                    )

                    profile_path = root / "profile.json"
                    profile_path.write_text(
                        json.dumps(
                            {
                                "account_id": "mox_bank_main",
                                "account": "Mox Main",
                                "institution": "Mox",
                                "country": "HK",
                                "account_currency": "HKD",
                                "owner": "Household",
                                "payment_method": "Bank Account",
                                "csv": {
                                    "columns": {
                                        "transaction_date": "Date",
                                        "description": "Description",
                                        "amount": "Amount",
                                        "original_currency": "Currency",
                                    }
                                },
                            }
                        ),
                        encoding="utf-8",
                    )

                    config_path = root / "config.json"
                    config_path.write_text(
                        json.dumps(
                            {
                                "base_currency": "HKD",
                                "exchange_rates": {"HKD": 1.0},
                                "profiles": [str(profile_path)],
                            }
                        ),
                        encoding="utf-8",
                    )

                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "honeymoney.cli",
                            "--input",
                            str(csv_path),
                            "--output",
                            str(output_dir / "categorized.csv"),
                            "--config",
                            str(config_path),
                            "--no-interactive",
                        ],
                        cwd=Path(__file__).resolve().parents[1],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    with (output_dir / "categorized.csv").open(
                        newline="", encoding="utf-8"
                    ) as fh:
                        [row] = list(csv.DictReader(fh))
                    report = json.loads((output_dir / "import_report.json").read_text())

                    self.assertEqual(row["original_amount"], "0.00")
                    self.assertEqual(row["amount_hkd"], "0.00")
                    self.assertIn("invalid_amount", row["flags"])
                    self.assertEqual(row["reason"], "Invalid amount in Amount")
                    self.assertEqual(row["needs_review"], "true")
                    self.assertIn(
                        "invalid_amount",
                        report["transaction_flags"][row["transaction_id"]],
                    )
                    self.assertEqual(
                        report["transaction_diagnostics"][row["transaction_id"]][
                            "reason"
                        ],
                        "Invalid amount in Amount",
                    )

    def test_csv_profile_can_use_merchant_and_credit_debit_indicator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "mox.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Transaction date,Post date,Description,Billing amount,Billing currency,Merchant name,Credit / Debit",
                        "2026-05-01,2026-05-02,CARD PURCHASE,88.00,HKD,Mox Cafe,Debit",
                        "2026-05-03,2026-05-04,REFUND,12.00,HKD,Mox Cafe,Credit",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "mox_credit_card",
                        "account": "Mox Credit Card",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Credit Card",
                        "csv": {
                            "columns": {
                                "transaction_date": "Transaction date",
                                "posting_date": "Post date",
                                "description": "Description",
                                "merchant": "Merchant name",
                                "amount": "Billing amount",
                                "original_currency": "Billing currency",
                                "credit_debit": "Credit / Debit",
                            },
                            "debit_values": ["Debit"],
                            "credit_values": ["Credit"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(rows[0]["merchant"], "Mox Cafe")
            self.assertEqual(rows[0]["original_description"], "CARD PURCHASE")
            self.assertEqual(rows[0]["original_amount"], "-88.00")
            self.assertEqual(rows[0]["payment_method"], "Credit Card")
            self.assertEqual(rows[1]["merchant"], "Mox Cafe")
            self.assertEqual(rows[1]["original_description"], "REFUND")
            self.assertEqual(rows[1]["original_amount"], "12.00")

    def test_foreign_transaction_uses_posted_hkd_for_amount_hkd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "card.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Original Amount,Original Currency,Posted Amount,Posted Currency",
                        "2026-05-01,US MERCHANT,-10.00,USD,-78.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_credit_card",
                        "account": "HSBC HK Credit Card",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Credit Card",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Original Amount",
                                "original_currency": "Original Currency",
                                "posted_amount": "Posted Amount",
                                "posted_currency": "Posted Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["original_amount"], "-10.00")
            self.assertEqual(row["original_currency"], "USD")
            self.assertEqual(row["posted_amount"], "-78.50")
            self.assertEqual(row["posted_currency"], "HKD")
            self.assertEqual(row["amount_hkd"], "-78.50")

    def test_posted_amount_uses_credit_debit_indicator_for_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "card.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Original Amount,Original Currency,Posted Amount,Posted Currency,Credit / Debit",
                        "2026-05-01,US MERCHANT,10.00,USD,78.50,HKD,Debit",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "mox_credit_card",
                        "account": "Mox Credit Card",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Credit Card",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Original Amount",
                                "original_currency": "Original Currency",
                                "posted_amount": "Posted Amount",
                                "posted_currency": "Posted Currency",
                                "credit_debit": "Credit / Debit",
                            },
                            "debit_values": ["Debit"],
                            "credit_values": ["Credit"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["original_amount"], "-10.00")
            self.assertEqual(row["posted_amount"], "-78.50")
            self.assertEqual(row["amount_hkd"], "-78.50")

    def test_profile_date_formats_normalize_dates_to_iso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Txn Date,Post Date,Description,Amount,Currency",
                        "01/05/2026,03/05/2026,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "date_formats": ["%d/%m/%Y"],
                        "csv": {
                            "columns": {
                                "transaction_date": "Txn Date",
                                "posting_date": "Post Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["transaction_date"], "2026-05-01")
            self.assertEqual(row["posting_date"], "2026-05-03")
            self.assertEqual(row["date"], "2026-05-01")

    def test_profile_statement_year_normalizes_month_day_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Txn Date,Description,Amount,Currency",
                        "05/01,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "date_formats": ["%m/%d"],
                        "statement_year": 2026,
                        "csv": {
                            "columns": {
                                "transaction_date": "Txn Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["transaction_date"], "2026-05-01")
            self.assertEqual(row["date"], "2026-05-01")

    def test_transaction_id_is_stable_when_source_filename_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                    }
                ),
                encoding="utf-8",
            )

            first_id = self._run_single_csv_and_get_transaction_id(
                root, "may-statement.csv", config_path
            )
            second_id = self._run_single_csv_and_get_transaction_id(
                root, "renamed-statement.csv", config_path
            )

            self.assertNotEqual(first_id, "")
            self.assertEqual(first_id, second_id)

    def test_duplicate_identity_collisions_get_distinct_ids_and_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,PARKNSHOP,-120.50,HKD",
                        "2026-05-04,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["transaction_id"], rows[1]["transaction_id"])
            self.assertIn("duplicate_identity_collision", rows[0]["flags"])
            self.assertIn("duplicate_identity_collision", rows[1]["flags"])

    def test_json_rules_categorize_matching_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,PARKNSHOP HONG KONG,-120.50,HKD",
                        "2026-05-05,MYSTERY MERCHANT,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rules": [
                            {
                                "id": "parksnshop-groceries",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["PARKNSHOP"],
                                "fields": ["merchant", "original_description"],
                                "category": "Groceries",
                                "owner": "Household",
                                "payment_method": "Bank Account",
                                "confidence": 0.98,
                                "notes": "Hong Kong supermarket",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "review_confidence_threshold": 0.8,
                        "profiles": [str(profile_path)],
                        "rules": str(rules_path),
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(rows[0]["category"], "Groceries")
            self.assertEqual(rows[0]["confidence"], "0.98")
            self.assertEqual(rows[0]["needs_review"], "false")
            self.assertIn("matched_rule:parksnshop-groceries", rows[0]["flags"])
            self.assertEqual(rows[0]["notes"], "Hong Kong supermarket")
            self.assertEqual(rows[1]["category"], "Unknown")
            self.assertEqual(rows[1]["needs_review"], "true")

    def test_rule_owner_override_wins_over_account_owner_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,WELLCOME,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "justin_card",
                        "account": "Justin Card",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Justin",
                        "payment_method": "Credit Card",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "wellcome-household",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["WELLCOME"],
                                "fields": ["merchant"],
                                "category": "Groceries",
                                "owner": "Household",
                                "confidence": 0.98,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "rules": str(rules_path),
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["payment_method"], "Credit Card")
            self.assertEqual(row["owner"], "Household")

    def test_rule_can_override_payment_method_for_octopus_topup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,OCTOPUS TOP UP,-500.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "octopus-topup",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["OCTOPUS"],
                                "fields": ["merchant"],
                                "category": "Octopus",
                                "payment_method": "Octopus",
                                "confidence": 0.95,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "rules": str(rules_path),
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["category"], "Octopus")
            self.assertEqual(row["payment_method"], "Octopus")

    def test_rules_use_priority_field_logic_and_match_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,APPLE,-10.00,HKD",
                        "2026-05-05,TRANSFER TO SAVINGS,-500.00,HKD",
                        "2026-05-06,IRD TAX PAYMENT,-1000.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "apple-keyword-shopping",
                                "enabled": True,
                                "priority": 1,
                                "match_type": "keyword",
                                "patterns": ["APP"],
                                "fields": ["merchant"],
                                "category": "Shopping",
                                "confidence": 0.95,
                            },
                            {
                                "id": "apple-exact-subscription",
                                "enabled": True,
                                "priority": 10,
                                "match_type": "exact",
                                "patterns": ["apple"],
                                "fields": ["merchant"],
                                "category": "Subscriptions",
                                "confidence": 0.91,
                            },
                            {
                                "id": "hsbc-transfer-all-fields",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["HSBC", "TRANSFER"],
                                "fields": ["institution", "original_description"],
                                "field_logic": "all",
                                "category": "Internal Transfer",
                                "confidence": 0.85,
                            },
                            {
                                "id": "ird-regex",
                                "enabled": True,
                                "match_type": "regex",
                                "patterns": ["\\bIRD\\b.*TAX"],
                                "fields": ["original_description"],
                                "category": "Taxes",
                                "confidence": 0.96,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                        "rules": str(rules_path),
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(rows[0]["category"], "Subscriptions")
            self.assertIn("matched_rule:apple-exact-subscription", rows[0]["flags"])
            self.assertEqual(rows[1]["category"], "Internal Transfer")
            self.assertIn("matched_rule:hsbc-transfer-all-fields", rows[1]["flags"])
            self.assertEqual(rows[2]["category"], "Taxes")
            self.assertIn("matched_rule:ird-regex", rows[2]["flags"])

    def test_active_rule_with_unknown_category_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "bad-category",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["ANYTHING"],
                                "fields": ["merchant"],
                                "category": "Review Needed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"rules": str(rules_path)}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "out" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported category", result.stderr)

    def test_profile_with_invalid_owner_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "bad_profile",
                        "account": "Bad Profile",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Review Needed",
                        "payment_method": "Bank Account",
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"profiles": [str(profile_path)]}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported owner in profile", result.stderr)

    def test_profile_with_invalid_payment_method_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "bad_profile",
                        "account": "Bad Profile",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Magic Beans",
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"profiles": [str(profile_path)]}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported payment_method in profile", result.stderr)

    def test_configured_profile_missing_account_id_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "bad_profile",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"profiles": [str(profile_path)]}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Missing required profile fields in profile bad_profile: account_id",
                result.stderr,
            )

    def test_configured_taxonomy_allows_custom_profile_owner_and_rule_category(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "transactions.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-01,PET SHOP,-88.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "family_card",
                        "account": "Family Card",
                        "institution": "Test Bank",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Family",
                        "payment_method": "Credit Card",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "pet-care",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["PET"],
                                "fields": ["merchant"],
                                "category": "Pet Care",
                                "confidence": 0.96,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "categories": ["Unknown", "Other", "Pet Care"],
                        "owners": ["Household", "Family"],
                        "profiles": [str(profile_path)],
                        "rules": str(rules_path),
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["category"], "Pet Care")
            self.assertEqual(row["owner"], "Family")

    def test_disabled_invalid_rule_does_not_fail_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "draft-invalid",
                                "enabled": False,
                                "match_type": "regex",
                                "patterns": ["["],
                                "fields": ["merchant"],
                                "category": "Review Needed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"rules": str(rules_path)}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_active_rule_with_bad_regex_fails_startup_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "bad-regex",
                                "enabled": True,
                                "match_type": "regex",
                                "patterns": ["["],
                                "fields": ["merchant"],
                                "category": "Other",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"rules": str(rules_path)}), encoding="utf-8"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid regex in rule bad-regex", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_manual_corrections_override_rules_and_clear_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,APPLE,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "apple-subscriptions",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["APPLE"],
                                "fields": ["merchant"],
                                "category": "Subscriptions",
                                "owner": "Household",
                                "confidence": 0.95,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            base_config = {
                "base_currency": "HKD",
                "exchange_rates": {"HKD": 1.0},
                "profiles": [str(profile_path)],
                "rules": str(rules_path),
            }
            first_config_path = root / "first-config.json"
            first_config_path.write_text(json.dumps(base_config), encoding="utf-8")
            first_output_dir = root / "first-output"

            first_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(first_output_dir / "categorized.csv"),
                    "--config",
                    str(first_config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            with (first_output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [first_row] = list(csv.DictReader(fh))

            corrections_path = root / "corrections.csv"
            corrections_path.write_text(
                "\n".join(
                    [
                        "transaction_id,category,owner,payment_method,notes",
                        f"{first_row['transaction_id']},Shopping,Justin,Credit Card,One-off hardware purchase",
                    ]
                ),
                encoding="utf-8",
            )
            second_config = dict(base_config)
            second_config["corrections"] = str(corrections_path)
            second_config_path = root / "second-config.json"
            second_config_path.write_text(json.dumps(second_config), encoding="utf-8")
            second_output_dir = root / "second-output"

            second_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(second_output_dir / "categorized.csv"),
                    "--config",
                    str(second_config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            with (second_output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [corrected_row] = list(csv.DictReader(fh))
            with (second_output_dir / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))

            self.assertEqual(corrected_row["category"], "Shopping")
            self.assertEqual(corrected_row["owner"], "Justin")
            self.assertEqual(corrected_row["payment_method"], "Credit Card")
            self.assertEqual(corrected_row["needs_review"], "false")
            self.assertIn("manual_correction", corrected_row["flags"])
            self.assertEqual(corrected_row["notes"], "One-off hardware purchase")
            self.assertEqual(review_rows, [])

    def test_manual_correction_can_explicitly_keep_review_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,MYSTERY,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            base_config = {
                "profiles": [str(profile_path)],
                "exchange_rates": {"HKD": 1.0},
            }
            first_config_path = root / "first-config.json"
            first_config_path.write_text(json.dumps(base_config), encoding="utf-8")
            first_output_dir = root / "first-output"

            first_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(first_output_dir / "categorized.csv"),
                    "--config",
                    str(first_config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            with (first_output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [first_row] = list(csv.DictReader(fh))

            corrections_path = root / "corrections.csv"
            corrections_path.write_text(
                "\n".join(
                    [
                        "transaction_id,category,needs_review,reason",
                        f"{first_row['transaction_id']},Other,true,Still need receipt",
                    ]
                ),
                encoding="utf-8",
            )
            second_config = dict(base_config)
            second_config["corrections"] = str(corrections_path)
            second_config_path = root / "second-config.json"
            second_config_path.write_text(json.dumps(second_config), encoding="utf-8")
            second_output_dir = root / "second-output"

            second_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(second_output_dir / "categorized.csv"),
                    "--config",
                    str(second_config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            with (second_output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [corrected_row] = list(csv.DictReader(fh))
            with (second_output_dir / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))

            self.assertEqual(corrected_row["category"], "Other")
            self.assertEqual(corrected_row["needs_review"], "true")
            self.assertEqual(corrected_row["reason"], "Still need receipt")
            self.assertEqual(len(review_rows), 1)

    def test_invalid_correction_category_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            corrections_path = root / "corrections.csv"
            corrections_path.write_text(
                "\n".join(
                    [
                        "transaction_id,category",
                        "txn_example,Review Needed",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"corrections": str(corrections_path)}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported category in correction", result.stderr)

    def test_invalid_correction_needs_review_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            corrections_path = root / "corrections.csv"
            corrections_path.write_text(
                "\n".join(
                    [
                        "transaction_id,category,needs_review",
                        "txn_example,Other,maybe",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"corrections": str(corrections_path)}),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(root / "output" / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported needs_review in correction", result.stderr)

    def test_invalid_correction_confidence_fails_startup(self) -> None:
        for confidence in ["NaN", "1.5", "-0.1", "not-a-number"]:
            with self.subTest(confidence=confidence):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    input_dir = root / "input"
                    input_dir.mkdir()
                    corrections_path = root / "corrections.csv"
                    corrections_path.write_text(
                        "\n".join(
                            [
                                "transaction_id,category,confidence",
                                f"txn_example,Other,{confidence}",
                            ]
                        ),
                        encoding="utf-8",
                    )
                    config_path = root / "config.json"
                    config_path.write_text(
                        json.dumps({"corrections": str(corrections_path)}),
                        encoding="utf-8",
                    )

                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "honeymoney.cli",
                            "--input",
                            str(input_dir),
                            "--output",
                            str(root / "output" / "categorized.csv"),
                            "--config",
                            str(config_path),
                            "--no-interactive",
                        ],
                        cwd=Path(__file__).resolve().parents[1],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 2)
                    self.assertIn("Unsupported confidence in correction", result.stderr)

    def test_duplicate_suspicions_keep_rule_matched_rows_under_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            for filename in ["first.csv", "second.csv"]:
                (input_dir / filename).write_text(
                    "\n".join(
                        [
                            "Date,Description,Amount,Currency",
                            "2026-05-04,PARKNSHOP,-120.50,HKD",
                        ]
                    ),
                    encoding="utf-8",
                )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "parksnshop-groceries",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["PARKNSHOP"],
                                "fields": ["merchant"],
                                "category": "Groceries",
                                "confidence": 0.98,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                        "rules": str(rules_path),
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            with (output_dir / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))

            self.assertEqual(len(rows), 2)
            self.assertEqual(len(review_rows), 2)
            for row in rows:
                self.assertEqual(row["category"], "Groceries")
                self.assertEqual(row["needs_review"], "true")
                self.assertIn("duplicate_suspected", row["flags"])

            report = json.loads((output_dir / "import_report.json").read_text())
            self.assertEqual(report["transaction_count"], 2)
            self.assertEqual(report["review_count"], 2)
            self.assertEqual(report["duplicate_count"], 2)
            self.assertEqual(
                sorted(report["transaction_flags"].values()),
                [
                    [
                        "duplicate_identity_collision",
                        "duplicate_suspected",
                        "matched_rule:parksnshop-groceries",
                    ],
                    [
                        "duplicate_identity_collision",
                        "duplicate_suspected",
                        "matched_rule:parksnshop-groceries",
                    ],
                ],
            )
            self.assertEqual(
                [file_report["source_file"] for file_report in report["files"]],
                ["first.csv", "second.csv"],
            )

    def test_duplicate_detection_flags_near_date_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "first.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-01,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            (input_dir / "second.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-02,PARKNSHOP,-120.50,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertIn("duplicate_suspected", row["flags"])
                self.assertEqual(row["needs_review"], "true")

    def test_duplicate_detection_flags_cross_account_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            input_dir.mkdir()
            profiles_dir.mkdir()
            for filename in ["bank.csv", "card.csv"]:
                (input_dir / filename).write_text(
                    "\n".join(
                        [
                            "Date,Description,Amount,Currency",
                            "2026-05-01,PARKNSHOP,-120.50,HKD",
                        ]
                    ),
                    encoding="utf-8",
                )
            profile_paths = []
            for profile_id, account in [
                ("hsbc_hk_checking", "HSBC HK Checking"),
                ("hsbc_hk_card", "HSBC HK Credit Card"),
            ]:
                profile_path = profiles_dir / f"{profile_id}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": profile_id,
                            "account_id": profile_id,
                            "account": account,
                            "institution": "HSBC HK",
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "csv": {
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                    "original_currency": "Currency",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profile_paths.append(str(profile_path))
            mapping_path = root / "profile_mappings.json"
            mapping_path.write_text(
                json.dumps(
                    {
                        "filename_patterns": [
                            {"pattern": "bank.csv", "profile": "hsbc_hk_checking"},
                            {"pattern": "card.csv", "profile": "hsbc_hk_card"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": profile_paths,
                        "profile_mappings": str(mapping_path),
                        "exchange_rates": {"HKD": 1.0},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(
                {row["account_id"] for row in rows},
                {"hsbc_hk_checking", "hsbc_hk_card"},
            )
            self.assertNotEqual(rows[0]["transaction_id"], rows[1]["transaction_id"])
            for row in rows:
                self.assertIn("duplicate_suspected", row["flags"])
                self.assertNotIn("duplicate_identity_collision", row["flags"])

    def test_unavailable_ollama_does_not_fail_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,MYSTERY MERCHANT,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                        "ollama": {
                            "enabled": True,
                            "url": "http://127.0.0.1:9/api/generate",
                            "model": "qwen2.5:7b-instruct",
                            "batch_size": 20,
                        },
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["category"], "Unknown")
            self.assertEqual(row["needs_review"], "true")
            self.assertIn("ollama_unavailable", row["flags"])
            self.assertIn("Ollama unavailable", row["reason"])
            self.assertIn("Warning: Ollama unavailable", result.stderr)
            self.assertEqual(report["ollama"]["status"], "unavailable")
            self.assertGreaterEqual(len(report["warnings"]), 1)

    def test_ollama_response_can_categorize_unresolved_rows(self) -> None:
        captured_requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                captured_requests.append(json.loads(self.rfile.read(length)))
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": json.loads(captured_requests[0]["prompt"])[
                                    "transactions"
                                ][0]["id"],
                                "category": "Dining",
                                "owner": "Household",
                                "confidence": 0.86,
                                "reason": "Restaurant-like merchant",
                            }
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
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,CAFE GOOD FOOD,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_currency": "HKD",
                        "exchange_rates": {"HKD": 1.0},
                        "profiles": [str(profile_path)],
                        "ollama": {
                            "enabled": True,
                            "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                            "model": "qwen2.5:7b-instruct",
                        },
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["category"], "Dining")
            self.assertEqual(row["owner"], "Household")
            self.assertEqual(row["confidence"], "0.86")
            self.assertEqual(row["needs_review"], "false")
            self.assertIn("ollama_categorized", row["flags"])
            self.assertEqual(row["reason"], "Restaurant-like merchant")
            self.assertEqual(report["ollama"]["status"], "success")
            prompt = json.loads(captured_requests[0]["prompt"])
            self.assertNotIn("source_file", prompt["transactions"][0])

    def test_invalid_ollama_response_marks_transaction_for_review(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": "not-the-transaction-id",
                                "category": "Review Needed",
                                "owner": "Household",
                                "confidence": 1.5,
                                "reason": "Bad response",
                            }
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
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-04,MYSTERY,-10.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "ollama": {
                            "enabled": True,
                            "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                            "model": "qwen2.5:7b-instruct",
                        },
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(csv_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["category"], "Unknown")
            self.assertEqual(row["needs_review"], "true")
            self.assertIn("ollama_invalid_response", row["flags"])
            self.assertIn("Ollama returned invalid categorization", row["reason"])
            self.assertEqual(report["ollama"]["status"], "invalid_response")

    def test_pdf_without_parser_dependency_is_reported_without_fake_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                "raise ImportError('pdfplumber intentionally unavailable')\n",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% synthetic placeholder\n")
            output_dir = root / "output"
            env = dict(os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(rows, [])
            self.assertEqual(report["status"], "partial_success")
            self.assertEqual(report["files"][0]["source_file"], "statement.pdf")
            self.assertEqual(report["files"][0]["status"], "failed")
            self.assertIn("PDF parsing requires pdfplumber", report["warnings"][0])

    def test_pdf_import_can_be_disabled_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "statement.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% synthetic placeholder\n")
            output_dir = root / "output"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "pdf": {"enabled": False},
                        "paths": {
                            "input": str(pdf_path),
                            "output": str(output_dir / "categorized.csv"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(rows, [])
            self.assertEqual(report["status"], "partial_success")
            self.assertEqual(report["files"][0]["source_file"], "statement.pdf")
            self.assertEqual(report["files"][0]["status"], "skipped")
            self.assertIn("PDF parsing disabled", report["warnings"][0])

    def test_text_pdf_table_can_be_imported_with_pdfplumber(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [
            Page(page.get("table") if isinstance(page, dict) else page)
            for page in data["pages"]
        ]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                ["Date", "Description", "Debit", "Credit", "Currency"],
                                ["2026-05-01", "PARKNSHOP", "120.50", "", "HKD"],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank_pdf",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "pdf": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "debit": "Debit",
                                "credit": "Credit",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["account_id"], "hsbc_hk_checking")
            self.assertEqual(row["merchant"], "PARKNSHOP")
            self.assertEqual(row["amount_hkd"], "-120.50")
            self.assertEqual(row["source_file"], "statement.pdf")
            self.assertEqual(row["source_page"], "1")
            self.assertEqual(row["source_row"], "2")
            self.assertEqual(row["notes"], "Imported from PDF")
            self.assertEqual(report["files"][0]["status"], "processed")
            self.assertEqual(report["files"][0]["parser"], "pdfplumber")

    def test_pdf_profile_can_be_selected_by_filename_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [
            Page(page.get("table") if isinstance(page, dict) else page)
            for page in data["pages"]
        ]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "mox-Apr2026.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                ["Date", "Description", "Amount", "Currency"],
                                ["2026-04-01", "Coffee Shop", "88.00", "HKD"],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profiles = []
            for profile_id, account_id, account in [
                ("hsbc_hk_bank_pdf", "hsbc_hk_checking", "HSBC HK Checking"),
                ("mox_bank_pdf", "mox_bank_main", "Mox Bank Main"),
            ]:
                profile_path = root / f"{profile_id}.json"
                profile_path.write_text(
                    json.dumps(
                        {
                            "id": profile_id,
                            "account_id": account_id,
                            "account": account,
                            "institution": account.split()[0],
                            "country": "HK",
                            "account_currency": "HKD",
                            "owner": "Household",
                            "payment_method": "Bank Account",
                            "pdf": {
                                "columns": {
                                    "transaction_date": "Date",
                                    "description": "Description",
                                    "amount": "Amount",
                                    "original_currency": "Currency",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                profiles.append(str(profile_path))
            mapping_path = root / "profile_mappings.json"
            mapping_path.write_text(
                json.dumps(
                    {
                        "filename_patterns": [
                            {"pattern": "mox-*.pdf", "profile": "mox_bank_pdf"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": profiles,
                        "profile_mappings": str(mapping_path),
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["account_id"], "mox_bank_main")
            self.assertEqual(report["files"][0]["profile_id"], "mox_bank_pdf")

    def test_pdf_multiline_table_row_can_expand_into_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [
            Page(page.get("table") if isinstance(page, dict) else page)
            for page in data["pages"]
        ]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                ["Post date", "Trans date", "Description", "Amount"],
                                [
                                    "02MAY\n03MAY",
                                    "01MAY\n02MAY",
                                    "Coffee\x00 Shop\nTaxi\nwrapped note",
                                    "88.00\n45.00CR",
                                ],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_credit_card_pdf",
                        "account_id": "hsbc_hk_credit_card",
                        "account": "HSBC HK Credit Card",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Credit Card",
                        "date_formats": ["%d%b"],
                        "statement_year": 2026,
                        "pdf": {
                            "amount_default_sign": "expense",
                            "split_multiline_rows": True,
                            "split_multiline_row_count_columns": [
                                "Post date",
                                "Trans date",
                                "Amount",
                            ],
                            "columns": {
                                "posting_date": "Post date",
                                "transaction_date": "Trans date",
                                "description": "Description",
                                "amount": "Amount",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual([row["merchant"] for row in rows], ["Coffee Shop", "Taxi"])
            self.assertEqual(
                [row["transaction_date"] for row in rows], ["2026-05-01", "2026-05-02"]
            )
            self.assertEqual([row["amount_hkd"] for row in rows], ["-88.00", "45.00"])

    def test_headerless_pdf_table_can_use_column_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [
            Page(page.get("table") if isinstance(page, dict) else page)
            for page in data["pages"]
        ]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                ["2026-04-01", "Coffee Shop", "88.00", "Debit"],
                                ["2026-04-02", "Refund", "12.00", "Credit"],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "mox_bank_pdf",
                        "account_id": "mox_bank_main",
                        "account": "Mox Main",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "pdf": {
                            "has_header": False,
                            "columns": {
                                "transaction_date": 0,
                                "description": 1,
                                "amount": 2,
                                "credit_debit": 3,
                            },
                            "debit_values": ["Debit"],
                            "credit_values": ["Credit"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(rows[0]["merchant"], "Coffee Shop")
            self.assertEqual(rows[0]["original_amount"], "-88.00")
            self.assertEqual(rows[0]["source_page"], "1")
            self.assertEqual(rows[0]["source_row"], "1")
            self.assertEqual(rows[1]["merchant"], "Refund")
            self.assertEqual(rows[1]["original_amount"], "12.00")

    def test_headerless_pdf_row_regex_can_extract_single_cell_transactions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [
            Page(page.get("table") if isinstance(page, dict) else page)
            for page in data["pages"]
        ]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                ["Activity Settlement Corresponding amount"],
                                ["01 Apr 02 Apr Coffee Shop 88.00"],
                                ["not a transaction row"],
                                ["02 Apr 03 Apr Refund -12.00"],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "mox_bank_pdf",
                        "account_id": "mox_bank_main",
                        "account": "Mox Main",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "date_formats": ["%d %b"],
                        "statement_year": 2026,
                        "pdf": {
                            "has_header": False,
                            "row_regex": (
                                r"^(?P<transaction_date>\d{1,2} [A-Za-z]{3})\s+"
                                r"(?P<posting_date>\d{1,2} [A-Za-z]{3})\s+"
                                r"(?P<description>.*?)\s+"
                                r"(?P<amount>-?\d[\d,]*\.\d{2})$"
                            ),
                            "columns": {
                                "transaction_date": "transaction_date",
                                "posting_date": "posting_date",
                                "description": "description",
                                "amount": "amount",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(
                [row["merchant"] for row in rows], ["Coffee Shop", "Refund"]
            )
            self.assertEqual(
                [row["transaction_date"] for row in rows], ["2026-04-01", "2026-04-02"]
            )
            self.assertEqual([row["amount_hkd"] for row in rows], ["88.00", "-12.00"])

    def test_pdf_row_regex_can_join_capture_groups_without_font_warning_noise(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json
import logging


class Page:
    def __init__(self, table):
        self._table = table

    def extract_tables(self):
        return [self._table]


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        logging.getLogger("pdfminer.pdffont").warning(
            "Could not get FontBBox from font descriptor because None cannot be parsed as 4 floats"
        )
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(page) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                [""],
                                [
                                    "Activity date Settlement date Description Amount (HKD)"
                                ],
                                [
                                    "ShopBack IDFC Coffee Hong "
                                    "22 May 22 May -99.90 Kong HKG"
                                ],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "mox_credit_card_pdf",
                        "account_id": "mox_credit_card",
                        "account": "Mox Credit Card",
                        "institution": "Mox",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Credit Card",
                        "date_formats": ["%d %b"],
                        "statement_year": 2026,
                        "pdf": {
                            "has_header": False,
                            "row_regex": (
                                r"^(?:(?P<description_prefix>.*?)\s+)?"
                                r"(?P<transaction_date>\d{1,2} [A-Za-z]{3})\s+"
                                r"(?P<posting_date>\d{1,2} [A-Za-z]{3})\s+"
                                r"(?P<description>.*?)"
                                r"(?P<amount>-?\d[\d,]*\.\d{2})(?!%)"
                                r"(?P<description_suffix>"
                                r"(?:(?!-?\d[\d,]*\.\d{2}(?!%)).)*)$"
                            ),
                            "join_fields": {
                                "description": [
                                    "description_prefix",
                                    "description",
                                    "description_suffix",
                                ]
                            },
                            "columns": {
                                "transaction_date": "transaction_date",
                                "posting_date": "posting_date",
                                "description": "description",
                                "amount": "amount",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Could not get FontBBox", result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["merchant"], "ShopBack IDFC Coffee Hong Kong HKG")
            self.assertEqual(row["transaction_date"], "2026-05-22")
            self.assertEqual(row["amount_hkd"], "-99.90")

    def test_committed_synthetic_pdf_table_fixtures_cover_bank_and_card_shapes(
        self,
    ) -> None:
        fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "pdf_tables"
        profiles_dir = Path(__file__).resolve().parents[1] / "examples" / "profiles"
        cases = [
            (
                "hsbc_hk_bank_pdf.json",
                json.loads(
                    (profiles_dir / "hsbc_hk_bank_pdf.json").read_text(encoding="utf-8")
                ),
                [
                    {
                        "merchant": "PARKNSHOP",
                        "transaction_date": "2026-05-01",
                        "amount_hkd": "-120.50",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "2",
                    },
                    {
                        "merchant": "SALARY",
                        "transaction_date": "2026-05-02",
                        "amount_hkd": "20000.00",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "3",
                    },
                ],
            ),
            (
                "hsbc_hk_credit_card_pdf.json",
                json.loads(
                    (profiles_dir / "hsbc_hk_credit_card_pdf.json").read_text(
                        encoding="utf-8"
                    )
                ),
                [
                    {
                        "merchant": "Coffee Shop",
                        "transaction_date": "2026-05-01",
                        "amount_hkd": "-88.00",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "2.1",
                    },
                    {
                        "merchant": "Taxi",
                        "transaction_date": "2026-05-02",
                        "amount_hkd": "45.00",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "2.2",
                    },
                ],
            ),
            (
                "mox_bank_pdf.json",
                json.loads(
                    (profiles_dir / "mox_bank_pdf.json").read_text(encoding="utf-8")
                ),
                [
                    {
                        "merchant": "Coffee Shop",
                        "transaction_date": "2026-04-01",
                        "amount_hkd": "88.00",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "2",
                    },
                    {
                        "merchant": "Refund",
                        "transaction_date": "2026-04-02",
                        "amount_hkd": "-12.00",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "4",
                    },
                ],
            ),
            (
                "mox_credit_card_pdf.json",
                json.loads(
                    (profiles_dir / "mox_credit_card_pdf.json").read_text(
                        encoding="utf-8"
                    )
                ),
                [
                    {
                        "merchant": "DINING PLACE",
                        "transaction_date": "2026-05-01",
                        "amount_hkd": "-188.00",
                        "original_currency": "HKD",
                        "source_page": "1",
                        "source_row": "2",
                    },
                    {
                        "merchant": "US SHOP",
                        "transaction_date": "2026-05-03",
                        "amount_hkd": "-78.00",
                        "original_currency": "HKD",
                        "source_page": "2",
                        "source_row": "2",
                    },
                ],
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


def is_table(value):
    return bool(value and isinstance(value[0], list) and (not value[0] or not isinstance(value[0][0], list)))


class Page:
    def __init__(self, payload):
        self._payload = payload

    def extract_table(self):
        tables = self.extract_tables()
        return tables[0] if tables else None

    def extract_tables(self):
        if not self._payload:
            return []
        return [self._payload] if is_table(self._payload) else self._payload


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(page) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            for fixture_name, profile, expected_rows in cases:
                with self.subTest(fixture=fixture_name):
                    run_dir = root / fixture_name.replace(".json", "")
                    run_dir.mkdir()
                    pdf_path = run_dir / "statement.pdf"
                    pdf_path.write_text(
                        (fixtures_dir / fixture_name).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    profile_path = run_dir / "profile.json"
                    profile_path.write_text(json.dumps(profile), encoding="utf-8")
                    config_path = run_dir / "config.json"
                    config_path.write_text(
                        json.dumps(
                            {
                                "profiles": [str(profile_path)],
                                "exchange_rates": {"HKD": 1.0, "USD": 7.8},
                                "pdf": {"enabled": True, "parser": "pdfplumber"},
                            }
                        ),
                        encoding="utf-8",
                    )
                    output_dir = run_dir / "output"

                    result = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "honeymoney.cli",
                            "--input",
                            str(pdf_path),
                            "--output",
                            str(output_dir / "categorized.csv"),
                            "--config",
                            str(config_path),
                            "--no-interactive",
                        ],
                        cwd=Path(__file__).resolve().parents[1],
                        env=env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    with (output_dir / "categorized.csv").open(
                        newline="", encoding="utf-8"
                    ) as fh:
                        rows = list(csv.DictReader(fh))

                    self.assertEqual(len(rows), len(expected_rows))
                    for row, expected in zip(rows, expected_rows):
                        for field, value in expected.items():
                            self.assertEqual(row[field], value)
                    self.assertTrue(
                        all(row["notes"] == "Imported from PDF" for row in rows)
                    )

    def test_example_config_includes_all_requested_pdf_profiles(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = json.loads(
            (repo_root / "examples" / "config.json").read_text(encoding="utf-8")
        )
        profile_ids = {
            json.loads((repo_root / profile_path).read_text(encoding="utf-8"))["id"]
            for profile_path in config["profiles"]
            if profile_path.endswith("_pdf.json")
        }

        self.assertEqual(
            profile_ids,
            {
                "hsbc_hk_bank_pdf",
                "hsbc_hk_credit_card_pdf",
                "mox_bank_pdf",
                "mox_credit_card_pdf",
            },
        )

    def test_pdf_import_reads_all_tables_on_a_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, tables):
        self._tables = tables

    def extract_table(self):
        return self._tables[0] if self._tables else None

    def extract_tables(self):
        return self._tables


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(tables) for tables in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                [
                                    [
                                        "Date",
                                        "Description",
                                        "Debit",
                                        "Credit",
                                        "Currency",
                                    ],
                                    ["2026-05-01", "PARKNSHOP", "120.50", "", "HKD"],
                                ],
                                [
                                    [
                                        "Date",
                                        "Description",
                                        "Debit",
                                        "Credit",
                                        "Currency",
                                    ],
                                    ["2026-05-02", "SALARY", "", "20000.00", "HKD"],
                                ],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank_pdf",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "pdf": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "debit": "Debit",
                                "credit": "Credit",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual([row["merchant"] for row in rows], ["PARKNSHOP", "SALARY"])
            self.assertEqual(rows[0]["source_row"], "2")
            self.assertEqual(rows[1]["source_row"], "2")

    def test_pdf_import_skips_tables_missing_required_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, tables):
        self._tables = tables

    def extract_tables(self):
        return self._tables


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(tables) for tables in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            [
                                [["Account", "Balance"], ["Checking", "1000.00"]],
                                [
                                    [
                                        "Date",
                                        "Description",
                                        "Debit",
                                        "Credit",
                                        "Currency",
                                    ],
                                    ["2026-05-01", "PARKNSHOP", "120.50", "", "HKD"],
                                ],
                            ]
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank_pdf",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "pdf": {
                            "required_columns": ["Date", "Description"],
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "debit": "Debit",
                                "credit": "Credit",
                                "original_currency": "Currency",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["merchant"], "PARKNSHOP")
            self.assertIn(
                "Skipped table on statement.pdf page 1 because required columns were missing",
                report["warnings"],
            )

    def test_pdf_page_without_table_reports_parser_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, table):
        self._table = table

    def extract_table(self):
        return self._table


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [
            Page(page.get("table") if isinstance(page, dict) else page)
            for page in data["pages"]
        ]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            (fake_modules / "fitz.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(page.get("text", "")) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __getitem__(self, index):
        return self.pages[index]


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps({"pages": [{"table": None, "text": "debug text"}]}),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank_pdf",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "pdf": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "debit": "Debit",
                                "credit": "Credit",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profiles": [str(profile_path)],
                        "exchange_rates": {"HKD": 1.0},
                        "pdf": {"enabled": True, "parser": "pdfplumber"},
                    }
                ),
                encoding="utf-8",
            )
            output_dir = root / "output"
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{Path(__file__).resolve().parents[1]}"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output_dir / "import_report.json").read_text())
            self.assertEqual(report["status"], "partial_success")
            self.assertEqual(report["files"][0]["status"], "processed")
            self.assertEqual(report["files"][0]["transaction_count"], "0")
            self.assertIn("No table found on statement.pdf page 1", report["warnings"])
            self.assertIn(
                "PyMuPDF text fallback found 10 characters on statement.pdf page 1",
                report["warnings"],
            )

    def test_strict_mode_returns_nonzero_when_import_has_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "statement.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% synthetic placeholder\n")
            output_dir = root / "output"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(pdf_path),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--strict",
                    "--no-interactive",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertTrue((output_dir / "import_report.json").exists())

    def test_mixed_v1_acceptance_path_uses_csv_pdf_rules_corrections_duplicates_and_ollama(
        self,
    ) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request_body = json.loads(self.rfile.read(length))
                prompt = json.loads(request_body["prompt"])
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": transaction["id"],
                                "category": "Dining",
                                "owner": "Household",
                                "confidence": 0.91,
                                "reason": "Local model matched dining-like transaction",
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
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = Path(__file__).resolve().parents[1]
            fixture_path = (
                repo_root
                / "tests"
                / "fixtures"
                / "pdf_tables"
                / "hsbc_hk_bank_pdf.json"
            )
            fake_modules = root / "fake_modules"
            input_dir = root / "input"
            profiles_dir = root / "profiles"
            fake_modules.mkdir()
            input_dir.mkdir()
            profiles_dir.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


def is_table(value):
    return bool(value and isinstance(value[0], list) and (not value[0] or not isinstance(value[0][0], list)))


class Page:
    def __init__(self, payload):
        self._payload = payload

    def extract_table(self):
        tables = self.extract_tables()
        return tables[0] if tables else None

    def extract_tables(self):
        if not self._payload:
            return []
        return [self._payload] if is_table(self._payload) else self._payload


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(page) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            (input_dir / "transactions.csv").write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-01,PARKNSHOP,-120.50,HKD",
                        "2026-05-03,MYSTERY OLLAMA,-88.00,HKD",
                        "2026-05-04,MANUAL SHOP,-55.00,HKD",
                    ]
                ),
                encoding="utf-8",
            )
            (input_dir / "statement.pdf").write_text(
                fixture_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            csv_profile = profiles_dir / "hsbc_hk_bank.json"
            pdf_profile = profiles_dir / "hsbc_hk_bank_pdf.json"
            csv_profile.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "csv": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "amount": "Amount",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            pdf_profile.write_text(
                json.dumps(
                    {
                        "id": "hsbc_hk_bank_pdf",
                        "account_id": "hsbc_hk_checking",
                        "account": "HSBC HK Checking",
                        "institution": "HSBC HK",
                        "country": "HK",
                        "account_currency": "HKD",
                        "owner": "Household",
                        "payment_method": "Bank Account",
                        "pdf": {
                            "columns": {
                                "transaction_date": "Date",
                                "description": "Description",
                                "debit": "Debit",
                                "credit": "Credit",
                                "original_currency": "Currency",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(
                json.dumps(
                    {
                        "rules": [
                            {
                                "id": "parksnshop-groceries",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["PARKNSHOP"],
                                "fields": ["merchant"],
                                "category": "Groceries",
                                "confidence": 0.98,
                            },
                            {
                                "id": "salary-income",
                                "enabled": True,
                                "match_type": "keyword",
                                "patterns": ["SALARY"],
                                "fields": ["merchant"],
                                "category": "Income",
                                "confidence": 0.99,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            mapping_path = root / "profile_mappings.json"
            mapping_path.write_text(
                json.dumps(
                    {
                        "filename_patterns": [
                            {"pattern": "transactions.csv", "profile": "hsbc_hk_bank"},
                            {"pattern": "statement.pdf", "profile": "hsbc_hk_bank_pdf"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config = {
                "profiles": [str(csv_profile), str(pdf_profile)],
                "profile_mappings": str(mapping_path),
                "rules": str(rules_path),
                "exchange_rates": {"HKD": 1.0},
                "pdf": {"enabled": True, "parser": "pdfplumber"},
                "ollama": {
                    "enabled": False,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                    "batch_size": 2,
                },
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            env = dict(**os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{repo_root}"

            first_output_dir = root / "first-output"
            first_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(first_output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=repo_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            with (first_output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                first_rows = list(csv.DictReader(fh))
            manual_id = next(
                row["transaction_id"]
                for row in first_rows
                if row["merchant"] == "MANUAL SHOP"
            )

            corrections_path = root / "corrections.csv"
            corrections_path.write_text(
                "\n".join(
                    [
                        "transaction_id,category,owner,payment_method,confidence,reason,notes",
                        f"{manual_id},Shopping,Justin,Bank Account,1.0,Reviewed manually,Reusable correction",
                    ]
                ),
                encoding="utf-8",
            )
            config["corrections"] = str(corrections_path)
            config["ollama"]["enabled"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")
            final_output_dir = root / "final-output"
            final_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(input_dir),
                    "--output",
                    str(final_output_dir / "categorized.csv"),
                    "--config",
                    str(config_path),
                    "--no-interactive",
                ],
                cwd=repo_root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(final_result.returncode, 0, final_result.stderr)
            with (final_output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = list(csv.DictReader(fh))
            with (final_output_dir / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_rows = list(csv.DictReader(fh))
            report = json.loads((final_output_dir / "import_report.json").read_text())

            self.assertEqual(len(rows), 5)
            self.assertTrue(review_rows)
            self.assertEqual(report["transaction_count"], 5)
            self.assertEqual(report["ollama"]["status"], "success")
            self.assertEqual(
                {file["source_file"] for file in report["files"]},
                {"transactions.csv", "statement.pdf"},
            )
            parksnshop_rows = [row for row in rows if row["merchant"] == "PARKNSHOP"]
            self.assertEqual(len(parksnshop_rows), 2)
            for row in parksnshop_rows:
                self.assertEqual(row["category"], "Groceries")
                self.assertIn("duplicate_suspected", row["flags"])
            mystery = next(row for row in rows if row["merchant"] == "MYSTERY OLLAMA")
            self.assertEqual(mystery["category"], "Dining")
            self.assertIn("ollama_categorized", mystery["flags"])
            manual = next(row for row in rows if row["merchant"] == "MANUAL SHOP")
            self.assertEqual(manual["transaction_id"], manual_id)
            self.assertEqual(manual["category"], "Shopping")
            self.assertEqual(manual["owner"], "Justin")
            self.assertEqual(manual["needs_review"], "false")
            pdf_row = next(row for row in rows if row["source_file"] == "statement.pdf")
            self.assertEqual(pdf_row["notes"], "Imported from PDF")

    def _run_single_csv_and_get_transaction_id(
        self, root: Path, filename: str, config_path: Path
    ) -> str:
        run_dir = root / filename.replace(".csv", "")
        run_dir.mkdir()
        csv_path = run_dir / filename
        csv_path.write_text(
            "\n".join(
                [
                    "Date,Description,Amount,Currency",
                    "2026-05-04,PARKNSHOP,-120.50,HKD",
                ]
            ),
            encoding="utf-8",
        )

        output_dir = run_dir / "output"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "honeymoney.cli",
                "--input",
                str(csv_path),
                "--output",
                str(output_dir / "categorized.csv"),
                "--config",
                str(config_path),
                "--no-interactive",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
            [row] = list(csv.DictReader(fh))
        return row["transaction_id"]


if __name__ == "__main__":
    unittest.main()
