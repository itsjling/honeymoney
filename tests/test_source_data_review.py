import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.csv_artifacts import csv_document
from honeymoney.identity_state import load_identity_state
from honeymoney.overlap import overlap_manifest_path, source_occurrences_path
from honeymoney.persistence import GenerationConflictError
from honeymoney.review_state import (
    REVIEW_REASON_IDENTITY,
    REVIEW_REASON_SOURCE_DATA,
)
from honeymoney.schema import (
    PRE_RATE_METADATA_CATEGORIZED_COLUMNS,
    PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS,
    SOURCE_OCCURRENCE_COLUMNS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class SourceDataReviewWorkflowTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
        filesystem_fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        python_paths = [str(REPO_ROOT)]
        if filesystem_fault is not None:
            python_paths.insert(0, str(REPO_ROOT / "tests" / "fault_injection"))
            env["HONEYMONEY_TEST_FS_FAULT"] = filesystem_fault
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _setup_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "synthetic-money"
        result = self._run_cli(
            ["setup", "--root", str(root), "--json"],
            cwd=REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _write_statement(self, path: Path, rows: list[str]) -> None:
        path.write_text(
            "\n".join(["Date,Description,Amount,Currency", *rows]),
            encoding="utf-8",
        )

    def _import(self, root: Path, statement: Path, *extra: str) -> None:
        result = self._run_cli(
            [
                "import",
                str(statement),
                *extra,
                "--no-interactive",
                "--json",
            ],
            cwd=root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _read_csv(self, path: Path) -> tuple[list[str], list[dict[str, str]]]:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or []), list(reader)

    def _write_csv(
        self,
        path: Path,
        fieldnames: list[str],
        rows: list[dict[str, str]],
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(
                {field: row.get(field, "") for field in fieldnames} for row in rows
            )

    def _mark_canonical_stale(
        self,
        root: Path,
        transaction_ids: set[str],
        *,
        correction: bool = False,
        page_flag: bool = False,
    ) -> None:
        ledger_path = root / "output" / "categorized.csv"
        fieldnames, rows = self._read_csv(ledger_path)
        for row in rows:
            if row["transaction_id"] not in transaction_ids:
                continue
            flags = [item for item in row["flags"].split(";") if item]
            if "invalid_amount" not in flags:
                flags.append("invalid_amount")
            if page_flag:
                flags.append("statement_opening_balance_conflict_page_4")
            reasons = [item for item in row["review_reasons"].split(";") if item]
            if REVIEW_REASON_SOURCE_DATA not in reasons:
                reasons.append(REVIEW_REASON_SOURCE_DATA)
            row["flags"] = ";".join(flags)
            row["review_reasons"] = ";".join(reasons)
            row["needs_review"] = "true"
        self._write_csv(ledger_path, fieldnames, rows)
        if correction:
            self._mark_correction_stale(root, transaction_ids)

    def _make_pre_canonical(self, root: Path) -> str:
        categorized_path = root / "output" / "categorized.csv"
        state = load_identity_state(categorized_path)
        assert state.source_rows is not None
        [source_row] = state.source_rows
        categorized_path.write_text(
            csv_document(SOURCE_OCCURRENCE_COLUMNS, [source_row]),
            encoding="utf-8",
        )
        source_occurrences_path(categorized_path).unlink()
        overlap_manifest_path(categorized_path).unlink()
        return source_row["transaction_id"]

    def _downgrade_rate_schema(self, root: Path) -> None:
        ledger_path = root / "output" / "categorized.csv"
        _, ledger = self._read_csv(ledger_path)
        self._write_csv(
            ledger_path,
            PRE_RATE_METADATA_CATEGORIZED_COLUMNS,
            ledger,
        )
        occurrence_path = root / "output" / ".honeymoney-source-occurrences.csv"
        _, occurrences = self._read_csv(occurrence_path)
        self._write_csv(
            occurrence_path,
            PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS,
            occurrences,
        )

    def _mark_correction_stale(
        self,
        root: Path,
        transaction_ids: set[str],
    ) -> None:
        correction_path = root / "corrections.csv"
        correction_fields, correction_rows = self._read_csv(correction_path)
        by_id = {row["transaction_id"]: row for row in correction_rows}
        for transaction_id in transaction_ids:
            row = by_id.setdefault(
                transaction_id,
                {field: "" for field in correction_fields},
            )
            row["transaction_id"] = transaction_id
            row["needs_review"] = "true"
            row["review_reasons"] = REVIEW_REASON_SOURCE_DATA
        self._write_csv(
            correction_path,
            correction_fields,
            sorted(by_id.values(), key=lambda row: row["transaction_id"]),
        )

    def _generation_bytes(self, root: Path) -> dict[str, bytes]:
        paths = [
            root / "output" / "categorized.csv",
            root / "output" / "review_needed.csv",
            root / "output" / ".honeymoney-identity.json",
            root / "output" / ".honeymoney-source-occurrences.csv",
            root / "output" / ".honeymoney-overlap.json",
            root / "corrections.csv",
        ]
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in paths
            if path.exists()
        }

    def test_ordinary_correction_repairs_cleared_history_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "history-provenance.csv"
            self._write_statement(
                statement,
                ["2026-05-01,SYNTHETIC HISTORY,-10.00,HKD"],
            )
            self._import(root, statement)
            ledger_path = root / "output" / "categorized.csv"
            fieldnames, rows = self._read_csv(ledger_path)
            [row] = rows
            transaction_id = row["transaction_id"]
            flags = [item for item in row["flags"].split(";") if item]
            flags.extend(
                [
                    "overlap_history_ambiguous",
                    "source_provenance_ambiguous",
                ]
            )
            reasons = [item for item in row["review_reasons"].split(";") if item]
            reasons.extend(
                [
                    REVIEW_REASON_IDENTITY,
                    REVIEW_REASON_SOURCE_DATA,
                ]
            )
            row["flags"] = ";".join(dict.fromkeys(flags))
            row["review_reasons"] = ";".join(dict.fromkeys(reasons))
            row["needs_review"] = "true"
            self._write_csv(ledger_path, fieldnames, rows)

            corrected = self._run_cli(
                [
                    "review",
                    "--transaction",
                    transaction_id,
                    "--as",
                    "expense",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(corrected.returncode, 0, corrected.stderr)
            _, [updated] = self._read_csv(ledger_path)
            updated_flags = set(filter(None, updated["flags"].split(";")))
            self.assertNotIn("overlap_history_ambiguous", updated_flags)
            self.assertNotIn("source_provenance_ambiguous", updated_flags)
            updated_reasons = set(filter(None, updated["review_reasons"].split(";")))
            self.assertNotIn(REVIEW_REASON_IDENTITY, updated_reasons)
            self.assertNotIn(REVIEW_REASON_SOURCE_DATA, updated_reasons)
            self.assertEqual(updated["needs_review"], "true")
            _, [saved_correction] = self._read_csv(root / "corrections.csv")
            saved_reasons = set(
                filter(None, saved_correction["review_reasons"].split(";"))
            )
            self.assertNotIn(REVIEW_REASON_IDENTITY, saved_reasons)
            self.assertNotIn(REVIEW_REASON_SOURCE_DATA, saved_reasons)

            inspected = self._run_cli(
                ["source-data", "inspect", transaction_id, "--json"],
                cwd=root,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            item = json.loads(inspected.stdout)["data"]["transaction"]
            self.assertEqual(item["evidence_status"], "clear")
            self.assertFalse(item["review_reason_active"])
            self.assertFalse(item["correction_review_reason_active"])
            self.assertEqual(item["source_data_flags"], [])
            self.assertEqual(item["active_evidence_flags"], [])

    def test_inspect_and_resolve_stale_actual_and_estimated_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["exchange_rates"]["EUR"] = 8.5
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            statement = root / "mixed-valuations.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-01,SYNTHETIC ACTUAL,-10.00,HKD",
                    "2026-05-02,SYNTHETIC ESTIMATED,-20.00,EUR",
                ],
            )
            self._import(root, statement)
            _, rows = self._read_csv(root / "output" / "categorized.csv")
            by_currency = {row["posted_currency"]: row for row in rows}
            actual_id = by_currency["HKD"]["transaction_id"]
            estimated_id = by_currency["EUR"]["transaction_id"]
            self._mark_canonical_stale(
                root,
                {actual_id, estimated_id},
                correction=True,
                page_flag=True,
            )

            inspected = self._run_cli(
                ["source-data", "inspect", actual_id, "--json"],
                cwd=root,
            )

            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            payload = json.loads(inspected.stdout)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["command"], "source-data.inspect")
            item = payload["data"]["transaction"]
            self.assertEqual(item["transaction_id"], actual_id)
            self.assertEqual(item["evidence_status"], "stale")
            self.assertEqual(item["valuation_status"], "actual")
            self.assertTrue(item["review_reason_active"])
            self.assertEqual(item["source_data_flags"], ["invalid_amount"])
            self.assertEqual(item["active_evidence_flags"], [])
            self.assertEqual(item["source_occurrence_count"], 1)
            self.assertEqual(item["evidence"][0]["evidence_status"], "no_support")
            self.assertEqual(
                item["evidence"][0]["source_display"],
                "mixed-valuations.csv",
            )
            self.assertEqual(item["evidence"][0]["field"], "")
            self.assertEqual(item["evidence"][0]["flag"], "")

            estimated = self._run_cli(
                ["source-data", "inspect", estimated_id, "--json"],
                cwd=root,
            )
            self.assertEqual(estimated.returncode, 0, estimated.stderr)
            estimated_item = json.loads(estimated.stdout)["data"]["transaction"]
            self.assertEqual(estimated_item["valuation_status"], "estimated")
            self.assertEqual(estimated_item["evidence_status"], "stale")
            self.assertTrue(estimated_item["review_reason_active"])
            self.assertTrue(estimated_item["correction_review_reason_active"])
            self.assertEqual(estimated_item["source_data_flags"], ["invalid_amount"])

            human = self._run_cli(
                ["source-data", "inspect", actual_id],
                cwd=root,
            )
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn("Evidence status: stale", human.stdout)
            self.assertIn("mixed-valuations.csv", human.stdout)
            self.assertNotIn("SYNTHETIC ACTUAL", human.stdout)
            self.assertNotIn("-10.00", human.stdout)

            failed_before = self._generation_bytes(root)
            failed = self._run_cli(
                ["source-data", "resolve", actual_id, "--json"],
                cwd=root,
                filesystem_fault="replace-before:categorized.csv",
            )
            self.assertEqual(failed.returncode, 2, failed.stderr)
            self.assertEqual(self._generation_bytes(root), failed_before)

            resolved = self._run_cli(
                ["source-data", "resolve", actual_id, "--json"],
                cwd=root,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            resolved_payload = json.loads(resolved.stdout)
            self.assertEqual(resolved_payload["command"], "source-data.resolve")
            self.assertEqual(resolved_payload["data"]["result"], "resolved")
            self.assertTrue(resolved_payload["data"]["changed"])
            _, repaired_rows = self._read_csv(root / "output" / "categorized.csv")
            repaired = next(
                row for row in repaired_rows if row["transaction_id"] == actual_id
            )
            self.assertNotIn("invalid_amount", repaired["flags"].split(";"))
            self.assertNotIn(
                "statement_opening_balance_conflict_page_4",
                repaired["flags"].split(";"),
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                repaired["review_reasons"].split(";"),
            )
            still_stale = next(
                row for row in repaired_rows if row["transaction_id"] == estimated_id
            )
            self.assertIn("invalid_amount", still_stale["flags"].split(";"))
            self.assertIn(
                "statement_opening_balance_conflict_page_4",
                still_stale["flags"].split(";"),
            )
            self.assertIn(
                REVIEW_REASON_SOURCE_DATA,
                still_stale["review_reasons"].split(";"),
            )
            _, corrections = self._read_csv(root / "corrections.csv")
            repaired_correction = next(
                row for row in corrections if row["transaction_id"] == actual_id
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                repaired_correction["review_reasons"].split(";"),
            )

            stable_before = self._generation_bytes(root)
            repeated = self._run_cli(
                ["source-data", "resolve", actual_id, "--json"],
                cwd=root,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["data"]["changed"])
            self.assertEqual(self._generation_bytes(root), stable_before)

            reconciled = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            _, reconciled_rows = self._read_csv(root / "output" / "categorized.csv")
            estimated_row = next(
                row for row in reconciled_rows if row["transaction_id"] == estimated_id
            )
            self.assertNotIn("invalid_amount", estimated_row["flags"].split(";"))
            self.assertNotIn(
                "statement_opening_balance_conflict_page_4",
                estimated_row["flags"].split(";"),
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                estimated_row["review_reasons"].split(";"),
            )

    def test_active_overlap_evidence_blocks_resolution_then_replace_clears_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "first.csv"
            second = root / "second.csv"
            row = "2026-05-04,SYNTHETIC OVERLAP,-30.00,JPY"
            self._write_statement(first, [row])
            self._write_statement(second, [row])
            self._import(root, first)
            self._import(root, second)
            occurrence_path = root / "output" / ".honeymoney-source-occurrences.csv"
            fields, occurrences = self._read_csv(occurrence_path)
            active = next(
                item for item in occurrences if item["source_file"] == "first.csv"
            )
            active["flags"] = (
                active["flags"]
                + ";statement_opening_balance_conflict"
                + ";statement_opening_balance_conflict_page_4"
            ).strip(";")
            active["source_page"] = "4"
            active["statement_section"] = "JPY Savings"
            self._write_csv(occurrence_path, fields, occurrences)

            reconciled = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            _, ledger = self._read_csv(root / "output" / "categorized.csv")
            [canonical] = ledger
            transaction_id = canonical["transaction_id"]
            self.assertIn(
                "statement_opening_balance_conflict",
                canonical["flags"].split(";"),
            )
            self.assertIn(
                REVIEW_REASON_SOURCE_DATA,
                canonical["review_reasons"].split(";"),
            )

            inspected = self._run_cli(
                ["source-data", "inspect", transaction_id, "--json"],
                cwd=root,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            item = json.loads(inspected.stdout)["data"]["transaction"]
            self.assertEqual(item["evidence_status"], "active")
            self.assertEqual(item["valuation_status"], "missing")
            self.assertEqual(item["source_occurrence_count"], 2)
            self.assertEqual(
                item["active_evidence_flags"],
                ["statement_opening_balance_conflict"],
            )
            active_evidence = next(
                evidence
                for evidence in item["evidence"]
                if evidence["evidence_status"] == "active"
            )
            self.assertEqual(active_evidence["source_display"], "first.csv")
            self.assertEqual(active_evidence["source_page"], "4")
            self.assertEqual(active_evidence["statement_section"], "JPY Savings")
            self.assertEqual(
                active_evidence["field"],
                "statement_opening_balance",
            )
            self.assertEqual(
                active_evidence["flag"],
                "statement_opening_balance_conflict",
            )
            self.assertNotIn("30.00", inspected.stdout)
            self.assertNotIn("SYNTHETIC OVERLAP", inspected.stdout)

            blocked = self._run_cli(
                ["source-data", "resolve", transaction_id, "--json"],
                cwd=root,
            )
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            error = json.loads(blocked.stdout)["errors"][0]
            self.assertEqual(error["code"], "source_data_evidence_active")
            self.assertNotIn("30.00", blocked.stdout)
            self.assertNotIn("SYNTHETIC OVERLAP", blocked.stdout)
            blocked_human = self._run_cli(
                ["source-data", "resolve", transaction_id],
                cwd=root,
            )
            self.assertEqual(blocked_human.returncode, 2)
            self.assertIn("source_data_evidence_active", blocked_human.stderr)
            self.assertNotIn("30.00", blocked_human.stderr)
            self.assertNotIn("SYNTHETIC OVERLAP", blocked_human.stderr)

            self._mark_canonical_stale(
                root,
                {transaction_id},
                correction=True,
            )
            self._import(root, first, "--replace")
            _, replaced_rows = self._read_csv(root / "output" / "categorized.csv")
            [replaced] = replaced_rows
            self.assertNotIn(
                "statement_opening_balance_conflict",
                replaced["flags"].split(";"),
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                replaced["review_reasons"].split(";"),
            )
            _, corrections = self._read_csv(root / "corrections.csv")
            [correction] = corrections
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                correction["review_reasons"].split(";"),
            )
            first_generation = self._generation_bytes(root)
            self._import(root, first, "--replace")
            self.assertEqual(self._generation_bytes(root), first_generation)

    def test_cli_reports_typed_active_provenance_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "provenance-first.csv"
            second = root / "provenance-second.csv"
            row = "2026-05-04,SYNTHETIC PROVENANCE,-10.00,HKD"
            self._write_statement(
                first,
                [row],
            )
            self._write_statement(
                second,
                [row, row],
            )
            self._import(root, first)
            self._import(root, second)
            _, ledger = self._read_csv(root / "output" / "categorized.csv")
            transaction_id = ledger[0]["transaction_id"]

            inspected = self._run_cli(
                ["source-data", "inspect", transaction_id, "--json"],
                cwd=root,
            )

            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            item = json.loads(inspected.stdout)["data"]["transaction"]
            self.assertEqual(item["evidence_status"], "active")
            self.assertEqual(
                item["active_evidence_flags"],
                ["source_provenance_inconsistent"],
            )
            self.assertIn(
                "source_provenance_inconsistent",
                item["source_data_flags"],
            )
            self.assertTrue(item["review_reason_active"])
            self.assertEqual(len(item["evidence"]), 3)
            self.assertEqual(
                {evidence["source_display"] for evidence in item["evidence"]},
                {"provenance-first.csv", "provenance-second.csv"},
            )
            self.assertTrue(
                all(
                    evidence["field"] == "provenance"
                    and evidence["flag"] == "source_provenance_inconsistent"
                    and evidence["evidence_type"] == "provenance_conflict"
                    and evidence["evidence_status"] == "active"
                    for evidence in item["evidence"]
                )
            )
            self.assertNotIn("SYNTHETIC PROVENANCE", inspected.stdout)
            self.assertNotIn("-10.00", inspected.stdout)

            blocked = self._run_cli(
                ["source-data", "resolve", transaction_id, "--json"],
                cwd=root,
            )
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            self.assertEqual(
                json.loads(blocked.stdout)["errors"][0]["code"],
                "source_data_evidence_active",
            )

    def test_resolve_rejects_a_generation_change_during_state_load(self) -> None:
        import honeymoney.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "concurrent-resolve.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC CONCURRENT RESOLVE,-10.00,HKD"],
            )
            self._import(root, statement)
            ledger_path = root / "output" / "categorized.csv"
            _, rows = self._read_csv(ledger_path)
            transaction_id = rows[0]["transaction_id"]
            self._mark_canonical_stale(root, {transaction_id})
            real_load = cli.load_configured_identity_state
            published_generation: dict[str, bytes] = {}

            def load_then_publish(*args: object, **kwargs: object) -> object:
                state = real_load(*args, **kwargs)
                fieldnames, current_rows = self._read_csv(ledger_path)
                current_rows[0]["notes"] = "Synthetic concurrent update"
                self._write_csv(ledger_path, fieldnames, current_rows)
                published_generation.update(self._generation_bytes(root))
                return state

            with (
                patch.object(
                    cli,
                    "load_configured_identity_state",
                    side_effect=load_then_publish,
                ),
                self.assertRaises(GenerationConflictError),
            ):
                cli._source_data_resolve_command(
                    transaction_id,
                    str(root / "config.json"),
                    json_output=True,
                )

            self.assertEqual(self._generation_bytes(root), published_generation)
            _, retained_rows = self._read_csv(ledger_path)
            self.assertEqual(
                retained_rows[0]["notes"],
                "Synthetic concurrent update",
            )

    def test_already_clear_resolution_rechecks_its_generation(self) -> None:
        import honeymoney.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "already-clear.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC ALREADY CLEAR,-10.00,HKD"],
            )
            self._import(root, statement)
            ledger_path = root / "output" / "categorized.csv"
            _, rows = self._read_csv(ledger_path)
            transaction_id = rows[0]["transaction_id"]
            real_repair = cli.repair_source_data_review_state

            def repair_then_publish(*args: object, **kwargs: object):
                changed_ids = real_repair(*args, **kwargs)
                ledger_path.write_bytes(ledger_path.read_bytes() + b"\n")
                return changed_ids

            with (
                patch.object(
                    cli,
                    "repair_source_data_review_state",
                    side_effect=repair_then_publish,
                ),
                self.assertRaises(GenerationConflictError),
            ):
                cli._source_data_resolve_command(
                    transaction_id,
                    str(root / "config.json"),
                    json_output=True,
                )

    def test_resolve_schema_migration_repairs_all_stale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "resolve-migration.csv"
            self._write_statement(
                statement,
                [
                    "2026-05-04,SYNTHETIC RESOLVE ONE,-10.00,HKD",
                    "2026-05-05,SYNTHETIC RESOLVE TWO,-20.00,HKD",
                ],
            )
            self._import(root, statement)
            _, rows = self._read_csv(root / "output" / "categorized.csv")
            transaction_ids = {row["transaction_id"] for row in rows}
            self._mark_canonical_stale(
                root,
                transaction_ids,
                correction=True,
                page_flag=True,
            )
            self._downgrade_rate_schema(root)

            resolved = self._run_cli(
                [
                    "source-data",
                    "resolve",
                    sorted(transaction_ids)[0],
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertTrue(json.loads(resolved.stdout)["data"]["changed"])
            fields, repaired_rows = self._read_csv(root / "output" / "categorized.csv")
            self.assertIn("valuation_rate_date", fields)
            for repaired in repaired_rows:
                self.assertNotIn("invalid_amount", repaired["flags"].split(";"))
                self.assertNotIn(
                    "statement_opening_balance_conflict_page_4",
                    repaired["flags"].split(";"),
                )
                self.assertNotIn(
                    REVIEW_REASON_SOURCE_DATA,
                    repaired["review_reasons"].split(";"),
                )
            _, corrections = self._read_csv(root / "corrections.csv")
            self.assertEqual(len(corrections), 2)
            self.assertTrue(
                all(
                    REVIEW_REASON_SOURCE_DATA
                    not in correction["review_reasons"].split(";")
                    for correction in corrections
                )
            )

    def test_duplicate_resolution_migration_repairs_stale_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            first = root / "duplicate-first.csv"
            second = root / "duplicate-second.csv"
            row = "2026-05-04,SYNTHETIC DUPLICATE,-10.00,HKD"
            self._write_statement(first, [row])
            self._write_statement(second, [row, row])
            self._import(root, first)
            self._import(root, second)
            listed = self._run_cli(["duplicates", "--json"], cwd=root)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            [group] = json.loads(listed.stdout)["data"]["groups"]
            _, ledger = self._read_csv(root / "output" / "categorized.csv")
            transaction_ids = {item["transaction_id"] for item in ledger}
            self._mark_canonical_stale(
                root,
                transaction_ids,
                correction=True,
                page_flag=True,
            )
            self._downgrade_rate_schema(root)

            resolved = self._run_cli(
                [
                    "duplicates",
                    "resolve",
                    group["group_id"],
                    "--as",
                    "keep-all",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            fields, repaired_rows = self._read_csv(root / "output" / "categorized.csv")
            self.assertIn("valuation_rate_date", fields)
            for repaired in repaired_rows:
                self.assertNotIn("invalid_amount", repaired["flags"].split(";"))
                self.assertNotIn(
                    "statement_opening_balance_conflict_page_4",
                    repaired["flags"].split(";"),
                )
                self.assertNotIn(
                    REVIEW_REASON_SOURCE_DATA,
                    repaired["review_reasons"].split(";"),
                )
            _, corrections = self._read_csv(root / "corrections.csv")
            self.assertTrue(
                all(
                    REVIEW_REASON_SOURCE_DATA
                    not in correction["review_reasons"].split(";")
                    for correction in corrections
                )
            )

    def test_rate_schema_migration_repairs_stale_source_data_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "rate-migration.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC RATE MIGRATION,-10.00,HKD"],
            )
            self._import(root, statement)
            _, rows = self._read_csv(root / "output" / "categorized.csv")
            transaction_id = rows[0]["transaction_id"]
            self._mark_canonical_stale(
                root,
                {transaction_id},
                correction=True,
                page_flag=True,
            )
            self._downgrade_rate_schema(root)
            rate_document = root / "rates-download.json"
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
                            "records": [
                                {
                                    "end_of_day": "2026-05-04",
                                    "eur": 9.25,
                                }
                            ],
                        },
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            migrated = self._run_cli(
                ["rates", "import", str(rate_document), "--json"],
                cwd=root,
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            _, repaired_rows = self._read_csv(root / "output" / "categorized.csv")
            [repaired] = repaired_rows
            self.assertNotIn("invalid_amount", repaired["flags"].split(";"))
            self.assertNotIn(
                "statement_opening_balance_conflict_page_4",
                repaired["flags"].split(";"),
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                repaired["review_reasons"].split(";"),
            )
            _, corrections = self._read_csv(root / "corrections.csv")
            [correction] = corrections
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                correction["review_reasons"].split(";"),
            )

    def test_correction_migration_repairs_projected_stale_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "correction-migration.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC CORRECTION MIGRATION,-10.00,HKD"],
            )
            self._import(root, statement)
            source_transaction_id = self._make_pre_canonical(root)
            self._mark_correction_stale(root, {source_transaction_id})
            decisions = root / "migration-decisions.json"
            decisions.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": source_transaction_id,
                            "decision": "expense",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            migrated = self._run_cli(
                ["review", "--file", str(decisions), "--json"],
                cwd=root,
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            _, ledger = self._read_csv(root / "output" / "categorized.csv")
            [canonical] = ledger
            self.assertNotEqual(
                canonical["transaction_id"],
                source_transaction_id,
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                canonical["review_reasons"].split(";"),
            )
            _, corrections = self._read_csv(root / "corrections.csv")
            [correction] = corrections
            self.assertEqual(
                correction["transaction_id"],
                canonical["transaction_id"],
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                correction["review_reasons"].split(";"),
            )

    def test_correction_schema_migration_repairs_stale_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "correction-schema.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC CORRECTION SCHEMA,-10.00,HKD"],
            )
            self._import(root, statement)
            _, rows = self._read_csv(root / "output" / "categorized.csv")
            transaction_id = rows[0]["transaction_id"]
            self._mark_canonical_stale(
                root,
                {transaction_id},
                correction=True,
                page_flag=True,
            )
            self._downgrade_rate_schema(root)

            migrated = self._run_cli(
                [
                    "review",
                    "--transaction",
                    transaction_id,
                    "--as",
                    "expense",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            _, ledger = self._read_csv(root / "output" / "categorized.csv")
            [repaired] = ledger
            self.assertNotIn("invalid_amount", repaired["flags"].split(";"))
            self.assertNotIn(
                "statement_opening_balance_conflict_page_4",
                repaired["flags"].split(";"),
            )
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                repaired["review_reasons"].split(";"),
            )
            _, corrections = self._read_csv(root / "corrections.csv")
            [correction] = corrections
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                correction["review_reasons"].split(";"),
            )

    def test_schema_migration_repairs_stale_source_data_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._setup_workspace(tmp)
            statement = root / "migration.csv"
            self._write_statement(
                statement,
                ["2026-05-04,SYNTHETIC MIGRATION,-10.00,HKD"],
            )
            self._import(root, statement)
            _, ledger = self._read_csv(root / "output" / "categorized.csv")
            transaction_id = ledger[0]["transaction_id"]
            self._mark_canonical_stale(
                root,
                {transaction_id},
                correction=True,
            )
            ledger_path = root / "output" / "categorized.csv"
            _, ledger = self._read_csv(ledger_path)
            self._write_csv(
                ledger_path,
                PRE_RATE_METADATA_CATEGORIZED_COLUMNS,
                ledger,
            )
            occurrence_path = root / "output" / ".honeymoney-source-occurrences.csv"
            _, occurrences = self._read_csv(occurrence_path)
            self._write_csv(
                occurrence_path,
                PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS,
                occurrences,
            )

            reconciled = self._run_cli(["reconcile", "--json"], cwd=root)

            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            fields, rows = self._read_csv(ledger_path)
            self.assertIn("valuation_rate_date", fields)
            self.assertIn("valuation_provider", fields)
            [repaired] = rows
            self.assertNotIn("invalid_amount", repaired["flags"].split(";"))
            self.assertNotIn(
                REVIEW_REASON_SOURCE_DATA,
                repaired["review_reasons"].split(";"),
            )
            stable_before = self._generation_bytes(root)
            repeated = self._run_cli(["reconcile", "--json"], cwd=root)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(self._generation_bytes(root), stable_before)


if __name__ == "__main__":
    unittest.main()
