from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from honeymoney.cli import (
    _active_source_ids_by_fingerprint,
    _normalize_loaded_rows,
    _reconcile_command,
    main,
)
from honeymoney.corrections import (
    CORRECTION_COLUMNS,
    apply_correction_operation,
    ledger_output_documents,
    read_ledger,
)
from honeymoney.csv_artifacts import csv_document
from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    IdentityError,
    extractor_contract_id,
    manifest_document,
    ownership_record,
    record_fingerprint,
    source_id,
    source_namespace_id,
    source_ownership,
    source_revision,
)
from honeymoney.identity_state import identity_manifest_path, load_identity_state
from honeymoney.overlap import (
    DuplicateResolutionError,
    canonicalize_overlaps,
    overlap_manifest_document,
    overlap_manifest_path,
    source_occurrences_path,
)
from honeymoney.persistence import persist_generation
from honeymoney.schema import CATEGORIZED_COLUMNS, SOURCE_OCCURRENCE_COLUMNS


def _source_row(
    name: str, locator: str, row_number: int = 2
) -> tuple[dict[str, str], dict[str, object]]:
    row = {column: "" for column in SOURCE_OCCURRENCE_COLUMNS}
    row.update(
        {
            "date": "2026-05-04",
            "transaction_date": "2026-05-04",
            "account_id": "synthetic-card",
            "account_type": "credit_card",
            "original_amount": "-12.00",
            "original_currency": "HKD",
            "posted_amount": "-12.00",
            "posted_currency": "HKD",
            "amount_hkd": "-12.00",
            "merchant": "SYNTHETIC OVERLAP",
            "original_description": "SYNTHETIC OVERLAP",
            "category": "Dining",
            "flow_type": "expense",
            "flow_source": "correction",
            "owner": "Household",
            "confidence": "1.00",
            "needs_review": "false",
            "source_file": name,
            "source_row": str(row_number),
        }
    )
    namespace = source_namespace_id("workspace", locator)
    source = source_id(namespace)
    revision = source_revision((name + "\n").encode())
    contract = extractor_contract_id(
        1, {"id": "synthetic", "csv": {"columns": {"date": "Date"}}}
    )
    fingerprint = record_fingerprint(row)
    owner = ownership_record(
        source_id_value=source,
        fingerprint=fingerprint,
        origin=AllocationOrigin(
            revision, contract, AllocationLocator(1, (row_number,)), 1
        ),
    )
    row.update(
        {
            "transaction_id": owner["transaction_id"],
            "source_id": source,
            "source_namespace_id": namespace,
            "source_revision": revision,
            "source_record_id": owner["source_record_id"],
        }
    )
    manifest_source = source_ownership(
        source_id_value=source,
        namespace_id=namespace,
        revision=revision,
        contract_id=contract,
        records=[owner],
    )
    return row, manifest_source


