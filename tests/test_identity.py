from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    IdentityError,
    IdentityResolution,
    IncomingRecordIdentity,
    IncomingSourceIdentity,
    RecordResolutionDiagnostic,
    ResolvedSourceIdentity,
    allocation_locator_bytes,
    canonical_profile_json,
    digest,
    empty_manifest,
    extractor_contract_id,
    logical_locator,
    manifest_document,
    ownership_exact_state_key,
    ownership_record,
    parse_manifest,
    record_fingerprint,
    resolve_batch,
    resolve_records,
    resolve_sources,
    source_id,
    source_namespace_id,
    source_ownership,
    source_record_id,
    source_revision,
    transaction_id,
    validate_manifest,
)


class IdentityCoreTest(unittest.TestCase):
    def _identity_values(self) -> tuple[str, str, str, str, AllocationOrigin]:
        namespace = source_namespace_id("workspace", "statements/may.csv")
        source = source_id(namespace)
        revision = source_revision(b"synthetic statement\n")
        contract = extractor_contract_id(
            1,
            {
                "id": "synthetic",
                "account_id": "checking",
                "csv": {"columns": {"date": "Date"}},
                "category": "Unknown",
            },
        )
        origin = AllocationOrigin(revision, contract, AllocationLocator(1, (2,)), 1)
        return namespace, source, revision, contract, origin

    def _valid_manifest(self) -> dict[str, object]:
        namespace, source, revision, contract, origin = self._identity_values()
        fingerprint = record_fingerprint(
            {
                "account_id": "checking",
                "date": "2026-06-18",
                "transaction_date": "2026-06-18",
                "posting_date": "",
                "original_amount": "-12.00",
                "original_currency": "hkd",
                "posted_amount": "-12",
                "posted_currency": "HKD",
                "merchant": "SYNTHETIC SHOP",
                "original_description": "SYNTHETIC SHOP",
            }
        )
        record = ownership_record(
            source_id_value=source,
            fingerprint=fingerprint,
            origin=origin,
        )
        return {
            "schema_version": 1,
            "sources": [
                source_ownership(
                    source_id_value=source,
                    namespace_id=namespace,
                    revision=revision,
                    contract_id=contract,
                    records=[record],
                )
            ],
        }

    def test_digest_uses_the_exact_binary_framing(self) -> None:
        domain = b"example-v1"
        components = (b"alpha", b"\x00beta")
        framed = (
            b"honeymoney.identity\x00"
            + struct.pack(">I", len(domain))
            + domain
            + struct.pack(">I", len(components))
            + b"".join(
                struct.pack(">Q", len(component)) + component
                for component in components
            )
        )
        self.assertEqual(
            digest("example-v1", *components), hashlib.sha256(framed).hexdigest()
        )
        self.assertNotEqual(
            digest("example-v1", b"alphabeta"), digest("example-v1", *components)
        )

    def test_logical_locator_normalizes_workspace_and_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            source = root / "statements" / "café.csv"
            source.parent.mkdir(parents=True)
            source.write_text("synthetic", encoding="utf-8")
            kind, locator = logical_locator(source, root)
            self.assertEqual(kind, "workspace")
            self.assertEqual(locator, "statements/café.csv")
            self.assertEqual(locator, unicodedata.normalize("NFC", locator))

            external = Path(temporary) / "outside.csv"
            external.write_text("synthetic", encoding="utf-8")
            external_kind, external_locator = logical_locator(external, root)
            self.assertEqual(external_kind, "external")
            self.assertTrue(external_locator.endswith("outside.csv"))

    def test_source_ids_are_domain_separated_and_full_length(self) -> None:
        workspace = source_namespace_id("workspace", "statements/may.csv")
        external = source_namespace_id("external", "statements/may.csv")
        self.assertNotEqual(workspace, external)
        self.assertRegex(workspace, r"^ns_[0-9a-f]{64}$")
        self.assertRegex(source_id(workspace), r"^src_[0-9a-f]{64}$")
        self.assertRegex(source_revision(b"a\r\n"), r"^rev_[0-9a-f]{64}$")

    def test_record_fingerprint_normalizes_unicode_whitespace_currency_and_negative_zero(
        self,
    ) -> None:
        first = {
            "account_id": "  CAFE\u0301  ACCOUNT ",
            "date": "2026-06-18",
            "transaction_date": "",
            "posting_date": "2026-06-18",
            "original_amount": "-0.000",
            "original_currency": " hkd ",
            "posted_amount": "12.3400",
            "posted_currency": "hKd",
            "merchant": "  SYNTHETIC\n SHOP ",
            "original_description": "  PAY\tMENT ",
        }
        second = {
            "account_id": "café account",
            "date": "2026-06-18",
            "transaction_date": "",
            "posting_date": "2026-06-18",
            "original_amount": "0",
            "original_currency": "HKD",
            "posted_amount": "12.34",
            "posted_currency": "HKD",
            "merchant": "synthetic shop",
            "original_description": "pay ment",
        }
        self.assertEqual(record_fingerprint(first), record_fingerprint(second))
        self.assertRegex(record_fingerprint(first), r"^fp_[0-9a-f]{64}$")
        with self.assertRaises(IdentityError):
            record_fingerprint({**first, "posted_amount": "NaN"})

    def test_allocation_locator_contract_and_derived_record_ids(self) -> None:
        namespace, source, revision, contract, origin = self._identity_values()
        self.assertTrue(namespace.startswith("ns_"))
        self.assertEqual(
            allocation_locator_bytes(origin.locator),
            b"honeymoney.record-locator-v1\x00" + b"\x01\x01" + struct.pack(">Q", 2),
        )
        fingerprint = "fp_" + "a" * 64
        record = source_record_id(source, origin, fingerprint)
        self.assertRegex(record, r"^rec_[0-9a-f]{64}$")
        self.assertRegex(transaction_id(source, record), r"^txn_[0-9a-f]{32}$")
        with self.assertRaises(IdentityError) as error:
            AllocationLocator(1, (0,))
        self.assertEqual(error.exception.code, "identity_allocation_locator_invalid")
        with self.assertRaises(IdentityError):
            AllocationLocator(2, (1, 2, 3))
        with self.assertRaises(IdentityError):
            AllocationLocator(5, (1,))

    def test_extractor_contract_canonicalizes_json_and_excludes_only_closed_top_level_keys(
        self,
    ) -> None:
        profile = {
            "id": "cafe\u0301",
            "account": "Display account",
            "category": "Dining",
            "csv": {"columns": {"date": "Date"}, "fraction": 1.5},
            "array": [True, None, 2, "é"],
        }
        canonical = canonical_profile_json(profile)
        self.assertEqual(
            canonical,
            b'{"array":[true,null,2,"\xc3\xa9"],"csv":{"columns":{"date":"Date"},"fraction":1.5},"id":"caf\xc3\xa9"}',
        )
        excluded_changed = {**profile, "account": "Different", "category": "Other"}
        included_changed = {
            **profile,
            "csv": {"columns": {"date": "Posted"}, "fraction": 1.5},
        }
        self.assertEqual(
            extractor_contract_id(1, profile),
            extractor_contract_id(1, excluded_changed),
        )
        self.assertNotEqual(
            extractor_contract_id(1, profile),
            extractor_contract_id(1, included_changed),
        )
        with self.assertRaises(IdentityError):
            canonical_profile_json({"e\u0301": 1, "é": 2})

    def test_manifest_serialization_sorts_and_does_not_store_private_inputs(
        self,
    ) -> None:
        manifest = self._valid_manifest()
        source = manifest["sources"][0]
        origin = AllocationOrigin.from_manifest(
            source["records"][0]["allocation_origin"]
        )
        second_origin = AllocationOrigin(
            origin.source_revision,
            origin.extractor_contract_id,
            AllocationLocator(1, (3,)),
            1,
        )
        source["records"].append(
            ownership_record(
                source_id_value=source["source_id"],
                fingerprint="fp_" + "d" * 64,
                origin=second_origin,
            )
        )
        source["records"].reverse()
        document = manifest_document(manifest)
        self.assertTrue(document.endswith("\n"))
        self.assertNotIn("synthetic statement", document)
        self.assertNotIn("SYNTHETIC SHOP", document)
        self.assertNotIn("/Users/", document)
        parsed = parse_manifest(document)
        self.assertEqual(document, manifest_document(parsed))
        source = parsed["sources"][0]
        self.assertEqual(
            [record["source_record_id"] for record in source["records"]],
            sorted(record["source_record_id"] for record in source["records"]),
        )
        self.assertEqual(
            set(source),
            {
                "source_id",
                "source_namespace_id",
                "source_revision",
                "extractor_contract_id",
                "records",
            },
        )
        record = source["records"][0]
        self.assertEqual(
            ownership_exact_state_key(record),
            (AllocationLocator(1, (2,)), record["record_fingerprint"]),
        )

    def test_manifest_rejects_duplicate_locator_and_ownership(self) -> None:
        manifest = self._valid_manifest()
        source = manifest["sources"][0]
        original = source["records"][0]
        second_fingerprint = "fp_" + "b" * 64
        origin = AllocationOrigin.from_manifest(original["allocation_origin"])
        duplicate_locator = ownership_record(
            source_id_value=source["source_id"],
            fingerprint=second_fingerprint,
            origin=origin,
        )
        source["records"].append(duplicate_locator)
        with self.assertRaises(IdentityError) as error:
            validate_manifest(manifest, require_canonical_order=False)
        self.assertEqual(error.exception.code, "identity_manifest_invalid")

        duplicate_source_manifest = self._valid_manifest()
        duplicate_source = json.loads(
            json.dumps(duplicate_source_manifest["sources"][0])
        )
        duplicate_source_manifest["sources"].append(duplicate_source)
        with self.assertRaises(IdentityError):
            validate_manifest(duplicate_source_manifest, require_canonical_order=False)

    def test_manifest_rejects_derived_id_mismatch_and_noncanonical_text(self) -> None:
        manifest = self._valid_manifest()
        record = manifest["sources"][0]["records"][0]
        record["transaction_id"] = "txn_" + "0" * 32
        with self.assertRaises(IdentityError):
            validate_manifest(manifest)

        valid_document = manifest_document(self._valid_manifest())
        pretty = json.dumps(json.loads(valid_document), indent=2, sort_keys=True) + "\n"
        with self.assertRaises(IdentityError):
            parse_manifest(pretty)

    def test_retired_preserved_legacy_ownership_is_valid(self) -> None:
        namespace, source, revision, contract, origin = self._identity_values()
        record = ownership_record(
            source_id_value=source,
            fingerprint="fp_" + "c" * 64,
            origin=origin,
            state="retired",
            transaction_id_kind="preserved_legacy",
            preserved_transaction_id="txn_0123456789abcdef",
        )
        manifest = {
            "schema_version": 1,
            "sources": [
                source_ownership(
                    source_id_value=source,
                    namespace_id=namespace,
                    revision=revision,
                    contract_id=contract,
                    records=[record],
                )
            ],
        }
        validate_manifest(manifest)
        self.assertIsNone(record["current_locator"])
        self.assertEqual(empty_manifest(), {"schema_version": 1, "sources": []})


