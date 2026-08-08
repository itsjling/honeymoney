from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from hmac import new as new_hmac
from pathlib import Path
from unittest.mock import patch

from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.doctor import (
    FindingSeverity,
    RepairActionKind,
    RepairClass,
    audit_workspace,
    build_repair_plan,
    fix_workspace,
)
from honeymoney.import_records import (
    initialize_record,
    safe_source_label,
    write_attempt,
    write_summary,
)
from honeymoney.rates import empty_rate_cache, rate_cache_document
from honeymoney.workspace_index import (
    empty_workspace_index,
    load_workspace_index,
    write_workspace_index,
)
from honeymoney.workspace_publication import (
    PublicationError,
    PublicationTarget,
    WorkspaceLock,
    publish_generation,
)
from honeymoney.workspace_views import ViewUnit, build_view_unit


def _private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    os.chmod(path.parent, 0o700)


def _private_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, 0o600)
    os.chmod(path.parent, 0o700)


def _healthy_workspace(root: Path) -> None:
    root.mkdir(mode=0o700)
    _private_write(
        root / "config.json",
        json.dumps(
            {
                "profiles": ["profiles/starter.json"],
                "profile_mappings": "profile_mappings.json",
                "rules": "rules.json",
                "corrections": "corrections.csv",
                "rate_cache": "rates.json",
            },
            sort_keys=True,
        )
        + "\n",
    )
    _private_write(root / "profiles" / "starter.json", '{"id":"starter"}\n')
    _private_write(root / "profile_mappings.json", "{}\n")
    _private_write(root / "rules.json", '{"rules":[]}\n')
    _private_write(root / "rates.json", rate_cache_document(empty_rate_cache()))
    _private_write(root / "corrections.csv", ",".join(CORRECTION_COLUMNS) + "\n")
    records = root / ".honeymoney" / "import-records"
    records.mkdir(parents=True, mode=0o700)
    os.chmod(root / ".honeymoney", 0o700)
    os.chmod(records, 0o700)
    write_workspace_index(
        root / ".honeymoney" / "workspace-index.json", empty_workspace_index()
    )


def _failed_attempt(source_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "honeymoney_version": "0.2.0",
        "source_id": source_id,
        "source_label": safe_source_label(source_id, "csv"),
        "attempt_number": 1,
        "requested_action": "import",
        "started_at": "2026-08-08T00:00:00Z",
        "finished_at": "2026-08-08T00:00:01Z",
        "outcome": "failure",
        "source_revision": "a" * 64,
        "parser_contract": "b" * 64,
        "counts": {},
        "warnings": [],
        "warning_count": 0,
        "omitted_warning_count": 0,
        "error_codes": ["parse_failed"],
        "error_count": 1,
        "omitted_error_count": 0,
    }


def _write_config(root: Path, changes: dict[str, object]) -> None:
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    config.update(changes)
    _private_write(root / "config.json", json.dumps(config, sort_keys=True) + "\n")


def _add_input_proofs(root: Path, index: dict[str, object]) -> None:
    key = bytes.fromhex(
        str(index["overlap_manifest"]["namespace_key"]).removeprefix("ovns_")
    )
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    proof_paths = {
        "config": root / "config.json",
        "corrections": root / config["corrections"],
        "mappings": root / config["profile_mappings"],
        "rates": root / config["rate_cache"],
        "rules": root / config["rules"],
    }
    proof_paths.update(
        {
            f"profile-{number:04d}": root / value
            for number, value in enumerate(config["profiles"])
        }
    )
    index["input_proofs"] = [
        {
            "name": name,
            "proof": new_hmac(
                key,
                b"honeymoney-input-proof-v1\0"
                + name.encode("ascii")
                + b"\0"
                + path.read_bytes(),
                sha256,
            ).hexdigest(),
        }
        for name, path in sorted(proof_paths.items())
    ]


