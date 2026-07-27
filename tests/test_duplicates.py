import json
import unittest

from honeymoney.corrections import apply_corrections
from honeymoney.duplicates import (
    DUPLICATE_FLAG,
    DUPLICATE_MATCH_TYPE,
    DUPLICATE_REVIEW_PROMOTED_FLAG,
    apply_duplicate_candidates,
    evaluate_duplicate_candidates,
)
from honeymoney.review_state import REVIEW_REASON_IDENTITY


def _id(prefix: str, character: str) -> str:
    suffix = character if len(character) == 64 else character * 64
    return prefix + suffix


def _row(
    transaction_character: str,
    source_character: str,
    *,
    account_id: str = "household_card",
    transaction_date: str = "2026-05-04",
    merchant: str = "SYNTHETIC RECURRING SHOP",
    source_row: str = "2",
    flags: str = "seed_flag",
    reason: str = "Seed reason",
    needs_review: str = "false",
) -> dict[str, str]:
    transaction_suffix = (
        transaction_character
        if len(transaction_character) == 32
        else transaction_character * 32
    )
    record_character = (
        transaction_suffix * 2
        if len(transaction_suffix) == 32
        else transaction_character
    )
    return {
        "transaction_id": "txn_" + transaction_suffix,
        "source_id": _id("src_", source_character),
        "source_namespace_id": _id("ns_", source_character),
        "source_revision": _id("rev_", source_character),
        "source_record_id": _id("rec_", record_character),
        "date": transaction_date,
        "transaction_date": transaction_date,
        "posting_date": "",
        "account_id": account_id,
        "original_amount": "-10.00",
        "original_currency": "HKD",
        "posted_amount": "-10.00",
        "posted_currency": "HKD",
        "amount_hkd": "-10.00",
        "merchant": merchant,
        "original_description": merchant,
        "category": "Dining",
        "owner": "Household",
        "needs_review": needs_review,
        "flags": flags,
        "reason": reason,
        "source_file": "display-only.csv",
        "source_row": source_row,
    }


