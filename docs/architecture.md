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
categorized.csv + review_needed.csv + import_report.json
                 |
                 v
status summaries and self-contained HTML reports
```

Imports merge into the cumulative ledger by `transaction_id`. `source_file`
provides statement-level replacement and reset behavior. Corrections are
persistent overrides keyed by `transaction_id`; rules and Ollama suggestions
run before corrections, so reviewed choices win.

## Source map

- `honeymoney/cli.py`: command routing, workspace setup, imports, profile
  selection, normalization, ledger management, corrections, and JSON output.
- `honeymoney/rules.py`: deterministic rule validation and application.
- `honeymoney/ollama.py`: optional local-only categorization fallback.
- `honeymoney/schema.py`: public ledger/review columns and allowed values.
- `honeymoney/report.py`: offline HTML report generation.
- `honeymoney/data/profiles/`: bundled institution profiles copied by setup.
- `tests/fixtures/`: synthetic golden inputs and expected behavior.

## Public boundaries

Treat CLI text behavior, the versioned JSON envelope, exit codes, configuration
fields, corrections, bundled profiles, and output columns as compatibility
contracts. JSON commands emit one document on stdout; progress belongs on
stderr. Exit `0` means success, `1` means strict partial success, and `2` means
usage, configuration, or validation failure.

`pending` exposes review rows. `correct` validates a complete JSON batch before
writing and then replaces the corrections and derived ledger files through
temporary files. Interactive `review` remains the human counterpart.

## Privacy boundary

Only synthetic fixtures may enter git or cloud Codex. Real statement files,
local workspaces, generated outputs, and live Ollama transcripts stay local.
Ollama is disabled by default and is never part of CI.
