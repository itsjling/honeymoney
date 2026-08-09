from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.csv_artifacts import csv_document
from honeymoney.rates import parse_hkma_daily_document
from honeymoney.workspace_commands import (
    WorkspaceCommandError,
    apply_workspace_corrections,
    apply_workspace_rate_observations,
    import_workspace,
)
from honeymoney.workspace_setup import setup_workspace


class WorkspaceCorrectionMutationTest(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def _workspace(self, root: Path) -> Path:
        paths = setup_workspace(root)
        source = root / "synthetic.csv"
        source.write_text(
            "Date,Description,Amount,Currency\n"
            "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
            encoding="utf-8",
        )
        import_workspace(
            source,
            config_path=paths.config,
            interactive=False,
        )
        return paths.config

    def _view_row(self, root: Path) -> dict[str, str]:
        with (root / "views/2026-08/transactions.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            return next(csv.DictReader(handle))

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_correction_publishes_saved_choice_and_changed_view_as_one_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            transaction_id = self._view_row(root)["transaction_id"]

            result = apply_workspace_corrections(
                {
                    transaction_id: {
                        "category": "Groceries",
                        "flow_type": "expense",
                        "confidence": "1.00",
                        "reason": "Reviewed locally",
                        "needs_review": "false",
                        "review_reasons": "",
                    }
                },
                config_path=config,
            )

            self.assertEqual(result.data["corrected_count"], 1)
            self.assertEqual(result.artifacts["views"][0]["period"], "2026-08")
            row = self._view_row(root)
            self.assertEqual(row["category"], "Groceries")
            self.assertEqual(row["needs_review"], "false")
            self.assertIn(transaction_id, (root / "corrections.csv").read_text())

            before = self._snapshot(root)
            repeated = apply_workspace_corrections(
                {
                    transaction_id: {
                        "category": "Groceries",
                        "flow_type": "expense",
                        "confidence": "1.00",
                        "reason": "Reviewed locally",
                        "needs_review": "false",
                        "review_reasons": "",
                    }
                },
                config_path=config,
            )
            self.assertEqual(repeated.data["written_count"], 0)
            self.assertEqual(self._snapshot(root), before)

    def test_review_and_correction_file_use_the_clean_workspace_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            transaction_id = self._view_row(root)["transaction_id"]

            reviewed = self._run(
                "review",
                "--transaction",
                transaction_id,
                "--as",
                "expense",
                "--config",
                str(config),
                "--json",
            )

            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            self.assertEqual(json.loads(reviewed.stdout)["command"], "review")
            self.assertEqual(self._view_row(root)["flow_type"], "expense")

            batch = root / "batch.csv"
            batch.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": transaction_id,
                            "category": "Groceries",
                            "flow_type": "expense",
                            "confidence": "1.00",
                            "reason": "Reviewed locally",
                            "needs_review": "false",
                            "review_reasons": "",
                        }
                    ],
                ),
                encoding="utf-8",
            )
            corrected = self._run(
                "correct",
                "--file",
                str(batch),
                "--config",
                str(config),
                "--json",
            )

            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            self.assertEqual(json.loads(corrected.stdout)["command"], "correct")
            self.assertEqual(self._view_row(root)["category"], "Groceries")

    def test_rate_observations_refresh_the_complete_affected_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Foreign Item,-10.00,USD\n",
                encoding="utf-8",
            )
            import_workspace(source, config_path=paths.config, interactive=False)
            self.assertEqual(self._view_row(root)["amount_hkd"], "-78.00")
            document = json.dumps(
                {
                    "header": {
                        "success": True,
                        "err_code": "0000",
                        "err_msg": "No error found",
                    },
                    "result": {
                        "datasize": 1,
                        "records": [{"end_of_day": "2026-08-08", "usd": 8}],
                    },
                }
            ).encode()
            observations = parse_hkma_daily_document(document, base_currency="HKD")

            result = apply_workspace_rate_observations(
                observations,
                config_path=paths.config,
            )

            self.assertEqual(result.data["imported_observation_count"], 1)
            self.assertEqual(result.artifacts["views"][0]["period"], "2026-08")
            self.assertEqual(self._view_row(root)["amount_hkd"], "-80.00")

            downloaded = root / "hkma.json"
            downloaded.write_bytes(document)
            imported = self._run(
                "rates",
                "import",
                str(downloaded),
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(json.loads(imported.stdout)["command"], "rates.import")

            refused = self._run(
                "rates",
                "fetch",
                "USD",
                "--start",
                "2026-08-08",
                "--end",
                "2026-08-08",
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(json.loads(refused.stdout)["command"], "rates.fetch")

    def test_correction_refuses_an_outside_input_edit_until_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._workspace(root)
            transaction_id = self._view_row(root)["transaction_id"]
            rules = root / "rules.json"
            rules.write_text(rules.read_text() + " ", encoding="utf-8")
            before = self._snapshot(root)

            with self.assertRaises(WorkspaceCommandError) as raised:
                apply_workspace_corrections(
                    {transaction_id: {"notes": "local note"}},
                    config_path=config,
                )

            self.assertEqual(raised.exception.code, "full_rebuild_required")
            self.assertEqual(self._snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
