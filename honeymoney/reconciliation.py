from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping

from honeymoney.classification_policy import trusted_accounting_provenance
from honeymoney.contracts import (
    AccountBalance,
    BalanceConflict,
    BalanceReconciliation,
    Config,
    ReconciliationSummary,
    StatementBalance,
)
from honeymoney.duplicates import (
    DUPLICATE_REVIEW_PROMOTED_FLAG,
    release_duplicate_review_ownership,
)
from honeymoney.identity import ambiguous_legacy_transaction_ids
from honeymoney.manual_pairs import (
    MANUAL_PAIR_FLAG_PREFIX,
    MANUAL_PAIR_INVALID_FLAG,
    MANUAL_PAIR_INVALID_REASON,
    ManualPairError,
    manual_pair_marker,
    validate_manual_pair_facts,
)
from honeymoney.review_state import (
    REVIEW_REASON_ACCOUNTING_FLOW,
    REVIEW_REASON_CATEGORY,
    REVIEW_REASON_CATEGORY_SUGGESTION,
    review_reason_tokens,
    set_review_reason,
    synchronize_review_states,
)
from honeymoney.schema import ALLOWED_ACCOUNT_TYPES, ALLOWED_FLOW_TYPES
from honeymoney.valuation import (
    configured_exchange_rate,
    set_matched_exchange_valuation,
    valuation_summary,
    value_transactions,
)

TRANSFER_FLOW_TYPES = {
    "internal_transfer",
    "credit_card_payment",
    "investment_transfer",
}
EXTERNAL_FLOW_TYPES = {"income", "expense", "refund"}
AMBIGUITY_FLAG = "reconciliation_ambiguous"
AMBIGUITY_PRIOR_REVIEW_FLAG = "reconciliation_ambiguous_prior_review"
AMBIGUITY_REASON = "Ambiguous transfer candidates"
CROSS_CURRENCY_FLAG = "cross_currency_exchange"
StatementBalanceKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class _TransferCandidate:
    row: dict[str, str]
    amount: Decimal
    row_date: date


