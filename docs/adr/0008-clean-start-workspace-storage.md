# ADR 0008: Clean-start workspace storage and generated views

- Status: Accepted by GitHub issue #108
- Date: 2026-08-08
- Release: 0.2.0
- Supersedes: the storage, authority, migration, publication, and public naming
  rules listed below

## Context

The old workspace used one cumulative `categorized.csv` as saved state and
output. It mixed source history, stable identity, saved choices, recovery, and
reports. Failed attempts, source replacement, overlap, repair, and month output
then depended on one public file serving several roles.

Version 0.2.0 makes a clean break. It does not migrate an old workspace.

## Decision

The directory that contains the resolved `config.json` is the workspace root.
User-owned config, profiles, rules, rates, account mappings, and
`corrections.csv` stay visible. Honeymoney keeps managed state under
`.honeymoney/` and replaceable output under `views/`.

The fixed top-level managed paths are
`.honeymoney/workspace-index.json`, `.honeymoney/import-records/`, the
disposable `.honeymoney/report-preview.html`, `views/<YYYY-MM>/`, and
`views/undated/`. Each import record lives at
`.honeymoney/import-records/<source_id>/`.

Setup creates valid starter inputs, an empty value-free workspace index, and
the import-record container. It creates no statement input folder and no
generated view.

### Durable authority

Each logical statement source has one import record. It contains:

- a disposable summary;
- the normalized statement transactions from its newest successful attempt,
  when one exists; and
- immutable, eight-digit attempt reports for every accepted attempt.

An attempt report has a fixed 64 KiB limit and remains for the life of the
workspace. Rejected preflight work consumes no attempt number. A first failure
creates a visible, not-ready record. A later failed replace or reset does not
remove the newest successful snapshot. A valid zero-row success is ready.

The workspace index is the sole workspace-wide identity authority. It owns
source, statement-transaction, view-transaction, canonical group and slot,
support membership, retired identity, duplicate-decision, registered-view,
generation, and input-proof state. It may store stable IDs, keyed hashes, HMAC
proofs, allocation evidence, small state markers, and schema or software
contracts. It stores no dates, amounts, descriptions, source paths, normalized
rows, correction values, or reusable cross-workspace financial digests.

Saved corrections remain a separate user-visible authority keyed by stable
view-transaction ID. Generated output, rule matches, local memory, local model
output, valuation, reconciliation, and review warnings own no saved decision.

### Identity and overlap

ADR 0001 digest framing, logical-source resolution, record fingerprints,
extractor contracts, conservative matching, allocation-origin recurrence,
collision checks, and privacy rules remain binding. Their state now lives in
import records and the workspace index.

ADR 0003 exact-overlap multiset rule, stable group and slot identity, pooled
multiplicity, exact-membership duplicate decisions, correction carryover, and
privacy rules remain binding. Its canonical transaction is now a view
transaction. Equal repeated rows keep their supported multiplicity; Honeymoney
never invents a pairing.

Replace keeps saved corrections. Reset clears a choice only when the successful
reset set fully supports that choice. Active and retired identity and duplicate
history remain for the workspace life, so exact recurrence can reconnect
without a guess.

### Derivation and views

Every refresh derives the whole workspace from all ready import records. It
resolves overlap, rules, local memory, saved corrections, configured local
model output, valuation, duplicate and source-data repair, review state, and
reconciliation before it splits output by period. Links may cross months.

A transaction uses its valid posting date, then its valid transaction date. A
transaction with neither belongs to the undated view. Each view contains:

```text
transactions.csv
review_needed.csv
report.html
```

Rows and bytes have a fixed order. A selected empty view contains header-only
CSVs and a zero-transaction report. One view directory is one logical write
unit, so any change replaces all three files.

Automatic refresh compares the old and new expected bytes and writes only
affected view units. It does not repair unrelated missing or edited views.
`views rebuild --all` is the full output oracle: it creates every implied view
and removes only registered views no longer implied. Month, range, and undated
rebuilds replace each selected view in full. A range never becomes a durable
folder.

The index stores keyed proofs and contract versions for the last full view
generation. A direct edit to a derivation input requires
`views rebuild --all`; narrow writers fail with `full_rebuild_required`.

### Publication and recovery

One accepted command holds one exclusive workspace lock and publishes one
workspace generation. It stages and flushes complete new files and the old
bytes needed for recovery. It installs non-index targets first, syncs their
directories, and replaces the workspace index last. That last replacement is
the commit point.

