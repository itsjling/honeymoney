# ADR 0005: Local HKMA daily-rate cache

- Status: Accepted by the implementation request
- Date: 2026-07-27
- Extends: ADR 0004 valuation order and public CSV fields

## Context

ADR 0004 supports exact-date and fixed rates from config. Users still need a
repeatable way to use official daily rates without putting statement data or
rate lookup logic on a cloud service.

HKMA publishes daily exchange rates as HKD per unit of foreign currency.
Weekends and public holidays have no same-day observation.

## Decision

`honeymoney rates import FILE` accepts a downloaded HKMA `er-eeri-daily` JSON
document. It does no network work. Before any write, it checks the response
shape, success header, record count, ISO dates, supported currencies, finite
positive rates, repeated dates, and the configured HKD base direction. A
present supported field must contain a rate; an explicit JSON null is invalid.

The versioned `rates.json` cache stores:

- provider and data set;
- requested transaction date and observed rate date;
- raw rate, HKD base, and foreign quote currency;
- SHA-256 digests of imported documents.

The cache stores no source path or statement text. Repeated imports merge the
same observations and produce the same bytes. A rate conflict for one currency
and date stops the whole import.

The HKMA API defines each supported field, including JPY, KRW, and IDR, as HKD
per one unit of foreign currency. The importer keeps that unit and does not
apply display-table multipliers. It stores equivalent decimal spellings in one
canonical form and compares observations by numeric value.

Resolution uses an observation on the transaction date when present. Otherwise
it uses the latest prior observation no more than seven calendar days old. It
never uses a future observation or crosses a longer gap.

Valuation order is:

1. an HKD amount posted on the statement row;
2. a matched owned-account exchange leg;
3. an exact-date rate in `dated_exchange_rates`;
4. an imported HKMA daily rate;
5. a fixed rate in `exchange_rates`;
6. no HKD value.

HKMA values use `valuation_source=hkma_daily_reference_rate` and
`valuation_status=estimated`. `valuation_rate_date` and `valuation_provider`
show the evidence. Reports call them reference estimates and do not claim that
they show a bank's conversion cost.

When a ledger exists, rate-cache, canonical ledger, review queue, hidden source
rows, and overlap state publish as one recoverable generation. A failed write
restores the prior cache and ledger.

When statements arrive after the rates, the normal import path adds their
requested-date resolutions to the same cache generation. A repeated rate
import reports `changed=false` when every output file already has the requested
content.

## Migration

The prior statement-section ledger, review, and hidden source headers remain
valid input. Honeymoney adds empty `valuation_rate_date` and
`valuation_provider` cells in memory and writes the current headers on the next
ledger change. Old configs without `rate_cache` use `rates.json` beside the
active config, which is the same path that `setup` writes. Honeymoney keeps
this default in memory and does not edit the config. An explicit cache path
always wins.

## Consequences

Offline revaluation stays deterministic after import. Exact configured rates
still let a user override the provider for a date, and fixed config rates
remain compatible as the last estimate. Public CSV clients must accept two new
text columns.
