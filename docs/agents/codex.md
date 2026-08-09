# Codex setup

## Environment

Use Python 3.14.6 in a virtual environment, then run:

```bash
./scripts/bootstrap.sh
./scripts/check.sh
```

The package contract is `>=3.14,<3.15`. Default tests block live non-local
network use. Run live Ollama or HKMA checks only when the user asks.

Cloud tasks may use only committed synthetic fixtures. Do not open real files
under `samples/`, `private_samples/`, `money/`, or another local workspace in a
cloud task. Do not put financial rows, generated views, reports, corrections,
or live model output in prompts or logs.

## Local operation

```bash
cd ./money
honeymoney setup
honeymoney import ../synthetic/may.csv
honeymoney imports list --json
honeymoney imports show IMPORT_ID --json
honeymoney views rebuild --all
honeymoney status --month 2026-05 --json
honeymoney doctor
```

The config directory is the workspace root. Import takes a required path; do
not use removed `run`, `--input`, or cumulative-ledger output forms. Use a saved
binding only for one file.

All period-aware commands share month, range, undated, and all selectors. No
selector means the current month. Schema version 3 uses import record,
statement transaction, view transaction, view, and `report_html` terms.

If a command reports `full_rebuild_required`, run
`honeymoney views rebuild --all`. If a publication journal remains, stop normal
work, run doctor, then use `doctor --fix` only when its plan is safe. Do not
delete hidden state by hand.

For backup, copy one complete idle workspace. Restore it into an empty path.
For 0.2.0 cutover, keep the old package and old full workspace together and
create a fresh workspace. See the linked storage, doctor, backup, and cutover
docs in the README.

## Public command rules

JSON writes one document to stdout and progress to stderr. Exit 0 means
success, 1 means strict partial success or doctor warnings, and 2 means usage,
validation, or blocking workspace failure.

Treat help text, JSON schema 3, exit codes, config fields, correction columns,
view CSV columns, profiles, and stable finding codes as public contracts. Use
the CLI and temporary synthetic workspaces as the main acceptance seam.