class RecordResolutionTest(unittest.TestCase):
    contract = "ext_" + "b" * 64

    def setUp(self) -> None:
        self.namespace = source_namespace_id("workspace", "synthetic.csv")
        self.source = source_id(self.namespace)
        self.first_revision = source_revision(b"first synthetic revision")
        self.second_revision = source_revision(b"second synthetic revision")

    def _assignment(self, revision: str | None = None) -> ResolvedSourceIdentity:
        return ResolvedSourceIdentity(
            "synthetic",
            "synthetic.csv",
            self.source,
            self.namespace,
            revision or self.first_revision,
            self.contract,
            "reused",
        )

    def _row(self, merchant: str = "SYNTHETIC SHOP") -> dict[str, str]:
        return {
            "account_id": "checking",
            "date": "2026-06-18",
            "transaction_date": "2026-06-18",
            "posting_date": "",
            "original_amount": "-12.00",
            "original_currency": "HKD",
            "posted_amount": "-12.00",
            "posted_currency": "HKD",
            "merchant": merchant,
            "original_description": merchant,
        }

    def _incoming(
        self, locator: int, merchant: str = "SYNTHETIC SHOP"
    ) -> IncomingRecordIdentity:
        return IncomingRecordIdentity(
            self._row(merchant), AllocationLocator(1, (locator,))
        )

    def _first_result(self, *incoming: IncomingRecordIdentity):
        return resolve_records(self._assignment(), incoming, None, [], "replace")

    def test_exact_state_is_a_no_op_and_validates_empty_sources(self) -> None:
        original = self._first_result(self._incoming(2))
        row = original.resolved_rows[0].row
        exact = resolve_records(
            self._assignment(),
            [self._incoming(2)],
            original.source_ownership,
            [row],
            "reset",
        )
        self.assertEqual(exact.resolved_rows[0].transaction_id, row["transaction_id"])
        self.assertEqual(exact.source_ownership, original.source_ownership)
        self.assertEqual(exact.reset_transaction_ids, (row["transaction_id"],))

        empty_source = source_ownership(
            source_id_value=self.source,
            namespace_id=self.namespace,
            revision=self.first_revision,
            contract_id=self.contract,
        )
        empty = resolve_records(self._assignment(), [], empty_source, [], "replace")
        self.assertEqual(empty.resolved_rows, ())
        self.assertEqual(empty.source_ownership, empty_source)
        with self.assertRaises(IdentityError) as raised:
            resolve_records(
                self._assignment(),
                [self._incoming(3)],
                original.source_ownership,
                [row],
                "replace",
            )
        self.assertEqual(raised.exception.code, "identity_exact_state_mismatch")

    def test_changed_revision_reuses_unique_rows_and_retires_unmatched(self) -> None:
        original = self._first_result(
            self._incoming(2, "ONE"), self._incoming(3, "TWO")
        )
        changed = resolve_records(
            self._assignment(self.second_revision),
            [self._incoming(8, "TWO")],
            original.source_ownership,
            [item.row for item in original.resolved_rows],
            "replace",
        )
        self.assertEqual(
            changed.resolved_rows[0].transaction_id,
            original.resolved_rows[1].transaction_id,
        )
        self.assertEqual(
            changed.retired_transaction_ids, (original.resolved_rows[0].transaction_id,)
        )
        states = {
            record["transaction_id"]: record["state"]
            for record in changed.source_ownership["records"]
        }
        self.assertEqual(states[original.resolved_rows[0].transaction_id], "retired")

    def test_repeated_changed_rows_are_ambiguous_without_position_tiebreak(
        self,
    ) -> None:
        original = self._first_result(self._incoming(2), self._incoming(3))
        with self.assertRaises(IdentityError) as raised:
            resolve_records(
                self._assignment(self.second_revision),
                [self._incoming(8)],
                original.source_ownership,
                [item.row for item in original.resolved_rows],
                "replace",
            )
        self.assertEqual(raised.exception.code, "identity_record_match_ambiguous")

    def test_retired_origin_reactivates_but_a_future_same_fingerprint_is_new(
        self,
    ) -> None:
        original = self._first_result(self._incoming(2))
        retired = resolve_records(
            self._assignment(self.second_revision),
            [],
            original.source_ownership,
            [item.row for item in original.resolved_rows],
            "replace",
        )
        recurrence = resolve_records(
            self._assignment(),
            [self._incoming(2)],
            retired.source_ownership,
            [],
            "replace",
        )
        self.assertEqual(
            recurrence.resolved_rows[0].transaction_id,
            original.resolved_rows[0].transaction_id,
        )
        future = resolve_records(
            self._assignment(source_revision(b"third synthetic revision")),
            [self._incoming(9)],
            retired.source_ownership,
            [],
            "replace",
        )
        self.assertNotEqual(
            future.resolved_rows[0].transaction_id,
            original.resolved_rows[0].transaction_id,
        )

    def test_new_occurrence_ordinals_follow_locator_not_input_order(self) -> None:
        result = self._first_result(self._incoming(9), self._incoming(2))
        records = result.source_ownership["records"]
        by_locator = {
            tuple(record["current_locator"]["components"]): record for record in records
        }
        self.assertEqual(by_locator[(2,)]["allocation_origin"]["occurrence_ordinal"], 1)
        self.assertEqual(by_locator[(9,)]["allocation_origin"]["occurrence_ordinal"], 2)

    def test_unique_legacy_migrates_and_duplicate_legacy_stays_protected(self) -> None:
        legacy = {**self._row(), "transaction_id": "txn_0123456789abcdef"}
        migrated = resolve_records(
            self._assignment(), [self._incoming(2)], None, [legacy], "replace"
        )
        self.assertEqual(
            migrated.resolved_rows[0].transaction_id, legacy["transaction_id"]
        )
        self.assertEqual(
            migrated.source_ownership["records"][0]["transaction_id_kind"],
            "preserved_legacy",
        )
        duplicate = [legacy, dict(legacy)]
        protected = resolve_records(
            self._assignment(), [self._incoming(2)], None, duplicate, "ordinary"
        )
        self.assertEqual(len(protected.retained_legacy_rows), 2)
        self.assertIsInstance(protected.diagnostics[0], RecordResolutionDiagnostic)
        self.assertEqual(
            protected.diagnostics[0].code, "identity_legacy_transaction_id_ambiguous"
        )
        with self.assertRaises(IdentityError) as raised:
            resolve_records(
                self._assignment(), [self._incoming(2)], None, duplicate, "reset"
            )
        self.assertEqual(
            raised.exception.code, "identity_legacy_transaction_id_ambiguous"
        )

    def test_row_state_manifest_agreement_and_collision_fail_without_data_leaks(
        self,
    ) -> None:
        partial = {**self._row(), "source_id": self.source}
        with self.assertRaises(IdentityError) as raised:
            resolve_records(
                self._assignment(), [self._incoming(2)], None, [partial], "replace"
            )
        self.assertEqual(raised.exception.code, "identity_partial_v2_metadata")

        original = self._first_result(self._incoming(2))
        corrupt = dict(original.resolved_rows[0].row)
        corrupt["transaction_id"] = "txn_" + "0" * 32
        with self.assertRaises(IdentityError) as raised:
            resolve_records(
                self._assignment(),
                [self._incoming(2)],
                original.source_ownership,
                [corrupt],
                "replace",
            )
        self.assertEqual(raised.exception.code, "identity_manifest_invalid")

        def collide(_source: str, _origin: AllocationOrigin, _fingerprint: str) -> str:
            return "rec_" + "0" * 64

        with self.assertRaises(IdentityError) as raised:
            resolve_records(
                self._assignment(),
                [self._incoming(2, "ONE"), self._incoming(3, "TWO")],
                None,
                [],
                "replace",
                record_id_factory=collide,
            )
        self.assertEqual(raised.exception.code, "identity_hash_conflict")
        self.assertNotIn("SYNTHETIC SHOP", str(raised.exception))

    def test_reset_includes_active_and_retired_ids_and_is_order_independent(
        self,
    ) -> None:
        original = self._first_result(
            self._incoming(2, "ONE"), self._incoming(3, "TWO")
        )
        retired = resolve_records(
            self._assignment(self.second_revision),
            [self._incoming(8, "TWO")],
            original.source_ownership,
            [item.row for item in original.resolved_rows],
            "replace",
        )
        reset = resolve_records(
            self._assignment(self.second_revision),
            [self._incoming(8, "TWO")],
            retired.source_ownership,
            [retired.resolved_rows[0].row],
            "reset",
        )
        self.assertEqual(
            reset.reset_transaction_ids,
            tuple(sorted(item.transaction_id for item in original.resolved_rows)),
        )

        first = self._incoming(9)
        second = self._incoming(2)
        forward = resolve_records(
            self._assignment(), [first, second], None, [], "replace"
        )
        reverse = resolve_records(
            self._assignment(), [second, first], None, [], "replace"
        )
        self.assertEqual(forward.source_ownership, reverse.source_ownership)
        self.assertEqual(first.row, self._row())
        self.assertEqual(second.row, self._row())


