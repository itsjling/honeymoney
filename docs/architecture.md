# Architecture

Honeymoney is a local-first Python CLI. The filesystem is its integration
boundary: statements and configuration go in, while CSV, JSON, and HTML
artifacts come out. There is no database or cloud service.

## Data flow

```text
config + profiles + statement files
                 |
                 v
profile detection and CSV/PDF parsing
                 |
                 v
normalized rows + batch-wide identity resolution against the ledger and manifest
                 |
                 v
deterministic rules -> opt-in local memory -> duplicate checks -> structural classification -> optional local Ollama
                 |
                 v
validated corrections
                 |
                 v
deterministic flow treatment + cumulative-ledger transfer reconciliation
                 |
                 v
categorized.csv + review_needed.csv + import_report.json + hidden identity manifest
                 |
                 v
status summaries and self-contained HTML reports
```

Imports merge into the cumulative ledger by `transaction_id`. The identity
resolver uses the hidden manifest, not `source_file`, to find sources for
replacement and reset. `source_file` is display provenance only. Corrections
are persistent overrides keyed by `transaction_id`; rules and Ollama
suggestions run before corrections, so reviewed choices win.

## Filesystem persistence

`categorized.csv` is the authoritative cumulative ledger. `review_needed.csv`
is regenerated from that ledger whenever the ledger changes, while
`import_report.json` records the last import attempt and is replaced with its
import generation. Corrections and remembered rules remain independent inputs,
but operations that change them and the ledger publish them through the same
recoverable persistence boundary.

Each operation writes and flushes complete staged files and prior-file backups
before replacing any public path. Non-ledger artifacts are replaced first and
`categorized.csv` last; that final ledger replacement is the generation commit
point. The containing directories are then synchronized. This ordering is a
recovery protocol, not a claim that several filesystem replacements are atomic.

Hidden generation state beside `categorized.csv` contains only paths, modes,
and content digests. If a write fails before the ledger commit point, the old
files are restored. If interruption occurs after it, the next command that
loads the active workspace configuration completes the new generation before
continuing. Recovery removes public files that were
absent in the prior generation, preserves existing file permissions, and does
not include transaction values in diagnostics. Retained state also prevents a
new operation from silently proceeding when recovery cannot be completed.

Reset derives correction removal from prior ledger rows belonging only to
sources whose current file report is `processed`. The filtered correction
document is held in memory during categorization and is published in the same
generation as the replacement ledger. Failed and skipped sources therefore
retain their rows and corrections; a persistence failure restores both inputs
to the prior generation. Import reports record the requested action and the
ledger action actually committed for each source.

The current import report describes the latest attempted import even when a
source fails, while the authoritative ledger, its derived review rows, and saved
corrections remain on the prior financial generation. Ollama is an optional
post-parse categorizer: its unavailability leaves parsed rows pending review and
does not turn a successfully processed statement into a failed reset.

`category` is the merchant/budget classification. `flow_type` is the accounting
treatment used by cash-flow totals. Ollama is limited to configured spending
categories and cannot set an owner or protected accounting treatment. Protected
categories are established only by rules, corrections, conservative structural
classification, or reconciliation. After rules, local Ollama, and corrections,
the cumulative ledger is reconciled across owned accounts. Unique opposite-sign,
equal-base-currency candidates within the configured date window receive stable
transfer links derived from their existing transaction IDs. Ambiguous candidates
are never auto-paired. Reports derive old ledgers in memory, and `reconcile`
provides an explicit inspect/rewrite seam.

## Transaction identity

Identity v2 gives each resolved ledger row four public fields, directly after
`transaction_id`: `source_id`, `source_namespace_id`, `source_revision`, and
`source_record_id`. A v2 row has all four fields. An unresolved legacy row has
all four empty. Partial metadata fails validation. New transaction IDs use a
128-bit, domain-separated digest of the source and record IDs. Source IDs use
the `src_`, `ns_`, `rev_`, and `rec_` prefixes plus full SHA-256 digests; new
transaction IDs use `txn_` plus 32 lowercase hexadecimal characters.
`source_file`, source page, and source row remain display fields and never form
identity or replacement keys.

The resolver runs for the whole input batch before categorization, correction
application, reconciliation, or any transaction-ID dictionary. It resolves a
logical source from its normalized workspace-safe locator and exact source
bytes. An ordinary import creates a new source when its namespace is new. A
replace or reset reuses a source only through one exact namespace match, or one
unclaimed equal-revision match for an accepted rename. It never guesses from a
file name, directory order, or an ambiguous match.