Attempt reports sit outside rollback replacement. After the generation result
is known, Honeymoney finalizes them atomically and more than once safely from
reserved journal facts. It keeps the journal until reports and cleanup finish.

Normal commands fail closed while a publication journal remains. Only
`doctor --fix` may settle it. Before the commit point the old generation wins;
at or after it the new generation wins. Recovery uses exact recorded bytes. It
does not rerun parsing, categorization, model calls, valuation, reconciliation,
or report generation. An unfinished attempt becomes success only when commit
proof exists; otherwise it becomes an interrupted failure.

### Doctor and repair

`doctor` audits the full workspace without writing, opening original
statements, calling a model, or using the network. Findings use stable codes and
bounded, safe paths. `doctor --fix` first builds one complete plan. Hard damage
to import records, attempt history, identity, corrections, or conflicting
durable authorities blocks every repair and requires a complete backup restore.

When proof exists, repair may settle publication, rebuild disposable summaries
and generated views, fix managed directories and owner-only access, and remove
a proved stale lock. It never guesses durable facts. It publishes repairs as
one generation and then runs the audit again.

Unsafe paths and symbolic links fail closed. Unknown managed entries warn and
remain untouched. Financial files use owner-only access.

### CLI and public contracts

`import PATH` requires one file or folder. A saved account binding remains
valid for a single file. Plain import retries a record with no success and
rejects a record that has succeeded; replacement needs `--replace` and reset
needs `--reset`. Add `imports list`, `imports show`, and `views rebuild`.

Remove `run`, `--input`, `paths.input`, and ledger uses of `--output`.
Period-aware commands share one exclusive selector contract. No selector means
the current calendar month; `--all` includes undated. `report --export PATH`
writes one named standalone file outside managed views. A range or `--all`
without export uses the disposable `.honeymoney/report-preview.html`.

JSON moves straight from schema version 2 to 3. Keep the common envelope. Use
`import_count`, `statement_transaction_count`, `view_transaction_count`,
`import_records`, `views`, and `report_html`; do not expose ledger-era names.

### Clean start, backup, and release

Legacy workspaces fail without change and state that a reset is required.
There is no migration command or compatibility window. A backup is one complete
idle workspace. Restore targets an empty location. Selective, merged, and
in-place restore are unsupported.

Release 0.2.0 requires Python 3.14.6 and declares
`requires-python >=3.14,<3.15`. Publish an annotated `v0.2.0` tag and immutable
GitHub Release assets: the checked wheel, source archive, and SHA-256 checksums.
Do not publish to PyPI. Never move the tag or replace an asset; fix later faults
in a patch release.

Cutover preserves the old package and full workspace as one dated, read-only
rollback unit. Create a fresh workspace, carry over only reviewed user-owned
inputs accepted by the new schemas, and reimport original statements locally.
Recreate corrections after import. A rollback must pair the old package with
the complete old workspace.

## Exact supersession

This ADR supersedes these parts of ADR 0001:

- public identity columns and exact cumulative CSV headers;
- `categorized.csv` authority and row/manifest agreement;
- the standalone identity manifest path and shape;
- pre-v2 bootstrap and all legacy migration;
- ledger-last publication and automatic retained-generation recovery; and
- ledger/import-report naming in diagnostics and tests.

This ADR supersedes these parts of ADR 0003:

- the canonical ledger and public canonical-row store;
- hidden source-occurrence CSV and separate overlap-manifest authority;
- canonical-ledger migration and source-ID correction aliases;
- `categorized.csv` as commit point; and
- ledger-era public counts, artifacts, and terms.

This ADR narrows ADR 0002: memory reads ready import records, saved corrections,
and checked index identity, not an authoritative ledger.

This ADR narrows ADRs 0004, 0005, and 0006: their review, valuation, rate, and
summary rules remain, but their fields live in generated views, their work
publishes through workspace generations, and their old migration clauses do
not apply.

This ADR narrows ADR 0007 only where it names ledger loading and publication.
Its user gate, fixed public request, and rule that no financial data enters the
request remain binding.

## Consequences

Source history, identity, saved choices, and output each have one clear owner.
Month views can be removed and rebuilt. Recovery can prove which generation
won without rerunning financial logic. The clean break costs users a local
reimport and gives 0.2.0 no legacy workspace compatibility.
