# Architecture

Honeymoney is a local-first Python CLI. It stores durable facts in files and
builds replaceable calendar-month views. It has no database or cloud AI path.
The user-gated `rates fetch` command may request public HKMA rates; no financial
row enters that request.

## Data flow

```text
config + profiles + rules + rates + mappings + saved corrections
                              |
explicit statement PATH -> parse and normalize -> import record attempt
                              |
              ready import-record snapshots + workspace index
                              |
whole-workspace identity and overlap -> rules -> local memory -> local Ollama
                              |
saved corrections -> valuation -> duplicate/source repair -> review -> reconciliation
                              |
              posting date, then transaction date, then undated
                              |
views/YYYY-MM/{transactions.csv,review_needed.csv,report.html}
views/undated/{transactions.csv,review_needed.csv,report.html}
```

Honeymoney derives the whole workspace before it splits rows into views. An
account transfer, duplicate group, overlap group, or correction may therefore
connect rows in different months. A month is an output choice, not a compute
boundary.

Rules and the optional local model produce derived suggestions. Saved
corrections remain the final human authority. Statement balance checks use each
complete contributing import record even when a report shows one month.

The only public network path is:

```text
rates fetch + approved currencies and dates
                    |
fixed HKMA HTTPS endpoint -> checked complete pages -> rates.json
```

The fetch path opens no import transaction snapshot before all public response
pages pass local checks.

## Storage authority

The directory that contains the resolved config file is the workspace root.
See [workspace storage](workspace-storage.md) and
[ADR 0008](adr/0008-clean-start-workspace-storage.md).

Authority has four parts:

1. Each import record owns one source lifecycle, immutable attempt history, and
   the normalized transactions from its newest success.
2. The value-free workspace index owns stable source, statement-transaction,
   view-transaction, overlap, duplicate-decision, registered-view, and
   generation identity.
3. User-owned config, profiles, rules, rates, mappings, and saved corrections
   own explicit choices and inputs.
4. Generated views own no durable fact or choice and can be rebuilt.

An import record can exist without being ready. Its first accepted attempt may
fail. A later failure does not replace its newest successful snapshot. A valid
empty success is ready. Plain import retries a record with no success and
rejects a record that already has one.

The workspace index holds IDs, keyed proofs, allocation evidence, small state
markers, membership, and contracts. It holds no date, amount, description,
source path, normalized row, correction value, or reusable cross-workspace
financial digest.

## Identity and overlap

The source and record identity rules retained from ADR 0001 run over the whole
accepted batch before any state changes. File display names never choose an
identity. Replacement and reset reuse a source only through proved namespace
or revision rules. Record matching accepts only unique evidence. Active and
retired owners remain in the index for exact recurrence.

The overlap rules retained from ADR 0003 group exact statement transactions
across sources. Stable view-transaction slots preserve supported multiplicity
without pairing indistinguishable repeated rows. Duplicate choices bind to the
exact reviewed support membership. Changed evidence asks for a new choice.

Saved corrections use stable view-transaction IDs. Replace keeps them. Reset
clears one only when the successful reset set fully supports its removal.

## View derivation

A transaction belongs to the month of its valid posting date. If no posting
date exists, it uses its valid transaction date. With neither date, it belongs
to `views/undated/`.

Serialization fixes row order, headers, quoting, text safety, JSON encoding,
and report bytes. A selected empty view has header-only CSVs and a report with
zero transactions. If one file in a view changes, publication replaces all
three files.

Every refresh computes all expected views, compares deterministic old and new
bytes, and writes only affected logical view units. It uses no month-local
cache or dependency graph. A narrow refresh leaves unrelated missing or edited
views alone. `views rebuild --all` creates every implied view and removes only
registered views no longer implied.

The index records keyed proofs for all derivation inputs at the last full
generation. A direct input edit makes normal writers and narrow rebuilds fail
with `full_rebuild_required`. The user must run `views rebuild --all`.

## Publication and recovery

One accepted state-changing command holds one exclusive workspace lock and
publishes one generation. It completes preflight before it assigns attempt
numbers. It then writes a checked journal containing the exact old and new
bytes for every target.

The publisher stages and flushes complete files and recovery bytes. It installs
non-index targets first, syncs their directories, and replaces the workspace
index last. The index replacement commits the generation. Attempt reports sit
outside rollback replacement; the command finalizes them atomically from
reserved journal facts after it knows which generation won.

A stopped command leaves the journal. Normal commands fail closed while it
exists. `doctor --fix` alone settles it: before index commit the old generation
wins, and at or after index commit the new one wins. Recovery copies recorded
bytes and never reruns financial logic. The journal remains until attempt
reports and cleanup finish.

See [doctor](doctor.md) for audit and repair boundaries.

## Public CLI

`import PATH` accepts one file or folder. `imports list` shows safe labels,
readiness, and statement-transaction counts. `imports show` shows current state
and bounded complete attempt history without values or raw paths.

`views rebuild` accepts the shared period selectors. Status, pending review,
valuation inspection, reports, and rebuilds use the same exclusive selector
language. No selector means the current month. `--all` includes undated.
`report export` writes one explicit standalone HTML file; managed reports stay
inside views.

The removed public contracts are `run`, `--input`, `paths.input`, and ledger
uses of `--output`.

JSON uses schema version 3. It keeps the common envelope and uses
`import_count`, `statement_transaction_count`, `view_transaction_count`,
`import_records`, `views`, and `report_html`.

## Module boundaries

- CLI composition parses commands and coordinates typed services. It does not
  own storage formats or financial rules.
- Import-record storage validates source packages, snapshots, summaries, and
  attempt reports.
- Workspace-index code owns value-free identity and cross-file checks.
- Identity and overlap code owns source, statement-transaction, and
  view-transaction allocation.
- View derivation builds the whole expected workspace output in memory.
- View serialization produces deterministic CSV and HTML bytes.
- Publication code owns locks, journals, staging, sync, index-last commit,
  attempt finalization, and cleanup.
- Doctor code owns full audit and proof-based repair.
- Parser, rules, correction, valuation, review, reconciliation, report, rate,
  and local Ollama modules keep their narrow domain work.

## Privacy boundary

Only synthetic fixtures may enter git or cloud Codex. Real statements, local
workspaces, generated views, and live Ollama transcripts stay local. Ollama
defaults off and may connect only to its checked loopback endpoint.

Managed paths reject symbolic links and unsafe targets. Financial files use
owner-only access. Operational output uses stable codes and bounded safe labels
without amounts, descriptions, raw paths, or statement text.

The HKMA exception sends only approved public currency codes, dates, and page
controls after consent. See [network boundary](network-boundary.md).

## Quality gates

Python 3.14.6 runs formatting, lint, strict types, full unit tests, at least 87
percent branch coverage, package checks, and builds. Fresh offline environments
install and smoke-test both release archives. See
[quality gates](quality-gates.md).
