import copy
import unittest

from honeymoney.overlap import (
    AMBIGUOUS_COUNT_STATUS,
    EQUAL_POOL_STATUS,
    EXACT_ONE_TO_ONE_STATUS,
    SINGLE_SOURCE_STATUS,
    canonicalize_overlaps,
    empty_overlap_manifest,
    project_corrections,
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
            result.manifest["groups"][0]["group_id"],
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
