from __future__ import annotations

import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import honeymoney.corrections as corrections_module
import honeymoney.identity as identity_module
import honeymoney.overlap as overlap_module
from honeymoney.corrections import (
    apply_correction_operation,
    apply_corrections,
    ledger_output_documents,
    load_corrections,
    prepare_corrections_document,
    review_state_correction_updates,
    to_review_row,
    validate_correction,
)
from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    IdentityError,
    IncomingRecordIdentity,
    IncomingSourceIdentity,
    ResolvedSourceIdentity,
    ambiguous_legacy_transaction_ids,
    canonical_profile_json,
    empty_manifest,
    extractor_contract_id,
    has_stable_v2_identity,
    logical_locator_from_resolved,
    manifest_document,
    normalized_decimal,
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
    source_revision,
    validate_ledger_manifest_agreement,
    validate_manifest,
)
from honeymoney.identity_state import IdentityState
from honeymoney.manual_pairs import (
    MANUAL_PAIR_FIELD,
    manual_pair_id,
    manual_pair_marker,
)
from honeymoney.overlap import (
    DuplicateResolutionError,
    apply_history_ambiguity,
    canonicalize_overlaps,
    clear_history_ambiguity,
    empty_overlap_manifest,
    enforce_overlap_review,
    list_duplicate_groups,
    overlap_manifest_document,
    parse_overlap_manifest,
    project_corrections,
    project_migration_corrections,
    project_replacement_corrections,
    release_overlap_review_ownership,
    resolve_duplicate_group,
    source_occurrences_path,
    validate_overlap_agreement,
)
from honeymoney.review_state import REVIEW_REASON_IDENTITY
from honeymoney.schema import CATEGORIZED_COLUMNS

_NAMESPACE_KEY = "ovns_" + "a" * 64


def _hex(character: str, length: int) -> str:
    return character * length


def _source_row(
    transaction_character: str,
    source_character: str,
    *,
    merchant: str = "SYNTHETIC COVERAGE SHOP",
    amount_hkd: str = "-10.00",
    needs_review: str = "false",
    flags: str = "",
    reason: str = "",
) -> dict[str, str]:
    """Return one complete synthetic source-occurrence row."""
    row = {column: "" for column in CATEGORIZED_COLUMNS}
    row.update(
        {
            "transaction_id": "txn_" + _hex(transaction_character, 32),
            "source_id": "src_" + _hex(source_character, 64),
            "source_namespace_id": "ns_" + _hex(source_character, 64),
            "source_revision": "rev_" + _hex(source_character, 64),
            "source_record_id": "rec_" + _hex(transaction_character, 64),
            "date": "2026-08-01",
            "transaction_date": "2026-08-01",
            "posting_date": "",
            "account_id": "synthetic-checking",
            "account": "Synthetic checking",
            "account_type": "bank",
            "institution": "Synthetic Bank",
            "country": "HK",
            "original_amount": "-10.00",
            "original_currency": "HKD",
            "posted_amount": "-10.00",
            "posted_currency": "HKD",
            "amount_hkd": amount_hkd,
            "valuation_source": "statement_posted",
            "valuation_status": "actual",
            "merchant": merchant,
            "original_description": merchant,
            "category": "Dining",
            "flow_type": "expense",
            "flow_source": "deterministic",
            "owner": "Household",
            "payment_method": "Bank Account",
            "confidence": "1.00",
            "needs_review": needs_review,
            "review_reasons": "",
            "flags": flags,
            "reason": reason,
            "source_file": f"synthetic-{source_character}.csv",
            "source_page": "1",
            "source_row": "2",
        }
    )
    return row


def _incoming_record(index: int = 1) -> IncomingRecordIdentity:
    row = _source_row("1", "a")
    for field in (
        "transaction_id",
        "source_id",
        "source_namespace_id",
        "source_revision",
        "source_record_id",
    ):
        row[field] = ""
    return IncomingRecordIdentity(row, AllocationLocator(1, (index,)))


