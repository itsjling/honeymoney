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
from honeymoney.corrections import CORRECTION_COLUMNS, ledger_output_documents
from honeymoney.csv_artifacts import csv_document
from honeymoney.duplicates import DUPLICATE_MATCH_TYPE
from honeymoney.identity import manifest_document
from honeymoney.identity_state import (
    load_configured_identity_state,
    load_identity_state,
)
from honeymoney.ollama import OllamaHttpRequest, apply_ollama_fallback
from honeymoney.persistence import GenerationConflictError
from honeymoney.schema import (
    ALLOWED_CATEGORIES,
    PREVIOUS_CATEGORIZED_COLUMNS,
    PREVIOUS_SOURCE_OCCURRENCE_COLUMNS,
    SOURCE_OCCURRENCE_COLUMNS,
)

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

    def _seed_balance_contract_workspace(
        self, tmp: str, *, repeated: bool
    ) -> tuple[Path, Path, Path, Path]:
        root = self._setup_workspace(tmp)
        fake_modules = root / "fake-balance-modules"
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
    def __init__(self, source_path):
        self.source_path = source_path
        self.pages = []

    def __enter__(self):
        data = json.loads(builtins.open(self.source_path, encoding="utf-8").read())
        self.pages = [Page(page) for page in data["pages"]]
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def open(source_path):
    return Pdf(source_path)