def _register_empty_view(root: Path, period: str) -> ViewUnit:
    index_path = root / ".honeymoney" / "workspace-index.json"
    index = load_workspace_index(index_path)
    key = bytes.fromhex(
        index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
    )
    unit = build_view_unit(period, (), content_proof_key=key)
    index["registered_views"] = [
        {"period": unit.period, "content_proof": unit.content_proof}
    ]
    _add_input_proofs(root, index)
    write_workspace_index(index_path, index)
    return unit


def _write_view(root: Path, unit: ViewUnit) -> None:
    for file in unit.files():
        assert file.content is not None
        _private_write_bytes(root / file.path, file.content)
    os.chmod(root / "views", 0o700)


class DoctorCoverageTest(unittest.TestCase):
    def _run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_audit_distinguishes_missing_and_non_directory_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            missing = audit_workspace(temporary_path / "missing")
            file_root = temporary_path / "workspace-file"
            file_root.write_text("synthetic\n", encoding="utf-8")
            non_directory = audit_workspace(file_root)

            self.assertFalse(missing.healthy)
            self.assertEqual(missing.exit_code, 2)
            self.assertEqual(
                [(finding.code, finding.path) for finding in missing.findings],
                [("workspace_input_invalid", None)],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in non_directory.findings],
                [("managed_path_unsafe", None)],
            )

    def test_audit_refuses_unsafe_lock_and_journal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            root = temporary_path / "workspace-lock"
            _healthy_workspace(root)
            outside = temporary_path / "outside-lock"
            outside.write_text("synthetic\n", encoding="utf-8")
            os.symlink(outside, root / ".honeymoney" / "workspace.lock")

            lock_result = audit_workspace(root)

            journal_root = temporary_path / "workspace-journal"
            _healthy_workspace(journal_root)
            journal_outside = temporary_path / "outside-journal"
            journal_outside.write_text("synthetic\n", encoding="utf-8")
            os.symlink(
                journal_outside,
                journal_root / ".honeymoney" / "publication-journal.json",
            )

            journal_result = audit_workspace(journal_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in lock_result.findings],
                [("managed_path_unsafe", ".honeymoney/workspace.lock")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in journal_result.findings],
                [("publication_state_invalid", ".honeymoney/publication-journal.json")],
            )

    def test_audit_blocks_unknown_lock_owners_and_invalid_journals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            root = temporary_path / "workspace-lock"
            _healthy_workspace(root)
            _private_write(root / ".honeymoney" / "workspace.lock", "{}\n")

            lock_audit = audit_workspace(root)
            lock_plan = build_repair_plan(root, audit=lock_audit)

            journal_root = temporary_path / "workspace-journal"
            _healthy_workspace(journal_root)
            _private_write(
                journal_root / ".honeymoney" / "publication-journal.json",
                "{}\n",
            )

            journal_audit = audit_workspace(journal_root)
            journal_plan = build_repair_plan(journal_root, audit=journal_audit)

            self.assertEqual(
                [finding.code for finding in lock_audit.findings],
                ["lock_owner_unknown"],
            )
            self.assertTrue(lock_plan.blocked)
            self.assertEqual(lock_plan.blocker_codes, ("lock_owner_unknown",))
            self.assertEqual(
                [finding.code for finding in journal_audit.findings],
                ["publication_state_invalid"],
            )
            self.assertTrue(journal_plan.blocked)
            self.assertEqual(journal_plan.blocker_codes, ("publication_state_invalid",))

    def test_audit_fails_closed_for_legacy_and_unsafe_configured_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            legacy_root = temporary_path / "workspace-legacy"
            _healthy_workspace(legacy_root)
            _write_config(legacy_root, {"paths": {}})

            legacy = audit_workspace(legacy_root)

            escaped_root = temporary_path / "workspace-escaped"
            _healthy_workspace(escaped_root)
            _write_config(escaped_root, {"rules": "../outside-rules.json"})

            escaped = audit_workspace(escaped_root)

            linked_root = temporary_path / "workspace-linked"
            _healthy_workspace(linked_root)
            outside = temporary_path / "outside-inputs"
            outside.mkdir()
            (outside / "rules.json").write_text('{"rules":[]}\n', encoding="utf-8")
            os.symlink(outside, linked_root / "inputs")
            _write_config(linked_root, {"rules": "inputs/rules.json"})

            linked = audit_workspace(linked_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in legacy.findings],
                [("workspace_input_invalid", "config.json")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in escaped.findings],
                [("managed_path_unsafe", "config.json")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in linked.findings],
                [("managed_path_unsafe", "config.json")],
            )

    def test_audit_reports_missing_config_and_import_record_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            config_root = temporary_path / "workspace-config"
            _healthy_workspace(config_root)
            (config_root / "config.json").unlink()

            missing_config = audit_workspace(config_root)

            records_root = temporary_path / "workspace-records"
            _healthy_workspace(records_root)
            (records_root / ".honeymoney" / "import-records").rmdir()

            missing_records = audit_workspace(records_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in missing_config.findings],
                [("workspace_input_invalid", "config.json")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in missing_records.findings],
                [("managed_metadata_invalid", ".honeymoney/import-records")],
            )

    def test_audit_rejects_blank_and_unsafe_configured_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            cases = (
                (
                    "blank-input",
                    {"rules": " "},
                    ("workspace_input_invalid", "config.json"),
                ),
                (
                    "empty-profiles",
                    {"profiles": []},
                    ("workspace_input_invalid", "config.json"),
                ),
                (
                    "blank-profile",
                    {"profiles": [" "]},
                    ("workspace_input_invalid", "config.json"),
                ),
                (
                    "escaped-profile",
                    {"profiles": ["../outside-profile.json"]},
                    ("managed_path_unsafe", "config.json"),
                ),
            )
            for name, changes, expected in cases:
                with self.subTest(name=name):
                    root = temporary_path / name
                    _healthy_workspace(root)
                    _write_config(root, changes)

                    result = audit_workspace(root)

                    self.assertEqual(
                        [(finding.code, finding.path) for finding in result.findings],
                        [expected],
                    )

    def test_audit_validates_each_configured_input_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            cases = (
                ("mappings", "profile_mappings.json", "[]\n"),
                ("rules", "rules.json", '{"rules":{}}\n'),
                ("rates", "rates.json", "{}\n"),
                ("profile", "profiles/starter.json", '{"id":""}\n'),
            )
            for name, relative, content in cases:
                with self.subTest(name=name):
                    root = temporary_path / name
                    _healthy_workspace(root)
                    _private_write(root / relative, content)

                    result = audit_workspace(root)

                    self.assertEqual(
                        [(finding.code, finding.path) for finding in result.findings],
                        [("workspace_input_invalid", relative)],
                    )

    def test_hard_linked_config_blocks_every_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            os.link(root / "config.json", root / "config-copy.json")

            audit = audit_workspace(root)
            fixed = fix_workspace(root)

            self.assertEqual(
                [
                    (finding.code, finding.severity, finding.path)
                    for finding in audit.findings
                ],
                [("managed_path_unsafe", FindingSeverity.ERROR, "config.json")],
            )
            self.assertTrue(fixed.plan.blocked)
            self.assertEqual(fixed.applied_actions, ())
            self.assertTrue((root / "config-copy.json").exists())

    def test_audit_blocks_corrupt_index_and_incomplete_input_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            corrupt_root = temporary_path / "workspace-corrupt"
            _healthy_workspace(corrupt_root)
            _private_write(
                corrupt_root / ".honeymoney" / "workspace-index.json",
                "not-json\n",
            )

            corrupt = audit_workspace(corrupt_root)

            proof_root = temporary_path / "workspace-proofs"
            _healthy_workspace(proof_root)
            index_path = proof_root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            index["input_proofs"] = [{"name": "config", "proof": "a" * 64}]
            write_workspace_index(index_path, index)

            incomplete = audit_workspace(proof_root)
            incomplete_plan = build_repair_plan(proof_root, audit=incomplete)

            self.assertEqual(
                [(finding.code, finding.path) for finding in corrupt.findings],
                [("workspace_index_invalid", ".honeymoney/workspace-index.json")],
            )
            self.assertEqual(
                [finding.code for finding in incomplete.findings],
                ["full_rebuild_required", "workspace_index_invalid"],
            )
            self.assertTrue(incomplete_plan.blocked)
            self.assertEqual(
                incomplete_plan.blocker_codes, ("workspace_index_invalid",)
            )

    def test_audit_requires_a_newer_release_for_a_future_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            index["contracts"]["honeymoney_version"] = "0.2.1"
            write_workspace_index(index_path, index)

            result = audit_workspace(root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in result.findings],
                [("newer_honeymoney_required", ".honeymoney/workspace-index.json")],
            )

    def test_audit_preserves_unknown_import_record_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(
                root / ".honeymoney" / "import-records" / "untracked-record",
                "keep\n",
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in result.findings],
                [
                    (
                        "unknown_managed_entry",
                        ".honeymoney/import-records/untracked-record",
                    )
                ],
            )

    def test_audit_rejects_structurally_corrupt_import_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source_id = "src_" + "1" * 64

            file_root = temporary_path / "workspace-file"
            _healthy_workspace(file_root)
            _private_write(
                file_root / ".honeymoney" / "import-records" / source_id,
                "not a record directory\n",
            )

            file_result = audit_workspace(file_root)

            attempts_root = temporary_path / "workspace-attempts"
            _healthy_workspace(attempts_root)
            record = initialize_record(
                attempts_root / ".honeymoney" / "import-records", source_id
            )
            (record / "attempts").rmdir()

            attempts_result = audit_workspace(attempts_root)

            entry_root = temporary_path / "workspace-entry"
            _healthy_workspace(entry_root)
            entry_record = initialize_record(
                entry_root / ".honeymoney" / "import-records", source_id
            )
            _private_write(entry_record / "attempts" / "note.txt", "keep\n")

            entry_result = audit_workspace(entry_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in file_result.findings],
                [("import_record_invalid", f".honeymoney/import-records/{source_id}")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in attempts_result.findings],
                [
                    (
                        "attempt_history_invalid",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in entry_result.findings],
                [
                    (
                        "attempt_history_invalid",
                        f".honeymoney/import-records/{source_id}/attempts",
                    )
                ],
            )

    def test_audit_requires_a_newer_release_for_a_future_attempt_schema(self) -> None:
        source_id = "src_" + "2" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            future_attempt = _failed_attempt(source_id)
            future_attempt["schema_version"] = 2
            _private_write(
                record / "attempts" / "00000001.json",
                json.dumps(future_attempt, sort_keys=True, separators=(",", ":"))
                + "\n",
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in result.findings],
                [
                    (
                        "newer_honeymoney_required",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )

    def test_audit_rejects_a_ready_record_without_its_current_snapshot(self) -> None:
        source_id = "src_" + "8" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            attempt = _failed_attempt(source_id)
            attempt.update(
                {
                    "outcome": "success",
                    "error_codes": [],
                    "error_count": 0,
                    "transactions_schema_version": 1,
                    "transactions_digest": "a" * 64,
                }
            )
            write_attempt(record, attempt)

            result = audit_workspace(root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in result.findings],
                [
                    (
                        "import_record_invalid",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )

    def test_audit_rejects_conflicting_durable_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            setup = self._run_cli("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Item,-1.00,HKD\n",
                encoding="utf-8",
            )
            imported = self._run_cli(
                "import",
                str(source),
                "--config",
                str(root / "config.json"),
                "--no-interactive",
                "--json",
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)

            records = root / ".honeymoney" / "import-records"
            record = next(records.iterdir())
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            transaction_id = index["overlap_manifest"]["groups"][0]["slots"][0][
                "transaction_id"
            ]
            invalid_correction = [
                transaction_id,
                "not-a-category",
                *["" for _ in CORRECTION_COLUMNS[2:]],
            ]
            _private_write(
                root / "corrections.csv",
                ",".join(CORRECTION_COLUMNS)
                + "\n"
                + ",".join(invalid_correction)
                + "\n",
            )
            _add_input_proofs(root, index)
            write_workspace_index(index_path, index)

            invalid_corrections = audit_workspace(root)

            _private_write(
                root / "corrections.csv", ",".join(CORRECTION_COLUMNS) + "\n"
            )
            _add_input_proofs(root, index)
            indexed_source = index["identity_manifest"]["sources"][0]
            revision = indexed_source["source_revision"]
            indexed_source["source_revision"] = revision[:-1] + (
                "a" if revision[-1] != "a" else "b"
            )
            write_workspace_index(index_path, index)

            mismatch = audit_workspace(root)
            shutil.rmtree(record)

            missing_record = audit_workspace(root)

            self.assertEqual(
                [
                    (finding.code, finding.path)
                    for finding in invalid_corrections.findings
                ],
                [("corrections_invalid", "corrections.csv")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in mismatch.findings],
                [
                    (
                        "durable_state_conflict",
                        f".honeymoney/import-records/{record.name}",
                    )
                ],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in missing_record.findings],
                [("durable_state_conflict", ".honeymoney/workspace-index.json")],
            )

    def test_audit_reports_unknown_and_symbolic_link_record_entries(self) -> None:
        source_id = "src_" + "3" * 64
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            unknown_root = temporary_path / "workspace-unknown"
            _healthy_workspace(unknown_root)
            record = initialize_record(
                unknown_root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(record, _failed_attempt(source_id))
            write_summary(record, source_id)
            _private_write(record / "note.txt", "keep\n")

            unknown = audit_workspace(unknown_root)

            linked_root = temporary_path / "workspace-linked"
            _healthy_workspace(linked_root)
            linked_record = initialize_record(
                linked_root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(linked_record, _failed_attempt(source_id))
            write_summary(linked_record, source_id)
            outside = temporary_path / "outside-record-note"
            outside.write_text("keep\n", encoding="utf-8")
            os.symlink(outside, linked_record / "note.txt")

            linked = audit_workspace(linked_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in unknown.findings],
                [
                    (
                        "unknown_managed_entry",
                        f".honeymoney/import-records/{source_id}/note.txt",
                    )
                ],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in linked.findings],
                [
                    (
                        "managed_path_unsafe",
                        f".honeymoney/import-records/{source_id}/note.txt",
                    )
                ],
            )

    def test_audit_rejects_corrupt_saved_correction_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            blank_row = ["", "set", *["" for _ in CORRECTION_COLUMNS[2:]]]
            unknown_row = [
                "view_synthetic",
                *["" for _ in CORRECTION_COLUMNS[1:]],
            ]
            cases = (
                ("short", ["view_synthetic"]),
                ("blank", blank_row),
                ("unknown", unknown_row),
            )
            for name, row in cases:
                with self.subTest(name=name):
                    root = temporary_path / name
                    _healthy_workspace(root)
                    _private_write(
                        root / "corrections.csv",
                        ",".join(CORRECTION_COLUMNS) + "\n" + ",".join(row) + "\n",
                    )

                    result = audit_workspace(root)

                    self.assertEqual(
                        [(finding.code, finding.path) for finding in result.findings],
                        [("corrections_invalid", "corrections.csv")],
                    )

    def test_audit_refuses_symbolic_link_record_metadata(self) -> None:
        source_id = "src_" + "4" * 64
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            cases = ("attempts", "transactions.csv", "summary.json")
            for name in cases:
                with self.subTest(name=name):
                    root = temporary_path / name
                    _healthy_workspace(root)
                    record = initialize_record(
                        root / ".honeymoney" / "import-records", source_id
                    )
                    target = record / name
                    if name == "attempts":
                        target.rmdir()
                    outside = temporary_path / f"outside-{name}"
                    outside.write_text("synthetic\n", encoding="utf-8")
                    os.symlink(outside, target)

                    result = audit_workspace(root)

                    self.assertEqual(
                        [(finding.code, finding.path) for finding in result.findings],
                        [
                            (
                                "managed_path_unsafe",
                                f".honeymoney/import-records/{source_id}/{name}",
                            )
                        ],
                    )

    def test_fix_combines_stale_lock_and_summary_repair(self) -> None:
        source_id = "src_" + "5" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(record, _failed_attempt(source_id))
            _private_write(
                root / ".honeymoney" / "workspace.lock",
                '{"pid":99999999,"schema_version":1}\n',
            )

            fixed = fix_workspace(root)

            self.assertEqual(
                [action.kind for action in fixed.applied_actions],
                [RepairActionKind.REMOVE_STALE_LOCK, RepairActionKind.REBUILD_SUMMARY],
            )
            self.assertTrue(fixed.healthy)
            self.assertTrue((record / "summary.json").is_file())
            self.assertFalse((root / ".honeymoney" / "workspace.lock").exists())

    def test_fix_rebuilds_a_summary_after_retained_publication_settlement(self) -> None:
        source_id = "src_" + "6" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(record, _failed_attempt(source_id))

            with (
                WorkspaceLock(root),
                patch(
                    "honeymoney.workspace_publication._install_entry",
                    side_effect=OSError("synthetic stop"),
                ),
            ):
                with self.assertRaises(PublicationError):
                    publish_generation(
                        root,
                        "doctor-follow-up",
                        [PublicationTarget("corrections.csv", b"replacement\n")],
                        b'{"generation_id":"doctor-follow-up"}\n',
                    )

            fixed = fix_workspace(root)

            self.assertEqual(
                [action.kind for action in fixed.applied_actions],
                [
                    RepairActionKind.SETTLE_RETAINED_PUBLICATION,
                    RepairActionKind.REBUILD_SUMMARY,
                ],
            )
            self.assertTrue(fixed.healthy)
            self.assertTrue((record / "summary.json").is_file())

    def test_fix_repairs_a_configured_input_parent_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(
                root / "saved" / "choices.csv", ",".join(CORRECTION_COLUMNS) + "\n"
            )
            _write_config(root, {"corrections": "saved/choices.csv"})
            os.chmod(root / "saved", 0o755)

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in audit.findings],
                [("managed_metadata_invalid", "saved")],
            )
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [(RepairActionKind.SET_PRIVATE_MODE, "saved")],
            )
            self.assertEqual(stat.S_IMODE((root / "saved").stat().st_mode), 0o700)
            self.assertTrue(fixed.healthy)

    def test_fix_repairs_the_disposable_report_preview_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            preview = root / ".honeymoney" / "report-preview.html"
            _private_write(preview, "<p>synthetic</p>\n")
            os.chmod(preview, 0o644)

            audit = audit_workspace(root)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in audit.findings],
                [("managed_metadata_invalid", ".honeymoney/report-preview.html")],
            )
            self.assertEqual(
                [action.kind for action in fixed.applied_actions],
                [RepairActionKind.SET_PRIVATE_MODE],
            )
            self.assertEqual(stat.S_IMODE(preview.stat().st_mode), 0o600)
            self.assertTrue(fixed.healthy)

    def test_audit_refuses_unsafe_internal_publication_and_preview_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            publication_root = temporary_path / "workspace-publication"
            _healthy_workspace(publication_root)
            _private_write(
                publication_root / ".honeymoney" / "publication",
                "not a directory\n",
            )

            publication = audit_workspace(publication_root)

            preview_root = temporary_path / "workspace-preview"
            _healthy_workspace(preview_root)
            outside = temporary_path / "outside-preview.html"
            outside.write_text("synthetic\n", encoding="utf-8")
            os.symlink(outside, preview_root / ".honeymoney" / "report-preview.html")

            preview = audit_workspace(preview_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in publication.findings],
                [("managed_path_unsafe", ".honeymoney/publication")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in preview.findings],
                [("managed_path_unsafe", ".honeymoney/report-preview.html")],
            )

    def test_audit_repairs_missing_view_and_rejects_unsafe_view_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            root = temporary_path / "workspace-root"
            _healthy_workspace(root)
            _register_empty_view(root, "2026-06")

            missing = audit_workspace(root)
            missing_fixed = fix_workspace(root)

            link_root = temporary_path / "workspace-link"
            _healthy_workspace(link_root)
            _register_empty_view(link_root, "2026-06")
            outside = temporary_path / "outside-views"
            outside.mkdir()
            os.symlink(outside, link_root / "views")

            linked = audit_workspace(link_root)

            self.assertEqual(
                [
                    (finding.code, finding.repair_class, finding.path)
                    for finding in missing.findings
                ],
                [("generated_view_invalid", RepairClass.SAFE, "views/2026-06")],
            )
            self.assertEqual(
                [action.kind for action in missing_fixed.applied_actions],
                [RepairActionKind.REBUILD_GENERATED_VIEW] * 3,
            )
            self.assertTrue(missing_fixed.after.healthy)
            self.assertEqual(
                [(finding.code, finding.path) for finding in linked.findings],
                [("managed_path_unsafe", "views")],
            )

    def test_audit_checks_registered_view_directories_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            file_root = temporary_path / "workspace-file"
            _healthy_workspace(file_root)
            _register_empty_view(file_root, "2026-08")
            _private_write(file_root / "views", "not a directory\n")

            file_result = audit_workspace(file_root)

            link_root = temporary_path / "workspace-link"
            _healthy_workspace(link_root)
            _register_empty_view(link_root, "2026-08")
            (link_root / "views").mkdir(mode=0o700)
            outside = temporary_path / "outside-view"
            outside.mkdir()
            os.symlink(outside, link_root / "views" / "2026-08")

            link_result = audit_workspace(link_root)

            missing_root = temporary_path / "workspace-missing"
            _healthy_workspace(missing_root)
            missing_unit = _register_empty_view(missing_root, "2026-08")
            _write_view(missing_root, missing_unit)
            (missing_root / "views" / "2026-08" / "transactions.csv").unlink()

            missing_result = audit_workspace(missing_root)

            unknown_root = temporary_path / "workspace-unknown"
            _healthy_workspace(unknown_root)
            unknown_unit = _register_empty_view(unknown_root, "2026-08")
            _write_view(unknown_root, unknown_unit)
            (unknown_root / "views" / "old-output").mkdir(mode=0o700)

            unknown_result = audit_workspace(unknown_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in file_result.findings],
                [("managed_path_unsafe", "views")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in link_result.findings],
                [("managed_path_unsafe", "views/2026-08")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in missing_result.findings],
                [("generated_view_invalid", "views/2026-08")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in unknown_result.findings],
                [("unknown_managed_entry", "views/old-output")],
            )

    def test_audit_refuses_symbolic_link_view_files_and_preserves_unknown_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            linked_root = temporary_path / "workspace-linked"
            _healthy_workspace(linked_root)
            linked_unit = _register_empty_view(linked_root, "2026-07")
            _write_view(linked_root, linked_unit)
            target = linked_root / "views" / "2026-07" / "report.html"
            outside = temporary_path / "outside-report.html"
            outside.write_text("synthetic\n", encoding="utf-8")
            target.unlink()
            os.symlink(outside, target)

            linked = audit_workspace(linked_root)

            unknown_root = temporary_path / "workspace-unknown"
            _healthy_workspace(unknown_root)
            unknown_unit = _register_empty_view(unknown_root, "2026-07")
            _write_view(unknown_root, unknown_unit)
            _private_write(unknown_root / "views" / "2026-07" / "notes.txt", "keep\n")

            unknown = audit_workspace(unknown_root)

            self.assertEqual(
                [(finding.code, finding.path) for finding in linked.findings],
                [("managed_path_unsafe", "views/2026-07/report.html")],
            )
            self.assertEqual(
                [(finding.code, finding.path) for finding in unknown.findings],
                [("unknown_managed_entry", "views/2026-07/notes.txt")],
            )


if __name__ == "__main__":
    unittest.main()