Within each source, the resolver matches records only on the accepted
fingerprint and manifest ownership. An unchanged source uses its exact stored
locator mapping. A changed source can reuse records only when there is one
maximum matching; otherwise it stops with an identity ambiguity error. New
records receive a stable allocation origin from immutable parser locators.
Retired records keep their ownership, so they cannot pass a correction to a
later similar transaction. Legacy IDs survive only when migration proves one
owner; shared legacy IDs stay unowned and require review. The full contract is
in [`ADR 0001`](adr/0001-stable-transaction-identity.md).

## Persistence authority and recovery

`categorized.csv` is the authoritative ledger. `review_needed.csv` is a
deterministic view of its rows whose `needs_review` value is `true`.
`import_report.json` is a snapshot derived from the most recent successful
import; review and correction commands do not rewrite that historical import
snapshot. `corrections.csv` remains durable input for applying reviewed choices
to future imports, but it is not a second ledger.

The hidden `<categorized.csv parent>/.honeymoney-identity-manifest.json` is the
authoritative source and record ownership store. It records IDs, hashes,
allocation locators, and active or retired state, but never source paths,
statement text, or display values. Ledger rows and the manifest must agree.
The first import writes both, including for a zero-record source. A missing
manifest can bootstrap only an exact pre-v2 ledger header. Missing v2 state or
a manifest without its ledger fails closed.

The manifest joins every recoverable ledger generation, including import,
replace, reset, correction, review, reconcile, and recovery. A change that
only updates mutable ledger fields carries validated ownership forward without
changing it.

## Source map

- `honeymoney/cli.py`: command routing, workspace setup, imports, profile
  selection, normalization, ledger management, review filtering, and JSON output.
- `honeymoney/identity.py`: identity-v2 digests, validation, source and record
  resolution, manifest ownership, and safe identity diagnostics.
- `honeymoney/identity_state.py`: ledger and manifest loading, bootstrap rules,
  cross-file validation, and manifest path handling.
- `honeymoney/corrections.py`: correction validation, merge-by-transaction-ID,
  cumulative reconciliation, and correction/ledger/review/rule generation content.
- `honeymoney/csv_artifacts.py`: reversible spreadsheet-safe serialization and
  canonical read-back for public CSV text cells; see
  [CSV compatibility](csv-compatibility.md).
- `honeymoney/persistence.py`: staged filesystem generation commits, authoritative
  ledger replacement, directory synchronization, and retained-state recovery.
- `honeymoney/rules.py`: deterministic rule validation and application.
- `honeymoney/categorization_memory.py`: opt-in, correction-derived local
  spending-category matches rebuilt from validated identity state.
- `honeymoney/ollama.py`: optional local-only categorization fallback. Its
  shared model-listing and generation transport accepts only `http` endpoints
  that resolve exclusively to loopback addresses, pins the connection to a
  validated numeric address, bypasses proxies, and revalidates redirects before
  following them.
- `honeymoney/schema.py`: public ledger/review columns and allowed values.
- `honeymoney/report.py`: offline HTML report generation.
- `honeymoney/reconciliation.py`: deterministic flow derivation, transfer pairing,
  and optional statement balance checks.
- `honeymoney/data/profiles/`: bundled institution profiles copied by setup.
- `tests/fixtures/`: synthetic golden inputs and expected behavior.

## Public boundaries

Treat CLI text behavior, the versioned JSON envelope, exit codes, configuration
fields, corrections, bundled profiles, and output columns as compatibility
contracts. JSON commands emit one document on stdout; progress belongs on
stderr. Exit `0` means success, `1` means strict partial success, and `2` means
usage, configuration, or validation failure.

`pending` exposes review rows. `correct` remains the structured machine/agent
seam. `review` is the human seam: period/category/flow/direction filters feed
interactive accounting decisions, while `--transaction ID --as DECISION` is a
fully specified one-shot form. Both call the same correction operation, which
validates the complete patch/rule set, merges saved corrections by transaction
ID, reconciles the cumulative ledger, and replaces all derived files through
temporary files. JSON review is accepted only for the non-prompting one-shot
form.

Remembered income rules are deterministic exact matches on institution,
account identity, normalized description, and the virtual inflow direction.
Direction is derived from `amount_hkd` and is not part of transaction identity.
Human corrections, deterministic rules, and conservative structural matching
may establish protected flows; reconciliation may establish owned-account
transfers. Refunds and owned-account flows remain distinct, and Ollama cannot
set flow treatment.

## Privacy boundary

Only synthetic fixtures may enter git or cloud Codex. Real statement files,
local workspaces, generated outputs, and live Ollama transcripts stay local.
Ollama is disabled by default and is never part of CI.
