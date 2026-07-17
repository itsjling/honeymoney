# CSV compatibility

Honeymoney keeps canonical transaction values in memory. Its public CSV
artifacts—`categorized.csv`, `review_needed.csv`, and `corrections.csv`—apply a
display-only safety encoding to text cells so spreadsheet applications do not
interpret statement-controlled text as formulas.

New-format artifacts quote every header and data cell. The fully quoted header
is the format marker: standard CSV readers still return the same public column
names, while Honeymoney uses it to distinguish its reversible encoding from
older unquoted artifacts.

## Reversible text encoding

A text cell receives one leading apostrophe when:

- its first non-whitespace character is `=`, `+`, `-`, or `@`;
- it starts with a tab or carriage return, including after leading spaces; or
- its canonical value already starts with an apostrophe.

Examples:

| Canonical value | CSV cell |
|---|---|
| `=SUM(A1:A2)` | `'=SUM(A1:A2)` |
| `  @VALUE` | `'  @VALUE` |
| tab followed by `VALUE` | apostrophe, tab, then `VALUE` |
| `'=literal text` | `''=literal text` |
| `Ordinary text` | `Ordinary text` |

When Honeymoney reads its ledger or corrections file, it removes exactly the
encoding apostrophe. Rewriting an artifact therefore produces the same cells
instead of accumulating prefixes. This decoding applies only at Honeymoney's
own public-artifact read boundaries; statement input is never rewritten before
normalization, transaction identity, matching, or accounting logic.

Legacy artifacts with an unquoted header are read without decoding. Literal
values such as `'=LEGACY` and `''LEGACY` therefore retain their apostrophes.
The file remains byte-for-byte untouched when read and is migrated to the
fully quoted safe format only when the normal import, correction, or reconcile
workflow next rewrites it.

## Canonical non-text columns

Amount, balance, confidence, review-state, page, and row columns bypass the
text encoding. In particular, a legitimate amount such as `-12.34` remains
`-12.34`, not `'-12.34`. Headers, column order, paths, file permissions, JSON
responses, and recoverable generation publishing are unchanged.

New public text columns are safe by default. A new numeric or other canonical
non-text column must be added to `CANONICAL_CSV_COLUMNS` and covered by a
negative-value or representation-preservation test.
