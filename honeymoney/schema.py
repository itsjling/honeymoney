from __future__ import annotations


CATEGORIZED_COLUMNS = [
    "transaction_id",
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "merchant",
    "original_description",
    "category",
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
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "merchant",
    "original_description",
    "suggested_category",
    "suggested_owner",
    "suggested_payment_method",
    "category",
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
