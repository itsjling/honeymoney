# Parse a statement without a workspace

```bash
honeymoney parse statement.pdf --profile mox_credit_card_pdf --json
```

`parse` reads one explicit CSV or text-based PDF and returns source facts. It
creates no workspace, import record, category, correction, or generated view.
It loads no nearby config, rules, memory, rates, or account bindings. It does
not call Ollama, perform valuation, or access the network. The caller owns its
records, categories, review decisions, and original file.

Choose one bundled profile:

| Profile ID | Input |
| --- | --- |
| `hsbc_one_pdf` | HSBC One PDF |
| `hsbc_hk_credit_card_pdf` | HSBC HK credit card PDF |
| `mox_bank_pdf` | Mox bank PDF |
| `mox_credit_card_pdf` | Mox credit card PDF |
| `mox_credit_card` | Mox credit card CSV |

The command does not select a profile or load a local profile for you. It
rejects a profile that does not support the input type. A profile describes a
known statement layout. Selecting it does not prove that the file matches.
Some bundled profiles use a fixed statement year when dates omit their year.
The result warns when a profile has this fixed-year assumption. Check all dates
against the original statement.

The HSBC One profile reads the statement date from the PDF text when present.
If that date is absent, it can read an `eStatementFile_YYYYMMDD` date from the
input filename. Keep that filename when you need to reproduce the result. The
filename does not appear in the JSON output, and `source_sha256` covers file
bytes only.

## JSON contract

Success uses the existing schema 3 envelope with `command: "parse"` and
`status: "success"`. The result is in `data`, and `artifacts` stays empty.
Source warnings live in `data.warnings`. The envelope's `warnings` stays empty.

The data contract has `parse_schema_version: 1` and these fields:

- `engine` contains the name `honeymoney` and package version `0.2.4`.
- `profile_id` and `profile_sha256` identify the bundled profile. The digest
  covers its validated JSON with sorted keys, compact separators, UTF-8
  encoding, and unescaped Unicode.
- `source_sha256` covers the exact bytes used for parsing. The command captures
  one bounded, stable source snapshot and parses those bytes.
- `page_count` gives the total PDF page count, including pages without
  transactions. Its value is `null` for CSV.
- `statement_metadata` contains an explicit printed statement date, statement
  period, and the one-based page that supports all returned dates. Missing
  fields use JSON `null`. Honeymoney does not turn a period end date into a
  statement date.
- `rows` contains normalized source facts in parser order.
- `warnings` contains source-data warnings and parser warnings.
- `balance_checks` contains source-level statement balance checks grouped by
  profile account ID. Each account has `status`, `result`, and `statements`.

Each row contains only these string fields. Missing values use empty strings:

```text
date transaction_date posting_date
account_id account account_type institution country
original_amount original_currency posted_amount posted_currency
statement_opening_balance statement_closing_balance statement_section
merchant original_description source_page source_row
```

Amounts are signed decimal strings. Outflows are negative, and inflows are
positive. This sign is not a category or an accounting-flow decision. When a
row has an invalid source amount, both amount fields are empty and the result
includes a warning. Such a row makes its balance check unavailable instead of
counting as zero. Dates use ISO format when the parser can resolve them. Page
numbers are one-based. CSV rows have no source page. Row references follow the
selected parser's existing locator format. Account IDs and names come from the
profile rather than a workspace binding. Categories, flow types, confidence,
review state, owner, valuation, FX estimates, and workspace IDs are absent.

The JSON includes financial facts by design. Human output gives only a row
count. Errors and source labels do not echo source paths or filenames. Balance
checks and warnings use `statement.pdf` or `statement.csv` as a display label.
Protect the JSON output like the original statement.

## Checks and limits

Balance results distinguish `matched`, `mismatched`, and missing or conflicting
evidence. An unavailable check does not count as a match. The calculation uses
complete parsed rows and the parser's account and currency sections. It does
not run whole-workspace transfer reconciliation.

A successful parse proposes rows but does not claim that each row is complete
or correct. A matched balance alone cannot prove completeness. Keep page and
row evidence, expose warnings and gaps, detect overlaps, and preserve later
corrections when you parse a document again.

The command does not run OCR on scans. Zero rows fail with `parse_no_rows`,
including a valid empty statement. This interface cannot prove whether a file
is empty or unsupported. Review the original in that case. Existing size,
page, text, and row limits still apply.

Errors return exit code 2 and the schema 3 error envelope with these stable
codes: `parse_unknown_profile`, `parse_unsupported_type`,
`parse_invalid_source`, `parse_profile_type_mismatch`,
`parse_dependency_missing`, `parse_failed`, and `parse_no_rows`. Invalid
command syntax keeps the existing `usage_error` code.

## Read dates without a parser profile

```bash
honeymoney statement-metadata statement.pdf --json
```

`statement-metadata` reads the same `statement_metadata` object used by
`parse`. It does not select a profile or require transaction rows, so it also
works for recognized statement headers that Honeymoney cannot import. Its data
object contains `source_sha256`, `page_count`, `engine`, and
`statement_metadata`. It uses the schema 3 envelope with
`command: "statement-metadata"`.

The command accepts text-based PDFs only. It reads explicit issue dates and
period labels. An explicit `Statement date` label is supported for any issuer;
unlabeled dates and billing periods require known HSBC, Mox, or Hang Seng
header layouts. It does not use a
filename, transaction date, payment due date, or inferred period end. Human
output contains no dates. Errors use
`statement_metadata_unsupported_type`, `statement_metadata_invalid_source`,
`statement_metadata_dependency_missing`, or `statement_metadata_failed`.
