from __future__ import annotations

import csv
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney import workspace_setup
from honeymoney.identity import record_fingerprint, source_revision
from honeymoney.workspace_commands import SNAPSHOT_COLUMNS
from honeymoney.workspace_publication import WorkspaceBusyError


class WorkspaceSetupAcceptanceTest(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def _metadata(self, root: Path) -> dict[str, tuple[int, int]]:
        return {
            path.relative_to(root).as_posix(): (
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_mtime_ns,
            )
            for path in (root, *sorted(root.rglob("*")))
        }

    def test_setup_creates_clean_start_workspace_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"

            first = self._run("setup", "--root", str(root), "--json")

            self.assertEqual(first.returncode, 0, first.stderr)
            payload = json.loads(first.stdout)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["command"], "setup")
            self.assertEqual(payload["status"], "success")
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                [
                    ".honeymoney",
                    "config.json",
                    "corrections.csv",
                    "profile_mappings.json",
                    "profiles",
                    "rates.json",
                    "rules.json",
                ],
            )
            self.assertEqual(
                sorted(path.name for path in (root / ".honeymoney").iterdir()),
                ["import-records", "workspace-index.json"],
            )
            self.assertFalse((root / "input").exists())
            self.assertFalse((root / "views").exists())
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertNotIn("paths", config)
            self.assertEqual(config["profiles"][0], "profiles/starter_csv.json")
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / ".honeymoney").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "config.json").stat().st_mode), 0o600)
            before = self._snapshot(root)
            before_metadata = self._metadata(root)

            second = self._run("setup", "--root", str(root), "--json")

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(self._snapshot(root), before)
            self.assertEqual(self._metadata(root), before_metadata)

    def test_setup_repairs_modes_in_a_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            first = self._run("setup", "--root", str(root), "--json")
            self.assertEqual(first.returncode, 0, first.stderr)
            index_path = root / ".honeymoney" / "workspace-index.json"
            before_generation = json.loads(index_path.read_text(encoding="utf-8"))[
                "generation_id"
            ]
            os.chmod(root / "rules.json", 0o644)

            repaired = self._run("setup", "--root", str(root), "--json")

            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(stat.S_IMODE((root / "rules.json").stat().st_mode), 0o600)
            after_generation = json.loads(index_path.read_text(encoding="utf-8"))[
                "generation_id"
            ]
            self.assertNotEqual(after_generation, before_generation)

    def test_setup_keeps_a_completed_concurrent_setup_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            real_lock = workspace_setup.WorkspaceLock
            run_cli = self._run
            test_case = self
            first_generation: str | None = None
            triggered = False

            class InterleavingLock:
                def __init__(self, lock_root: Path) -> None:
                    self.lock = real_lock(lock_root)

                def __enter__(self) -> InterleavingLock:
                    nonlocal first_generation, triggered
                    if not triggered:
                        triggered = True
                        completed = run_cli("setup", "--root", str(root), "--json")
                        test_case.assertEqual(completed.returncode, 0, completed.stderr)
                        index = json.loads(
                            (root / ".honeymoney" / "workspace-index.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        first_generation = index["generation_id"]
                    self.lock.__enter__()
                    return self

                def __exit__(self, *arguments: object) -> None:
                    self.lock.__exit__(*arguments)

            with patch.object(workspace_setup, "WorkspaceLock", InterleavingLock):
                workspace_setup.setup_workspace(root)

            self.assertIsNotNone(first_generation)
            after = json.loads(
                (root / ".honeymoney" / "workspace-index.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(after["generation_id"], first_generation)

    def test_setup_rejects_changed_state_after_lock_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            unexpected = root / "rules.json"
            real_lock = workspace_setup.WorkspaceLock

            class InterleavingLock:
                def __init__(self, lock_root: Path) -> None:
                    self.lock = real_lock(lock_root)

                def __enter__(self) -> InterleavingLock:
                    unexpected.parent.mkdir()
                    unexpected.write_text('{"rules": ["changed"]}\n', encoding="utf-8")
                    self.lock.__enter__()
                    return self

                def __exit__(self, *arguments: object) -> None:
                    self.lock.__exit__(*arguments)

            with (
                patch.object(workspace_setup, "WorkspaceLock", InterleavingLock),
                self.assertRaisesRegex(WorkspaceBusyError, "retry setup"),
            ):
                workspace_setup.setup_workspace(root)

            self.assertEqual(
                unexpected.read_text(encoding="utf-8"), '{"rules": ["changed"]}\n'
            )

    def test_setup_rejects_legacy_workspace_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            root.mkdir()
            legacy = {
                "paths": {
                    "input": "./input",
                    "output": "./output/categorized.csv",
                }
            }
            (root / "config.json").write_text(
                json.dumps(legacy, sort_keys=True), encoding="utf-8"
            )
            before = self._snapshot(root)

            result = self._run("setup", "--root", str(root), "--json")

            self.assertEqual(result.returncode, 2)
            self.assertEqual(self._snapshot(root), before)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(
                payload["errors"][0]["code"], "legacy_workspace_reset_required"
            )


class ImportLifecycleAcceptanceTest(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def _setup(self, root: Path) -> Path:
        result = self._run("setup", "--root", str(root), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return root / "config.json"

    def test_first_import_creates_record_attempt_and_month_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._setup(root)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
                encoding="utf-8",
            )

            result = self._run(
                "import",
                str(source),
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["command"], "import")
            self.assertEqual(payload["data"]["import_count"], 1)
            self.assertEqual(payload["data"]["statement_transaction_count"], 1)
            self.assertEqual(payload["data"]["view_transaction_count"], 1)
            records = list((root / ".honeymoney" / "import-records").iterdir())
            self.assertEqual(len(records), 1)
            record = records[0]
            summary = json.loads((record / "summary.json").read_text())
            self.assertTrue(summary["ready"])
            self.assertEqual(summary["current_attempt_number"], 1)
            self.assertTrue((record / "transactions.csv").is_file())
            self.assertTrue((record / "attempts" / "00000001.json").is_file())
            view = root / "views" / "2026-08"
            self.assertEqual(
                sorted(path.name for path in view.iterdir()),
                ["report.html", "review_needed.csv", "transactions.csv"],
            )

            listed = self._run("imports", "list", "--config", str(config), "--json")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            listed_payload = json.loads(listed.stdout)
            self.assertEqual(listed_payload["data"]["import_count"], 1)
            self.assertEqual(
                listed_payload["data"]["import_records"][0]["source_id"],
                record.name,
            )
            shown = self._run(
                "imports", "show", record.name, "--config", str(config), "--json"
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(
                json.loads(shown.stdout)["data"]["attempts"][0]["outcome"],
                "success",
            )

            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            repeated = self._run(
                "import",
                str(source),
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(
                json.loads(repeated.stdout)["errors"][0]["code"],
                "source_already_imported",
            )
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_all_rebuild_adopts_missing_optional_workspace_inputs(self) -> None:
        for filename in (
            "corrections.csv",
            "profile_mappings.json",
            "rates.json",
            "rules.json",
        ):
            with (
                self.subTest(filename=filename),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "money"
                config = self._setup(root)
                source = root / "synthetic.csv"
                source.write_text(
                    "Date,Description,Amount,Currency\n"
                    "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
                    encoding="utf-8",
                )
                imported = self._run(
                    "import",
                    str(source),
                    "--config",
                    str(config),
                    "--no-interactive",
                    "--json",
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                (root / filename).unlink()

                blocked = self._run(
                    "status", "--all", "--config", str(config), "--json"
                )
                self.assertEqual(blocked.returncode, 2)
                self.assertEqual(
                    json.loads(blocked.stdout)["errors"][0]["code"],
                    "full_rebuild_required",
                )

                rebuilt = self._run(
                    "views",
                    "rebuild",
                    "--all",
                    "--config",
                    str(config),
                    "--json",
                )
                self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
                status = self._run("status", "--all", "--config", str(config), "--json")
                self.assertEqual(status.returncode, 0, status.stderr)
                doctor = self._run("doctor", "--config", str(config), "--json")
                self.assertEqual(doctor.returncode, 0, doctor.stderr)

    def test_index_evidence_is_private_to_each_workspace(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            roots = [Path(temporary) / name for name in ("first", "second")]
            index_values: list[dict[str, object]] = []
            raw_revision = source_revision(
                b"Date,Description,Amount,Currency\n"
                b"2026-08-08,Synthetic Grocer,-12.00,HKD\n"
            )
            raw_fingerprint = ""
            for root in roots:
                config = self._setup(root)
                source = root / "synthetic.csv"
                source.write_text(
                    "Date,Description,Amount,Currency\n"
                    "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
                    encoding="utf-8",
                )
                imported = self._run(
                    "import",
                    str(source),
                    "--config",
                    str(config),
                    "--no-interactive",
                    "--json",
                )
                self.assertEqual(imported.returncode, 0, imported.stderr)
                record = next((root / ".honeymoney" / "import-records").iterdir())
                with (record / "transactions.csv").open(
                    encoding="utf-8", newline=""
                ) as handle:
                    row = next(csv.DictReader(handle))
                self.assertEqual(tuple(row), SNAPSHOT_COLUMNS)
                raw_fingerprint = record_fingerprint(row)
                index_path = root / ".honeymoney" / "workspace-index.json"
                document = index_path.read_text(encoding="utf-8")
                self.assertNotIn(raw_revision, document)
                self.assertNotIn(raw_fingerprint, document)
                index_values.append(json.loads(document))

            first_identity = index_values[0]["identity_manifest"]  # type: ignore[index]
            second_identity = index_values[1]["identity_manifest"]  # type: ignore[index]
            first_source = first_identity["sources"][0]  # type: ignore[index]
            second_source = second_identity["sources"][0]  # type: ignore[index]
            self.assertNotEqual(
                first_source["source_revision"], second_source["source_revision"]
            )
            self.assertNotEqual(
                first_source["records"][0]["record_fingerprint"],
                second_source["records"][0]["record_fingerprint"],
            )

    def test_exact_replace_restores_workspace_identity(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary) / "money"
            config = self._setup(root)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic recurrence,-12.00,HKD\n",
                encoding="utf-8",
            )
            first = self._run(
                "import",
                str(source),
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )
            self.assertEqual(first.returncode, 0, first.stdout)
            index_path = root / ".honeymoney" / "workspace-index.json"
            before = json.loads(index_path.read_text(encoding="utf-8"))

            replaced = self._run(
                "import",
                str(source),
                "--replace",
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )

            self.assertEqual(replaced.returncode, 0, replaced.stdout)
            after = json.loads(index_path.read_text(encoding="utf-8"))
            before_source = before["identity_manifest"]["sources"][0]
            after_source = after["identity_manifest"]["sources"][0]
            self.assertEqual(before_source["records"], after_source["records"])
            self.assertEqual(
                before["overlap_manifest"]["groups"],
                after["overlap_manifest"]["groups"],
            )

    def test_valid_zero_row_import_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._setup(root)
            source = root / "empty.csv"
            source.write_text("Date,Description,Amount,Currency\n", encoding="utf-8")

            result = self._run(
                "import",
                str(source),
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            record = next((root / ".honeymoney" / "import-records").iterdir())
            summary = json.loads((record / "summary.json").read_text())
            self.assertTrue(summary["ready"])
            self.assertEqual(summary["statement_transaction_count"], 0)
            self.assertEqual((record / "transactions.csv").read_text().count("\n"), 1)

    def test_period_queries_share_selectors_and_report_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            config = self._setup(root)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic August Item,-12.00,HKD\n"
                "2026-09-02,Synthetic September Item,-8.00,HKD\n",
                encoding="utf-8",
            )
            imported = self._run(
                "import",
                str(source),
                "--config",
                str(config),
                "--no-interactive",
                "--json",
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            status = self._run(
                "status",
                "--start",
                "2026-08-08",
                "--end",
                "2026-08-08",
                "--config",
                str(config),
                "--json",
            )
            pending = self._run(
                "pending",
                "--month",
                "2026-08",
                "--config",
                str(config),
                "--json",
            )
            reviewed = self._run(
                "review",
                "--month",
                "2026-08",
                "--config",
                str(config),
                "--json",
            )
            missing = self._run(
                "valuation",
                "missing",
                "--month",
                "2026-08",
                "--config",
                str(config),
                "--json",
            )
            managed = self._run(
                "report",
                "--month",
                "2026-08",
                "--no-open",
                "--config",
                str(config),
                "--json",
            )
            preview = self._run(
                "report",
                "--start",
                "2026-08-08",
                "--end",
                "2026-09-02",
                "--no-open",
                "--config",
                str(config),
                "--json",
            )

            for result in (status, pending, reviewed, missing, managed, preview):
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(status.stdout)["data"]["view_transaction_count"], 1
            )
            self.assertEqual(json.loads(pending.stdout)["data"]["pending_count"], 1)
            self.assertEqual(json.loads(reviewed.stdout)["data"]["pending_count"], 1)
            self.assertEqual(
                json.loads(missing.stdout)["data"]["missing_valuation_count"], 0
            )
            self.assertEqual(
                json.loads(managed.stdout)["artifacts"]["report_html"],
                str((root / "views/2026-08/report.html").resolve()),
            )
            self.assertEqual(
                json.loads(preview.stdout)["artifacts"]["report_html"],
                str((root / ".honeymoney/report-preview.html").resolve()),
            )
            self.assertTrue((root / ".honeymoney/report-preview.html").is_file())


if __name__ == "__main__":
    unittest.main()
