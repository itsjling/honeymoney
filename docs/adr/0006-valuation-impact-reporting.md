# ADR 0006: Valuation impact reporting

- Status: Accepted by the implementation request
- Date: 2026-07-27
- Extends: ADR 0004 and ADR 0005 valuation reporting

## Context

A single missing-value count cannot say whether a row changes income, spending,
or net cash flow. Transfers and other excluded movements can lack HKD values
without making budget totals incomplete. One combined total also hides how
much comes from statement facts and how much comes from rate estimates.

## Decision

Valuation summaries keep the existing source and status counts and add these
missing-value groups:

- `cash_flow_blocking_missing_count` for non-zero or unknown `income`,
  `expense`, and `refund` posted amounts;
- `excluded_flow_missing_count` for internal transfers, card payments, and
  investment transfers;
- `unresolved_flow_missing_count` for rows whose accounting flow is not yet
  set;
- `zero_amount_missing_count` for confirmed cash-flow rows with an exact zero
  posted amount;
- `other_flow_missing_count` for an unknown flow value.

`missing_count` remains the total of all rows with missing valuation status.
Only the first group makes `cash_flow_complete` false. An unresolved row never
moves into the blocking or excluded group by sign alone.

For finite HKD values, summaries report signed income, spending, refunds, and
net cash flow in three groups:

1. `actual` for statement-posted and matched-exchange values;
2. `estimated` for provider, configured exact-date, and fixed-rate values;
3. `combined_estimate` as their sum.

The source counts remain separate, so the estimated group does not erase its
rate source. Status, report JSON, and HTML use the same period-filtered
canonical rows. The HTML keeps the existing combined tiles, labels them as
combined estimates, and adds the full breakdown, including other flow values.
The older `missing_base_currency_count` field keeps its non-zero finite posted
amount rule. New totals live under `valuation`.

## Consequences

Missing excluded flows remain visible without making confirmed income or
spending look incomplete. Missing unresolved flows still require an accounting
choice. A zero cash-flow row cannot block a total. Combined estimates remain
useful for a household budget, but they are not exact bank conversion costs or
tax valuations.
