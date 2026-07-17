# CSV compatibility

Honeymoney keeps canonical transaction values in memory. Its public CSV
artifacts—`categorized.csv`, `review_needed.csv`, and `corrections.csv`—apply a
display-only safety encoding to text cells so spreadsheet applications do not
interpret statement-controlled text as formulas.

New-format artifacts start with a UTF-8 byte-order mark (BOM). Honeymoney uses
that signature as required safe-format metadata to distinguish its reversible
encoding from older artifacts. Data otherwise uses normal minimal CSV quoting,
so numeric cells remain unquoted unless CSV syntax itself requires quoting.
Consumers should read these artifacts as `utf-8-sig` (or otherwise discard the
BOM before parsing the header).

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

Artifacts without the BOM are read as legacy files without decoding, regardless
of whether their CSV cells are minimally quoted or quote-all. Literal values
such as `'=LEGACY` and `''LEGACY` therefore retain their apostrophes. The file
remains byte-for-byte untouched when read and is migrated to the signed safe
format only when the normal import, correction, or reconcile workflow next
rewrites it.

The BOM must survive any external edit or reserialization. An external tool
that strips it has removed the only unambiguous safe-format discriminator, so
the resulting file is outside Honeymoney's supported reversible round trip and
is conservatively treated as legacy. Because an unsigned safe file is
indistinguishable from a legitimate legacy file, Honeymoney cannot reliably
reject every such case without risking rejection of valid legacy data.

## Canonical non-text columns

Amount, balance, confidence, review-state, page, and row columns bypass the
text encoding. In particular, a legitimate amount such as `-12.34` remains
`-12.34`, not `'-12.34`. Headers, column order, paths, file permissions, JSON
responses, and recoverable generation publishing are unchanged.

New public text columns are safe by default. A new numeric or other canonical
non-text column must be added to `CANONICAL_CSV_COLUMNS` and covered by a
negative-value or representation-preservation test.
