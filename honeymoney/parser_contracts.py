"""Public static contracts for validated profiles and parser results."""

from __future__ import annotations

from typing import TypeAlias, TypedDict

ColumnSource: TypeAlias = str | int


class ProfileColumns(TypedDict, total=False):
    transaction_date: ColumnSource
    posting_date: ColumnSource
    description: ColumnSource
    merchant: ColumnSource
    amount: ColumnSource
    debit: ColumnSource
    credit: ColumnSource
    deposit: ColumnSource
    withdrawal: ColumnSource
    credit_debit: ColumnSource
    account_id: ColumnSource
    account: ColumnSource
    original_currency: ColumnSource
    posted_amount: ColumnSource
    posted_currency: ColumnSource
    statement_opening_balance: ColumnSource
    statement_closing_balance: ColumnSource


class StatementDateSettings(TypedDict):
    regex: str
    date_formats: list[str]


class ParserSettings(TypedDict, total=False):
    columns: ProfileColumns
    parser: str
    has_header: bool
    delimiter: str
    encoding: str
    amount_default_sign: str
    detect_headers: list[str]
    debit_values: list[str]
    credit_values: list[str]
    required_columns: list[str]
    row_regex: str
    join_fields: dict[str, list[str]]
    word_rows: bool | str
    word_rows_only: bool
    word_header_markers: list[str]
    word_table_end_markers: list[str]
    word_columns: dict[str, list[int]]
    split_multiline_rows: bool
    split_multiline_row_count_columns: list[str]
    balance_mappings: list[dict[str, str]]
    sectioned_word_rows: dict[str, object]
    statement_date: StatementDateSettings
    mox_statement: str


class Profile(TypedDict, total=False):
    id: str
    account_id: str
    account: str
    account_type: str
    institution: str
    country: str
    account_currency: str
    owner: str
    payment_method: str
    date_formats: list[str]
    statement_year: int
    skip_descriptions: list[str]
    csv: ParserSettings
    pdf: ParserSettings


class ParserSourceReport(TypedDict, total=False):
    source_file: str
    status: str
    reason: str
    transaction_count: str
    profile_id: str
    parser: str
    binding_id: str
    binding_selection: str
    statement_date: str | None
    statement_date_source_pages: list[int]


class ParseResult(TypedDict):
    transactions: list[dict[str, str]]
    warnings: list[str]
    statement_date: str | None
    statement_date_source_pages: list[int]
