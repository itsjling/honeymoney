import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from honeymoney import cli, persistence

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGED_FILES_NAME = ".honeymoney-managed-files.json"


class WorkspaceUpgradeTest(unittest.TestCase):
    def _run_cli(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            cwd=REPO_ROOT,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _install_recorded_prior_profile(
        self,
        root: Path,
        content: bytes,
        *,
        honeymoney_version: str | None = None,
    ) -> tuple[Path, bytes]:
        profile_path = root / "profiles" / "starter_csv.json"
        installed_profile = profile_path.read_bytes()
        profile_path.write_bytes(content)
        manifest_path = root / MANAGED_FILES_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if honeymoney_version is not None:
            manifest["honeymoney_version"] = honeymoney_version
        for entry in manifest["files"]:
            if entry["path"] == "profiles/starter_csv.json":
                entry["sha256"] = hashlib.sha256(content).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return profile_path, installed_profile

    def test_fresh_setup_records_versioned_managed_profile_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"

            result = self._run_cli(["setup", "--root", str(root), "--json"])

            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(
                (root / MANAGED_FILES_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["honeymoney_version"], "0.1.0")
            entries = {entry["path"]: entry for entry in manifest["files"]}
            profile_paths = sorted(
                path.relative_to(root).as_posix()
                for path in (root / "profiles").glob("*.json")
            )
            self.assertEqual(sorted(entries), profile_paths)
            for relative_path, entry in entries.items():
                profile_bytes = (root / relative_path).read_bytes()
                self.assertEqual(
                    entry,
                    {
                        "path": relative_path,
                        "origin": f"honeymoney.bundle/{relative_path}",
                        "sha256": hashlib.sha256(profile_bytes).hexdigest(),
                    },
                )

    def test_current_workspace_dry_run_is_noop_and_reports_safe_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            result = self._run_cli(
                [
                    "setup",
                    "--upgrade",
                    "--root",
                    str(root),
                    "--dry-run",
                    "--json",
                ]
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["command"], "setup")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["data"]["operation"], "upgrade")
            self.assertFalse(payload["data"]["applied"])
            self.assertTrue(payload["data"]["dry_run"])
            self.assertEqual(
                sorted(payload["data"]["result_counts"]),
                ["conflict", "create", "preserved", "unchanged", "update"],
            )
            profile_results = [
                item
                for item in payload["data"]["results"]
                if item["kind"] == "bundled_profile"
            ]
            self.assertTrue(profile_results)
            self.assertEqual(
                {item["result"] for item in profile_results},
                {"unchanged"},
            )
            self.assertTrue(
                any(
                    item["kind"] == "statement_input" and item["result"] == "preserved"
                    for item in payload["data"]["results"]
                )
            )
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_proven_old_profile_updates_without_changing_user_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)

            old_profile = b'{"id":"starter_csv","synthetic_old_bundle":true}\n'
            profile_path, installed_profile = self._install_recorded_prior_profile(
                root,
                old_profile,
                honeymoney_version="0.0.9",
            )

            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["custom_setting"] = {"synthetic": True}
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            sentinels = {
                root / "input" / "statement.txt": b"synthetic statement sentinel\n",
                root / "output" / "categorized.csv": b"synthetic ledger sentinel\n",
                root / "output" / "report.html": b"synthetic report sentinel\n",
                root / "corrections.csv": b"synthetic corrections sentinel\n",
                root / "rules.json": b'{"synthetic":"custom rules"}\n',
                root / "rates.json": b'{"synthetic":"cached rates"}\n',
                root / "profile_mappings.json": b'{"synthetic":"custom mappings"}\n',
            }
            for path, content in sentinels.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            preserved_before = {
                path: path.read_bytes() for path in [config_path, *sentinels]
            }

            result = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["data"]["applied"])
            self.assertTrue(
                any(
                    item["path"] == "profiles/starter_csv.json"
                    and item["result"] == "update"
                    for item in payload["data"]["results"]
                )
            )
            self.assertEqual(profile_path.read_bytes(), installed_profile)
            self.assertEqual(
                {path: path.read_bytes() for path in preserved_before},
                preserved_before,
            )

            completed_bytes = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            repeated = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--json"]
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["data"]["changed"])
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                completed_bytes,
            )

    def test_locally_modified_managed_profile_is_a_byte_preserving_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            profile_path = root / "profiles" / "starter_csv.json"
            local_content = b'{"id":"starter_csv","user_change":"keep"}\n'
            profile_path.write_bytes(local_content)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            result = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "partial_success")
            self.assertFalse(payload["data"]["applied"])
            self.assertTrue(
                any(
                    item["path"] == "profiles/starter_csv.json"
                    and item["result"] == "conflict"
                    for item in payload["data"]["results"]
                )
            )
            self.assertEqual(profile_path.read_bytes(), local_content)
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

    def test_legacy_workspace_never_infers_ownership_from_a_profile_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            (root / MANAGED_FILES_NAME).unlink()
            profile_path = root / "profiles" / "starter_csv.json"
            legacy_content = b'{"id":"starter_csv","legacy_custom":true}\n'
            profile_path.write_bytes(legacy_content)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            result = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(
                any(
                    item["path"] == "profiles/starter_csv.json"
                    and item["result"] == "conflict"
                    and "origin" in item["reason"]
                    for item in payload["data"]["results"]
                )
            )
            self.assertFalse((root / MANAGED_FILES_NAME).exists())
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

    def test_newer_or_uncomparable_evidence_fails_closed_without_a_downgrade(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            manifest_path = root / MANAGED_FILES_NAME
            original_manifest = manifest_path.read_bytes()

            for evidence_version, error_text in (
                ("99.0.0", "newer HoneyMoney version"),
                ("0.1.0.post1", "version is invalid"),
            ):
                with self.subTest(evidence_version=evidence_version):
                    manifest = json.loads(original_manifest)
                    manifest["honeymoney_version"] = evidence_version
                    manifest_path.write_text(
                        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    before = {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    }

                    result = self._run_cli(
                        [
                            "setup",
                            "--upgrade",
                            "--root",
                            str(root),
                            "--yes",
                            "--json",
                        ]
                    )

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(
                        error_text,
                        json.loads(result.stdout)["errors"][0]["message"],
                    )
                    self.assertEqual(
                        {
                            path.relative_to(root).as_posix(): path.read_bytes()
                            for path in root.rglob("*")
                            if path.is_file()
                        },
                        before,
                    )
                    manifest_path.write_bytes(original_manifest)

    def test_noninteractive_write_requires_yes_and_dry_run_never_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            old_profile = b'{"id":"starter_csv","old":true}\n'
            profile_path, installed_profile = self._install_recorded_prior_profile(
                root,
                old_profile,
            )
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            unapproved = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--json"]
            )
            self.assertEqual(unapproved.returncode, 2, unapproved.stderr)
            unapproved_payload = json.loads(unapproved.stdout)
            self.assertEqual(unapproved_payload["status"], "error")
            self.assertEqual(
                unapproved_payload["errors"][0]["type"],
                "ApprovalRequired",
            )
            self.assertEqual(profile_path.read_bytes(), old_profile)

            dry_run = self._run_cli(
                [
                    "setup",
                    "--upgrade",
                    "--root",
                    str(root),
                    "--dry-run",
                    "--json",
                ]
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertFalse(json.loads(dry_run.stdout)["data"]["applied"])
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

            approved = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertEqual(profile_path.read_bytes(), installed_profile)

    def test_missing_legacy_bundle_is_created_with_narrow_ownership_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            manifest_path = root / MANAGED_FILES_NAME
            manifest_path.unlink()
            profile_path = root / "profiles" / "starter_csv.json"
            installed_profile = profile_path.read_bytes()
            profile_path.unlink()

            result = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(
                any(
                    item["path"] == "profiles/starter_csv.json"
                    and item["result"] == "create"
                    for item in payload["data"]["results"]
                )
            )
            self.assertEqual(profile_path.read_bytes(), installed_profile)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["path"] for entry in manifest["files"]],
                ["profiles/starter_csv.json"],
            )

    def test_configured_input_directory_is_never_a_managed_write_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["paths"]["input"] = str(root / "profiles")
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            profile_path = root / "profiles" / "starter_csv.json"
            sentinel = b"synthetic private input sentinel\n"
            profile_path.write_bytes(sentinel)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            result = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(
                any(
                    item["path"] == "profiles/starter_csv.json"
                    and item["result"] == "preserved"
                    for item in payload["data"]["results"]
                )
            )
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

    def test_managed_profile_symlink_is_a_conflict_and_never_updates_its_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            profile_path = root / "profiles" / "starter_csv.json"
            target_path = root / "profiles" / "mox_bank_pdf.json"
            target_before = target_path.read_bytes()
            profile_path.unlink()
            profile_path.symlink_to(target_path.name)

            result = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--yes", "--json"]
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(
                any(
                    item["path"] == "profiles/starter_csv.json"
                    and item["result"] == "conflict"
                    and "symbolic link" in item["reason"]
                    for item in payload["data"]["results"]
                )
            )
            self.assertTrue(profile_path.is_symlink())
            self.assertEqual(target_path.read_bytes(), target_before)

    def test_failed_publication_restores_the_prior_workspace_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            old_profile = b'{"id":"starter_csv","old":true}\n'
            self._install_recorded_prior_profile(root, old_profile)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            replace_from_retained = persistence._replace_from_retained

            def fail_manifest_commit(
                entry: persistence.GenerationEntry,
                source_field: Literal["staged", "backup"],
            ) -> None:
                if (
                    source_field == "staged"
                    and Path(entry["target"]).name == MANAGED_FILES_NAME
                ):
                    raise OSError("synthetic manifest publication failure")
                replace_from_retained(entry, source_field)

            with (
                patch.object(
                    persistence,
                    "_replace_from_retained",
                    side_effect=fail_manifest_commit,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "synthetic manifest publication failure",
                ),
            ):
                cli.main(
                    [
                        "setup",
                        "--upgrade",
                        "--root",
                        str(root),
                        "--yes",
                        "--json",
                    ]
                )

            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

    def test_next_upgrade_recovers_an_interrupted_committed_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            old_profile = b'{"id":"starter_csv","old":true}\n'
            profile_path, installed_profile = self._install_recorded_prior_profile(
                root,
                old_profile,
            )

            with (
                patch.object(
                    persistence,
                    "_finish_generation",
                    side_effect=OSError("synthetic cleanup interruption"),
                ),
                self.assertRaisesRegex(OSError, "synthetic cleanup interruption"),
            ):
                cli.main(
                    [
                        "setup",
                        "--upgrade",
                        "--root",
                        str(root),
                        "--yes",
                        "--json",
                    ]
                )

            self.assertEqual(profile_path.read_bytes(), installed_profile)
            retained_bytes = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            dry_run = self._run_cli(
                [
                    "setup",
                    "--upgrade",
                    "--root",
                    str(root),
                    "--dry-run",
                    "--json",
                ]
            )
            self.assertEqual(dry_run.returncode, 2, dry_run.stderr)
            self.assertIn(
                "recovery is required",
                json.loads(dry_run.stdout)["errors"][0]["message"],
            )
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                retained_bytes,
            )
            recovered = self._run_cli(
                ["setup", "--upgrade", "--root", str(root), "--json"]
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertFalse(json.loads(recovered.stdout)["data"]["changed"])
            self.assertFalse(
                (root / f".{MANAGED_FILES_NAME}.honeymoney-state.json").exists()
            )

    def test_interactive_decline_leaves_the_workspace_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            old_profile = b'{"id":"starter_csv","old":true}\n'
            self._install_recorded_prior_profile(root, old_profile)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            class InteractiveInput(io.StringIO):
                def isatty(self) -> bool:
                    return True

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(sys, "stdin", InteractiveInput("no\n")),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                returncode = cli.main(["setup", "--upgrade", "--root", str(root)])

            self.assertEqual(returncode, 0, stderr.getvalue())
            self.assertIn("Upgrade declined", stdout.getvalue())
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                before,
            )

    def test_force_stays_destructive_explicit_and_warned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(["setup", "--root", str(root), "--json"])
            self.assertEqual(setup.returncode, 0, setup.stderr)
            custom_rules = b'{"synthetic":"custom rules"}\n'
            (root / "rules.json").write_bytes(custom_rules)

            forced = self._run_cli(["setup", "--root", str(root), "--force", "--json"])

            self.assertEqual(forced.returncode, 0, forced.stderr)
            forced_payload = json.loads(forced.stdout)
            self.assertEqual(len(forced_payload["warnings"]), 1)
            self.assertIn("--force replaces", forced_payload["warnings"][0])
            self.assertNotEqual((root / "rules.json").read_bytes(), custom_rules)

            invalid = self._run_cli(
                [
                    "setup",
                    "--upgrade",
                    "--root",
                    str(root),
                    "--force",
                    "--json",
                ]
            )
            self.assertEqual(invalid.returncode, 2, invalid.stderr)
            self.assertIn(
                "cannot be combined",
                json.loads(invalid.stdout)["errors"][0]["message"],
            )


if __name__ == "__main__":
    unittest.main()
