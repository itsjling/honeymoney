# CSV compatibility

Honeymoney writes generated `transactions.csv` and `review_needed.csv` files in
each view and keeps saved choices in `corrections.csv`. Public headers, column
order, UTF-8 encoding, quoting, and row order are contracts.

## Reversible text safety

Text cells use the product's v1 safety prefix when the first non-space
character is `=`, `+`, `-`, or `@`, when a cell starts with a tab or carriage
return, or when its value already starts with that prefix. A canonical value
that already starts with the prefix gets two prefixes. Reading removes exactly
one. Repeated writes therefore preserve the value and do not add prefixes.

The prefix is an apostrophe followed by the Unicode tag sequence for
`honeymoney-csv-v1`. Files have no BOM or file-level marker. Ordinary leading
apostrophes remain ordinary text.

Amount, balance, confidence, review-state, page, row, canonical slot, and count
columns bypass text safety. A negative amount stays a numeric negative amount.
New non-text columns must join the checked non-text set and have representation
tests.

## Deterministic generated files

CSV output uses UTF-8 without a BOM, normal minimal quoting, LF endings, fixed
headers, canonical numeric forms, and fixed row order. A selected empty view
contains each exact header followed by one LF and no data row.

Honeymoney writes a complete view unit when any expected file changes. Direct
edits to view CSVs never become durable input. A full rebuild restores expected
bytes from import records, the workspace index, and user-owned inputs.

Version 0.2.0 accepts no old cumulative-ledger header as workspace state and
runs no CSV migration. JSON moves from schema version 2 to 3.
