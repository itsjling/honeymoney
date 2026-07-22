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
or filesystem work. It also checks duplicate candidates against supplied
retained and incoming rows, and only marks incoming rows.

These boundaries preserve the identity rule: display source fields never choose
identity ownership. The identity resolver remains the only owner of source and
record resolution.