def _incoming_source(
    handle: str,
    display: str,
    tag: str,
    *,
    record_data: object | None = (),
    namespace: str | None = None,
    revision: str | None = None,
    contract: str | None = None,
) -> IncomingSourceIdentity:
    return IncomingSourceIdentity(
        handle,
        display,
        namespace or source_namespace_id("workspace", f"statements/{tag}.csv"),
        revision or source_revision(f"synthetic {tag}\n".encode()),
        contract or extractor_contract_id(1, {"id": f"synthetic-{tag}"}),
        record_data,
    )


def _identity_row_and_manifest(
    tag: str = "one",
) -> tuple[dict[str, str], dict[str, object]]:
    row = _source_row("1", "a")
    namespace = source_namespace_id("workspace", f"statements/{tag}.csv")
    identifier = source_id(namespace)
    revision = source_revision(f"synthetic statement {tag}\n".encode())
    contract = extractor_contract_id(1, {"id": f"synthetic-{tag}"})
    origin = AllocationOrigin(revision, contract, AllocationLocator(1, (1,)), 1)
    record = ownership_record(
        source_id_value=identifier,
        fingerprint=record_fingerprint(row),
        origin=origin,
    )
    row.update(
        {
            "transaction_id": record["transaction_id"],
            "source_id": identifier,
            "source_namespace_id": namespace,
            "source_revision": revision,
            "source_record_id": record["source_record_id"],
        }
    )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "sources": [
            source_ownership(
                source_id_value=identifier,
                namespace_id=namespace,
                revision=revision,
                contract_id=contract,
                records=[record],
            )
        ],
    }
    return row, manifest


