from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.import_records import (
    ImportRecordError,
    attempt_document,
    attempt_path,
    build_summary,
    import_record_path,
    initialize_record,
    load_attempts,
    read_transaction_snapshot,
    safe_source_label,
    summary_document,
    transaction_snapshot_document,
    validate_attempt_report,
    write_attempt,
)
from honeymoney.workspace_index import (
    WorkspaceIndexError,
    empty_workspace_index,
    load_workspace_index,
    parse_workspace_index,
    workspace_index_document,
    workspace_index_path,
)
from honeymoney.workspace_paths import (
    WorkspacePathError,
    WorkspacePaths,
    checked_workspace_path,
    reject_legacy_workspace,
)
from honeymoney.workspace_publication import (
    JOURNAL_SCHEMA_VERSION,
    AttemptReservation,
    PublicationError,
    PublicationTarget,
    WorkspaceBusyError,
    WorkspaceLock,
    inspect_lock,
    inspect_retained_publication,
    publish_generation,
    settle_retained_publication,
)
from honeymoney.workspace_setup import setup_workspace

SOURCE_ID = f"src_{'a' * 64}"
RECORD_ID = f"rec_{'b' * 64}"


def _report(number: int, *, outcome: str) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": 1,
        "honeymoney_version": "0.2.0",
        "source_id": SOURCE_ID,
        "source_label": safe_source_label(SOURCE_ID, "csv"),
        "attempt_number": number,
        "requested_action": "import",
        "started_at": "2026-08-08T00:00:00Z",
        "finished_at": "2026-08-08T00:00:01Z",
        "outcome": outcome,
        "source_revision": "c" * 64,
        "parser_contract": "d" * 64,
        "counts": {"statement_transaction_count": 0},
        "warnings": [],
        "warning_count": 0,
        "omitted_warning_count": 0,
        "error_codes": [] if outcome == "success" else ["interrupted"],
        "error_count": 0 if outcome == "success" else 1,
        "omitted_error_count": 0,
    }
    if outcome == "success":
        report["transactions_schema_version"] = 1
        report["transactions_digest"] = "e" * 64
    return report


def _attempt_reservation() -> AttemptReservation:
    return AttemptReservation(
        path=(f".honeymoney/import-records/{SOURCE_ID}/attempts/00000001.json"),
        success_content=attempt_document(_report(1, outcome="success")).encode(),
        interrupted_content=attempt_document(_report(1, outcome="failure")).encode(),
    )


def _index_bytes(generation_id: str) -> bytes:
    return json.dumps({"generation_id": generation_id}).encode() + b"\n"