class DuplicateEvaluationTest(unittest.TestCase):
    def test_requires_same_account_fingerprint_and_distinct_v2_sources(self) -> None:
        adjacent = [
            _row("1", "a", transaction_date="2026-05-04"),
            _row("2", "b", transaction_date="2026-05-05"),
        ]
        same_source = [_row("3", "c"), _row("4", "c", source_row="3")]
        cross_account = [
            _row("5", "d", account_id="household_card"),
            _row("6", "e", account_id="household_bank"),
        ]
        empty_account = [
            _row("7", "f", account_id=""),
            _row("8", "0", account_id=""),
        ]
        legacy = [_row("9", "1"), _row("a", "2")]
        for row in legacy:
            row["source_id"] = ""
            row["source_namespace_id"] = ""
            row["source_revision"] = ""
            row["source_record_id"] = ""
            row["transaction_id"] = "txn_" + row["transaction_id"][-16:]

        for rows in (adjacent, same_source, cross_account, empty_account, legacy):
            with self.subTest(rows=rows):
                self.assertEqual(evaluate_duplicate_candidates(rows).groups, ())

    def test_candidate_groups_and_diagnostics_are_stable_under_permutation(
        self,
    ) -> None:
        rows = [_row("3", "c"), _row("1", "a"), _row("2", "b")]
        expected_ids = tuple(sorted(row["transaction_id"] for row in rows))

        forward = evaluate_duplicate_candidates(rows)
        reverse = evaluate_duplicate_candidates(list(reversed(rows)))

        self.assertEqual(forward, reverse)
        self.assertEqual(len(forward.groups), 1)
        self.assertEqual(forward.groups[0].match_type, DUPLICATE_MATCH_TYPE)
        self.assertEqual(forward.groups[0].occurrence_ids, expected_ids)
        self.assertEqual(
            forward.diagnostic_for(expected_ids[0]),
            {
                "match_type": DUPLICATE_MATCH_TYPE,
                "occurrence_ids": list(expected_ids),
                "counterpart_occurrence_ids": list(expected_ids[1:]),
            },
        )
        serialized = json.dumps(forward.as_diagnostic(), sort_keys=True)
        for forbidden in (
            "source_id",
            "source_revision",
            "record_fingerprint",
            "display-only.csv",
            "SYNTHETIC RECURRING SHOP",
            "-10.00",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_application_is_idempotent_and_clears_stale_legacy_and_current_state(
        self,
    ) -> None:
        rows = [_row("1", "a"), _row("2", "b")]
        evaluation = evaluate_duplicate_candidates(rows)

        apply_duplicate_candidates(rows, evaluation)
        once = [dict(row) for row in rows]
        apply_duplicate_candidates(rows, evaluation)

        self.assertEqual(rows, once)
        for row in rows:
            self.assertEqual(row["needs_review"], "true")
            self.assertIn(DUPLICATE_FLAG, row["flags"].split(";"))
            self.assertIn(DUPLICATE_REVIEW_PROMOTED_FLAG, row["flags"].split(";"))
            self.assertIn(DUPLICATE_MATCH_TYPE, row["reason"])

        rows[1]["source_id"] = rows[0]["source_id"]
        apply_duplicate_candidates(rows, evaluate_duplicate_candidates(rows))

        for row in rows:
            self.assertEqual(row["needs_review"], "false")
            self.assertNotIn(DUPLICATE_FLAG, row["flags"].split(";"))
            self.assertNotIn(DUPLICATE_REVIEW_PROMOTED_FLAG, row["flags"].split(";"))
            self.assertEqual(row["flags"], "seed_flag")
            self.assertEqual(row["reason"], "Seed reason")

        legacy = _row(
            "4",
            "d",
            flags="seed_flag;duplicate_suspected",
            reason="Seed reason; Possible duplicate transaction",
            needs_review="true",
        )
        apply_duplicate_candidates([legacy], evaluate_duplicate_candidates([legacy]))
        self.assertEqual(legacy["flags"], "seed_flag")
        self.assertEqual(legacy["reason"], "Seed reason")
        self.assertEqual(legacy["needs_review"], "false")

    def test_later_independent_review_survives_stale_candidate_cleanup(self) -> None:
        rows = [
            _row("1", "a", flags="", reason=""),
            _row("2", "b", flags="", reason=""),
        ]
        apply_duplicate_candidates(rows, evaluate_duplicate_candidates(rows))
        reviewed_id = rows[0]["transaction_id"]

        apply_corrections(
            rows,
            {
                reviewed_id: {
                    "needs_review": "true",
                    "review_reasons": "other_decision",
                    "reason": "Independent synthetic review",
                }
            },
        )
        rows[1]["source_id"] = rows[0]["source_id"]
        apply_duplicate_candidates(rows, evaluate_duplicate_candidates(rows))

        reviewed = next(row for row in rows if row["transaction_id"] == reviewed_id)
        other = next(row for row in rows if row["transaction_id"] != reviewed_id)
        self.assertEqual(reviewed["needs_review"], "true")
        self.assertIn("manual_correction", reviewed["flags"].split(";"))
        self.assertEqual(reviewed["reason"], "Independent synthetic review")
        self.assertEqual(other["needs_review"], "false")

    def test_stale_duplicate_cleanup_keeps_other_identity_conflicts(self) -> None:
        for identity_flag in (
            "overlap_count_ambiguous",
            "overlap_history_ambiguous",
            "identity_migration_ambiguous",
        ):
            with self.subTest(identity_flag=identity_flag):
                row = _row(
                    "1",
                    "a",
                    flags=f"seed_flag;duplicate_suspected;{identity_flag}",
                    needs_review="true",
                )
                row["review_reasons"] = REVIEW_REASON_IDENTITY

                apply_duplicate_candidates(
                    [row],
                    evaluate_duplicate_candidates([row]),
                )

                self.assertEqual(row["needs_review"], "true")
                self.assertEqual(row["review_reasons"], REVIEW_REASON_IDENTITY)
                self.assertIn(identity_flag, row["flags"].split(";"))
                self.assertNotIn(DUPLICATE_FLAG, row["flags"].split(";"))


if __name__ == "__main__":
    unittest.main()
