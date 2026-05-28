# Project Scope: Local Private Spending Categorization System

## Goal

Build a simple, privacy-preserving system to categorize household financial transactions for a married couple living in Hong Kong with some U.S. accounts.

The system should run locally as much as possible and avoid sending sensitive financial data over the internet.

## Users

- Justin
- Franchesca

## Primary Use Case

I will export transaction files from banks, credit cards, and financial accounts. The system should import those files, clean and normalize the data, categorize each transaction, and output a categorized CSV suitable for budgeting/spending analysis.

## Key Requirements

### 1. Local-First / Privacy

- All transaction processing should run locally.
- Do not send transaction data to cloud APIs unless explicitly enabled later.
- Use local files, local scripts, and local models.
- Preferred local LLM runtime: **Ollama**.
- Preferred local models to support:
  - `qwen2.5:7b-instruct`
  - `llama3.1:8b-instruct`
  - optional lightweight fallback: `llama3.2:3b-instruct`
- Optional embedding model:
  - `nomic-embed-text`

### 2. Input Sources

The system should accept one or more files with potentially different schemas.

Supported input formats:

```text
CSV
PDF
```

CSV support is required for the first version.

PDF support should be included as an input source. It may initially be implemented as a best-effort parser for bank or credit card statements.

Minimum expected input fields, where available:

```text
Date
Description / Merchant
Amount
Currency
Account name
Institution
Transaction type
```

The importer should allow column mapping per institution/account if needed.

For PDFs, the system should attempt to extract transaction tables and normalize them into the same transaction schema used for CSV imports.

### 3. Output

Produce a normalized categorized CSV with these columns:

```text
Transaction_ID
Date
Account
Institution
Country
Currency
Amount
Amount_HKD
Merchant
Original_Description
Category
Owner
Payment_Method
Confidence
Needs_Review
Reason
Notes
```

### 4. Base Currency

- Base household currency: **HKD**.
- Preserve original amount and currency.
- Add `Amount_HKD`.
- For USD conversion, use a configurable exchange rate, default:

```text
1 USD = 7.8 HKD
```

### 5. Categories

Use exactly these categories unless changed in config:

```text
Income
Rent/Mortgage
Utilities
Mobile/Internet
Groceries
Restaurants
Transport
Octopus
Shopping
Travel
Medical/Health
Subscriptions
Entertainment
Fitness
Insurance
Taxes
Gifts/Donations
Household
Savings/Investments
Fees
Credit Card Payment
Internal Transfer
Other
Review Needed
```

### 6. Owner Field

Each transaction should be assigned one of:

```text
Household
Justin
Franchesca
Review Needed
```

Default to `Household` unless rules specify otherwise or confidence is low.

### 7. Categorization Method

Use a hybrid approach:

1. Exact merchant rules
2. Keyword rules
3. Optional embedding similarity against known labeled transactions
4. Local LLM categorization for uncategorized/ambiguous transactions
5. Manual review flag for low-confidence transactions

### 8. Rules System

Create a simple editable **JSON** rules file.

Rules should support:

- exact match against merchant/description
- keyword match against merchant/description
- regex match against merchant/description
- category assignment
- owner assignment
- confidence override
- notes
- enabled/disabled flag

Example `rules.json`:

```json
{
  "version": 1,
  "rules": [
    {
      "id": "parksnshop-groceries",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["PARKNSHOP", "PARKNSHOP HONG KONG"],
      "fields": ["merchant", "description"],
      "category": "Groceries",
      "owner": "Household",
      "confidence": 0.98,
      "notes": "Hong Kong supermarket"
    },
    {
      "id": "wellcome-groceries",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["WELLCOME"],
      "fields": ["merchant", "description"],
      "category": "Groceries",
      "owner": "Household",
      "confidence": 0.98,
      "notes": "Hong Kong supermarket"
    },
    {
      "id": "mtr-transport",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["MTR"],
      "fields": ["merchant", "description"],
      "category": "Transport",
      "owner": "Household",
      "confidence": 0.95,
      "notes": "Hong Kong public transport"
    },
    {
      "id": "octopus",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["OCTOPUS"],
      "fields": ["merchant", "description"],
      "category": "Octopus",
      "owner": "Household",
      "confidence": 0.9,
      "notes": "Default Octopus top-ups to Octopus"
    },
    {
      "id": "netflix-subscription",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["NETFLIX"],
      "fields": ["merchant", "description"],
      "category": "Subscriptions",
      "owner": "Household",
      "confidence": 0.98,
      "notes": "Streaming subscription"
    },
    {
      "id": "apple-review",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["APPLE.COM/BILL", "APPLE"],
      "fields": ["merchant", "description"],
      "category": "Subscriptions",
      "owner": "Review Needed",
      "confidence": 0.7,
      "notes": "Could be subscription, app, hardware, or family purchase"
    },
    {
      "id": "ird-taxes",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["IRD", "INLAND REVENUE"],
      "fields": ["merchant", "description"],
      "category": "Taxes",
      "owner": "Household",
      "confidence": 0.98,
      "notes": "Hong Kong tax authority"
    },
    {
      "id": "credit-card-payment",
      "enabled": true,
      "match_type": "regex",
      "patterns": ["CREDIT\\s+CARD\\s+PAYMENT", "CARD\\s+PAYMENT", "PAYMENT\\s+THANK\\s+YOU"],
      "fields": ["merchant", "description"],
      "category": "Credit Card Payment",
      "owner": "Household",
      "confidence": 0.95,
      "notes": "Do not count as spending"
    },
    {
      "id": "internal-transfer",
      "enabled": true,
      "match_type": "keyword",
      "patterns": ["TRANSFER", "FPS", "ACH", "WIRE"],
      "fields": ["merchant", "description"],
      "category": "Internal Transfer",
      "owner": "Household",
      "confidence": 0.75,
      "notes": "May need review to confirm own-account transfer"
    }
  ]
}
```

