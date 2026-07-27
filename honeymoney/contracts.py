"""Shared static contracts for the checked financial core."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, TypeAlias, TypedDict

Config: TypeAlias = Mapping[str, object]
GenerationDocuments: TypeAlias = Mapping[Path, str]


class BalanceConflict(TypedDict):
    source_file: str
    source_page: str
    statement_section: str
    field: str


class StatementBalance(TypedDict, total=False):
    source_file: str
    statement_section: str
    posted_currency: str
    status: str
    result: str
    reason: str
    conflicts: list[BalanceConflict]
    opening_balance: str
    closing_balance: str
    calculated_closing_balance: str
    difference: str


class _RequiredAccountBalance(TypedDict):
    status: str
    result: str
    statements: list[StatementBalance]


class AccountBalance(_RequiredAccountBalance, total=False):
    reason: str


BalanceReconciliation: TypeAlias = dict[str, AccountBalance]


class ReconciliationSummary(TypedDict):
    transaction_count: int
    paired_groups: int
    paired_transactions: int
    ambiguous_transactions: int
    unmatched_transactions: int
    unresolved_transactions: int
    cross_currency_paired_groups: int
    matched_exchange_valuations: int
    missing_valuation_transactions: int
    estimated_valuation_transactions: int
    balance_reconciliation: BalanceReconciliation