def reconcile_ledger(
    rows: list[dict[str, str]],
    config: Config,
    *,
    statement_rows: list[dict[str, str]] | None = None,
) -> ReconciliationSummary:
    """Derive cash-flow treatment and pair unique owned-account transfers."""
    validate_reconciliation_config(config)
    window = reconciliation_date_window(config)
    ambiguous_legacy_ids = ambiguous_legacy_transaction_ids(rows)
    protected = {
        id(row)
        for row in rows
        if row.get("transaction_id", "") in ambiguous_legacy_ids
        and not any(
            row.get(field, "")
            for field in (
                "source_id",
                "source_namespace_id",
                "source_revision",
                "source_record_id",
            )
        )
    }
    value_transactions(
        (row for row in rows if id(row) not in protected),
        config,
        preserve_matched=False,
    )
    by_id = {
        row.get("transaction_id", ""): row
        for row in rows
        if id(row) not in protected and row.get("transaction_id")
    }
    manual_groups, invalid_manual_rows = _manual_pair_groups(rows)
    manual_reserved_ids = {
        row.get("transaction_id", "")
        for group_rows in manual_groups.values()
        for row in group_rows
    }
    manual_reserved_ids.update(
        row.get("transaction_id", "") for row in invalid_manual_rows
    )
    manual_reserved_ids.discard("")
    for row in rows:
        if id(row) in protected:
            continue
        _reset_reconciliation(row)
        derive_flow_type(row)

    paired, manual_pair_count = _apply_manual_pairs(
        manual_groups,
        invalid_manual_rows,
        protected,
    )
    cross_currency_pairs = _cross_currency_exchange_pairs(
        rows,
        config,
        protected,
        manual_reserved_ids,
    )
    for base_row, foreign_row in cross_currency_pairs:
        _pair_cross_currency(base_row, foreign_row)
        paired.update(
            {
                base_row["transaction_id"],
                foreign_row["transaction_id"],
            }
        )

    candidates: list[tuple[int, str, str, str]] = []
    eligible: list[_TransferCandidate] = []
    for row in rows:
        if (
            id(row) in protected
            or row.get("transaction_id") in paired
            or row.get("transaction_id") in manual_reserved_ids
        ):
            continue
        amount = _amount(row)
        row_date = _row_date(row)
        if amount is None or row_date is None:
            continue
        transfer_candidate = _TransferCandidate(row, amount, row_date)
        if _transfer_eligible(transfer_candidate):
            eligible.append(transfer_candidate)

    candidates_by_amount_date: dict[
        tuple[Decimal, date], list[tuple[int, _TransferCandidate]]
    ] = defaultdict(list)
    for index, candidate in enumerate(eligible):
        candidates_by_amount_date[(candidate.amount, candidate.row_date)].append(
            (index, candidate)
        )

    for index, left in enumerate(eligible):
        for day_offset in range(-window, window + 1):
            try:
                candidate_date = left.row_date + timedelta(days=day_offset)
            except OverflowError:
                continue
            for right_index, right in candidates_by_amount_date.get(
                (-left.amount, candidate_date), []
            ):
                if right_index <= index:
                    continue
                pair_candidate = _transfer_pair_candidate(left, right, window)
                if pair_candidate is not None:
                    candidates.append(pair_candidate)

    choices: dict[str, list[tuple[int, str, str]]] = {}
    for distance, left_id, right_id, flow_type in candidates:
        choices.setdefault(left_id, []).append((distance, right_id, flow_type))
        choices.setdefault(right_id, []).append((distance, left_id, flow_type))

    best: dict[str, tuple[str, str]] = {}
    for transaction_id, options in choices.items():
        minimum = min(option[0] for option in options)
        nearest = [option for option in options if option[0] == minimum]
        if len(nearest) == 1:
            _, other_id, flow_type = nearest[0]
            best[transaction_id] = (other_id, flow_type)

    paired_groups = manual_pair_count + len(cross_currency_pairs)
    for distance, left_id, right_id, flow_type in sorted(candidates):
        if left_id in paired or right_id in paired:
            continue
        if best.get(left_id) != (right_id, flow_type):
            continue
        if best.get(right_id) != (left_id, flow_type):
            continue
        _pair(by_id[left_id], by_id[right_id], flow_type, distance)
        paired.update({left_id, right_id})
        paired_groups += 1

    ambiguous = 0
    unmatched = 0
    for row in rows:
        if id(row) in protected:
            continue
        transaction_id = row.get("transaction_id", "")
        if transaction_id in paired:
            continue
        if transaction_id in choices:
            row["reconciliation_status"] = "ambiguous"
            row["reconciliation_confidence"] = "0.00"
            if (
                row.get("needs_review") == "true"
                and bool(review_reason_tokens(row.get("review_reasons", "")))
                and REVIEW_REASON_ACCOUNTING_FLOW
                not in review_reason_tokens(row.get("review_reasons", ""))
                and DUPLICATE_REVIEW_PROMOTED_FLAG not in _tokens(row.get("flags", ""))
            ):
                row["flags"] = _append_token(
                    row.get("flags", ""), AMBIGUITY_PRIOR_REVIEW_FLAG
                )
            release_duplicate_review_ownership(row)
            set_review_reason(row, REVIEW_REASON_ACCOUNTING_FLOW, True)
            row["flags"] = _append_token(row.get("flags", ""), AMBIGUITY_FLAG)
            row["reason"] = _append_reason(row.get("reason", ""), AMBIGUITY_REASON)
            if row.get("flow_source") not in {"rule", "correction"}:
                row["flow_type"] = "unresolved"
                row["flow_source"] = "reconciliation"
            ambiguous += 1
        elif row.get("reconciliation_status") == "unmatched":
            unmatched += 1
        elif row.get("flow_type") in TRANSFER_FLOW_TYPES:
            row["reconciliation_status"] = "unmatched"
            unmatched += 1

    synchronize_review_states(
        (row for row in rows if id(row) not in protected),
        legacy=False,
    )
    valuations = valuation_summary(rows)
    return {
        "transaction_count": len(rows),
        "paired_groups": paired_groups,
        "paired_transactions": len(paired),
        "ambiguous_transactions": ambiguous,
        "unmatched_transactions": unmatched,
        "unresolved_transactions": sum(
            1 for row in rows if row.get("flow_type") == "unresolved"
        ),
        "cross_currency_paired_groups": len(cross_currency_pairs),
        "matched_exchange_valuations": len(cross_currency_pairs),
        "missing_valuation_transactions": valuations["missing_count"],
        "estimated_valuation_transactions": valuations["estimated_count"],
        "balance_reconciliation": _balance_reconciliation(
            statement_rows if statement_rows is not None else rows
        ),
    }


def reconciliation_date_window(config: Config) -> int:
    raw_reconciliation = config.get("reconciliation", {})
    reconciliation = (
        raw_reconciliation if isinstance(raw_reconciliation, Mapping) else {}
    )
    value = reconciliation.get("date_window_days", 3)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 31:
        raise ValueError(
            "Config field reconciliation.date_window_days must be an integer from 0 to 31"
        )
    return value