class IdentityBoundaryCoverageTest(unittest.TestCase):
    def test_identity_primitives_reject_bad_inputs_and_normalize_json_edges(
        self,
    ) -> None:
        with self.assertRaises(IdentityError):
            source_namespace_id("unknown", "synthetic.csv")
        with self.assertRaises(TypeError):
            source_revision("synthetic")  # type: ignore[arg-type]
        with self.assertRaises(IdentityError):
            logical_locator_from_resolved(Path("relative.csv"), Path.cwd())

        for value in ("not-a-number", "NaN", "Infinity"):
            with self.subTest(decimal=value), self.assertRaises(IdentityError):
                normalized_decimal(value)

        profile = {
            "payload": [
                None,
                True,
                False,
                7,
                0.0,
                0.0000001,
                1e22,
                Decimal("1.2300"),
            ]
        }
        self.assertEqual(
            canonical_profile_json(profile),
            b'{"payload":[null,true,false,7,0,1e-7,1e+22,1.23]}',
        )
        for invalid_profile in (
            [],
            {"value": float("nan")},
            {"value": Decimal("NaN")},
            {"value": {1: "not text"}},
            {"e\u0301": 1, "é": 2},
            {"value": object()},
        ):
            with (
                self.subTest(invalid_profile=repr(invalid_profile)),
                self.assertRaises(IdentityError),
            ):
                canonical_profile_json(invalid_profile)  # type: ignore[arg-type]

        self.assertEqual(identity_module._scientific_number("1.0e-7"), "1e-7")
        with self.assertRaises(IdentityError):
            identity_module._canonical_json({1: "not a text key"})
        with self.assertRaises(IdentityError):
            identity_module._json_object_without_duplicates([("key", 1), ("key", 2)])
        with self.assertRaises(IdentityError):
            extractor_contract_id(99, {"id": "synthetic"})

    def test_source_and_record_resolvers_validate_structural_inputs(self) -> None:
        first = _incoming_source("first", "first.csv", "first")
        with self.assertRaises(IdentityError):
            resolve_sources(empty_manifest(), [], ["not a source"], "import")  # type: ignore[list-item]
        with self.assertRaises(IdentityError):
            resolve_sources(empty_manifest(), [], [first, first], "import")

        shared_source_id = "src_" + "a" * 64
        first_namespace = source_namespace_id("workspace", "statements/first.csv")
        second_namespace = source_namespace_id("workspace", "statements/second.csv")
        revision = source_revision(b"synthetic source\n")
        contract = extractor_contract_id(1, {"id": "synthetic"})
        malformed_candidate_manifest = {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": shared_source_id,
                    "source_namespace_id": first_namespace,
                    "source_revision": revision,
                    "extractor_contract_id": contract,
                    "records": [],
                },
                {
                    "source_id": shared_source_id,
                    "source_namespace_id": second_namespace,
                    "source_revision": revision,
                    "extractor_contract_id": contract,
                    "records": [],
                },
            ],
        }
        incoming = [
            _incoming_source(
                "first",
                "first.csv",
                "first",
                namespace=first_namespace,
                revision=revision,
                contract=contract,
            ),
            _incoming_source(
                "second",
                "second.csv",
                "second",
                namespace=second_namespace,
                revision=revision,
                contract=contract,
            ),
        ]
        with self.assertRaisesRegex(
            IdentityError, "identity_source_namespace_ambiguous"
        ):
            resolve_sources(malformed_candidate_manifest, [], incoming, "replace")

        assignment = ResolvedSourceIdentity(
            "one",
            first.source_display,
            source_id(first.namespace_id),
            first.namespace_id,
            first.revision,
            first.contract_id,
            "new",
        )
        cases: list[tuple[str, dict[str, object]]] = [
            ("bad assignment", {"assignment": "not an assignment"}),
            ("bad record", {"incoming_records": ["not a record"]}),
            ("bad record factory", {"record_id_factory": "not callable"}),
            ("bad transaction factory", {"transaction_id_factory": "not callable"}),
            ("bad reallocation flag", {"allow_unmatched_reallocation": "true"}),
            (
                "bad parser upgrade flag",
                {"allow_parser_upgrade_reallocation": "true"},
            ),
            ("bad prior row", {"prior_rows": ["not a mapping"]}),
        ]
        for label, overrides in cases:
            arguments: dict[str, object] = {
                "assignment": assignment,
                "incoming_records": [_incoming_record()],
                "prior_source": None,
                "prior_rows": [],
                "intent": "import",
            }
            arguments.update(overrides)
            with self.subTest(label=label), self.assertRaises(IdentityError):
                resolve_records(**arguments)  # type: ignore[arg-type]

    def test_batch_and_manifest_boundaries_fail_closed(self) -> None:
        source = _incoming_source("one", "one.csv", "one", record_data=None)
        for label, arguments in (
            (
                "bad ledger row",
                {"ledger_rows": ["not a mapping"], "sources": []},
            ),
            (
                "bad unmatched flag",
                {
                    "ledger_rows": [],
                    "sources": [],
                    "allow_unmatched_reallocation": "true",
                },
            ),
            (
                "bad parser upgrade flag",
                {
                    "ledger_rows": [],
                    "sources": [],
                    "allow_parser_upgrade_reallocation": "true",
                },
            ),
            (
                "source records are not a tuple",
                {"ledger_rows": [], "sources": [source]},
            ),
        ):
            with self.subTest(label=label), self.assertRaises(IdentityError):
                resolve_batch(
                    manifest=empty_manifest(),
                    intent="import",
                    **arguments,  # type: ignore[arg-type]
                )

        row, manifest = _identity_row_and_manifest()
        validate_ledger_manifest_agreement([row], manifest)
        with self.assertRaises(IdentityError):
            validate_ledger_manifest_agreement(["not a row"], manifest)  # type: ignore[list-item]
        with self.assertRaises(IdentityError):
            validate_ledger_manifest_agreement([], manifest)
        with self.assertRaises(IdentityError):
            validate_ledger_manifest_agreement([row, row], manifest)
        legacy = {field: "" for field in identity_module.ID_FIELDS}
        legacy["transaction_id"] = "not-a-legacy-id"
        with self.assertRaises(IdentityError):
            validate_ledger_manifest_agreement([legacy], empty_manifest())
        self.assertFalse(has_stable_v2_identity("not a row"))  # type: ignore[arg-type]
        self.assertEqual(
            ambiguous_legacy_transaction_ids(
                [
                    {"transaction_id": "txn_0123456789abcdef"},
                    {"transaction_id": "txn_0123456789abcdef"},
                    row,
                ]
            ),
            frozenset({"txn_0123456789abcdef"}),
        )

        canonical = manifest_document(manifest)
        for document in (b"\xff", 3, "\ufeff" + canonical, "[]", canonical + " "):
            with (
                self.subTest(document=repr(document)),
                self.assertRaises(IdentityError),
            ):
                parse_manifest(document)  # type: ignore[arg-type]

        malformed_manifests: list[dict[str, object]] = [
            {},
            {"schema_version": 2, "sources": []},
            {"schema_version": 1, "sources": {}},
            {"schema_version": 1, "sources": [{}]},
        ]
        bad_records = copy.deepcopy(manifest)
        bad_records["sources"][0]["records"] = {}  # type: ignore[index]
        malformed_manifests.append(bad_records)
        bad_record_shape = copy.deepcopy(manifest)
        bad_record_shape["sources"][0]["records"][0] = {}  # type: ignore[index]
        malformed_manifests.append(bad_record_shape)
        for malformed in malformed_manifests:
            with self.subTest(malformed=malformed), self.assertRaises(IdentityError):
                validate_manifest(malformed)

    def test_ownership_validation_checks_all_state_variants(self) -> None:
        row, manifest = _identity_row_and_manifest()
        record = manifest["sources"][0]["records"][0]  # type: ignore[index]
        variants: list[dict[str, object]] = []
        for field, value in (
            ("source_record_id", "rec_" + "0" * 64),
            ("transaction_id_kind", "future"),
            ("state", "future"),
        ):
            altered = copy.deepcopy(manifest)
            altered["sources"][0]["records"][0][field] = value  # type: ignore[index]
            variants.append(altered)
        retired_with_locator = copy.deepcopy(manifest)
        retired = retired_with_locator["sources"][0]["records"][0]  # type: ignore[index]
        retired["state"] = "retired"
        variants.append(retired_with_locator)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(IdentityError):
                validate_manifest(variant)

        self.assertEqual(record["state"], "active")
        with self.assertRaises(IdentityError):
            ownership_exact_state_key("not a record")  # type: ignore[arg-type]
        with self.assertRaises(IdentityError):
            ownership_exact_state_key({"current_locator": None})
        origin = AllocationOrigin(
            row["source_revision"],
            manifest["sources"][0]["extractor_contract_id"],  # type: ignore[index]
            AllocationLocator(1, (1,)),
            1,
        )
        for kwargs in (
            {"state": "future"},
            {"transaction_id_kind": "future"},
            {"preserved_transaction_id": "txn_0123456789abcdef"},
            {"transaction_id_kind": "preserved_legacy"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(IdentityError):
                ownership_record(
                    source_id_value=row["source_id"],
                    fingerprint=record_fingerprint(row),
                    origin=origin,
                    **kwargs,  # type: ignore[arg-type]
                )


class OverlapBoundaryCoverageTest(unittest.TestCase):
    def test_manifest_validation_paths_and_overlap_agreement_guards(self) -> None:
        with self.assertRaises(ValueError):
            empty_overlap_manifest("not a namespace")
        for document in ("not json", "[]", '{"schema_version":2}\n'):
            with self.subTest(document=document), self.assertRaises(ValueError):
                parse_overlap_manifest(document)

        first = _source_row("1", "a")
        second = _source_row("2", "b")
        result = canonicalize_overlaps(
            [first, second], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        validate_overlap_agreement(result.rows, [first, second], result.manifest)
        with self.assertRaises(ValueError):
            validate_overlap_agreement([], [first, second], result.manifest)

        missing_column = dict(result.rows[0])
        missing_column.pop("notes")
        incomplete_group = dict(result.rows[0])
        incomplete_group["canonical_group_id"] = ""
        bad_legacy_evidence = dict(result.rows[0])
        for field in (
            "canonical_group_id",
            "canonical_slot",
            "provenance_status",
            "source_occurrence_count",
        ):
            bad_legacy_evidence[field] = ""
        bad_legacy_evidence["source_id"] = first["source_id"]
        changed_group = dict(result.rows[0])
        changed_group["canonical_slot"] = "2"
        public_source_display = dict(result.rows[0])
        public_source_display["source_file"] = "synthetic.csv"
        for actual_rows in (
            [missing_column],
            [incomplete_group],
            [bad_legacy_evidence],
            [changed_group],
            [public_source_display],
        ):
            with self.subTest(actual_rows=actual_rows), self.assertRaises(ValueError):
                validate_overlap_agreement(
                    actual_rows, [first, second], result.manifest
                )

        bad_amount = dict(result.rows[0])
        bad_amount["amount_hkd"] = "-99.00"
        with self.assertRaises(ValueError):
            overlap_module._validate_canonical_amount_hkd(
                bad_amount,
                [first, second],
                result.manifest["groups"][0]["record_fingerprint"],
            )
        invalid_amount = dict(result.rows[0])
        invalid_amount["amount_hkd"] = "not a decimal"
        with self.assertRaises(ValueError):
            overlap_module._validate_canonical_amount_hkd(
                invalid_amount,
                [first, second],
                result.manifest["groups"][0]["record_fingerprint"],
            )

        malformed = copy.deepcopy(result.manifest)
        malformed["schema_version"] = 4
        with self.assertRaises(ValueError):
            overlap_manifest_document(malformed)
        malformed_groups = copy.deepcopy(result.manifest)
        malformed_groups["groups"] = {}
        with self.assertRaises(ValueError):
            overlap_manifest_document(malformed_groups)
        malformed_group = copy.deepcopy(result.manifest)
        malformed_group["groups"][0]["memberships"] = {}
        with self.assertRaises(ValueError):
            overlap_manifest_document(malformed_group)
        malformed_slots = copy.deepcopy(result.manifest)
        malformed_slots["groups"][0]["slots"] = {}
        with self.assertRaises(ValueError):
            overlap_manifest_document(malformed_slots)
        malformed_slot = copy.deepcopy(result.manifest)
        malformed_slot["groups"][0]["slots"][0]["state"] = "future"
        with self.assertRaises(ValueError):
            overlap_manifest_document(malformed_slot)

        legacy_manifest = {
            "schema_version": 1,
            "namespace_key": _NAMESPACE_KEY,
            "groups": [
                {
                    "group_id": result.manifest["groups"][0]["overlap_group_id"],
                    "record_fingerprint": result.manifest["groups"][0][
                        "record_fingerprint"
                    ],
                    "slots": result.manifest["groups"][0]["slots"],
                }
            ],
        }
        with self.assertRaises(ValueError):
            overlap_manifest_document(legacy_manifest)
        self.assertEqual(
            source_occurrences_path(Path("output/categorized.csv")),
            Path("output/.honeymoney-source-occurrences.csv"),
        )

    def test_projection_paths_preserve_only_proven_history(self) -> None:
        repeated = [
            _source_row("1", "a"),
            _source_row("2", "a"),
            _source_row("3", "b"),
            _source_row("4", "b"),
        ]
        repeated_result = canonicalize_overlaps(
            repeated, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        patch = {"category": "Dining", "needs_review": "false"}
        projected = project_corrections(
            repeated_result,
            {row["transaction_id"]: patch for row in repeated},
        )
        self.assertEqual(
            projected.corrections,
            {row["transaction_id"]: patch for row in repeated_result.rows},
        )
        self.assertEqual(projected.ambiguous_transaction_ids, ())

        prior = [_source_row("5", "c")]
        next_rows = [_source_row("6", "c")]
        next_result = canonicalize_overlaps(
            next_rows, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        migrated = project_migration_corrections(
            next_result,
            prior,
            next_rows,
            {prior[0]["transaction_id"]: patch},
            {},
        )
        self.assertEqual(
            migrated.corrections, {next_result.rows[0]["transaction_id"]: patch}
        )
        self.assertEqual(migrated.ambiguous_transaction_ids, ())
        self.assertEqual(
            migrated.removed_transaction_ids, (prior[0]["transaction_id"],)
        )

        ambiguous_prior = [_source_row("7", "d"), _source_row("8", "d")]
        ambiguous_next = [_source_row("9", "d"), _source_row("a", "d")]
        ambiguous_result = canonicalize_overlaps(
            ambiguous_next, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        ambiguous = project_migration_corrections(
            ambiguous_result,
            ambiguous_prior,
            ambiguous_next,
            {ambiguous_prior[0]["transaction_id"]: patch},
            {},
        )
        self.assertEqual(
            ambiguous.ambiguous_transaction_ids,
            tuple(sorted(row["transaction_id"] for row in ambiguous_result.rows)),
        )

    def test_replacement_projection_and_review_ownership_paths(self) -> None:
        patch = {"category": "Dining", "needs_review": "false"}
        prior_rows = [_source_row("1", "a")]
        next_rows = [_source_row("2", "a")]
        prior = canonicalize_overlaps(
            prior_rows, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        next_result = canonicalize_overlaps(
            next_rows, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        projected = project_replacement_corrections(
            prior,
            next_result,
            prior_rows,
            next_rows,
            {prior.rows[0]["transaction_id"]: patch},
            {prior_rows[0]["source_id"]},
        )
        self.assertEqual(
            projected.corrections,
            {next_result.rows[0]["transaction_id"]: patch},
        )

        removed = project_replacement_corrections(
            prior,
            canonicalize_overlaps([], [], prior.manifest),
            prior_rows,
            [],
            {prior.rows[0]["transaction_id"]: patch},
            {prior_rows[0]["source_id"]},
        )
        self.assertEqual(
            removed.removed_transaction_ids, (prior.rows[0]["transaction_id"],)
        )

        repeated_prior_rows = [_source_row("3", "b"), _source_row("4", "b")]
        repeated_next_rows = [_source_row("5", "b"), _source_row("6", "b")]
        repeated_prior = canonicalize_overlaps(
            repeated_prior_rows, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        repeated_next = canonicalize_overlaps(
            repeated_next_rows, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        ambiguous = project_replacement_corrections(
            repeated_prior,
            repeated_next,
            repeated_prior_rows,
            repeated_next_rows,
            {repeated_prior.rows[0]["transaction_id"]: patch},
            {repeated_prior_rows[0]["source_id"]},
        )
        self.assertEqual(
            ambiguous.ambiguous_transaction_ids,
            tuple(sorted(row["transaction_id"] for row in repeated_next.rows)),
        )

        rows = [
            {
                "transaction_id": "selected",
                "canonical_group_id": "",
                "provenance_status": "",
                "flags": "duplicate_suspected",
                "reason": "Possible duplicate transaction",
                "needs_review": "false",
                "review_reasons": "",
            },
            {
                "transaction_id": "other",
                "canonical_group_id": "group",
                "provenance_status": "ambiguous_count_mismatch",
                "flags": "overlap_history_ambiguous",
                "reason": "",
                "needs_review": "false",
                "review_reasons": "",
            },
        ]
        apply_history_ambiguity(rows, {"selected"})
        apply_history_ambiguity(rows, {"selected"})
        self.assertIn("overlap_history_ambiguous", rows[0]["flags"])
        clear_history_ambiguity(rows, {"selected"})
        self.assertNotIn("overlap_history_ambiguous", rows[0]["flags"])
        enforce_overlap_review(rows)
        self.assertNotIn("duplicate_suspected", rows[0]["flags"])
        self.assertIn(REVIEW_REASON_IDENTITY, rows[1]["review_reasons"])
        release_overlap_review_ownership(rows)

    def test_duplicate_listing_resolution_and_collision_guards(self) -> None:
        occurrences = [
            _source_row("1", "a"),
            _source_row("2", "a"),
            _source_row("3", "b"),
        ]
        for occurrence in occurrences:
            occurrence["date"] = ""
            occurrence["transaction_date"] = ""
            occurrence["posting_date"] = "2026-08-02"
            occurrence["posted_amount"] = ""
            occurrence["posted_currency"] = ""
        occurrences[0]["source_file"] = "folder\\" + "x" * 130 + ".csv"
        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [listing] = list_duplicate_groups(result, occurrences)
        self.assertEqual(listing["occurrences"][0]["source_display"], "x" * 119 + "…")
        self.assertEqual(listing["occurrences"][0]["date"], "2026-08-02")
        self.assertEqual(listing["occurrences"][0]["amount"], "-10.00")
        self.assertEqual(listing["occurrences"][0]["currency"], "HKD")

        with self.assertRaisesRegex(
            DuplicateResolutionError, "duplicate_choice_invalid"
        ):
            resolve_duplicate_group(
                occurrences,
                result.rows,
                result.manifest,
                listing["group_id"],
                "skip",
                {},
            )
        resolution = resolve_duplicate_group(
            occurrences,
            result.rows,
            result.manifest,
            listing["group_id"],
            "keep-all",
            {},
        )
        with self.assertRaisesRegex(
            DuplicateResolutionError, "duplicate_resolution_conflict"
        ):
            resolve_duplicate_group(
                occurrences,
                resolution.result.rows,
                resolution.result.manifest,
                listing["group_id"],
                "same-event",
                {},
            )

        collision = _source_row("4", "c")
        collision["transaction_id"] = result.manifest["groups"][0]["slots"][0][
            "transaction_id"
        ]
        with self.assertRaisesRegex(ValueError, "overlap_identity_hash_conflict"):
            canonicalize_overlaps([collision], [], result.manifest)


class CorrectionBoundaryCoverageTest(unittest.TestCase):
    def test_correction_csv_and_field_validation_boundaries(self) -> None:
        self.assertEqual(load_corrections({}), {})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.csv"
            self.assertEqual(
                load_corrections({"corrections": str(Path(temporary) / "missing.csv")}),
                {},
            )
            cases = {
                "empty": "",
                "duplicate header": "transaction_id,transaction_id\n",
                "unknown header": "transaction_id,future\n",
                "blank row": "transaction_id,category\n,\n",
            }
            for label, document in cases.items():
                path.write_text(document, encoding="utf-8")
                if label == "blank row":
                    self.assertEqual(load_corrections({"corrections": str(path)}), {})
                else:
                    with self.subTest(label=label), self.assertRaises(ValueError):
                        load_corrections({"corrections": str(path)})

        config = {
            "categories": ["Dining", "Unknown"],
            "owners": ["Household"],
            "payment_methods": ["Bank Account"],
        }
        invalid = (
            {"future": "value"},
            {"category": "Groceries"},
            {"flow_type": "future"},
            {"owner": "Justin"},
            {"payment_method": "Cash"},
            {"confidence": "not-a-number"},
            {"confidence": "2"},
            {"needs_review": "sometimes"},
            {"review_reasons": "future_reason"},
            {MANUAL_PAIR_FIELD: "not-a-pair"},
        )
        for correction in invalid:
            with self.subTest(correction=correction), self.assertRaises(ValueError):
                validate_correction("synthetic", correction, config)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrections.csv"
            path.write_text(
                "transaction_id,category,notes\nsaved,Dining,old\n", encoding="utf-8"
            )
            configured = {**config, "corrections": str(path)}
            output, document, merged = prepare_corrections_document(
                configured,
                {"saved": {"notes": ""}, "new": {"category": "Dining"}},
                removed_transaction_ids={"missing"},
            )
            self.assertEqual(output, path)
            self.assertEqual(merged["saved"]["notes"], "")
            self.assertIn("new,Dining", document)
        with self.assertRaisesRegex(ValueError, "Config must define"):
            prepare_corrections_document({})

    def test_correction_application_review_output_and_small_helpers(self) -> None:
        pair = manual_pair_id(["first", "second"])
        row = _source_row("1", "a", needs_review="true", flags="uncategorized")
        row["review_reasons"] = "category_decision"
        ignored = _source_row("2", "b")
        apply_corrections(
            [row, ignored],
            {
                row["transaction_id"]: {
                    "category": "Dining",
                    "flow_type": "expense",
                    "owner": "Household",
                    "payment_method": "Bank Account",
                    "confidence": "1.00",
                    "reason": "Synthetic review",
                    "notes": "",
                    "needs_review": "false",
                    "review_reasons": "",
                    MANUAL_PAIR_FIELD: pair,
                }
            },
        )
        self.assertEqual(row["flow_source"], "correction")
        self.assertEqual(manual_pair_marker(row), pair)
        self.assertIn("manual_correction", row["flags"])
        self.assertEqual(ignored["flags"], "")
        apply_corrections([row], {row["transaction_id"]: {MANUAL_PAIR_FIELD: ""}})
        self.assertEqual(manual_pair_marker(row), "")

        updates = review_state_correction_updates(
            {
                row["transaction_id"]: {"needs_review": "true", "review_reasons": "x"},
                "gone": {"needs_review": "true"},
            },
            [row],
        )
        self.assertEqual(
            updates,
            {row["transaction_id"]: {"needs_review": "false", "review_reasons": ""}},
        )
        review = to_review_row(row)
        self.assertEqual(review["suggested_category"], "Dining")
        self.assertEqual(review["category"], "")

        self.assertEqual(corrections_module._append_flag("one", "two"), "one;two")
        self.assertEqual(corrections_module._append_flag("one;two", "two"), "one;two")
        self.assertEqual(corrections_module._remove_flag("one;two;one", "one"), "two")
        self.assertEqual(
            corrections_module._correction_csv_value("notes", " ", False), ""
        )
        self.assertEqual(
            corrections_module._correction_csv_value("notes", "  x  ", True), "  x  "
        )
        self.assertEqual(
            corrections_module._correction_row("saved", {"notes": ""})["notes"], " "
        )

        rows = [
            {"payment_method": "Bank Account", "account_type": ""},
            {"payment_method": "Credit Card", "account_type": ""},
            {"payment_method": "Brokerage", "account_type": ""},
            {"payment_method": "Other", "account_type": ""},
        ]
        state = IdentityState(
            rows, empty_manifest(), manifest_document(empty_manifest())
        )
        normalized = corrections_module._normalize_ledger_rows(state)
        self.assertEqual(
            [row["account_type"] for row in normalized],
            ["bank", "credit_card", "investment", "unknown"],
        )

    def test_correction_operation_preflight_and_document_conflicts(self) -> None:
        with self.assertRaisesRegex(ValueError, "Config must define"):
            apply_correction_operation({}, Path("categorized.csv"), {})

        state = IdentityState([], empty_manifest(), manifest_document(empty_manifest()))
        with (
            patch.object(
                corrections_module, "generation_member_paths", return_value=[]
            ),
            patch.object(
                corrections_module,
                "generation_hashes",
                side_effect=[{"before": "hash"}, {"after": "hash"}],
            ),
            patch.object(
                corrections_module, "load_configured_identity_state", return_value=state
            ),
        ):
            with self.assertRaisesRegex(
                corrections_module.GenerationConflictError, "changed"
            ):
                apply_correction_operation(
                    {"corrections": "synthetic.csv"}, Path("categorized.csv"), {}
                )

        row = _source_row("1", "a")
        state = IdentityState(
            [row], empty_manifest(), manifest_document(empty_manifest())
        )
        common = (
            patch.object(
                corrections_module, "generation_member_paths", return_value=[]
            ),
            patch.object(corrections_module, "generation_hashes", return_value={}),
            patch.object(
                corrections_module, "load_configured_identity_state", return_value=state
            ),
            patch.object(corrections_module, "load_corrections", return_value={}),
        )
        with common[0], common[1], common[2], common[3]:
            with self.assertRaisesRegex(ValueError, "Unknown transaction_id"):
                apply_correction_operation(
                    {"corrections": "synthetic.csv"},
                    Path("categorized.csv"),
                    {"missing": {"category": "Dining"}},
                )
        with common[0], common[1], common[2], common[3]:
            with self.assertRaisesRegex(ValueError, "must set"):
                apply_correction_operation(
                    {"corrections": "synthetic.csv"},
                    Path("categorized.csv"),
                    {row["transaction_id"]: {}},
                )

        with self.assertRaisesRegex(ValueError, "either an identity manifest"):
            ledger_output_documents(
                Path("categorized.csv"),
                [],
                identity_manifest=empty_manifest(),
                identity_manifest_document=manifest_document(empty_manifest()),
            )
        with self.assertRaisesRegex(ValueError, "either an overlap manifest"):
            ledger_output_documents(
                Path("categorized.csv"),
                [],
                overlap_manifest=empty_overlap_manifest(_NAMESPACE_KEY),
                overlap_manifest_document_value="{}\n",
            )


if __name__ == "__main__":
    unittest.main()
