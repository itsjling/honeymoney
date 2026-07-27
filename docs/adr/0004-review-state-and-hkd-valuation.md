# ADR 0004: Explicit review state and HKD valuation

- Status: Accepted by the implementation request
- Date: 2026-07-27
- Narrows: ADR 0003 public CSV headers and mutable review state

## Context

`needs_review` had many writers. A rule, correction, overlap check, or transfer
check could change the boolean without stating which current choice a person
still had to make. The free-text `reason` field could not fix this because it
also records how Honeymoney categorized or processed a row.

`amount_hkd` also hid a key difference. It could hold an HKD amount printed by
a statement, a value made with a fixed rate, or no value. Separate HKD and
foreign-currency account legs could then count one currency purchase twice.

## Decision

Add `review_reasons` after `needs_review` in the ledger and hidden source CSV.
Add it after `confidence` in `review_needed.csv`. It stores a
semicolon-separated set from this fixed vocabulary:

| Token | Plain label |
|---|---|
| `category_decision` | Choose a category |
| `category_suggestion` | Approve the suggested category |
| `accounting_flow` | Resolve the accounting flow |
| `identity_conflict` | Resolve a transaction identity conflict |
| `source_data_issue` | Fix source data |
| `ownership_decision` | Choose an owner |
| `other_decision` | Make another recorded decision |

`needs_review` is now derived: it is `true` exactly when `review_reasons` is not
empty. `reason` remains processing and categorization provenance. Missing HKD
valuation is not a review reason.

Add `valuation_source` and `valuation_status` after `amount_hkd` in the ledger,
review, and hidden source CSVs. Sources are `statement_posted`,
`matched_exchange_leg`, `configured_dated_rate`, `configured_fixed_rate`, and
`missing`. Status is `actual`, `estimated`, or `missing`.

Valuation uses this order:

1. an HKD posted amount on the same statement row;
2. the HKD leg of a strongly matched owned-account currency exchange;
3. an exact-date rate in `dated_exchange_rates`;
4. a fixed fallback in `exchange_rates`;
5. no HKD value.

It never uses a balance. A missing value stays out of HKD totals and appears in
the valuation-completeness count.

Cross-currency matching uses bank accounts owned by the ledger, the same date
and institution, statement text that marks an exchange debit and a foreign
deposit, equal leg counts, one foreign currency per group, and a checked spread
between implied rates. It checks each implied rate against a configured rate or
against at least two consistent exchange events for the same institution and
currency. Honeymoney pairs a group only when exactly one assignment passes
those checks; otherwise it leaves the group unpaired. The default text markers
are `exchange` and `deposit`.
`reconciliation.exchange_debit_markers`,
`reconciliation.foreign_deposit_markers`, and
`reconciliation.exchange_rate_spread_tolerance` may narrow the check. A match
sets both legs to `Internal Transfer` and `internal_transfer`. This accounting
fact overrides an old merchant-category correction on an exchange leg, but it
does not change the correction ID or transaction ID. Later foreign merchant
withdrawals lack the exchange/deposit evidence and stay expenses or refunds.

## Migration

The exact ADR 0003 headers remain valid input. Honeymoney adds empty new cells
in memory, repairs review reasons from current facts, and writes the new header
on the next ledger change. A categorized row with only a stale
`uncategorized` flag loses that flag. An old categorized correction with
`needs_review=true` and no typed reason becomes resolved. A still-pending
correction must use `review_reasons`.

The correction CSV adds `review_reasons`. Old correction headers still load.
The `reconcile` command rewrites old correction and ledger headers in one
recoverable generation. Repeating the command changes no bytes.

Agent JSON envelopes move from schema version 1 to 2. Review rows expose both
tokens and plain labels, and `review_needed.csv` includes both forms. Status,
import, reconcile, and report JSON expose valuation counts.

## Consequences

Review cannot drift from its cause. Valuation gaps stay visible without posing
as category doubt. Currency purchases no longer add both the HKD debit and the
foreign deposit to spending. `categorized.csv` gains `valuation_source`,
`valuation_status`, and `review_reasons`. `review_needed.csv` gains those fields
and `review_reason_labels`. The correction CSV gains `review_reasons`. Clients
that check exact headers must accept the new order.
