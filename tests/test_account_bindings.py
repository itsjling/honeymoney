import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class AccountBindingWorkflowTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        filesystem_fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        python_paths = []
        if filesystem_fault is not None:
            python_paths.append(REPO_ROOT / "tests" / "fault_injection")
            env["HONEYMONEY_TEST_FS_FAULT"] = filesystem_fault
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

    def _setup_workspace(self, temporary_root: str) -> Path:
        root = Path(temporary_root) / "money"
        result = self._run_cli(["setup", "--root", str(root)], cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _bind(
        self,
        root: Path,
        binding_id: str,
        pattern: str,
        owner: str,
        account_id: str,
        account: str,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_cli(
            [
                "profile",
                "bind",
                binding_id,
                "--pattern",
                pattern,
                "--profile",
                "starter_csv",
                "--owner",
                owner,
                "--account",
                f"starter_csv={account_id}={account}",
                "--json",
            ],
            cwd=root,
        )

    def test_cli_creates_lists_and_reuses_two_bindings_for_one_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statements = root / "shared-layout"
            statements.mkdir()
            (statements / "justin-may.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-01,SYNTHETIC JUSTIN,-10.00,HKD\n",
                encoding="utf-8",
            )
            (statements / "franchesca-may.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-02,SYNTHETIC FRANCHESCA,-20.00,HKD\n",
                encoding="utf-8",
            )
            parser_profile = root / "profiles" / "starter_csv.json"
            parser_profile_before = parser_profile.read_bytes()

            justin = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                " Justin ",
                "justin_local",
                "Justin Local Account",
            )
            franchesca = self._bind(
                root,
                "franchesca-local",
                "franchesca-*.csv",
                "Franchesca",
                "franchesca_local",
                "Franchesca Local Account",
            )

            self.assertEqual(justin.returncode, 0, justin.stderr)
            self.assertEqual(franchesca.returncode, 0, franchesca.stderr)
            justin_payload = json.loads(justin.stdout)
            self.assertEqual(justin_payload["command"], "profile.bind")
            self.assertEqual(justin_payload["status"], "success")
            self.assertEqual(
                justin_payload["data"]["binding"],
                {
                    "id": "justin-local",
                    "profile": "starter_csv",
                    "owner": "Justin",
                    "accounts": [
                        {
                            "source_account_id": "starter_csv",
                            "account_id": "justin_local",
                            "account": "Justin Local Account",
                        }
                    ],
                    "patterns": ["justin-*.csv"],
                },
            )
            self.assertEqual(
                justin_payload["artifacts"]["profile_mappings_json"],
                str((root / "profile_mappings.json").resolve()),
            )
            self.assertEqual(parser_profile.read_bytes(), parser_profile_before)
            listed = self._run_cli(["profile", "bindings", "--json"], cwd=root)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            payload = json.loads(listed.stdout)
            self.assertEqual(payload["command"], "profile.bindings")
            self.assertEqual(
                [item["id"] for item in payload["data"]["bindings"]],
                ["franchesca-local", "justin-local"],
            )

            interactive_import = self._run_cli(
                ["import", str(statements / "justin-may.csv")],
                cwd=root,
                input_text="q\n",
            )
            self.assertEqual(
                interactive_import.returncode, 0, interactive_import.stderr
            )
            imported = self._run_cli(
                [
                    "import",
                    str(statements / "franchesca-may.csv"),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            import_payload = json.loads(imported.stdout)
            self.assertEqual(
                import_payload["data"]["files"][0]["binding_id"],
                "franchesca-local",
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {(row["owner"], row["account_id"], row["account"]) for row in rows},
                {
                    ("Justin", "justin_local", "Justin Local Account"),
                    (
                        "Franchesca",
                        "franchesca_local",
                        "Franchesca Local Account",
                    ),
                },
            )

    def test_incomplete_and_colliding_bindings_fail_without_changing_mappings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()

            incomplete = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-hsbc-one",
                    "--pattern",
                    "justin-hsbc-*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Justin",
                    "--account",
                    "hsbc_one_hkd_savings=justin_hsbc_savings=Justin HSBC Savings",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(incomplete.returncode, 2, incomplete.stderr)
            incomplete_error = json.loads(incomplete.stdout)["errors"][0]["message"]
            self.assertIn("does not cover profile hsbc_one_pdf", incomplete_error)
            self.assertIn("hsbc_one_hkd_current", incomplete_error)
            self.assertEqual(mappings_path.read_bytes(), before)

            first = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "Shared_Account_ID",
                "Justin Local Account",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = mappings_path.read_bytes()
            collision = self._bind(
                root,
                "franchesca-local",
                "franchesca-*.csv",
                "Franchesca",
                "shared_account_id",
                "Franchesca Local Account",
            )
            self.assertEqual(collision.returncode, 2, collision.stderr)
            collision_error = json.loads(collision.stdout)["errors"][0]["message"]
            self.assertIn("Account identity collision", collision_error)
            self.assertNotIn("Shared_Account_ID", collision_error)
            self.assertNotIn("shared_account_id", collision_error)
            self.assertNotIn("Local Account", collision_error)
            self.assertEqual(mappings_path.read_bytes(), after_first)

    def test_sectioned_profile_binds_every_emitted_account_to_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statement = root / "justin-hsbc-one.pdf"
            fixture = (
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "import_profiles"
                / "hsbc_one_pdf"
                / "accepted_statement"
                / "input.pdf"
            )
            statement.write_bytes(fixture.read_bytes())

            bound = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-hsbc-one",
                    "--pattern",
                    "justin-hsbc-*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Justin",
                    "--account",
                    "hsbc_one_hkd_savings=justin_hsbc_savings=Justin HSBC Savings",
                    "--account",
                    "hsbc_one_hkd_current=justin_hsbc_current=Justin HSBC Current",
                    "--account",
                    "hsbc_one_fcy_savings=justin_hsbc_foreign=Justin HSBC Foreign Currency",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertEqual({row["owner"] for row in rows}, {"Justin"})
            self.assertEqual(
                {row["account_id"] for row in rows},
                {
                    "justin_hsbc_savings",
                    "justin_hsbc_current",
                    "justin_hsbc_foreign",
                },
            )
            self.assertEqual(
                {row["account"] for row in rows},
                {
                    "Justin HSBC Savings",
                    "Justin HSBC Current",
                    "Justin HSBC Foreign Currency",
                },
            )

    def test_cli_replaces_one_binding_pattern_and_replay_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            bound = self._bind(
                root,
                "justin-local",
                "justin-old-*.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            mappings_path = root / "profile_mappings.json"
            before = json.loads(mappings_path.read_text(encoding="utf-8"))

            args = [
                "profile",
                "replace-pattern",
                "justin-local",
                "--old-pattern",
                "justin-old-*.csv",
                "--new-pattern",
                "justin-new-*.csv",
                "--json",
            ]
            replaced = self._run_cli(args, cwd=root)

            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            payload = json.loads(replaced.stdout)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["command"], "profile.replace-pattern")
            self.assertEqual(
                payload["data"],
                {
                    "binding_id": "justin-local",
                    "changed": True,
                    "new_pattern": "justin-new-*.csv",
                    "old_pattern": "justin-old-*.csv",
                    "profile": "starter_csv",
                    "result": "replaced",
                },
            )
            after = json.loads(mappings_path.read_text(encoding="utf-8"))
            self.assertEqual(after["account_bindings"], before["account_bindings"])
            self.assertEqual(
                after["filename_patterns"],
                [
                    {
                        "binding": "justin-local",
                        "pattern": "justin-new-*.csv",
                        "profile": "starter_csv",
                    }
                ],
            )
            self.assertEqual(
                after["replaced_filename_patterns"],
                [
                    {
                        "binding": "justin-local",
                        "new_pattern": "justin-new-*.csv",
                        "old_pattern": "justin-old-*.csv",
                        "profile": "starter_csv",
                    }
                ],
            )

            written_inode = mappings_path.stat().st_ino
            written_bytes = mappings_path.read_bytes()
            replayed = self._run_cli(args, cwd=root)

            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            replay_payload = json.loads(replayed.stdout)
            self.assertFalse(replay_payload["data"]["changed"])
            self.assertEqual(replay_payload["data"]["result"], "already_replaced")
            self.assertEqual(mappings_path.stat().st_ino, written_inode)
            self.assertEqual(mappings_path.read_bytes(), written_bytes)

    def test_cli_removes_one_pattern_and_keeps_the_used_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            for pattern in ("justin-old-*.csv", "justin-current-*.csv"):
                bound = self._bind(
                    root,
                    "justin-local",
                    pattern,
                    "Justin",
                    "justin_local",
                    "Justin Local Account",
                )
                self.assertEqual(bound.returncode, 0, bound.stderr)
            mappings_path = root / "profile_mappings.json"

            args = [
                "profile",
                "remove-pattern",
                "justin-local",
                "--pattern",
                "justin-old-*.csv",
                "--json",
            ]
            removed = self._run_cli(args, cwd=root)

            self.assertEqual(removed.returncode, 0, removed.stderr)
            payload = json.loads(removed.stdout)
            self.assertEqual(payload["command"], "profile.remove-pattern")
            self.assertEqual(
                payload["data"],
                {
                    "binding_id": "justin-local",
                    "binding_removed": False,
                    "changed": True,
                    "pattern": "justin-old-*.csv",
                    "profile": "starter_csv",
                    "result": "removed",
                },
            )
            mappings = json.loads(mappings_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [binding["id"] for binding in mappings["account_bindings"]],
                ["justin-local"],
            )
            self.assertEqual(
                mappings["filename_patterns"],
                [
                    {
                        "binding": "justin-local",
                        "pattern": "justin-current-*.csv",
                        "profile": "starter_csv",
                    }
                ],
            )

            written_inode = mappings_path.stat().st_ino
            written_bytes = mappings_path.read_bytes()
            replayed = self._run_cli(args, cwd=root)

            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            replay_payload = json.loads(replayed.stdout)
            self.assertFalse(replay_payload["data"]["changed"])
            self.assertEqual(replay_payload["data"]["result"], "already_removed")
            self.assertEqual(mappings_path.stat().st_ino, written_inode)
            self.assertEqual(mappings_path.read_bytes(), written_bytes)

    def test_cli_requires_confirmation_to_remove_the_final_pattern_and_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            bound = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()
            base_args = [
                "profile",
                "remove-pattern",
                "justin-local",
                "--pattern",
                "justin-*.csv",
                "--json",
            ]

            unconfirmed = self._run_cli(base_args, cwd=root)

            self.assertEqual(unconfirmed.returncode, 2, unconfirmed.stderr)
            error_payload = json.loads(unconfirmed.stdout)
            self.assertEqual(error_payload["schema_version"], 2)
            self.assertEqual(error_payload["command"], "profile.remove-pattern")
            self.assertIn(
                "pass --yes to confirm", error_payload["errors"][0]["message"]
            )
            self.assertEqual(mappings_path.read_bytes(), before)

            confirmed = self._run_cli([*base_args[:-1], "--yes", "--json"], cwd=root)

            self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
            payload = json.loads(confirmed.stdout)
            self.assertTrue(payload["data"]["binding_removed"])
            self.assertTrue(payload["data"]["changed"])
            mappings = json.loads(mappings_path.read_text(encoding="utf-8"))
            self.assertEqual(mappings["account_bindings"], [])
            self.assertEqual(mappings["filename_patterns"], [])
            self.assertEqual(
                mappings["removed_filename_patterns"],
                [
                    {
                        "binding": "justin-local",
                        "pattern": "justin-*.csv",
                        "profile": "starter_csv",
                    }
                ],
            )

            written_inode = mappings_path.stat().st_ino
            written_bytes = mappings_path.read_bytes()
            replayed = self._run_cli(base_args, cwd=root)

            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            replay_payload = json.loads(replayed.stdout)
            self.assertTrue(replay_payload["data"]["binding_removed"])
            self.assertFalse(replay_payload["data"]["changed"])
            self.assertEqual(replay_payload["data"]["result"], "already_removed")
            self.assertEqual(mappings_path.stat().st_ino, written_inode)
            self.assertEqual(mappings_path.read_bytes(), written_bytes)

    def test_recreating_a_binding_invalidates_its_removal_replay_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            removed_pattern = "justin-old-*.csv"
            first = self._bind(
                root,
                "justin-local",
                removed_pattern,
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            removed = self._run_cli(
                [
                    "profile",
                    "remove-pattern",
                    "justin-local",
                    "--pattern",
                    removed_pattern,
                    "--yes",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            recreated = self._bind(
                root,
                "justin-local",
                "justin-new-*.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(recreated.returncode, 0, recreated.stderr)
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()

            stale_replay = self._run_cli(
                [
                    "profile",
                    "remove-pattern",
                    "justin-local",
                    "--pattern",
                    removed_pattern,
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(stale_replay.returncode, 2, stale_replay.stderr)
            self.assertEqual(
                json.loads(stale_replay.stdout)["errors"][0]["message"],
                "Account binding justin-local does not use filename pattern "
                f"{removed_pattern}",
            )
            self.assertEqual(mappings_path.read_bytes(), before)

    def test_pattern_edit_receipts_are_kept_for_other_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            bindings = (
                ("justin-local", "justin", "Justin", "justin_local"),
                (
                    "franchesca-local",
                    "franchesca",
                    "Franchesca",
                    "franchesca_local",
                ),
            )
            for binding_id, prefix, owner, account_id in bindings:
                bound = self._bind(
                    root,
                    binding_id,
                    f"{prefix}-old-*.csv",
                    owner,
                    account_id,
                    f"{owner} Local Account",
                )
                self.assertEqual(bound.returncode, 0, bound.stderr)
                replaced = self._run_cli(
                    [
                        "profile",
                        "replace-pattern",
                        binding_id,
                        "--old-pattern",
                        f"{prefix}-old-*.csv",
                        "--new-pattern",
                        f"{prefix}-new-*.csv",
                        "--json",
                    ],
                    cwd=root,
                )
                self.assertEqual(replaced.returncode, 0, replaced.stderr)

            replacement_replay = self._run_cli(
                [
                    "profile",
                    "replace-pattern",
                    "justin-local",
                    "--old-pattern",
                    "justin-old-*.csv",
                    "--new-pattern",
                    "justin-new-*.csv",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(
                replacement_replay.returncode, 0, replacement_replay.stderr
            )
            self.assertFalse(json.loads(replacement_replay.stdout)["data"]["changed"])

            for binding_id, prefix, owner, account_id in bindings:
                extra = self._bind(
                    root,
                    binding_id,
                    f"{prefix}-extra-*.csv",
                    owner,
                    account_id,
                    f"{owner} Local Account",
                )
                self.assertEqual(extra.returncode, 0, extra.stderr)
                removed = self._run_cli(
                    [
                        "profile",
                        "remove-pattern",
                        binding_id,
                        "--pattern",
                        f"{prefix}-extra-*.csv",
                        "--json",
                    ],
                    cwd=root,
                )
                self.assertEqual(removed.returncode, 0, removed.stderr)

            removal_replay = self._run_cli(
                [
                    "profile",
                    "remove-pattern",
                    "justin-local",
                    "--pattern",
                    "justin-extra-*.csv",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(removal_replay.returncode, 0, removal_replay.stderr)
            self.assertFalse(json.loads(removal_replay.stdout)["data"]["changed"])

    def test_profile_bind_can_repair_a_binding_after_its_profile_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            first = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["account_id"] = "starter_csv_v2"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            repaired = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-local",
                    "--pattern",
                    "justin-*.csv",
                    "--profile",
                    "starter_csv",
                    "--owner",
                    "Justin",
                    "--account",
                    "starter_csv_v2=justin_local=Justin Local Account",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            payload = json.loads(repaired.stdout)
            self.assertEqual(
                payload["data"]["binding"]["accounts"][0]["source_account_id"],
                "starter_csv_v2",
            )

    def test_pattern_edits_reject_missing_targets_and_conflicts_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            justin = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            franchesca = self._bind(
                root,
                "franchesca-local",
                "franchesca-*.csv",
                "Franchesca",
                "franchesca_local",
                "Franchesca Local Account",
            )
            self.assertEqual(justin.returncode, 0, justin.stderr)
            self.assertEqual(franchesca.returncode, 0, franchesca.stderr)
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()

            missing_binding = self._run_cli(
                [
                    "profile",
                    "replace-pattern",
                    "missing-binding",
                    "--old-pattern",
                    "missing-old-*.csv",
                    "--new-pattern",
                    "missing-new-*.csv",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(missing_binding.returncode, 2, missing_binding.stderr)
            missing_binding_payload = json.loads(missing_binding.stdout)
            self.assertEqual(missing_binding_payload["schema_version"], 2)
            self.assertEqual(
                missing_binding_payload["errors"][0]["message"],
                "Unknown account binding: missing-binding",
            )
            self.assertEqual(mappings_path.read_bytes(), before)

            unproved_replay = self._run_cli(
                [
                    "profile",
                    "replace-pattern",
                    "justin-local",
                    "--old-pattern",
                    "never-used-*.csv",
                    "--new-pattern",
                    "justin-*.csv",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(unproved_replay.returncode, 2, unproved_replay.stderr)
            self.assertEqual(
                json.loads(unproved_replay.stdout)["errors"][0]["message"],
                "Account binding justin-local does not use filename pattern "
                "never-used-*.csv",
            )
            self.assertEqual(mappings_path.read_bytes(), before)

            missing_pattern = self._run_cli(
                [
                    "profile",
                    "remove-pattern",
                    "justin-local",
                    "--pattern",
                    "missing-*.csv",
                ],
                cwd=root,
            )
            self.assertEqual(missing_pattern.returncode, 2, missing_pattern.stderr)
            self.assertEqual(missing_pattern.stdout, "")
            self.assertIn(
                "Account binding justin-local does not use filename pattern "
                "missing-*.csv",
                missing_pattern.stderr,
            )
            self.assertEqual(mappings_path.read_bytes(), before)

            conflict = self._run_cli(
                [
                    "profile",
                    "replace-pattern",
                    "justin-local",
                    "--old-pattern",
                    "justin-*.csv",
                    "--new-pattern",
                    "franchesca-*.csv",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(conflict.returncode, 2, conflict.stderr)
            conflict_payload = json.loads(conflict.stdout)
            self.assertEqual(conflict_payload["command"], "profile.replace-pattern")
            self.assertEqual(
                conflict_payload["errors"][0]["message"],
                "Filename pattern franchesca-*.csv already selects another "
                "profile or binding",
            )
            self.assertEqual(mappings_path.read_bytes(), before)

    def test_pattern_edit_write_failures_leave_mappings_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            for pattern in ("justin-old-*.csv", "justin-current-*.csv"):
                bound = self._bind(
                    root,
                    "justin-local",
                    pattern,
                    "Justin",
                    "justin_local",
                    "Justin Local Account",
                )
                self.assertEqual(bound.returncode, 0, bound.stderr)
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()

            commands = (
                [
                    "profile",
                    "replace-pattern",
                    "justin-local",
                    "--old-pattern",
                    "justin-old-*.csv",
                    "--new-pattern",
                    "justin-new-*.csv",
                    "--json",
                ],
                [
                    "profile",
                    "remove-pattern",
                    "justin-local",
                    "--pattern",
                    "justin-old-*.csv",
                    "--json",
                ],
            )
            for command in commands:
                with self.subTest(command=command[1]):
                    failed = self._run_cli(
                        command,
                        cwd=root,
                        filesystem_fault="replace-before:profile_mappings.json",
                    )
                    self.assertEqual(failed.returncode, 2, failed.stderr)
                    self.assertIn(
                        "synthetic replacement failure",
                        json.loads(failed.stdout)["errors"][0]["message"],
                    )
                    self.assertEqual(mappings_path.read_bytes(), before)

    def test_pattern_edits_change_only_profile_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            for pattern in ("justin-old-*.csv", "justin-current-*.csv"):
                bound = self._bind(
                    root,
                    "justin-local",
                    pattern,
                    "Justin",
                    "justin_local",
                    "Justin Local Account",
                )
                self.assertEqual(bound.returncode, 0, bound.stderr)
            output = root / "output"
            output.mkdir(exist_ok=True)
            artifacts = {
                output / "categorized.csv": b"synthetic ledger sentinel\n",
                root / "corrections.csv": b"synthetic corrections sentinel\n",
                output / "report.html": b"synthetic report sentinel\n",
                output / "import_report.json": b'{"synthetic":"report sentinel"}\n',
            }
            for path, content in artifacts.items():
                path.write_bytes(content)

            replaced = self._run_cli(
                [
                    "profile",
                    "replace-pattern",
                    "justin-local",
                    "--old-pattern",
                    "justin-old-*.csv",
                    "--new-pattern",
                    "justin-new-*.csv",
                    "--json",
                ],
                cwd=root,
            )
            removed = self._run_cli(
                [
                    "profile",
                    "remove-pattern",
                    "justin-local",
                    "--pattern",
                    "justin-current-*.csv",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in artifacts},
                artifacts,
            )

    def test_replace_under_new_binding_preserves_review_and_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statement = root / "justin-existing.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-03,SYNTHETIC SALARY,100.00,HKD\n",
                encoding="utf-8",
            )
            first_import = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(first_import.returncode, 0, first_import.stderr)
            ledger_path = root / "output" / "categorized.csv"
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                imported_row = next(csv.DictReader(handle))

            reviewed = self._run_cli(
                [
                    "review",
                    "--transaction",
                    imported_row["transaction_id"],
                    "--as",
                    "income",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                reviewed_row = next(csv.DictReader(handle))
            owner_correction_path = root / "owner-correction.json"
            owner_correction_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": reviewed_row["transaction_id"],
                            "owner": "Franchesca",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            owner_corrected = self._run_cli(
                ["correct", "--file", str(owner_correction_path), "--json"],
                cwd=root,
            )
            self.assertEqual(owner_corrected.returncode, 0, owner_corrected.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                reviewed_row = next(csv.DictReader(handle))
            self.assertEqual(reviewed_row["owner"], "Franchesca")
            protected_fields = {
                field: reviewed_row[field]
                for field in (
                    "category",
                    "flow_type",
                    "flow_source",
                    "needs_review",
                    "review_reasons",
                    "reason",
                    "confidence",
                )
            }
            self.assertEqual(protected_fields["category"], "Income")
            self.assertEqual(protected_fields["flow_type"], "income")

            bound = self._bind(
                root,
                "justin-local",
                "justin-existing.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
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
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                rebound_row = next(csv.DictReader(handle))
            self.assertEqual(rebound_row["owner"], "Justin")
            self.assertEqual(rebound_row["account_id"], "justin_local")
            self.assertEqual(rebound_row["account"], "Justin Local Account")
            self.assertEqual(
                {field: rebound_row[field] for field in protected_fields},
                protected_fields,
            )
            self.assertNotEqual(
                rebound_row["transaction_id"], reviewed_row["transaction_id"]
            )
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                corrections = list(csv.DictReader(handle))
            self.assertEqual(
                [row["transaction_id"] for row in corrections],
                [rebound_row["transaction_id"]],
            )
            self.assertEqual(corrections[0]["owner"], "Justin")

            notes_path = root / "notes-correction.json"
            notes_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": rebound_row["transaction_id"],
                            "notes": "Synthetic note",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            notes_corrected = self._run_cli(
                ["correct", "--file", str(notes_path), "--json"],
                cwd=root,
            )
            self.assertEqual(notes_corrected.returncode, 0, notes_corrected.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                notes_row = next(csv.DictReader(handle))
            self.assertEqual(notes_row["owner"], "Justin")
            self.assertEqual(notes_row["notes"], "Synthetic note")

    def test_binding_owner_does_not_change_an_unbound_matching_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statements = root / "same-account-id"
            statements.mkdir()
            (statements / "justin-bound.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-05,SYNTHETIC BOUND,-10.00,HKD\n",
                encoding="utf-8",
            )
            (statements / "plain-unbound.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-06,SYNTHETIC UNBOUND,-20.00,HKD\n",
                encoding="utf-8",
            )
            bound = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "starter_csv",
                "Starter Account",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)

            imported = self._run_cli(
                ["import", str(statements), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["original_description"]: row["owner"] for row in rows},
                {
                    "SYNTHETIC BOUND": "Justin",
                    "SYNTHETIC UNBOUND": "Household",
                },
            )

            bound_row = next(
                row for row in rows if row["original_description"] == "SYNTHETIC BOUND"
            )
            correction_path = root / "retained-owner-correction.json"
            correction_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": bound_row["transaction_id"],
                            "owner": "Franchesca",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            corrected = self._run_cli(
                ["correct", "--file", str(correction_path), "--json"],
                cwd=root,
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            (statements / "plain-duplicate.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-05,SYNTHETIC BOUND,-10.00,HKD\n",
                encoding="utf-8",
            )
            duplicate_import = self._run_cli(
                [
                    "import",
                    str(statements / "plain-duplicate.csv"),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(duplicate_import.returncode, 0, duplicate_import.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows_after_duplicate = list(csv.DictReader(handle))
            retained_bound_row = next(
                row
                for row in rows_after_duplicate
                if row["original_description"] == "SYNTHETIC BOUND"
            )
            self.assertEqual(retained_bound_row["owner"], "Justin")

    def test_import_rejects_uncovered_dynamic_account_and_conflicting_matches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["csv"]["columns"]["account_id"] = "Account ID"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            statement = root / "dynamic-may.csv"
            statement.write_text(
                "Date,Description,Amount,Currency,Account ID\n"
                "2026-05-04,PRIVATE SENTINEL,-987.65,HKD,uncovered_account\n",
                encoding="utf-8",
            )
            bound = self._run_cli(
                [
                    "profile",
                    "bind",
                    "dynamic-one",
                    "--pattern",
                    "dynamic-*.csv",
                    "--profile",
                    "starter_csv",
                    "--owner",
                    "Justin",
                    "--account",
                    "known_source=justin_known=Justin Known Account",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)

            incomplete = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(incomplete.returncode, 2, incomplete.stderr)
            incomplete_message = json.loads(incomplete.stdout)["errors"][0]["message"]
            self.assertIn("does not cover 1 emitted account id", incomplete_message)
            self.assertNotIn("uncovered_account", incomplete.stdout)
            self.assertNotIn("PRIVATE SENTINEL", incomplete.stdout)
            self.assertNotIn("987.65", incomplete.stdout)
            self.assertFalse((root / "output" / "categorized.csv").exists())

            second = self._run_cli(
                [
                    "profile",
                    "bind",
                    "dynamic-two",
                    "--pattern",
                    "*.csv",
                    "--profile",
                    "starter_csv",
                    "--owner",
                    "Franchesca",
                    "--account",
                    "uncovered_account=franchesca_dynamic=Franchesca Dynamic Account",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            conflicting = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(conflicting.returncode, 2, conflicting.stderr)
            conflict_message = json.loads(conflicting.stdout)["errors"][0]["message"]
            self.assertEqual(
                conflict_message,
                "Conflicting filename mappings for dynamic-may.csv",
            )
            self.assertNotIn("PRIVATE SENTINEL", conflicting.stdout)
            self.assertFalse((root / "output" / "categorized.csv").exists())

    def test_pdf_binding_conflict_is_an_error_not_a_skipped_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statement = root / "justin-conflict.pdf"
            fixture = (
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "import_profiles"
                / "hsbc_one_pdf"
                / "accepted_statement"
                / "input.pdf"
            )
            statement.write_bytes(fixture.read_bytes())
            first = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-hsbc",
                    "--pattern",
                    "justin-*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Justin",
                    "--account",
                    "hsbc_one_hkd_savings=justin_savings=Justin Savings",
                    "--account",
                    "hsbc_one_hkd_current=justin_current=Justin Current",
                    "--account",
                    "hsbc_one_fcy_savings=justin_foreign=Justin Foreign",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self._run_cli(
                [
                    "profile",
                    "bind",
                    "franchesca-hsbc",
                    "--pattern",
                    "*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Franchesca",
                    "--account",
                    "hsbc_one_hkd_savings=franchesca_savings=Franchesca Savings",
                    "--account",
                    "hsbc_one_hkd_current=franchesca_current=Franchesca Current",
                    "--account",
                    "hsbc_one_fcy_savings=franchesca_foreign=Franchesca Foreign",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            result = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(
                payload["errors"][0]["message"],
                "Conflicting filename mappings for justin-conflict.pdf",
            )
            self.assertFalse((root / "output" / "categorized.csv").exists())


if __name__ == "__main__":
    unittest.main()