def validate_reconciliation_config(config: Config) -> None:
    raw = config.get("reconciliation")
    if raw is None:
        reconciliation_date_window(config)
        return
    if not isinstance(raw, Mapping):
        raise ValueError("Config field reconciliation must be a JSON object")
    reconciliation_date_window(config)
    for field in ("exchange_debit_markers", "foreign_deposit_markers"):
        if field not in raw:
            continue
        value = raw[field]
        if not isinstance(value, list):
            raise ValueError(
                f"Config field reconciliation.{field} must be a JSON array"
            )
        if not value:
            raise ValueError(f"Config field reconciliation.{field} must not be empty")
        markers: list[str] = []
        for index, marker in enumerate(value):
            if not isinstance(marker, str) or not marker.strip():
                raise ValueError(
                    f"Config field reconciliation.{field}[{index}] "
                    "must be a non-empty string"
                )
            markers.append(marker.strip().casefold())
        if len(markers) != len(set(markers)):
            raise ValueError(
                f"Config field reconciliation.{field} must not contain duplicates"
            )
    if "exchange_rate_spread_tolerance" in raw:
        value = raw["exchange_rate_spread_tolerance"]
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            parsed = None
        else:
            try:
                parsed = Decimal(str(value))
            except InvalidOperation, ValueError:
                parsed = None
        if parsed is None or not parsed.is_finite() or parsed < 0 or parsed > 1:
            raise ValueError(
                "Config field reconciliation.exchange_rate_spread_tolerance "
                "must be a number from 0 to 1"
            )


def transaction_direction(row: dict[str, str]) -> str | None:
    amount = _amount(row)
    if amount is None:
        amount = _amount_from_field(row, "posted_amount")
    if amount is None or amount == 0:
        return None
    return "inflow" if amount > 0 else "outflow"


def _reset_reconciliation(row: dict[str, str]) -> None:
    flags = row.get("flags", "")
    if AMBIGUITY_FLAG in _tokens(flags):
        prior_review = AMBIGUITY_PRIOR_REVIEW_FLAG in _tokens(flags)
        current_review = row.get("needs_review", "")
        row["flags"] = _remove_token(
            _remove_token(flags, AMBIGUITY_FLAG), AMBIGUITY_PRIOR_REVIEW_FLAG
        )
        row["reason"] = _remove_reason(row.get("reason", ""), AMBIGUITY_REASON)
        if current_review != "false":
            if prior_review:
                release_duplicate_review_ownership(row)
            row["needs_review"] = "true" if prior_review else "false"
        set_review_reason(
            row,
            REVIEW_REASON_ACCOUNTING_FLOW,
            prior_review,
        )
    row["transfer_group_id"] = ""
    row["paired_transaction_id"] = ""
    row["reconciliation_status"] = "not_applicable"
    row["reconciliation_confidence"] = ""
    if row.get("flow_source") == "reconciliation":
        row["flow_type"] = ""
        row["flow_source"] = ""


