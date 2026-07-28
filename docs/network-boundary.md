# Public rate network boundary

Honeymoney keeps all statement processing local. `honeymoney rates fetch` is
the sole public network exception. It retrieves public daily exchange rates
from the
[Hong Kong Monetary Authority](https://apidocs.hkma.gov.hk/documentation/market-data-and-statistics/monthly-statistical-bulletin/er-ir/er-eeri-daily/).
It does not call an AI service.

## User gate

The command shows the provider, fixed endpoint, currencies, and requested date
range before network access. A person at an interactive terminal must approve
that request. JSON and other non-interactive runs must pass
`--allow-network`. No config setting enables background access.

Import, review, correction, reconciliation, status, valuation inspection, and
reporting do not call the fetch module. Cached rates make all later work
offline.

## Outbound allowlist

The HTTP boundary accepts one request shape:

- scheme: `https`;
- host: `api.hkma.gov.hk`;
- path: the HKMA `er-eeri-daily` public API;
- method: `GET`;
- query keys: `pagesize`, `offset`, `fields`, `choose`, `from`, `to`, `sortby`,
  and `sortorder`;
- fields: `end_of_day` and supported public foreign-currency codes;
- date filter and sort field: `end_of_day`;
- sort order: ascending;
- page size: from 1 through 1000.

HKD is the data set's fixed base currency. The code rejects other hosts, paths,
ports, credentials, fragments, query keys, field names, and sort values before
opening a connection. It uses a direct TLS connection to the fixed host. It
does not follow redirects or use a proxy. TLS keeps certificate and hostname
verification on. The client augments the local system trust with the packaged
`certifi` CA bundle and can use that bundle alone when a local Python install
cannot load its system trust.

## Data that may leave the machine

The request discloses:

- the user's IP address and request time;
- the static Honeymoney public-rate user agent;
- requested foreign currency codes;
- requested start and end dates;
- public page and sort controls.

No code path from a transaction or source row enters the request builder. The
request cannot contain transaction values, descriptions, merchant names,
accounts, owners, statement paths, source IDs, ledger rows, corrections, or
model prompts.

## Inbound checks and writes

Each page has a 15-second timeout and an 8 MiB response limit. Every page must
pass the same provider shape, success, record-count, date, currency, rate, and
base-direction checks as a local rate import. Each record must contain every
requested currency. Dates must stay within the shown range, sort in ascending
order within and across pages, and never overlap another page. The final page
must be shorter than the requested page size.

The CLI collects and checks all pages before it loads ledger rows. A provider
error, timeout, bad document, out-of-range date, repeated page, or incomplete
page causes no cache or ledger write. After success, the shared rate operation
publishes only the cache and any derived valuation generation. Its retained
write protocol restores the prior cache and ledger if publication fails.

Human and versioned JSON errors use these privacy-safe classes:

- `rate_fetch_certificate_verification`
- `rate_fetch_name_resolution`
- `rate_fetch_connection`
- `rate_fetch_timeout`
- `rate_fetch_http_status`
- `rate_fetch_response_malformed`
- `rate_fetch_response_too_large`
- `rate_fetch_pagination_incomplete`

Messages may include an HTTP status but never include request headers, response
bodies, transaction data, account data, or local paths. A retry starts from the
same prior generation.

Tests replace the HTTP transport with in-memory responses and inspect each full
outbound URL. The default suite blocks live non-local sockets.
