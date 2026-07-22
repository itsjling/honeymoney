import unittest
from datetime import date, timedelta

from honeymoney.cli import _annotate_duplicate_suspicions

DUPLICATE_FLAG = "duplicate_suspected"
DUPLICATE_REASON = "Possible duplicate transaction"


def _row(
    transaction_id: str,
    transaction_date: str,
    key: str,
    *,
    flags: str = "seed_flag",
    reason: str = "Seed reason",
) -> dict[str, str]:
    return {
        "transaction_id": transaction_id,
        "date": transaction_date,
        "amount_hkd": "-10.00",
        "original_amount": "-10.00",
        "original_currency": "HKD",
        "merchant": key,
        "original_description": key,
        "needs_review": "false",
        "flags": flags,
        "reason": reason,
    }


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def _key(row: dict[str, str], *, include_date: bool) -> tuple[str, ...]:
    fields = [
        "amount_hkd",
        "original_amount",
        "original_currency",
        "merchant",
        "original_description",
    ]
    if include_date:
        fields.insert(0, "date")
    return tuple(_normalize(row.get(field, "")) for field in fields)


def _parsed_date(row: dict[str, str]) -> date | None:
    try:
        return date.fromisoformat(row.get("date", ""))
    except ValueError:
        return None


def _mark(row: dict[str, str]) -> None:
    flags = [flag for flag in row.get("flags", "").split(";") if flag]
    if DUPLICATE_FLAG not in flags:
        flags.append(DUPLICATE_FLAG)
    row["flags"] = ";".join(flags)
    reasons = row.get("reason", "").split("; ") if row.get("reason") else []
    if DUPLICATE_REASON not in reasons:
        reasons.append(DUPLICATE_REASON)
    row["reason"] = "; ".join(reasons)
    row["needs_review"] = "true"


def _pairwise_oracle(
    current: list[dict[str, str]], retained: list[dict[str, str]]
) -> None:
    comparison = [*retained, *current]
    for row in current:
        if (
            sum(
                _key(other, include_date=True) == _key(row, include_date=True)
                for other in comparison
            )
            > 1
        ):
            _mark(row)

        row_date = _parsed_date(row)
        if row_date is None:
            continue
        for other in comparison:
            if other is row or _key(other, include_date=False) != _key(
                row, include_date=False
            ):
                continue
            other_date = _parsed_date(other)
            if other_date is not None and abs((row_date - other_date).days) <= 1:
                _mark(row)
                break


class DuplicatePairwiseOracleTest(unittest.TestCase):
    def test_current_duplicate_outputs_match_pairwise_oracle(self) -> None:
        retained = [
            _row("history-invalid", "not-a-date", "INVALID EXACT"),
            _row("history-same", "2026-05-10", "SAME DATE"),
            _row("history-minus", "2026-05-10", "MINUS ONE"),
            _row("history-plus", "2026-05-10", "PLUS ONE"),
            _row("history-two", "2026-05-10", "TWO DAYS"),
            _row("history-many-a", "2026-05-10", "MANY MATCHES"),
            _row("history-many-b", "2026-05-11", "MANY MATCHES"),
            _row("history-key-a", "2026-05-10", "KEY A"),
        ]
        current = [
            _row("current-invalid-exact", "not-a-date", "INVALID EXACT"),
            _row("current-invalid-unique", "still-not-a-date", "INVALID UNIQUE"),
            _row("current-same", "2026-05-10", "SAME DATE"),
            _row("current-minus", "2026-05-09", "MINUS ONE"),
            _row("current-plus", "2026-05-11", "PLUS ONE"),
            _row("current-two", "2026-05-12", "TWO DAYS"),
            _row("current-many-a", "2026-05-09", "MANY MATCHES"),
            _row("current-many-b", "2026-05-12", "MANY MATCHES"),
            _row("current-key-b", "2026-05-10", "KEY B"),
            _row("current-pair-a", "2026-06-01", "CURRENT PAIR"),
            _row("current-pair-b", "2026-06-02", "CURRENT PAIR"),
            _row(
                "current-idempotent",
                "2026-05-10",
                "SAME DATE",
                flags="seed_flag;duplicate_suspected",
                reason="Seed reason; Possible duplicate transaction",
            ),
        ]
        expected_current = [dict(row) for row in current]
        actual_current = [dict(row) for row in current]
        actual_retained = [dict(row) for row in retained]

        _pairwise_oracle(expected_current, retained)
        _annotate_duplicate_suspicions(actual_current, actual_retained)
        _annotate_duplicate_suspicions(actual_current, actual_retained)

        self.assertEqual(actual_current, expected_current)
        self.assertEqual(actual_retained, retained)
        self.assertNotIn(DUPLICATE_FLAG, actual_current[1]["flags"])
        self.assertNotIn(DUPLICATE_FLAG, actual_current[5]["flags"])
        self.assertNotIn(DUPLICATE_FLAG, actual_current[8]["flags"])

    def test_separator_text_cannot_create_a_false_duplicate(self) -> None:
        retained = [_row("history", "2026-05-10", "A|B")]
        retained[0]["original_description"] = "C"
        current = [_row("incoming", "2026-05-10", "A")]
        current[0]["original_description"] = "B|C"
        expected = [dict(row) for row in current]
        actual = [dict(row) for row in current]

        _pairwise_oracle(expected, retained)
        _annotate_duplicate_suspicions(actual, retained)

        self.assertEqual(actual, expected)
        self.assertNotIn(DUPLICATE_FLAG, actual[0]["flags"])


class DuplicateScalingTest(unittest.TestCase):
    def test_date_parsing_and_window_checks_are_bounded(self) -> None:
        start = date(2000, 1, 1)
        retained = [
            _row(
                f"retained-{index}",
                (start + timedelta(days=index * 2)).isoformat(),
                "LARGE SAME KEY GROUP",
                flags="",
                reason="",
            )
            for index in range(4_000)
        ]
        incoming = [
            _row(
                f"incoming-{index}",
                (start + timedelta(days=(4_000 + index) * 2)).isoformat(),
                "LARGE SAME KEY GROUP",
                flags="",
                reason="",
            )
            for index in range(4_000)
        ]
        operation_counts: dict[str, int] = {}

        _annotate_duplicate_suspicions(
            incoming, retained, operation_counts=operation_counts
        )

        comparison_count = len(retained) + len(incoming)
        self.assertEqual(operation_counts["date_parses"], comparison_count)
        self.assertLessEqual(operation_counts["window_checks"], comparison_count * 2)
        self.assertTrue(all(DUPLICATE_FLAG not in row["flags"] for row in incoming))
