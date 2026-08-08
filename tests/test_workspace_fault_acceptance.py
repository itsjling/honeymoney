"""Public CLI checks for stopped workspace publication recovery."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FAULT_HOOK = REPO_ROOT / "tests" / "fault_injection"


class WorkspaceFaultAcceptanceTest(unittest.TestCase):
    def _environment(
        self,
        *,
        fault: str | None = None,
        ready_path: Path | None = None,
    ) -> dict[str, str]:
        environment = dict(os.environ)
        if fault is not None:
            environment["HONEYMONEY_TEST_FS_FAULT"] = fault
            if ready_path is not None:
                environment["HONEYMONEY_TEST_FS_FAULT_READY"] = str(ready_path)
            inherited = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(FAULT_HOOK), str(REPO_ROOT), inherited) if item
            )
        return environment

    def _run(
        self,
        *arguments: str,
        fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            cwd=REPO_ROOT,
            env=self._environment(fault=fault),
            text=True,
            capture_output=True,
            check=False,
        )

    def _start_stopped(
        self,
        root: Path,
        *arguments: str,
        fault: str,
    ) -> subprocess.Popen[str]:
        ready_path = root.parent / "fault-ready"
        process = subprocess.Popen(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            cwd=REPO_ROOT,
            env=self._environment(fault=fault, ready_path=ready_path),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not ready_path.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                process.kill()
                process.communicate(timeout=5)
                self.fail("faulted writer did not stop")
            time.sleep(0.01)
        if not ready_path.exists():
            _stdout, stderr = process.communicate(timeout=5)
            self.fail(f"faulted writer exited before stopping: {stderr}")
        return process

    def _kill(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)

    def _setup(self, root: Path) -> tuple[Path, Path]:
        setup = self._run("setup", "--root", str(root), "--json")
        self.assertEqual(setup.returncode, 0, setup.stderr)
        document = root.parent / "rates-download.json"
        document.write_text(
            json.dumps(
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
            ),
            encoding="utf-8",
        )
        return root / "config.json", document

    def _workspace_bytes(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def _assert_recovery_required(self, config: Path) -> None:
        blocked = self._run("status", "--all", "--config", str(config), "--json")
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(
            json.loads(blocked.stdout)["errors"][0]["code"],
            "publication_recovery_required",
        )
        inspected = self._run("doctor", "--config", str(config), "--json")
        self.assertEqual(inspected.returncode, 2)
        self.assertIn(
            "publication_recovery_required",
            [
                finding["code"]
                for finding in json.loads(inspected.stdout)["data"]["findings"]
            ],
        )
        self.assertTrue(
            (config.parent / ".honeymoney" / "publication-journal.json").is_file()
        )

    def _assert_retry_is_idempotent(self, config: Path, document: Path) -> None:
        first = self._run(
            "rates", "import", str(document), "--config", str(config), "--json"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        root = config.parent
        published = self._workspace_bytes(root)
        repeated = self._run(
            "rates", "import", str(document), "--config", str(config), "--json"
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["data"]["written_count"], 0)
        self.assertEqual(self._workspace_bytes(root), published)

    def _assert_precommit_fault(self, fault: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config, document = self._setup(root)
            before = self._workspace_bytes(root)

            writer = self._start_stopped(
                root,
                "rates",
                "import",
                str(document),
                "--config",
                str(config),
                "--json",
                fault=fault,
            )
            try:
                self.assertEqual(
                    self._workspace_bytes(root)["rates.json"], before["rates.json"]
                )
                self.assertEqual(
                    self._workspace_bytes(root)[".honeymoney/workspace-index.json"],
                    before[".honeymoney/workspace-index.json"],
                )
                inspected = self._run("doctor", "--config", str(config), "--json")
                self.assertEqual(inspected.returncode, 2)
                self.assertIn(
                    "workspace_busy",
                    {
                        finding["code"]
                        for finding in json.loads(inspected.stdout)["data"]["findings"]
                    },
                )
            finally:
                self._kill(writer)

            self.assertTrue(
                (root / ".honeymoney" / "publication-journal.json").is_file()
            )
            self._assert_recovery_required(config)

            fixed = self._run("doctor", "--fix", "--config", str(config), "--json")

            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertEqual(self._workspace_bytes(root), before)
            self._assert_retry_is_idempotent(config, document)

    def test_staged_write_fault_keeps_old_generation_until_doctor_fix(self) -> None:
        self._assert_precommit_fault("stop:staged-write:rates.json:1")

    def test_staged_file_fsync_fault_keeps_old_generation_until_doctor_fix(
        self,
    ) -> None:
        self._assert_precommit_fault("stop:staged-file-fsync:rates.json:2")

    def test_non_index_replacement_fault_keeps_old_generation_until_doctor_fix(
        self,
    ) -> None:
        self._assert_precommit_fault("stop:replacement:rates.json:1")

    def test_directory_fsync_fault_keeps_old_generation_until_doctor_fix(
        self,
    ) -> None:
        self._assert_precommit_fault("stop:directory-fsync:.honeymoney/publication:1")

    def test_index_last_stop_blocks_readers_then_doctor_keeps_new_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config, document = self._setup(root)
            before = self._workspace_bytes(root)
            writer = self._start_stopped(
                root,
                "rates",
                "import",
                str(document),
                "--config",
                str(config),
                "--json",
                fault="stop:index-commit:.honeymoney/workspace-index.json:1",
            )
            try:
                committed_rates = (root / "rates.json").read_bytes()
                committed_index = (
                    root / ".honeymoney" / "workspace-index.json"
                ).read_bytes()
                self.assertNotEqual(committed_rates, before["rates.json"])
                self.assertNotEqual(
                    committed_index,
                    before[".honeymoney/workspace-index.json"],
                )
                blocked = self._run(
                    "status", "--all", "--config", str(config), "--json"
                )
                self.assertEqual(blocked.returncode, 2)
                blocked_payload = json.loads(blocked.stdout)
                self.assertEqual(
                    blocked_payload["errors"][0]["code"],
                    "publication_recovery_required",
                )
                self.assertEqual(blocked_payload["data"], {})
                inspected = self._run("doctor", "--config", str(config), "--json")
                self.assertEqual(inspected.returncode, 2)
                self.assertIn(
                    "workspace_busy",
                    [
                        finding["code"]
                        for finding in json.loads(inspected.stdout)["data"]["findings"]
                    ],
                )
            finally:
                self._kill(writer)

            diagnosed = self._run("doctor", "--config", str(config), "--json")
            self.assertEqual(diagnosed.returncode, 2)
            codes = {
                finding["code"]
                for finding in json.loads(diagnosed.stdout)["data"]["findings"]
            }
            self.assertTrue({"stale_lock", "publication_recovery_required"} <= codes)

            fixed = self._run("doctor", "--fix", "--config", str(config), "--json")

            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertEqual((root / "rates.json").read_bytes(), committed_rates)
            self.assertEqual(
                (root / ".honeymoney" / "workspace-index.json").read_bytes(),
                committed_index,
            )
            self.assertFalse(
                (root / ".honeymoney" / "publication-journal.json").exists()
            )
            self.assertFalse((root / ".honeymoney" / "workspace.lock").exists())
            self._assert_retry_is_idempotent(config, document)

    def test_live_lock_blocks_second_cli_then_doctor_recovers_stale_owner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config, document = self._setup(root)
            before = self._workspace_bytes(root)
            writer = self._start_stopped(
                root,
                "rates",
                "import",
                str(document),
                "--config",
                str(config),
                "--json",
                fault="stop:lock-acquired:.honeymoney/workspace.lock:1",
            )
            try:
                blocked = self._run(
                    "status", "--all", "--config", str(config), "--json"
                )
                self.assertEqual(blocked.returncode, 2)
                blocked_error = json.loads(blocked.stdout)["errors"][0]
                self.assertEqual(blocked_error["type"], "WorkspaceBusyError")
                self.assertEqual(blocked_error["code"], "workspace_busy")
                self.assertEqual(blocked_error["message"], "workspace busy")
                self.assertEqual(
                    self._workspace_bytes(root),
                    before
                    | {
                        ".honeymoney/workspace.lock": (
                            root / ".honeymoney" / "workspace.lock"
                        ).read_bytes()
                    },
                )
            finally:
                self._kill(writer)

            diagnosed = self._run("doctor", "--config", str(config), "--json")
            self.assertEqual(diagnosed.returncode, 2)
            self.assertIn(
                "stale_lock",
                [
                    finding["code"]
                    for finding in json.loads(diagnosed.stdout)["data"]["findings"]
                ],
            )

            fixed = self._run("doctor", "--fix", "--config", str(config), "--json")
            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertEqual(self._workspace_bytes(root), before)
            self.assertFalse((root / ".honeymoney" / "workspace.lock").exists())
            self._assert_retry_is_idempotent(config, document)

    def test_setup_stop_before_index_commit_settles_old_bytes_then_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            root.mkdir()
            os.chmod(root, 0o755)
            writer = self._start_stopped(
                root,
                "setup",
                "--root",
                str(root),
                "--json",
                fault="stop:replacement:.honeymoney/workspace-index.json:1",
            )
            try:
                self.assertTrue(
                    (root / ".honeymoney" / "publication-journal.json").is_file()
                )
                self.assertFalse(
                    (root / ".honeymoney" / "workspace-index.json").exists()
                )
            finally:
                self._kill(writer)

            blocked = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(
                json.loads(blocked.stdout)["errors"][0]["message"],
                "publication recovery is required",
            )
            diagnosed = self._run(
                "doctor", "--config", str(root / "config.json"), "--json"
            )
            self.assertEqual(diagnosed.returncode, 2)
            self.assertIn(
                "publication_recovery_required",
                [
                    finding["code"]
                    for finding in json.loads(diagnosed.stdout)["data"]["findings"]
                ],
            )
            fixed = self._run(
                "doctor", "--fix", "--config", str(root / "config.json"), "--json"
            )

            self.assertEqual(fixed.returncode, 2)
            self.assertFalse(
                (root / ".honeymoney" / "publication-journal.json").exists()
            )
            self.assertEqual(self._workspace_bytes(root), {})
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertIn(
                "workspace_input_invalid",
                [
                    finding["code"]
                    for finding in json.loads(fixed.stdout)["data"]["findings"]
                ],
            )

            setup = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            before_repeat = self._workspace_bytes(root)
            repeated = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(self._workspace_bytes(root), before_repeat)

    def test_setup_stop_after_index_commit_keeps_new_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            writer = self._start_stopped(
                root,
                "setup",
                "--root",
                str(root),
                "--json",
                fault="stop:index-commit:.honeymoney/workspace-index.json:1",
            )
            try:
                self.assertTrue((root / "config.json").is_file())
                self.assertTrue(
                    (root / ".honeymoney" / "workspace-index.json").is_file()
                )
                expected_new = {
                    path: content
                    for path, content in self._workspace_bytes(root).items()
                    if path
                    not in {
                        ".honeymoney/publication-journal.json",
                        ".honeymoney/workspace.lock",
                    }
                    and not path.startswith(".honeymoney/publication/")
                }
            finally:
                self._kill(writer)

            diagnosed = self._run(
                "doctor", "--config", str(root / "config.json"), "--json"
            )
            self.assertEqual(diagnosed.returncode, 2)
            self.assertIn(
                "publication_recovery_required",
                [
                    finding["code"]
                    for finding in json.loads(diagnosed.stdout)["data"]["findings"]
                ],
            )
            fixed = self._run(
                "doctor", "--fix", "--config", str(root / "config.json"), "--json"
            )

            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertFalse(
                (root / ".honeymoney" / "publication-journal.json").exists()
            )
            self.assertEqual(self._workspace_bytes(root), expected_new)
            before_repeat = self._workspace_bytes(root)
            repeated = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(self._workspace_bytes(root), before_repeat)


if __name__ == "__main__":
    unittest.main()