""",
            encoding="utf-8",
        )
        statement = root / "contract-statement.pdf"
        transaction_lines = (
            [
                ["01 Apr 02 Apr SYNTHETIC REPEAT +5.00"],
                ["01 Apr 02 Apr SYNTHETIC REPEAT +5.00"],
            ]
            if repeated
            else [
                ["01 Apr 02 Apr SYNTHETIC ONE +5.00"],
                ["02 Apr 03 Apr SYNTHETIC TWO +10.00"],
            ]
        )
        closing_balance = "110.00" if repeated else "115.00"
        statement.write_text(
            json.dumps(
                {
                    "pages": [
                        [
                            ["01 Apr 01 Apr OPENING BALANCE +100.00"],
                            *transaction_lines,
                            [f"30 Apr 30 Apr CLOSING BALANCE +{closing_balance}"],
                        ]
                    ]
                }
            ),
            encoding="utf-8",
        )
        profile_path = root / "profiles" / "balance_contract_pdf.json"
        profile_path.write_text(
            json.dumps(
                {
                    "id": "balance_contract_pdf",
                    "account_id": "balance_contract",
                    "account": "Balance Contract",
                    "account_type": "bank",
                    "institution": "Synthetic",
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
                            r"(?P<amount>[+-]?\d[\d,]*\.\d{2})$"
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
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["profiles"].append(str(profile_path))
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (root / "profile_mappings.json").write_text(
            json.dumps(
                {
                    "filename_patterns": [
                        {
                            "pattern": statement.name,
                            "profile": "balance_contract_pdf",
                        }
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
        return root, fake_modules, statement, profile_path

    def _add_balance_contract_mapping(self, profile_path: Path) -> None:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["pdf"]["balance_mappings"] = [
            {
                "account_id": "balance_contract",
                "currency": "HKD",
                "opening_regex": (
                    r"^\d{1,2} [A-Za-z]{3} \d{1,2} [A-Za-z]{3} "
                    r"OPENING BALANCE (?P<balance>[+-]?\d[\d,]*\.\d{2})$"
                ),
                "closing_regex": (
                    r"^\d{1,2} [A-Za-z]{3} \d{1,2} [A-Za-z]{3} "
                    r"CLOSING BALANCE (?P<balance>[+-]?\d[\d,]*\.\d{2})$"
                ),
            }
        ]
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

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
                "output/.honeymoney-source-occurrences.csv",
                "output/.honeymoney-overlap-manifest.json",
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
                "output/.honeymoney-source-occurrences.csv",
                "output/.honeymoney-overlap-manifest.json",
            ],
        )

    def test_opt_in_local_memory_uses_two_reviewed_v2_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["categorization_memory"]["enabled"] = True
            config_path.write_text(json.dumps(config), encoding="utf-8")

            for index, merchant in enumerate(("Park-N-Shop", "PARK N SHOP"), 1):
                statement = root / f"reviewed-{index}.csv"
                self._write_statement(
                    statement, [f"2026-07-0{index},{merchant},-10.00,HKD"]
                )
                result = self._run_cli(
                    ["import", str(statement)],
                    cwd=root,
                    input_text=f"{_category_number('Groceries')}\n",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            target = root / "target.csv"
            self._write_statement(target, ["2026-07-03,park.n.shop,-10.00,HKD"])
            result = self._run_cli(
                ["import", str(target), "--no-interactive"], cwd=root
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                row = next(
                    item
                    for item in csv.DictReader(handle)
                    if item["date"] == "2026-07-03"
                )
            self.assertEqual(row["category"], "Groceries")
            self.assertEqual(row["confidence"], "0.90")
            self.assertEqual(row["needs_review"], "false")
            self.assertIn("local_memory_categorized", row["flags"])

            reset = self._run_cli(
                ["import", str(root / "reviewed-2.csv"), "--reset", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            after_reset = root / "after-reset.csv"
            self._write_statement(after_reset, ["2026-07-04,park n shop,-10.00,HKD"])
            result = self._run_cli(
                ["import", str(after_reset), "--no-interactive"], cwd=root
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                after_reset_row = next(
                    item
                    for item in csv.DictReader(handle)
                    if item["date"] == "2026-07-04"
                )
            self.assertEqual(after_reset_row["category"], "Unknown")
            self.assertNotIn("local_memory_categorized", after_reset_row["flags"])

    def test_first_import_failure_does_not_publish_a_partial_generation(self) -> None:
        faults = [
            "file-fsync:review_needed.csv",
            "file-fsync:import_report.json",
            "file-fsync:.honeymoney-source-occurrences.csv",
            "file-fsync:.honeymoney-overlap-manifest.json",
            "file-fsync:categorized.csv",
            "replace-before:review_needed.csv",
            "replace-before:import_report.json",
            "replace-before:.honeymoney-source-occurrences.csv",
            "replace-before:.honeymoney-overlap-manifest.json",
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
                    ".honeymoney-identity-manifest.json",
                    ".honeymoney-source-occurrences.csv",
                    ".honeymoney-overlap-manifest.json",
                ):
                    self.assertFalse((root / "output" / name).exists())

    def test_import_does_not_overwrite_a_generation_changed_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "first.csv"
            self._write_statement(first, ["2026-05-04,SYNTHETIC FIRST,-12.00,HKD"])
            imported = self._run_cli(
                ["import", str(first), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            second = root / "second.csv"
            self._write_statement(second, ["2026-05-05,SYNTHETIC SECOND,-8.00,HKD"])
            ledger_path = root / "output" / "categorized.csv"
            real_persist = cli.persist_generation
            concurrent_ledger: bytes | None = None

            def persist_after_concurrent_change(
                authoritative_path: Path,
                files: dict[Path, str],
                **kwargs: object,
            ) -> None:
                nonlocal concurrent_ledger
                ledger_path.write_bytes(ledger_path.read_bytes() + b"\n")
                concurrent_ledger = ledger_path.read_bytes()
                real_persist(authoritative_path, files, **kwargs)

            prior_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    patch.object(
                        cli,
                        "persist_generation",
                        side_effect=persist_after_concurrent_change,
                    ),
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(GenerationConflictError),
                ):
                    cli._import_command(
                        [
                            str(second),
                            "--config",
                            str(root / "config.json"),
                            "--no-interactive",
                            "--json",
                        ]
                    )
            finally:
                os.chdir(prior_cwd)

            self.assertIsNotNone(concurrent_ledger)
            self.assertEqual(ledger_path.read_bytes(), concurrent_ledger)

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
            "file-fsync:.honeymoney-source-occurrences.csv",
            "file-fsync:.honeymoney-overlap-manifest.json",
            "file-fsync:categorized.csv",
            "replace-before:review_needed.csv",
            "replace-before:import_report.json",
            "replace-before:.honeymoney-source-occurrences.csv",
            "replace-before:.honeymoney-overlap-manifest.json",
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

    def _write_previous_review_schema(
        self,
        root: Path,
        canonical_rows: list[dict[str, str]],
        source_rows: list[dict[str, str]],
    ) -> None:
        output = root / "output"
        (output / "categorized.csv").write_text(
            csv_document(PREVIOUS_CATEGORIZED_COLUMNS, canonical_rows),
            encoding="utf-8",
        )
        (output / ".honeymoney-source-occurrences.csv").write_text(
            csv_document(PREVIOUS_SOURCE_OCCURRENCE_COLUMNS, source_rows),
            encoding="utf-8",
        )

    def test_synthetic_pdf_import_persists_balances_identity_and_report(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "mox-synthetic.pdf"
            fixture = (
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "import_profiles"
                / "mox_bank_pdf"
                / "accepted_statement"
                / "input.pdf"
            )
            statement.write_bytes(fixture.read_bytes())
            (root / "profile_mappings.json").write_text(
                json.dumps(
                    {
                        "filename_patterns": [
                            {
                                "pattern": statement.name,
                                "profile": "mox_bank_pdf",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = self._ledger_rows(root)
            self.assertEqual(len(rows), 5)
            self.assertTrue(all(not row["source_id"] for row in rows))
            source_rows = load_identity_state(
                root / "output" / "categorized.csv"
            ).source_rows
            self.assertTrue(all(row["source_id"] for row in source_rows))
            self.assertEqual(source_rows[0]["statement_opening_balance"], "10000.00")
            self.assertEqual(source_rows[-1]["statement_closing_balance"], "45191.75")
            report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            balance = report["reconciliation"]["balance_reconciliation"][
                "mox_bank_main"
            ]
            self.assertEqual(balance["status"], "reconciled")
            self.assertEqual(balance["result"], "matched")
            self.assertEqual(balance["statements"][0]["status"], "reconciled")
            self.assertEqual(balance["statements"][0]["result"], "matched")

    def test_balance_mapping_contract_upgrade_preserves_unique_transaction_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, profile_path = (
                self._seed_balance_contract_workspace(tmp, repeated=False)
            )
            original_rows = self._ledger_rows(root)
            original_ids = {
                row["merchant"]: row["transaction_id"] for row in original_rows
            }
            manifest_path = root / "output" / ".honeymoney-identity-manifest.json"
            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            original_contract = original_manifest["sources"][0]["extractor_contract_id"]
            original_revision = original_manifest["sources"][0]["source_revision"]
            self._add_balance_contract_mapping(profile_path)

            replacement = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replaced_rows = self._ledger_rows(root)
            self.assertEqual(
                {row["merchant"]: row["transaction_id"] for row in replaced_rows},
                original_ids,
            )
            replaced_source_rows = load_identity_state(
                root / "output" / "categorized.csv"
            ).source_rows
            self.assertEqual(
                replaced_source_rows[0]["statement_opening_balance"], "100.00"
            )
            self.assertEqual(
                replaced_source_rows[-1]["statement_closing_balance"], "115.00"
            )
            replaced_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotEqual(
                replaced_manifest["sources"][0]["extractor_contract_id"],
                original_contract,
            )
            self.assertEqual(
                replaced_manifest["sources"][0]["source_revision"],
                original_revision,
            )

    def test_repeated_balance_contract_upgrade_reallocates_without_pairing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, profile_path = (
                self._seed_balance_contract_workspace(tmp, repeated=True)
            )
            before = load_identity_state(root / "output" / "categorized.csv")
            old_ids = {row["transaction_id"] for row in before.rows}
            self._add_balance_contract_mapping(profile_path)

            replacement = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replaced = load_identity_state(root / "output" / "categorized.csv")
            self.assertEqual(len(replaced.rows), 2)
            self.assertEqual({row["transaction_id"] for row in replaced.rows}, old_ids)
            self.assertEqual(
                replaced.source_rows[0]["statement_opening_balance"], "100.00"
            )
            self.assertEqual(
                replaced.source_rows[-1]["statement_closing_balance"], "110.00"
            )

    def test_identity_v2_replace_migrates_reviewed_repeated_rows_in_place(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, profile_path = (
                self._seed_balance_contract_workspace(tmp, repeated=True)
            )
            categorized_path = root / "output" / "categorized.csv"
            state = load_identity_state(categorized_path)
            source_rows = [dict(row) for row in state.source_rows]
            for row in source_rows:
                row.update(
                    {
                        "category": "Dining",
                        "flow_type": "expense",
                        "flow_source": "correction",
                        "confidence": "1.00",
                        "needs_review": "false",
                        "reason": "Synthetic legacy review",
                    }
                )
            categorized_path.write_text(
                csv_document(SOURCE_OCCURRENCE_COLUMNS, source_rows),
                encoding="utf-8",
            )
            (root / "output" / ".honeymoney-source-occurrences.csv").unlink()
            (root / "output" / ".honeymoney-overlap-manifest.json").unlink()

            manifest_path = root / "output" / ".honeymoney-identity-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for record in manifest["sources"][0]["records"]:
                locator = record["current_locator"]
                locator["components"][-1] += 10
            manifest_path.write_text(
                manifest_document(manifest),
                encoding="utf-8",
            )
            source_ids = [row["transaction_id"] for row in source_rows]
            (root / "corrections.csv").write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": transaction_id,
                            "category": "Dining",
                            "flow_type": "expense",
                            "confidence": "1.00",
                            "reason": "Synthetic legacy review",
                            "needs_review": "false",
                        }
                        for transaction_id in source_ids
                    ],
                ),
                encoding="utf-8",
            )
            self._add_balance_contract_mapping(profile_path)
            upgraded_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            upgraded_profile["account_id"] = "balance_contract_v2"
            upgraded_profile["account"] = "Balance Contract V2"
            upgraded_profile["pdf"]["balance_mappings"][0]["account_id"] = (
                "balance_contract_v2"
            )
            profile_path.write_text(json.dumps(upgraded_profile), encoding="utf-8")

            replacement = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replacement_data = json.loads(replacement.stdout)["data"]
            self.assertEqual(replacement_data["source_occurrence_count"], 2)
            self.assertEqual(replacement_data["canonical_occurrence_count"], 2)
            migrated = load_identity_state(categorized_path)
            self.assertFalse(migrated.canonical_migration_required)
            canonical_ids = [row["transaction_id"] for row in migrated.rows]
            self.assertTrue(all(row["category"] == "Dining" for row in migrated.rows))
            self.assertTrue(
                all(row["needs_review"] == "false" for row in migrated.rows)
            )
            self.assertTrue(
                all(
                    not row["source_id"] and not row["source_file"]
                    for row in migrated.rows
                )
            )
            self.assertTrue(set(source_ids).isdisjoint(canonical_ids))
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as corrections_file:
                correction_ids = {
                    row["transaction_id"] for row in csv.DictReader(corrections_file)
                }
            self.assertEqual(set(canonical_ids), correction_ids)
            first_generation = self._artifact_bytes(
                root,
                [
                    "output/categorized.csv",
                    "output/review_needed.csv",
                    "output/.honeymoney-identity-manifest.json",
                    "output/.honeymoney-source-occurrences.csv",
                    "output/.honeymoney-overlap-manifest.json",
                    "corrections.csv",
                ],
            )

            repeated = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                self._artifact_bytes(
                    root,
                    [
                        "output/categorized.csv",
                        "output/review_needed.csv",
                        "output/.honeymoney-identity-manifest.json",
                        "output/.honeymoney-source-occurrences.csv",
                        "output/.honeymoney-overlap-manifest.json",
                        "corrections.csv",
                    ],
                ),
                first_generation,
            )
            html_path = root / "output" / "migration.html"
            report = self._run_cli(
                [
                    "report",
                    "2026-04",
                    "--output",
                    str(html_path),
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertEqual(json.loads(report.stdout)["data"]["transaction_count"], 2)
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("Dining", html)
            for transaction_id in canonical_ids:
                self.assertIn(transaction_id, html)

    def test_replace_migrates_pending_model_review_before_reprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "legacy-review.csv"
            self._write_statement(
                statement,
                [
                    "2026-07-01,MYSTERY SUGGESTION,-10.00,HKD",
                    "2026-07-02,MANUAL FLOW,-20.00,HKD",
                ],
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            state = load_identity_state(root / "output" / "categorized.csv")
            manual_id = next(
                row["transaction_id"]
                for row in state.rows
                if row["merchant"] == "MANUAL FLOW"
            )
            reviewed = self._run_cli(
                [
                    "review",
                    "--transaction",
                    manual_id,
                    "--as",
                    "internal-transfer",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)

            state = load_identity_state(root / "output" / "categorized.csv")
            canonical_rows = [dict(row) for row in state.rows]
            source_rows = [dict(row) for row in state.source_evidence_rows]
            pending = next(
                row for row in canonical_rows if row["merchant"] == "MYSTERY SUGGESTION"
            )
            pending.update(
                {
                    "category": "Dining",
                    "flow_type": "expense",
                    "confidence": "0.72",
                    "needs_review": "true",
                    "reason": "Local model suggestion",
                    "flags": "ollama_categorized",
                }
            )
            self._write_previous_review_schema(root, canonical_rows, source_rows)

            replaced = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            data = json.loads(replaced.stdout)["data"]
            self.assertEqual(
                data["migration"],
                {
                    "legacy_review_state_applied": True,
                    "legacy_review_state_detected": True,
                },
            )
            rows = {row["merchant"]: row for row in self._ledger_rows(root)}
            self.assertEqual(rows["MYSTERY SUGGESTION"]["category"], "Dining")
            self.assertEqual(
                rows["MYSTERY SUGGESTION"]["review_reasons"],
                "category_suggestion",
            )
            self.assertEqual(rows["MYSTERY SUGGESTION"]["needs_review"], "true")
            self.assertEqual(rows["MANUAL FLOW"]["flow_type"], "internal_transfer")
            self.assertEqual(rows["MANUAL FLOW"]["needs_review"], "false")
            self.assertEqual(rows["MANUAL FLOW"]["review_reasons"], "")

            financial_paths = [
                "output/categorized.csv",
                "output/review_needed.csv",
                "output/.honeymoney-identity-manifest.json",
                "output/.honeymoney-source-occurrences.csv",
                "output/.honeymoney-overlap-manifest.json",
                "corrections.csv",
            ]
            first_generation = self._artifact_bytes(root, financial_paths)
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
            self.assertEqual(
                self._artifact_bytes(root, financial_paths),
                first_generation,
            )

            current = load_identity_state(root / "output" / "categorized.csv")
            self._write_previous_review_schema(
                root,
                [dict(row) for row in current.rows],
                [dict(row) for row in current.source_evidence_rows],
            )
            reset = self._run_cli(
                ["import", str(statement), "--reset", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertIn(
                "Migration: legacy review decisions were made explicit",
                reset.stdout,
            )
            reset_rows = {row["merchant"]: row for row in self._ledger_rows(root)}
            self.assertNotEqual(
                reset_rows["MANUAL FLOW"]["flow_type"],
                "internal_transfer",
            )
            with (root / "corrections.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                correction_ids = {
                    row["transaction_id"] for row in csv.DictReader(handle)
                }
            self.assertNotIn(manual_id, correction_ids)

    def test_replace_projects_pending_legacy_suggestion_through_profile_rekey(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, profile_path = (
                self._seed_balance_contract_workspace(tmp, repeated=False)
            )
            categorized_path = root / "output" / "categorized.csv"
            state = load_identity_state(categorized_path)
            canonical_rows = [dict(row) for row in state.rows]
            source_rows = [dict(row) for row in state.source_evidence_rows]
            pending = next(
                row for row in canonical_rows if row["merchant"] == "SYNTHETIC ONE"
            )
            prior_transaction_id = pending["transaction_id"]
            pending.update(
                {
                    "category": "Dining",
                    "flow_type": "expense",
                    "flow_source": "ollama",
                    "confidence": "0.72",
                    "needs_review": "true",
                    "reason": "Local model suggestion",
                    "flags": "ollama_categorized",
                }
            )
            self._write_previous_review_schema(root, canonical_rows, source_rows)
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["account_id"] = "balance_contract_v2"
            profile["account"] = "Balance Contract V2"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            replaced = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            rows = {row["merchant"]: row for row in self._ledger_rows(root)}
            migrated = rows["SYNTHETIC ONE"]
            self.assertNotEqual(migrated["transaction_id"], prior_transaction_id)
            self.assertEqual(migrated["category"], "Dining")
            self.assertIn(
                "category_suggestion",
                migrated["review_reasons"].split(";"),
            )
            self.assertEqual(migrated["needs_review"], "true")
            with (root / "corrections.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_failed_replace_does_not_publish_a_detected_review_migration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "legacy-review.csv"
            self._write_statement(
                statement,
                ["2026-07-01,MYSTERY SUGGESTION,-10.00,HKD"],
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            state = load_identity_state(root / "output" / "categorized.csv")
            self._write_previous_review_schema(
                root,
                [dict(row) for row in state.rows],
                [dict(row) for row in state.source_evidence_rows],
            )
            financial_paths = [
                "output/categorized.csv",
                "output/review_needed.csv",
                "output/.honeymoney-identity-manifest.json",
                "output/.honeymoney-source-occurrences.csv",
                "output/.honeymoney-overlap-manifest.json",
                "corrections.csv",
            ]
            before = self._artifact_bytes(root, financial_paths)
            statement.write_text(
                "Not,A,Supported,Statement\n1,2,3,4\n",
                encoding="utf-8",
            )

            failed = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(failed.returncode, 2, failed.stderr)
            self.assertEqual(json.loads(failed.stdout)["status"], "error")
            self.assertEqual(self._artifact_bytes(root, financial_paths), before)

    def test_failed_replace_reports_the_preserved_legacy_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, _ = self._seed_pdf_replacement_workspace(tmp)
            categorized_path = root / "output" / "categorized.csv"
            state = load_identity_state(categorized_path)
            canonical_rows = [dict(row) for row in state.rows]
            source_rows = [dict(row) for row in state.source_evidence_rows]
            pending = canonical_rows[0]
            pending.update(
                {
                    "category": "Dining",
                    "flow_type": "expense",
                    "flow_source": "ollama",
                    "confidence": "0.72",
                    "needs_review": "true",
                    "reason": "Local model suggestion",
                    "flags": "ollama_categorized",
                }
            )
            pending_id = pending["transaction_id"]
            self._write_previous_review_schema(root, canonical_rows, source_rows)
            financial_paths = [
                "output/categorized.csv",
                "output/review_needed.csv",
                "output/.honeymoney-identity-manifest.json",
                "output/.honeymoney-source-occurrences.csv",
                "output/.honeymoney-overlap-manifest.json",
                "corrections.csv",
            ]
            before = self._artifact_bytes(root, financial_paths)
            (fake_modules / "pdfplumber.py").write_text(
                "def open(path):\n"
                "    raise RuntimeError('synthetic malformed statement')\n",
                encoding="utf-8",
            )

            failed = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(failed.returncode, 0, failed.stderr)
            report = json.loads(failed.stdout)["data"]
            self.assertEqual(report["status"], "partial_success")
            self.assertEqual(
                report["migration"],
                {
                    "legacy_review_state_applied": False,
                    "legacy_review_state_detected": True,
                },
            )
            self.assertTrue(
                all(
                    count == 0
                    for count in report["ledger"]["review_reason_counts"].values()
                )
            )
            diagnostic = report["transaction_diagnostics"][pending_id]
            self.assertEqual(diagnostic["category"], "Dining")
            self.assertEqual(diagnostic["review_reasons"], [])
            self.assertEqual(
                report["transaction_flags"][pending_id],
                ["ollama_categorized"],
            )
            self.assertEqual(self._artifact_bytes(root, financial_paths), before)

    def test_current_replace_rekeys_repeated_rows_after_profile_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, profile_path = (
                self._seed_balance_contract_workspace(tmp, repeated=True)
            )
            categorized_path = root / "output" / "categorized.csv"
            original = load_identity_state(categorized_path)
            original_ids = [row["transaction_id"] for row in original.rows]
            (root / "corrections.csv").write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": transaction_id,
                            "category": "Dining",
                            "flow_type": "expense",
                            "confidence": "1.00",
                            "reason": "Synthetic repeated review",
                            "needs_review": "false",
                        }
                        for transaction_id in original_ids
                    ],
                ),
                encoding="utf-8",
            )
            upgraded_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            upgraded_profile["account_id"] = "balance_contract_v2"
            upgraded_profile["account"] = "Balance Contract V2"
            profile_path.write_text(json.dumps(upgraded_profile), encoding="utf-8")

            replacement = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replaced = load_identity_state(categorized_path)
            replacement_ids = [row["transaction_id"] for row in replaced.rows]
            self.assertEqual(len(replacement_ids), 2)
            self.assertTrue(set(original_ids).isdisjoint(replacement_ids))
            self.assertTrue(all(row["category"] == "Dining" for row in replaced.rows))
            self.assertTrue(
                all(row["needs_review"] == "false" for row in replaced.rows)
            )
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                correction_ids = {
                    row["transaction_id"] for row in csv.DictReader(handle)
                }
            self.assertEqual(correction_ids, set(replacement_ids))
            first_generation = self._artifact_bytes(
                root,
                [
                    "output/categorized.csv",
                    "output/review_needed.csv",
                    "output/.honeymoney-identity-manifest.json",
                    "output/.honeymoney-source-occurrences.csv",
                    "output/.honeymoney-overlap-manifest.json",
                    "corrections.csv",
                ],
            )

            repeated = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                self._artifact_bytes(root, list(first_generation)),
                first_generation,
            )

    def test_current_replace_keeps_conflicting_repeated_history_reviewable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, statement, profile_path = (
                self._seed_balance_contract_workspace(tmp, repeated=True)
            )
            categorized_path = root / "output" / "categorized.csv"
            original = load_identity_state(categorized_path)
            original_ids = [row["transaction_id"] for row in original.rows]
            (root / "corrections.csv").write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": original_ids[0],
                            "category": "Dining",
                            "needs_review": "false",
                        },
                        {
                            "transaction_id": original_ids[1],
                            "category": "Shopping",
                            "needs_review": "false",
                        },
                    ],
                ),
                encoding="utf-8",
            )
            upgraded_profile = json.loads(profile_path.read_text(encoding="utf-8"))
            upgraded_profile["account_id"] = "balance_contract_v2"
            upgraded_profile["account"] = "Balance Contract V2"
            profile_path.write_text(json.dumps(upgraded_profile), encoding="utf-8")

            replacement = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(replacement.returncode, 0, replacement.stderr)
            replaced = load_identity_state(categorized_path)
            self.assertTrue(
                all(
                    "overlap_history_ambiguous" in row["flags"].split(";")
                    and row["needs_review"] == "true"
                    for row in replaced.rows
                )
            )
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                correction_ids = {
                    row["transaction_id"] for row in csv.DictReader(handle)
                }
            self.assertTrue(set(original_ids).isdisjoint(correction_ids))

    def test_import_loads_identity_state_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "single-identity-load.csv"
            self._write_statement(statement, ["2026-07-01,SINGLE LOAD,-10.00,HKD"])
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch(
                    "honeymoney.cli.load_configured_identity_state",
                    wraps=load_configured_identity_state,
                ) as load_patch:
                    result = cli._run_pipeline(
                        ["--input", str(statement), "--no-interactive"]
                    )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result, 0)
            self.assertEqual(load_patch.call_count, 1)

    def test_cross_import_duplicate_preserves_validated_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            history = root / "history.csv"
            self._write_statement(
                history,
                [
                    "2026-05-04,REVIEWED REPEAT,-12.00,HKD",
                    "2026-05-01,HISTORY CONTEXT,-1.00,HKD",
                ],
            )
            first = self._run_cli(
                ["import", str(history), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            categorized_path = root / "output" / "categorized.csv"
            state = load_identity_state(categorized_path)
            historical_id = next(
                row["transaction_id"]
                for row in state.rows
                if row["merchant"] == "REVIEWED REPEAT"
            )
            corrected = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": historical_id,
                            "category": "Dining",
                            "needs_review": False,
                            "reason": "Synthetic historical review",
                        }
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)

            candidate = root / "candidate.csv"
            self._write_statement(
                candidate,
                [
                    "2026-05-04,REVIEWED REPEAT,-12.00,HKD",
                    "2026-05-02,CANDIDATE CONTEXT,-2.00,HKD",
                ],
            )
            imported = self._run_cli(
                ["import", str(candidate), "--no-interactive", "--json"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            report = json.loads(imported.stdout)["data"]
            self.assertEqual(report["duplicate_count"], 2)
            self.assertEqual(report["duplicate_group_count"], 1)
            state = load_identity_state(categorized_path)
            canonical = next(
                row for row in state.rows if row["merchant"] == "REVIEWED REPEAT"
            )
            self.assertEqual(canonical["transaction_id"], historical_id)
            self.assertEqual(canonical["category"], "Dining")
            self.assertEqual(canonical["provenance_status"], "exact_one_to_one")
            self.assertEqual(canonical["source_occurrence_count"], "2")
            self.assertEqual(
                sum(row["merchant"] == "REVIEWED REPEAT" for row in state.source_rows),
                2,
            )
            expected_ids = sorted(
                row["transaction_id"]
                for row in state.source_rows
                if row["merchant"] == "REVIEWED REPEAT"
            )
            self.assertEqual(
                report["duplicate_candidates"]["groups"],
                [
                    {
                        "match_type": DUPLICATE_MATCH_TYPE,
                        "occurrence_ids": expected_ids,
                    }
                ],
            )
            self.assertEqual(canonical["needs_review"], "false")
            self.assertNotIn("duplicate_suspected", canonical["flags"].split(";"))
            self.assertNotIn("duplicate_review_promoted", canonical["flags"].split(";"))

            with (root / "output" / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                review_ids = {
                    row["transaction_id"]
                    for row in csv.DictReader(fh)
                    if "duplicate_suspected" in row["flags"]
                }
            self.assertEqual(review_ids, set())

            pending = self._run_cli(["pending", "2026-05", "--json"], cwd=root)
            self.assertEqual(pending.returncode, 0, pending.stderr)
            pending_data = json.loads(pending.stdout)["data"]
            self.assertEqual(pending_data["duplicate_count"], 0)
            self.assertNotIn(
                canonical["transaction_id"],
                {row["transaction_id"] for row in pending_data["transactions"]},
            )

            status = self._run_cli(["status", "2026-05", "--json"], cwd=root)
            self.assertEqual(status.returncode, 0, status.stderr)
            status_data = json.loads(status.stdout)["data"]
            self.assertEqual(status_data["records_processed"], 3)
            self.assertEqual(status_data["source_occurrence_count"], 4)
            self.assertEqual(status_data["duplicate_count"], 2)
            self.assertEqual(status_data["duplicate_group_count"], 1)
            self.assertEqual(
                status_data["duplicate_candidates"], report["duplicate_candidates"]
            )
            reconcile = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(reconcile.returncode, 0, reconcile.stderr)
            self.assertEqual(
                json.loads(reconcile.stdout)["data"]["duplicate_candidates"],
                report["duplicate_candidates"],
            )
            state = load_identity_state(categorized_path)
            reconciled_historical = next(
                row for row in state.rows if row["transaction_id"] == historical_id
            )
            self.assertEqual(reconciled_historical["needs_review"], "false")

            html_path = root / "output" / "duplicates.html"
            html_report = self._run_cli(
                [
                    "report",
                    "2026-05",
                    "--output",
                    str(html_path),
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(html_report.returncode, 0, html_report.stderr)
            html_data = json.loads(html_report.stdout)["data"]
            self.assertEqual(html_data["transaction_count"], 3)
            self.assertEqual(
                html_data["duplicate_candidates"], report["duplicate_candidates"]
            )
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("exact_one_to_one", html)
            self.assertIn(canonical["transaction_id"], html)

            canonical_diagnostic = report["transaction_diagnostics"][
                canonical["transaction_id"]
            ]
            self.assertEqual(canonical_diagnostic["category"], "Dining")
            self.assertFalse(canonical_diagnostic["needs_review"])

    def test_failed_import_reports_retained_duplicates_without_rewriting_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, fake_modules, malformed_source, _ = (
                self._seed_pdf_replacement_workspace(tmp)
            )
            for filename in ("duplicate-a.csv", "duplicate-b.csv"):
                statement = root / filename
                self._write_statement(
                    statement,
                    ["2026-05-04,SYNTHETIC PARENT DUPLICATE,-12.00,HKD"],
                )
                imported = self._run_cli(
                    ["import", str(statement), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)

            categorized_path = root / "output" / "categorized.csv"
            rows = self._ledger_rows(root)
            duplicate_rows = [
                row for row in rows if row["merchant"] == "SYNTHETIC PARENT DUPLICATE"
            ]
            self.assertEqual(len(duplicate_rows), 1)
            [canonical_duplicate] = duplicate_rows
            self.assertEqual(canonical_duplicate["source_occurrence_count"], "2")
            canonical_duplicate["flags"] = "uncategorized;duplicate_suspected"
            canonical_duplicate["reason"] = (
                "No matching category rule; Possible duplicate transaction"
            )
            for path, content in ledger_output_documents(
                categorized_path, rows
            ).items():
                path.write_text(content, encoding="utf-8")

            protected_before = self._artifact_bytes(
                root,
                [
                    "output/categorized.csv",
                    "output/review_needed.csv",
                    "output/.honeymoney-identity-manifest.json",
                    "output/.honeymoney-source-occurrences.csv",
                    "output/.honeymoney-overlap-manifest.json",
                ],
            )
            (fake_modules / "pdfplumber.py").write_text(
                "def open(path):\n"
                "    raise RuntimeError('synthetic malformed statement')\n",
                encoding="utf-8",
            )

            failed = self._run_cli(
                [
                    "import",
                    str(malformed_source),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
                extra_pythonpath=fake_modules,
            )

            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertEqual(
                self._artifact_bytes(root, list(protected_before)),
                protected_before,
            )
            report = json.loads(failed.stdout)["data"]
            persisted_report = json.loads(
                (root / "output" / "import_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted_report, report)
            self.assertEqual(report["status"], "partial_success")
            self.assertEqual(report["files"][0]["status"], "failed")
            self.assertEqual(report["files"][0]["ledger_action"], "preserved")
            self.assertEqual(report["duplicate_count"], 2)
            self.assertEqual(report["duplicate_group_count"], 1)
            self.assertEqual(
                report["duplicate_candidates"]["occurrence_count"],
                2,
            )

    def test_interactive_review_keeps_duplicate_diagnostic_without_repromotion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            for filename in ("first.csv", "second.csv"):
                statement = root / filename
                self._write_statement(
                    statement,
                    ["2026-05-04,SYNTHETIC REVIEWED DUPLICATE,-12.00,HKD"],
                )
                imported = self._run_cli(
                    ["import", str(statement), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)

            reviewed = self._run_cli(
                ["review"],
                cwd=root,
                input_text=f"{_category_number('Dining')}\nq\n",
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)

            state = load_identity_state(root / "output" / "categorized.csv")
            resolved = next(
                row for row in state.rows if "manual_correction" in row["flags"]
            )
            self.assertEqual(len(state.rows), 1)
            self.assertEqual(resolved["source_occurrence_count"], "2")
            self.assertEqual(resolved["needs_review"], "false")
            self.assertNotIn("duplicate_suspected", resolved["flags"].split(";"))
            self.assertNotIn("duplicate_review_promoted", resolved["flags"].split(";"))

            pending_result = self._run_cli(["pending", "2026-05", "--json"], cwd=root)
            self.assertEqual(pending_result.returncode, 0, pending_result.stderr)
            pending_rows = json.loads(pending_result.stdout)["data"]["transactions"]
            self.assertEqual(pending_rows, [])

    def test_combined_and_sequential_imports_produce_same_duplicate_group(
        self,
    ) -> None:
        def prepare_workspace(parent: str) -> tuple[Path, Path]:
            root = self._setup_workspace(parent)
            statements = root / "statements"
            statements.mkdir()
            for name in ("a.csv", "b.csv"):
                self._write_statement(
                    statements / name,
                    ["2026-05-04,SYNTHETIC OVERLAP,-12.00,HKD"],
                )
            return root, statements

        with tempfile.TemporaryDirectory() as tmp:
            combined_root, combined_statements = prepare_workspace(
                str(Path(tmp) / "combined")
            )
            combined = self._run_cli(
                [
                    "import",
                    str(combined_statements),
                    "--no-interactive",
                ],
                cwd=combined_root,
            )
            self.assertEqual(combined.returncode, 0, combined.stderr)
            self.assertIn(
                "Canonical overlap: 1 source occurrence consolidated across 1 group",
                combined.stdout,
            )

            sequential_root, sequential_statements = prepare_workspace(
                str(Path(tmp) / "sequential")
            )
            first = self._run_cli(
                [
                    "import",
                    str(sequential_statements / "b.csv"),
                    "--no-interactive",
                    "--json",
                ],
                cwd=sequential_root,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self._run_cli(
                [
                    "import",
                    str(sequential_statements / "a.csv"),
                    "--no-interactive",
                    "--json",
                ],
                cwd=sequential_root,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            combined_report = json.loads(
                (combined_root / "output" / "import_report.json").read_text(
                    encoding="utf-8"
                )
            )
            sequential_report = json.loads(second.stdout)["data"]
            self.assertEqual(
                combined_report["overlap"]["consolidated_occurrence_count"],
                sequential_report["overlap"]["consolidated_occurrence_count"],
            )
            self.assertEqual(combined_report["duplicate_count"], 2)
            self.assertEqual(sequential_report["duplicate_count"], 2)
            for root in (combined_root, sequential_root):
                rows = self._ledger_rows(root)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["provenance_status"], "exact_one_to_one")

    def test_public_overlap_diagnostics_keep_membership_digests_hidden(self) -> None:
        repeated_row = "2026-05-04,SYNTHETIC PRIVATE MEMBERSHIP,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            for name, count in (("a.csv", 3), ("b.csv", 2), ("c.csv", 1)):
                self._write_statement(statements / name, [repeated_row] * count)
            imported = self._run_cli(
                ["import", str(statements), "--no-interactive", "--json"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            outputs = [
                imported.stdout,
                (root / "output" / "import_report.json").read_text(encoding="utf-8"),
            ]
            for args in (
                ["reconcile", "--dry-run", "--json"],
                ["report", "2026-05", "--no-open", "--json"],
                ["duplicates", "--json"],
            ):
                result = self._run_cli(args, cwd=root)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(result.stdout)

            overlap_manifest = json.loads(
                (root / "output" / ".honeymoney-overlap-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(
                any(
                    membership.get("membership_digest")
                    for group in overlap_manifest["groups"]
                    for membership in group["memberships"]
                )
            )
            for output in outputs:
                self.assertNotIn("membership_digest", output)

    def test_duplicates_cli_lists_and_resolves_same_event_idempotently(self) -> None:
        repeated_row = "2026-05-04,SYNTHETIC COUNT MISMATCH,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            for name, count in (("a.csv", 3), ("b.csv", 2), ("c.csv", 1)):
                self._write_statement(statements / name, [repeated_row] * count)
            imported = self._run_cli(
                ["import", str(statements), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            listed = self._run_cli(["duplicates", "--json"], cwd=root)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            listed_document = json.loads(listed.stdout)
            self.assertEqual(listed_document["schema_version"], 2)
            self.assertEqual(listed_document["command"], "duplicates")
            [group] = listed_document["data"]["groups"]
            self.assertEqual(group["keep_all_count"], 3)
            self.assertEqual(group["same_event_count"], 2)
            self.assertEqual(
                {item["source_display"] for item in group["occurrences"]},
                {"a.csv", "b.csv", "c.csv"},
            )
            self.assertNotIn("original_description", listed.stdout)
            self.assertNotIn("source_id", listed.stdout)
            self.assertNotIn(str(root.resolve()), listed.stdout)
            text_listed = self._run_cli(["duplicates"], cwd=root)
            self.assertEqual(text_listed.returncode, 0, text_listed.stderr)
            self.assertIn(
                "match=exact_normalized_financial_identity",
                text_listed.stdout,
            )
            self.assertIn(
                f"honeymoney duplicates resolve {group['group_id']} --as same-event",
                text_listed.stdout,
            )
            self.assertIn(
                f"honeymoney duplicates resolve {group['group_id']} --as keep-all",
                text_listed.stdout,
            )
            before_report = (root / "output" / "import_report.json").read_bytes()

            resolved = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "same-event",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr or resolved.stdout)
            resolved_document = json.loads(resolved.stdout)
            self.assertTrue(resolved_document["data"]["changed"])
            self.assertEqual(resolved_document["data"]["old_group_canonical_count"], 3)
            self.assertEqual(resolved_document["data"]["new_group_canonical_count"], 2)
            self.assertEqual(resolved_document["data"]["remaining_unresolved_count"], 0)
            self.assertIn("review_needed_csv", resolved_document["artifacts"])
            self.assertEqual(len(self._ledger_rows(root)), 2)
            state = load_identity_state(root / "output" / "categorized.csv")
            self.assertEqual(len(state.source_rows), 6)
            self.assertEqual(
                (root / "output" / "import_report.json").read_bytes(),
                before_report,
            )
            empty = self._run_cli(["duplicates", "--json"], cwd=root)
            self.assertEqual(
                json.loads(empty.stdout)["data"],
                {"group_count": 0, "groups": []},
            )
            retained_rows = self._ledger_rows(root)
            corrected = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Dining",
                            "needs_review": False,
                        }
                        for row in retained_rows
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            reconciled = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            reconciliation = json.loads(reconciled.stdout)["data"]
            self.assertEqual(reconciliation["transaction_count"], 2)
            self.assertEqual(
                len(
                    reconciliation["balance_reconciliation"]["starter_csv"][
                        "statements"
                    ]
                ),
                3,
            )
            reported = self._run_cli(
                ["report", "2026-05", "--no-open", "--json"], cwd=root
            )
            self.assertEqual(reported.returncode, 0, reported.stderr)
            report_html = (root / "output" / "report.html").read_text(encoding="utf-8")
            self.assertIn('id="tile-spending">-24.00<', report_html)
            unrelated = root / "unrelated.csv"
            self._write_statement(
                unrelated,
                ["2026-05-05,SYNTHETIC UNRELATED,-5.00,HKD"],
            )
            imported_again = self._run_cli(
                ["import", str(unrelated), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported_again.returncode, 0, imported_again.stderr)
            target_rows = [
                row
                for row in self._ledger_rows(root)
                if row["merchant"] == "SYNTHETIC COUNT MISMATCH"
            ]
            self.assertEqual(len(target_rows), 2)
            self.assertTrue(
                all(
                    "overlap_count_ambiguous" not in row["flags"].split(";")
                    for row in target_rows
                )
            )
            self.assertEqual(
                json.loads(self._run_cli(["duplicates", "--json"], cwd=root).stdout)[
                    "data"
                ],
                {"group_count": 0, "groups": []},
            )
            before_repeat = self._reset_state_bytes(root)

            repeated = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "same-event",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["data"]["changed"])
            self.assertEqual(self._reset_state_bytes(root), before_repeat)

            invalid = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "merge",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(invalid.returncode, 2)
            invalid_document = json.loads(invalid.stdout)
            self.assertEqual(invalid_document["command"], "duplicates.resolve")
            self.assertEqual(
                invalid_document["errors"][0]["code"],
                "duplicate_choice_invalid",
            )
            self.assertEqual(self._reset_state_bytes(root), before_repeat)

            conflict = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "keep-all",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(conflict.returncode, 2)
            conflict_document = json.loads(conflict.stdout)
            self.assertEqual(conflict_document["command"], "duplicates.resolve")
            self.assertEqual(
                conflict_document["errors"][0]["code"],
                "duplicate_resolution_conflict",
            )
            self.assertEqual(self._reset_state_bytes(root), before_repeat)

    def test_rule_categorized_count_mismatch_resolutions_leave_review_clear(
        self,
    ) -> None:
        repeated_row = "2026-05-04,SYNTHETIC RULED COUNT MISMATCH,-12.00,HKD"
        for choice, expected_count in (("same-event", 1), ("keep-all", 2)):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                (root / "rules.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "rules": [
                                {
                                    "id": "synthetic-count-mismatch",
                                    "enabled": True,
                                    "priority": 100,
                                    "conditions": [
                                        {
                                            "field": "original_description",
                                            "match_type": "exact",
                                            "patterns": [
                                                "SYNTHETIC RULED COUNT MISMATCH"
                                            ],
                                        }
                                    ],
                                    "category": "Dining",
                                    "flow_type": "expense",
                                    "owner": "Household",
                                    "confidence": 0.99,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                statements = root / "statements"
                statements.mkdir()
                self._write_statement(statements / "a.csv", [repeated_row] * 2)
                self._write_statement(statements / "b.csv", [repeated_row])
                imported = self._run_cli(
                    ["import", str(statements), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                listed = self._run_cli(["duplicates", "--json"], cwd=root)
                self.assertEqual(listed.returncode, 0, listed.stderr)
                [group] = json.loads(listed.stdout)["data"]["groups"]

                resolved = self._run_cli(
                    [
                        "duplicates",
                        "resolve",
                        group["group_id"],
                        "--as",
                        choice,
                        "--json",
                    ],
                    cwd=root,
                )

                self.assertEqual(
                    resolved.returncode, 0, resolved.stderr or resolved.stdout
                )
                rows = self._ledger_rows(root)
                self.assertEqual(len(rows), expected_count)
                self.assertTrue(
                    all(
                        row["category"] == "Dining"
                        and row["needs_review"] == "false"
                        and row["reason"] == "Matched rule synthetic-count-mismatch"
                        for row in rows
                    )
                )
                with (root / "output" / "review_needed.csv").open(
                    newline="", encoding="utf-8"
                ) as fh:
                    self.assertEqual(list(csv.DictReader(fh)), [])
                pending = self._run_cli(["pending", "2026-05", "--json"], cwd=root)
                self.assertEqual(pending.returncode, 0, pending.stderr)
                self.assertEqual(json.loads(pending.stdout)["data"]["count"], 0)
                self.assertEqual(json.loads(pending.stdout)["data"]["transactions"], [])

    def test_reset_drift_gets_a_new_group_and_safe_warning(
        self,
    ) -> None:
        repeated_row = "2026-05-04,SYNTHETIC DRIFT,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            for name, count in (("a.csv", 3), ("b.csv", 2), ("c.csv", 1)):
                self._write_statement(statements / name, [repeated_row] * count)
            first = self._run_cli(
                ["import", str(statements), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            listed = self._run_cli(["duplicates", "--json"], cwd=root)
            [old_group] = json.loads(listed.stdout)["data"]["groups"]
            resolved = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    old_group["group_id"],
                    "--as",
                    "same-event",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)

            replaced = statements / "a.csv"
            self._write_statement(replaced, [])
            drifted = self._run_cli(
                [
                    "import",
                    str(replaced),
                    "--reset",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(drifted.returncode, 0, drifted.stderr or drifted.stdout)
            drifted_document = json.loads(drifted.stdout)
            self.assertEqual(
                drifted_document["warnings"],
                [
                    "duplicate_membership_changed: duplicate group membership "
                    f"changed; review "
                    f"{drifted_document['data']['overlap']['warnings'][0]['group_id']}"
                ],
            )
            warning_text = json.dumps(drifted_document["warnings"])
            self.assertNotIn("SYNTHETIC DRIFT", warning_text)
            self.assertNotIn("-12.00", warning_text)
            listed = self._run_cli(["duplicates", "--json"], cwd=root)
            [new_group] = json.loads(listed.stdout)["data"]["groups"]
            self.assertNotEqual(new_group["group_id"], old_group["group_id"])
            self.assertEqual(len(self._ledger_rows(root)), 2)
            self.assertTrue(
                all(
                    row["needs_review"] == "true"
                    and "overlap_count_ambiguous" in row["flags"].split(";")
                    for row in self._ledger_rows(root)
                )
            )
            unrelated = root / "replacement-unrelated.csv"
            self._write_statement(
                unrelated,
                ["2026-05-05,SYNTHETIC REPLACEMENT UNRELATED,-5.00,HKD"],
            )
            unchanged = self._run_cli(
                ["import", str(unrelated), "--no-interactive", "--json"], cwd=root
            )
            self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
            self.assertFalse(
                any(
                    "duplicate_membership_changed" in warning
                    for warning in json.loads(unchanged.stdout)["warnings"]
                )
            )

            before = self._reset_state_bytes(root)
            stale = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    old_group["group_id"],
                    "--as",
                    "same-event",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(stale.returncode, 2)
            stale_document = json.loads(stale.stdout)
            self.assertEqual(stale_document["command"], "duplicates.resolve")
            self.assertEqual(
                stale_document["errors"][0]["code"], "duplicate_group_stale"
            )
            self.assertEqual(self._reset_state_bytes(root), before)

    def test_strict_import_warns_for_mismatch_after_equal_count_transition(
        self,
    ) -> None:
        repeated_row = "2026-05-04,SYNTHETIC EQUAL BRIDGE,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            first_source = statements / "a.csv"
            second_source = statements / "b.csv"
            third_source = statements / "c.csv"
            self._write_statement(first_source, [repeated_row] * 3)
            self._write_statement(second_source, [repeated_row] * 2)
            self._write_statement(third_source, [repeated_row] * 2)
            imported = self._run_cli(
                ["import", str(statements), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            [group] = json.loads(
                self._run_cli(["duplicates", "--json"], cwd=root).stdout
            )["data"]["groups"]
            resolved = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "same-event",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)

            self._write_statement(first_source, [])
            equalized = self._run_cli(
                [
                    "import",
                    str(first_source),
                    "--reset",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(
                equalized.returncode, 0, equalized.stderr or equalized.stdout
            )
            self.assertEqual(json.loads(equalized.stdout)["warnings"], [])
            self.assertEqual(
                {row["provenance_status"] for row in self._ledger_rows(root)},
                {"pooled_equal_count"},
            )

            new_source = statements / "d.csv"
            self._write_statement(new_source, [repeated_row])
            drifted = self._run_cli(
                [
                    "import",
                    str(new_source),
                    "--no-interactive",
                    "--strict",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(drifted.returncode, 1, drifted.stderr)
            document = json.loads(drifted.stdout)
            self.assertEqual(document["status"], "partial_success")
            [warning] = document["data"]["overlap"]["warnings"]
            self.assertEqual(warning["code"], "duplicate_membership_changed")
            self.assertEqual(
                document["warnings"],
                [
                    "duplicate_membership_changed: duplicate group membership "
                    f"changed; review {warning['group_id']}"
                ],
            )

    def test_keep_all_preserves_corrections_and_resolution_fault_rolls_back(
        self,
    ) -> None:
        repeated_row = "2026-05-04,SYNTHETIC KEEP ALL,-12.00,HKD"
        for fault in (None, "replace-before:categorized.csv"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statements = root / "statements"
                statements.mkdir()
                for name, count in (("a.csv", 3), ("b.csv", 2)):
                    self._write_statement(statements / name, [repeated_row] * count)
                imported = self._run_cli(
                    ["import", str(statements), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                listed = self._run_cli(["duplicates", "--json"], cwd=root)
                [group] = json.loads(listed.stdout)["data"]["groups"]
                rows = sorted(
                    self._ledger_rows(root),
                    key=lambda row: int(row["canonical_slot"]),
                )
                tail_id = rows[-1]["transaction_id"]
                corrected = self._run_cli(
                    ["correct", "--file", "-", "--json"],
                    cwd=root,
                    input_text=json.dumps(
                        [
                            {
                                "transaction_id": tail_id,
                                "category": "Dining",
                                "needs_review": False,
                            }
                        ]
                    ),
                )
                self.assertEqual(corrected.returncode, 0, corrected.stderr)
                correction_bytes = (root / "corrections.csv").read_bytes()
                before = self._reset_state_bytes(root)

                unsafe = self._run_cli(
                    [
                        "duplicates",
                        "resolve",
                        group["group_id"],
                        "--as",
                        "same-event",
                        "--json",
                    ],
                    cwd=root,
                )
                self.assertEqual(unsafe.returncode, 2)
                self.assertIn("duplicate_history_conflict", unsafe.stdout)
                self.assertEqual(self._reset_state_bytes(root), before)

                kept = self._run_cli(
                    [
                        "duplicates",
                        "resolve",
                        group["group_id"],
                        "--as",
                        "keep-all",
                        "--json",
                    ],
                    cwd=root,
                    filesystem_fault=fault,
                )
                if fault is not None:
                    self.assertEqual(kept.returncode, 2)
                    self.assertEqual(self._reset_state_bytes(root), before)
                    continue
                self.assertEqual(kept.returncode, 0, kept.stderr)
                self.assertEqual(len(self._ledger_rows(root)), 3)
                self.assertEqual(
                    (root / "corrections.csv").read_bytes(), correction_bytes
                )
                self.assertEqual(
                    json.loads(
                        self._run_cli(["duplicates", "--json"], cwd=root).stdout
                    )["data"],
                    {"group_count": 0, "groups": []},
                )
                kept_rows = self._ledger_rows(root)
                corrected_again = self._run_cli(
                    ["correct", "--file", "-", "--json"],
                    cwd=root,
                    input_text=json.dumps(
                        [
                            {
                                "transaction_id": kept_rows[0]["transaction_id"],
                                "notes": "Synthetic keep-all note",
                            }
                        ]
                    ),
                )
                self.assertEqual(corrected_again.returncode, 0, corrected_again.stderr)
                reconciled = self._run_cli(["reconcile", "--json"], cwd=root)
                self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
                unrelated = root / "unrelated.csv"
                self._write_statement(
                    unrelated,
                    ["2026-05-05,SYNTHETIC KEEP UNRELATED,-5.00,HKD"],
                )
                imported_again = self._run_cli(
                    ["import", str(unrelated), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported_again.returncode, 0, imported_again.stderr)
                kept_rows = [
                    row
                    for row in self._ledger_rows(root)
                    if row["merchant"] == "SYNTHETIC KEEP ALL"
                ]
                self.assertEqual(len(kept_rows), 3)
                self.assertTrue(
                    all(
                        "overlap_count_ambiguous" not in row["flags"].split(";")
                        for row in kept_rows
                    )
                )
                self.assertEqual(
                    json.loads(
                        self._run_cli(["duplicates", "--json"], cwd=root).stdout
                    )["data"],
                    {"group_count": 0, "groups": []},
                )

    def test_same_event_moves_the_only_tail_correction_to_the_retained_slot(
        self,
    ) -> None:
        repeated_row = "2026-05-04,SYNTHETIC CORRECTION MOVE,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            self._write_statement(statements / "a.csv", [repeated_row] * 2)
            self._write_statement(statements / "b.csv", [repeated_row])
            imported = self._run_cli(
                ["import", str(statements), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            [group] = json.loads(
                self._run_cli(["duplicates", "--json"], cwd=root).stdout
            )["data"]["groups"]
            rows = sorted(
                self._ledger_rows(root),
                key=lambda row: int(row["canonical_slot"]),
            )
            retained_id = rows[0]["transaction_id"]
            tail_id = rows[1]["transaction_id"]
            corrected = self._run_cli(
                ["correct", "--file", "-", "--json"],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": tail_id,
                            "category": "Dining",
                            "flow_type": "expense",
                            "needs_review": False,
                        }
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)

            resolved = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "same-event",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(resolved.returncode, 0, resolved.stderr or resolved.stdout)
            [row] = self._ledger_rows(root)
            self.assertEqual(row["transaction_id"], retained_id)
            self.assertEqual(row["category"], "Dining")
            self.assertEqual(row["needs_review"], "false")
            with (root / "corrections.csv").open(newline="", encoding="utf-8") as fh:
                [correction] = list(csv.DictReader(fh))
            self.assertEqual(correction["transaction_id"], retained_id)
            self.assertEqual(correction["category"], "Dining")
            self.assertFalse(
                tail_id in (root / "corrections.csv").read_text(encoding="utf-8")
            )

    def test_same_event_resolution_recovers_each_generation_boundary(self) -> None:
        repeated_row = "2026-05-04,SYNTHETIC RESOLUTION FAULT,-12.00,HKD"
        faults = (
            "replace-before:review_needed.csv",
            "replace-before:.honeymoney-overlap-manifest.json",
            "replace-before:corrections.csv",
            "replace-after:categorized.csv",
        )
        for fault in faults:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = self._setup_workspace(tmp)
                statements = root / "statements"
                statements.mkdir()
                self._write_statement(statements / "a.csv", [repeated_row] * 2)
                self._write_statement(statements / "b.csv", [repeated_row])
                imported = self._run_cli(
                    ["import", str(statements), "--no-interactive"], cwd=root
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                [group] = json.loads(
                    self._run_cli(["duplicates", "--json"], cwd=root).stdout
                )["data"]["groups"]
                rows = sorted(
                    self._ledger_rows(root),
                    key=lambda row: int(row["canonical_slot"]),
                )
                retained_id = rows[0]["transaction_id"]
                tail_id = rows[1]["transaction_id"]
                corrected = self._run_cli(
                    ["correct", "--file", "-", "--json"],
                    cwd=root,
                    input_text=json.dumps(
                        [
                            {
                                "transaction_id": tail_id,
                                "category": "Dining",
                                "flow_type": "expense",
                                "needs_review": False,
                            }
                        ]
                    ),
                )
                self.assertEqual(corrected.returncode, 0, corrected.stderr)
                before = self._reset_state_bytes(root)

                interrupted = self._run_cli(
                    [
                        "duplicates",
                        "resolve",
                        group["group_id"],
                        "--as",
                        "same-event",
                        "--json",
                    ],
                    cwd=root,
                    filesystem_fault=fault,
                )

                committed = fault == "replace-after:categorized.csv"
                self.assertEqual(interrupted.returncode, 75 if committed else 2)
                duplicates = self._run_cli(["duplicates", "--json"], cwd=root)
                status = self._run_cli(["status", "2026-05", "--json"], cwd=root)
                self.assertEqual(duplicates.returncode, 0, duplicates.stderr)
                self.assertEqual(status.returncode, 0, status.stderr)
                after = self._reset_state_bytes(root)
                if not committed:
                    self.assertEqual(after, before)
                    self.assertEqual(
                        json.loads(duplicates.stdout)["data"]["group_count"], 1
                    )
                    continue

                self.assertNotEqual(after, before)
                self.assertEqual(
                    json.loads(duplicates.stdout)["data"],
                    {"group_count": 0, "groups": []},
                )
                [row] = self._ledger_rows(root)
                self.assertEqual(row["transaction_id"], retained_id)
                self.assertEqual(row["category"], "Dining")
                with (root / "corrections.csv").open(
                    newline="", encoding="utf-8"
                ) as fh:
                    [correction] = list(csv.DictReader(fh))
                self.assertEqual(correction["transaction_id"], retained_id)
                self.assertNotIn(
                    tail_id,
                    (root / "corrections.csv").read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    list((root / "output").glob(".*honeymoney-state.json")),
                    [],
                )

    def test_duplicate_resolution_recovers_custom_output_with_external_corrections(
        self,
    ) -> None:
        repeated_row = "2026-05-04,SYNTHETIC CUSTOM RESOLUTION,-12.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            custom_output = root / "custom-output" / "nested" / "ledger.csv"
            statements = root / "statements"
            statements.mkdir()
            self._write_statement(statements / "a.csv", [repeated_row] * 2)
            self._write_statement(statements / "b.csv", [repeated_row])
            imported = self._run_cli(
                [
                    "import",
                    str(statements),
                    "--output",
                    str(custom_output),
                    "--no-interactive",
                ],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            listed = self._run_cli(
                ["duplicates", "--output", str(custom_output), "--json"],
                cwd=root,
            )
            [group] = json.loads(listed.stdout)["data"]["groups"]
            with custom_output.open(newline="", encoding="utf-8") as handle:
                rows = sorted(
                    csv.DictReader(handle),
                    key=lambda row: int(row["canonical_slot"]),
                )
            tail_id = rows[-1]["transaction_id"]
            corrected = self._run_cli(
                [
                    "correct",
                    "--output",
                    str(custom_output),
                    "--file",
                    "-",
                    "--json",
                ],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": tail_id,
                            "category": "Dining",
                            "flow_type": "expense",
                            "needs_review": False,
                        }
                    ]
                ),
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)

            interrupted = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "same-event",
                    "--output",
                    str(custom_output),
                    "--json",
                ],
                cwd=root,
                filesystem_fault="replace-after:ledger.csv",
            )
            self.assertEqual(interrupted.returncode, 75, interrupted.stderr)

            recovered = self._run_cli(
                [
                    "correct",
                    "--output",
                    str(custom_output),
                    "--file",
                    "-",
                    "--json",
                ],
                cwd=root,
                input_text=json.dumps(
                    [
                        {
                            "transaction_id": rows[0]["transaction_id"],
                            "notes": "Synthetic recovered correction",
                        }
                    ]
                ),
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            listed_after_recovery = self._run_cli(
                ["duplicates", "--output", str(custom_output), "--json"],
                cwd=root,
            )
            self.assertEqual(
                json.loads(listed_after_recovery.stdout)["data"],
                {"group_count": 0, "groups": []},
            )
            self.assertEqual(
                list(custom_output.parent.glob(".*honeymoney-state.json")),
                [],
            )
            self.assertIn(
                "Dining",
                (root / "corrections.csv").read_text(encoding="utf-8"),
            )

    def test_source_replacement_excludes_retired_rows_from_duplicate_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statements = root / "statements"
            statements.mkdir()
            statement = statements / "replacement.csv"
            overlap = statements / "overlap.csv"
            repeated_row = "2026-05-04,REPLACED ROW,-12.00,HKD"
            self._write_statement(statement, [repeated_row])
            self._write_statement(overlap, [repeated_row])
            first = self._run_cli(
                ["import", str(statements), "--no-interactive"], cwd=root
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            [overlapped] = self._ledger_rows(root)
            self.assertEqual(overlapped["provenance_status"], "exact_one_to_one")
            self._write_statement(
                statement,
                ["2026-05-05,REPLACED ROW,-12.00,HKD"],
            )

            replaced = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual(json.loads(replaced.stdout)["data"]["duplicate_count"], 0)
            rows = self._ledger_rows(root)
            self.assertEqual(len(rows), 2)
            replacement = next(row for row in rows if row["date"] == "2026-05-05")
            self.assertEqual(replacement["date"], "2026-05-05")
            for row in rows:
                self.assertNotIn("duplicate_suspected", row["flags"])
                self.assertNotIn(DUPLICATE_MATCH_TYPE, row["reason"])

    def test_pending_json_clears_stale_duplicate_state_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "same-source.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,SYNTHETIC SAME SOURCE,-12.00,HKD",
                    "2026-05-04,SYNTHETIC SAME SOURCE,-12.00,HKD",
                ],
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            categorized_path = root / "output" / "categorized.csv"
            with categorized_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            for row in rows:
                row["category"] = "Dining"
                row["flow_type"] = "expense"
                row["needs_review"] = "true"
                row["review_reasons"] = "identity_conflict"
                row["flags"] = "duplicate_suspected;duplicate_review_promoted"
                row["reason"] = (
                    "Duplicate candidate [match_type=legacy, occurrence_ids=stale]"
                )
            with categorized_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)

            before = self._artifact_bytes(
                root,
                [
                    "output/categorized.csv",
                    "output/review_needed.csv",
                    "output/.honeymoney-identity-manifest.json",
                ],
            )
            pending = self._run_cli(["pending", "2026-05", "--json"], cwd=root)

            self.assertEqual(pending.returncode, 0, pending.stderr)
            data = json.loads(pending.stdout)["data"]
            self.assertEqual(data["count"], 0)
            self.assertEqual(data["transactions"], [])
            self.assertEqual(
                data["duplicate_candidates"],
                {"group_count": 0, "groups": [], "occurrence_count": 0},
            )
            self.assertEqual(
                before,
                self._artifact_bytes(
                    root,
                    [
                        "output/categorized.csv",
                        "output/review_needed.csv",
                        "output/.honeymoney-identity-manifest.json",
                    ],
                ),
            )

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

            self.assertEqual(len(separate_rows), 1)
            self.assertEqual(len(directory_rows), 1)
            separate_ids = {row["transaction_id"] for row in separate_rows}
            directory_ids = {row["transaction_id"] for row in directory_rows}
            self.assertEqual(len(separate_ids), 1)
            self.assertEqual(len(directory_ids), 1)
            self.assertEqual(separate_rows[0]["provenance_status"], "exact_one_to_one")
            self.assertEqual(directory_rows[0]["provenance_status"], "exact_one_to_one")

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

    def test_rename_replace_updates_retired_evidence_ownership(self) -> None:
        first_row = "2026-06-18,SYNTHETIC CURRENT RECORD,-24.00,HKD"
        retired_row = "2026-06-19,SYNTHETIC RETIRED RECORD,-25.00,HKD"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            original = root / "original.csv"
            self._write_statement(original, [first_row, retired_row])
            imported = self._run_cli(
                ["import", str(original), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            renamed = root / "renamed.csv"
            original.rename(renamed)
            accepted_rename = self._run_cli(
                ["import", str(renamed), "--replace", "--no-interactive"], cwd=root
            )
            self.assertEqual(accepted_rename.returncode, 0, accepted_rename.stderr)

            self._write_statement(renamed, [first_row])
            replaced = self._run_cli(
                ["import", str(renamed), "--replace", "--no-interactive"], cwd=root
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)

            state = load_identity_state(root / "output" / "categorized.csv")
            records = {
                record["transaction_id"]: (source, record)
                for source in state.manifest["sources"]
                for record in source["records"]
            }
            retired = next(
                row
                for row in state.source_evidence_rows
                if records[row["transaction_id"]][1]["state"] == "retired"
            )
            source, record = records[retired["transaction_id"]]

            self.assertEqual(retired["source_id"], source["source_id"])
            self.assertEqual(
                retired["source_namespace_id"], source["source_namespace_id"]
            )
            self.assertEqual(retired["source_record_id"], record["source_record_id"])
            self.assertEqual(
                retired["source_revision"],
                record["allocation_origin"]["source_revision"],
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
            [canonical] = self._ledger_rows(root)
            corrected_id = canonical["transaction_id"]
            self._correct_category(root, corrected_id)
            alpha = statements / "alpha.csv"
            self._write_statement(alpha, [identical_row])
            inserted = self._run_cli(
                ["import", str(alpha), "--no-interactive"], cwd=root
            )
            self.assertEqual(inserted.returncode, 0, inserted.stderr)
            [after] = self._ledger_rows(root)
            self.assertEqual(after["transaction_id"], corrected_id)
            self.assertEqual(after["category"], "Dining")
            self.assertEqual(after["source_occurrence_count"], "3")

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
            retired_state = load_identity_state(root / "output" / "categorized.csv")
            self.assertEqual(retired_state.source_rows, [])
            self.assertEqual(len(retired_state.source_evidence_rows), 1)
            self.assertEqual(
                retired_state.source_evidence_rows[0]["merchant"],
                "SYNTHETIC REUSED FACTS",
            )

            # The facts are identical, but the blank physical row moves the CSV
            # allocation locator from physical row 2 to physical row 3.
            self._write_statement(statement, ["", source_row])
            replaced = self._run_cli(
                ["import", str(statement), "--replace", "--no-interactive"],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            [new_row] = self._ledger_rows(root)
            self.assertEqual(new_row["transaction_id"], first_row["transaction_id"])
            self.assertEqual(new_row["category"], "Dining")

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
            rows = {
                row["source_file"]: row
                for row in load_identity_state(
                    root / "output" / "categorized.csv"
                ).source_rows
            }
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

    def test_ollama_skips_corrected_rows_on_replace_and_uses_them_after_reset(
        self,
    ) -> None:
        sent_batches: list[list[str]] = []

        class FakeTransport:
            def request(self, request: OllamaHttpRequest) -> bytes:
                assert request.body is not None
                prompt = json.loads(json.loads(request.body)["prompt"])
                transactions = prompt["transactions"]
                sent_batches.append([transaction["id"] for transaction in transactions])
                return json.dumps(
                    {
                        "response": json.dumps(
                            [
                                {
                                    "id": transaction["id"],
                                    "category": "Dining",
                                    "confidence": 0.9,
                                    "reason": "Synthetic local category",
                                }
                                for transaction in transactions
                            ]
                        )
                    }
                ).encode()

        def fake_apply(transactions, config, progress=None, corrections=None):
            return apply_ollama_fallback(
                transactions,
                config,
                progress=progress,
                transport=FakeTransport(),
                corrections=corrections,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,SYNTHETIC REVIEWED SHOP,-12.00,HKD",
                    "2026-05-05,SYNTHETIC UNREVIEWED SHOP,-15.00,HKD",
                ],
            )
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"] = {
                "enabled": False,
                "url": "http://localhost:11434/api/generate",
                "model": "test",
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(
                    cli.main(["import", str(statement), "--no-interactive"]), 0
                )
                corrected_id = next(
                    row["transaction_id"]
                    for row in self._ledger_rows(root)
                    if row["merchant"] == "SYNTHETIC REVIEWED SHOP"
                )
                (root / "corrections.csv").write_text(
                    f"transaction_id,category\n{corrected_id},Groceries\n",
                    encoding="utf-8",
                )
                config["ollama"]["enabled"] = True
                config_path.write_text(json.dumps(config), encoding="utf-8")

                with patch.object(cli, "apply_ollama_fallback", side_effect=fake_apply):
                    self.assertEqual(
                        cli.main(
                            [
                                "import",
                                str(statement),
                                "--replace",
                                "--no-interactive",
                            ]
                        ),
                        0,
                    )

                    replace_report = json.loads(
                        (root / "output" / "import_report.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(replace_report["ollama"]["candidate_count"], 1)
                    self.assertEqual(
                        replace_report["ollama"][
                            "skipped_exact_category_correction_count"
                        ],
                        1,
                    )
                    self.assertEqual(
                        replace_report["categorization"]["provenance"],
                        {
                            "total_count": 2,
                            "deterministic_count": 0,
                            "memory_count": 0,
                            "exact_correction_count": 1,
                            "accepted_model_count": 0,
                            "reviewable_model_count": 1,
                            "unresolved_count": 0,
                        },
                    )
                    self.assertEqual(
                        sent_batches,
                        [
                            [
                                row["transaction_id"]
                                for row in self._ledger_rows(root)
                                if row["merchant"] == "SYNTHETIC UNREVIEWED SHOP"
                            ]
                        ],
                    )

                    sent_batches.clear()
                    self.assertEqual(
                        cli.main(
                            [
                                "import",
                                str(statement),
                                "--reset",
                                "--no-interactive",
                            ]
                        ),
                        0,
                    )
                reset_report = json.loads(
                    (root / "output" / "import_report.json").read_text(encoding="utf-8")
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(reset_report["ollama"]["candidate_count"], 2)
            self.assertEqual(
                reset_report["ollama"]["skipped_exact_category_correction_count"],
                0,
            )
            self.assertEqual(len(sent_batches), 1)
            self.assertEqual(len(sent_batches[0]), 2)
            self.assertEqual(
                len(
                    (root / "corrections.csv").read_text(encoding="utf-8").splitlines()
                ),
                1,
            )

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
            state = load_identity_state(categorized)
            legacy_rows = [dict(item) for item in state.rows]
            legacy_rows[0].update(
                {
                    "category": "Groceries",
                    "confidence": "1.00",
                    "needs_review": "true",
                    "reason": "Synthetic legacy manual review",
                    "flags": "manual_correction",
                }
            )
            self._write_previous_review_schema(
                root,
                legacy_rows,
                [dict(item) for item in state.source_evidence_rows],
            )
            (root / "corrections.csv").write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Groceries",
                            "confidence": "1.00",
                            "reason": "Synthetic legacy manual review",
                            "needs_review": "true",
                        }
                    ],
                ),
                encoding="utf-8",
            )
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
                rows = {row["merchant"]: row for row in csv.DictReader(fh)}
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
            pdf_id = rows["SYNTHETIC MARKET"]["transaction_id"]
            csv_id = rows["ORIGINAL SHOP"]["transaction_id"]
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
                reset_rows = {row["merchant"]: row for row in csv.DictReader(fh)}
            self.assertEqual(reset_rows["SYNTHETIC MARKET"]["category"], "Groceries")
            self.assertEqual(reset_rows["UPDATED SHOP"]["category"], "Unknown")
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
                input_text=f"{_category_number('Dining')}\n\n",
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
            self.assertIn("Resolve the accounting flow", review_result.stdout)
            self.assertIn("Choose a category", review_result.stdout)
            self.assertNotIn("REVIEWED PURCHASE", review_result.stdout)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as fh:
                rows = {row["merchant"]: row for row in csv.DictReader(fh)}
            self.assertEqual(rows["REVIEWED PURCHASE"]["category"], "Dining")
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
                input_text=f"{_category_number('Dining')}\n",
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

            self.assertIn("Ledger now has 2 canonical records", result.stdout)
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
        default_rate_cache = examples_dir / "rates.json"
        self.assertFalse(default_rate_cache.exists())
        self.addCleanup(default_rate_cache.unlink, missing_ok=True)
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
            with (output_dir / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                generated_rows = list(csv.DictReader(handle))
            self.assertTrue(generated_rows)
            self.assertTrue(all(row["canonical_group_id"] for row in generated_rows))
            self.assertTrue(all(not row["source_id"] for row in generated_rows))
            with (output_dir / "review_needed.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                self.assertTrue(list(csv.DictReader(handle)))
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
            self.assertEqual(actual_report["status"], expected_report["status"])
            self.assertEqual(
                actual_report["input_count"], expected_report["input_count"]
            )
            self.assertEqual(
                actual_report["source_occurrence_count"],
                expected_report["successful_record_count"],
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
            self.assertIn("Canonical records:    2", result.stdout)
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

            structured = self._run_cli(["status", "--json"], cwd=root)
            self.assertEqual(structured.returncode, 0, structured.stderr)
            data = json.loads(structured.stdout)["data"]
            self.assertEqual(data["duplicate_count"], 0)
            self.assertEqual(data["duplicate_group_count"], 0)
            self.assertEqual(
                data["duplicate_candidates"],
                {"group_count": 0, "groups": [], "occurrence_count": 0},
            )

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

    def test_report_refuses_a_symbolic_link_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "may.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC REPORT,-12.00,HKD"],
            )
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive"], cwd=root
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            protected = root / "protected.txt"
            protected.write_text("keep me\n", encoding="utf-8")
            report_path = root / "output" / "report.html"
            report_path.symlink_to(protected)

            result = self._run_cli(
                [
                    "report",
                    "--month",
                    "2026-05",
                    "--output",
                    str(report_path),
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(protected.read_text(encoding="utf-8"), "keep me\n")
            self.assertTrue(report_path.is_symlink())

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

        def fake_apply(transactions, config, progress=None, corrections=None):
            return apply_ollama_fallback(
                transactions,
                config,
                progress=progress,
                transport=FakeTransport(),
                corrections=corrections,
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
