# ADR 0007: Explicit public HKMA rate fetch

- Status: Accepted by the implementation request
- Date: 2026-07-27
- Extends: ADR 0005 local rate import and cache
- Narrowed by: ADR 0008 only where this ADR names ledger loading and
  publication. The user gate, fixed public request, and ban on financial data
  in the request remain binding.

## Context

ADR 0005 requires a person to download the official document before local
import. A built-in fetch can remove that manual step, but a broad HTTP client
could expose financial data or make ordinary commands depend on a service.

HKMA documents the
[daily-rate endpoint and public currency fields](https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/monthly-statistical-bulletin/er-ir/er-eeri-daily/)
and its
[date and page query fields](https://apidocs.hkma.gov.hk/documentation/).
The data set fixes HKD as the base and reports HKD per unit of foreign
currency.

## Decision

Add `honeymoney rates fetch CURRENCY... --start DATE --end DATE`. It prints the
public request before access. Non-interactive use requires
`--allow-network`; an interactive terminal may approve the shown request.

The fetch module accepts only the fixed HKMA HTTPS host and daily-rate path. It
builds a `GET` query from checked public currency codes, the user-supplied date
range normalized to calendar ISO dates, and fixed date, sort, and page fields.
It does not accept a configurable
URL, headers, body, or free-form query values. It does not follow redirects or
use a proxy.

The CLI loads config and the existing local cache before access, but it does
not load transaction or source rows until every response page has passed
provider validation. It then sends the observations through the same merge,
valuation, and recoverable publication operation as local import.

All other commands remain offline. There is no scheduled or automatic fetch.
A saved cache makes later imports, reconciliation, status, and reports
repeatable without the network.

## Consequences

HKMA receives the user's IP address, time, selected currencies, and date range.
It receives no statement or ledger data. A failed request or incomplete page
set changes no cache or ledger file. Tests use an in-memory transport and never
call the live endpoint.

The detailed allowlist and failure model are in
[`../network-boundary.md`](../network-boundary.md).
