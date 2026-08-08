# Changelog

## 0.2.0

### Breaking changes

- The directory that contains `config.json` is now the workspace root. New
  configs have no `paths` section.
- `honeymoney import PATH` replaces `run` and the old `--input` form. Import no
  longer accepts a ledger-path `--output` option.
- `status`, `pending`, filtered `review`, `report`, `valuation missing`, and
  `views rebuild` use one month, range, undated, or all selector language.
  `report --export PATH` replaces ledger-based report output.
- The cumulative `categorized.csv`, separate identity and overlap manifests,
  source-occurrence CSV, and old output folder stop being workspace state.
  Import records and `.honeymoney/workspace-index.json` now hold durable facts;
  replaceable output lives under `views/`.
- JSON moves from schema version 2 to 3. It uses `import_count`,
  `statement_transaction_count`, `view_transaction_count`, `import_records`,
  `views`, and `report_html`. It removes `statements_processed`,
  `records_processed`, nested `ledger`, `categorized_csv`, `review_needed_csv`,
  and `import_report_json`.
- Legacy workspaces are rejected without change. Version 0.2.0 has no migration
  command, old-state reader, or compatibility window.
- `honeymoney evaluate` is retired with the named
  `legacy_csv_contract_removed` error. Its old two-file cumulative CSV comparison
  needs removed durable inputs; generated views are not a replacement authority.

### Added

- Durable import records with immutable attempt history, plus `imports list`
  and `imports show`.
- Deterministic whole-workspace derivation and complete month or undated view
  units through `views rebuild`.
- Index-last workspace generations, checked publication journals, full
  workspace doctor, and proof-based repair.
- Require Python 3.14 through, but not including, 3.15, with release checks on
  Python 3.14.6.
- Publish checked wheel and source archives with SHA-256 checksums as immutable
  GitHub Release assets. PyPI publication remains out of scope.
- Retain non-ledger command forms: `profile bind`, `profile bindings`,
  `profile replace-pattern`, `profile remove-pattern`, `config`, `learn`,
  `reconcile`, `review pair`, and `source-data inspect|resolve`. Writers publish
  their user input or corrections with affected views and the workspace index.
