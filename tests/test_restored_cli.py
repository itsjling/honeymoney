from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.csv_artifacts import csv_document
from honeymoney.workspace_paths import WorkspacePaths
from honeymoney.workspace_setup import setup_workspace


class RestoredCliTest(unittest.TestCase):
    def _run(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *arguments],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def _import_rows(
        self, paths: WorkspacePaths, document: str
    ) -> list[dict[str, str]]:
        source = paths.root / "synthetic.csv"
        source.write_text(document, encoding="utf-8")
        imported = self._run(
            "import",
            str(source),
            "--config",
            str(paths.config),
            "--no-interactive",
            "--json",
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        with (paths.root / "views/2026-08/transactions.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            return list(csv.DictReader(handle))

    def test_json_mutation_error_does_not_echo_correction_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            private_value = "private-category-value"
            correction = paths.root / "bad-correction.csv"
            correction.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            **{column: "" for column in CORRECTION_COLUMNS},
                            "transaction_id": "txn_" + "a" * 32,
                            "category": private_value,
                        }
                    ],
                ),
                encoding="utf-8",
            )

            result = self._run(
                "correct",
                "--file",
                str(correction),
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(result.returncode, 2)
            self.assertNotIn(private_value, result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["errors"][0]["message"], "Correction file is invalid"
            )

    def test_config_hides_runtime_fields_and_publishes_ollama_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            raw = json.loads(paths.config.read_text(encoding="utf-8"))
            raw["_private_runtime_value"] = "never-print"
            paths.config.write_text(json.dumps(raw), encoding="utf-8")

            shown = self._run("config", "--config", str(paths.config), "--json")

            self.assertEqual(shown.returncode, 0, shown.stderr)
            displayed = json.loads(shown.stdout)["data"]["config"]
            self.assertNotIn("_private_runtime_value", displayed)

        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            before_index = paths.workspace_index.read_bytes()

            changed = self._run(
                "config",
                "edit",
                "ollama",
                "--model",
                "local-test",
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(changed.returncode, 0, changed.stderr)
            payload = json.loads(changed.stdout)
            self.assertEqual(payload["command"], "config")
            self.assertTrue(payload["data"]["changed"])
            self.assertTrue(payload["data"]["ollama"]["enabled"])
            self.assertEqual(payload["data"]["ollama"]["model"], "local-test")
            self.assertNotEqual(paths.workspace_index.read_bytes(), before_index)

    def test_reconcile_is_a_read_only_current_derivation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            before = {
                path.relative_to(paths.root).as_posix(): path.read_bytes()
                for path in paths.root.rglob("*")
                if path.is_file()
            }

            result = self._run("reconcile", "--config", str(paths.config), "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["command"], "reconcile")
            self.assertTrue(payload["data"]["read_only"])
            self.assertEqual(payload["data"]["view_transaction_count"], 0)
            self.assertNotIn("transaction_count", payload["data"])
            self.assertEqual(
                {
                    path.relative_to(paths.root).as_posix(): path.read_bytes()
                    for path in paths.root.rglob("*")
                    if path.is_file()
                },
                before,
            )

    def test_config_editor_stages_outside_the_workspace_then_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "money"
            paths = setup_workspace(root)
            recorded_path = Path(temporary) / "editor-path.txt"
            editor = Path(temporary) / "editor.py"
            editor.write_text(
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "target = Path(sys.argv[1])\n"
                "value = json.loads(target.read_text())\n"
                "value['review_confidence_threshold'] = 0.9\n"
                "target.write_text(json.dumps(value))\n"
                f"Path({str(recorded_path)!r}).write_text(str(target))\n",
                encoding="utf-8",
            )
            environment = {**os.environ, "EDITOR": f"{sys.executable} {editor}"}

            edited = self._run(
                "config",
                "edit",
                "--config",
                str(paths.config),
                environment=environment,
            )

            self.assertEqual(edited.returncode, 0, edited.stderr)
            staged = Path(recorded_path.read_text(encoding="utf-8"))
            self.assertFalse(staged.is_relative_to(root))
            self.assertFalse(staged.exists())
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            self.assertEqual(config["review_confidence_threshold"], 0.9)
            self.assertFalse(any(root.glob(".config.*.json")))

    def test_review_pair_saves_manual_pair_corrections_and_requires_confirmation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            rows = self._import_rows(
                paths,
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Cash movement,-25.00,HKD\n"
                "2026-08-09,Cash movement,25.00,HKD\n",
            )
            identifiers = [row["transaction_id"] for row in rows]

            unconfirmed = self._run(
                "review",
                "pair",
                *identifiers,
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(unconfirmed.returncode, 2)
            self.assertEqual(
                json.loads(unconfirmed.stdout)["errors"][0]["code"], "usage_error"
            )

            paired = self._run(
                "review",
                "pair",
                *identifiers,
                "--yes",
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(paired.returncode, 0, paired.stderr)
            payload = json.loads(paired.stdout)
            self.assertEqual(payload["command"], "review.pair")
            self.assertTrue(payload["data"]["changed"])
            pair_id = payload["data"]["pair_id"]
            self.assertIn(pair_id, paths.corrections.read_text(encoding="utf-8"))

            repeated = self._run(
                "review",
                "pair",
                *identifiers,
                "--yes",
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertTrue(json.loads(repeated.stdout)["data"]["unchanged"])

    def test_learn_dry_run_is_read_only_and_yes_publishes_rules_and_views(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            inputs = paths.root / "inputs"
            inputs.mkdir(mode=0o700)
            rules_path = inputs / "rules.json"
            rules_path.write_bytes(paths.rules.read_bytes())
            os.chmod(rules_path, 0o600)
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            config["rules"] = "inputs/rules.json"
            paths.config.write_text(json.dumps(config), encoding="utf-8")
            rows = self._import_rows(
                paths,
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic Coffee,-12.00,HKD\n"
                "2026-08-09,Synthetic Coffee,-12.00,HKD\n",
            )
            corrections = Path(temporary) / "choices.csv"
            corrections.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "category": "Dining",
                            "flow_type": "expense",
                            "confidence": "1.00",
                            "reason": "Reviewed locally",
                            "needs_review": "false",
                            "review_reasons": "",
                        }
                        for row in rows
                    ],
                ),
                encoding="utf-8",
            )
            corrected = self._run(
                "correct",
                "--file",
                str(corrections),
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            before = {
                path.relative_to(paths.root).as_posix(): path.read_bytes()
                for path in paths.root.rglob("*")
                if path.is_file()
            }

            dry_run = self._run("learn", "--config", str(paths.config), "--json")

            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            dry_payload = json.loads(dry_run.stdout)
            self.assertEqual(dry_payload["status"], "dry_run")
            self.assertEqual(dry_payload["data"]["written_count"], 0)
            self.assertEqual(
                {
                    path.relative_to(paths.root).as_posix(): path.read_bytes()
                    for path in paths.root.rglob("*")
                    if path.is_file()
                },
                before,
            )

            learned = self._run(
                "learn", "--yes", "--config", str(paths.config), "--json"
            )

            self.assertEqual(learned.returncode, 0, learned.stderr)
            payload = json.loads(learned.stdout)
            self.assertEqual(payload["command"], "learn")
            self.assertTrue(payload["data"]["changed"])
            document = json.loads(rules_path.read_text(encoding="utf-8"))
            self.assertTrue(
                any(
                    rule.get("managed_by") == "honeymoney.learn.v1"
                    for rule in document["rules"]
                )
            )
            self.assertFalse(
                any(
                    rule.get("managed_by") == "honeymoney.learn.v1"
                    for rule in json.loads(paths.rules.read_text(encoding="utf-8"))[
                        "rules"
                    ]
                )
            )

    def test_rate_import_writes_the_configured_rate_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            inputs = paths.root / "inputs"
            inputs.mkdir(mode=0o700)
            cache_path = inputs / "rates.json"
            cache_path.write_bytes(paths.rates.read_bytes())
            os.chmod(cache_path, 0o600)
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            config["rate_cache"] = "inputs/rates.json"
            paths.config.write_text(json.dumps(config), encoding="utf-8")
            rate_document = Path(temporary) / "rates-download.json"
            rate_document.write_text(
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

            imported = self._run(
                "rates",
                "import",
                str(rate_document),
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(len(cache["observations"]), 1)
            self.assertEqual(
                json.loads(paths.rates.read_text(encoding="utf-8"))["observations"], []
            )

    def test_import_reset_updates_the_configured_corrections_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            inputs = paths.root / "inputs"
            inputs.mkdir(mode=0o700)
            corrections_path = inputs / "corrections.csv"
            corrections_path.write_bytes(paths.corrections.read_bytes())
            os.chmod(corrections_path, 0o600)
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            config["corrections"] = "inputs/corrections.csv"
            paths.config.write_text(json.dumps(config), encoding="utf-8")
            [row] = self._import_rows(
                paths,
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic reset,-12.00,HKD\n",
            )
            correction_file = Path(temporary) / "choice.csv"
            correction_file.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [{"transaction_id": row["transaction_id"], "notes": "Synthetic"}],
                ),
                encoding="utf-8",
            )
            corrected = self._run(
                "correct",
                "--file",
                str(correction_file),
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            self.assertIn(
                row["transaction_id"], corrections_path.read_text(encoding="utf-8")
            )

            reset = self._run(
                "import",
                str(paths.root / "synthetic.csv"),
                "--reset",
                "--no-interactive",
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertNotIn(
                row["transaction_id"], corrections_path.read_text(encoding="utf-8")
            )
            self.assertNotIn(
                row["transaction_id"], paths.corrections.read_text(encoding="utf-8")
            )

    def test_changed_configured_input_requires_a_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            inputs = paths.root / "inputs"
            inputs.mkdir(mode=0o700)
            configured_inputs = {
                "profile_mappings": (paths.profile_mappings, inputs / "mappings.json"),
                "rules": (paths.rules, inputs / "rules.json"),
                "corrections": (paths.corrections, inputs / "corrections.csv"),
                "rate_cache": (paths.rates, inputs / "rates.json"),
            }
            config = json.loads(paths.config.read_text(encoding="utf-8"))
            for field, (default_path, configured_path) in configured_inputs.items():
                configured_path.write_bytes(default_path.read_bytes())
                os.chmod(configured_path, 0o600)
                config[field] = configured_path.relative_to(paths.root).as_posix()
            paths.config.write_text(json.dumps(config), encoding="utf-8")
            rebuilt = self._run(
                "views",
                "rebuild",
                "--all",
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
            healthy_doctor = self._run(
                "doctor", "--config", str(paths.config), "--json"
            )
            self.assertEqual(healthy_doctor.returncode, 0, healthy_doctor.stderr)
            self.assertEqual(
                json.loads(healthy_doctor.stdout)["data"]["finding_count"], 0
            )
            rules_path = configured_inputs["rules"][1]
            rules_path.write_text(
                rules_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )

            status = self._run(
                "status", "--all", "--config", str(paths.config), "--json"
            )
            doctor = self._run("doctor", "--config", str(paths.config), "--json")

            self.assertEqual(status.returncode, 2)
            self.assertEqual(
                json.loads(status.stdout)["errors"][0]["code"], "full_rebuild_required"
            )
            self.assertEqual(doctor.returncode, 2)
            findings = json.loads(doctor.stdout)["data"]["findings"]
            self.assertIn(
                {
                    "code": "full_rebuild_required",
                    "severity": "warning",
                    "repair_class": "full_rebuild",
                    "path": "inputs/rules.json",
                    "next_action": (
                        "Run views rebuild --all to regenerate output from changed inputs."
                    ),
                    "detail_count": 0,
                    "omitted_detail_count": 0,
                },
                findings,
            )

    def test_source_data_inspect_and_resolve_use_stored_normalized_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = setup_workspace(Path(temporary) / "money")
            [row] = self._import_rows(
                paths,
                "Date,Description,Amount,Currency\n"
                "2026-08-08,Synthetic source check,-12.00,HKD\n",
            )
            correction_file = Path(temporary) / "stale-choice.csv"
            correction_file.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": row["transaction_id"],
                            "review_reasons": "source_data_issue",
                            "needs_review": "true",
                        }
                    ],
                ),
                encoding="utf-8",
            )
            corrected = self._run(
                "correct",
                "--file",
                str(correction_file),
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)

            inspected = self._run(
                "source-data",
                "inspect",
                row["transaction_id"],
                "--config",
                str(paths.config),
                "--json",
            )

            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            inspection = json.loads(inspected.stdout)
            self.assertEqual(inspection["command"], "source-data.inspect")
            self.assertEqual(
                inspection["data"]["transaction"]["evidence_status"], "stale"
            )
            self.assertTrue(
                inspection["data"]["transaction"]["correction_review_reason_active"]
            )

            resolved = self._run(
                "source-data",
                "resolve",
                row["transaction_id"],
                "--config",
                str(paths.config),
                "--json",
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            payload = json.loads(resolved.stdout)
            self.assertEqual(payload["command"], "source-data.resolve")
            self.assertTrue(payload["data"]["changed"])
            self.assertNotIn(
                "source_data_issue", paths.corrections.read_text(encoding="utf-8")
            )

    def test_evaluate_returns_a_named_clean_start_contract_error(self) -> None:
        result = self._run(
            "evaluate", "candidate.csv", "--reference", "truth.csv", "--json"
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "evaluate")
        self.assertEqual(payload["errors"][0]["code"], "legacy_csv_contract_removed")
        self.assertNotIn("ledger", result.stdout.casefold())

    def test_help_lists_restored_and_retired_forms(self) -> None:
        result = self._run("help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for form in (
            "profile bind",
            "profile bindings",
            "profile replace-pattern",
            "profile remove-pattern",
            "config [edit",
            "reconcile [--dry-run]",
            "review pair",
            "learn [--yes]",
            "source-data inspect",
            "evaluate` CSV comparison command is retired",
        ):
            self.assertIn(form, result.stdout)


if __name__ == "__main__":
    unittest.main()
