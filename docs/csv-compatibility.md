# CSV compatibility

Honeymoney keeps canonical transaction values in memory. Its public CSV
artifacts—`categorized.csv`, `review_needed.csv`, and `corrections.csv`—apply a
display-only safety encoding to text cells so spreadsheet applications do not
interpret statement-controlled text as formulas.

Artifacts retain their ordinary UTF-8 header bytes and use normal minimal CSV
quoting, so existing `utf-8` CSV readers continue to see the same column names
and numeric cells remain unquoted unless CSV syntax itself requires quoting.
There is no file-level format marker: neither a BOM nor a quote style enables
decoding.

## Reversible text encoding

A text cell receives a product-specific v1 prefix when:

- its first non-whitespace character is `=`, `+`, `-`, or `@`;
- it starts with a tab or carriage return, including after leading spaces; or
- its canonical value already starts with that exact v1 prefix.

The prefix is an apostrophe followed by the invisible Unicode tag sequence for
`honeymoney-csv-v1`. The apostrophe neutralizes spreadsheet interpretation; the
long, versioned tag makes an encoded cell self-identifying without relying on
document metadata. The notation `<HMCSV-v1>` below represents that entire
invisible prefix.

Examples:

| Canonical value | CSV cell |
|---|---|
| `=SUM(A1:A2)` | `<HMCSV-v1>=SUM(A1:A2)` |
| `  @VALUE` | `<HMCSV-v1>  @VALUE` |
| tab followed by `VALUE` | `<HMCSV-v1>`, tab, then `VALUE` |
| a value beginning with `<HMCSV-v1>` | two consecutive `<HMCSV-v1>` prefixes |
| `'=literal text` | `'=literal text` |
| `Ordinary text` | `Ordinary text` |

When Honeymoney reads its ledger or corrections file, it removes exactly one
v1 prefix from every text cell that starts with it. A canonical value already
starting with the prefix is doubled on write, so the mapping is injective and
repeated rewrites do not accumulate or lose prefixes. This decoding applies
only at Honeymoney's own public-artifact read boundaries; statement input is
never rewritten before normalization, transaction identity, matching, or
accounting logic.

Legacy artifacts are compatible regardless of whether they have a UTF-8 BOM or
use minimal or quote-all CSV formatting. Ordinary leading-apostrophe values
such as `'=LEGACY` and `''LEGACY` do not match the v1 prefix and therefore keep
their apostrophes. Only the exact Honeymoney v1 tag sequence is reserved as an
encoded-cell discriminator.

Corrections retain legacy whitespace behavior per cell. Unencoded fields are
trimmed, and a single-space notes cell remains the explicit empty-note sentinel.
Encoded formula-like fields are decoded without trimming so their canonical
leading whitespace survives. This avoids requiring a document marker even
when an ordinary Honeymoney-authored corrections file contains no encoded
cells.

## Canonical non-text columns

Amount, balance, confidence, review-state, page, and row columns bypass the
text encoding. In particular, a legitimate amount such as `-12.34` remains
`-12.34`, not `'-12.34`. Headers, column order, paths, file permissions, JSON
responses, and recoverable generation publishing are unchanged.

New public text columns are safe by default. A new numeric or other canonical
non-text column must be added to `CANONICAL_CSV_COLUMNS` and covered by a
negative-value or representation-preservation test.
