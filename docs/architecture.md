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
normalized rows + stable transaction IDs
                 |
                 v
deterministic rules -> duplicate checks -> structural classification -> optional local Ollama
                 |
                 v
validated corrections
                 |
                 v
deterministic flow treatment + cumulative-ledger transfer reconciliation
                 |
                 v
categorized.csv + review_needed.csv + import_report.json
                 |
                 v
status summaries and self-contained HTML reports
```

Imports merge into the cumulative ledger by `transaction_id`. `source_file`
provides statement-level replacement and reset behavior. Corrections are
persistent overrides keyed by `transaction_id`; rules and Ollama suggestions
run before corrections, so reviewed choices win.

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

## Source map

- `honeymoney/cli.py`: command routing, workspace setup, imports, profile
  selection, normalization, ledger management, review filtering, and JSON output.
- `honeymoney/corrections.py`: correction validation, merge-by-transaction-ID,
  cumulative reconciliation, and correction/ledger/review/rule generation content.
- `honeymoney/persistence.py`: staged filesystem generation commits, authoritative
  ledger replacement, directory synchronization, and retained-state recovery.
- `honeymoney/rules.py`: deterministic rule validation and application.
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
