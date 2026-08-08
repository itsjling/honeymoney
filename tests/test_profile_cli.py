from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.workspace_setup import setup_workspace


class ProfileCliTest(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_validate_is_read_only_and_input_preview_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "profile",
                    "validate",
                    str(paths.profiles / "starter_csv.json"),
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["command"], "profile.validate")
            self.assertEqual(payload["data"]["profile_id"], "starter_csv")
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

            removed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "honeymoney.cli",
                    "profile",
                    "validate",
                    str(paths.profiles / "starter_csv.json"),
                    "--input",
                    str(root / "never-read.csv"),
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(removed.returncode, 2)
            self.assertEqual(
                json.loads(removed.stdout)["errors"][0]["code"], "usage_error"
            )

    def test_json_binding_error_does_not_echo_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            private_value = "private-owner-value"

            result = self._run(
                "profile",
                "bind",
                "household",
                "--pattern",
                "synthetic-*.csv",
                "--profile",
                "starter_csv",
                "--owner",
                private_value,
                "--account",
                "starter_csv=checking=Checking",
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(result.returncode, 2)
            self.assertNotIn(private_value, result.stdout)
            self.assertEqual(
                json.loads(result.stdout)["errors"][0]["message"],
                "Profile mappings are invalid.",
            )

    def test_account_binding_commands_keep_the_public_management_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            inputs = paths.root / "inputs"
            inputs.mkdir(mode=0o700)
            mappings_path = inputs / "mappings.json"
            mappings_path.write_bytes(paths.profile_mappings.read_bytes())
            os.chmod(mappings_path, 0o600)
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            config["profile_mappings"] = "inputs/mappings.json"
            paths.config.write_text(json.dumps(config), encoding="utf-8")
            common = ("--config", str(paths.config), "--json")

            bound = self._run(
                "profile",
                "bind",
                "household",
                "--pattern",
                "synthetic-*.csv",
                "--profile",
                "starter_csv",
                "--owner",
                "Household",
                "--account",
                "starter_csv=checking=Checking",
                *common,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            bound_payload = json.loads(bound.stdout)
            self.assertEqual(bound_payload["command"], "profile.bind")
            self.assertEqual(
                bound_payload["artifacts"]["profile_mappings_json"], str(mappings_path)
            )
            self.assertIn("household", mappings_path.read_text(encoding="utf-8"))
            self.assertNotIn(
                "household", paths.profile_mappings.read_text(encoding="utf-8")
            )

            listed = self._run("profile", "bindings", *common)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            bindings = json.loads(listed.stdout)["data"]["bindings"]
            self.assertEqual(bindings[0]["patterns"], ["synthetic-*.csv"])

            replaced = self._run(
                "profile",
                "replace-pattern",
                "household",
                "--old-pattern",
                "synthetic-*.csv",
                "--new-pattern",
                "household-*.csv",
                *common,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertTrue(json.loads(replaced.stdout)["data"]["changed"])

            before = mappings_path.read_bytes()
            unconfirmed = self._run(
                "profile",
                "remove-pattern",
                "household",
                "--pattern",
                "household-*.csv",
                *common,
            )
            self.assertEqual(unconfirmed.returncode, 2)
            self.assertEqual(mappings_path.read_bytes(), before)

            removed = self._run(
                "profile",
                "remove-pattern",
                "household",
                "--pattern",
                "household-*.csv",
                "--yes",
                *common,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertTrue(json.loads(removed.stdout)["data"]["binding_removed"])

    def test_bind_can_repair_a_saved_binding_after_a_profile_account_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            common = ("--config", str(paths.config), "--json")
            original = self._run(
                "profile",
                "bind",
                "household",
                "--pattern",
                "synthetic-*.csv",
                "--profile",
                "starter_csv",
                "--owner",
                "Household",
                "--account",
                "starter_csv=checking=Checking",
                *common,
            )
            self.assertEqual(original.returncode, 0, original.stderr)
            profile = json.loads(
                (paths.profiles / "starter_csv.json").read_text(encoding="utf-8")
            )
            profile["account_id"] = "starter_csv_v2"
            (paths.profiles / "starter_csv.json").write_text(
                json.dumps(profile), encoding="utf-8"
            )
            rebuilt = self._run(
                "views", "rebuild", "--all", "--config", str(paths.config), "--json"
            )
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)

            repaired = self._run(
                "profile",
                "bind",
                "household",
                "--pattern",
                "synthetic-*.csv",
                "--profile",
                "starter_csv",
                "--owner",
                "Household",
                "--account",
                "starter_csv_v2=checking=Checking",
                *common,
            )

            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            mappings = json.loads(paths.profile_mappings.read_text(encoding="utf-8"))
            self.assertEqual(
                mappings["account_bindings"][0]["accounts"][0]["source_account_id"],
                "starter_csv_v2",
            )

    def test_single_file_import_rederives_the_saved_binding_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            common = ("--config", str(paths.config), "--json")
            bound = self._run(
                "profile",
                "bind",
                "personal",
                "--pattern",
                "synthetic.csv",
                "--profile",
                "starter_csv",
                "--owner",
                "Justin",
                "--account",
                "starter_csv=justin_account=Personal account",
                *common,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            statement = paths.root / "synthetic.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-09,Synthetic purchase,-1.00,HKD\n",
                encoding="utf-8",
            )

            imported = self._run(
                "import",
                str(statement),
                "--binding",
                "personal",
                *common,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            with (paths.views / "2026-08" / "transactions.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                [row] = csv.DictReader(handle)
            self.assertEqual(row["owner"], "Justin")
            self.assertEqual(row["account_id"], "justin_account")


if __name__ == "__main__":
    unittest.main()
