# Duplicate candidates

Honeymoney treats duplicate detection as a review aid. It does not delete,
merge, hide, or exclude candidate rows from totals.

## Evidence and match type

The public match type is `exact_same_account_event_v2`. A candidate group must
meet every condition:

- every occurrence has complete identity-v2 metadata and a v2 transaction ID;
- `account_id` is non-empty;
- every occurrence has the same accepted identity-v2 record fingerprint; and
- the group contains at least two distinct `source_id` values.

The record fingerprint follows ADR 0001. It includes the normalized account,
dates, posted and original amounts and currencies, merchant, and original
description. Adjacent dates therefore differ. Equal rows from one statement
source do not form a candidate group. Equal-looking rows from different
accounts also differ.

Source display, paths, source revisions, row positions, input order, and
one-day windows never supply duplicate evidence.

## Recalculation and review state

Import resolves identity and removes replaced source rows before it evaluates
the complete prospective ledger. Combined, sequential, reordered, and
replacement imports therefore produce the same groups.

Each group sorts its occurrence IDs by transaction ID. Candidate rows carry
the `duplicate_suspected` flag, a reason naming the match type, all occurrence
IDs, and the row's counterpart IDs. If duplicate review changes
`needs_review` from false to true, the `duplicate_review_promoted` flag records
that change. Recalculation removes both flags and the reason before applying
current evidence. When evidence disappears, the promotion flag lets
Honeymoney restore the prior false review state. Rows that already needed
review remain queued. If a later correction or check sets review for another
cause, it removes the promotion flag so duplicate cleanup cannot clear that
review state.

Recalculation also removes the retired `Possible duplicate transaction`
reason and its legacy flag. Repeated evaluation is idempotent.

## Diagnostics and privacy

Import, status, pending, report JSON, and HTML use the same evaluated groups.
Operational duplicate diagnostics contain only:

- the documented match type;
- sorted opaque occurrence transaction IDs;
- sorted opaque counterpart transaction IDs; and
- structural group and occurrence counts.

They never contain transaction values, record fingerprints, source IDs,
source revisions, source paths, or display provenance. Ledger and review CSV
reasons use the same opaque identifiers and match type.
