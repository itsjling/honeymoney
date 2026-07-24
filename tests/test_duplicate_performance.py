import unittest

from honeymoney.duplicates import evaluate_duplicate_candidates
from tests.test_duplicates import _row


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


if __name__ == "__main__":
    unittest.main()
