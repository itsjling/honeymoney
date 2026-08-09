from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from honeymoney import cli
from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.doctor import fix_workspace
from honeymoney.periods import resolve_period_selection
from honeymoney.workspace_commands import (
    WorkspaceCommandError,
    import_workspace,
    load_workspace,
    workspace_report,
    workspace_status,
)
from honeymoney.workspace_paths import WorkspacePathError
from honeymoney.workspace_setup import setup_workspace


class WorkspacePathSafetyTest(unittest.TestCase):
    def _run_cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["honeymoney", *arguments]),
            redirect_stdout(output),
        ):
            return cli.run(), output.getvalue()

    def test_setup_rejects_a_symbolic_link_component_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            linked_parent = base / "linked-parent"
            os.symlink(outside, linked_parent)
            root = linked_parent / "workspace"

            with self.assertRaises(WorkspacePathError) as raised:
                setup_workspace(root)

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(list(outside.iterdir()), [])

    def test_setup_rejects_a_symbolic_link_before_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real = base / "real"
            real.mkdir()
            outside = base / "outside"
            outside.mkdir()
            linked_parent = real / "linked-parent"
            os.symlink(outside, linked_parent)
            root = linked_parent / ".." / "workspace"

            with self.assertRaises(WorkspacePathError) as raised:
                setup_workspace(root)

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertFalse((base / "workspace").exists())

    def test_setup_rejects_a_user_link_directly_below_tmp_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            linked_root = Path("/tmp") / f"honeymoney-path-safety-{uuid4().hex}"
            os.symlink(outside, linked_root)
            try:
                with self.assertRaises(WorkspacePathError) as raised:
                    setup_workspace(linked_root / "workspace")
            finally:
                linked_root.unlink(missing_ok=True)

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(list(outside.iterdir()), [])

    def test_setup_rejects_a_symbolic_link_root_without_writing_through_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            root = base / "workspace"
            os.symlink(outside, root)

            with self.assertRaises(WorkspacePathError) as raised:
                setup_workspace(root)

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(list(outside.iterdir()), [])
            fixed = fix_workspace(root)
            self.assertTrue(fixed.plan.blocked)
            self.assertEqual(fixed.plan.blocker_codes, ("managed_path_unsafe",))
            self.assertEqual(list(outside.iterdir()), [])

    def test_setup_rejects_existing_managed_child_links_without_touching_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for relative in (
                "profiles",
                "profiles/starter_csv.json",
                ".honeymoney",
                ".honeymoney/import-records",
                "rules.json",
            ):
                with self.subTest(path=relative):
                    root = base / relative.replace("/", "-")
                    root.mkdir()
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    outside = base / f"outside-{target.name}"
                    if target.suffix:
                        outside.write_text("outside bytes\n", encoding="utf-8")
                        before = outside.read_bytes()
                    else:
                        outside.mkdir()
                        sentinel = outside / "sentinel"
                        sentinel.write_text("outside bytes\n", encoding="utf-8")
                        before = {
                            path.name: path.read_bytes() for path in outside.iterdir()
                        }
                    os.symlink(outside, target)

                    with self.assertRaises(WorkspacePathError) as raised:
                        setup_workspace(root)

                    self.assertEqual(raised.exception.code, "managed_path_unsafe")
                    if target.suffix:
                        self.assertEqual(outside.read_bytes(), before)
                    else:
                        self.assertEqual(
                            {
                                path.name: path.read_bytes()
                                for path in outside.iterdir()
                            },
                            before,
                        )

    def test_load_rejects_symbolic_link_config_and_managed_view_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_root = base / "config-link"
            paths = setup_workspace(config_root)
            real_config = config_root / "config.real.json"
            paths.config.rename(real_config)
            os.symlink(real_config, paths.config)

            with self.assertRaises(WorkspacePathError) as raised:
                load_workspace(paths.config)
            self.assertEqual(raised.exception.code, "managed_path_unsafe")

            view_root = base / "view-link"
            view_paths = setup_workspace(view_root)
            outside = base / "outside-views"
            outside.mkdir()
            os.symlink(outside, view_paths.views)

            with self.assertRaises(WorkspaceCommandError) as raised_command:
                load_workspace(view_paths.config)
            self.assertEqual(raised_command.exception.code, "managed_path_unsafe")

    def test_load_rejects_a_config_below_a_symbolic_link_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            linked_parent = base / "linked-parent"
            os.symlink(paths.root, linked_parent)

            with self.assertRaises(WorkspacePathError) as raised:
                load_workspace(linked_parent / "config.json")

            self.assertEqual(raised.exception.code, "managed_path_unsafe")

    def test_load_rejects_a_configured_input_below_a_symbolic_link_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            outside = base / "outside"
            outside.mkdir()
            rules = outside / "rules.json"
            rules.write_text('{"rules": [], "version": 1}\n', encoding="utf-8")
            os.symlink(outside, paths.root / "linked-input")
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            config["rules"] = "linked-input/rules.json"
            paths.config.write_text(
                json.dumps(config, indent=2, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaises(WorkspaceCommandError) as raised:
                load_workspace(paths.config)

            self.assertEqual(raised.exception.code, "managed_path_unsafe")

    def test_setup_hardens_existing_managed_paths_in_a_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            paths = setup_workspace(root)
            directories = (
                paths.root,
                paths.profiles,
                paths.internal,
                paths.import_records,
            )
            for directory in directories:
                os.chmod(directory, 0o755)
            files = [path for path in root.rglob("*") if path.is_file()]
            for path in files:
                os.chmod(path, 0o644)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes() for path in files
            }
            index_relative = ".honeymoney/workspace-index.json"
            before_generation = json.loads(before[index_relative])["generation_id"]

            self.assertEqual(setup_workspace(root), paths)

            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(
                {
                    path: content
                    for path, content in after.items()
                    if path != index_relative
                },
                {
                    path: content
                    for path, content in before.items()
                    if path != index_relative
                },
            )
            self.assertNotEqual(
                json.loads(after[index_relative])["generation_id"], before_generation
            )
            for directory in directories:
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_setup_rejects_a_legacy_workspace_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            os.chmod(root, 0o755)
            legacy = root / "categorized.csv"
            legacy.write_text("synthetic legacy bytes\n", encoding="utf-8")
            os.chmod(legacy, 0o644)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            with self.assertRaises(WorkspacePathError) as raised:
                setup_workspace(root)

            self.assertEqual(raised.exception.code, "legacy_workspace_reset_required")
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o644)
            self.assertFalse((root / ".honeymoney").exists())

    def test_setup_rejects_output_side_legacy_markers_without_changing_bytes(
        self,
    ) -> None:
        markers = (
            "categorized.csv",
            "review_needed.csv",
            "import_report.json",
            ".honeymoney-identity-manifest.json",
            ".honeymoney-source-occurrences.csv",
            ".honeymoney-overlap-manifest.json",
            ".categorized.csv.honeymoney-state.json",
            ".categorized.csv.honeymoney-lock",
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for marker in markers:
                with self.subTest(marker=marker):
                    root = base / marker.replace(".", "_")
                    output = root / "output"
                    output.mkdir(parents=True)
                    legacy = output / marker
                    legacy.write_text("synthetic legacy bytes\n", encoding="utf-8")
                    before = {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    }

                    with self.assertRaises(WorkspacePathError) as raised:
                        setup_workspace(root)

                    self.assertEqual(
                        raised.exception.code, "legacy_workspace_reset_required"
                    )
                    after = {
                        path.relative_to(root).as_posix(): path.read_bytes()
                        for path in root.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(after, before)
                    self.assertFalse((root / ".honeymoney").exists())

    def test_json_hides_configured_input_paths_outside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            paths = setup_workspace(root)
            missing = Path(temporary) / "outside" / "rules.json"
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            config["rules"] = str(missing)
            paths.config.write_text(
                json.dumps(config, indent=2, sort_keys=True), encoding="utf-8"
            )

            code, output = self._run_cli(
                "status", "--all", "--config", str(paths.config), "--json"
            )

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "managed_path_unsafe")
            self.assertNotIn(str(root), output)
            self.assertNotIn(str(missing), output)

    def test_json_hides_unreadable_configured_input_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "workspace")
            os.chmod(paths.rules, 0o000)
            try:
                code, output = self._run_cli(
                    "status", "--all", "--config", str(paths.config), "--json"
                )
            finally:
                os.chmod(paths.rules, 0o600)

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "workspace_input_invalid")
            self.assertNotIn(str(paths.root), output)
            self.assertNotIn(str(paths.rules), output)

    def test_json_hides_invalid_rule_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "workspace")
            private_value = "private-rule-value-["
            paths.rules.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rules": [
                            {
                                "id": "invalid-rule",
                                "conditions": [
                                    {
                                        "field": "description",
                                        "match_type": "regex",
                                        "patterns": [private_value],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, output = self._run_cli(
                "status", "--all", "--config", str(paths.config), "--json"
            )

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "workspace_input_invalid")
            self.assertNotIn(private_value, output)
            self.assertNotIn(str(paths.root), output)

    def test_import_json_hides_invalid_rule_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            private_value = "private-import-rule-value-["
            paths.rules.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "rules": [
                            {
                                "id": "invalid-rule",
                                "conditions": [
                                    {
                                        "field": "description",
                                        "match_type": "regex",
                                        "patterns": [private_value],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            statement = base / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-08-09,Synthetic,-1.00,HKD\n",
                encoding="utf-8",
            )

            code, output = self._run_cli(
                "import",
                str(statement),
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "workspace_input_invalid")
            self.assertNotIn(private_value, output)
            self.assertNotIn(str(paths.root), output)

    def test_json_hides_invalid_correction_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "workspace")
            private_value = "private-correction-value"
            correction = {column: "" for column in CORRECTION_COLUMNS}
            correction["transaction_id"] = "transaction-id"
            correction["category"] = private_value
            paths.corrections.write_text(
                ",".join(CORRECTION_COLUMNS)
                + "\n"
                + ",".join(correction[column] for column in CORRECTION_COLUMNS)
                + "\n",
                encoding="utf-8",
            )

            code, output = self._run_cli(
                "status", "--all", "--config", str(paths.config), "--json"
            )

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "workspace_input_invalid")
            self.assertNotIn(private_value, output)
            self.assertNotIn(str(paths.root), output)

    def test_import_rejects_a_symbolic_link_statement_without_an_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            paths = setup_workspace(root)
            statement = base / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-08-09,Synthetic,-1.00,HKD\n",
                encoding="utf-8",
            )
            linked_statement = root / "statement.csv"
            os.symlink(statement, linked_statement)

            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    linked_statement,
                    config_path=paths.config,
                    interactive=False,
                )

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(list(paths.import_records.iterdir()), [])

    def test_import_rejects_a_statement_below_a_symbolic_link_parent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            outside = base / "outside"
            outside.mkdir()
            statement = outside / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-08-09,Synthetic,-1.00,HKD\n",
                encoding="utf-8",
            )
            linked_parent = base / "linked-parent"
            os.symlink(outside, linked_parent)

            with self.assertRaises(WorkspaceCommandError) as raised:
                import_workspace(
                    linked_parent / "statement.csv",
                    config_path=paths.config,
                    interactive=False,
                )

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(list(paths.import_records.iterdir()), [])

    def test_workspace_rejects_a_symbolic_linked_import_record_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            statement = base / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-08-09,Synthetic,-1.00,HKD\n",
                encoding="utf-8",
            )
            import_workspace(statement, config_path=paths.config, interactive=False)
            record = next(paths.import_records.iterdir())
            snapshot = record / "transactions.csv"
            outside = base / "outside-transactions.csv"
            snapshot.replace(outside)
            before = outside.read_bytes()
            os.symlink(outside, snapshot)

            with self.assertRaises(WorkspaceCommandError) as raised:
                workspace_status(
                    resolve_period_selection(all_periods=True), config_path=paths.config
                )

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(outside.read_bytes(), before)

    def test_workspace_rejects_a_symbolic_linked_import_record_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            statement = base / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-08-09,Synthetic,-1.00,HKD\n",
                encoding="utf-8",
            )
            import_workspace(statement, config_path=paths.config, interactive=False)
            record = next(paths.import_records.iterdir())
            outside = base / "outside-record"
            record.replace(outside)
            os.symlink(outside, record)

            with self.assertRaises(WorkspaceCommandError) as raised:
                workspace_status(
                    resolve_period_selection(all_periods=True), config_path=paths.config
                )

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertTrue((outside / "transactions.csv").is_file())

    def test_json_hides_unreadable_import_record_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = setup_workspace(base / "workspace")
            statement = base / "statement.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n2026-08-09,Synthetic,-1.00,HKD\n",
                encoding="utf-8",
            )
            import_workspace(statement, config_path=paths.config, interactive=False)
            attempts = next(paths.import_records.iterdir()) / "attempts"
            os.chmod(attempts, 0o000)
            try:
                code, output = self._run_cli(
                    "status", "--all", "--config", str(paths.config), "--json"
                )
            finally:
                os.chmod(attempts, 0o700)

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "durable_state_conflict")
            self.assertNotIn(str(paths.root), output)
            self.assertNotIn(str(attempts), output)

    def test_imports_list_json_hides_unreadable_import_records_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "workspace")
            os.chmod(paths.import_records, 0o000)
            try:
                code, output = self._run_cli(
                    "imports", "list", "--config", str(paths.config), "--json"
                )
            finally:
                os.chmod(paths.import_records, 0o700)

            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertEqual(payload["errors"][0]["code"], "import_record_invalid")
            self.assertNotIn(str(paths.root), output)
            self.assertNotIn(str(paths.import_records), output)

    def test_report_export_cannot_replace_a_workspace_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "workspace")
            before = paths.config.read_bytes()

            with self.assertRaises(WorkspaceCommandError) as raised:
                workspace_report(
                    resolve_period_selection(all_periods=True),
                    config_path=paths.config,
                    export_path=paths.config,
                )

            self.assertEqual(raised.exception.code, "managed_path_unsafe")
            self.assertEqual(paths.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
