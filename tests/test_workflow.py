import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from honeymoney import cli
from honeymoney.cli import (
    _report_command,
    _resolve_period,
    _starter_csv_profile,
    _StatusLine,
)
from honeymoney.ollama import OllamaHttpRequest, apply_ollama_fallback
from honeymoney.schema import ALLOWED_CATEGORIES

REPO_ROOT = Path(__file__).resolve().parents[1]

PROMPT_CATEGORIES = sorted(ALLOWED_CATEGORIES - {"Unknown"})

# Fixed pre-identity-v2 public header. Tests that exercise migration must not
# seed their input by running the future importer or by borrowing its schema.
LEGACY_CATEGORIZED_COLUMNS = [
    "transaction_id",
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
    "category",
    "flow_type",
    "flow_source",
    "transfer_group_id",
    "paired_transaction_id",
    "reconciliation_status",
    "reconciliation_confidence",
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

LEGACY_CORRECTION_COLUMNS = [
    "transaction_id",
    "category",
    "flow_type",
    "owner",
    "payment_method",
    "confidence",
    "reason",
    "notes",
    "needs_review",
]


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
        self,
        args: list[str],
        cwd: Path,
        input_text: str | None = None,
        extra_pythonpath: Path | None = None,
        filesystem_fault: str | None = None,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        python_paths = []
        if filesystem_fault is not None:
            python_paths.append(REPO_ROOT / "tests" / "fault_injection")
            env["HONEYMONEY_TEST_FS_FAULT"] = filesystem_fault
        if extra_pythonpath is not None:
            python_paths.append(extra_pythonpath)
        python_paths.append(REPO_ROOT)
        env["PYTHONPATH"] = os.pathsep.join(map(str, python_paths))
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

    def _seed_pdf_replacement_workspace(
        self, tmp: str
    ) -> tuple[Path, Path, Path, Path]:
        root = self._setup_workspace(tmp)
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
        self.pages = [Page(page) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
            encoding="utf-8",
        )
        statements_dir = root / "replacement-input"
        statements_dir.mkdir()
        statement = statements_dir / "statement.pdf"
        statement.write_text(
            json.dumps(
                {
                    "pages": [
                        [
                            ["Date", "Description", "Debit", "Credit"],
                            ["2026-05-01", "SYNTHETIC MARKET", "10.00", ""],
                        ]
                    ]
                }
            ),
            encoding="utf-8",
        )
        profile_path = root / "profiles" / "synthetic_pdf.json"
        profile_path.write_text(
            json.dumps(
                {
                    "id": "synthetic_pdf",
                    "account_id": "synthetic_bank",
                    "account": "Synthetic Bank",
                    "account_type": "bank",
                    "institution": "Synthetic",
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
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["profiles"] = [
            str(root / "profiles" / "starter_csv.json"),
            str(profile_path),
        ]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (root / "profile_mappings.json").write_text(
            json.dumps(
                {
                    "filename_patterns": [
                        {"pattern": "statement.pdf", "profile": "synthetic_pdf"},
                        {"pattern": "*.csv", "profile": "starter_csv"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        first = self._run_cli(
            ["import", str(statement), "--no-interactive"],
            cwd=root,
            extra_pythonpath=fake_modules,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        return root, fake_modules, statement, config_path

    def _artifact_bytes(
        self, root: Path, relative_paths: list[str]
    ) -> dict[str, bytes | None]:
        return {
            relative_path: (
                (root / relative_path).read_bytes()
                if (root / relative_path).exists()
                else None
            )
            for relative_path in relative_paths
        }

    def _review_artifact_bytes(self, root: Path) -> dict[str, bytes | None]:
        return self._artifact_bytes(
            root,
            [
                "output/categorized.csv",
                "output/review_needed.csv",
                "corrections.csv",
                "output/.honeymoney-identity-manifest.json",
            ],
        )

    def _import_artifact_bytes(self, root: Path) -> dict[str, bytes | None]:
        return self._artifact_bytes(
            root,
            [
                "output/categorized.csv",
                "output/review_needed.csv",
                "output/import_report.json",
                "output/.honeymoney-identity-manifest.json",
            ],
        )

    def _reset_state_bytes(self, root: Path) -> dict[str, bytes | None]:
        return self._artifact_bytes(
            root,
            [
                "output/categorized.csv",
                "output/review_needed.csv",
                "output/import_report.json",
                "corrections.csv",
                "output/.honeymoney-identity-manifest.json",
            ],
        )

    def test_first_import_failure_does_not_publish_a_partial_generation(self) -> None:
        faults = [
            "file-fsync:review_needed.csv",
            "file-fsync:import_report.json",
            "file-fsync:categorized.csv",
            "replace-before:review_needed.csv",
            "replace-before:import_report.json",
            "replace-before:categorized.csv",
            "directory-fsync-after:categorized.csv",
        ]
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statement = root / "may.csv"
                self._write_statement(
                    statement, ["2026-05-04,SYNTHETIC MARKET,-12.00,HKD"]
                )

                result = self._run_cli(
                    ["import", str(statement), "--no-interactive"],
                    cwd=root,
                    filesystem_fault=fault,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                for name in (
                    "categorized.csv",
                    "review_needed.csv",
                    "import_report.json",
                ):
                    self.assertFalse((root / "output" / name).exists())

    def test_interactive_import_failure_restores_the_correction_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,SYNTHETIC MARKET,-12.00,HKD"])
            corrections = root / "corrections.csv"
            before = corrections.read_bytes()

            result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
                filesystem_fault="replace-before:corrections.csv",
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(corrections.read_bytes(), before)
            for name in (
                "categorized.csv",
                "review_needed.csv",
                "import_report.json",
            ):
                self.assertFalse((root / "output" / name).exists())

    def test_failed_replacement_restores_the_complete_old_generation(self) -> None:
        faults = [
            "file-fsync:review_needed.csv",
            "file-fsync:import_report.json",
            "file-fsync:categorized.csv",
            "replace-before:review_needed.csv",
            "replace-before:import_report.json",
            "replace-before:categorized.csv",
            "directory-fsync-after:categorized.csv",
        ]
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statement = root / "may.csv"
                self._write_statement(
                    statement, ["2026-05-04,ORIGINAL MARKET,-12.00,HKD"]
                )
                first = self._run_cli(
                    ["import", str(statement), "--no-interactive"], cwd=root
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                before = self._import_artifact_bytes(root)
                self._write_statement(
                    statement, ["2026-05-04,UPDATED MARKET,-15.00,HKD"]
                )

                result = self._run_cli(
                    [
                        "import",
                        str(statement),
                        "--replace",
                        "--no-interactive",
                    ],
                    cwd=root,
                    filesystem_fault=fault,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self._import_artifact_bytes(root), before)

    def test_next_command_recovers_a_retained_committed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,ORIGINAL MARKET,-12.00,HKD"])
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self._write_statement(statement, ["2026-05-04,UPDATED MARKET,-15.00,HKD"])

            interrupted = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
                filesystem_fault="replace-after:categorized.csv",
            )
            self.assertEqual(interrupted.returncode, 75, interrupted.stderr)

            recovered = self._run_cli(["status"], cwd=root)

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["merchant"], "UPDATED MARKET")
            self.assertTrue((root / "output" / "review_needed.csv").exists())
            self.assertTrue((root / "output" / "import_report.json").exists())
            self.assertEqual(
                list((root / "output").glob(".*honeymoney-state.json")), []
            )

    def test_next_command_discards_a_retained_uncommitted_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,SYNTHETIC MARKET,-12.00,HKD"])

            interrupted = self._run_cli(
                ["import", str(statement), "--no-interactive"],
                cwd=root,
                filesystem_fault="replace-after:import_report.json",
            )
            self.assertEqual(interrupted.returncode, 75, interrupted.stderr)

            recovered = self._run_cli(["status"], cwd=root)

            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            for name in (
                "categorized.csv",
                "review_needed.csv",
                "import_report.json",
            ):
                self.assertFalse((root / "output" / name).exists())
            self.assertEqual(
                list((root / "output").glob(".*honeymoney-state.json")), []
            )

    def _ledger_rows(self, root: Path) -> list[dict[str, str]]:
        with (root / "output" / "categorized.csv").open(
            newline="", encoding="utf-8"
        ) as fh:
            return list(csv.DictReader(fh))

    def _legacy_ledger_row(
        self,
        *,
        transaction_id: str,
        merchant: str,
        source_file: str,
        source_row: str,
        amount: str = "-44.00",
    ) -> dict[str, str]:
        row = {column: "" for column in LEGACY_CATEGORIZED_COLUMNS}
        row.update(
            {
                "transaction_id": transaction_id,
                "date": "2026-06-18",
                "transaction_date": "2026-06-18",
                "account_id": "starter_csv",
                "account": "Starter CSV",
                "account_type": "bank",
                "institution": "Starter",
                "country": "HK",
                "original_amount": amount,
                "original_currency": "HKD",
                "posted_amount": amount,
                "posted_currency": "HKD",
                "amount_hkd": amount,
                "merchant": merchant,
                "original_description": merchant,
                "category": "Unknown",
                "flow_type": "unresolved",
                "flow_source": "deterministic",
                "reconciliation_status": "not_applicable",
                "owner": "Household",
                "payment_method": "Bank Account",
                "confidence": "0.00",
                "needs_review": "true",
                "reason": "No matching category rule",
                "flags": "uncategorized",
                "source_file": source_file,
                "source_row": source_row,
            }
        )
        return row

    def _write_legacy_ledger(self, root: Path, rows: list[dict[str, str]]) -> None:
        with (root / "output" / "categorized.csv").open(
            "w", newline="", encoding="utf-8"
        ) as fh:
            writer = csv.DictWriter(fh, fieldnames=LEGACY_CATEGORIZED_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def _write_legacy_correction(
        self, root: Path, transaction_id: str, category: str = "Dining"
    ) -> None:
        row = {column: "" for column in LEGACY_CORRECTION_COLUMNS}
        row.update(
            {
                "transaction_id": transaction_id,
                "category": category,
                "needs_review": "false",
            }
        )
        with (root / "corrections.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEGACY_CORRECTION_COLUMNS)
            writer.writeheader()
            writer.writerow(row)

    def _correct_category(
        self, root: Path, transaction_id: str, category: str = "Dining"
    ) -> None:
        result = self._run_cli(
            ["correct", "--file", "-", "--json"],
            cwd=root,
            input_text=json.dumps(
                [{"transaction_id": transaction_id, "category": category}]
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_collision_replace_preserves_correction_and_reset_clears_it(self) -> None:
        identical = "2026-05-04,PERSISTED REPEAT,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "repeats.csv"
            self._write_statement(statement, [identical, identical])
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            original_rows = self._ledger_rows(root)
            corrected_id = original_rows[1]["transaction_id"]
            (root / "corrections.csv").write_text(
                "transaction_id,category,reason\n"
                f"{corrected_id},Dining,Synthetic persisted review\n",
                encoding="utf-8",
            )

            replaced = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            replaced_rows = self._ledger_rows(root)
            self.assertEqual(
                [row["transaction_id"] for row in replaced_rows],
                [row["transaction_id"] for row in original_rows],
            )
            self.assertEqual(
                [row["category"] for row in replaced_rows], ["Unknown", "Dining"]
            )

            reset = self._run_cli(
                ["import", str(statement), "--reset", "--no-interactive"], cwd=root
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertEqual(
                [row["category"] for row in self._ledger_rows(root)],
                ["Unknown", "Unknown"],
            )
            self.assertEqual(
                len(
                    (root / "corrections.csv").read_text(encoding="utf-8").splitlines()
                ),
                1,
            )

    def test_identical_rows_have_the_same_distinct_ids_separately_or_as_a_directory(
        self,
    ) -> None:
        identical_row = "2026-06-18,SYNTHETIC REPEATED CHARGE,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            separate_parent = base / "separate"
            separate_parent.mkdir()
            separate_root = self._setup_workspace(str(separate_parent))
            separate_statement_dir = separate_root / "statements"
            separate_statement_dir.mkdir()
            separate_statements = [
                separate_statement_dir / "first.csv",
                separate_statement_dir / "second.csv",
            ]
            for statement in separate_statements:
                self._write_statement(statement, [identical_row])
                result = self._run_cli(
                    ["import", str(statement), "--no-interactive"],
                    cwd=separate_root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            separate_rows = self._ledger_rows(separate_root)

            directory_parent = base / "directory"
            directory_parent.mkdir()
            directory_root = self._setup_workspace(str(directory_parent))
            statement_dir = directory_root / "statements"
            statement_dir.mkdir()
            for name in ("first.csv", "second.csv"):
                self._write_statement(statement_dir / name, [identical_row])
            directory_result = self._run_cli(
                ["import", str(statement_dir), "--no-interactive"],
                cwd=directory_root,
            )
            self.assertEqual(directory_result.returncode, 0, directory_result.stderr)
            directory_rows = self._ledger_rows(directory_root)

            self.assertEqual(len(separate_rows), 2)
            self.assertEqual(len(directory_rows), 2)
            separate_ids = {row["transaction_id"] for row in separate_rows}
            directory_ids = {row["transaction_id"] for row in directory_rows}
            self.assertEqual(len(separate_ids), 2)
            self.assertEqual(separate_ids, directory_ids)

    def test_same_basename_statements_from_distinct_directories_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first_dir = root / "first-source"
            second_dir = root / "second-source"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / "may.csv"
            second = second_dir / "may.csv"
            self._write_statement(
                first, ["2026-05-04,SYNTHETIC FIRST SOURCE,-12.00,HKD"]
            )
            self._write_statement(
                second, ["2026-05-05,SYNTHETIC SECOND SOURCE,-18.00,HKD"]
            )

            for statement in (first, second):
                result = self._run_cli(
                    ["import", str(statement), "--no-interactive"], cwd=root
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            rows = self._ledger_rows(root)
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {row["merchant"] for row in rows},
                {"SYNTHETIC FIRST SOURCE", "SYNTHETIC SECOND SOURCE"},
            )

    def test_source_rename_and_folder_invocation_keep_transaction_identity(
        self,
    ) -> None:
        row = "2026-06-18,SYNTHETIC INVOCATION STABILITY,-24.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement_dir = root / "statements"
            statement_dir.mkdir()
            statement = statement_dir / "original.csv"
            self._write_statement(statement, [row])

            folder_import = self._run_cli(
                ["import", str(statement_dir), "--no-interactive"], cwd=root
            )
            self.assertEqual(folder_import.returncode, 0, folder_import.stderr)
            [folder_row] = self._ledger_rows(root)

            single_import = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"], cwd=root
            )
            self.assertEqual(single_import.returncode, 0, single_import.stderr)
            [single_row] = self._ledger_rows(root)

            renamed = statement.with_name("renamed.csv")
            statement.rename(renamed)
            renamed_import = self._run_cli(
                ["import", str(renamed), "--replace", "--no-interactive"], cwd=root
            )
            self.assertEqual(renamed_import.returncode, 0, renamed_import.stderr)
            [renamed_row] = self._ledger_rows(root)

            self.assertEqual(
                {
                    folder_row["transaction_id"],
                    single_row["transaction_id"],
                    renamed_row["transaction_id"],
                },
                {folder_row["transaction_id"]},
            )

    def test_inserting_an_identical_source_does_not_move_a_saved_correction(
        self,
    ) -> None:
        identical_row = "2026-06-18,SYNTHETIC SOURCE COLLISION,-32.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            for name in ("middle.csv", "zeta.csv"):
                self._write_statement(statements / name, [identical_row])
            first = self._run_cli(
                ["import", str(statements), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            rows_by_source = {
                row["source_file"]: row for row in self._ledger_rows(root)
            }
            original_ids = {
                source: row["transaction_id"] for source, row in rows_by_source.items()
            }
            corrected_id = rows_by_source["zeta.csv"]["transaction_id"]
            self._correct_category(root, corrected_id)

            alpha = statements / "alpha.csv"
            self._write_statement(alpha, [identical_row])
            inserted = self._run_cli(
                ["import", str(alpha), "--no-interactive"], cwd=root
            )
            self.assertEqual(inserted.returncode, 0, inserted.stderr)
            inserted_by_source = {
                row["source_file"]: row for row in self._ledger_rows(root)
            }
            self.assertEqual(
                set(inserted_by_source), {"alpha.csv", "middle.csv", "zeta.csv"}
            )
            self.assertEqual(
                {
                    source: inserted_by_source[source]["transaction_id"]
                    for source in original_ids
                },
                original_ids,
            )
            self.assertEqual(
                len({row["transaction_id"] for row in inserted_by_source.values()}),
                3,
            )

            replacement = self._run_cli(
                ["import", str(statements), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replaced_by_source = {
                row["source_file"]: row for row in self._ledger_rows(root)
            }

            self.assertEqual(
                {
                    source: replaced_by_source[source]["transaction_id"]
                    for source in inserted_by_source
                },
                {
                    source: inserted_by_source[source]["transaction_id"]
                    for source in inserted_by_source
                },
            )
            self.assertEqual(
                {source: row["category"] for source, row in replaced_by_source.items()},
                {
                    "alpha.csv": "Unknown",
                    "middle.csv": "Unknown",
                    "zeta.csv": "Dining",
                },
            )

    def test_ambiguous_legacy_duplicate_replace_and_reset_fail_before_mutation(
        self,
    ) -> None:
        identical_row = "2026-06-18,SYNTHETIC LEGACY COLLISION,-44.00,HKD"
        for action in ("--replace", "--reset"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statement = root / "legacy.csv"
                self._write_statement(statement, [identical_row, identical_row])
                shared_id = "txn_aaaaaaaaaaaaaaaa"
                duplicate_rows = [
                    self._legacy_ledger_row(
                        transaction_id=shared_id,
                        merchant="SYNTHETIC LEGACY COLLISION",
                        source_file="legacy.csv",
                        source_row=source_row,
                    )
                    for source_row in ("2", "3")
                ]
                self._write_legacy_ledger(root, duplicate_rows)
                self.assertEqual(len(duplicate_rows), 2)
                self._write_legacy_correction(root, shared_id)
                before = self._review_artifact_bytes(root)
                self._write_statement(statement, [identical_row])

                result = self._run_cli(
                    ["import", str(statement), action, "--no-interactive"],
                    cwd=root,
                )

                after = self._review_artifact_bytes(root)
                with self.subTest(action=action, contract="rejected"):
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(action=action, contract="no mutation"):
                    changed_artifacts = [
                        name for name in before if before[name] != after[name]
                    ]
                    self.assertEqual(changed_artifacts, [])

    def test_changed_namespace_and_revision_replace_reset_fail_before_mutation(
        self,
    ) -> None:
        for action in ("--replace", "--reset"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                original_dir = root / "original-source"
                moved_dir = root / "moved-source"
                original_dir.mkdir()
                moved_dir.mkdir()
                original = original_dir / "statement.csv"
                self._write_statement(
                    original,
                    ["2026-06-18,SYNTHETIC ORIGINAL REVISION,-51.00,HKD"],
                )
                first = self._run_cli(
                    ["import", str(original), "--no-interactive"], cwd=root
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                [original_row] = self._ledger_rows(root)
                self._correct_category(root, original_row["transaction_id"])
                before = self._reset_state_bytes(root)

                moved = moved_dir / original.name
                original.rename(moved)
                self._write_statement(
                    moved,
                    ["2026-06-19,SYNTHETIC CHANGED REVISION,-52.00,HKD"],
                )
                result = self._run_cli(
                    ["import", str(moved), action, "--no-interactive"], cwd=root
                )

                after = self._reset_state_bytes(root)
                with self.subTest(action=action, contract="target not found"):
                    self.assertEqual(result.returncode, 2, result.stderr)
                with self.subTest(action=action, contract="no mutation"):
                    changed_artifacts = [
                        name for name in before if before[name] != after[name]
                    ]
                    self.assertEqual(changed_artifacts, [])

    def test_accepted_rename_empty_reset_and_exact_recurrence_clear_correction(
        self,
    ) -> None:
        source_row = "2026-06-18,SYNTHETIC RECURRING SOURCE,-61.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            original_dir = root / "original-source"
            renamed_dir = root / "renamed-source"
            original_dir.mkdir()
            renamed_dir.mkdir()
            statement = original_dir / "statement.csv"
            self._write_statement(statement, [source_row])
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            [first_row] = self._ledger_rows(root)
            self._correct_category(root, first_row["transaction_id"])

            renamed = renamed_dir / statement.name
            statement.rename(renamed)
            accepted_rename = self._run_cli(
                ["import", str(renamed), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(accepted_rename.returncode, 0, accepted_rename.stderr)
            [renamed_row] = self._ledger_rows(root)
            self.assertEqual(renamed_row["transaction_id"], first_row["transaction_id"])
            self.assertEqual(renamed_row["category"], "Dining")

            self._write_statement(renamed, [])
            emptied = self._run_cli(
                ["import", str(renamed), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(emptied.returncode, 0, emptied.stderr)
            self.assertEqual(self._ledger_rows(root), [])

            reset_empty = self._run_cli(
                ["import", str(renamed), "--reset", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(reset_empty.returncode, 0, reset_empty.stderr)
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])

            self._write_statement(renamed, [source_row])
            recurred = self._run_cli(
                ["import", str(renamed), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(recurred.returncode, 0, recurred.stderr)
            [recurred_row] = self._ledger_rows(root)
            self.assertEqual(
                recurred_row["transaction_id"], first_row["transaction_id"]
            )
            self.assertNotEqual(recurred_row["category"], "Dining")

    def test_reset_clears_corrections_for_active_and_retired_source_records(
        self,
    ) -> None:
        row_a = "2026-06-18,SYNTHETIC ACTIVE RECORD,-62.00,HKD"
        row_b = "2026-06-19,SYNTHETIC RETIRED RECORD,-63.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "partial-retirement.csv"
            self._write_statement(statement, [row_a, row_b])
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_rows = {row["merchant"]: row for row in self._ledger_rows(root)}
            self._correct_category(
                root, first_rows["SYNTHETIC ACTIVE RECORD"]["transaction_id"]
            )
            self._correct_category(
                root, first_rows["SYNTHETIC RETIRED RECORD"]["transaction_id"]
            )

            self._write_statement(statement, [row_a])
            replaced = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            [active_row] = self._ledger_rows(root)
            self.assertEqual(active_row["category"], "Dining")

            reset = self._run_cli(
                ["import", str(statement), "--reset", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            [reset_row] = self._ledger_rows(root)
            self.assertNotEqual(reset_row["category"], "Dining")
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])

    def test_new_same_fingerprint_at_different_origin_gets_no_retired_correction(
        self,
    ) -> None:
        source_row = "2026-06-18,SYNTHETIC REUSED FACTS,-64.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "origin-change.csv"
            self._write_statement(statement, [source_row])
            first = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            [first_row] = self._ledger_rows(root)
            self._correct_category(root, first_row["transaction_id"])

            self._write_statement(statement, [])
            emptied = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(emptied.returncode, 0, emptied.stderr)

            # The facts are identical, but the blank physical row moves the CSV
            # allocation locator from physical row 2 to physical row 3.
            self._write_statement(statement, ["", source_row])
            replaced = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            [new_row] = self._ledger_rows(root)
            self.assertNotEqual(new_row["transaction_id"], first_row["transaction_id"])
            self.assertNotEqual(new_row["category"], "Dining")

    def test_unrelated_import_retains_an_unresolved_legacy_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            unrelated = root / "unrelated.csv"
            legacy_before = self._legacy_ledger_row(
                transaction_id="txn_bbbbbbbbbbbbbbbb",
                merchant="SYNTHETIC LEGACY RETAINED",
                source_file="legacy.csv",
                source_row="2",
                amount="-71.00",
            )
            self._write_legacy_ledger(root, [legacy_before])

            self._write_statement(
                unrelated, ["2026-06-19,SYNTHETIC UNRELATED SOURCE,-72.00,HKD"]
            )
            second = self._run_cli(
                ["import", str(unrelated), "--no-interactive"], cwd=root
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            rows_by_merchant = {row["merchant"]: row for row in self._ledger_rows(root)}

            self.assertEqual(
                set(rows_by_merchant),
                {
                    "SYNTHETIC LEGACY RETAINED",
                    "SYNTHETIC UNRELATED SOURCE",
                },
            )
            self.assertEqual(
                rows_by_merchant["SYNTHETIC LEGACY RETAINED"]["transaction_id"],
                legacy_before["transaction_id"],
            )
            self.assertTrue(
                all(
                    not rows_by_merchant["SYNTHETIC LEGACY RETAINED"].get(field, "")
                    for field in (
                        "source_id",
                        "source_namespace_id",
                        "source_revision",
                        "source_record_id",
                    )
                )
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
            self.assertIn("identity_source_already_imported", second.stderr)
            self.assertIn("action=import", second.stderr)
            self.assertIn("replace or reset", second.stderr)

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

    def test_replace_preserves_pdf_ledger_when_pdf_support_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, config_path = (
                self._seed_pdf_replacement_workspace(tmp)
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            categorized_path = root / "output" / "categorized.csv"
            before = categorized_path.read_bytes()

            config["pdf"]["enabled"] = False
            config_path.write_text(json.dumps(config), encoding="utf-8")
            replacement = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            self.assertEqual(categorized_path.read_bytes(), before)
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "partial_success")
            self.assertEqual(report["files"][0]["status"], "skipped")
            self.assertIn("PDF parsing disabled", report["warnings"][0])

    def test_import_replace_preserves_pdf_rows_for_each_failure_stage(self) -> None:
        for failure_stage in [
            "missing_parser_support",
            "profile_selection",
            "parsing",
        ]:
            with self.subTest(failure_stage=failure_stage):
                with tempfile.TemporaryDirectory() as tmp:
                    root, fake_modules, statement, _ = (
                        self._seed_pdf_replacement_workspace(tmp)
                    )
                    categorized_path = root / "output" / "categorized.csv"
                    before = categorized_path.read_bytes()

                    if failure_stage == "missing_parser_support":
                        (fake_modules / "pdfplumber.py").write_text(
                            "raise ImportError('synthetic missing dependency')\n",
                            encoding="utf-8",
                        )
                    elif failure_stage == "profile_selection":
                        (root / "profile_mappings.json").write_text(
                            json.dumps({"filename_patterns": []}), encoding="utf-8"
                        )
                    else:
                        (fake_modules / "pdfplumber.py").write_text(
                            "def open(path):\n"
                            "    raise RuntimeError('synthetic parser failure')\n",
                            encoding="utf-8",
                        )

                    replacement = self._run_cli(
                        [
                            "import",
                            str(statement),
                            "--replace",
                            "--no-interactive",
                        ],
                        cwd=root,
                        extra_pythonpath=fake_modules,
                    )

                    self.assertEqual(replacement.returncode, 0, replacement.stderr)
                    self.assertEqual(categorized_path.read_bytes(), before)
                    report = json.loads(
                        (root / "output" / "import_report.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(report["status"], "partial_success")
                    self.assertEqual(report["files"][0]["status"], "failed")
                    self.assertTrue(report["files"][0]["reason"])
                    self.assertEqual(report["warnings"], [report["files"][0]["reason"]])

    def test_import_replace_updates_processed_csv_and_preserves_failed_pdf(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, _ = self._seed_pdf_replacement_workspace(tmp)
            statements_dir = statement.parent
            csv_statement = statements_dir / "may.csv"
            self._write_statement(
                csv_statement, ["2026-05-02,ORIGINAL SYNTHETIC SHOP,-20.00,HKD"]
            )
            first_csv = self._run_cli(
                ["import", str(csv_statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(first_csv.returncode, 0, first_csv.stderr)
            self._write_statement(
                csv_statement, ["2026-05-03,UPDATED SYNTHETIC SHOP,-30.00,HKD"]
            )
            (fake_modules / "pdfplumber.py").write_text(
                "def open(path):\n    raise RuntimeError('synthetic parser failure')\n",
                encoding="utf-8",
            )

            replacement = self._run_cli(
                ["import", str(statements_dir), "--replace", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = {row["source_file"]: row for row in csv.DictReader(fh)}
            self.assertEqual(set(rows), {"statement.pdf", "may.csv"})
            self.assertEqual(rows["statement.pdf"]["merchant"], "SYNTHETIC MARKET")
            self.assertEqual(rows["may.csv"]["merchant"], "UPDATED SYNTHETIC SHOP")
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            statuses = {
                file_report["source_file"]: file_report["status"]
                for file_report in report["files"]
            }
            self.assertEqual(
                statuses, {"may.csv": "processed", "statement.pdf": "failed"}
            )

    def test_import_replace_processed_empty_pdf_removes_prior_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, _ = self._seed_pdf_replacement_workspace(tmp)
            statement.write_text(json.dumps({"pages": []}), encoding="utf-8")

            replacement = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["files"][0]["status"], "processed")
            self.assertEqual(report["files"][0]["transaction_count"], "0")
            self.assertEqual(report["warnings"], [])

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

    def test_interactive_reset_replaces_the_old_correction_with_the_new_choice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,SYNTHETIC MARKET,-12.00,HKD"])
            first = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            reset = self._run_cli(
                ["import", str(statement), "--reset"],
                cwd=root,
                input_text=f"{_category_number('Dining')}\n",
            )

            self.assertEqual(reset.returncode, 0, reset.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                [row] = list(csv.DictReader(fh))
            self.assertEqual(row["category"], "Dining")
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                [correction] = list(csv.DictReader(fh))
            self.assertEqual(correction["transaction_id"], row["transaction_id"])
            self.assertEqual(correction["category"], "Dining")

    def test_reset_rule_and_persistence_failures_restore_the_old_generation(
        self,
    ) -> None:
        for failure in ("rules", "persistence"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statement = root / "may.csv"
                self._write_statement(
                    statement, ["2026-05-04,SYNTHETIC MARKET,-12.00,HKD"]
                )
                first = self._run_cli(
                    ["import", str(statement)],
                    cwd=root,
                    input_text=f"{_category_number('Groceries')}\n",
                )
                self.assertEqual(first.returncode, 0, first.stderr)
                before = self._reset_state_bytes(root)
                if failure == "rules":
                    (root / "rules.json").write_text(
                        json.dumps(
                            {"rules": [{"id": "invalid", "category": "Not configured"}]}
                        ),
                        encoding="utf-8",
                    )
                    fault = None
                else:
                    fault = "replace-before:categorized.csv"

                result = self._run_cli(
                    ["import", str(statement), "--reset", "--no-interactive"],
                    cwd=root,
                    filesystem_fault=fault,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self._reset_state_bytes(root), before)

    def test_reset_csv_validation_failure_preserves_the_old_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,SYNTHETIC MARKET,-12.00,HKD"])
            first = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            before = self._reset_state_bytes(root)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["csv"]["columns"]["amount"] = "Missing Amount"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            result = self._run_cli(
                ["import", str(statement), "--reset", "--no-interactive"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self._reset_state_bytes(root), before)

    def test_failed_pdf_reset_preserves_ledger_and_correction_but_reports_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, _ = self._seed_pdf_replacement_workspace(tmp)
            categorized = root / "output" / "categorized.csv"
            with categorized.open(newline="", encoding="utf-8") as fh:
                [row] = list(csv.DictReader(fh))
            corrected = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                            "needs_review": False,
                        }
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            protected_before = {
                path: path.read_bytes()
                for path in (
                    categorized,
                    root / "output" / "review_needed.csv",
                    root / "corrections.csv",
                )
            }
            (fake_modules / "pdfplumber.py").write_text(
                "def open(path):\n    raise RuntimeError('synthetic parser failure')\n",
                encoding="utf-8",
            )

            result = self._run_cli(
                ["import", str(statement), "--reset", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in protected_before},
                protected_before,
            )
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["files"][0]["status"], "failed")
            self.assertEqual(report["files"][0]["requested_action"], "reset")
            self.assertEqual(report["files"][0]["ledger_action"], "preserved")

    def test_mixed_reset_removes_corrections_only_for_processed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, pdf_statement, _ = self._seed_pdf_replacement_workspace(
                tmp
            )
            statements = pdf_statement.parent
            csv_statement = statements / "may.csv"
            self._write_statement(
                csv_statement, ["2026-05-02,ORIGINAL SHOP,-20.00,HKD"]
            )
            imported = self._run_cli(
                ["import", str(csv_statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            categorized = root / "output" / "categorized.csv"
            with categorized.open(newline="", encoding="utf-8") as fh:
                rows = {row["source_file"]: row for row in csv.DictReader(fh)}
            corrected = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                            "needs_review": False,
                        }
                        for row in rows.values()
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            pdf_id = rows["statement.pdf"]["transaction_id"]
            csv_id = rows["may.csv"]["transaction_id"]
            self._write_statement(csv_statement, ["2026-05-03,UPDATED SHOP,-30.00,HKD"])
            (fake_modules / "pdfplumber.py").write_text(
                "def open(path):\n    raise RuntimeError('synthetic parser failure')\n",
                encoding="utf-8",
            )

            reset = self._run_cli(
                [
                    "import",
                    str(statements),
                    "--reset",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(reset.returncode, 0, reset.stderr)
            with categorized.open(newline="", encoding="utf-8") as fh:
                reset_rows = {row["source_file"]: row for row in csv.DictReader(fh)}
            self.assertEqual(reset_rows["statement.pdf"]["category"], "Groceries")
            self.assertEqual(reset_rows["may.csv"]["merchant"], "UPDATED SHOP")
            self.assertEqual(reset_rows["may.csv"]["category"], "Unknown")
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                correction_ids = {row["transaction_id"] for row in csv.DictReader(fh)}
            self.assertIn(pdf_id, correction_ids)
            self.assertNotIn(csv_id, correction_ids)
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            actions = {
                item["source_file"]: (item["status"], item["ledger_action"])
                for item in report["files"]
            }
            self.assertEqual(
                actions,
                {
                    "may.csv": ("processed", "reset"),
                    "statement.pdf": ("failed", "preserved"),
                },
            )
            payload = json.loads(reset.stdout)
            self.assertEqual(payload["command"], "import")
            self.assertEqual(payload["status"], "partial_success")
            self.assertEqual(payload["data"]["files"], report["files"])

            before_repeat = self._reset_state_bytes(root)
            repeated = self._run_cli(
                ["import", str(statements), "--reset", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(self._reset_state_bytes(root), before_repeat)

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
            self.assertIn(
                "Review complete: 1 updated, 0 still need review", review_result.stdout
            )
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

    def test_interactive_and_one_shot_review_share_persistence_rollback(self) -> None:
        for review_kind in ("interactive", "one-shot"):
            with (
                self.subTest(review_kind=review_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = self._setup_workspace(tmp)
                statement = root / "may.csv"
                self._write_statement(
                    statement, ["2026-05-04,SYNTHETIC PURCHASE,-12.00,HKD"]
                )
                imported = self._run_cli(
                    ["import", str(statement), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                with (root / "output" / "categorized.csv").open(
                    newline="", encoding="utf-8"
                ) as fh:
                    [row] = list(csv.DictReader(fh))
                before = self._review_artifact_bytes(root)
                if review_kind == "interactive":
                    args = ["review"]
                    input_text = f"{_category_number('Groceries')}\n"
                else:
                    args = [
                        "review",
                        "--transaction",
                        row["transaction_id"],
                        "--as",
                        "expense",
                        "--json",
                    ]
                    input_text = None

                result = self._run_cli(
                    args,
                    cwd=root,
                    input_text=input_text,
                    filesystem_fault="replace-before:categorized.csv",
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self._review_artifact_bytes(root), before)

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
            self.assertIn(
                "Review complete: 0 updated, 0 still need review", review_result.stdout
            )

    def test_review_category_revisits_matching_rows_without_adding_pending_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,GENERAL STORE,-120.50,HKD",
                    "2026-05-05,UNSORTED PURCHASE,-8.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Other')}\n\n",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            review_result = self._run_cli(
                ["review", "--category", "Other"],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn("1 records in selected categories", review_result.stdout)
            self.assertIn("GENERAL STORE", review_result.stdout)
            self.assertNotIn("UNSORTED PURCHASE", review_result.stdout)
            self.assertIn(
                "Review complete: 1 updated from selected categories, "
                "1 still need review",
                review_result.stdout,
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = {row["merchant"]: row for row in csv.DictReader(fh)}
            self.assertEqual(rows["GENERAL STORE"]["category"], "Groceries")
            self.assertEqual(rows["GENERAL STORE"]["needs_review"], "false")
            self.assertEqual(rows["GENERAL STORE"]["flow_type"], "expense")
            self.assertEqual(rows["GENERAL STORE"]["flow_source"], "deterministic")
            self.assertEqual(rows["UNSORTED PURCHASE"]["category"], "Unknown")
            self.assertEqual(rows["UNSORTED PURCHASE"]["needs_review"], "true")

            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                corrections = list(csv.DictReader(fh))
            self.assertEqual(
                corrections[-1]["transaction_id"],
                rows["GENERAL STORE"]["transaction_id"],
            )
            self.assertEqual(corrections[-1]["category"], "Groceries")

    def test_bare_review_ignores_rows_that_do_not_need_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,REVIEWED PURCHASE,-120.50,HKD",
                    "2026-05-05,PENDING PURCHASE,-8.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Other')}\n\n",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            review_result = self._run_cli(
                ["review"],
                cwd=root,
                input_text=f"{_category_number('Groceries')}\n",
            )

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn("1 records need review", review_result.stdout)
            self.assertIn("PENDING PURCHASE", review_result.stdout)
            self.assertNotIn("REVIEWED PURCHASE", review_result.stdout)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = {row["merchant"]: row for row in csv.DictReader(fh)}
            self.assertEqual(rows["REVIEWED PURCHASE"]["category"], "Other")
            self.assertEqual(rows["PENDING PURCHASE"]["category"], "Groceries")

    def test_repeated_review_categories_select_the_union_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            merchants = ["FIRST PURCHASE", "SECOND PURCHASE", "THIRD PURCHASE"]
            self._write_statement(
                statement,
                [
                    f"2026-05-04,{merchants[0]},-10.00,HKD",
                    f"2026-05-05,{merchants[1]},-20.00,HKD",
                    f"2026-05-06,{merchants[2]},-30.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=(
                    f"{_category_number('Other')}\n"
                    f"{_category_number('Groceries')}\n"
                    f"{_category_number('Other')}\n"
                ),
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)

            review_result = self._run_cli(
                [
                    "review",
                    "--category",
                    "Other",
                    "--category",
                    "Other",
                    "--category",
                    "Groceries",
                ],
                cwd=root,
                input_text="\n\n\n",
            )

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn("3 records in selected categories", review_result.stdout)
            self.assertIn("[3/3]", review_result.stdout)
            for merchant in merchants:
                self.assertEqual(review_result.stdout.count(merchant), 1)

    def test_filtered_review_skip_and_quit_do_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,FIRST OTHER,-10.00,HKD",
                    "2026-05-05,SECOND OTHER,-20.00,HKD",
                    "2026-05-06,THIRD OTHER,-30.00,HKD",
                ],
            )
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=(f"{_category_number('Other')}\n" * 3),
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            before = self._review_artifact_bytes(root)

            review_result = self._run_cli(
                ["review", "--category", "Other"], cwd=root, input_text="\nq\n"
            )

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn("FIRST OTHER", review_result.stdout)
            self.assertIn("SECOND OTHER", review_result.stdout)
            self.assertNotIn("THIRD OTHER", review_result.stdout)
            self.assertEqual(self._review_artifact_bytes(root), before)

    def test_filtered_review_choice_then_quit_updates_only_affected_pair_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["csv"]["columns"]["account_id"] = "Account ID"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            statement = root / "may.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency,Account ID",
                        "2026-05-04,INCOMING TRANSFER,100.00,HKD,primary_bank",
                        "2026-05-04,OUTGOING TRANSFER,-100.00,HKD,secondary_bank",
                        "2026-05-05,UNRELATED PURCHASE,-30.00,HKD,primary_bank",
                    ]
                ),
                encoding="utf-8",
            )
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=(
                    f"{_category_number('Other')}\n"
                    f"{_category_number('Groceries')}\n"
                    f"{_category_number('Shopping')}\n"
                ),
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            ledger_path = root / "output" / "categorized.csv"
            reconcile_result = self._run_cli(["reconcile"], cwd=root)
            self.assertEqual(reconcile_result.returncode, 0, reconcile_result.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
                fieldnames = list(rows[0])
            rows[2]["flow_type"] = "unresolved"
            rows[2]["flow_source"] = ""
            with ledger_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            before = {row["merchant"]: row for row in rows}
            self.assertEqual(
                before["OUTGOING TRANSFER"]["flow_type"], "internal_transfer"
            )
            self.assertEqual(
                before["OUTGOING TRANSFER"]["reconciliation_status"], "paired"
            )

            review_result = self._run_cli(
                ["review", "--category", "Other", "--category", "Groceries"],
                cwd=root,
                input_text=f"{_category_number('Income')}\nq\n",
            )

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as fh:
                after = {row["merchant"]: row for row in csv.DictReader(fh)}
            self.assertEqual(after["INCOMING TRANSFER"]["category"], "Income")
            self.assertEqual(after["INCOMING TRANSFER"]["flow_type"], "income")
            self.assertEqual(
                after["OUTGOING TRANSFER"]["category"],
                before["OUTGOING TRANSFER"]["category"],
            )
            self.assertEqual(
                after["OUTGOING TRANSFER"]["needs_review"],
                before["OUTGOING TRANSFER"]["needs_review"],
            )
            self.assertEqual(after["OUTGOING TRANSFER"]["flow_type"], "expense")
            self.assertEqual(
                after["OUTGOING TRANSFER"]["reconciliation_status"],
                "not_applicable",
            )
            self.assertEqual(after["OUTGOING TRANSFER"]["paired_transaction_id"], "")
            self.assertEqual(after["UNRELATED PURCHASE"], before["UNRELATED PURCHASE"])
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                corrections = list(csv.DictReader(fh))
            outgoing_id = after["OUTGOING TRANSFER"]["transaction_id"]
            self.assertEqual(
                sum(
                    correction["transaction_id"] == outgoing_id
                    for correction in corrections
                ),
                1,
            )

    def test_filtered_review_empty_selection_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,GENERAL STORE,-10.00,HKD"])
            import_result = self._run_cli(
                ["import", str(statement)],
                cwd=root,
                input_text=f"{_category_number('Other')}\n",
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            before = self._review_artifact_bytes(root)

            review_result = self._run_cli(["review", "--category", "Travel"], cwd=root)

            self.assertEqual(review_result.returncode, 0, review_result.stderr)
            self.assertIn(
                "No transactions found in selected categories: Travel",
                review_result.stdout,
            )
            self.assertIn(
                "Review complete: 0 updated from selected categories, "
                "0 still need review",
                review_result.stdout,
            )
            self.assertEqual(self._review_artifact_bytes(root), before)

    def test_filtered_review_rejects_invalid_or_malformed_categories_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(statement, ["2026-05-04,PENDING PURCHASE,-10.00,HKD"])
            import_result = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["categories"] = ["Groceries", "Unknown"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            before = self._review_artifact_bytes(root)

            invalid_result = self._run_cli(["review", "--category", "Other"], cwd=root)

            self.assertEqual(invalid_result.returncode, 2)
            self.assertIn("Unsupported review category: Other", invalid_result.stderr)
            self.assertNotIn("Category [number/Enter/q]", invalid_result.stdout)
            self.assertEqual(self._review_artifact_bytes(root), before)

            malformed_result = self._run_cli(["review", "--category"], cwd=root)

            self.assertEqual(malformed_result.returncode, 2)
            self.assertIn(
                "argument --category: expected one argument", malformed_result.stderr
            )
            self.assertEqual(self._review_artifact_bytes(root), before)

    def test_review_help_and_readme_document_category_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            help_result = self._run_cli(["help"], cwd=root)
            review_help_result = self._run_cli(["review", "--help"], cwd=root)

            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("honeymoney review [--category CATEGORY]", help_result.stdout)
            self.assertEqual(
                review_help_result.returncode, 0, review_help_result.stderr
            )
            self.assertIn("--category CATEGORY", review_help_result.stdout)
            self.assertIn("repeat to select multiple", review_help_result.stdout)
            readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
            self.assertIn("honeymoney review --category Other", readme)

    def test_starter_profile_skips_previous_balance_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "starter.csv"
            statement.write_text(
                "\n".join(
                    [
                        "Date,Description,Amount,Currency",
                        "2026-05-18,PREVIOUS BALANCE,-5632.88,HKD",
                        "2026-05-19,PARKNSHOP,-120.50,HKD",
                        "2026-05-20,SALARY,20000,HKD",
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

    def test_hsbc_credit_card_pdf_word_rows_keep_amounts_with_merchants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_modules = root / "fake_modules"
            fake_modules.mkdir()
            (fake_modules / "pdfplumber.py").write_text(
                """
import builtins
import json


class Page:
    def __init__(self, words):
        self._words = words

    def extract_words(self, **kwargs):
        return self._words

    def extract_tables(self):
        return []


class Pdf:
    def __init__(self, path):
        self.path = path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.path, encoding="utf-8").read())
        self.pages = [Page(page["words"]) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(path):
    return Pdf(path)
""",
                encoding="utf-8",
            )

            def word(text: str, top: float, x0: float) -> dict[str, object]:
                return {"text": text, "top": top, "x0": x0}

            pdf_path = root / "statement.pdf"
            pdf_path.write_text(
                json.dumps(
                    {
                        "pages": [
                            {
                                "words": [
                                    word("Post", 10, 60),
                                    word("date", 10, 75),
                                    word("Trans", 10, 100),
                                    word("date", 10, 120),
                                    word("Description", 10, 267),
                                    word("Amount", 10, 495),
                                    word("PREVIOUS", 20, 137),
                                    word("BALANCE", 20, 180),
                                    word("5,632.88", 20, 518),
                                    word("19MAY", 30, 64),
                                    word("18MAY", 30, 99),
                                    word("GOGO", 30, 137),
                                    word("TECH", 30, 161),
                                    word("LIMITED", 30, 185),
                                    word("95.00", 30, 532),
                                    word("02JUN", 40, 64),
                                    word("01JUN", 40, 99),
                                    word("24/7", 40, 137),
                                    word("FITNESS", 40, 161),
                                    word("HONG", 40, 262),
                                    word("KONG", 40, 286),
                                    word("HK", 40, 334),
                                    word("498.00", 40, 527),
                                    word("04JUN", 50, 64),
                                    word("02JUN", 50, 99),
                                    word("DCC", 50, 137),
                                    word("FEE-NON-HK", 50, 156),
                                    word("MERCHANT", 50, 209),
                                    word("0.08", 50, 537),
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile_path = root / "profile.json"
            bundled_profile_path = (
                REPO_ROOT
                / "honeymoney"
                / "data"
                / "profiles"
                / "hsbc_hk_credit_card_pdf.json"
            )
            profile_path.write_text(
                bundled_profile_path.read_text(encoding="utf-8"),
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
            env = dict(os.environ)
            env["PYTHONPATH"] = f"{fake_modules}:{REPO_ROOT}"

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
                cwd=REPO_ROOT,
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
                rows = {row["merchant"]: row for row in csv.DictReader(fh)}
            self.assertNotIn("PREVIOUS BALANCE", rows)
            self.assertEqual(rows["24/7 FITNESS HONG KONG HK"]["amount_hkd"], "-498.00")
            self.assertEqual(rows["DCC FEE-NON-HK MERCHANT"]["amount_hkd"], "-0.08")

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
        example_profiles = sorted(
            path.name
            for path in examples_dir.glob("*.json")
            if path.name != "starter_csv.json"
        )
        self.assertEqual(packaged, example_profiles)
        self.assertEqual(
            json.loads((examples_dir / "starter_csv.json").read_text(encoding="utf-8")),
            _starter_csv_profile(),
        )
        self.assertIn("hsbc_one_pdf.json", packaged)
        self.assertIn("mox_credit_card_pdf.json", packaged)
        for name in packaged:
            self.assertEqual(
                json.loads((packaged_dir / name).read_text(encoding="utf-8")),
                json.loads((examples_dir / name).read_text(encoding="utf-8")),
                f"{name} differs between honeymoney/data/profiles and examples/profiles",
            )

    def test_checked_in_example_outputs_match_current_pipeline(self) -> None:
        examples_dir = REPO_ROOT / "examples"
        expected_dir = examples_dir / "expected-output"
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "--input",
                    str(examples_dir / "input"),
                    "--output",
                    str(output_dir / "categorized.csv"),
                    "--config",
                    str(examples_dir / "config.json"),
                    "--no-interactive",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("categorized.csv", "review_needed.csv"):
                self.assertEqual(
                    (output_dir / name).read_text(encoding="utf-8").splitlines(),
                    (expected_dir / name).read_text(encoding="utf-8").splitlines(),
                )
            self.assertEqual(
                (output_dir / ".honeymoney-identity-manifest.json").read_text(
                    encoding="utf-8"
                ),
                (expected_dir / ".honeymoney-identity-manifest.json").read_text(
                    encoding="utf-8"
                ),
            )

            actual_report = json.loads(
                (output_dir / "import_report.json").read_text(encoding="utf-8")
            )
            expected_report = json.loads(
                (expected_dir / "import_report.json").read_text(encoding="utf-8")
            )
            actual_report["output"] = expected_report["output"]
            self.assertEqual(actual_report, expected_report)

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
            for external_reference in [
                'src="http',
                "src='http",
                'href="http',
                "url(http",
                "@import",
            ]:
                self.assertNotIn(external_reference, html)

    def test_report_command_defaults_to_current_calendar_month(self) -> None:
        class FixedDate(date):
            @classmethod
            def today(cls) -> date:
                return cls(2026, 7, 7)

        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "transactions.csv"
            self._write_statement(
                statement,
                [
                    "2026-07-04,JULY SHOP,-10.00,HKD",
                    "2026-06-30,JUNE SHOP,-20.00,HKD",
                ],
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            report_path = root / "output" / "report.html"

            with (
                patch("honeymoney.cli.date", FixedDate),
                redirect_stdout(io.StringIO()),
            ):
                result = _report_command(
                    [
                        "--config",
                        str(root / "config.json"),
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
        class FakeTransport:
            def request(self, request: OllamaHttpRequest) -> bytes:
                assert request.body is not None
                prompt = json.loads(json.loads(request.body)["prompt"])
                return json.dumps(
                    {
                        "response": json.dumps(
                            [
                                {
                                    "id": transaction["id"],
                                    "category": "Groceries",
                                    "confidence": 0.9,
                                    "reason": "Supermarket merchant",
                                }
                                for transaction in prompt["transactions"]
                            ]
                        )
                    }
                ).encode()

        def fake_apply(transactions, config, progress=None):
            return apply_ollama_fallback(
                transactions,
                config,
                progress=progress,
                transport=FakeTransport(),
            )

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
                "url": "http://localhost:11434/api/generate",
                "model": "test",
                "batch_size": 20,
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            status_output = io.StringIO()
            stdout = io.StringIO()
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    patch.object(
                        cli, "_status", _StatusLine(status_output, enabled=True)
                    ),
                    patch.object(cli, "apply_ollama_fallback", side_effect=fake_apply),
                    redirect_stdout(stdout),
                ):
                    returncode = cli.main(["import", str(statement)])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(returncode, 0)
            text = status_output.getvalue() + stdout.getvalue()
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


class CategoryMenuTest(unittest.TestCase):
    def _render(self, categories: list[str], columns: int) -> list[str]:
        import contextlib
        import io

        from honeymoney.cli import _print_category_menu

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _print_category_menu(categories, columns=columns)
        return buffer.getvalue().splitlines()

    def _leading_numbers(self, lines: list[str]) -> list[int]:
        return [int(line.strip().split(".", 1)[0]) for line in lines]

    def test_numbers_increment_down_each_column(self) -> None:
        lines = self._render(["A", "B", "C", "D", "E"], columns=2)

        # First column, read top to bottom, increments 1, 2, 3.
        self.assertEqual(self._leading_numbers(lines), [1, 2, 3])
        # Column-major: item 1 and item 4 sit on the same first row.
        self.assertIn(" 1. A", lines[0])
        self.assertIn(" 4. D", lines[0])
        self.assertIn(" 5. E", lines[1])
        self.assertIn(" 3. C", lines[2])

    def test_full_taxonomy_columns_are_sequential(self) -> None:
        categories = sorted(ALLOWED_CATEGORIES - {"Unknown"})
        lines = self._render(categories, columns=3)

        row_count = (len(categories) + 2) // 3
        self.assertEqual(len(lines), row_count)
        self.assertEqual(self._leading_numbers(lines), list(range(1, row_count + 1)))

    def test_empty_categories_print_nothing(self) -> None:
        self.assertEqual(self._render([], columns=3), [])


if __name__ == "__main__":
    unittest.main()
