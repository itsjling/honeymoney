from __future__ import annotations

from honeymoney.contracts import Config

PREVIOUS_SOURCE_OCCURRENCE_COLUMNS = [
    "transaction_id",
    "source_id",
    "source_namespace_id",
    "source_revision",
    "source_record_id",
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "account_type",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "statement_opening_balance",
    "statement_closing_balance",
    "merchant",
    "original_description",
    "category",
    "flow_type",
    "flow_source",
    "transfer_group_id",
    "paired_transaction_id",
    "reconciliation_status",
    "reconciliation_confidence",
    "owner",
    "payment_method",
    "confidence",
    "needs_review",
    "reason",
    "flags",
    "notes",
    "source_file",
    "source_page",
    "source_row",
]

PRE_STATEMENT_SECTION_SOURCE_OCCURRENCE_COLUMNS = [
    *PREVIOUS_SOURCE_OCCURRENCE_COLUMNS[:18],
    "valuation_source",
    "valuation_status",
    *PREVIOUS_SOURCE_OCCURRENCE_COLUMNS[18:33],
    "review_reasons",
    *PREVIOUS_SOURCE_OCCURRENCE_COLUMNS[33:],
]

PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS = [
    *PRE_STATEMENT_SECTION_SOURCE_OCCURRENCE_COLUMNS[:22],
    "statement_section",
    *PRE_STATEMENT_SECTION_SOURCE_OCCURRENCE_COLUMNS[22:],
]

_SOURCE_VALUATION_END = (
    PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS.index("valuation_status") + 1
)
SOURCE_OCCURRENCE_COLUMNS = [
    *PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS[:_SOURCE_VALUATION_END],
    "valuation_rate_date",
    "valuation_provider",
    *PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS[_SOURCE_VALUATION_END:],
]

PREVIOUS_CATEGORIZED_COLUMNS = [
    "transaction_id",
    "canonical_group_id",
    "canonical_slot",
    "provenance_status",
    "source_occurrence_count",
    *PREVIOUS_SOURCE_OCCURRENCE_COLUMNS[1:],
]

PRE_STATEMENT_SECTION_CATEGORIZED_COLUMNS = [
    "transaction_id",
    "canonical_group_id",
    "canonical_slot",
    "provenance_status",
    "source_occurrence_count",
    *PRE_STATEMENT_SECTION_SOURCE_OCCURRENCE_COLUMNS[1:],
]

PRE_RATE_METADATA_CATEGORIZED_COLUMNS = [
    "transaction_id",
    "canonical_group_id",
    "canonical_slot",
    "provenance_status",
    "source_occurrence_count",
    *PRE_RATE_METADATA_SOURCE_OCCURRENCE_COLUMNS[1:],
]

CATEGORIZED_COLUMNS = [
    "transaction_id",
    "canonical_group_id",
    "canonical_slot",
    "provenance_status",
    "source_occurrence_count",
    *SOURCE_OCCURRENCE_COLUMNS[1:],
]

PREVIOUS_REVIEW_NEEDED_COLUMNS = [
    "transaction_id",
    "canonical_group_id",
    "canonical_slot",
    "provenance_status",
    "source_occurrence_count",
    "source_id",
    "source_namespace_id",
    "source_revision",
    "source_record_id",
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "account_type",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "statement_opening_balance",
    "statement_closing_balance",
    "merchant",
    "original_description",
    "suggested_category",
    "suggested_flow_type",
    "transfer_group_id",
    "paired_transaction_id",
    "reconciliation_status",
    "suggested_owner",
    "suggested_payment_method",
    "category",
    "flow_type",
    "owner",
    "payment_method",
    "confidence",
    "reason",
    "flags",
    "notes",
    "source_file",
    "source_page",
    "source_row",
]

PRE_STATEMENT_SECTION_REVIEW_NEEDED_COLUMNS = [
    *PREVIOUS_REVIEW_NEEDED_COLUMNS[:22],
    "valuation_source",
    "valuation_status",
    *PREVIOUS_REVIEW_NEEDED_COLUMNS[22:38],
    "review_reasons",
    "review_reason_labels",
    *PREVIOUS_REVIEW_NEEDED_COLUMNS[38:],
]

PRE_RATE_METADATA_REVIEW_NEEDED_COLUMNS = [
    *PRE_STATEMENT_SECTION_REVIEW_NEEDED_COLUMNS[:26],
    "statement_section",
    *PRE_STATEMENT_SECTION_REVIEW_NEEDED_COLUMNS[26:],
]

_REVIEW_VALUATION_END = (
    PRE_RATE_METADATA_REVIEW_NEEDED_COLUMNS.index("valuation_status") + 1
)
REVIEW_NEEDED_COLUMNS = [
    *PRE_RATE_METADATA_REVIEW_NEEDED_COLUMNS[:_REVIEW_VALUATION_END],
    "valuation_rate_date",
    "valuation_provider",
    *PRE_RATE_METADATA_REVIEW_NEEDED_COLUMNS[_REVIEW_VALUATION_END:],
]


ALLOWED_CATEGORIES = {
    "Income",
    "Rent/Mortgage",
    "Utilities",
    "Groceries",
    "Dining",
    "Transport",
    "Octopus",
    "Cash",
    "Shopping",
    "Travel",
    "Health",
    "Subscriptions",
    "Entertainment",
    "Insurance",
    "Taxes",
    "Gifts",
    "Household",
    "Savings",
    "Investments",
    "Credit Card Payment",
    "Internal Transfer",
    "Other",
    "Unknown",
}


ALLOWED_OWNERS = {"Household", "Justin", "Franchesca", "Unknown"}


ALLOWED_PAYMENT_METHODS = {
    "Bank Account",
    "Credit Card",
    "Debit Card",
    "Octopus",
    "Cash",
    "Brokerage",
    "Unknown",
}


ALLOWED_ACCOUNT_TYPES = {"bank", "credit_card", "investment", "unknown"}


ALLOWED_FLOW_TYPES = {
    "income",
    "expense",
    "refund",
    "internal_transfer",
    "credit_card_payment",
    "investment_transfer",
    "unresolved",
}


def allowed_categories(config: Config | None = None) -> set[str]:
    categories = config.get("categories") if config else None
    if isinstance(categories, (list, tuple, set)):
        return {str(category) for category in categories}
    return set(ALLOWED_CATEGORIES)


def allowed_owners(config: Config | None = None) -> set[str]:
    owners = config.get("owners") if config else None
    if isinstance(owners, (list, tuple, set)):
        return {str(owner) for owner in owners}
    return set(ALLOWED_OWNERS)


def allowed_payment_methods(config: Config | None = None) -> set[str]:
    payment_methods = config.get("payment_methods") if config else None
    if isinstance(payment_methods, (list, tuple, set)):
        return {str(method) for method in payment_methods}
    return set(ALLOWED_PAYMENT_METHODS)
