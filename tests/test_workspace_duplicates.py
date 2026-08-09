from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class WorkspaceDuplicateAcceptanceTest(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_membership_choice_is_saved_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            setup = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config = root / "config.json"
            sources = root / "sources"
            sources.mkdir()
            header = "Date,Description,Amount,Currency\n"
            row = "2026-08-08,Synthetic Equal Item,-12.00,HKD\n"
            (sources / "one.csv").write_text(header + row + row, encoding="utf-8")
            (sources / "two.csv").write_text(header + row, encoding="utf-8")
            imported = self._run(
                "import",
                str(sources),
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            listed = self._run("duplicates", "--config", str(config), "--json")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            payload = json.loads(listed.stdout)
            self.assertEqual(payload["data"]["duplicate_group_count"], 1)
            group_id = payload["data"]["groups"][0]["group_id"]

            resolved = self._run(
                "duplicates",
                "resolve",
                group_id,
                "--as",
                "same-event",
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            result = json.loads(resolved.stdout)
            self.assertEqual(result["data"]["remaining_duplicate_group_count"], 0)
            self.assertFalse(result["data"]["idempotent"])
            document = (root / "views/2026-08/transactions.csv").read_text()
            self.assertEqual(len(list(csv.DictReader(io.StringIO(document)))), 1)

            repeated = self._run(
                "duplicates",
                "resolve",
                group_id,
                "--as",
                "same-event",
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertTrue(json.loads(repeated.stdout)["data"]["idempotent"])

            conflict = self._run(
                "duplicates",
                "resolve",
                group_id,
                "--as",
                "keep-all",
                "--config",
                str(config),
                "--json",
            )
            self.assertEqual(conflict.returncode, 2)
            self.assertEqual(
                json.loads(conflict.stdout)["errors"][0]["code"],
                "duplicate_resolution_conflict",
            )


if __name__ == "__main__":
    unittest.main()
