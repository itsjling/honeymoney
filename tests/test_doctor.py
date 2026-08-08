from __future__ import annotations

import json
import os
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
    write_transaction_snapshot,
)
from honeymoney.overlap import canonicalize_overlaps, empty_overlap_manifest
from honeymoney.rates import empty_rate_cache, rate_cache_document
from honeymoney.workspace_commands import import_workspace
from honeymoney.workspace_index import (
    WORKSPACE_INDEX_SCHEMA_VERSION,
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
from honeymoney.workspace_views import (
    VIEW_FILE_NAMES,
    build_view_unit,
    view_content_proof,
)


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


class DoctorAuditTest(unittest.TestCase):
    def _run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_audit_reports_a_clean_synthetic_workspace_as_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)

            result = audit_workspace(root)

            self.assertTrue(result.healthy)
            self.assertEqual(result.findings, ())

    def test_cli_doctor_honors_a_custom_config_name_for_audit_and_fix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            config = root / "custom-name.json"
            (root / "config.json").replace(config)

            healthy = self._run_cli("doctor", "--config", str(config), "--json")

            self.assertEqual(healthy.returncode, 0, healthy.stderr)
            self.assertEqual(json.loads(healthy.stdout)["data"]["finding_count"], 0)

            os.chmod(root / "rules.json", 0o644)

            fixed = self._run_cli("doctor", "--fix", "--config", str(config), "--json")

            self.assertEqual(fixed.returncode, 0, fixed.stderr)
            self.assertEqual(json.loads(fixed.stdout)["data"]["finding_count"], 0)
            self.assertGreater(json.loads(fixed.stdout)["data"]["repaired_count"], 0)
            self.assertEqual(stat.S_IMODE((root / "rules.json").stat().st_mode), 0o600)

    def test_audit_accepts_a_setup_and_committed_synthetic_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            setup = self._run_cli("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
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

            result = audit_workspace(root)

            self.assertTrue(result.healthy)
            self.assertEqual(result.findings, ())

    def test_audit_accepts_a_model_allowed_unavailable_ollama_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            setup = self._run_cli("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["ollama"] = {
                "enabled": True,
                "url": "http://127.0.0.1:9/api/generate",
                "model": "synthetic-local-model",
                "timeout_seconds": 0.1,
            }
            _private_write(
                config_path,
                json.dumps(config, ensure_ascii=False, sort_keys=True) + "\n",
            )
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
                encoding="utf-8",
            )

            with patch(
                "honeymoney.ollama._request_ollama",
                side_effect=OSError("synthetic Ollama unavailable"),
            ):
                imported = import_workspace(
                    source,
                    config_path=config_path,
                    interactive=False,
                )

            self.assertTrue(
                any(
                    warning.startswith("Ollama unavailable:")
                    for warning in imported.warnings
                )
            )
            with patch(
                "honeymoney.workspace_commands.derive_workspace_for_repair",
                side_effect=AssertionError("doctor must not rederive model output"),
            ) as rederive:
                result = audit_workspace(root)

            rederive.assert_not_called()
            self.assertTrue(result.healthy)
            self.assertEqual(result.findings, ())

            (root / "views" / "2026-08" / "report.html").write_bytes(b"changed")
            with patch(
                "honeymoney.workspace_commands.derive_workspace_for_repair",
                side_effect=AssertionError("doctor must not rederive model output"),
            ) as rederive:
                damaged = audit_workspace(root)
                plan = build_repair_plan(root, audit=damaged)
                fixed = fix_workspace(root)

            rederive.assert_not_called()
            self.assertEqual(
                [
                    (item.code, item.repair_class, item.path)
                    for item in damaged.findings
                ],
                [
                    (
                        "generated_view_invalid",
                        RepairClass.FULL_REBUILD,
                        "views/2026-08",
                    )
                ],
            )
            self.assertEqual(plan.actions, ())
            self.assertEqual(fixed.applied_actions, ())
            self.assertEqual(fixed.after.findings, damaged.findings)

    def test_audit_rejects_overlap_support_without_active_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            source_id = "src_" + "a" * 64
            occurrence = {
                "transaction_id": "txn_" + "b" * 32,
                "source_id": source_id,
                "source_namespace_id": "ns_" + "a" * 64,
                "source_revision": "rev_" + "a" * 64,
                "source_record_id": "rec_" + "a" * 64,
                "account_id": "checking",
                "date": "2026-08-08",
                "transaction_date": "2026-08-08",
                "posting_date": "",
                "original_amount": "-12",
                "original_currency": "HKD",
                "posted_amount": "-12",
                "posted_currency": "HKD",
                "merchant": "Synthetic Shop",
                "original_description": "Synthetic Shop",
            }
            index["overlap_manifest"] = canonicalize_overlaps(
                [occurrence],
                [],
                empty_overlap_manifest(index["overlap_manifest"]["namespace_key"]),
            ).manifest
            _private_write(
                index_path,
                json.dumps(
                    index, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                + "\n",
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("workspace_index_invalid", ".honeymoney/workspace-index.json")],
            )

    def test_audit_rejects_a_snapshot_with_a_changed_header_and_matching_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            setup = self._run_cli("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
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
            record = next((root / ".honeymoney" / "import-records").iterdir())
            snapshot = record / "transactions.csv"
            changed = snapshot.read_text(encoding="utf-8").replace(
                "original_amount", "changed_amount", 1
            )
            snapshot.write_text(changed, encoding="utf-8")
            attempt_path = record / "attempts" / "00000001.json"
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["transactions_digest"] = sha256(changed.encode("utf-8")).hexdigest()
            attempt_path.write_text(
                json.dumps(
                    attempt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [
                    (
                        "import_record_invalid",
                        f".honeymoney/import-records/{record.name}",
                    )
                ],
            )

    def test_audit_rejects_changed_snapshot_facts_with_a_matching_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            setup = self._run_cli("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
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
            record = next((root / ".honeymoney" / "import-records").iterdir())
            snapshot = record / "transactions.csv"
            changed = snapshot.read_text(encoding="utf-8").replace("-12.00", "-99.00")
            snapshot.write_text(changed, encoding="utf-8")
            attempt_path = record / "attempts" / "00000001.json"
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt["transactions_digest"] = sha256(changed.encode("utf-8")).hexdigest()
            attempt_path.write_text(
                json.dumps(
                    attempt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [
                    (
                        "durable_state_conflict",
                        f".honeymoney/import-records/{record.name}",
                    )
                ],
            )

    def test_audit_rederives_registered_views_instead_of_trusting_their_proof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            setup = self._run_cli("setup", "--root", str(root), "--json")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            source = root / "synthetic.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Grocer,-12.00,HKD\n",
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
            period = "2026-08"
            view = root / "views" / period
            files = {name: (view / name).read_bytes() for name in VIEW_FILE_NAMES}
            files["transactions.csv"] = files["transactions.csv"].replace(
                b"-12.00", b"-99.00"
            )
            (view / "transactions.csv").write_bytes(files["transactions.csv"])
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            key = bytes.fromhex(
                index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
            )
            index["registered_views"][0]["content_proof"] = view_content_proof(
                period, files, content_proof_key=key
            )
            write_workspace_index(index_path, index)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("durable_state_conflict", ".honeymoney/workspace-index.json")],
            )

    def test_audit_stops_when_a_live_lock_owns_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)

            with WorkspaceLock(root):
                result = audit_workspace(root)

            self.assertEqual(
                [
                    (item.code, item.severity, item.repair_class)
                    for item in result.findings
                ],
                [("workspace_busy", FindingSeverity.ERROR, RepairClass.NONE)],
            )

    def test_audit_rejects_a_retained_publication_with_changed_target_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            target = root / "corrections.csv"

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
                        "doctor-journal",
                        [PublicationTarget("corrections.csv", b"new\n")],
                        b'{"generation_id":"doctor-journal"}\n',
                    )
            target.write_text("not valid csv state\n", encoding="utf-8")

            result = audit_workspace(root)

            self.assertEqual(
                [
                    (item.code, item.severity, item.repair_class)
                    for item in result.findings
                ],
                [
                    (
                        "publication_state_invalid",
                        FindingSeverity.ERROR,
                        RepairClass.MANUAL,
                    )
                ],
            )

    def test_fix_settles_a_proved_retained_publication_before_reauditing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
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
                        "doctor-settlement",
                        [PublicationTarget("corrections.csv", b"new\n")],
                        b'{"generation_id":"doctor-settlement"}\n',
                    )

            fixed = fix_workspace(root)

            self.assertEqual(
                [action.kind for action in fixed.applied_actions],
                [RepairActionKind.SETTLE_RETAINED_PUBLICATION],
            )
            self.assertFalse(
                (root / ".honeymoney" / "publication-journal.json").exists()
            )
            self.assertTrue(fixed.after.healthy)

    def test_audit_rejects_a_non_object_config_before_trusting_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            (root / "config.json").write_text("[]\n", encoding="utf-8")

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("workspace_input_invalid", "config.json")],
            )

    def test_audit_rejects_a_symbolic_link_config_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            config = root / "config.json"
            outside = Path(temporary) / "outside-config.json"
            config.rename(outside)
            os.symlink(outside, config)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("managed_path_unsafe", "config.json")],
            )

    def test_audit_rejects_a_symbolic_link_internal_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            internal = root / ".honeymoney"
            outside = Path(temporary) / "outside-internal"
            internal.rename(outside)
            os.symlink(outside, internal)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("managed_path_unsafe", ".honeymoney")],
            )

    def test_fix_rebuilds_a_missing_disposable_summary_then_reaudits(self) -> None:
        source_id = "src_" + "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(record, _failed_attempt(source_id))

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [
                    (
                        "summary_invalid",
                        f".honeymoney/import-records/{source_id}/summary.json",
                    )
                ],
            )
            self.assertFalse(plan.blocked)
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [
                    (
                        RepairActionKind.REBUILD_SUMMARY,
                        f".honeymoney/import-records/{source_id}/summary.json",
                    )
                ],
            )
            self.assertTrue((record / "summary.json").exists())
            self.assertTrue(fixed.after.healthy)

    def test_hard_attempt_damage_blocks_every_safe_repair(self) -> None:
        good_source = "src_" + "a" * 64
        damaged_source = "src_" + "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            good = initialize_record(
                root / ".honeymoney" / "import-records", good_source
            )
            write_attempt(good, _failed_attempt(good_source))
            damaged = initialize_record(
                root / ".honeymoney" / "import-records", damaged_source
            )
            write_attempt(damaged, _failed_attempt(damaged_source))
            (damaged / "attempts" / "00000001.json").write_text(
                "{}\n", encoding="utf-8"
            )

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [item.code for item in audit.findings],
                ["attempt_history_invalid", "summary_invalid"],
            )
            self.assertTrue(plan.blocked)
            self.assertEqual(plan.blocker_codes, ("attempt_history_invalid",))
            self.assertEqual(plan.actions, ())
            self.assertFalse((good / "summary.json").exists())
            self.assertEqual(fixed.applied_actions, ())

    def test_audit_preserves_and_warns_about_unknown_managed_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(root / ".honeymoney" / "unregistered.txt", "keep\n")

            result = audit_workspace(root)

            self.assertEqual(
                [
                    (item.code, item.severity, item.repair_class, item.path)
                    for item in result.findings
                ],
                [
                    (
                        "unknown_managed_entry",
                        FindingSeverity.WARNING,
                        RepairClass.NONE,
                        ".honeymoney/unregistered.txt",
                    )
                ],
            )

    def test_audit_rejects_legacy_workspace_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(root / "categorized.csv", "synthetic legacy marker\n")

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("workspace_input_invalid", "categorized.csv")],
            )

    def test_audit_rejects_orphan_retained_publication_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(
                root / ".honeymoney" / "publication" / "orphan" / "new.bin",
                "retained bytes\n",
            )

            result = audit_workspace(root)
            plan = build_repair_plan(root, audit=result)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("publication_state_invalid", ".honeymoney/publication")],
            )
            self.assertTrue(plan.blocked)

    def test_audit_omits_an_unsafe_unknown_name_from_its_finding_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(root / ".honeymoney" / "unsafe\nname", "keep\n")

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("unknown_managed_entry", None)],
            )

    def test_audit_rejects_a_managed_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index = root / ".honeymoney" / "workspace-index.json"
            outside = Path(temporary) / "outside-index.json"
            outside.write_text("not a workspace index\n", encoding="utf-8")
            index.unlink()
            os.symlink(outside, index)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("managed_path_unsafe", ".honeymoney/workspace-index.json")],
            )

    def test_fix_restores_a_managed_directory_to_owner_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            records = root / ".honeymoney" / "import-records"
            os.chmod(records, 0o755)

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [("managed_metadata_invalid", ".honeymoney/import-records")],
            )
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [(RepairActionKind.SET_PRIVATE_MODE, ".honeymoney/import-records")],
            )
            self.assertEqual(stat.S_IMODE(records.stat().st_mode), 0o700)
            self.assertTrue(fixed.after.healthy)

    def test_fix_recreates_the_empty_import_record_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            records = root / ".honeymoney" / "import-records"
            records.rmdir()

            plan = build_repair_plan(root)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [
                    (
                        RepairActionKind.CREATE_PRIVATE_DIRECTORY,
                        ".honeymoney/import-records",
                    )
                ],
            )
            self.assertTrue(records.is_dir())
            self.assertEqual(stat.S_IMODE(records.stat().st_mode), 0o700)
            self.assertTrue(fixed.after.healthy)

    def test_fix_does_not_create_a_directory_before_its_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            records = root / ".honeymoney" / "import-records"
            records.rmdir()

            with patch(
                "honeymoney.workspace_publication._write_journal",
                side_effect=OSError("synthetic journal stop"),
            ):
                with self.assertRaises(PublicationError):
                    fix_workspace(root)

            self.assertFalse(records.exists())

    def test_fix_does_not_change_a_mode_before_its_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            records = root / ".honeymoney" / "import-records"
            os.chmod(records, 0o755)

            with patch(
                "honeymoney.workspace_publication._write_journal",
                side_effect=OSError("synthetic journal stop"),
            ):
                with self.assertRaises(PublicationError):
                    fix_workspace(root)

            self.assertEqual(stat.S_IMODE(records.stat().st_mode), 0o755)

    def test_fix_restores_the_workspace_root_to_owner_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            os.chmod(root, 0o755)

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [("managed_metadata_invalid", ".")],
            )
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [(RepairActionKind.SET_PRIVATE_MODE, ".")],
            )
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertTrue(fixed.after.healthy)

    def test_fix_restores_a_disposable_summary_to_owner_only_mode(self) -> None:
        source_id = "src_" + "7" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(record, _failed_attempt(source_id))
            write_summary(record, source_id)
            summary = record / "summary.json"
            os.chmod(summary, 0o644)

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [
                    (
                        "managed_metadata_invalid",
                        f".honeymoney/import-records/{source_id}/summary.json",
                    )
                ],
            )
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [
                    (
                        RepairActionKind.SET_PRIVATE_MODE,
                        f".honeymoney/import-records/{source_id}/summary.json",
                    )
                ],
            )
            self.assertEqual(stat.S_IMODE(summary.stat().st_mode), 0o600)
            self.assertTrue(fixed.after.healthy)

    def test_changed_proved_input_requires_a_full_rebuild_not_doctor_fix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            key = bytes.fromhex(
                index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
            )
            proof_paths = {
                "config": root / "config.json",
                "corrections": root / "corrections.csv",
                "mappings": root / "profile_mappings.json",
                "rates": root / "rates.json",
                "rules": root / "rules.json",
                "profile-0000": root / "profiles" / "starter.json",
            }
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
            write_workspace_index(index_path, index)
            self.assertTrue(audit_workspace(root).healthy)
            _private_write(root / "rules.json", '{"rules":[{"id":"new"}]}\n')

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(item.code, item.repair_class, item.path) for item in audit.findings],
                [("full_rebuild_required", RepairClass.FULL_REBUILD, "rules.json")],
            )
            self.assertFalse(plan.blocked)
            self.assertEqual(plan.actions, ())
            self.assertEqual(fixed.applied_actions, ())
            self.assertEqual(fixed.after.findings, audit.findings)

    def test_audit_accepts_a_missing_optional_user_owned_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            (root / "profile_mappings.json").unlink()

            result = audit_workspace(root)

            self.assertTrue(result.healthy)

    def test_audit_marks_a_missing_proved_optional_input_for_full_rebuild(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            key = bytes.fromhex(
                index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
            )
            proof_paths = {
                "config": root / "config.json",
                "corrections": root / "corrections.csv",
                "mappings": root / "profile_mappings.json",
                "rates": root / "rates.json",
                "rules": root / "rules.json",
                "profile-0000": root / "profiles" / "starter.json",
            }
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
            write_workspace_index(index_path, index)
            (root / "profile_mappings.json").unlink()

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.repair_class, item.path) for item in result.findings],
                [
                    (
                        "full_rebuild_required",
                        RepairClass.FULL_REBUILD,
                        "profile_mappings.json",
                    )
                ],
            )

    def test_audit_accepts_the_complete_current_input_proof_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            key = bytes.fromhex(
                index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
            )
            proof_paths = {
                "config": root / "config.json",
                "corrections": root / "corrections.csv",
                "mappings": root / "profile_mappings.json",
                "rates": root / "rates.json",
                "rules": root / "rules.json",
                "profile-0000": root / "profiles" / "starter.json",
            }
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
            write_workspace_index(index_path, index)

            result = audit_workspace(root)

            self.assertTrue(result.healthy)

    def test_invalid_saved_corrections_block_all_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(root / "corrections.csv", "wrong_header\n")

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [("corrections_invalid", "corrections.csv")],
            )
            self.assertTrue(plan.blocked)
            self.assertEqual(plan.blocker_codes, ("corrections_invalid",))

    def test_audit_checks_the_configured_corrections_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(root / "saved" / "choices.csv", "wrong_header\n")
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            config["corrections"] = "saved/choices.csv"
            _private_write(
                root / "config.json", json.dumps(config, sort_keys=True) + "\n"
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("corrections_invalid", "saved/choices.csv")],
            )

    def test_audit_reports_a_newer_workspace_index_as_an_upgrade_need(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            document = json.loads(index_path.read_text(encoding="utf-8"))
            document["schema_version"] = WORKSPACE_INDEX_SCHEMA_VERSION + 1
            _private_write(
                index_path,
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.repair_class, item.path) for item in result.findings],
                [
                    (
                        "newer_honeymoney_required",
                        RepairClass.MANUAL,
                        ".honeymoney/workspace-index.json",
                    )
                ],
            )

    def test_fix_rebuilds_a_registered_view_as_one_proved_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            key = bytes.fromhex(
                index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
            )
            unit = build_view_unit("2026-05", (), content_proof_key=key)
            index["registered_views"] = [
                {"period": unit.period, "content_proof": unit.content_proof}
            ]
            proof_paths = {
                "config": root / "config.json",
                "corrections": root / "corrections.csv",
                "mappings": root / "profile_mappings.json",
                "rates": root / "rates.json",
                "rules": root / "rules.json",
                "profile-0000": root / "profiles" / "starter.json",
            }
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
            write_workspace_index(index_path, index)
            for file in unit.files():
                assert file.content is not None
                _private_write_bytes(root / file.path, file.content)
            os.chmod(root / "views", 0o700)
            _private_write_bytes(root / "views" / "2026-05" / "report.html", b"changed")

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.repair_class, item.path) for item in result.findings],
                [
                    (
                        "generated_view_invalid",
                        RepairClass.SAFE,
                        "views/2026-05",
                    )
                ],
            )
            plan = build_repair_plan(root, audit=result)
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [
                    (
                        RepairActionKind.REBUILD_GENERATED_VIEW,
                        f"views/2026-05/{name}",
                    )
                    for name in ("report.html", "review_needed.csv", "transactions.csv")
                ],
            )
            published_paths: list[str] = []

            def capture_publish(
                workspace_root: Path,
                generation_id: str,
                targets: list[PublicationTarget],
                index_bytes: bytes,
            ) -> None:
                published_paths.extend(target.path for target in targets)
                publish_generation(
                    workspace_root,
                    generation_id,
                    targets,
                    index_bytes,
                )

            with patch(
                "honeymoney.doctor.publish_generation",
                side_effect=capture_publish,
            ):
                fixed = fix_workspace(root)

            self.assertEqual(
                sorted(published_paths),
                [f"views/2026-05/{name}" for name in sorted(VIEW_FILE_NAMES)],
            )
            for file in unit.files():
                assert file.content is not None
                self.assertEqual((root / file.path).read_bytes(), file.content)
            self.assertTrue(fixed.after.healthy)

    def test_fix_restores_a_registered_view_file_to_owner_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            key = bytes.fromhex(
                index["overlap_manifest"]["namespace_key"].removeprefix("ovns_")
            )
            unit = build_view_unit("2026-05", (), content_proof_key=key)
            index["registered_views"] = [
                {"period": unit.period, "content_proof": unit.content_proof}
            ]
            proof_paths = {
                "config": root / "config.json",
                "corrections": root / "corrections.csv",
                "mappings": root / "profile_mappings.json",
                "rates": root / "rates.json",
                "rules": root / "rules.json",
                "profile-0000": root / "profiles" / "starter.json",
            }
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
            write_workspace_index(index_path, index)
            for file in unit.files():
                assert file.content is not None
                _private_write_bytes(root / file.path, file.content)
            os.chmod(root / "views", 0o700)
            target = root / "views" / "2026-05" / "transactions.csv"
            os.chmod(target, 0o644)

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [("managed_metadata_invalid", "views/2026-05/transactions.csv")],
            )
            self.assertEqual(
                [(action.kind, action.path) for action in plan.actions],
                [
                    (
                        RepairActionKind.SET_PRIVATE_MODE,
                        "views/2026-05/transactions.csv",
                    )
                ],
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertTrue(fixed.after.healthy)

    def test_audit_refuses_a_symbolic_link_in_import_records(self) -> None:
        source_id = "src_" + "c" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            outside = Path(temporary) / "outside-record"
            outside.mkdir()
            os.symlink(
                outside,
                root / ".honeymoney" / "import-records" / source_id,
            )

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [
                    (
                        "managed_path_unsafe",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )

    def test_audit_refuses_a_symbolic_link_attempt_file(self) -> None:
        source_id = "src_" + "9" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            attempt = write_attempt(record, _failed_attempt(source_id))
            outside = Path(temporary) / "outside-attempt.json"
            outside.write_text(attempt.read_text(encoding="utf-8"), encoding="utf-8")
            attempt.unlink()
            os.symlink(outside, attempt)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [
                    (
                        "managed_path_unsafe",
                        f".honeymoney/import-records/{source_id}/attempts/00000001.json",
                    )
                ],
            )

    def test_audit_rejects_a_noncanonical_current_snapshot(self) -> None:
        source_id = "src_" + "8" * 64
        record_id = "rec_" + "b" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            snapshot, _ = write_transaction_snapshot(
                record,
                ["source_record_id"],
                [{"source_record_id": record_id}],
            )
            snapshot.write_bytes(snapshot.read_bytes().replace(b"\n", b"\r\n"))
            attempt = _failed_attempt(source_id)
            attempt.update(
                {
                    "outcome": "success",
                    "error_codes": [],
                    "error_count": 0,
                    "transactions_schema_version": 1,
                    "transactions_digest": sha256(
                        snapshot.read_text(encoding="utf-8").encode("utf-8")
                    ).hexdigest(),
                }
            )
            write_attempt(record, attempt)
            write_summary(record, source_id)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [
                    (
                        "import_record_invalid",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )

    def test_audit_reports_a_newer_import_contract_as_an_upgrade_need(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            index_path = root / ".honeymoney" / "workspace-index.json"
            index = load_workspace_index(index_path)
            index["contracts"]["attempt_schema_version"] = 2
            write_workspace_index(index_path, index)

            result = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("newer_honeymoney_required", ".honeymoney/workspace-index.json")],
            )

    def test_fix_removes_only_a_proved_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            _private_write(
                root / ".honeymoney" / "workspace.lock",
                '{"pid":99999999,"schema_version":1}\n',
            )

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)
            fixed = fix_workspace(root)

            self.assertEqual([item.code for item in audit.findings], ["stale_lock"])
            self.assertEqual(
                [action.kind for action in plan.actions],
                [RepairActionKind.REMOVE_STALE_LOCK],
            )
            self.assertFalse((root / ".honeymoney" / "workspace.lock").exists())
            self.assertTrue(fixed.after.healthy)

    def test_attempt_report_must_name_its_own_import_record(self) -> None:
        source_id = "src_" + "d" * 64
        other_source_id = "src_" + "e" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            write_attempt(record, _failed_attempt(other_source_id))

            audit = audit_workspace(root)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [
                    (
                        "attempt_history_invalid",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )

    def test_ready_import_record_must_agree_with_workspace_identity(self) -> None:
        source_id = "src_" + "f" * 64
        record_id = "rec_" + "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            record = initialize_record(
                root / ".honeymoney" / "import-records", source_id
            )
            _, digest = write_transaction_snapshot(
                record,
                ["source_record_id"],
                [{"source_record_id": record_id}],
            )
            attempt = _failed_attempt(source_id)
            attempt.update(
                {
                    "outcome": "success",
                    "error_codes": [],
                    "error_count": 0,
                    "transactions_schema_version": 1,
                    "transactions_digest": digest,
                }
            )
            write_attempt(record, attempt)
            write_summary(record, source_id)

            audit = audit_workspace(root)
            plan = build_repair_plan(root, audit=audit)

            self.assertEqual(
                [(item.code, item.path) for item in audit.findings],
                [
                    (
                        "import_record_invalid",
                        f".honeymoney/import-records/{source_id}",
                    )
                ],
            )
            self.assertTrue(plan.blocked)

    def test_audit_rejects_a_symbolic_link_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            _healthy_workspace(root)
            alias = Path(temporary) / "workspace-link"
            os.symlink(root, alias)

            result = audit_workspace(alias)

            self.assertEqual(
                [(item.code, item.path) for item in result.findings],
                [("managed_path_unsafe", None)],
            )


if __name__ == "__main__":
    unittest.main()