class BatchIdentityResolutionTest(unittest.TestCase):
    contract = "ext_" + "c" * 64

    def _row(self, merchant: str = "SYNTHETIC SHOP") -> dict[str, object]:
        return {
            "account_id": "checking",
            "date": "2026-06-18",
            "transaction_date": "2026-06-18",
            "posting_date": "",
            "original_amount": "-12.00",
            "original_currency": "HKD",
            "posted_amount": "-12.00",
            "posted_currency": "HKD",
            "merchant": merchant,
            "original_description": merchant,
        }

    def _source(
        self,
        handle: str,
        display: str,
        locator: str,
        revision: bytes,
        *records: tuple[int, str],
    ) -> IncomingSourceIdentity:
        return IncomingSourceIdentity(
            handle,
            display,
            source_namespace_id("workspace", locator),
            source_revision(revision),
            self.contract,
            tuple(
                IncomingRecordIdentity(
                    self._row(merchant), AllocationLocator(1, (line,))
                )
                for line, merchant in records
            ),
        )

    def _ledger(self, result: IdentityResolution) -> list[dict[str, object]]:
        return [
            dict(row) for row in (*result.retained_ledger_rows, *result.resolved_rows)
        ]

    def test_identical_sources_match_separate_and_batch_imports(self) -> None:
        first = self._source("a", "a.csv", "a.csv", b"same", (2, "SAME"))
        second = self._source("b", "b.csv", "b.csv", b"same", (2, "SAME"))
        separate_first = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[first],
            intent="ordinary",
        )
        separate_second = resolve_batch(
            ledger_rows=self._ledger(separate_first),
            manifest=separate_first.next_manifest,
            sources=[second],
            intent="ordinary",
        )
        together = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[first, second],
            intent="ordinary",
        )
        separate_ids = {
            row["source_id"]: row["transaction_id"]
            for row in (*separate_first.resolved_rows, *separate_second.resolved_rows)
        }
        together_ids = {
            row["source_id"]: row["transaction_id"] for row in together.resolved_rows
        }
        self.assertEqual(separate_ids, together_ids)
        self.assertEqual(len(together_ids), 2)

    def test_zero_row_source_is_persisted_and_then_already_imported(self) -> None:
        source = self._source("empty", "empty.csv", "empty.csv", b"empty")
        first = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[source],
            intent="ordinary",
        )
        self.assertEqual(first.resolved_rows, ())
        self.assertEqual(len(first.next_manifest["sources"]), 1)
        with self.assertRaises(IdentityError) as raised:
            resolve_batch(
                ledger_rows=[],
                manifest=first.next_manifest,
                sources=[source],
                intent="ordinary",
            )
        self.assertEqual(raised.exception.code, "identity_source_already_imported")

    def test_replace_two_sources_and_rename_keep_their_ownership(self) -> None:
        first = self._source("a", "a.csv", "a.csv", b"old-a", (2, "A"))
        second = self._source("b", "b.csv", "b.csv", b"old-b", (2, "B"))
        original = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[first, second],
            intent="ordinary",
        )
        changed_first = self._source("a", "a.csv", "a.csv", b"new-a", (8, "A"))
        changed_second = self._source("b", "b.csv", "b.csv", b"new-b", (8, "B"))
        replaced = resolve_batch(
            ledger_rows=self._ledger(original),
            manifest=original.next_manifest,
            sources=[changed_first, changed_second],
            intent="replace",
        )
        self.assertEqual(len(replaced.replaced_source_ids), 2)
        self.assertEqual(replaced.retained_ledger_rows, ())
        self.assertEqual(
            {row["transaction_id"] for row in replaced.resolved_rows},
            {row["transaction_id"] for row in original.resolved_rows},
        )

        renamed = self._source("a", "renamed.csv", "renamed.csv", b"new-a", (8, "A"))
        rename_result = resolve_batch(
            ledger_rows=self._ledger(replaced),
            manifest=replaced.next_manifest,
            sources=[renamed],
            intent="replace",
        )
        self.assertEqual(
            rename_result.resolved_rows[0]["transaction_id"],
            next(
                row["transaction_id"]
                for row in replaced.resolved_rows
                if row["merchant"] == "A"
            ),
        )
        self.assertEqual(len(rename_result.retained_ledger_rows), 1)

    def test_unrelated_legacy_rows_pass_through_without_a_warning(self) -> None:
        legacy = {
            **self._row("OLD"),
            "transaction_id": "txn_0123456789abcdef",
            "source_file": "old.csv",
        }
        source = self._source("new", "new.csv", "new.csv", b"new", (2, "NEW"))
        result = resolve_batch(
            ledger_rows=[legacy],
            manifest=empty_manifest(),
            sources=[source],
            intent="ordinary",
        )
        self.assertEqual(result.retained_ledger_rows, (legacy,))
        self.assertEqual(result.diagnostics, ())

    def test_targeted_legacy_migration_and_ambiguity_protection(self) -> None:
        legacy = {
            **self._row("OLD"),
            "transaction_id": "txn_0123456789abcdef",
            "source_file": "old.csv",
        }
        source = self._source("old", "old.csv", "old.csv", b"old", (2, "OLD"))
        migrated = resolve_batch(
            ledger_rows=[legacy],
            manifest=empty_manifest(),
            sources=[source],
            intent="replace",
        )
        self.assertEqual(
            migrated.resolved_rows[0]["transaction_id"], legacy["transaction_id"]
        )
        self.assertEqual(migrated.retained_ledger_rows, ())

        duplicate = [
            {**legacy, "flags": "first;second", "needs_review": "false"},
            {
                **legacy,
                "flags": "first;identity_migration_ambiguous;second",
                "needs_review": "false",
            },
        ]
        ordinary = resolve_batch(
            ledger_rows=duplicate,
            manifest=empty_manifest(),
            sources=[source],
            intent="ordinary",
        )
        self.assertEqual(len(ordinary.retained_ledger_rows), 2)
        self.assertTrue(
            all(row["needs_review"] == "true" for row in ordinary.retained_ledger_rows)
        )
        self.assertEqual(
            [row["flags"] for row in ordinary.retained_ledger_rows],
            [
                "first;second;identity_migration_ambiguous",
                "first;identity_migration_ambiguous;second",
            ],
        )
        self.assertEqual(
            ordinary.diagnostics[0].code, "identity_legacy_transaction_id_ambiguous"
        )
        with self.assertRaises(IdentityError) as raised:
            resolve_batch(
                ledger_rows=duplicate,
                manifest=empty_manifest(),
                sources=[source],
                intent="reset",
            )
        self.assertEqual(
            raised.exception.code, "identity_legacy_transaction_id_ambiguous"
        )

    def test_global_validation_reset_retired_ids_and_input_non_mutation(self) -> None:
        first = self._source(
            "source", "source.csv", "source.csv", b"one", (2, "ONE"), (3, "TWO")
        )
        original = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[first],
            intent="ordinary",
        )
        changed = self._source("source", "source.csv", "source.csv", b"two", (8, "TWO"))
        replacement = resolve_batch(
            ledger_rows=self._ledger(original),
            manifest=original.next_manifest,
            sources=[changed],
            intent="replace",
        )
        reset = resolve_batch(
            ledger_rows=self._ledger(replacement),
            manifest=replacement.next_manifest,
            sources=[changed],
            intent="reset",
        )
        self.assertEqual(
            reset.reset_transaction_ids,
            tuple(sorted(row["transaction_id"] for row in original.resolved_rows)),
        )

        corrupt = self._ledger(original)
        corrupt[0]["source_revision"] = source_revision(b"wrong")
        before_manifest = json.loads(json.dumps(original.next_manifest))
        with self.assertRaises(IdentityError) as raised:
            resolve_batch(
                ledger_rows=corrupt,
                manifest=original.next_manifest,
                sources=[changed],
                intent="replace",
            )
        self.assertEqual(raised.exception.code, "identity_manifest_invalid")
        self.assertEqual(original.next_manifest, before_manifest)

    def test_partial_metadata_order_independence_and_collision_validation(self) -> None:
        partial = {
            **self._row(),
            "transaction_id": "txn_0123456789abcdef",
            "source_id": "src_" + "0" * 64,
        }
        source_a = self._source("a", "a.csv", "a.csv", b"a", (2, "A"))
        source_b = self._source("b", "b.csv", "b.csv", b"b", (2, "B"))
        with self.assertRaises(IdentityError) as raised:
            resolve_batch(
                ledger_rows=[partial],
                manifest=empty_manifest(),
                sources=[source_a],
                intent="ordinary",
            )
        self.assertEqual(raised.exception.code, "identity_partial_v2_metadata")

        forward = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[source_a, source_b],
            intent="ordinary",
        )
        reverse = resolve_batch(
            ledger_rows=[],
            manifest=empty_manifest(),
            sources=[source_b, source_a],
            intent="ordinary",
        )
        self.assertEqual(forward.next_manifest, reverse.next_manifest)
        self.assertEqual(forward.resolved_rows, reverse.resolved_rows)

        ledger_before = []
        source_rows_before = [
            [dict(record.row) for record in source.record_data]
            for source in (source_a, source_b)
        ]
        resolve_batch(
            ledger_rows=ledger_before,
            manifest=empty_manifest(),
            sources=[source_a, source_b],
            intent="ordinary",
        )
        self.assertEqual(ledger_before, [])
        self.assertEqual(
            [
                [dict(record.row) for record in source.record_data]
                for source in (source_a, source_b)
            ],
            source_rows_before,
        )

        with patch(
            "honeymoney.identity.transaction_id", return_value="txn_" + "0" * 32
        ):
            with self.assertRaises(IdentityError) as collided:
                resolve_batch(
                    ledger_rows=[],
                    manifest=empty_manifest(),
                    sources=[source_a, source_b],
                    intent="ordinary",
                )
        self.assertEqual(collided.exception.code, "identity_hash_conflict")


