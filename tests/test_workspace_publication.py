import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import honeymoney.workspace_publication as workspace_publication
from honeymoney.import_records import attempt_document, safe_source_label
from honeymoney.workspace_publication import (
    AttemptReservation,
    FixedAttemptReservation,
    PendingAttemptReservation,
    PublicationDirectory,
    PublicationError,
    PublicationTarget,
    WorkspaceBusyError,
    WorkspaceLock,
    inspect_lock,
    inspect_retained_publication,
    publish_failed_attempts,
    publish_generation,
    publish_reserved_failures,
    publish_reserved_generation,
    reserve_publication,
    settle_retained_publication,
)

_SOURCE_ID = f"src_{'1' * 64}"


def _attempt_reservation() -> AttemptReservation:
    common: dict[str, object] = {
        "schema_version": 1,
        "honeymoney_version": "0.2.0",
        "source_id": _SOURCE_ID,
        "source_label": safe_source_label(_SOURCE_ID, "csv"),
        "attempt_number": 1,
        "requested_action": "import",
        "started_at": "2026-08-08T00:00:00Z",
        "finished_at": "2026-08-08T00:00:01Z",
        "source_revision": "2" * 64,
        "parser_contract": "3" * 64,
        "counts": {"statement_transaction_count": 1},
        "warnings": [],
        "warning_count": 0,
        "omitted_warning_count": 0,
    }
    success = {
        **common,
        "outcome": "success",
        "error_codes": [],
        "error_count": 0,
        "omitted_error_count": 0,
        "transactions_schema_version": 1,
        "transactions_digest": "4" * 64,
    }
    interrupted = {
        **common,
        "outcome": "failure",
        "counts": {"statement_transaction_count": 0},
        "error_codes": ["interrupted"],
        "error_count": 1,
        "omitted_error_count": 0,
    }
    return AttemptReservation(
        path=(f".honeymoney/import-records/{_SOURCE_ID}/attempts/00000001.json"),
        success_content=attempt_document(success).encode(),
        interrupted_content=attempt_document(interrupted).encode(),
    )


def _pending_attempt_reservation() -> PendingAttemptReservation:
    interrupted = json.loads(_attempt_reservation().interrupted_content)
    return PendingAttemptReservation(
        path=(f".honeymoney/import-records/{_SOURCE_ID}/attempts/00000001.json"),
        interrupted_content=attempt_document(interrupted).encode(),
    )