### 9. Special Handling

#### Credit Card Payments

- Do not count credit card payments as expenses.
- Categorize them as `Credit Card Payment`.

#### Internal Transfers

- Do not count transfers between own accounts as expenses.
- Categorize them as `Internal Transfer`.

#### Octopus

- Default Octopus top-ups to `Octopus`.
- Do not attempt to split Octopus unless detailed transaction data is available.

#### Salary / Income

- Salary, payroll, interest, and dividends should be categorized as `Income` unless they are transfers.

#### Taxes

- HK IRD, Inland Revenue, IRS, and similar should be `Taxes`.

### 10. Confidence and Review

Each transaction should have:

```text
Confidence: number from 0 to 1
Needs_Review: true/false
Reason: short explanation
```

Flag as `Needs_Review = true` when:

- category is uncertain
- confidence is below configurable threshold, default `0.8`
- transaction may be a transfer or duplicate
- merchant is ambiguous
- local model returns invalid or unsupported category
- PDF extraction is incomplete or uncertain

### 11. PDF Handling

PDF support should be local-only.

Accept PDFs such as:

```text
bank statements
credit card statements
brokerage statements
```

PDF extraction requirements:

- Extract text and/or tables locally.
- Normalize extracted rows into transactions.
- Preserve the source filename.
- Add notes when data came from PDF.
- Flag transactions for review when extraction confidence is low.
- Do not require OCR in the first version unless text extraction fails.

Preferred local Python libraries:

```text
pdfplumber
pymupdf
camelot
tabula-py
```

Initial implementation may use `pdfplumber` or `pymupdf`.

Optional later enhancement:

```text
OCR via tesseract for scanned PDFs
```

### 12. Local LLM Prompting

For LLM categorization, send only batches of uncategorized transactions to the local model.

The model should return valid JSON only.

Required JSON fields:

```text
id
category
owner
confidence
reason
```

The system should validate that:

- category is from the allowed category list
- owner is one of the allowed owners
- confidence is between 0 and 1

### 13. Duplicate Detection

Basic duplicate detection should flag possible duplicates using:

- same date or near date
- same amount
- same merchant/description
- different imported files/accounts

Do not delete duplicates automatically. Add note or review flag.

### 14. Configuration

Use a simple config file for:

- base currency
- exchange rates
- review confidence threshold
- category list
- owner list
- input column mappings
- Ollama model name
- batch size
- paths for input/output/rules
- PDF parser options

Example `config.json`:

```json
{
  "base_currency": "HKD",
  "exchange_rates": {
    "USD": 7.8,
    "HKD": 1.0
  },
  "review_confidence_threshold": 0.8,
  "owners": ["Household", "Justin", "Franchesca", "Review Needed"],
  "categories": [
    "Income",
    "Rent/Mortgage",
    "Utilities",
    "Mobile/Internet",
    "Groceries",
    "Restaurants",
    "Transport",
    "Octopus",
    "Shopping",
    "Travel",
    "Medical/Health",
    "Subscriptions",
    "Entertainment",
    "Fitness",
    "Insurance",
    "Taxes",
    "Gifts/Donations",
    "Household",
    "Savings/Investments",
    "Fees",
    "Credit Card Payment",
    "Internal Transfer",
    "Other",
    "Review Needed"
  ],
  "ollama": {
    "enabled": true,
    "url": "http://localhost:11434/api/generate",
    "model": "qwen2.5:7b-instruct",
    "batch_size": 20
  },
  "pdf": {
    "enabled": true,
    "parser": "pdfplumber",
    "ocr_enabled": false
  },
  "paths": {
    "input": "./input",
    "output": "./output/categorized.csv",
    "rules": "./rules.json"
  }
}
```

### 15. Minimal CLI

Provide a command-line interface, for example:

```bash
python categorize.py --input ./input --output ./output/categorized.csv
```

Optional:

```bash
python categorize.py --config config.json
```

The CLI should process all supported files in the input directory:

```text
.csv
.pdf
```

### 16. Desired Tech Stack

Preferred:

- Python
- pandas
- requests
- local Ollama API
- JSON config/rules

For PDF support:

- pdfplumber or pymupdf

Optional:

- camelot or tabula-py for table extraction
- pytesseract for OCR later
- scikit-learn or sentence-transformers for embeddings
- SQLite for local transaction storage

### 17. Deliverables

Provide:

1. Working local script or small CLI tool
2. Example `config.json`
3. Example `rules.json`
4. Example input CSV
5. Example PDF parsing example or fixture
6. Example categorized output CSV
7. Short README with setup and usage instructions

### 18. Out of Scope for Initial Version

Do not build yet:

- mobile app
- web app
- bank login/scraping
- automatic cloud sync
- cloud AI categorization
- investment performance tracking
- tax reporting
- complex budgeting dashboard
- recurring subscription detection beyond simple merchant rules
- automatic deletion of duplicates
- automatic Octopus transaction splitting
- OCR for scanned PDFs unless trivial to add

## Success Criteria

The initial version is successful if it can:

- import one or more CSV files
- import text-based PDF statements on a best-effort basis
- normalize transactions into a common format
- categorize obvious transactions using JSON rules
- use a local Ollama model for uncertain transactions
- flag low-confidence items for review
- output a clean categorized CSV
- preserve privacy by running locally without cloud APIs