class ImportRecordStorageCoverageTest(unittest.TestCase):
    def test_attempts_and_snapshots_reject_tampering_without_replacing_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            record = initialize_record(records, SOURCE_ID)

            with self.assertRaisesRegex(ImportRecordError, "source_id_invalid"):
                import_record_path(records, "source-id")
            for number in (False, 0, 100_000_000):
                with self.assertRaisesRegex(
                    ImportRecordError, "attempt_number_invalid"
                ):
                    attempt_path(record, number)

            invalid = _report(1, outcome="failure")
            invalid["warning_count"] = True
            with self.assertRaisesRegex(ImportRecordError, "attempt_report_invalid"):
                validate_attempt_report(invalid)

            report = _report(1, outcome="failure")
            written = write_attempt(record, report)
            self.assertEqual(
                written.read_text(encoding="utf-8"), attempt_document(report)
            )
            with self.assertRaisesRegex(
                ImportRecordError, "attempt_immutable_conflict"
            ):
                write_attempt(
                    record,
                    {**report, "source_label": safe_source_label(SOURCE_ID, "pdf")},
                )

            (record / "attempts" / "sidecar.txt").write_text(
                "synthetic", encoding="utf-8"
            )
            with self.assertRaisesRegex(ImportRecordError, "attempt_history_invalid"):
                load_attempts(record)
            (record / "attempts" / "sidecar.txt").unlink()
            self.assertEqual(load_attempts(record), [report])

            outside = root / "outside.json"
            outside.write_text(attempt_document(_report(2, outcome="failure")))
            os.symlink(outside, record / "attempts" / "00000002.json")
            with self.assertRaisesRegex(ImportRecordError, "attempt_history_invalid"):
                load_attempts(record)

    def test_snapshots_and_summaries_require_exact_durable_state(self) -> None:
        columns = ["source_record_id", "description"]
        with self.assertRaisesRegex(ImportRecordError, "transaction_snapshot_invalid"):
            transaction_snapshot_document([], [])
        with self.assertRaisesRegex(ImportRecordError, "transaction_snapshot_invalid"):
            transaction_snapshot_document(
                columns,
                [{"source_record_id": "not-a-record", "description": "Synthetic"}],
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "transactions.csv"
            snapshot.write_text(
                f"source_record_id,description\n{RECORD_ID},Synthetic,extra\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ImportRecordError, "transaction_snapshot_invalid"
            ):
                read_transaction_snapshot(snapshot, columns)

            record = initialize_record(root / "records", SOURCE_ID)
            write_attempt(record, _report(1, outcome="failure"))
            (record / "transactions.csv").write_text(
                "source_record_id\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ImportRecordError, "import_record_disagreement"
            ):
                build_summary(record, SOURCE_ID)
            with self.assertRaisesRegex(
                ImportRecordError, "import_record_summary_invalid"
            ):
                summary_document(
                    {
                        "schema_version": 1,
                        "source_id": SOURCE_ID,
                        "source_label": safe_source_label(SOURCE_ID, "csv"),
                        "ready": True,
                        "current_attempt_number": None,
                        "statement_transaction_count": 0,
                    }
                )


class WorkspaceAuthorityCoverageTest(unittest.TestCase):
    def test_workspace_index_requires_canonical_value_free_schema(self) -> None:
        proof = "f" * 64
        index = empty_workspace_index()
        index["registered_views"] = [
            {"period": "2026-01", "content_proof": proof},
            {"period": "undated", "content_proof": proof},
        ]
        index["input_proofs"] = [
            {"name": "config", "proof": proof},
            {"name": "rules", "proof": proof},
        ]
        document = workspace_index_document(index)
        self.assertEqual(parse_workspace_index(document), index)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                workspace_index_path(root),
                root / ".honeymoney" / "workspace-index.json",
            )
            with self.assertRaisesRegex(
                WorkspaceIndexError, "workspace_index_unreadable"
            ):
                load_workspace_index(root / "missing.json")

        for malformed in ("{", "[]"):
            with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
                parse_workspace_index(malformed)

        invalid = json.loads(document)
        invalid["contracts"]["import_record_schema_version"] = True
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(invalid)

        invalid = json.loads(document)
        invalid["registered_views"].reverse()
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(invalid)

        invalid = json.loads(document)
        invalid["input_proofs"].reverse()
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(invalid)

    def test_workspace_paths_reject_legacy_escapes_and_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            config = root / "config.json"
            config.write_text("{}\n", encoding="utf-8")
            paths = WorkspacePaths.from_config(config)

            self.assertEqual(paths.root, root.resolve())
            self.assertEqual(
                paths.relative(root / "profiles" / "starter.json"),
                "profiles/starter.json",
            )
            self.assertEqual(
                checked_workspace_path(paths, "profiles/new.json", must_exist=False),
                (root / "profiles" / "new.json").resolve(),
            )
            with self.assertRaisesRegex(WorkspacePathError, "leaves the workspace"):
                paths.relative(root.parent / "outside")
            with self.assertRaises(WorkspacePathError) as raised:
                checked_workspace_path(paths, "../outside", must_exist=False)
            self.assertEqual(raised.exception.code, "managed_path_unsafe")

            outside = Path(temporary) / "outside"
            outside.mkdir()
            os.symlink(outside, root / "linked")
            with self.assertRaises(WorkspacePathError) as raised:
                checked_workspace_path(paths, "linked", must_exist=False)
            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            with self.assertRaises(WorkspacePathError) as raised:
                reject_legacy_workspace(paths, {"paths": {}})
            self.assertEqual(raised.exception.code, "legacy_workspace_reset_required")

            (root / "categorized.csv").write_text("synthetic\n", encoding="utf-8")
            with self.assertRaises(WorkspacePathError) as raised:
                reject_legacy_workspace(paths)
            self.assertEqual(raised.exception.code, "legacy_workspace_reset_required")

    def test_setup_is_idempotent_and_refuses_conflicting_or_invalid_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            paths = setup_workspace(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            self.assertEqual(setup_workspace(root), paths)
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse(paths.views.exists())
            self.assertEqual(stat.S_IMODE(paths.internal.stat().st_mode), 0o700)

            paths.rules.write_text('{"rules":["changed"]}\n', encoding="utf-8")
            with self.assertRaises(WorkspacePathError) as raised:
                setup_workspace(root)
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            self.assertEqual(
                paths.rules.read_text(encoding="utf-8"), '{"rules":["changed"]}\n'
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "invalid"
            root.mkdir()
            (root / "config.json").write_text("{", encoding="utf-8")
            with self.assertRaises(WorkspacePathError) as raised:
                setup_workspace(root)
            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            self.assertFalse((root / ".honeymoney").exists())


class PublicationCoverageTest(unittest.TestCase):
    def test_publication_requires_owner_only_safe_targets_and_a_clean_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(WorkspaceBusyError):
                publish_generation(root, "no-lock", [], _index_bytes("no-lock"))

            with WorkspaceLock(root):
                with self.assertRaisesRegex(ValueError, "invalid generation"):
                    publish_generation(root, "bad/path", [], _index_bytes("bad/path"))
                with self.assertRaisesRegex(ValueError, "must be JSON"):
                    publish_generation(root, "bad-index", [], b"not-json")
                with self.assertRaisesRegex(ValueError, "owner-only mode"):
                    publish_generation(
                        root,
                        "bad-mode",
                        [PublicationTarget("item.txt", b"new", mode=0o644)],
                        _index_bytes("bad-mode"),
                    )
                with self.assertRaisesRegex(ValueError, "duplicate publication target"):
                    publish_generation(
                        root,
                        "duplicate",
                        [
                            PublicationTarget("item.txt", b"one"),
                            PublicationTarget("item.txt", b"two"),
                        ],
                        _index_bytes("duplicate"),
                    )
                with self.assertRaisesRegex(ValueError, "invalid workspace-relative"):
                    publish_generation(
                        root,
                        "escape",
                        [PublicationTarget("../escape", b"new")],
                        _index_bytes("escape"),
                    )

                outside = root / "outside"
                outside.mkdir()
                os.symlink(outside, root / "views")
                with self.assertRaisesRegex(ValueError, "symbolic links"):
                    publish_generation(
                        root,
                        "link-target",
                        [PublicationTarget("views/item.txt", b"new")],
                        _index_bytes("link-target"),
                    )
                (root / "views").unlink()

                (root / "directory-target").mkdir()
                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    publish_generation(
                        root,
                        "directory-target",
                        [PublicationTarget("directory-target", b"new")],
                        _index_bytes("directory-target"),
                    )

                journal = root / ".honeymoney" / "publication-journal.json"
                journal.write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(PublicationError, "recovery is required"):
                    publish_generation(root, "blocked", [], _index_bytes("blocked"))

    def test_attempt_reservations_require_canonical_matching_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reservation = _attempt_reservation()
            with WorkspaceLock(root):
                with self.assertRaisesRegex(
                    ValueError, "invalid attempt report target"
                ):
                    publish_generation(
                        root,
                        "bad-attempt-path",
                        [],
                        _index_bytes("bad-attempt-path"),
                        attempt_reports=[
                            AttemptReservation(
                                "attempts/00000001.json",
                                reservation.success_content,
                                reservation.interrupted_content,
                            )
                        ],
                    )
                with self.assertRaisesRegex(ValueError, "exceeds its size limit"):
                    publish_generation(
                        root,
                        "large-attempt",
                        [],
                        _index_bytes("large-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                b"x" * (64 * 1024 + 1),
                                reservation.interrupted_content,
                            )
                        ],
                    )
                with self.assertRaisesRegex(ValueError, "not valid JSON"):
                    publish_generation(
                        root,
                        "json-attempt",
                        [],
                        _index_bytes("json-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                b"not-json",
                                reservation.interrupted_content,
                            )
                        ],
                    )
                with self.assertRaisesRegex(ValueError, "not an object"):
                    publish_generation(
                        root,
                        "object-attempt",
                        [],
                        _index_bytes("object-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                b"[]",
                                reservation.interrupted_content,
                            )
                        ],
                    )
                with self.assertRaisesRegex(ValueError, "not canonical"):
                    publish_generation(
                        root,
                        "canonical-attempt",
                        [],
                        _index_bytes("canonical-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                reservation.success_content + b"\n",
                                reservation.interrupted_content,
                            )
                        ],
                    )

                wrong_number = attempt_document(_report(2, outcome="success")).encode()
                with self.assertRaisesRegex(ValueError, "target disagrees"):
                    publish_generation(
                        root,
                        "number-attempt",
                        [],
                        _index_bytes("number-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                wrong_number,
                                reservation.interrupted_content,
                            )
                        ],
                    )

                no_interruption = _report(1, outcome="failure")
                no_interruption["error_codes"] = []
                no_interruption["error_count"] = 0
                with self.assertRaisesRegex(ValueError, "lacks its error code"):
                    publish_generation(
                        root,
                        "interrupted-attempt",
                        [],
                        _index_bytes("interrupted-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                reservation.success_content,
                                attempt_document(no_interruption).encode(),
                            )
                        ],
                    )

                changed_label = _report(1, outcome="failure")
                changed_label["source_label"] = safe_source_label(SOURCE_ID, "pdf")
                with self.assertRaisesRegex(ValueError, "reservation disagrees"):
                    publish_generation(
                        root,
                        "mismatch-attempt",
                        [],
                        _index_bytes("mismatch-attempt"),
                        attempt_reports=[
                            AttemptReservation(
                                reservation.path,
                                reservation.success_content,
                                attempt_document(changed_label).encode(),
                            )
                        ],
                    )
                with self.assertRaisesRegex(ValueError, "duplicate attempt report"):
                    publish_generation(
                        root,
                        "duplicate-attempt",
                        [],
                        _index_bytes("duplicate-attempt"),
                        attempt_reports=[reservation, reservation],
                    )

    def test_locks_reject_changed_owners_and_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = WorkspaceLock(root)
            owner.release()
            owner.acquire()
            owner.path.write_text('{"pid":0,"schema_version":1}\n', encoding="utf-8")
            with self.assertRaisesRegex(PublicationError, "ownership changed"):
                owner.release()
            self.assertEqual(inspect_lock(root), "unknown")
            with self.assertRaisesRegex(WorkspaceBusyError, "unknown"):
                WorkspaceLock(root).acquire()

            owner.path.unlink()
            outside = root / "outside-lock"
            outside.write_text("synthetic\n", encoding="utf-8")
            os.symlink(outside, owner.path)
            self.assertEqual(inspect_lock(root), "unknown")
            with self.assertRaisesRegex(WorkspaceBusyError, "unknown"):
                WorkspaceLock(root).acquire()

    def test_retained_journal_schema_and_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "item.txt"
            target.write_bytes(b"old")
            real_replace = os.replace

            def stop_after_index(source: object, destination: object) -> None:
                real_replace(source, destination)
                if Path(destination).name == "workspace-index.json":
                    raise OSError("synthetic stop after commit")

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", stop_after_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "strict-journal",
                        [PublicationTarget("item.txt", b"new")],
                        _index_bytes("strict-journal"),
                    )

            journal = root / ".honeymoney" / "publication-journal.json"
            original = journal.read_text(encoding="utf-8")
            self.assertEqual(inspect_retained_publication(root), "new")

            for field, value in (
                ("schema_version", JOURNAL_SCHEMA_VERSION + 1),
                ("generation_id", "bad/path"),
                ("phase", "complete"),
                ("index_target", "item.txt"),
                ("index_commit_sha256", "not-a-digest"),
                ("entries", []),
                ("attempts", {}),
            ):
                invalid = json.loads(original)
                invalid[field] = value
                journal.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaisesRegex(
                    PublicationError, "publication state is invalid"
                ):
                    inspect_retained_publication(root)

            invalid = json.loads(original)
            invalid["entries"][0]["target"] = "../item.txt"
            journal.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(
                PublicationError, "publication state is invalid"
            ):
                inspect_retained_publication(root)

            invalid = json.loads(original)
            invalid["entries"][0]["mode"] = 0o644
            journal.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(
                PublicationError, "publication state is invalid"
            ):
                inspect_retained_publication(root)

            invalid = json.loads(original)
            invalid["entries"][0]["new_path"] = "wrong-retained-path"
            journal.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(
                PublicationError, "publication state is invalid"
            ):
                inspect_retained_publication(root)

            journal.unlink()
            outside = root / "journal-bytes"
            outside.write_text(original, encoding="utf-8")
            os.symlink(outside, journal)
            with self.assertRaisesRegex(PublicationError, "journal path is unsafe"):
                inspect_retained_publication(root)

    def test_publication_finalizes_one_immutable_attempt_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reservation = _attempt_reservation()
            with WorkspaceLock(root):
                publish_generation(
                    root,
                    "attempt-success",
                    [],
                    _index_bytes("attempt-success"),
                    attempt_reports=[reservation],
                )

            report = root / reservation.path
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8"))["outcome"], "success"
            )
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            with WorkspaceLock(root):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    publish_generation(
                        root,
                        "attempt-reuse",
                        [],
                        _index_bytes("attempt-reuse"),
                        attempt_reports=[reservation],
                    )

    def test_recovery_removes_a_proved_stale_lock_but_preserves_immutable_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reservation = _attempt_reservation()
            real_replace = os.replace

            def stop_after_index(source: object, destination: object) -> None:
                real_replace(source, destination)
                if Path(destination).name == "workspace-index.json":
                    raise OSError("synthetic stop after commit")

            with (
                WorkspaceLock(root),
                patch("honeymoney.workspace_publication.os.replace", stop_after_index),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "recovery-immutable",
                        [],
                        _index_bytes("recovery-immutable"),
                        attempt_reports=[reservation],
                    )

            report = root / reservation.path
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("conflicting synthetic bytes\n", encoding="utf-8")
            lock = root / ".honeymoney" / "workspace.lock"
            lock.write_text('{"pid":999999999,"schema_version":1}\n', encoding="utf-8")
            with patch(
                "honeymoney.workspace_publication.os.kill",
                side_effect=ProcessLookupError,
            ):
                self.assertEqual(inspect_lock(root), "stale")
                with self.assertRaisesRegex(
                    PublicationError, "attempt report is immutable"
                ):
                    settle_retained_publication(root)

            self.assertFalse(lock.exists())
            self.assertEqual(
                report.read_text(encoding="utf-8"), "conflicting synthetic bytes\n"
            )
            self.assertEqual(inspect_retained_publication(root), "new")

    def test_unknown_lock_and_malformed_journal_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            internal = root / ".honeymoney"
            internal.mkdir()
            lock = internal / "workspace.lock"
            lock.write_text("{}\n", encoding="utf-8")
            self.assertEqual(inspect_lock(root), "unknown")
            with self.assertRaisesRegex(WorkspaceBusyError, "unknown"):
                settle_retained_publication(root)

            lock.unlink()
            journal = internal / "publication-journal.json"
            journal.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                PublicationError, "publication state is invalid"
            ):
                inspect_retained_publication(root)


if __name__ == "__main__":
    unittest.main()