class OverlapWorkspaceStateTest(unittest.TestCase):
    def _write_issue_31_state(self, path: Path) -> None:
        first, first_source = _source_row("a.csv", "a.csv")
        second, second_source = _source_row("b.csv", "b.csv")
        manifest = {
            "schema_version": 1,
            "sources": [first_source, second_source],
        }
        path.write_text(
            csv_document(SOURCE_OCCURRENCE_COLUMNS, [first, second]),
            encoding="utf-8",
        )
        identity_manifest_path(path).write_text(
            manifest_document(manifest), encoding="utf-8"
        )

    def test_exact_issue_31_state_migrates_in_memory_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)

            state = load_identity_state(path)

            self.assertTrue(state.canonical_migration_required)
            self.assertEqual(len(state.source_rows), 2)
            self.assertEqual(len(state.rows), 2)
            self.assertEqual(
                {row["transaction_id"] for row in state.rows},
                {row["transaction_id"] for row in state.source_rows},
            )
            self.assertRegex(
                state.overlap_manifest["namespace_key"], r"^ovns_[0-9a-f]{64}$"
            )
            self.assertFalse(source_occurrences_path(path).exists())
            self.assertFalse(overlap_manifest_path(path).exists())

    def test_duplicate_commands_reject_unpersisted_canonical_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps({"paths": {"output": str(path)}}), encoding="utf-8"
            )
            before = {
                path: path.read_bytes(),
                identity_manifest_path(path): identity_manifest_path(path).read_bytes(),
            }

            for argv in (
                ["duplicates", "--config", str(config_path), "--json"],
                [
                    "duplicates",
                    "resolve",
                    "ovr_" + "a" * 64,
                    "--as",
                    "same-event",
                    "--config",
                    str(config_path),
                    "--json",
                ],
            ):
                with (
                    self.subTest(argv=argv),
                    self.assertRaises(DuplicateResolutionError) as error,
                ):
                    main(argv)
                self.assertEqual(
                    error.exception.code, "duplicate_canonical_migration_required"
                )

            self.assertEqual(path.read_bytes(), before[path])
            self.assertEqual(
                identity_manifest_path(path).read_bytes(),
                before[identity_manifest_path(path)],
            )
            self.assertFalse(source_occurrences_path(path).exists())
            self.assertFalse(overlap_manifest_path(path).exists())

    def test_complete_state_publishes_and_reloads_both_hidden_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)
            migrated = load_identity_state(path)
            canonical = canonicalize_overlaps(
                migrated.source_rows, [], migrated.overlap_manifest
            )

            files = ledger_output_documents(
                path,
                canonical.rows,
                identity_manifest=migrated.manifest,
                source_occurrences=migrated.source_rows,
                overlap_manifest=canonical.manifest,
            )
            persist_generation(path, files)
            loaded = load_identity_state(path)

            self.assertFalse(loaded.canonical_migration_required)
            self.assertEqual(loaded.rows, canonical.rows)
            self.assertEqual(loaded.source_rows, migrated.source_rows)
            self.assertEqual(loaded.overlap_manifest, canonical.manifest)
            self.assertTrue(source_occurrences_path(path).exists())
            self.assertTrue(overlap_manifest_path(path).exists())
            self.assertEqual(
                list(loaded.rows[0]),
                CATEGORIZED_COLUMNS,
            )

    def test_reconcile_migrates_saved_source_correction_to_canonical_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "categorized.csv"
            self._write_issue_31_state(path)
            source_transaction_id = load_identity_state(path).source_rows[0][
                "transaction_id"
            ]
            corrections_path = root / "corrections.csv"
            corrections_path.write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": source_transaction_id,
                            "category": "Groceries",
                            "needs_review": "false",
                        }
                    ],
                ),
                encoding="utf-8",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "paths": {"output": str(path)},
                        "corrections": str(corrections_path),
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(_reconcile_command(["--config", str(config_path)]), 0)

            [canonical] = load_identity_state(path).rows
            self.assertEqual(canonical["category"], "Groceries")
            self.assertEqual(canonical["needs_review"], "false")

    def test_validated_canonical_account_type_conflict_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            first, first_source = _source_row("a.csv", "a.csv")
            second, second_source = _source_row("b.csv", "b.csv")
            first["account_type"] = "credit_card"
            second["account_type"] = "bank"
            manifest = {"schema_version": 1, "sources": [first_source, second_source]}
            canonical = canonicalize_overlaps(
                [first, second],
                [],
                {
                    "schema_version": 1,
                    "namespace_key": "ovns_" + "a" * 64,
                    "groups": [],
                },
            )
            files = ledger_output_documents(
                path,
                canonical.rows,
                identity_manifest=manifest,
                source_occurrences=[first, second],
                overlap_manifest=canonical.manifest,
            )
            persist_generation(path, files)

            [loaded] = read_ledger(path)

            self.assertEqual(loaded["account_type"], "")
            self.assertEqual(_normalize_loaded_rows([loaded])[0]["account_type"], "")

    def test_load_state_rejects_edited_conflicted_canonical_display_field(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            first, first_source = _source_row("a.csv", "a.csv")
            second, second_source = _source_row("b.csv", "b.csv")
            first["account_type"] = "credit_card"
            second["account_type"] = "bank"
            manifest = {"schema_version": 1, "sources": [first_source, second_source]}
            canonical = canonicalize_overlaps(
                [first, second],
                [],
                {
                    "schema_version": 1,
                    "namespace_key": "ovns_" + "a" * 64,
                    "groups": [],
                },
            )
            files = ledger_output_documents(
                path,
                canonical.rows,
                identity_manifest=manifest,
                source_occurrences=[first, second],
                overlap_manifest=canonical.manifest,
            )
            persist_generation(path, files)
            canonical.rows[0]["account_type"] = "arbitrary"
            path.write_text(
                csv_document(CATEGORIZED_COLUMNS, canonical.rows), encoding="utf-8"
            )

            with self.assertRaisesRegex(IdentityError, "identity_manifest_invalid"):
                load_identity_state(path)

    def test_load_state_allows_reviewed_canonical_mutable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)
            migrated = load_identity_state(path)
            canonical = canonicalize_overlaps(
                migrated.source_rows, [], migrated.overlap_manifest
            )
            canonical.rows[0]["category"] = "Shopping"
            canonical.rows[0]["needs_review"] = "false"
            files = ledger_output_documents(
                path,
                canonical.rows,
                identity_manifest=migrated.manifest,
                source_occurrences=migrated.source_rows,
                overlap_manifest=canonical.manifest,
            )
            persist_generation(path, files)

            [loaded] = load_identity_state(path).rows

            self.assertEqual(loaded["category"], "Shopping")
            self.assertEqual(loaded["needs_review"], "false")

    def test_migrated_partial_source_correction_marks_pooled_history_for_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            first_a, source_a = _source_row("a.csv", "a.csv", 2)
            second_a, duplicate_source_a = _source_row("a.csv", "a.csv", 3)
            first_b, source_b = _source_row("b.csv", "b.csv", 2)
            second_b, duplicate_source_b = _source_row("b.csv", "b.csv", 3)
            source_a["records"].extend(duplicate_source_a["records"])
            source_b["records"].extend(duplicate_source_b["records"])
            path.write_text(
                csv_document(
                    SOURCE_OCCURRENCE_COLUMNS,
                    [first_a, second_a, first_b, second_b],
                ),
                encoding="utf-8",
            )
            identity_manifest_path(path).write_text(
                manifest_document(
                    {
                        "schema_version": 1,
                        "sources": [source_a, source_b],
                    }
                ),
                encoding="utf-8",
            )
            source_id = first_a["transaction_id"]
            corrections_path = Path(temporary) / "corrections.csv"

            result = apply_correction_operation(
                {"corrections": str(corrections_path)},
                path,
                {source_id: {"category": "Dining", "needs_review": "false"}},
            )

            self.assertEqual(result.applied_count, 1)
            self.assertTrue(
                all(row["needs_review"] == "true" for row in result.ledger_rows)
            )
            self.assertTrue(
                all(
                    "overlap_history_ambiguous" in row["flags"].split(";")
                    for row in result.ledger_rows
                )
            )

    def test_reset_support_map_ignores_retired_source_records(self) -> None:
        first, first_source = _source_row("a.csv", "a.csv")
        _, retired_source = _source_row("b.csv", "b.csv")
        retired_source["records"][0]["state"] = "retired"
        manifest = {
            "schema_version": 1,
            "sources": [first_source, retired_source],
        }

        support = _active_source_ids_by_fingerprint(manifest)

        self.assertEqual(
            support[record_fingerprint(first)], {first_source["source_id"]}
        )

    def test_schema_one_overlap_manifest_migrates_in_memory_then_on_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)
            issue_31 = load_identity_state(path)
            canonical = canonicalize_overlaps(
                issue_31.source_rows, [], issue_31.overlap_manifest
            )
            files = ledger_output_documents(
                path,
                canonical.rows,
                identity_manifest=issue_31.manifest,
                source_occurrences=issue_31.source_rows,
                overlap_manifest=canonical.manifest,
            )
            v1_manifest = {
                "schema_version": 1,
                "namespace_key": canonical.manifest["namespace_key"],
                "groups": [
                    {
                        "group_id": group["overlap_group_id"],
                        "record_fingerprint": group["record_fingerprint"],
                        "slots": group["slots"],
                    }
                    for group in canonical.manifest["groups"]
                ],
            }
            files[overlap_manifest_path(path)] = (
                json.dumps(
                    v1_manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
            persist_generation(path, files)
            raw_v1 = overlap_manifest_path(path).read_bytes()

            migrated = load_identity_state(path)

            self.assertTrue(migrated.overlap_migration_required)
            self.assertEqual(migrated.overlap_manifest["schema_version"], 2)
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps({"paths": {"output": str(path)}}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = main(
                    ["duplicates", "--config", str(config_path), "--json"]
                )
            self.assertEqual(return_code, 0)
            self.assertEqual(overlap_manifest_path(path).read_bytes(), raw_v1)
            rewritten = ledger_output_documents(
                path,
                migrated.rows,
                identity_manifest_document=migrated.manifest_document,
                source_occurrences=migrated.source_rows,
                source_evidence=migrated.source_evidence_rows,
                overlap_manifest=migrated.overlap_manifest,
            )
            self.assertIn(
                '"schema_version":2',
                rewritten[overlap_manifest_path(path)],
            )
            persist_generation(path, rewritten)
            reloaded = load_identity_state(path)
            self.assertFalse(reloaded.overlap_migration_required)
            self.assertEqual(
                overlap_manifest_path(path).read_text(encoding="utf-8"),
                overlap_manifest_document(reloaded.overlap_manifest),
            )


if __name__ == "__main__":
    unittest.main()
