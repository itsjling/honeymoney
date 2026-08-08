from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.doctor import audit_workspace, fix_workspace
from honeymoney.identity import IdentityError
from honeymoney.workspace_commands import (
    WorkspaceCommandError,
    import_workspace,
    list_imports,
    show_import,
)
from honeymoney.workspace_publication import PublicationError
from honeymoney.workspace_setup import setup_workspace


class WorkspaceAttemptRecoveryTest(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "synthetic.csv"
        source.write_text(
            "Date,Description,Amount,Currency\n"
            "2026-08-08,Synthetic Recovery Item,-12.00,HKD\n",
            encoding="utf-8",
        )
        return source

    def _configure_ambiguous_csv_profiles(self, paths: object) -> None:
        profiles = getattr(paths, "profiles")
        config_path = getattr(paths, "config")
        starter = json.loads(
            (profiles / "starter_csv.json").read_text(encoding="utf-8")
        )
        starter["id"] = "second_csv"
        starter["account_id"] = "second_csv"
        second_profile = profiles / "second_csv.json"
        second_profile.write_text(
            json.dumps(starter, indent=2, sort_keys=True), encoding="utf-8"
        )
        os.chmod(second_profile, 0o600)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["profiles"] = [
            "profiles/starter_csv.json",
            "profiles/second_csv.json",
        ]
        config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _map_starter_csv_profile(self, paths: object, pattern: str = "*.csv") -> None:
        mappings_path = getattr(paths, "profile_mappings")
        mappings_path.write_text(
            json.dumps(
                {
                    "account_bindings": [],
                    "filename_patterns": [
                        {"pattern": pattern, "profile": "starter_csv"}
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_precommit_stop_becomes_failure_and_plain_retry_uses_next_number(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)
            real_replace = os.replace

            def stop_before_index(source_path: object, destination: object) -> None:
                if Path(destination).name == "workspace-index.json":
                    raise OSError("synthetic precommit stop")
                real_replace(source_path, destination)

            with patch(
                "honeymoney.workspace_publication.os.replace", stop_before_index
            ):
                with self.assertRaises(PublicationError):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            self.assertEqual(
                [item.code for item in audit_workspace(root).findings],
                ["publication_recovery_required"],
            )
            fixed = fix_workspace(root)
            self.assertTrue(fixed.after.healthy)
            record = next(paths.import_records.iterdir())
            first = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first["outcome"], "failure")
            self.assertEqual(first["error_codes"], ["interrupted"])
            listed = list_imports(paths.config)
            self.assertFalse(listed.data["import_records"][0]["ready"])  # type: ignore[index]

            retried = import_workspace(
                source,
                config_path=paths.config,
                interactive=False,
            )

            self.assertEqual(retried.data["import_count"], 1)
            second = json.loads(
                (record / "attempts/00000002.json").read_text(encoding="utf-8")
            )
            self.assertEqual(second["outcome"], "success")

    def test_attempt_is_reserved_before_parsing_and_recovered_if_work_stops(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)

            def stop_during_parse(*_args: object, **_kwargs: object) -> object:
                journal = json.loads(paths.journal.read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "reserved")
                self.assertEqual(len(journal["attempts"]), 1)
                self.assertTrue(
                    journal["attempts"][0]["target"].endswith("/00000001.json")
                )
                pending = json.loads(journal["attempts"][0]["interrupted_document"])
                self.assertTrue(pending["source_revision"].startswith("rev_"))
                self.assertRegex(pending["parser_contract"], r"^ext_[0-9a-f]{64}$")
                self.assertNotEqual(pending["parser_contract"], "ext_" + "0" * 64)
                self.assertEqual(
                    stat.S_IMODE(paths.journal.stat().st_mode),
                    0o600,
                )
                raise KeyboardInterrupt

            with patch(
                "honeymoney.workspace_commands._parse_source",
                side_effect=stop_during_parse,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            fixed = fix_workspace(root)

            self.assertTrue(fixed.after.healthy)
            record = next(paths.import_records.iterdir())
            attempt = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertEqual(attempt["error_codes"], ["interrupted"])
            self.assertTrue(attempt["source_revision"].startswith("rev_"))
            self.assertRegex(attempt["parser_contract"], r"^ext_[0-9a-f]{64}$")
            self.assertNotEqual(attempt["parser_contract"], "ext_" + "0" * 64)

    def test_postcommit_stop_becomes_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)
            real_replace = os.replace

            def stop_after_index(source_path: object, destination: object) -> None:
                real_replace(source_path, destination)
                if Path(destination).name == "workspace-index.json":
                    raise OSError("synthetic postcommit stop")

            with patch("honeymoney.workspace_publication.os.replace", stop_after_index):
                with self.assertRaises(PublicationError):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            fixed = fix_workspace(root)

            self.assertTrue(fixed.after.healthy)
            record = next(paths.import_records.iterdir())
            report = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["outcome"], "success")
            self.assertTrue(
                list_imports(paths.config).data["import_records"][0]["ready"]
            )  # type: ignore[index]

    def test_parse_failure_writes_nothing_before_its_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._map_starter_csv_profile(paths)
            source = root / "invalid.csv"
            source.write_bytes(b"\xff")

            with patch(
                "honeymoney.workspace_publication._write_journal",
                side_effect=OSError("synthetic journal stop"),
            ) as write_journal:
                with self.assertRaises(PublicationError):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            write_journal.assert_called_once()
            self.assertEqual(list(paths.import_records.iterdir()), [])

    def test_unknown_undecodable_source_is_rejected_before_attempt_reservation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = root / "invalid.csv"
            source.write_bytes(b"\xff")

            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(source, config_path=paths.config, interactive=False)

            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            self.assertFalse(paths.journal.exists())
            self.assertEqual(list(paths.import_records.iterdir()), [])

    def test_fixed_parse_failure_keeps_workspace_index_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._map_starter_csv_profile(paths)
            source = root / "invalid.csv"
            source.write_bytes(b"\xff")
            before_index = paths.workspace_index.read_bytes()

            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(source, config_path=paths.config, interactive=False)

            self.assertEqual(raised.exception.code, "import_failed")
            self.assertEqual(paths.workspace_index.read_bytes(), before_index)
            record = next(paths.import_records.iterdir())
            summary = json.loads((record / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["ready"])
            attempt = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertTrue(audit_workspace(root).healthy)

    def test_post_reservation_identity_failure_is_finalized_without_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)
            before_index = paths.workspace_index.read_bytes()

            with patch(
                "honeymoney.workspace_commands.resolve_batch",
                side_effect=IdentityError("identity_hash_conflict"),
            ):
                with self.assertRaises(WorkspaceCommandError) as raised:
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            self.assertEqual(raised.exception.code, "identity_hash_conflict")
            self.assertFalse(paths.journal.exists())
            self.assertEqual(paths.workspace_index.read_bytes(), before_index)
            self.assertTrue(audit_workspace(root).healthy)
            record = next(paths.import_records.iterdir())
            summary = json.loads((record / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["ready"])
            self.assertIsNone(summary["current_attempt_number"])
            attempt = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertEqual(attempt["error_codes"], ["identity_hash_conflict"])

    def test_post_reservation_derivation_failure_keeps_prior_success_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)
            import_workspace(source, config_path=paths.config, interactive=False)
            record = next(paths.import_records.iterdir())
            before_index = paths.workspace_index.read_bytes()
            before_snapshot = (record / "transactions.csv").read_bytes()
            before_summary = (record / "summary.json").read_bytes()
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-09,Synthetic Replacement Item,-13.00,HKD\n",
                encoding="utf-8",
            )

            with patch(
                "honeymoney.workspace_commands.derive_workspace_rows",
                side_effect=WorkspaceCommandError("workspace_input_invalid"),
            ):
                with self.assertRaises(WorkspaceCommandError) as raised:
                    import_workspace(
                        source,
                        config_path=paths.config,
                        action="replace",
                        interactive=False,
                    )

            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            self.assertFalse(paths.journal.exists())
            self.assertEqual(paths.workspace_index.read_bytes(), before_index)
            self.assertEqual(
                (record / "transactions.csv").read_bytes(), before_snapshot
            )
            self.assertEqual((record / "summary.json").read_bytes(), before_summary)
            self.assertTrue(audit_workspace(root).healthy)
            attempt = json.loads(
                (record / "attempts/00000002.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertEqual(attempt["error_codes"], ["workspace_input_invalid"])

    def test_post_reservation_ready_row_failure_is_finalized_without_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)
            import_workspace(source, config_path=paths.config, interactive=False)
            record = next(paths.import_records.iterdir())
            before_snapshot = (record / "transactions.csv").read_bytes()
            before_summary = (record / "summary.json").read_bytes()
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-09,Synthetic Replacement Item,-13.00,HKD\n",
                encoding="utf-8",
            )

            with patch(
                "honeymoney.workspace_commands._load_ready_source_rows",
                side_effect=WorkspaceCommandError("durable_state_conflict"),
            ):
                with self.assertRaises(WorkspaceCommandError) as raised:
                    import_workspace(
                        source,
                        config_path=paths.config,
                        action="replace",
                        interactive=False,
                    )

            self.assertEqual(raised.exception.code, "durable_state_conflict")
            self.assertFalse(paths.journal.exists())
            self.assertEqual(
                (record / "transactions.csv").read_bytes(), before_snapshot
            )
            self.assertEqual((record / "summary.json").read_bytes(), before_summary)
            self.assertTrue(audit_workspace(root).healthy)
            attempt = json.loads(
                (record / "attempts/00000002.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertEqual(attempt["error_codes"], ["durable_state_conflict"])

    def test_stopped_parse_failure_is_finalized_by_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._map_starter_csv_profile(paths)
            source = root / "invalid.csv"
            source.write_bytes(b"\xff")

            with patch(
                "honeymoney.workspace_publication._finalize_attempts",
                side_effect=OSError("synthetic attempt stop"),
            ):
                with self.assertRaises(PublicationError):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            self.assertTrue(paths.journal.is_file())
            fixed = fix_workspace(root)

            self.assertTrue(fixed.after.healthy)
            record = next(paths.import_records.iterdir())
            attempt = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertNotIn("interrupted", attempt["error_codes"])
            self.assertTrue(attempt["source_revision"].startswith("rev_"))
            self.assertRegex(attempt["parser_contract"], r"^ext_[0-9a-f]{64}$")
            self.assertFalse(
                list_imports(paths.config).data["import_records"][0]["ready"]
            )  # type: ignore[index]

    def test_failed_only_summary_is_a_recoverable_publication_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._map_starter_csv_profile(paths)
            source = root / "invalid.csv"
            source.write_bytes(b"\xff")

            with patch(
                "honeymoney.workspace_publication._retain_entry",
                side_effect=OSError("synthetic staging stop"),
            ):
                with self.assertRaises(PublicationError):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            journal = json.loads(paths.journal.read_text(encoding="utf-8"))
            targets = [entry["target"] for entry in journal["entries"]]
            self.assertEqual(journal["commit_policy"], "fixed")
            self.assertEqual(
                [entry["entry_kind"] for entry in journal["entries"]],
                ["directory", "directory", "file"],
            )
            self.assertTrue(any(target.endswith("/summary.json") for target in targets))
            self.assertTrue(any(target.endswith("/attempts") for target in targets))
            self.assertNotIn(".honeymoney/workspace-index.json", targets)

            fixed = fix_workspace(root)

            self.assertTrue(fixed.after.healthy)
            record = next(paths.import_records.iterdir())
            self.assertTrue((record / "summary.json").is_file())
            attempt = json.loads(
                (record / "attempts/00000001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(attempt["outcome"], "failure")
            self.assertNotIn("interrupted", attempt["error_codes"])

    def test_mixed_batch_commits_success_and_fixed_failure_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._map_starter_csv_profile(paths)
            sources = root / "sources"
            sources.mkdir()
            (sources / "invalid.csv").write_bytes(b"\xff")
            self._source(sources).rename(sources / "valid.csv")

            result = import_workspace(
                sources,
                config_path=paths.config,
                interactive=False,
            )

            self.assertEqual(result.data["import_count"], 1)
            records = list_imports(paths.config).data["import_records"]
            self.assertEqual(len(records), 2)  # type: ignore[arg-type]
            self.assertEqual(
                sorted(item["ready"] for item in records),  # type: ignore[union-attr]
                [False, True],
            )
            self.assertTrue(audit_workspace(root).healthy)

    def test_mixed_batch_keeps_reservation_order_when_valid_source_sorts_first(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._map_starter_csv_profile(paths)
            sources = root / "sources"
            sources.mkdir()
            self._source(sources).rename(sources / "a-valid.csv")
            (sources / "z-invalid.csv").write_bytes(b"\xff")

            result = import_workspace(
                sources,
                config_path=paths.config,
                interactive=False,
            )

            self.assertEqual(result.data["import_count"], 1)
            records = list_imports(paths.config).data["import_records"]
            self.assertEqual(len(records), 2)  # type: ignore[arg-type]
            self.assertEqual(
                sorted(item["ready"] for item in records),  # type: ignore[union-attr]
                [False, True],
            )
            self.assertTrue(audit_workspace(root).healthy)

    def test_rename_replace_reuses_the_existing_source_before_reservation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = self._source(root)
            import_workspace(source, config_path=paths.config, interactive=False)
            original = list_imports(paths.config).data["import_records"]
            original_source_id = original[0]["source_id"]  # type: ignore[index]
            renamed = root / "renamed.csv"
            source.rename(renamed)

            result = import_workspace(
                renamed,
                config_path=paths.config,
                action="replace",
                interactive=False,
            )

            self.assertEqual(result.data["import_count"], 1)
            records = list_imports(paths.config).data["import_records"]
            self.assertEqual(len(records), 1)  # type: ignore[arg-type]
            self.assertEqual(records[0]["source_id"], original_source_id)  # type: ignore[index]
            attempts = sorted(
                (paths.import_records / str(original_source_id) / "attempts").glob(
                    "*.json"
                )
            )
            self.assertEqual(
                [item.name for item in attempts], ["00000001.json", "00000002.json"]
            )

    def test_unmatched_replace_rejects_before_reserving_an_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            import_workspace(
                self._source(root), config_path=paths.config, interactive=False
            )
            replacement = root / "replacement.csv"
            replacement.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Replacement Item,-13.00,HKD\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                WorkspaceCommandError, "identity_source_target_not_found"
            ):
                import_workspace(
                    replacement,
                    config_path=paths.config,
                    action="replace",
                    interactive=False,
                )

            self.assertFalse(paths.journal.exists())
            records = list_imports(paths.config).data["import_records"]
            self.assertEqual(len(records), 1)  # type: ignore[arg-type]

    def test_interactive_import_does_not_write_profile_mappings_before_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._configure_ambiguous_csv_profiles(paths)
            source = self._source(root)
            original_mappings = paths.profile_mappings.read_bytes()

            with (
                patch("builtins.input", side_effect=["1", "yes"]),
                patch(
                    "honeymoney.workspace_commands.derive_workspace_rows",
                    side_effect=KeyboardInterrupt,
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=True,
                    )

            self.assertEqual(paths.profile_mappings.read_bytes(), original_mappings)
            self.assertTrue(paths.journal.exists())

    def test_interactive_import_never_writes_profile_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._configure_ambiguous_csv_profiles(paths)
            source = self._source(root)
            original_mappings = paths.profile_mappings.read_bytes()

            with patch("builtins.input", side_effect=["1", "yes"]):
                result = import_workspace(
                    source,
                    config_path=paths.config,
                    interactive=True,
                )

            self.assertEqual(result.data["import_count"], 1)
            self.assertEqual(paths.profile_mappings.read_bytes(), original_mappings)
            self.assertTrue(audit_workspace(root).healthy)

    def test_ambiguous_profile_rejects_before_reserving_an_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            self._configure_ambiguous_csv_profiles(paths)

            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    self._source(root),
                    config_path=paths.config,
                    interactive=False,
                )

            self.assertEqual(raised.exception.code, "workspace_input_invalid")
            self.assertFalse(paths.journal.exists())
            self.assertEqual(list(paths.import_records.iterdir()), [])

    def test_import_history_uses_a_safe_source_pseudonym(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            source = root / "private-statement\ncontrol.csv"
            source.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Label Item,-12.00,HKD\n",
                encoding="utf-8",
            )

            with patch(
                "honeymoney.workspace_commands._parse_source",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    import_workspace(
                        source,
                        config_path=paths.config,
                        interactive=False,
                    )

            journal = paths.journal.read_text(encoding="utf-8")
            self.assertNotIn("private-statement", journal)
            self.assertNotIn("\\ncontrol", journal)
            fixed = fix_workspace(root)
            self.assertTrue(fixed.after.healthy)
            source_id = list_imports(paths.config).data["import_records"][0][
                "source_id"
            ]  # type: ignore[index]
            report = show_import(str(source_id), paths.config)
            rendered = json.dumps(report.data, sort_keys=True)
            self.assertNotIn("private-statement", rendered)
            self.assertNotIn("\\ncontrol", rendered)
            self.assertEqual(
                report.data["source_label"],
                f"CSV source {str(source_id)[4:16]}",
            )

    def test_fresh_import_with_safe_label_passes_doctor_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)

            import_workspace(
                self._source(root), config_path=paths.config, interactive=False
            )

            self.assertTrue(audit_workspace(root).healthy)


if __name__ == "__main__":
    unittest.main()