class WorkspacePublicationTest(unittest.TestCase):
    def test_staging_stop_before_retained_bytes_settles_the_old_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney" / "workspace-index.json"
            index.parent.mkdir()
            index.write_bytes(b'{"generation_id":"old"}\n')
            target = root / "item.txt"
            target.write_bytes(b"old")

            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._retain_entry",
                    side_effect=OSError("synthetic staging stop"),
                ),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "staging-stop",
                        [PublicationTarget("item.txt", b"new")],
                        b'{"generation_id":"staging-stop"}\n',
                    )

            self.assertEqual(inspect_retained_publication(root), "old")
            self.assertEqual(settle_retained_publication(root), "old")
            self.assertEqual(target.read_bytes(), b"old")

    def test_metadata_recovery_restores_exact_old_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney" / "workspace-index.json"
            index.parent.mkdir()
            index.write_bytes(b'{"generation_id":"old"}\n')
            os.chmod(index, 0o644)
            target = root / "item.txt"
            target.write_bytes(b"old")
            os.chmod(target, 0o644)
            directory = root / "views"
            directory.mkdir()
            os.chmod(directory, 0o755)
            real_replace = os.replace

            def fail_index(source: object, destination: object) -> None:
                if Path(destination).name == "workspace-index.json":
                    raise OSError("synthetic index stop")
                real_replace(source, destination)

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", fail_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "metadata-old",
                        [PublicationTarget("item.txt", b"old")],
                        b'{"generation_id":"metadata-old"}\n',
                        directory_targets=[PublicationDirectory("views")],
                    )

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(settle_retained_publication(root), "old")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o644)

    def test_metadata_recovery_removes_new_directories_with_the_old_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney" / "workspace-index.json"
            index.parent.mkdir()
            index.write_bytes(b'{"generation_id":"old"}\n')
            real_replace = os.replace

            def fail_index(source: object, destination: object) -> None:
                if Path(destination).name == "workspace-index.json":
                    raise OSError("synthetic index stop")
                real_replace(source, destination)

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", fail_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "metadata-create",
                        [PublicationTarget("views/2026-05/transactions.csv", b"new")],
                        b'{"generation_id":"metadata-create"}\n',
                        directory_targets=[
                            PublicationDirectory("views"),
                            PublicationDirectory("views/2026-05"),
                        ],
                    )

            self.assertEqual(settle_retained_publication(root), "old")
            self.assertFalse((root / "views" / "2026-05").exists())
            self.assertFalse((root / "views").exists())

    def test_reserved_failures_keep_workspace_index_bytes_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney" / "workspace-index.json"
            index.parent.mkdir()
            index_bytes = b'{"generation_id":"old"}\n'
            index.write_bytes(index_bytes)
            pending = _pending_attempt_reservation()
            fixed = FixedAttemptReservation(
                pending.path,
                pending.interrupted_content,
            )

            with WorkspaceLock(root):
                reserve_publication(root, "failure-only", [pending])
                publish_reserved_failures(
                    root,
                    "failure-only",
                    [PublicationTarget("summary.json", b"summary\n")],
                    attempt_reports=[fixed],
                )

            self.assertEqual(index.read_bytes(), index_bytes)
            self.assertEqual(
                json.loads((root / pending.path).read_text(encoding="utf-8"))[
                    "outcome"
                ],
                "failure",
            )
            self.assertEqual((root / "summary.json").read_bytes(), b"summary\n")

    def test_fixed_attempt_publication_journals_new_record_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney" / "workspace-index.json"
            index.parent.mkdir()
            index.write_bytes(b'{"generation_id":"old"}\n')
            pending = _pending_attempt_reservation()
            fixed = FixedAttemptReservation(
                pending.path,
                pending.interrupted_content,
            )

            with WorkspaceLock(root):
                publish_failed_attempts(root, "fixed-attempt", [fixed])

            record = root / ".honeymoney" / "import-records" / _SOURCE_ID
            self.assertTrue((record / "attempts").is_dir())
            self.assertEqual(
                json.loads((root / pending.path).read_text(encoding="utf-8"))[
                    "outcome"
                ],
                "failure",
            )

    def test_reserved_attempt_recovers_as_interrupted_before_work_is_prepared(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            index = root / ".honeymoney/workspace-index.json"
            index.parent.mkdir()
            index.write_bytes(b'{"generation_id":"old"}\n')
            reservation = _pending_attempt_reservation()

            with WorkspaceLock(root):
                reserve_publication(root, "generation-reserved", [reservation])

            journal = root / ".honeymoney/publication-journal.json"
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
            self.assertEqual(json.loads(journal.read_text())["phase"], "reserved")
            self.assertEqual(inspect_retained_publication(root), "old")
            self.assertEqual(settle_retained_publication(root), "old")

            report = root / reservation.path
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["outcome"], "failure")
            self.assertEqual(value["error_codes"], ["interrupted"])
            self.assertEqual(index.read_bytes(), b'{"generation_id":"old"}\n')

    def test_reserved_publication_is_enriched_and_committed_index_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney/workspace-index.json"
            index.parent.mkdir()
            index.write_bytes(b'{"generation_id":"old"}\n')
            pending = _pending_attempt_reservation()
            completed = _attempt_reservation()
            seen: list[str] = []
            real_replace = os.replace

            def record_replace(source: object, destination: object) -> None:
                seen.append(Path(destination).name)
                real_replace(source, destination)

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", record_replace),
            ):
                reserve_publication(root, "generation-reserved", [pending])
                publish_reserved_generation(
                    root,
                    "generation-reserved",
                    [PublicationTarget("views/2026-05/transactions.csv", b"new\n")],
                    b'{"generation_id":"generation-reserved"}\n',
                    attempt_reports=[completed],
                )

            self.assertLess(
                max(i for i, name in enumerate(seen) if name == "transactions.csv"),
                max(i for i, name in enumerate(seen) if name == "workspace-index.json"),
            )
            self.assertEqual(
                json.loads((root / completed.path).read_text())["outcome"],
                "success",
            )
            self.assertFalse((root / ".honeymoney/publication-journal.json").exists())

    def test_lock_records_owner_and_rejects_a_second_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with WorkspaceLock(root):
                self.assertEqual(inspect_lock(root), "live")
                with self.assertRaises(WorkspaceBusyError) as raised:
                    WorkspaceLock(root).acquire()
                self.assertEqual(raised.exception.code, "workspace_busy")
                self.assertEqual(str(raised.exception), "workspace busy")
            self.assertEqual(inspect_lock(root), "absent")

    def test_publish_installs_index_last_and_removes_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ".honeymoney" / "workspace-index.json"
            view = root / "views" / "2026-05" / "transactions.csv"
            seen: list[str] = []
            real_replace = os.replace

            def record_replace(source: object, destination: object) -> None:
                seen.append(Path(destination).name)
                real_replace(source, destination)

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", record_replace),
            ):
                publish_generation(
                    root,
                    "generation-1",
                    [PublicationTarget("views/2026-05/transactions.csv", b"new\n")],
                    b'{"generation_id":"generation-1"}\n',
                )

            self.assertEqual(view.read_bytes(), b"new\n")
            self.assertEqual(index.read_bytes(), b'{"generation_id":"generation-1"}\n')
            self.assertLess(
                max(i for i, name in enumerate(seen) if name == "transactions.csv"),
                max(i for i, name in enumerate(seen) if name == "workspace-index.json"),
            )
            self.assertIsNone(inspect_retained_publication(root))

    def test_syncs_the_full_recovery_directory_chain_before_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            synced: list[Path] = []

            def record_sync(path: Path) -> None:
                synced.append(path)

            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._fsync_directory",
                    side_effect=record_sync,
                ),
            ):
                publish_generation(
                    root,
                    "generation-recovery-sync",
                    [PublicationTarget("views/2026-05/transactions.csv", b"new\n")],
                    b'{"generation_id":"generation-recovery-sync"}\n',
                )

            recovery = root / ".honeymoney" / "publication" / "generation-recovery-sync"
            self.assertTrue(
                {recovery, recovery.parent, recovery.parent.parent}.issubset(synced)
            )

    def test_recovery_sync_fault_keeps_a_staging_journal_for_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._fsync_recovery_chain",
                    side_effect=OSError("synthetic recovery sync stop"),
                ),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-recovery-sync-fault",
                        [PublicationTarget("item.txt", b"new")],
                        b'{"generation_id":"generation-recovery-sync-fault"}\n',
                    )

            journal = root / ".honeymoney/publication-journal.json"
            self.assertEqual(
                json.loads(journal.read_text(encoding="utf-8"))["phase"], "staging"
            )
            self.assertEqual(settle_retained_publication(root), "old")
            self.assertFalse(journal.exists())

    def test_cleanup_sync_fault_after_commit_remains_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            recovery = root / ".honeymoney" / "publication" / "generation-cleanup-sync"
            publication = recovery.parent
            real_sync = workspace_publication._fsync_directory

            def stop_after_recovery_removal(path: Path) -> None:
                if path == publication and not recovery.exists():
                    raise OSError("synthetic cleanup sync stop")
                real_sync(path)

            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._fsync_directory",
                    side_effect=stop_after_recovery_removal,
                ),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-cleanup-sync",
                        [PublicationTarget("item.txt", b"new")],
                        b'{"generation_id":"generation-cleanup-sync"}\n',
                    )

            self.assertEqual(settle_retained_publication(root), "new")

    def test_precommit_failure_retains_state_and_doctor_restores_old_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "corrections.csv"
            target.write_bytes(b"old\n")
            real_replace = os.replace

            def fail_index(source: object, destination: object) -> None:
                if Path(destination).name == "workspace-index.json":
                    raise OSError("stop before commit")
                real_replace(source, destination)

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", fail_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-2",
                        [PublicationTarget("corrections.csv", b"new\n")],
                        b'{"generation_id":"generation-2"}\n',
                    )
            self.assertEqual(target.read_bytes(), b"new\n")
            self.assertEqual(inspect_retained_publication(root), "old")
            settle_retained_publication(root)
            self.assertEqual(target.read_bytes(), b"old\n")
            self.assertIsNone(inspect_retained_publication(root))

    def test_precommit_recovery_finalizes_an_interrupted_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_replace = os.replace

            def fail_index(source: object, destination: object) -> None:
                if Path(destination).name == "workspace-index.json":
                    raise OSError("stop before commit")
                real_replace(source, destination)

            reservation = _attempt_reservation()
            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", fail_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-attempt-old",
                        [],
                        b'{"generation_id":"generation-attempt-old"}\n',
                        attempt_reports=[reservation],
                    )
            report = root / reservation.path
            self.assertFalse(report.exists())
            self.assertEqual(settle_retained_publication(root), "old")
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["outcome"], "failure")
            self.assertEqual(value["error_codes"], ["interrupted"])

    def test_postcommit_recovery_finalizes_a_successful_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_replace = os.replace

            def stop_after_index(source: object, destination: object) -> None:
                real_replace(source, destination)
                if Path(destination).name == "workspace-index.json":
                    raise OSError("stop after commit")

            reservation = _attempt_reservation()
            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", stop_after_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-attempt-new",
                        [],
                        b'{"generation_id":"generation-attempt-new"}\n',
                        attempt_reports=[reservation],
                    )
            report = root / reservation.path
            self.assertFalse(report.exists())
            self.assertEqual(settle_retained_publication(root), "new")
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8"))["outcome"],
                "success",
            )

    def test_postcommit_failure_completes_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"old-one")
            second.write_bytes(b"old-two")
            real_replace = os.replace
            committed = False

            def stop_after_commit(source: object, destination: object) -> None:
                nonlocal committed
                real_replace(source, destination)
                if Path(destination).name == "workspace-index.json":
                    committed = True
                    raise OSError("stop after commit")

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", stop_after_commit),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-3",
                        [
                            PublicationTarget("first.txt", b"new-one"),
                            PublicationTarget("second.txt", b"new-two"),
                        ],
                        b'{"generation_id":"generation-3"}\n',
                    )
            self.assertTrue(committed)
            self.assertEqual(inspect_retained_publication(root), "new")
            settle_retained_publication(root)
            self.assertEqual(first.read_bytes(), b"new-one")
            self.assertEqual(second.read_bytes(), b"new-two")

    def test_recovery_refuses_changed_target_and_unsafe_or_value_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "item.txt"
            target.write_bytes(b"old")
            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._install_entry",
                    side_effect=OSError("synthetic"),
                ),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-4",
                        [PublicationTarget("item.txt", b"new")],
                        b'{"generation_id":"generation-4"}\n',
                    )
            target.write_bytes(b"other")
            with self.assertRaisesRegex(PublicationError, "item.txt"):
                settle_retained_publication(root)

            journal = root / ".honeymoney" / "publication-journal.json"
            document = json.loads(journal.read_text())
            document["amount"] = "1.00"
            journal.write_text(json.dumps(document))
            with self.assertRaises(PublicationError):
                inspect_retained_publication(root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with WorkspaceLock(root):
                with self.assertRaises(ValueError):
                    publish_generation(
                        root,
                        "generation-5",
                        [PublicationTarget("../escape", b"bad")],
                        b"{}",
                    )

    def test_staging_failure_restores_old_state_and_removal_is_recoverable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            removed = root / "obsolete.txt"
            removed.write_bytes(b"old")
            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._copy_new",
                    side_effect=OSError("stopped while staging"),
                ),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "generation-6",
                        [PublicationTarget("obsolete.txt", None)],
                        b'{"generation_id":"generation-6"}\n',
                    )
            self.assertEqual(inspect_retained_publication(root), "old")
            self.assertEqual(settle_retained_publication(root), "old")
            self.assertEqual(removed.read_bytes(), b"old")

            with WorkspaceLock(root):
                publish_generation(
                    root,
                    "generation-7",
                    [PublicationTarget("obsolete.txt", None)],
                    b'{"generation_id":"generation-7"}\n',
                )
            self.assertFalse(removed.exists())


if __name__ == "__main__":
    unittest.main()
