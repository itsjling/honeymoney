import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.identity_state import load_configured_identity_state
from honeymoney.valuation_inspection import (
    ValuationInspectionError,
    inspect_missing_valuations,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class ValuationInspectionWorkflowTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "synthetic-money"
        result = self._run_cli(
            ["setup", "--root", str(root), "--json"],
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _write_statement(
        self,
        path: Path,
        rows: list[str],
    ) -> None:
        path.write_text(
            "\n".join(["Date,Description,Amount,Currency", *rows]),
            encoding="utf-8",
        )

    def _import(self, root: Path, statement: Path, *extra: str) -> None:
        result = self._run_cli(
            [
                "import",
                str(statement),
                *extra,
                "--no-interactive",
                "--json",
            ],
            cwd=root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _workspace_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_lists_all_dates_period_and_transaction_with_active_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "missing-values.csv"
            self._write_statement(
                statement,
                [
                    "2025-12-31,SYNTHETIC OLD EUR,-10.00,EUR",
                    "2026-05-04,SYNTHETIC CURRENT EUR,20.00,EUR",
                ],
            )
            self._import(root, statement)
            before = self._workspace_bytes(root)

            result = self._run_cli(
                ["valuation", "missing", "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["command"], "valuation.missing")
            self.assertEqual(payload["data"]["period"], {"all_dates": True})
            self.assertEqual(payload["data"]["count"], 2)
            transactions = payload["data"]["transactions"]
            self.assertEqual(
                {item["date"] for item in transactions},
                {"2025-12-31", "2026-05-04"},
            )
            for item in transactions:
                self.assertTrue(item["transaction_id"])
                self.assertEqual(item["original_currency"], "EUR")
                self.assertEqual(item["posted_currency"], "EUR")
                self.assertEqual(item["flow_type"], "unresolved")
                self.assertEqual(item["valuation_status"], "missing")
                self.assertEqual(item["valuation_source"], "missing")
                self.assertEqual(item["source_occurrence_count"], 1)
                self.assertEqual(len(item["source_evidence"]), 1)
                self.assertEqual(
                    item["source_evidence"][0]["source_file"],
                    "missing-values.csv",
                )
                self.assertEqual(
                    item["source_evidence"][0]["source_display"],
                    "missing-values.csv",
                )
                self.assertTrue(item["source_evidence"][0]["source_row"])
            self.assertEqual(self._workspace_bytes(root), before)

            period = self._run_cli(
                ["valuation", "missing", "2026-05", "--json"],
                cwd=root,
            )
            self.assertEqual(period.returncode, 0, period.stderr)
            [current] = json.loads(period.stdout)["data"]["transactions"]
            self.assertEqual(current["date"], "2026-05-04")
            focused = self._run_cli(
                [
                    "valuation",
                    "missing",
                    "--transaction",
                    current["transaction_id"],
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(focused.returncode, 0, focused.stderr)
            self.assertEqual(
                json.loads(focused.stdout)["data"]["transactions"], [current]
            )
            human = self._run_cli(
                [
                    "valuation",
                    "missing",
                    "--transaction",
                    current["transaction_id"],
                ],
                cwd=root,
            )
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn(current["transaction_id"], human.stdout)
            self.assertIn("Original: 20.00 EUR", human.stdout)
            self.assertIn("missing-values.csv", human.stdout)

    def test_configured_input_evidence_keeps_its_workspace_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "input" / "normal.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC INPUT EUR,-10.00,EUR"],
            )
            self._import(root, statement)

            result = self._run_cli(
                ["valuation", "missing", "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            [transaction] = json.loads(result.stdout)["data"]["transactions"]
            [evidence] = transaction["source_evidence"]
            self.assertEqual(evidence["source_file"], "input/normal.csv")
            self.assertEqual(evidence["source_display"], "normal.csv")

    def test_same_external_labels_keep_multiplicity_without_guessing_a_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = Path(tmp) / "external-one" / "same.csv"
            second = Path(tmp) / "external-two" / "same.csv"
            first.parent.mkdir()
            second.parent.mkdir()
            row = "2026-05-04,SYNTHETIC SAME LABEL EUR,-10.00,EUR"
            self._write_statement(first, [row])
            self._write_statement(second, [row])
            self._import(root, first)
            self._import(root, second)
            self._write_statement(
                root / "input" / "same.csv",
                ["2026-05-05,UNRELATED SYNTHETIC EUR,-20.00,EUR"],
            )

            result = self._run_cli(
                ["valuation", "missing", "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            [transaction] = json.loads(result.stdout)["data"]["transactions"]
            self.assertEqual(transaction["source_occurrence_count"], 2)
            self.assertEqual(len(transaction["source_evidence"]), 2)
            self.assertEqual(
                transaction["source_evidence"],
                [
                    {
                        "source_file": "",
                        "source_display": "same.csv",
                        "source_page": "",
                        "source_row": "2",
                    },
                    {
                        "source_file": "",
                        "source_display": "same.csv",
                        "source_page": "",
                        "source_row": "2",
                    },
                ],
            )

    def test_consolidated_and_repeated_overlap_lists_the_active_source_pool(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "first.csv"
            second = root / "second.csv"
            repeated_rows = [
                "2026-05-04,SYNTHETIC REPEATED EUR,-10.00,EUR",
                "2026-05-04,SYNTHETIC REPEATED EUR,-10.00,EUR",
            ]
            self._write_statement(first, repeated_rows)
            self._write_statement(second, repeated_rows)
            self._import(root, first)
            self._import(root, second)

            result = self._run_cli(
                ["valuation", "missing", "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            transactions = json.loads(result.stdout)["data"]["transactions"]
            self.assertEqual(len(transactions), 2)
            self.assertEqual(
                {item["source_occurrence_count"] for item in transactions},
                {4},
            )
            for item in transactions:
                self.assertEqual(
                    {
                        (
                            evidence["source_file"],
                            evidence["source_row"],
                        )
                        for evidence in item["source_evidence"]
                    },
                    {
                        ("first.csv", "2"),
                        ("first.csv", "3"),
                        ("second.csv", "2"),
                        ("second.csv", "3"),
                    },
                )

    def test_retired_evidence_is_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "first.csv"
            second = root / "second.csv"
            row = "2026-05-04,SYNTHETIC SHARED EUR,-10.00,EUR"
            self._write_statement(first, [row])
            self._write_statement(second, [row])
            self._import(root, first)
            self._import(root, second)
            self._write_statement(first, [])
            self._import(root, first, "--replace")

            result = self._run_cli(
                ["valuation", "missing", "--json"],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            [transaction] = json.loads(result.stdout)["data"]["transactions"]
            self.assertEqual(transaction["source_occurrence_count"], 1)
            self.assertEqual(
                transaction["source_evidence"],
                [
                    {
                        "source_file": "second.csv",
                        "source_display": "second.csv",
                        "source_page": "",
                        "source_row": "2",
                    }
                ],
            )

    def test_source_data_flags_and_missing_display_provenance_are_explicit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "source-data.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC SOURCE DATA,-10.00,EUR"],
            )
            self._import(root, statement)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            state = load_configured_identity_state(
                Path(config["paths"]["output"]),
                config,
            )
            state.rows[0]["flags"] = "invalid_amount"
            state.source_rows[0]["source_file"] = ""
            state.source_rows[0]["source_page"] = ""
            state.source_rows[0]["source_row"] = ""

            [item] = inspect_missing_valuations(
                state,
                workspace_root=root,
            )

            self.assertEqual(item["source_data_flags"], ["invalid_amount"])
            self.assertEqual(
                item["source_evidence"],
                [
                    {
                        "source_file": "",
                        "source_display": "",
                        "source_page": "",
                        "source_row": "",
                    }
                ],
            )

    def test_inconsistent_or_missing_provenance_fails_closed_without_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "private-shaped.csv"
            self._write_statement(
                statement,
                ["2026-05-04,PRIVATE-SHAPED TEXT,-9182.73,EUR"],
            )
            self._import(root, statement)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            state = load_configured_identity_state(
                Path(config["paths"]["output"]),
                config,
            )
            state.rows[0]["source_occurrence_count"] = "99"

            with self.assertRaises(ValuationInspectionError) as raised:
                inspect_missing_valuations(state, workspace_root=root)

            self.assertEqual(
                raised.exception.code,
                "valuation_provenance_inconsistent",
            )
            occurrence_path = root / "output" / ".honeymoney-source-occurrences.csv"
            with occurrence_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                header = reader.fieldnames or []
            occurrence_path.write_text(",".join(header) + "\n", encoding="utf-8")
            failed = self._run_cli(
                ["valuation", "missing", "--json"],
                cwd=root,
            )
            self.assertEqual(failed.returncode, 2, failed.stderr)
            self.assertEqual(json.loads(failed.stdout)["status"], "error")
            self.assertNotIn("9182.73", failed.stdout)
            self.assertNotIn("PRIVATE-SHAPED", failed.stdout)


if __name__ == "__main__":
    unittest.main()
