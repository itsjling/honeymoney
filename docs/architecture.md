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
deterministic rules -> duplicate checks -> optional local Ollama
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

`category` is the merchant/budget classification. `flow_type` is the accounting
treatment used by cash-flow totals. After rules, local Ollama, and corrections,
the cumulative ledger is reconciled across owned accounts. Unique opposite-sign,
equal-base-currency candidates within the configured date window receive stable
transfer links derived from their existing transaction IDs. Ambiguous candidates
are never auto-paired. Reports derive old ledgers in memory, and `reconcile`
provides an explicit inspect/rewrite seam.

## Source map

- `honeymoney/cli.py`: command routing, workspace setup, imports, profile
  selection, normalization, ledger management, review filtering, and JSON output.
- `honeymoney/corrections.py`: correction validation, merge-by-transaction-ID,
  cumulative reconciliation, and atomic correction/ledger/review/rule writes.
- `honeymoney/rules.py`: deterministic rule validation and application.
- `honeymoney/ollama.py`: optional local-only categorization fallback.
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
Only a human correction or deterministic rule may establish `flow_type=income`;
refunds and owned-account flows remain distinct, and Ollama cannot set flow
treatment.

## Privacy boundary

Only synthetic fixtures may enter git or cloud Codex. Real statement files,
local workspaces, generated outputs, and live Ollama transcripts stay local.
Ollama is disabled by default and is never part of CI.
