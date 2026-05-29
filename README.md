# Honeymoney

Local-first household transaction categorization for exported CSV and statement files.

## Privacy

Honeymoney is designed to run locally. Transaction processing uses local files and local configuration. Ollama support, when enabled, calls a local Ollama endpoint; cloud AI APIs are not used.

Keep real bank statements and private samples out of git. This repo's `.gitignore` excludes `samples/`.

## Current CLI

Run from the repo root:

```bash
python3 -m honeymoney.cli --input ./input --output ./output/categorized.csv --config ./config.json
```

Useful flags:

```bash
python3 -m honeymoney.cli --input ./input --output ./output/categorized.csv --config ./config.json --strict --no-interactive
```

The installed script name is `honeymoney` once the package is installed.

For text-based PDF parsing, install the optional PDF dependencies:

```bash
python3 -m pip install ".[pdf]"
```

## Outputs

The CLI writes:

- `categorized.csv`: normalized transaction rows with snake_case columns.
- `review_needed.csv`: rows requiring manual review, with context and editable correction columns.
- `import_report.json`: run diagnostics, processed files, warnings, duplicate counts, review counts, and Ollama status.

See `examples/expected-output/` for output artifacts generated from `examples/config.json`.

Spending summaries should use `amount_hkd` and exclude `Credit Card Payment` and `Internal Transfer` unless intentionally analyzing cash movement.

Cashflow signs use the household perspective: money leaving the household is negative, and money entering the household is positive. Bank debits and credit-card purchases are therefore negative; salary, refunds, and credits are positive unless a profile supplies an already signed posted amount.

## Config Shape

Minimal example:

```json
{
  "base_currency": "HKD",
  "exchange_rates": {
    "HKD": 1.0,
    "USD": 7.8
  },
  "review_confidence_threshold": 0.8,
  "categories": ["Income", "Groceries", "Dining", "Other", "Unknown"],
  "owners": ["Household", "Justin", "Franchesca", "Unknown"],
  "payment_methods": ["Bank Account", "Credit Card", "Cash", "Unknown"],
  "profiles": ["./profiles/hsbc_hk_bank.json"],
  "profile_mappings": "./profile_mappings.json",
  "rules": "./rules.json",
  "corrections": "./corrections.csv",
  "pdf": {
    "enabled": true,
    "parser": "pdfplumber"
  },
  "ollama": {
    "enabled": false,
    "url": "http://localhost:11434/api/generate",
    "model": "qwen2.5:7b-instruct",
    "batch_size": 20
  },
  "paths": {
    "input": "./input",
    "output": "./output/categorized.csv"
  }
}
```

## Profile Shape

CSV profile example:

```json
{
  "id": "hsbc_hk_bank",
  "account_id": "hsbc_hk_checking",
  "account": "HSBC HK Checking",
  "institution": "HSBC HK",
  "country": "HK",
  "account_currency": "HKD",
  "owner": "Household",
  "payment_method": "Bank Account",
  "date_formats": ["%Y-%m-%d", "%d/%m/%Y"],
  "statement_year": 2026,
  "csv": {
    "columns": {
      "transaction_date": "Date",
      "description": "Description",
      "debit": "Debit",
      "credit": "Credit",
      "original_currency": "Currency"
    }
  }
}
```

When more than one profile matches a file in interactive mode, the CLI prompts for a numbered selection. If `profile_mappings` is configured, it can remember the chosen filename pattern for future runs. Filename mappings can route both CSV exports and PDF statements.

Profiles can map merchant separately from description and use a credit/debit indicator to sign amounts:

```json
{
  "csv": {
    "columns": {
      "transaction_date": "Transaction date",
      "posting_date": "Post date",
      "description": "Description",
      "merchant": "Merchant name",
      "amount": "Billing amount",
      "original_currency": "Billing currency",
      "credit_debit": "Credit / Debit"
    },
    "debit_values": ["Debit"],
    "credit_values": ["Credit"]
  }
}
```

PDF profiles use the same logical column names. For statement tables without a usable header row, set `has_header` to `false` and map fields by zero-based column index:

```json
{
  "pdf": {
    "parser": "pdfplumber",
    "has_header": false,
    "columns": {
      "transaction_date": 0,
      "description": 1,
      "amount": 2,
      "credit_debit": 3
    },
    "debit_values": ["Debit", "DR"],
    "credit_values": ["Credit", "CR"]
  }
}
```

For statement PDFs that contain several non-transaction tables, `required_columns` can skip tables whose header row is not a transaction table. When no table is found and PyMuPDF is installed, the importer records a text-length diagnostic without writing raw statement text to outputs.

Some PDFs collapse multiple visible transaction rows into one extracted table row. Set `split_multiline_rows` to `true` to expand newline-separated cells; use `split_multiline_row_count_columns` to name the date/amount columns that determine the transaction count when descriptions wrap.

For statement tables where each transaction is extracted as a single text cell, `row_regex` can extract named groups such as `transaction_date`, `posting_date`, `description`, and `amount`. Rows that do not match the regex are skipped.

## Rules

Rules are applied by priority, with file order as the tie-breaker.

```json
{
  "version": 1,
  "rules": [
    {
      "id": "parksnshop-groceries",
      "enabled": true,
      "priority": 10,
      "match_type": "keyword",
      "patterns": ["PARKNSHOP"],
      "fields": ["merchant", "original_description"],
      "category": "Groceries",
      "owner": "Household",
      "confidence": 0.98,
      "notes": "Hong Kong supermarket"
    }
  ]
}
```

## Corrections

Corrections apply by exact `transaction_id`. Edit `review_needed.csv` or create a compatible CSV with columns such as:

```csv
transaction_id,category,owner,payment_method,confidence,reason,notes
txn_example,Shopping,Justin,Credit Card,1.0,Manual review,One-off purchase
```

Applying a meaningful correction clears review by default.

## Tests

Run:

```bash
python3 -m unittest discover
```
