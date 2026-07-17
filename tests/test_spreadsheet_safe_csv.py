import codecs
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.corrections import (
    CORRECTION_COLUMNS,
    ledger_output_documents,
    load_corrections,
    prepare_corrections_document,
    read_ledger,
)
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.rules import apply_rules
from honeymoney.schema import (
    ALLOWED_CATEGORIES,
    ALLOWED_OWNERS,
    ALLOWED_PAYMENT_METHODS,
    CATEGORIZED_COLUMNS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class SpreadsheetSafeCsvTest(unittest.TestCase):
    def _run_cli(
        self, args: list[str], *, cwd: Path, input_text: str | None = None
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

    def _setup_workspace(self, temporary_root: str) -> Path:
        root = Path(temporary_root) / "money"
        result = self._run_cli(["setup", "--root", str(root), "--json"], cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _write_statement(self, path: Path, description: str) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Date", "Description", "Amount", "Currency"])
            writer.writerow(["2026-06-01", description, "-12.34", "HKD"])

    def _ledger_row(self, transaction_id: str, **values: str) -> dict[str, str]:
        row = {column: "" for column in CATEGORIZED_COLUMNS}
        row.update(
            {
                "transaction_id": transaction_id,
                "date": "2026-06-01",
                "account_type": "bank",
                "amount_hkd": "-12.34",
                "category": "Unknown",
                "flow_type": "unresolved",
                "owner": "Household",
                "confidence": "0.00",
                "needs_review": "true",
                "flags": "uncategorized",
            }
        )
        row.update(values)
        return row

    def test_ledger_and_review_exports_are_reversible_and_keep_amounts_numeric(
        self,
    ) -> None:
        dangerous_values = [
            ("=SUM(A1:A2)", "'=SUM(A1:A2)"),
            ("+FORMULA", "'+FORMULA"),
            ("-FORMULA", "'-FORMULA"),
            ("@FORMULA", "'@FORMULA"),
            ("\tFORMULA", "'\tFORMULA"),
            ("\rFORMULA", "'\rFORMULA"),
            ("  =FORMULA", "'  =FORMULA"),
            ("'=LEGITIMATE TEXT", "''=LEGITIMATE TEXT"),
        ]
        rows = []
        for index, (canonical, _) in enumerate(dangerous_values, start=1):
            row = {column: "" for column in CATEGORIZED_COLUMNS}
            row.update(
                {
                    "transaction_id": f"txn_{index}",
                    "date": f"2026-06-{index:02d}",
                    "original_amount": "-12.34",
                    "posted_amount": "-12.34",
                    "amount_hkd": "-12.34",
                    "account_type": "bank",
                    "merchant": canonical,
                    "original_description": canonical,
                    "category": canonical,
                    "owner": canonical,
                    "payment_method": canonical,
                    "confidence": "0.25",
                    "needs_review": "true",
                    "reason": canonical,
                    "notes": canonical,
                }
            )
            rows.append(row)

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "categorized.csv"
            documents = ledger_output_documents(ledger_path, rows)
            ledger_text = documents[ledger_path]
            ledger_path.write_text(ledger_text, encoding="utf-8", newline="")

            ledger_bytes = ledger_path.read_bytes()
            self.assertTrue(ledger_bytes.startswith(codecs.BOM_UTF8))
            first_data_line = ledger_bytes.splitlines()[1]
            self.assertEqual(first_data_line.count(b",-12.34,"), 3)
            self.assertIn(b",0.25,true,", first_data_line)
            self.assertNotIn(b'"-12.34"', first_data_line)
            self.assertNotIn(b'"0.25"', first_data_line)

            with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
                exported_rows = list(csv.DictReader(handle))
            with (ledger_path.parent / "review_needed.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                handle.write(documents[ledger_path.parent / "review_needed.csv"])
            with (ledger_path.parent / "review_needed.csv").open(
                newline="", encoding="utf-8-sig"
            ) as handle:
                review_rows = list(csv.DictReader(handle))

            for exported, review, (_, expected) in zip(
                exported_rows, review_rows, dangerous_values
            ):
                for field in (
                    "merchant",
                    "original_description",
                    "category",
                    "owner",
                    "payment_method",
                    "reason",
                    "notes",
                ):
                    self.assertEqual(exported[field], expected)
                self.assertEqual(review["merchant"], expected)
                self.assertEqual(review["suggested_category"], expected)
                self.assertEqual(exported["original_amount"], "-12.34")
                self.assertEqual(exported["posted_amount"], "-12.34")
                self.assertEqual(exported["amount_hkd"], "-12.34")
                self.assertEqual(exported["confidence"], "0.25")

            canonical_rows = read_ledger(ledger_path)
            self.assertEqual(
                [row["merchant"] for row in canonical_rows],
                [canonical for canonical, _ in dangerous_values],
            )
            self.assertEqual(
                ledger_output_documents(ledger_path, canonical_rows)[ledger_path],
                ledger_text,
            )

    def test_legacy_quote_all_artifacts_preserve_literal_apostrophes_until_rewritten(
        self,
    ) -> None:
        legacy_row = self._ledger_row(
            "txn_legacy",
            merchant="'=LEGACY MERCHANT",
            original_description="''LEGACY DESCRIPTION",
            notes="'=LEGACY NOTE",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "categorized.csv"
            with ledger_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=CATEGORIZED_COLUMNS,
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerow(legacy_row)
            ledger_before = ledger_path.read_bytes()

            [loaded] = read_ledger(ledger_path)

            self.assertEqual(ledger_path.read_bytes(), ledger_before)
            self.assertEqual(loaded["merchant"], "'=LEGACY MERCHANT")
            self.assertEqual(loaded["original_description"], "''LEGACY DESCRIPTION")
            self.assertEqual(loaded["notes"], "'=LEGACY NOTE")

            migrated = ledger_output_documents(ledger_path, [loaded])[ledger_path]
            self.assertTrue(migrated.startswith("\ufefftransaction_id,date"))
            ledger_path.write_text(migrated, encoding="utf-8", newline="")
            [reloaded] = read_ledger(ledger_path)
            self.assertEqual(reloaded["merchant"], "'=LEGACY MERCHANT")
            self.assertEqual(reloaded["original_description"], "''LEGACY DESCRIPTION")

            corrections_path = root / "corrections.csv"
            with corrections_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=CORRECTION_COLUMNS,
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "transaction_id": "txn_legacy",
                        "category": "'=Legacy Category",
                        "notes": "''Legacy note",
                    }
                )
            corrections_before = corrections_path.read_bytes()
            config = {
                "corrections": str(corrections_path),
                "categories": ["'=Legacy Category"],
            }

            loaded_corrections = load_corrections(config)

            self.assertEqual(corrections_path.read_bytes(), corrections_before)
            self.assertEqual(
                loaded_corrections["txn_legacy"]["category"], "'=Legacy Category"
            )
            self.assertEqual(loaded_corrections["txn_legacy"]["notes"], "''Legacy note")
            _, migrated_corrections, _ = prepare_corrections_document(config)
            self.assertTrue(migrated_corrections.startswith("\ufefftransaction_id"))
            corrections_path.write_text(
                migrated_corrections, encoding="utf-8", newline=""
            )
            self.assertEqual(load_corrections(config), loaded_corrections)

    def test_decoded_formula_text_reaches_rule_matching(self) -> None:
        row = self._ledger_row(
            "txn_rule",
            merchant="=PAYROLL",
            original_description="=PAYROLL",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "categorized.csv"
            ledger_path.write_text(
                ledger_output_documents(ledger_path, [row])[ledger_path],
                encoding="utf-8",
                newline="",
            )
            rows = read_ledger(ledger_path)

            apply_rules(
                rows,
                [
                    {
                        "id": "formula-payroll",
                        "match_type": "exact",
                        "patterns": ["=PAYROLL"],
                        "fields": ["original_description"],
                        "category": "Income",
                        "flow_type": "income",
                        "confidence": 1.0,
                    }
                ],
                {},
            )

            self.assertEqual(rows[0]["original_description"], "=PAYROLL")
            self.assertEqual(rows[0]["category"], "Income")
            self.assertEqual(rows[0]["flow_type"], "income")

    def test_reconciliation_rewrite_preserves_decoded_formula_text(self) -> None:
        rows = [
            self._ledger_row(
                "txn_bank",
                account_id="bank_main",
                account_type="bank",
                amount_hkd="-500.00",
                category="Other",
                merchant="@BANK TRANSFER",
                original_description="@BANK TRANSFER",
            ),
            self._ledger_row(
                "txn_card",
                account_id="card_main",
                account_type="credit_card",
                amount_hkd="500.00",
                category="Other",
                merchant="+CARD PAYMENT",
                original_description="+CARD PAYMENT",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "categorized.csv"
            ledger_path.write_text(
                ledger_output_documents(ledger_path, rows)[ledger_path],
                encoding="utf-8",
                newline="",
            )
            canonical_rows = read_ledger(ledger_path)

            summary = reconcile_ledger(
                canonical_rows, {"reconciliation": {"date_window_days": 3}}
            )
            rewritten = ledger_output_documents(ledger_path, canonical_rows)[
                ledger_path
            ]
            ledger_path.write_text(rewritten, encoding="utf-8", newline="")
            reloaded = {row["transaction_id"]: row for row in read_ledger(ledger_path)}

            self.assertEqual(summary["paired_groups"], 1)
            self.assertEqual(reloaded["txn_bank"]["merchant"], "@BANK TRANSFER")
            self.assertEqual(reloaded["txn_card"]["merchant"], "+CARD PAYMENT")
            self.assertEqual(
                {row["flow_type"] for row in reloaded.values()},
                {"credit_card_payment"},
            )

    def test_correction_export_restores_configurable_text_without_double_escaping(
        self,
    ) -> None:
        correction = {
            "category": "=Custom Category",
            "owner": "@Custom Owner",
            "payment_method": "+Custom Method",
            "confidence": "0.75",
            "reason": "-Reviewed formula-like reason",
            "notes": "\tFormula-like note",
            "needs_review": "true",
        }
        with tempfile.TemporaryDirectory() as tmp:
            corrections_path = Path(tmp) / "corrections.csv"
            corrections_path.write_text(
                "transaction_id,category,flow_type,owner,payment_method,confidence,reason,notes,needs_review\n",
                encoding="utf-8",
            )
            config = {
                "corrections": str(corrections_path),
                "categories": ["=Custom Category"],
                "owners": ["@Custom Owner"],
                "payment_methods": ["+Custom Method"],
            }

            _, content, merged = prepare_corrections_document(
                config, {"=txn_safe": correction}
            )

            self.assertEqual(merged, {"=txn_safe": correction})
            corrections_path.write_text(content, encoding="utf-8", newline="")
            with corrections_path.open(newline="", encoding="utf-8-sig") as handle:
                [exported] = list(csv.DictReader(handle))
            self.assertEqual(exported["transaction_id"], "'=txn_safe")
            self.assertEqual(exported["category"], "'=Custom Category")
            self.assertEqual(exported["owner"], "'@Custom Owner")
            self.assertEqual(exported["payment_method"], "'+Custom Method")
            self.assertEqual(exported["reason"], "'-Reviewed formula-like reason")
            self.assertEqual(exported["notes"], "'\tFormula-like note")
            self.assertEqual(exported["confidence"], "0.75")
            self.assertEqual(exported["needs_review"], "true")

            self.assertEqual(load_corrections(config), {"=txn_safe": correction})
            _, rewritten, _ = prepare_corrections_document(config)
            self.assertEqual(rewritten, content)

    def test_normal_import_neutralizes_statement_text_but_not_negative_amounts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "formula.csv"
            self._write_statement(statement, "=SUM(A1:A2)")

            result = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"], cwd=root
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            ledger_path = root / "output" / "categorized.csv"
            with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
                [ledger_row] = list(csv.DictReader(handle))
            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8-sig"
            ) as handle:
                [review_row] = list(csv.DictReader(handle))
            self.assertEqual(ledger_row["merchant"], "'=SUM(A1:A2)")
            self.assertEqual(ledger_row["original_description"], "'=SUM(A1:A2)")
            self.assertEqual(review_row["merchant"], "'=SUM(A1:A2)")
            self.assertEqual(ledger_row["original_amount"], "-12.34")
            self.assertEqual(ledger_row["posted_amount"], "-12.34")
            self.assertEqual(ledger_row["amount_hkd"], "-12.34")
            [canonical] = read_ledger(ledger_path)
            self.assertEqual(canonical["merchant"], "=SUM(A1:A2)")
            self.assertEqual(canonical["original_description"], "=SUM(A1:A2)")
            before = ledger_path.read_bytes()

            repeated = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(ledger_path.read_bytes(), before)
            with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
                [repeated_row] = list(csv.DictReader(handle))
            self.assertEqual(
                repeated_row["transaction_id"], ledger_row["transaction_id"]
            )

    def test_structured_and_interactive_corrections_share_safe_serialization(
        self,
    ) -> None:
        custom_category = "=Custom Category"
        custom_owner = "@Custom Owner"
        custom_method = "+Custom Method"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "structured.csv"
            self._write_statement(statement, "SYNTHETIC STRUCTURED")
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            ledger_path = root / "output" / "categorized.csv"
            with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
                [imported_row] = list(csv.DictReader(handle))

            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["categories"] = sorted({*ALLOWED_CATEGORIES, custom_category})
            config["owners"] = sorted({*ALLOWED_OWNERS, custom_owner})
            config["payment_methods"] = sorted(
                {*ALLOWED_PAYMENT_METHODS, custom_method}
            )
            config_path.write_text(json.dumps(config), encoding="utf-8")
            correction_batch = json.dumps(
                [
                    {
                        "transaction_id": imported_row["transaction_id"],
                        "category": custom_category,
                        "owner": custom_owner,
                        "payment_method": custom_method,
                        "confidence": 0.75,
                        "reason": "-Structured reason",
                        "notes": "@Structured note",
                        "needs_review": True,
                    }
                ]
            )

            corrected = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=correction_batch,
            )

            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            artifact_paths = [
                ledger_path,
                root / "output" / "review_needed.csv",
                root / "corrections.csv",
            ]
            before = {path: path.read_bytes() for path in artifact_paths}
            with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
                [ledger_row] = list(csv.DictReader(handle))
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8-sig"
            ) as handle:
                [correction_row] = list(csv.DictReader(handle))
            self.assertEqual(ledger_row["category"], "'=Custom Category")
            self.assertEqual(ledger_row["owner"], "'@Custom Owner")
            self.assertEqual(ledger_row["payment_method"], "'+Custom Method")
            self.assertEqual(ledger_row["reason"], "'-Structured reason")
            self.assertEqual(ledger_row["notes"], "'@Structured note")
            self.assertEqual(correction_row["category"], "'=Custom Category")
            self.assertEqual(correction_row["confidence"], "0.75")

            repeated = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=correction_batch,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in artifact_paths}, before
            )

            interactive_statement = root / "interactive.csv"
            self._write_statement(interactive_statement, "SYNTHETIC INTERACTIVE")
            selectable_categories = sorted(set(config["categories"]) - {"Unknown"})
            category_number = selectable_categories.index(custom_category) + 1
            interactive = self._run_cli(
                ["import", str(interactive_statement)],
                cwd=root,
                input_text=f"{category_number}\n",
            )
            self.assertEqual(interactive.returncode, 0, interactive.stderr)
            with ledger_path.open(newline="", encoding="utf-8-sig") as handle:
                rows_by_merchant = {
                    row["merchant"]: row for row in csv.DictReader(handle)
                }
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8-sig"
            ) as handle:
                correction_rows = list(csv.DictReader(handle))
            self.assertEqual(
                rows_by_merchant["SYNTHETIC INTERACTIVE"]["category"],
                "'=Custom Category",
            )
            self.assertIn(
                "'=Custom Category", {row["category"] for row in correction_rows}
            )


if __name__ == "__main__":
    unittest.main()
