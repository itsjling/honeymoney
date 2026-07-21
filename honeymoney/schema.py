from __future__ import annotations

CATEGORIZED_COLUMNS = [
    "transaction_id",
    "identity_version",
    "identity_fingerprint",
    "identity_source_fingerprint",
    "identity_occurrence",
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


REVIEW_NEEDED_COLUMNS = [
    "transaction_id",
    "identity_version",
    "identity_fingerprint",
    "identity_source_fingerprint",
    "identity_occurrence",
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


def allowed_categories(config: dict | None = None) -> set[str]:
    if config and config.get("categories"):
        return {str(category) for category in config["categories"]}
    return set(ALLOWED_CATEGORIES)


def allowed_owners(config: dict | None = None) -> set[str]:
    if config and config.get("owners"):
        return {str(owner) for owner in config["owners"]}
    return set(ALLOWED_OWNERS)


def allowed_payment_methods(config: dict | None = None) -> set[str]:
    if config and config.get("payment_methods"):
        return {str(method) for method in config["payment_methods"]}
    return set(ALLOWED_PAYMENT_METHODS)