def _manual_pair_groups(
    rows: list[dict[str, str]],
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    invalid: list[dict[str, str]] = []
    for row in rows:
        marker = manual_pair_marker(row)
        has_marker = any(
            token.startswith(MANUAL_PAIR_FLAG_PREFIX)
            for token in _tokens(row.get("flags", ""))
        )
        if marker:
            groups.setdefault(marker, []).append(row)
        elif has_marker:
            invalid.append(row)
    return groups, invalid


def _apply_manual_pairs(
    groups: dict[str, list[dict[str, str]]],
    invalid_rows: list[dict[str, str]],
    protected: set[int],
) -> tuple[set[str], int]:
    paired: set[str] = set()
    pair_count = 0
    for pair_rows in groups.values():
        if len(pair_rows) != 2 or any(id(row) in protected for row in pair_rows):
            invalid_rows.extend(row for row in pair_rows if id(row) not in protected)
            continue
        left, right = pair_rows
        try:
            validate_manual_pair_facts(left, right)
        except ManualPairError:
            invalid_rows.extend(pair_rows)
            continue
        _pair_manual(left, right)
        paired.update({left["transaction_id"], right["transaction_id"]})
        pair_count += 1
    for row in invalid_rows:
        if id(row) in protected:
            continue
        row["flow_type"] = "unresolved"
        row["flow_source"] = "reconciliation"
        row["reconciliation_status"] = "unmatched"
        row["reconciliation_confidence"] = "0.00"
        row["flags"] = _append_token(
            row.get("flags", ""),
            MANUAL_PAIR_INVALID_FLAG,
        )
        row["reason"] = _append_reason(
            row.get("reason", ""),
            MANUAL_PAIR_INVALID_REASON,
        )
        set_review_reason(row, REVIEW_REASON_ACCOUNTING_FLOW, True)
    return paired, pair_count


def _pair_manual(
    left: dict[str, str],
    right: dict[str, str],
) -> None:
    group_id = manual_pair_marker(left)
    if not group_id or manual_pair_marker(right) != group_id:
        raise AssertionError("manual pair membership validation was skipped")
    for row, other in ((left, right), (right, left)):
        row["category"] = "Internal Transfer"
        row["flow_type"] = "internal_transfer"
        row["flow_source"] = "correction"
        row["transfer_group_id"] = group_id
        row["paired_transaction_id"] = other["transaction_id"]
        row["reconciliation_status"] = "paired"
        row["reconciliation_confidence"] = "1.00"
        row["flags"] = _remove_token(
            _remove_token(row.get("flags", ""), "uncategorized"),
            MANUAL_PAIR_INVALID_FLAG,
        )
        row["reason"] = _remove_reason(
            row.get("reason", ""),
            MANUAL_PAIR_INVALID_REASON,
        )
        set_review_reason(row, REVIEW_REASON_CATEGORY, False)
        set_review_reason(row, REVIEW_REASON_CATEGORY_SUGGESTION, False)
        set_review_reason(row, REVIEW_REASON_ACCOUNTING_FLOW, False)


def derive_flow_type(row: dict[str, str]) -> None:
    existing = row.get("flow_type", "")
    if existing in ALLOWED_FLOW_TYPES and row.get("flow_source") in {
        "rule",
        "correction",
        "structural",
    }:
        return

    category = row.get("category", "")
    amount = _amount(row)
    if amount is None:
        amount = _amount_from_field(row, "posted_amount")
    account_type = row.get("account_type", "unknown")
    if account_type not in ALLOWED_ACCOUNT_TYPES:
        account_type = "unknown"

    if category == "Income":
        flow_type = "income" if trusted_accounting_provenance(row) else "unresolved"
    elif category == "Credit Card Payment":
        flow_type = (
            "credit_card_payment"
            if trusted_accounting_provenance(row)
            else "unresolved"
        )
    elif category == "Internal Transfer":
        flow_type = (
            "internal_transfer" if trusted_accounting_provenance(row) else "unresolved"
        )
    elif category in {"Savings", "Investments"}:
        flow_type = (
            "investment_transfer"
            if trusted_accounting_provenance(row)
            else "unresolved"
        )
    elif amount is None or amount == 0:
        flow_type = "unresolved"
    elif category in {"", "Unknown", "Other"}:
        flow_type = "unresolved"
    elif amount > 0 and (
        account_type == "credit_card"
        or (account_type == "bank" and _has_refund_evidence(row))
    ):
        flow_type = "refund"
    elif amount < 0:
        flow_type = "expense"
    else:
        flow_type = "unresolved"
    row["flow_type"] = flow_type
    row["flow_source"] = "deterministic"


def _has_refund_evidence(row: Mapping[str, str]) -> bool:
    text = " ".join(
        (
            row.get("merchant", ""),
            row.get("original_description", ""),
        )
    ).casefold()
    return any(marker in text for marker in ("refund", "rebate", "cashback"))


def _transfer_eligible(candidate: _TransferCandidate) -> bool:
    row = candidate.row
    explicit_flow = row.get("flow_source") in {"rule", "correction"}
    if explicit_flow and row.get("flow_type") not in TRANSFER_FLOW_TYPES:
        return False
    return bool(
        row.get("transaction_id")
        and row.get("account_id")
        and row.get("account_type") in {"bank", "credit_card", "investment"}
        and candidate.amount != 0
    )


def _transfer_pair_candidate(
    left: _TransferCandidate,
    right: _TransferCandidate,
    window: int,
) -> tuple[int, str, str, str] | None:
    left_row = left.row
    right_row = right.row
    if left_row["account_id"] == right_row["account_id"]:
        return None
    if all(
        row.get("flow_source") == "deterministic"
        and row.get("flow_type") in EXTERNAL_FLOW_TYPES
        for row in (left_row, right_row)
    ):
        return None
    if left.amount != -right.amount:
        return None
    distance = abs((left.row_date - right.row_date).days)
    if distance > window:
        return None

    outgoing, incoming = (
        (left_row, right_row) if left.amount < 0 else (right_row, left_row)
    )
    out_type = outgoing["account_type"]
    in_type = incoming["account_type"]
    if out_type == "bank" and in_type == "credit_card":
        flow_type = "credit_card_payment"
    elif {out_type, in_type} == {"bank"}:
        flow_type = "internal_transfer"
    elif {out_type, in_type} == {"bank", "investment"}:
        flow_type = "investment_transfer"
    else:
        return None
    if any(
        row.get("flow_source") in {"rule", "correction"}
        and row.get("flow_type") != flow_type
        for row in (left_row, right_row)
    ):
        return None
    return (
        distance,
        left_row["transaction_id"],
        right_row["transaction_id"],
        flow_type,
    )


def _pair(
    left: dict[str, str], right: dict[str, str], flow_type: str, distance: int
) -> None:
    transaction_ids = sorted([left["transaction_id"], right["transaction_id"]])
    digest = hashlib.sha256("|".join(transaction_ids).encode("utf-8")).hexdigest()[:16]
    group_id = f"xfer_{digest}"
    confidence = "1.00" if distance == 0 else "0.95"
    for row, other in ((left, right), (right, left)):
        if row.get("flow_source") not in {"rule", "correction"}:
            row["flow_type"] = flow_type
            row["flow_source"] = "reconciliation"
        row["transfer_group_id"] = group_id
        row["paired_transaction_id"] = other["transaction_id"]
        row["reconciliation_status"] = "paired"
        row["reconciliation_confidence"] = confidence


def _cross_currency_exchange_pairs(
    rows: list[dict[str, str]],
    config: Config,
    protected: set[int],
    excluded_transaction_ids: set[str],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Match statement-labelled HKD exchange debits to foreign deposits."""
    raw_reconciliation = config.get("reconciliation", {})
    settings = raw_reconciliation if isinstance(raw_reconciliation, Mapping) else {}
    exchange_markers = _marker_list(
        settings.get("exchange_debit_markers"),
        ("exchange",),
    )
    deposit_markers = _marker_list(
        settings.get("foreign_deposit_markers"),
        ("deposit",),
    )
    tolerance = _decimal_setting(
        settings.get("exchange_rate_spread_tolerance"),
        Decimal("0.10"),
    )
    groups: dict[
        tuple[str, str],
        tuple[list[dict[str, str]], list[dict[str, str]]],
    ] = defaultdict(lambda: ([], []))
    base_currency = str(config.get("base_currency", "HKD")).upper()

    for row in rows:
        if (
            id(row) in protected
            or row.get("transaction_id", "") in excluded_transaction_ids
            or row.get("account_type") != "bank"
        ):
            continue
        institution = row.get("institution", "").strip()
        account_id = row.get("account_id", "").strip()
        if not institution or not account_id:
            continue
        amount = _amount_from_field(row, "posted_amount")
        if amount is None or amount == 0 or _row_date(row) is None:
            continue
        text = " ".join(
            (
                row.get("merchant", ""),
                row.get("original_description", ""),
            )
        ).casefold()
        key = (row.get("date", ""), institution)
        currency = row.get("posted_currency", "").upper()
        if (
            currency == base_currency
            and amount < 0
            and any(marker in text for marker in exchange_markers)
        ):
            groups[key][0].append(row)
        elif (
            currency not in {"", base_currency}
            and amount > 0
            and any(marker in text for marker in deposit_markers)
        ):
            groups[key][1].append(row)

    candidates: list[
        tuple[
            str,
            str,
            list[dict[str, str]],
            list[dict[str, str]],
            Decimal,
        ]
    ] = []
    for (transaction_date, institution), (base_rows, foreign_rows) in groups.items():
        if not base_rows or len(base_rows) != len(foreign_rows):
            continue
        currencies = {row.get("posted_currency", "").upper() for row in foreign_rows}
        if len(currencies) != 1:
            continue
        currency = next(iter(currencies))
        foreign_total = sum(
            (_absolute_posted_amount(row) for row in foreign_rows), Decimal("0")
        )
        if foreign_total == 0:
            continue
        event_rate = (
            sum((_absolute_posted_amount(row) for row in base_rows), Decimal("0"))
            / foreign_total
        )
        candidates.append(
            (
                transaction_date,
                institution,
                base_rows,
                foreign_rows,
                event_rate,
            )
        )

    cohort_rates: dict[tuple[str, str], Decimal] = {}
    rates_by_cohort: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for _, institution, _, foreign_rows, event_rate in candidates:
        currency = foreign_rows[0].get("posted_currency", "").upper()
        rates_by_cohort[(institution, currency)].append(event_rate)
    for key, rates in rates_by_cohort.items():
        if len(rates) < 2:
            continue
        reference = sum(rates, Decimal("0")) / Decimal(len(rates))
        if all(_rate_within_tolerance(rate, reference, tolerance) for rate in rates):
            cohort_rates[key] = reference

    pairs: list[tuple[dict[str, str], dict[str, str]]] = []
    for transaction_date, institution, base_rows, foreign_rows, _ in candidates:
        currency = foreign_rows[0].get("posted_currency", "").upper()
        reference_rate = configured_exchange_rate(
            config, currency, transaction_date
        ) or cohort_rates.get((institution, currency))
        if reference_rate is None:
            continue
        assignment = _unique_exchange_assignment(
            sorted(base_rows, key=_absolute_posted_amount),
            sorted(foreign_rows, key=_absolute_posted_amount),
            reference_rate,
            tolerance,
        )
        if assignment is not None:
            pairs.extend(assignment)
    return pairs


def _unique_exchange_assignment(
    base_rows: list[dict[str, str]],
    foreign_rows: list[dict[str, str]],
    reference_rate: Decimal,
    tolerance: Decimal,
) -> list[tuple[dict[str, str], dict[str, str]]] | None:
    edges: list[list[int]] = []
    for base in base_rows:
        possible: list[int] = []
        for foreign_index, foreign in enumerate(foreign_rows):
            foreign_amount = _absolute_posted_amount(foreign)
            if (
                foreign_amount != 0
                and base.get("account_id") != foreign.get("account_id")
                and _rate_within_tolerance(
                    _absolute_posted_amount(base) / foreign_amount,
                    reference_rate,
                    tolerance,
                )
            ):
                possible.append(foreign_index)
        edges.append(possible)

    matching = _perfect_exchange_matching(edges, len(foreign_rows))
    if matching is None:
        return None
    for base_index, foreign_index in enumerate(matching):
        if (
            _perfect_exchange_matching(
                edges,
                len(foreign_rows),
                excluded=(base_index, foreign_index),
            )
            is not None
        ):
            return None
    return [
        (base_rows[base_index], foreign_rows[foreign_index])
        for base_index, foreign_index in enumerate(matching)
    ]


def _perfect_exchange_matching(
    edges: list[list[int]],
    foreign_count: int,
    *,
    excluded: tuple[int, int] | None = None,
) -> list[int] | None:
    matched_base = [-1] * foreign_count

    def assign(base_index: int, seen: set[int]) -> bool:
        for foreign_index in edges[base_index]:
            if excluded == (base_index, foreign_index) or foreign_index in seen:
                continue
            seen.add(foreign_index)
            prior_base = matched_base[foreign_index]
            if prior_base == -1 or assign(prior_base, seen):
                matched_base[foreign_index] = base_index
                return True
        return False

    for base_index in range(len(edges)):
        if not assign(base_index, set()):
            return None
    if len(edges) != foreign_count:
        return None
    matching = [-1] * len(edges)
    for foreign_index, base_index in enumerate(matched_base):
        if base_index >= 0:
            matching[base_index] = foreign_index
    return matching if all(index >= 0 for index in matching) else None


def _rate_within_tolerance(
    rate: Decimal, reference_rate: Decimal, tolerance: Decimal
) -> bool:
    return (
        reference_rate > 0 and abs(rate - reference_rate) / reference_rate <= tolerance
    )


def _pair_cross_currency(
    base_row: dict[str, str],
    foreign_row: dict[str, str],
) -> None:
    base_amount = _amount(base_row)
    if base_amount is None:
        raise AssertionError("base exchange leg lost its statement valuation")
    set_matched_exchange_valuation(foreign_row, abs(base_amount))
    transaction_ids = sorted(
        [base_row["transaction_id"], foreign_row["transaction_id"]]
    )
    digest = hashlib.sha256("|".join(transaction_ids).encode("utf-8")).hexdigest()[:16]
    group_id = f"xfer_{digest}"
    for row, other in ((base_row, foreign_row), (foreign_row, base_row)):
        row["category"] = "Internal Transfer"
        row["flow_type"] = "internal_transfer"
        row["flow_source"] = "reconciliation"
        row["transfer_group_id"] = group_id
        row["paired_transaction_id"] = other["transaction_id"]
        row["reconciliation_status"] = "paired"
        row["reconciliation_confidence"] = "1.00"
        row["flags"] = _append_token(
            _remove_token(row.get("flags", ""), "uncategorized"),
            CROSS_CURRENCY_FLAG,
        )
        set_review_reason(row, REVIEW_REASON_CATEGORY, False)
        set_review_reason(row, REVIEW_REASON_CATEGORY_SUGGESTION, False)
        set_review_reason(row, REVIEW_REASON_ACCOUNTING_FLOW, False)


def _marker_list(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list):
        raise AssertionError("reconciliation marker validation was skipped")
    return tuple(item.strip().casefold() for item in value if isinstance(item, str))


def _decimal_setting(value: object, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        parsed = Decimal(str(value))
    except InvalidOperation, ValueError:
        raise AssertionError(
            "reconciliation tolerance validation was skipped"
        ) from None
    return parsed


def _absolute_posted_amount(row: dict[str, str]) -> Decimal:
    return abs(_amount_from_field(row, "posted_amount") or Decimal("0"))


def _amount(row: dict[str, str]) -> Decimal | None:
    try:
        amount = Decimal(row.get("amount_hkd", ""))
    except InvalidOperation, ValueError:
        return None
    return amount if amount.is_finite() else None


def _row_date(row: dict[str, str]) -> date | None:
    try:
        return date.fromisoformat(row.get("date", ""))
    except ValueError:
        return None


def _balance_reconciliation(
    rows: list[dict[str, str]],
) -> BalanceReconciliation:
    groups: dict[StatementBalanceKey, list[dict[str, str]]] = {}
    for row in rows:
        key = _statement_balance_key(row)
        if key is None:
            continue
        groups.setdefault(key, []).append(row)

    accounts: BalanceReconciliation = {}
    for (
        account_id,
        _source_kind,
        _source_value,
        statement_section,
        posted_currency,
    ), statement_rows in sorted(groups.items()):
        source_files = sorted(
            {
                row.get("source_file", "")
                for row in statement_rows
                if row.get("source_file", "")
            }
        )
        statement: StatementBalance = {
            "source_file": source_files[0] if source_files else "",
            "statement_section": statement_section,
            "posted_currency": posted_currency,
            "status": "unavailable",
            "result": "unavailable",
            "opening_evidence_found": False,
            "closing_evidence_found": False,
        }
        opening, opening_problem = _statement_balance(
            statement_rows, "statement_opening_balance", "Opening"
        )
        closing, closing_problem = _statement_balance(
            statement_rows, "statement_closing_balance", "Closing"
        )
        opening_conflict = _has_statement_balance_conflict(statement_rows, "opening")
        closing_conflict = _has_statement_balance_conflict(statement_rows, "closing")
        if opening_conflict:
            opening = None
            opening_problem = "Opening balances conflict."
        if closing_conflict:
            closing = None
            closing_problem = "Closing balances conflict."
        statement["opening_evidence_found"] = opening is not None
        statement["closing_evidence_found"] = closing is not None
        conflicts = [
            *_statement_balance_conflicts(statement_rows, "opening"),
            *_statement_balance_conflicts(statement_rows, "closing"),
        ]
        if conflicts:
            statement["conflicts"] = conflicts
        if opening_conflict or closing_conflict:
            statement["result"] = "conflicting_evidence"
        elif opening is None and closing is None:
            statement["result"] = "missing_both"
        elif opening is None:
            statement["result"] = "missing_opening"
        elif closing is None:
            statement["result"] = "missing_closing"
        if opening_problem or closing_problem:
            problems = [
                problem for problem in (opening_problem, closing_problem) if problem
            ]
            if problems == [
                "Opening balance is unavailable.",
                "Closing balance is unavailable.",
            ]:
                statement["reason"] = "Opening and closing balances are unavailable."
            else:
                statement["reason"] = " ".join(problems)
        elif not posted_currency:
            statement["reason"] = "Posted currency is unavailable."
        else:
            amounts = [
                _amount_from_field(row, "posted_amount") for row in statement_rows
            ]
            if any(amount is None for amount in amounts):
                statement["reason"] = "One or more posted amounts are unavailable."
            else:
                assert opening is not None
                assert closing is not None
                activity = sum(
                    (amount for amount in amounts if amount is not None),
                    Decimal("0"),
                )
                signed_calculated = opening + activity
                liability_calculated = opening - activity
                credit_liability = all(
                    row.get("account_type", "") == "credit_card"
                    for row in statement_rows
                )
                calculated = signed_calculated
                if credit_liability and (
                    liability_calculated == closing
                    or signed_calculated != closing
                    and opening >= 0
                    and closing >= 0
                ):
                    calculated = liability_calculated
                difference = closing - calculated
                statement.update(
                    {
                        "status": "reconciled" if difference == 0 else "difference",
                        "result": "matched" if difference == 0 else "mismatched",
                        "opening_balance": _decimal_text(opening),
                        "closing_balance": _decimal_text(closing),
                        "calculated_closing_balance": _decimal_text(calculated),
                        "difference": _decimal_text(difference),
                    }
                )
        account: AccountBalance = accounts.setdefault(
            account_id,
            {
                "status": "unavailable",
                "result": "unavailable",
                "statements": [],
            },
        )
        account["statements"].append(statement)

    for account in accounts.values():
        results = {statement["result"] for statement in account["statements"]}
        if "mismatched" in results:
            account["status"] = "difference"
            account["result"] = "mismatched"
        elif results == {"matched"}:
            account["status"] = "reconciled"
            account["result"] = "matched"
        elif results - {"matched"}:
            account["reason"] = "One or more statement balance checks are unavailable."
    return accounts


def complete_statement_rows(
    all_rows: list[dict[str, str]],
    represented_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return every row from each statement represented by the selected rows."""
    represented_keys = {
        key
        for row in represented_rows
        if (key := _statement_balance_key(row)) is not None
    }
    return [row for row in all_rows if _statement_balance_key(row) in represented_keys]


def statement_balance_reconciliation(
    rows: list[dict[str, str]],
) -> BalanceReconciliation:
    """Return source-level balance checks for complete statement rows."""
    return _balance_reconciliation(rows)


def _statement_balance_key(row: Mapping[str, str]) -> StatementBalanceKey | None:
    account_id = row.get("account_id", "")
    if not account_id:
        return None
    source_id = row.get("source_id", "").strip()
    return (
        account_id,
        "source_id" if source_id else "source_file",
        source_id or row.get("source_file", ""),
        row.get("statement_section", "").strip(),
        row.get("posted_currency", "").strip().upper(),
    )


def _has_statement_balance_conflict(rows: list[dict[str, str]], kind: str) -> bool:
    return bool(_statement_balance_conflicts(rows, kind))


def _statement_balance_conflicts(
    rows: list[dict[str, str]],
    kind: str,
) -> list[BalanceConflict]:
    marker = f"statement_{kind}_balance_conflict"
    page_marker = f"{marker}_page_"
    field = f"statement_{kind}_balance"
    raw_values = [
        row.get(field, "").strip() for row in rows if row.get(field, "").strip()
    ]
    endpoint_conflict = _balance_values_conflict(raw_values)
    contexts: set[tuple[str, str, str]] = set()
    for row in rows:
        flags = row.get("flags", "").split(";")
        marked = marker in flags
        if not marked and not (endpoint_conflict and row.get(field, "").strip()):
            continue
        marked_pages = sorted(
            {
                flag.removeprefix(page_marker)
                for flag in flags
                if flag.startswith(page_marker)
                and flag.removeprefix(page_marker).isdigit()
            },
            key=int,
        )
        pages = marked_pages or [row.get("source_page", "")]
        for page in pages:
            contexts.add(
                (
                    _safe_source_label(row.get("source_file", "")),
                    page,
                    row.get("statement_section", ""),
                )
            )
    return [
        {
            "source_file": source_file,
            "source_page": source_page,
            "statement_section": statement_section,
            "field": field,
        }
        for source_file, source_page, statement_section in sorted(contexts)
    ]


def _balance_values_conflict(raw_values: list[str]) -> bool:
    values: set[Decimal] = set()
    for raw_value in raw_values:
        try:
            value = Decimal(raw_value)
        except InvalidOperation, ValueError:
            return False
        if not value.is_finite():
            return False
        values.add(value)
    return len(values) > 1


def _safe_source_label(source_file: str) -> str:
    return source_file.replace("\\", "/").rsplit("/", 1)[-1][:128]


def _statement_balance(
    rows: list[dict[str, str]], field: str, label: str
) -> tuple[Decimal | None, str]:
    raw_values = [
        row.get(field, "").strip() for row in rows if row.get(field, "").strip()
    ]
    if not raw_values:
        return None, f"{label} balance is unavailable."
    values: set[Decimal] = set()
    for raw_value in raw_values:
        try:
            value = Decimal(raw_value)
        except InvalidOperation, ValueError:
            return None, f"{label} balance is invalid."
        if not value.is_finite():
            return None, f"{label} balance is invalid."
        values.add(value)
    if len(values) > 1:
        return None, f"{label} balances conflict."
    return next(iter(values)), ""


def _amount_from_field(row: dict[str, str], field: str) -> Decimal | None:
    try:
        amount = Decimal(row.get(field, ""))
    except InvalidOperation, ValueError:
        return None
    return amount if amount.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _append_token(existing: str, token: str) -> str:
    tokens = _tokens(existing)
    if token not in tokens:
        tokens.append(token)
    return ";".join(tokens)


def _remove_token(existing: str, token: str) -> str:
    return ";".join(item for item in _tokens(existing) if item != token)


def _tokens(existing: str) -> list[str]:
    return [item for item in existing.split(";") if item]


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing}; {reason}"


def _remove_reason(existing: str, reason: str) -> str:
    return "; ".join(
        item.strip() for item in existing.split(";") if item.strip() != reason
    )
