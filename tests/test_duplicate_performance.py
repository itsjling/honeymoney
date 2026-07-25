import unittest
from collections.abc import Iterator

from honeymoney.duplicates import (
    DuplicateCandidateGroup,
    DuplicateEvaluation,
    apply_duplicate_candidates,
    evaluate_duplicate_candidates,
)
from tests.test_duplicates import _row


class _CountingGroups(tuple[DuplicateCandidateGroup, ...]):
    iterations: int

    def __new__(cls, groups: tuple[DuplicateCandidateGroup, ...]) -> "_CountingGroups":
        instance = super().__new__(cls, groups)
        instance.iterations = 0
        return instance

    def __iter__(self) -> Iterator[DuplicateCandidateGroup]:
        self.iterations += 1
        return super().__iter__()


class DuplicateScalingTest(unittest.TestCase):
    def test_evaluation_work_is_linear_in_ledger_rows(self) -> None:
        rows = [
            _row(
                f"{index:032x}",
                f"{index:064x}",
                transaction_date=f"2026-05-{index % 28 + 1:02d}",
                merchant=f"SYNTHETIC MERCHANT {index}",
            )
            for index in range(8_000)
        ]
        operation_counts: dict[str, int] = {}

        evaluation = evaluate_duplicate_candidates(
            rows, operation_counts=operation_counts
        )

        self.assertEqual(evaluation.groups, ())
        self.assertEqual(operation_counts["rows_examined"], len(rows))
        self.assertEqual(operation_counts["fingerprints_calculated"], len(rows))
        self.assertEqual(operation_counts["candidate_buckets"], len(rows))

    def test_candidate_heavy_application_does_not_rescan_groups_per_occurrence(
        self,
    ) -> None:
        rows = [
            _row(
                f"{index * 2 + offset:032x}",
                f"{index * 2 + offset:064x}",
                merchant=f"SYNTHETIC CANDIDATE {index}",
            )
            for index in range(250)
            for offset in range(2)
        ]
        evaluated = evaluate_duplicate_candidates(rows)
        groups = _CountingGroups(evaluated.groups)
        evaluation = DuplicateEvaluation(groups)
        groups.iterations = 0

        apply_duplicate_candidates(rows, evaluation)

        self.assertEqual(groups.iterations, 1)
        self.assertTrue(
            all("duplicate_suspected" in row["flags"].split(";") for row in rows)
        )


if __name__ == "__main__":
    unittest.main()
