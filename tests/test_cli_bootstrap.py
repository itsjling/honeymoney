import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import unittest


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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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

    def test_non_interactive_undetected_profile_fails_when_multiple_profiles_exist(self) -> None:
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

    def test_interactive_ambiguous_profile_detection_prompts_for_selection(self) -> None:
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
            config_path.write_text(json.dumps({"profiles": profile_paths}), encoding="utf-8")

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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
                {"filename_patterns": [{"pattern": "ambiguous.csv", "profile": "second"}]},
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
                json.dumps({"filename_patterns": [{"pattern": "mox-*.csv", "profile": "mox"}]}),
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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

            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["source_file"], "single.csv")
            self.assertEqual(row["original_amount"], "-1000.00")
            self.assertEqual(row["original_currency"], "JPY")
            self.assertEqual(row["amount_hkd"], "")
            self.assertIn("missing_exchange_rate", row["flags"])
            self.assertEqual(row["reason"], "Missing exchange rate for JPY")
            self.assertEqual(row["needs_review"], "true")

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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))

            self.assertEqual(row["original_amount"], "-10.00")
            self.assertEqual(row["original_currency"], "USD")
            self.assertEqual(row["posted_amount"], "-78.50")
            self.assertEqual(row["posted_currency"], "HKD")
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            config_path.write_text(json.dumps({"rules": str(rules_path)}), encoding="utf-8")

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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
                    ["duplicate_identity_collision", "duplicate_suspected", "matched_rule:parksnshop-groceries"],
                    ["duplicate_identity_collision", "duplicate_suspected", "matched_rule:parksnshop-groceries"],
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertIn("duplicate_suspected", row["flags"])
                self.assertEqual(row["needs_review"], "true")

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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["category"], "Unknown")
            self.assertEqual(row["needs_review"], "true")
            self.assertIn("ollama_unavailable", row["flags"])
            self.assertIn("Ollama unavailable", row["reason"])
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
        self.pages = [Page(table) for table in data["pages"]]
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            report = json.loads((output_dir / "import_report.json").read_text())

            self.assertEqual(row["account_id"], "hsbc_hk_checking")
            self.assertEqual(row["merchant"], "PARKNSHOP")
            self.assertEqual(row["amount_hkd"], "-120.50")
            self.assertEqual(row["source_file"], "statement.pdf")
            self.assertEqual(row["source_page"], "1")
            self.assertEqual(row["source_row"], "2")
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
        self.pages = [Page(table) for table in data["pages"]]
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
        self.pages = [Page(table) for table in data["pages"]]
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual([row["merchant"] for row in rows], ["Coffee Shop", "Taxi"])
            self.assertEqual([row["transaction_date"] for row in rows], ["2026-05-01", "2026-05-02"])
            self.assertEqual([row["amount_hkd"] for row in rows], ["88.00", "-45.00"])

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
        self.pages = [Page(table) for table in data["pages"]]
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual(rows[0]["merchant"], "Coffee Shop")
            self.assertEqual(rows[0]["original_amount"], "-88.00")
            self.assertEqual(rows[0]["source_page"], "1")
            self.assertEqual(rows[0]["source_row"], "1")
            self.assertEqual(rows[1]["merchant"], "Refund")
            self.assertEqual(rows[1]["original_amount"], "12.00")

    def test_headerless_pdf_row_regex_can_extract_single_cell_transactions(self) -> None:
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
        self.pages = [Page(table) for table in data["pages"]]
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            self.assertEqual([row["merchant"] for row in rows], ["Coffee Shop", "Refund"])
            self.assertEqual([row["transaction_date"] for row in rows], ["2026-04-01", "2026-04-02"])
            self.assertEqual([row["amount_hkd"] for row in rows], ["88.00", "-12.00"])

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
                                    ["Date", "Description", "Debit", "Credit", "Currency"],
                                    ["2026-05-01", "PARKNSHOP", "120.50", "", "HKD"],
                                ],
                                [
                                    ["Date", "Description", "Debit", "Credit", "Currency"],
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
                                    ["Date", "Description", "Debit", "Credit", "Currency"],
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
            with (output_dir / "categorized.csv").open(newline="", encoding="utf-8") as fh:
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
        self.pages = [Page(table) for table in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )
            pdf_path = root / "statement.pdf"
            pdf_path.write_text(json.dumps({"pages": [None]}), encoding="utf-8")
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
