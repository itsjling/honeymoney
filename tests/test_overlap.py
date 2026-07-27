import copy
import json
import unittest

from honeymoney.corrections import apply_corrections
from honeymoney.manual_pairs import MANUAL_PAIR_FIELD
from honeymoney.overlap import (
    AMBIGUOUS_COUNT_STATUS,
    EQUAL_POOL_STATUS,
    EXACT_ONE_TO_ONE_STATUS,
    SINGLE_SOURCE_STATUS,
    DuplicateResolutionError,
    apply_history_ambiguity,
    canonicalize_overlaps,
    empty_overlap_manifest,
    enforce_overlap_review,
    list_duplicate_groups,
    overlap_manifest_document,
    parse_overlap_manifest,
    project_corrections,
    project_migration_corrections,
    release_overlap_review_ownership,
    resolve_duplicate_group,
    validate_overlap_agreement,
)
from honeymoney.report import build_report_html
from honeymoney.schema import CATEGORIZED_COLUMNS
from tests.test_duplicates import _row

_NAMESPACE_KEY = "ovns_" + "a" * 64


def _occurrence(
    transaction_character: str,
    source_character: str,
    *,
    date: str = "2026-05-04",
    merchant: str = "SYNTHETIC OVERLAP",
) -> dict[str, str]:
    row = _row(
        transaction_character,
        source_character,
        transaction_date=date,
        merchant=merchant,
        flags="",
        reason="",
    )
    row.update(
        {
            "canonical_group_id": "",
            "canonical_slot": "",
            "provenance_status": "",
            "source_occurrence_count": "",
        }
    )
    return row