class SourceResolutionTest(unittest.TestCase):
    contract = "ext_" + "a" * 64

    def _incoming(
        self,
        handle: str,
        display: str,
        namespace_name: str,
        revision_bytes: bytes,
        *,
        record_data: object | None = None,
    ) -> IncomingSourceIdentity:
        return IncomingSourceIdentity(
            stable_handle=handle,
            source_display=display,
            namespace_id=source_namespace_id("workspace", namespace_name),
            revision=source_revision(revision_bytes),
            contract_id=self.contract,
            record_data=record_data,
        )

    def _source(
        self,
        namespace_name: str,
        revision_bytes: bytes,
        *,
        source_id_value: str | None = None,
    ) -> dict[str, object]:
        namespace = source_namespace_id("workspace", namespace_name)
        return source_ownership(
            source_id_value=source_id_value or source_id(namespace),
            namespace_id=namespace,
            revision=source_revision(revision_bytes),
            contract_id=self.contract,
        )

    def _manifest(self, *sources: dict[str, object]) -> dict[str, object]:
        return {"schema_version": 1, "sources": list(sources)}

    def _error(
        self,
        manifest: dict[str, object],
        incoming: list[IncomingSourceIdentity],
        intent: str,
        code: str,
        *,
        legacy_rows: list[dict[str, object]] | None = None,
    ) -> IdentityError:
        with self.assertRaises(IdentityError) as raised:
            resolve_sources(manifest, legacy_rows or [], incoming, intent)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_source_table(self) -> None:
        prior = self._source("old.csv", b"same")
        exact = self._incoming("exact", "old.csv", "old.csv", b"changed")
        manifest = self._manifest(prior)
        self._error(
            manifest,
            [exact],
            "import",
            "identity_source_already_imported",
        )
        for action in ("replace", "reset"):
            with self.subTest(action=action, row="exact namespace"):
                result = resolve_sources(manifest, [], [exact], action)
                [assignment] = result.assignments
                self.assertEqual(assignment.source_id, prior["source_id"])
                self.assertEqual(assignment.disposition, "reused")

        duplicate_namespace = source_namespace_id("workspace", "duplicate.csv")
        ambiguous_manifest = self._manifest(
            self._source("duplicate.csv", b"one", source_id_value="src_" + "1" * 64),
            self._source("duplicate.csv", b"two", source_id_value="src_" + "2" * 64),
        )
        self._error(
            ambiguous_manifest,
            [
                IncomingSourceIdentity(
                    "duplicate",
                    "duplicate.csv",
                    duplicate_namespace,
                    source_revision(b"new"),
                    self.contract,
                )
            ],
            "replace",
            "identity_source_namespace_ambiguous",
        )

        copied = self._incoming("copy", "copy.csv", "copy.csv", b"same")
        copy_result = resolve_sources(manifest, [], [copied], "import")
        [copy_assignment] = copy_result.assignments
        self.assertEqual(copy_assignment.source_id, source_id(copied.namespace_id))
        self.assertEqual(copy_assignment.disposition, "new")

        renamed = self._incoming("rename", "renamed.csv", "renamed.csv", b"same")
        renamed_result = resolve_sources(manifest, [], [renamed], "replace")
        [renamed_assignment] = renamed_result.assignments
        self.assertEqual(renamed_assignment.source_id, prior["source_id"])
        self.assertEqual(renamed_assignment.disposition, "reused")

        missing = self._incoming("missing", "missing.csv", "missing.csv", b"new")
        self._error(
            manifest,
            [missing],
            "reset",
            "identity_source_target_not_found",
        )

        same_revision_manifest = self._manifest(
            prior,
            self._source("another.csv", b"same"),
        )
        self._error(
            same_revision_manifest,
            [renamed],
            "replace",
            "identity_source_revision_ambiguous",
        )

    def test_two_incoming_sources_cannot_claim_one_prior_source(self) -> None:
        manifest = self._manifest(self._source("old.csv", b"same"))
        first = self._incoming("first", "first.csv", "first.csv", b"same")
        second = self._incoming("second", "second.csv", "second.csv", b"same")
        self._error(
            manifest,
            [first, second],
            "replace",
            "identity_source_revision_ambiguous",
        )

    def test_same_new_namespace_fails_before_duplicate_source_allocation(self) -> None:
        private_locator = "/private/same-source.csv"
        namespace = source_namespace_id("external", private_locator)
        first = IncomingSourceIdentity(
            "first",
            "first.csv",
            namespace,
            source_revision(b"first private revision"),
            self.contract,
            record_data={"raw": "first private record"},
        )
        second = IncomingSourceIdentity(
            "second",
            "second.csv",
            namespace,
            source_revision(b"second private revision"),
            self.contract,
            record_data={"raw": "second private record"},
        )
        manifest = self._manifest()
        before_manifest = json.loads(json.dumps(manifest))

        forward = self._error(
            manifest,
            [first, second],
            "import",
            "identity_source_namespace_ambiguous",
        )
        reverse = self._error(
            manifest,
            [second, first],
            "import",
            "identity_source_namespace_ambiguous",
        )
        self.assertEqual(forward.diagnostic, reverse.diagnostic)
        diagnostic = forward.diagnostic
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidate_count, 2)
        rendered = str(forward) + repr(diagnostic)
        self.assertNotIn(private_locator, rendered)
        self.assertNotIn("private revision", rendered)
        self.assertNotIn("private record", rendered)
        self.assertEqual(manifest, before_manifest)

    def test_legacy_source_claims_are_unique_and_action_aware(self) -> None:
        incoming = self._incoming("legacy", "cafe\u0301.csv", "new.csv", b"same")
        legacy_rows = [
            {
                "source_file": "café.csv",
                "source_id": "",
                "source_namespace_id": "",
                "source_revision": "",
                "source_record_id": "",
            }
        ]
        result = resolve_sources(self._manifest(), legacy_rows, [incoming], "replace")
        [assignment] = result.assignments
        self.assertEqual(assignment.source_id, source_id(incoming.namespace_id))
        self.assertEqual(assignment.disposition, "legacy")
        self._error(
            self._manifest(),
            [incoming],
            "import",
            "identity_source_already_imported",
            legacy_rows=legacy_rows,
        )

        competing = self._incoming("other", "café.csv", "other.csv", b"same")
        self._error(
            self._manifest(),
            [incoming, competing],
            "reset",
            "identity_legacy_source_ambiguous",
            legacy_rows=legacy_rows,
        )

    def test_legacy_and_v2_namespace_claims_compete_batch_wide(self) -> None:
        private_locator = "/private/existing-source.csv"
        namespace = source_namespace_id("external", private_locator)
        prior = source_ownership(
            source_id_value=source_id(namespace),
            namespace_id=namespace,
            revision=source_revision(b"old private revision"),
            contract_id=self.contract,
        )
        manifest = self._manifest(prior)
        legacy_rows = [{"source_file": "legacy.csv"}]
        legacy = IncomingSourceIdentity(
            "legacy",
            "legacy.csv",
            namespace,
            source_revision(b"new private revision"),
            self.contract,
            record_data={"raw": "legacy private record"},
        )
        direct = self._error(
            manifest,
            [legacy],
            "replace",
            "identity_source_namespace_ambiguous",
            legacy_rows=legacy_rows,
        )
        direct_diagnostic = direct.diagnostic
        assert direct_diagnostic is not None
        self.assertEqual(direct_diagnostic.candidate_count, 2)

        exact_v2 = IncomingSourceIdentity(
            "v2",
            "v2.csv",
            namespace,
            source_revision(b"other private revision"),
            self.contract,
        )
        before_manifest = json.loads(json.dumps(manifest))
        before_legacy = json.loads(json.dumps(legacy_rows))
        forward = self._error(
            manifest,
            [legacy, exact_v2],
            "replace",
            "identity_source_namespace_ambiguous",
            legacy_rows=legacy_rows,
        )
        reverse = self._error(
            manifest,
            [exact_v2, legacy],
            "replace",
            "identity_source_namespace_ambiguous",
            legacy_rows=legacy_rows,
        )
        self.assertEqual(forward.diagnostic, reverse.diagnostic)
        diagnostic = forward.diagnostic
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidate_count, 2)
        rendered = str(forward) + repr(diagnostic)
        self.assertNotIn(private_locator, rendered)
        self.assertNotIn("private revision", rendered)
        self.assertNotIn("private record", rendered)
        self.assertEqual(manifest, before_manifest)
        self.assertEqual(legacy_rows, before_legacy)

    def test_resolution_is_order_independent_and_does_not_mutate_inputs(self) -> None:
        prior = self._source("old.csv", b"same")
        second_prior = self._source("new.csv", b"older")
        manifest = self._manifest(prior, second_prior)
        legacy_rows = [{"source_file": "unrelated.csv"}]
        first = self._incoming("b", "renamed.csv", "renamed.csv", b"same")
        second = self._incoming("a", "new.csv", "new.csv", b"other")
        before_manifest = json.loads(json.dumps(manifest))
        before_legacy = json.loads(json.dumps(legacy_rows))
        before_sources = (first, second)

        forward = resolve_sources(manifest, legacy_rows, [first, second], "replace")
        reverse = resolve_sources(manifest, legacy_rows, [second, first], "replace")
        self.assertEqual(forward, reverse)
        self.assertEqual(
            {item.stable_handle: item.source_id for item in forward.assignments},
            {"a": second_prior["source_id"], "b": prior["source_id"]},
        )
        self.assertEqual(manifest, before_manifest)
        self.assertEqual(legacy_rows, before_legacy)
        self.assertEqual((first, second), before_sources)

    def test_resolution_errors_keep_locator_revision_and_record_data_private(
        self,
    ) -> None:
        private_locator = "/private/secret-statement.csv"
        private_bytes = b"secret statement bytes"
        incoming = IncomingSourceIdentity(
            stable_handle="private",
            source_display="statement.csv",
            namespace_id=source_namespace_id("external", private_locator),
            revision=source_revision(private_bytes),
            contract_id=self.contract,
            record_data={"merchant": "secret merchant"},
        )
        error = self._error(
            self._manifest(),
            [incoming],
            "replace",
            "identity_source_target_not_found",
        )
        self.assertEqual(str(error), "identity_source_target_not_found")
        self.assertIsNotNone(error.diagnostic)
        diagnostic = error.diagnostic
        assert diagnostic is not None
        self.assertEqual(
            set(vars(diagnostic)),
            {"code", "source_display", "action", "candidate_count", "remediation"},
        )
        rendered = repr(diagnostic) + str(error)
        self.assertNotIn(private_locator, rendered)
        self.assertNotIn(private_bytes.decode(), rendered)
        self.assertNotIn("secret merchant", rendered)
        self.assertEqual(diagnostic.source_display, "statement.csv")
        self.assertEqual(diagnostic.action, "replace")
        self.assertEqual(diagnostic.candidate_count, 0)
        self.assertTrue(diagnostic.remediation)


if __name__ == "__main__":
    unittest.main()
