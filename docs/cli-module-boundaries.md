# CLI module boundaries

`cli.py` owns command parsing, workspace recovery, identity-state loading,
batch identity resolution, manifest publication, corrections, rules, local
memory, Ollama, reconciliation, and report output. It does not decide parser
coordinates or normalize statement values.

`importers.py` owns statement discovery, profile validation and selection, and
CSV/PDF parsing. It builds each processed source's private ADR 0001 input from
the exact bytes, logical locator, extractor contract, and parser record
locators. CSV uses adapter tag 1 and the physical CSV line. PDF tables use tag
2 with page, table, row, and expansion; word rows use tag 3 with page and
physical line; sectioned rows use tag 4 with page and line. Importers receive a
status callback and never import `cli.py`.

`normalization.py` is pure. It receives source text, profile settings, config,
and an already-computed display `source_file`; it performs no path, directory,
or filesystem work.

`duplicates.py` owns duplicate-candidate evaluation and annotations across the
complete prospective ledger. The CLI uses read-only evaluation for reports
when no source was processed; otherwise it applies the result before it
publishes the ledger.

`provenance.py` owns the checked join from canonical rows to active source
occurrences. It validates overlap membership and occurrence counts and proves
workspace-relative paths. `valuation_inspection.py` uses that join for missing
values. `source_data_review.py` uses it to derive typed source-data review and
value-free evidence for any valuation state. The CLI loads identity state
without recovery for read-only inspection and owns recoverable repair writes.

`rates.py` owns official HKMA document checks, cache checks, cache merging, and
date resolution. It performs no network or ledger I/O. The CLI owns local file
reads and recoverable publication.

`rate_fetch.py` owns the sole public HTTPS boundary. It builds only the fixed
HKMA query, makes direct requests without redirects or proxy routing, limits
response size and time, and collects complete checked pages. It has no ledger
or statement API. The CLI asks for consent before calling it and opens ledger
state only after it returns.

These boundaries preserve the identity rule: display source fields never choose
identity ownership. The identity resolver remains the only owner of source and
record resolution.