class CanonicalOverlapTest(unittest.TestCase):
    def test_v1_migration_rejects_unsorted_and_reused_manifest_entries(self) -> None:
        result = canonicalize_overlaps(
            [
                _occurrence("1", "a", merchant="SYNTHETIC FIRST"),
                _occurrence("2", "b", merchant="SYNTHETIC SECOND"),
            ],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        v1 = {
            "schema_version": 1,
            "namespace_key": result.manifest["namespace_key"],
            "groups": [
                {
                    "group_id": group["overlap_group_id"],
                    "record_fingerprint": group["record_fingerprint"],
                    "slots": group["slots"],
                }
                for group in result.manifest["groups"]
            ],
        }

        malformed = []
        unsorted = copy.deepcopy(v1)
        unsorted["groups"].reverse()
        malformed.append(unsorted)
        reused_fingerprint = copy.deepcopy(v1)
        reused_fingerprint["groups"].append(
            copy.deepcopy(reused_fingerprint["groups"][-1])
        )
        malformed.append(reused_fingerprint)
        reused_transaction = copy.deepcopy(v1)
        reused_transaction["groups"][1]["slots"][0]["transaction_id"] = (
            reused_transaction["groups"][0]["slots"][0]["transaction_id"]
        )
        malformed.append(reused_transaction)

        for document in malformed:
            with self.subTest(document=document), self.assertRaises(ValueError):
                parse_overlap_manifest(
                    json.dumps(
                        document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                )

    def test_one_to_one_overlap_becomes_one_canonical_occurrence(self) -> None:
        occurrences = [_occurrence("1", "a"), _occurrence("2", "b")]

        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        self.assertEqual(len(result.rows), 1)
        [canonical] = result.rows
        self.assertEqual(canonical["provenance_status"], EXACT_ONE_TO_ONE_STATUS)
        self.assertEqual(canonical["source_occurrence_count"], "2")
        self.assertEqual(canonical["canonical_slot"], "1")
        self.assertEqual(canonical["source_id"], "")
        self.assertEqual(canonical["source_file"], "")
        self.assertEqual(result.source_occurrence_count, 2)
        self.assertEqual(result.canonical_occurrence_count, 1)
        self.assertEqual(result.consolidated_occurrence_count, 1)
        self.assertEqual(
            result.diagnostic["provenance_counts"],
            {
                AMBIGUOUS_COUNT_STATUS: 0,
                EQUAL_POOL_STATUS: 0,
                EXACT_ONE_TO_ONE_STATUS: 1,
                SINGLE_SOURCE_STATUS: 0,
            },
        )
        self.assertEqual(
            result.manifest["groups"][0]["overlap_group_id"],
            "ovg_5f5f4ebc1ea6bfdbfd6850cd67789f01d75526dda3adadaa5be4f7a8ccdf7eb5",
        )
        self.assertEqual(
            canonical["transaction_id"], "txn_625c12709363b08f99b2645cb612fc91"
        )

    def test_equal_repeated_sources_preserve_supported_multiplicity(self) -> None:
        occurrences = [
            *[
                _occurrence(str(index), "a", merchant="SYNTHETIC REPEAT")
                for index in range(1, 4)
            ],
            *[
                _occurrence(str(index), "b", merchant="SYNTHETIC REPEAT")
                for index in range(4, 7)
            ],
        ]

        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        self.assertEqual(len(result.rows), 3)
        self.assertEqual(
            {row["provenance_status"] for row in result.rows}, {EQUAL_POOL_STATUS}
        )
        self.assertEqual(
            [row["canonical_slot"] for row in result.rows], ["1", "2", "3"]
        )
        self.assertEqual({row["source_occurrence_count"] for row in result.rows}, {"6"})
        self.assertTrue(all(row["needs_review"] == "false" for row in result.rows))
        [group] = result.diagnostic["groups"]
        self.assertEqual(group["source_counts"], [3, 3])
        self.assertNotIn("source_id", str(result.diagnostic))
        self.assertNotIn("record_fingerprint", str(result.diagnostic))

    def test_count_mismatch_keeps_maximum_and_marks_ambiguity(self) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
            _occurrence("6", "c"),
        ]

        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        self.assertEqual(len(result.rows), 3)
        self.assertEqual(
            {row["provenance_status"] for row in result.rows},
            {AMBIGUOUS_COUNT_STATUS},
        )
        [group] = result.diagnostic["groups"]
        self.assertEqual(group["source_counts"], [1, 2, 3])
        self.assertEqual(group["slot_support_counts"], [3, 2, 1])
        for row in result.rows:
            self.assertEqual(row["needs_review"], "true")
            self.assertIn("overlap_count_ambiguous", row["flags"].split(";"))
        self.assertEqual(result.ambiguous_group_count, 1)

    def test_duplicate_review_lists_only_count_mismatches_with_supported_floor(
        self,
    ) -> None:
        mismatch = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
            _occurrence("6", "c"),
        ]
        equal = [
            _occurrence("7", "d", merchant="SYNTHETIC EQUAL"),
            _occurrence("8", "e", merchant="SYNTHETIC EQUAL"),
        ]

        result = canonicalize_overlaps(
            [*mismatch, *equal], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        groups = list_duplicate_groups(result, [*mismatch, *equal])

        self.assertEqual(len(groups), 1)
        [group] = groups
        self.assertRegex(group["group_id"], r"^ovr_[0-9a-f]{64}$")
        self.assertEqual(group["keep_all_count"], 3)
        self.assertEqual(group["same_event_count"], 2)
        self.assertEqual(group["source_counts"], [1, 2, 3])
        self.assertEqual(len(group["occurrences"]), 6)
        self.assertEqual(
            group["canonical_occurrence_ids"],
            sorted(
                row["transaction_id"]
                for row in result.rows
                if row["merchant"] == "SYNTHETIC OVERLAP"
            ),
        )
        self.assertEqual(
            set(group["occurrences"][0]),
            {
                "account",
                "account_id",
                "amount",
                "currency",
                "date",
                "institution",
                "merchant",
                "occurrence_id",
                "source_display",
                "source_page",
                "source_row",
            },
        )
        self.assertNotIn("original_description", str(group))
        self.assertNotIn("source_id", str(group))
        self.assertNotIn("namespace_key", str(group))
        self.assertNotIn("membership_digest", str(group))
        self.assertNotIn("membership_digest", str(result.diagnostic))

    def test_same_event_resolution_keeps_the_second_largest_multiplicity(
        self,
    ) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
            _occurrence("6", "c"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)

        resolved = resolve_duplicate_group(
            occurrences,
            unresolved.rows,
            unresolved.manifest,
            group["group_id"],
            "same-event",
            {},
        )

        self.assertEqual(len(resolved.result.rows), 2)
        self.assertEqual(resolved.removed_correction_ids, ())
        self.assertFalse(resolved.idempotent)
        self.assertTrue(
            all(
                "overlap_count_ambiguous" not in row["flags"].split(";")
                for row in resolved.result.rows
            )
        )
        [manifest_group] = [
            item
            for item in resolved.result.manifest["groups"]
            if item["overlap_group_id"] == unresolved.rows[0]["canonical_group_id"]
        ]
        [membership] = manifest_group["memberships"]
        self.assertEqual(membership["group_id"], group["group_id"])
        self.assertEqual(
            membership["overlap_group_id"], manifest_group["overlap_group_id"]
        )
        self.assertEqual(membership["resolution"], "same-event")
        self.assertRegex(membership["membership_digest"], r"^ovm_[0-9a-f]{64}$")

    def test_same_resolution_is_byte_idempotent_and_conflict_fails(self) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        with self.assertRaises(DuplicateResolutionError) as invalid:
            resolve_duplicate_group(
                occurrences,
                unresolved.rows,
                unresolved.manifest,
                group["group_id"],
                "merge",
                {},
            )
        self.assertEqual(invalid.exception.code, "duplicate_choice_invalid")
        first = resolve_duplicate_group(
            occurrences,
            unresolved.rows,
            unresolved.manifest,
            group["group_id"],
            "keep-all",
            {},
        )
        first_document = overlap_manifest_document(first.result.manifest)

        repeated = resolve_duplicate_group(
            occurrences,
            first.result.rows,
            first.result.manifest,
            group["group_id"],
            "keep-all",
            {},
        )

        self.assertTrue(repeated.idempotent)
        self.assertEqual(repeated.result.rows, first.result.rows)
        self.assertEqual(
            overlap_manifest_document(repeated.result.manifest), first_document
        )
        with self.assertRaises(DuplicateResolutionError) as conflict:
            resolve_duplicate_group(
                occurrences,
                first.result.rows,
                first.result.manifest,
                group["group_id"],
                "same-event",
                {},
            )
        self.assertEqual(conflict.exception.code, "duplicate_resolution_conflict")

    def test_membership_drift_ignores_saved_resolution_and_restores_review(
        self,
    ) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
            _occurrence("6", "c"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [old_group] = list_duplicate_groups(unresolved, occurrences)
        resolved = resolve_duplicate_group(
            occurrences,
            unresolved.rows,
            unresolved.manifest,
            old_group["group_id"],
            "same-event",
            {},
        )

        changed_occurrences = occurrences[:-1]
        changed = canonicalize_overlaps(
            changed_occurrences, resolved.result.rows, resolved.result.manifest
        )
        [new_group] = list_duplicate_groups(changed, changed_occurrences)

        self.assertNotEqual(new_group["group_id"], old_group["group_id"])
        self.assertEqual(len(changed.rows), 3)
        self.assertTrue(all(row["needs_review"] == "true" for row in changed.rows))
        self.assertEqual(
            changed.diagnostic["warnings"],
            [
                {
                    "code": "duplicate_membership_changed",
                    "group_id": new_group["group_id"],
                }
            ],
        )
        with self.assertRaises(DuplicateResolutionError) as stale:
            resolve_duplicate_group(
                changed_occurrences,
                changed.rows,
                changed.manifest,
                old_group["group_id"],
                "same-event",
                {},
            )
        self.assertEqual(stale.exception.code, "duplicate_group_stale")
        with self.assertRaises(DuplicateResolutionError) as unknown:
            resolve_duplicate_group(
                changed_occurrences,
                changed.rows,
                changed.manifest,
                "ovr_" + "f" * 64,
                "same-event",
                {},
            )
        self.assertEqual(unknown.exception.code, "duplicate_group_unknown")

    def test_resolved_membership_history_warns_after_equal_count_transition(
        self,
    ) -> None:
        original_occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
        ]
        original = canonicalize_overlaps(
            original_occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [original_group] = list_duplicate_groups(original, original_occurrences)
        resolved = resolve_duplicate_group(
            original_occurrences,
            original.rows,
            original.manifest,
            original_group["group_id"],
            "same-event",
            {},
        )
        equal_occurrences = original_occurrences[1:]
        equal = canonicalize_overlaps(
            equal_occurrences,
            resolved.result.rows,
            resolved.result.manifest,
        )
        changed_occurrences = equal_occurrences[:-1]

        changed = canonicalize_overlaps(
            changed_occurrences,
            equal.rows,
            equal.manifest,
        )
        [changed_group] = list_duplicate_groups(changed, changed_occurrences)
        stable = canonicalize_overlaps(
            changed_occurrences,
            changed.rows,
            changed.manifest,
        )

        self.assertEqual(equal.diagnostic["warnings"], [])
        self.assertEqual(
            changed.diagnostic["warnings"],
            [
                {
                    "code": "duplicate_membership_changed",
                    "group_id": changed_group["group_id"],
                }
            ],
        )
        self.assertEqual(stable.diagnostic["warnings"], [])

    def test_returning_to_an_unresolved_membership_warns_after_a_resolution(
        self,
    ) -> None:
        original_occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
            _occurrence("6", "c"),
        ]
        original = canonicalize_overlaps(
            original_occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [original_group] = list_duplicate_groups(original, original_occurrences)
        changed_occurrences = original_occurrences[:-1]
        changed = canonicalize_overlaps(
            changed_occurrences, original.rows, original.manifest
        )
        [changed_group] = list_duplicate_groups(changed, changed_occurrences)
        resolved = resolve_duplicate_group(
            changed_occurrences,
            changed.rows,
            changed.manifest,
            changed_group["group_id"],
            "same-event",
            {},
        )

        returned = canonicalize_overlaps(
            original_occurrences,
            resolved.result.rows,
            resolved.result.manifest,
        )
        [returned_group] = list_duplicate_groups(returned, original_occurrences)

        self.assertEqual(returned_group["group_id"], original_group["group_id"])
        self.assertEqual(
            returned.diagnostic["warnings"],
            [
                {
                    "code": "duplicate_membership_changed",
                    "group_id": original_group["group_id"],
                }
            ],
        )
        self.assertTrue(all(row["needs_review"] == "true" for row in returned.rows))

    def test_membership_binds_source_records_counts_slots_and_tail(self) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
        ]
        first = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [first_group] = list_duplicate_groups(first, occurrences)
        [first_membership] = first.manifest["groups"][0]["memberships"]
        reallocated = copy.deepcopy(occurrences)
        reallocated[0]["source_record_id"] = "rec_" + "f" * 64

        changed = canonicalize_overlaps(reallocated, first.rows, first.manifest)
        [changed_group] = list_duplicate_groups(changed, reallocated)
        changed_membership = next(
            membership
            for membership in changed.manifest["groups"][0]["memberships"]
            if membership["group_id"] == changed_group["group_id"]
        )

        self.assertNotEqual(
            changed_membership["membership_digest"],
            first_membership["membership_digest"],
        )
        self.assertNotEqual(changed_group["group_id"], first_group["group_id"])
        self.assertEqual(
            first_membership["membership_digest"],
            "ovm_714ca8c7e9462c590f454d32ccd434805c82f84f66e5caab900325dd3c3822a9",
        )
        self.assertEqual(
            first_group["group_id"],
            "ovr_b7c20c5ed9091bea3b85c91b57935a7fac92e5ba73b3ae24fcb92d32a3524596",
        )

    def test_duplicate_evidence_uses_one_amount_currency_basis(self) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        for row in occurrences:
            row.update(
                {
                    "original_amount": "-1.00",
                    "original_currency": "USD",
                    "posted_amount": "-8.00",
                    "posted_currency": "HKD",
                    "amount_hkd": "-8.00",
                }
            )
        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        [group] = list_duplicate_groups(result, occurrences)

        self.assertEqual(
            {(item["amount"], item["currency"]) for item in group["occurrences"]},
            {("-8.00", "HKD")},
        )

    def test_same_event_removes_only_proven_equal_tail_corrections(self) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            *[_occurrence(str(index), "b") for index in range(4, 6)],
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        identifiers = [row["transaction_id"] for row in unresolved.rows]
        patch = {"category": "Dining", "needs_review": "false"}

        with self.assertRaises(DuplicateResolutionError) as conflict:
            resolve_duplicate_group(
                occurrences,
                unresolved.rows,
                unresolved.manifest,
                group["group_id"],
                "same-event",
                {identifiers[-1]: patch},
            )
        self.assertEqual(conflict.exception.code, "duplicate_history_conflict")

        resolved = resolve_duplicate_group(
            occurrences,
            unresolved.rows,
            unresolved.manifest,
            group["group_id"],
            "same-event",
            {identifier: patch for identifier in identifiers},
        )

        self.assertEqual(resolved.removed_correction_ids, (identifiers[-1],))
        self.assertEqual(resolved.correction_updates, {})
        self.assertEqual(len(resolved.result.rows), 2)

    def test_same_event_migrates_one_tail_correction_only_at_floor_one(
        self,
    ) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        first_id, tail_id = [row["transaction_id"] for row in unresolved.rows]
        patch = {"category": "Dining", "needs_review": "false"}
        unresolved.rows[-1].update(patch)
        unresolved.rows[-1]["flags"] += ";manual_correction"

        resolved = resolve_duplicate_group(
            occurrences,
            unresolved.rows,
            unresolved.manifest,
            group["group_id"],
            "same-event",
            {tail_id: patch},
        )

        self.assertEqual(resolved.removed_correction_ids, (tail_id,))
        self.assertEqual(resolved.correction_updates, {first_id: patch})
        self.assertEqual(len(resolved.result.rows), 1)

    def test_same_event_rejects_category_tail_correction_with_flow_conflict(
        self,
    ) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        retained, tail = unresolved.rows
        retained.update({"flow_type": "expense", "flow_source": "correction"})
        tail.update({"flow_type": "income", "flow_source": "rule"})
        patch = {"category": "Dining", "needs_review": "false"}
        rows_before = copy.deepcopy(unresolved.rows)
        manifest_before = copy.deepcopy(unresolved.manifest)

        with self.assertRaises(DuplicateResolutionError) as conflict:
            resolve_duplicate_group(
                occurrences,
                unresolved.rows,
                unresolved.manifest,
                group["group_id"],
                "same-event",
                {tail["transaction_id"]: patch},
            )

        self.assertEqual(conflict.exception.code, "duplicate_history_conflict")
        self.assertEqual(
            unresolved.manifest["groups"][0]["memberships"][0]["resolution"],
            "unresolved",
        )
        self.assertEqual(unresolved.rows, rows_before)
        self.assertEqual(unresolved.manifest, manifest_before)

    def test_same_event_rejects_multiple_tail_corrections_at_floor_one(self) -> None:
        occurrences = [
            *[_occurrence(str(index), "a") for index in range(1, 4)],
            _occurrence("4", "b"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        tail_ids = [row["transaction_id"] for row in unresolved.rows[1:]]
        patch = {"category": "Dining", "needs_review": "false"}

        with self.assertRaises(DuplicateResolutionError) as conflict:
            resolve_duplicate_group(
                occurrences,
                unresolved.rows,
                unresolved.manifest,
                group["group_id"],
                "same-event",
                {identifier: patch for identifier in tail_ids},
            )

        self.assertEqual(conflict.exception.code, "duplicate_history_conflict")

    def test_keep_all_preserves_current_manual_review_state(self) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        for occurrence in occurrences:
            occurrence["needs_review"] = "true"
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        corrected_id = unresolved.rows[0]["transaction_id"]
        apply_corrections(
            unresolved.rows,
            {corrected_id: {"needs_review": "false"}},
        )

        resolved = resolve_duplicate_group(
            occurrences,
            unresolved.rows,
            unresolved.manifest,
            group["group_id"],
            "keep-all",
            {corrected_id: {"needs_review": "false"}},
        )

        corrected = next(
            row for row in resolved.result.rows if row["transaction_id"] == corrected_id
        )
        self.assertEqual(corrected["needs_review"], "false")
        self.assertNotIn("overlap_count_ambiguous", corrected["flags"].split(";"))
        self.assertNotIn("overlap_count_prior_review", corrected["flags"].split(";"))

    def test_same_event_protects_non_overlap_review_history(self) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        unresolved.rows[-1]["notes"] = "Synthetic protected review note"

        with self.assertRaises(DuplicateResolutionError) as conflict:
            resolve_duplicate_group(
                occurrences,
                unresolved.rows,
                unresolved.manifest,
                group["group_id"],
                "same-event",
                {},
            )

        self.assertEqual(conflict.exception.code, "duplicate_history_conflict")

    def test_resolutions_clear_only_overlap_owned_review(self) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        unresolved = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        [group] = list_duplicate_groups(unresolved, occurrences)
        release_overlap_review_ownership(unresolved.rows)
        for row in unresolved.rows:
            row["needs_review"] = "true"
            row["reason"] = "Synthetic independent review"
            row["flags"] = "synthetic_independent_review"
        enforce_overlap_review(unresolved.rows, unresolved)

        for choice in ("same-event", "keep-all"):
            with self.subTest(choice=choice):
                resolved = resolve_duplicate_group(
                    occurrences,
                    copy.deepcopy(unresolved.rows),
                    unresolved.manifest,
                    group["group_id"],
                    choice,
                    {},
                )
                self.assertTrue(
                    all(row["needs_review"] == "true" for row in resolved.result.rows)
                )
                self.assertTrue(
                    all(
                        "synthetic_independent_review" in row["flags"].split(";")
                        and "Synthetic independent review" in row["reason"]
                        and "overlap_count_ambiguous" not in row["flags"].split(";")
                        and "Exact overlap has different source occurrence counts"
                        not in row["reason"]
                        for row in resolved.result.rows
                    )
                )

    def test_single_source_repeats_stay_separate(self) -> None:
        occurrences = [_occurrence(str(index), "a") for index in range(1, 4)]

        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        self.assertEqual(len(result.rows), 3)
        self.assertEqual(
            {row["provenance_status"] for row in result.rows},
            {SINGLE_SOURCE_STATUS},
        )

    def test_input_order_does_not_change_rows_or_provenance(self) -> None:
        occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
            _occurrence("4", "b"),
        ]
        manifest = empty_overlap_manifest(_NAMESPACE_KEY)

        forward = canonicalize_overlaps(occurrences, [], manifest)
        reverse = canonicalize_overlaps(
            list(reversed(occurrences)), [], copy.deepcopy(manifest)
        )

        self.assertEqual(forward.rows, reverse.rows)
        self.assertEqual(forward.manifest, reverse.manifest)
        self.assertEqual(forward.diagnostic, reverse.diagnostic)

    def test_combined_and_sequential_import_preserve_the_same_mutable_state(
        self,
    ) -> None:
        first_occurrence = _occurrence("1", "a")
        second_occurrence = _occurrence("2", "b")
        for occurrence in (first_occurrence, second_occurrence):
            occurrence.update(
                {
                    "category": "Dining",
                    "flow_type": "expense",
                    "needs_review": "false",
                    "confidence": "1.00",
                }
            )
        combined = canonicalize_overlaps(
            [first_occurrence, second_occurrence],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        first = canonicalize_overlaps(
            [first_occurrence], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        sequential = canonicalize_overlaps(
            [first_occurrence, second_occurrence], first.rows, first.manifest
        )

        self.assertEqual(combined.rows, sequential.rows)
        self.assertEqual(combined.manifest, sequential.manifest)

    def test_slot_tombstones_restore_ids_after_count_shrink_and_growth(self) -> None:
        three = [_occurrence(str(index), "a") for index in range(1, 4)]
        first = canonicalize_overlaps(three, [], empty_overlap_manifest(_NAMESPACE_KEY))
        original_ids = [row["transaction_id"] for row in first.rows]

        shrunk = canonicalize_overlaps(three[:2], first.rows, first.manifest)
        self.assertEqual(
            [row["transaction_id"] for row in shrunk.rows], original_ids[:2]
        )
        [group] = shrunk.manifest["groups"]
        self.assertEqual(
            [slot["state"] for slot in group["slots"]],
            ["active", "active", "retired"],
        )

        restored = canonicalize_overlaps(three, shrunk.rows, shrunk.manifest)
        self.assertEqual([row["transaction_id"] for row in restored.rows], original_ids)

    def test_existing_transfer_link_stays_with_the_canonical_slot(self) -> None:
        occurrences = [_occurrence("1", "a"), _occurrence("2", "b")]
        first = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        first.rows[0].update(
            {
                "flow_type": "internal_transfer",
                "flow_source": "correction",
                "transfer_group_id": "transfer_synthetic",
                "paired_transaction_id": "txn_" + "f" * 32,
            }
        )

        repeated = canonicalize_overlaps(
            list(reversed(occurrences)), first.rows, first.manifest
        )

        self.assertEqual(
            repeated.rows[0]["transaction_id"], first.rows[0]["transaction_id"]
        )
        self.assertEqual(repeated.rows[0]["paired_transaction_id"], "txn_" + "f" * 32)
        self.assertEqual(repeated.rows[0]["transfer_group_id"], "transfer_synthetic")

    def test_removing_one_overlap_source_keeps_the_canonical_decision(self) -> None:
        first_occurrence = _occurrence("1", "a")
        second_occurrence = _occurrence("2", "b")
        first = canonicalize_overlaps(
            [first_occurrence, second_occurrence],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        first.rows[0]["category"] = "Dining"

        remaining = canonicalize_overlaps(
            [second_occurrence], first.rows, first.manifest
        )

        self.assertEqual(
            remaining.rows[0]["transaction_id"], first.rows[0]["transaction_id"]
        )
        self.assertEqual(remaining.rows[0]["category"], "Dining")
        self.assertEqual(remaining.rows[0]["provenance_status"], SINGLE_SOURCE_STATUS)

    def test_current_source_template_replaces_stale_canonical_fields(self) -> None:
        first_source = _occurrence("1", "a")
        second_source = _occurrence("2", "b")
        first_source.update({"account": "Card", "account_type": "credit_card"})
        second_source.update({"account": "Bank", "account_type": "bank"})
        first = canonicalize_overlaps(
            [first_source, second_source], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        first.rows[0]["category"] = "Groceries"

        current = canonicalize_overlaps([first_source], first.rows, first.manifest)

        [canonical] = current.rows
        self.assertEqual(canonical["account"], "Card")
        self.assertEqual(canonical["account_type"], "credit_card")
        self.assertEqual(canonical["category"], "Groceries")

    def test_replacement_to_zero_retires_then_restores_the_same_slot(self) -> None:
        occurrence = _occurrence("1", "a")
        first = canonicalize_overlaps(
            [occurrence], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        empty = canonicalize_overlaps([], first.rows, first.manifest)
        self.assertEqual(empty.rows, [])
        self.assertEqual(empty.manifest["groups"][0]["slots"][0]["state"], "retired")

        restored = canonicalize_overlaps([occurrence], [], empty.manifest)
        self.assertEqual(
            restored.rows[0]["transaction_id"], first.rows[0]["transaction_id"]
        )

    def test_new_growth_slot_flags_conflicting_pooled_history(self) -> None:
        first_occurrence = _occurrence("1", "a")
        first = canonicalize_overlaps(
            [first_occurrence], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        conflicting = _occurrence("2", "a")
        conflicting["category"] = "Other"

        grown = canonicalize_overlaps(
            [
                first_occurrence,
                conflicting,
                _occurrence("3", "b"),
                _occurrence("4", "b"),
            ],
            first.rows,
            first.manifest,
        )

        self.assertNotIn("overlap_history_ambiguous", grown.rows[0]["flags"])
        self.assertIn("overlap_history_ambiguous", grown.rows[1]["flags"])

    def test_fresh_raw_overlap_does_not_create_migration_history_ambiguity(
        self,
    ) -> None:
        reviewed = _occurrence("1", "a")
        reviewed.update(
            {
                "category": "Dining",
                "flow_type": "expense",
                "flow_source": "correction",
                "confidence": "1.00",
                "needs_review": "false",
            }
        )
        raw = _occurrence("2", "b")
        raw.update(
            {
                "category": "Unknown",
                "flow_type": "unresolved",
                "flow_source": "deterministic",
                "confidence": "0.00",
                "needs_review": "true",
                "reason": "No categorization rules have been applied",
            }
        )

        migrated = canonicalize_overlaps(
            [reviewed, raw],
            [reviewed],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )

        [canonical] = migrated.rows
        self.assertEqual(canonical["category"], "Dining")
        self.assertEqual(canonical["needs_review"], "false")
        self.assertNotIn("overlap_history_ambiguous", canonical["flags"].split(";"))

    def test_canonical_correction_clears_history_ambiguity(self) -> None:
        result = canonicalize_overlaps(
            [_occurrence("1", "a"), _occurrence("2", "b")],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        [canonical] = result.rows
        apply_history_ambiguity(result.rows, {canonical["transaction_id"]})

        apply_corrections(
            result.rows,
            {
                canonical["transaction_id"]: {
                    "category": "Groceries",
                    "needs_review": "false",
                }
            },
        )
        enforce_overlap_review(result.rows)

        self.assertEqual(canonical["needs_review"], "false")
        self.assertNotIn("overlap_history_ambiguous", canonical["flags"].split(";"))
        self.assertNotIn("conflicting review history", canonical["reason"].casefold())

    def test_corrections_project_only_when_assignment_is_proven(self) -> None:
        one_to_one = canonicalize_overlaps(
            [_occurrence("1", "a"), _occurrence("2", "b")],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        projected = project_corrections(
            one_to_one,
            {
                "txn_" + "1" * 32: {
                    "category": "Dining",
                    "needs_review": "false",
                }
            },
        )
        [canonical_id] = [row["transaction_id"] for row in one_to_one.rows]
        self.assertEqual(projected.corrections[canonical_id]["category"], "Dining")
        self.assertEqual(projected.ambiguous_transaction_ids, ())

        repeated = canonicalize_overlaps(
            [
                _occurrence("1", "a"),
                _occurrence("2", "a"),
                _occurrence("3", "b"),
                _occurrence("4", "b"),
            ],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        conflicted = project_corrections(
            repeated,
            {
                "txn_" + "1" * 32: {
                    "category": "Dining",
                    "needs_review": "false",
                }
            },
        )
        self.assertEqual(
            set(conflicted.ambiguous_transaction_ids),
            {row["transaction_id"] for row in repeated.rows},
        )

    def test_canonical_correction_wins_over_ambiguous_source_history(self) -> None:
        repeated = canonicalize_overlaps(
            [
                _occurrence("1", "a"),
                _occurrence("2", "a"),
                _occurrence("3", "b"),
                _occurrence("4", "b"),
            ],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        first_id, second_id = [row["transaction_id"] for row in repeated.rows]

        projected = project_corrections(
            repeated,
            {
                first_id: {"category": "Groceries", "needs_review": "false"},
                "txn_" + "1" * 32: {
                    "category": "Dining",
                    "needs_review": "false",
                },
            },
        )

        self.assertEqual(projected.corrections[first_id]["category"], "Groceries")
        self.assertNotIn(second_id, projected.corrections)
        self.assertEqual(projected.ambiguous_transaction_ids, (second_id,))

    def test_migration_rekeys_unique_history_and_retires_conflicting_aliases(
        self,
    ) -> None:
        prior = [
            _occurrence("1", "a", merchant="SYNTHETIC REPEAT"),
            _occurrence("2", "a", merchant="SYNTHETIC REPEAT"),
            _occurrence("5", "a", merchant="SYNTHETIC UNIQUE"),
        ]
        current = [
            _occurrence("3", "a", merchant="SYNTHETIC REPEAT"),
            _occurrence("4", "a", merchant="SYNTHETIC REPEAT"),
            _occurrence("6", "a", merchant="SYNTHETIC UNIQUE"),
        ]
        for row in current:
            row["account_id"] = "household_card_v2"
        result = canonicalize_overlaps(
            current, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        source_corrections = {
            prior[0]["transaction_id"]: {
                "category": "Dining",
                "needs_review": "false",
            },
            prior[1]["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
            },
            prior[2]["transaction_id"]: {
                "category": "Transport",
                "needs_review": "false",
            },
        }

        projected = project_migration_corrections(
            result,
            prior,
            current,
            source_corrections,
            source_corrections,
        )

        unique_id = next(
            row["transaction_id"]
            for row in result.rows
            if row["merchant"] == "SYNTHETIC UNIQUE"
        )
        repeated_ids = {
            row["transaction_id"]
            for row in result.rows
            if row["merchant"] == "SYNTHETIC REPEAT"
        }
        self.assertEqual(
            projected.corrections,
            {
                unique_id: {
                    "category": "Transport",
                    "needs_review": "false",
                }
            },
        )
        self.assertEqual(set(projected.ambiguous_transaction_ids), repeated_ids)
        self.assertEqual(
            set(projected.removed_transaction_ids),
            set(source_corrections),
        )

    def test_migration_preserves_shared_manual_pair_id_only_for_unique_rows(
        self,
    ) -> None:
        pair_id = "mpair_" + "a" * 32
        prior = [
            _occurrence("1", "a", merchant="SYNTHETIC CASH OUT"),
            _occurrence("2", "a", merchant="SYNTHETIC CASH IN"),
        ]
        current = [
            _occurrence("3", "a", merchant="SYNTHETIC CASH OUT"),
            _occurrence("4", "a", merchant="SYNTHETIC CASH IN"),
        ]
        for row in current:
            row["account_id"] = "cash_account_v2"
        result = canonicalize_overlaps(
            current,
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )
        prior_corrections = {
            row["transaction_id"]: {
                "category": "Internal Transfer",
                "flow_type": "internal_transfer",
                MANUAL_PAIR_FIELD: pair_id,
            }
            for row in prior
        }

        projected = project_migration_corrections(
            result,
            prior,
            current,
            prior_corrections,
            prior_corrections,
        )

        self.assertEqual(
            set(projected.corrections),
            {row["transaction_id"] for row in result.rows},
        )
        self.assertEqual(
            {
                correction[MANUAL_PAIR_FIELD]
                for correction in projected.corrections.values()
            },
            {pair_id},
        )
        self.assertEqual(projected.ambiguous_transaction_ids, ())

    def test_unresolved_legacy_rows_pass_through_without_consolidation(self) -> None:
        legacy = _occurrence("1", "a")
        legacy.update(
            {
                "transaction_id": "legacy-transaction",
                "source_id": "",
                "source_namespace_id": "",
                "source_revision": "",
                "source_record_id": "",
            }
        )

        result = canonicalize_overlaps(
            [legacy], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(set(result.rows[0]), set(CATEGORIZED_COLUMNS))
        self.assertEqual(result.rows[0]["transaction_id"], "legacy-transaction")
        self.assertEqual(result.rows[0]["canonical_group_id"], "")
        validate_overlap_agreement(result.rows, [legacy], result.manifest)

    def test_agreement_rejects_partial_canonical_metadata_on_legacy_row(self) -> None:
        legacy = _occurrence("1", "a")
        legacy.update(
            {
                "transaction_id": "legacy-transaction",
                "source_id": "",
                "source_namespace_id": "",
                "source_revision": "",
                "source_record_id": "",
            }
        )
        result = canonicalize_overlaps(
            [legacy], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        for field, value in (
            ("canonical_slot", "1"),
            ("provenance_status", SINGLE_SOURCE_STATUS),
            ("source_occurrence_count", "1"),
        ):
            with self.subTest(field=field):
                corrupted = copy.deepcopy(result.rows)
                corrupted[0][field] = value
                with self.assertRaisesRegex(ValueError, "overlap_manifest_invalid"):
                    validate_overlap_agreement(corrupted, [legacy], result.manifest)

    def test_agreement_accepts_only_matched_exchange_valuation_without_source_hkd(
        self,
    ) -> None:
        occurrence = _occurrence("1", "a")
        occurrence.update(
            {
                "original_amount": "100.00",
                "original_currency": "EUR",
                "posted_amount": "100.00",
                "posted_currency": "EUR",
                "amount_hkd": "",
                "valuation_source": "missing",
                "valuation_status": "missing",
            }
        )
        result = canonicalize_overlaps(
            [occurrence], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        matched = copy.deepcopy(result.rows)
        matched[0]["amount_hkd"] = "850.00"
        matched[0]["valuation_source"] = "matched_exchange_leg"
        matched[0]["valuation_status"] = "actual"

        validate_overlap_agreement(matched, [occurrence], result.manifest)

        unsupported = copy.deepcopy(matched)
        unsupported[0]["valuation_source"] = "configured_fixed_rate"
        with self.assertRaisesRegex(ValueError, "overlap_manifest_invalid"):
            validate_overlap_agreement(unsupported, [occurrence], result.manifest)

        configured_source = copy.deepcopy(occurrence)
        configured_source["amount_hkd"] = "780.00"
        configured_source["valuation_source"] = "configured_fixed_rate"
        configured_source["valuation_status"] = "estimated"
        configured_result = canonicalize_overlaps(
            [configured_source], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        preferred_match = copy.deepcopy(configured_result.rows)
        preferred_match[0]["amount_hkd"] = "850.00"
        preferred_match[0]["valuation_source"] = "matched_exchange_leg"
        preferred_match[0]["valuation_status"] = "actual"
        validate_overlap_agreement(
            preferred_match,
            [configured_source],
            configured_result.manifest,
        )

    def test_agreement_rejects_public_source_display_and_balance_evidence(self) -> None:
        occurrences = [_occurrence("1", "a"), _occurrence("2", "b")]
        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        corrupted = copy.deepcopy(result.rows)
        corrupted[0]["source_file"] = "statement.csv"
        with self.assertRaisesRegex(ValueError, "overlap_manifest_invalid"):
            validate_overlap_agreement(corrupted, occurrences, result.manifest)
        corrupted = copy.deepcopy(result.rows)
        corrupted[0]["statement_closing_balance"] = "100.00"
        with self.assertRaisesRegex(ValueError, "overlap_manifest_invalid"):
            validate_overlap_agreement(corrupted, occurrences, result.manifest)

    def test_agreement_rejects_canonical_total_without_active_source_support(
        self,
    ) -> None:
        occurrences = [_occurrence("1", "a"), _occurrence("2", "b")]
        result = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        corrupted = copy.deepcopy(result.rows)
        corrupted[0]["amount_hkd"] = "-99.00"

        with self.assertRaisesRegex(ValueError, "overlap_manifest_invalid"):
            validate_overlap_agreement(corrupted, occurrences, result.manifest)

    def test_equal_counts_clear_review_owned_only_by_overlap_ambiguity(self) -> None:
        ambiguous_occurrences = [
            _occurrence("1", "a"),
            _occurrence("2", "a"),
            _occurrence("3", "b"),
        ]
        first = canonicalize_overlaps(
            ambiguous_occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        self.assertTrue(all(row["needs_review"] == "true" for row in first.rows))

        equal = canonicalize_overlaps(
            [*ambiguous_occurrences, _occurrence("4", "b")],
            first.rows,
            first.manifest,
        )

        self.assertTrue(all(row["needs_review"] == "false" for row in equal.rows))
        self.assertTrue(
            all(
                AMBIGUOUS_COUNT_STATUS not in row["provenance_status"]
                for row in equal.rows
            )
        )
        self.assertTrue(
            all("overlap_count_ambiguous" not in row["flags"] for row in equal.rows)
        )

    def test_canonical_transaction_id_may_not_collide_with_source_identity(
        self,
    ) -> None:
        occurrences = [_occurrence("1", "a"), _occurrence("2", "b")]
        first = canonicalize_overlaps(
            occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
        )
        occurrences[0]["transaction_id"] = first.rows[0]["transaction_id"]

        with self.assertRaisesRegex(ValueError, "overlap_identity_hash_conflict"):
            canonicalize_overlaps(
                occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
            )

    def test_equal_identity_with_disagreeing_total_fails_closed(self) -> None:
        occurrences = [_occurrence("1", "a"), _occurrence("2", "b")]
        occurrences[1]["amount_hkd"] = "-99.00"

        with self.assertRaisesRegex(ValueError, "overlap_immutable_conflict"):
            canonicalize_overlaps(
                occurrences, [], empty_overlap_manifest(_NAMESPACE_KEY)
            )

    def test_normalized_identity_and_total_values_consolidate(self) -> None:
        first = _occurrence("1", "a", merchant="SYNTHETIC OVERLAP")
        second = _occurrence("2", "b", merchant=" synthetic  overlap ")
        first.update(
            {
                "account": "Household card",
                "account_type": "credit_card",
                "institution": "Synthetic Bank A",
                "country": "HK",
            }
        )
        second.update(
            {
                "account_id": " HOUSEHOLD_CARD ",
                "original_amount": "-10",
                "original_currency": "hkd",
                "posted_amount": "-10.000",
                "posted_currency": "hkd",
                "amount_hkd": "-10.000",
                "original_description": " synthetic  overlap ",
                "account": "Different display",
                "account_type": "bank",
                "institution": "Synthetic Bank B",
                "country": "GB",
            }
        )

        result = canonicalize_overlaps(
            [first, second], [], empty_overlap_manifest(_NAMESPACE_KEY)
        )

        [canonical] = result.rows
        self.assertEqual(canonical["account_id"], "household_card")
        self.assertEqual(canonical["merchant"], "synthetic overlap")
        self.assertEqual(canonical["original_description"], "synthetic overlap")
        self.assertEqual(canonical["original_amount"], "-10")
        self.assertEqual(canonical["posted_amount"], "-10")
        self.assertEqual(canonical["amount_hkd"], "-10")
        self.assertEqual(canonical["original_currency"], "HKD")
        self.assertEqual(canonical["posted_currency"], "HKD")
        for field in ("account", "account_type", "institution", "country"):
            self.assertEqual(canonical[field], "")

    def test_html_distinguishes_source_and_canonical_counts(self) -> None:
        result = canonicalize_overlaps(
            [_occurrence("1", "a"), _occurrence("2", "b")],
            [],
            empty_overlap_manifest(_NAMESPACE_KEY),
        )

        document = build_report_html(result.rows, "2026-05", source_occurrence_count=2)

        self.assertIn("2 source occurrences", document)
        self.assertIn("1</span> canonical transactions", document)
        self.assertIn('"provenance_status": "exact_one_to_one"', document)


if __name__ == "__main__":
    unittest.main()
