# Honeymoney

Honeymoney imports household CSV and text-based PDF statements, keeps source
history and saved choices on your machine, and builds calendar-month views. It
does not send financial data to cloud AI services. Optional Ollama use stays on
the checked local endpoint. The only public network command fetches public HKMA
rates after consent.

Version 0.2.0 uses a new workspace format and requires Python 3.14.6. It rejects
old workspaces without changing them. There is no migration command.

## Quick start

```bash
python3.14 -m venv .venv
source .venv/bin/activate
./scripts/bootstrap.sh

mkdir money
cd money
honeymoney setup --root .
honeymoney import ../statements/may.csv
honeymoney imports list
honeymoney status
honeymoney views rebuild --all
honeymoney doctor
```

The directory that contains the resolved `config.json` is the workspace root.
Setup creates config, starter user inputs, the empty workspace index, and the
import-record container. It does not create an input folder or any generated
view. See [workspace storage](docs/workspace-storage.md).

## Import records

Import requires one file or folder:

```bash
honeymoney import PATH
honeymoney import PATH --replace
honeymoney import PATH --reset
honeymoney import one.csv --binding ACCOUNT_BINDING
```

Each logical statement source gets one import record. Every accepted attempt
gets a permanent sequence number and outcome. A preflight rejection gets no
number. A failed first attempt leaves a visible record that is not ready. A
failed replacement or reset keeps the newest successful facts. A valid empty
statement is ready.

Plain import retries a record with no success and rejects one that already has
a success. Use `--replace` to change its current facts. Use `--reset` when the
same change must also clear saved choices fully supported by the successful
reset set. Replace keeps saved corrections.

```bash
honeymoney imports list
honeymoney imports show IMPORT_ID
```

The list shows safe source labels, readiness, and statement-transaction counts.
Show includes current state and complete bounded attempt history. Neither form
prints transaction values or raw source paths.

Honeymoney does not copy the original statement into the workspace. Keep your
original files in your own protected storage for later reimport.

## Generated views

Honeymoney derives all ready import records together, then places each view
transaction by valid posting date, valid transaction date, or `undated`.

Each view has:

```text
views/2026-05/transactions.csv
views/2026-05/review_needed.csv
views/2026-05/report.html
```

The undated view has the same three files under `views/undated/`. A selected
empty view has header-only CSVs and a zero-transaction report. Views are
replaceable. Do not store choices by editing them.

Successful state changes refresh affected view units before they report
success. Failed changes refresh none. A narrow refresh does not repair an
unrelated missing or edited view.

```bash
honeymoney views rebuild --month 2026-05
honeymoney views rebuild --start 2026-01-01 --end 2026-06-30
honeymoney views rebuild --undated
honeymoney views rebuild --all
```

A range replaces every touched calendar month in full; it does not create a
range folder. `--all` creates every implied view and removes registered views
that current facts no longer imply. If you edit config, profiles, rules, rates,
or mappings by hand, run `views rebuild --all`. Other writers fail with
`full_rebuild_required` until you do.

## Configuration and bindings

Use the binding commands to select a profile and owned accounts from a safe
filename pattern. Binding changes publish the mapping file and affected views
together.

```bash
honeymoney profile bind household --pattern 'household-*.csv' --profile starter_csv --owner Household --account starter_csv=checking=Checking
honeymoney profile bindings
honeymoney profile replace-pattern household --old-pattern 'household-*.csv' --new-pattern 'checking-*.csv'
honeymoney profile remove-pattern household --pattern 'checking-*.csv' --yes
```

`honeymoney config` shows the public config without runtime or secret-like
fields. `honeymoney config edit` stages an editor change, then publishes the
config and affected views as one generation. `honeymoney config edit ollama`
also accepts `--model`, `--enable`, and `--disable`.

`honeymoney reconcile` is a read-only summary of the current full-workspace
derivation. It never treats a generated view as source state.

## Period selectors

Status, pending review, reports, valuation inspection, and view rebuilds share
one exclusive selector language:

- one month;
- an inclusive month range;
- the undated set; or
- all output.

No selector means the current calendar month. `--all` includes undated.

```bash
honeymoney status
honeymoney status --month 2026-05 --json
honeymoney pending --undated
honeymoney report --start 2026-01-01 --end 2026-06-30
honeymoney report --month 2026-05 --export ./may-report.html
```

Managed reports stay inside their views. Report export writes one explicit,
standalone file and does not register it as managed state. A range or `--all`
without `--export` uses `.honeymoney/report-preview.html`; the next preview
replaces it.

## Review and corrections

`review_needed.csv` is generated from typed `review_reasons`. A missing HKD
value remains valuation state, not category doubt.

```bash
honeymoney review --month 2026-05
honeymoney review --transaction VIEW_TRANSACTION_ID --as DECISION
honeymoney review --file decisions.csv
honeymoney review pair VIEW_TRANSACTION_ID VIEW_TRANSACTION_ID --yes
honeymoney correct --file corrections.csv
```

Saved corrections live outside import records and views. They use stable
view-transaction IDs and remain saved when a transaction is not current.
Rules, local memory, Ollama output, valuation, reconciliation, and review flags
remain derived output.

Exact overlaps across statements keep one stable view identity. Equal repeated
rows keep supported multiplicity; Honeymoney does not invent a pairing.
Duplicate choices bind to the exact reviewed membership. Changed support asks
for a new choice.

`review pair` stores one explicit manual-pair correction after it checks that
the two current view transactions have one account, currency, and matching
opposite amounts. It updates the corrections file and affected views together.

`source-data inspect VIEW_TRANSACTION_ID` shows bounded evidence from the
stored normalized facts. `source-data resolve VIEW_TRANSACTION_ID` clears only
a stale saved source-data review reason; it refuses while current stored facts
still support the issue.

## Categorization and accounting

Derivation applies deterministic rules, opt-in correction-derived local
memory, saved local-model behavior, valuation, duplicate and source-data
repair, review state, and reconciliation across the whole workspace before it
splits views. Saved corrections remain human authority.

Ollama defaults off. It may suggest configured spending categories but cannot
set owners or protected accounting flows. It connects only to a checked
loopback endpoint and never joins the release or CI workflow.

`honeymoney learn` previews conservative exact rules from active saved reviews.
`honeymoney learn --yes` writes managed rules and every affected view as one
workspace generation. The preview writes nothing.

Transfer and exchange links may cross months. Source opening and closing
balance checks use the whole contributing import record. Reports label these
as source-level checks so they are not confused with month totals.

## Rates

Import a downloaded public HKMA document without network access:

```bash
honeymoney rates import hkma-rates.json
```

Or fetch approved public fields:

```bash
honeymoney rates fetch USD EUR --start 2026-05-01 --end 2026-05-31
honeymoney rates fetch USD --start 2026-05-01 --end 2026-05-31 --allow-network
```

The interactive form shows the fixed request and asks for consent. A
non-interactive command needs `--allow-network`. HKMA receives currency codes,
dates, public page controls, time, and the caller's IP address. It receives no
statement row, amount, description, account, owner, local path, correction, or
model prompt. See [network boundary](docs/network-boundary.md).

## Doctor

```bash
honeymoney doctor
honeymoney doctor --fix
```

Doctor audits the whole workspace without writing, reading original
statements, calling a model, or using the network. Normal commands fail while a
publication journal remains. Only `doctor --fix` may settle it from exact
recorded old or new bytes.

Doctor can repair generated output, disposable summaries, managed directories,
owner-only access, proved stale locks, and publication state when proof exists.
Damage to import facts, attempt history, stable identity, saved corrections, or
conflicting durable authorities requires a complete backup restore. See
[doctor](docs/doctor.md).

## Backup and restore

Back up one complete idle workspace. Stop all commands, run doctor, then copy
the workspace root as one unit while preserving modes. Restore into an empty
location and run doctor there. Selective restore, merged restore, and repair by
copying only the workspace index are unsupported. See
[backup and restore](docs/backup-restore.md).

## JSON and exit codes

JSON uses schema version 3 and keeps the common command envelope. Public result
terms include:

- `import_count`;
- `statement_transaction_count`;
- `view_transaction_count`;
- `import_records`;
- `views`; and
- `report_html`.

Ledger-era count and artifact names are not part of schema 3. JSON commands
write one document to stdout; progress goes to stderr. For most commands, exit
0 means success, 1 means strict partial success, and 2 means usage,
configuration, validation, or blocking workspace failure. Doctor uses 0 for a
healthy result, 2 for any warning or error that needs action, and 1 for an
unexpected program or operating-system failure.

`honeymoney evaluate` is deliberately retired. Its old form compared two
cumulative category CSV files, which were part of the removed storage model.
It now returns the named `legacy_csv_contract_removed` error instead of acting
as an unknown command.

## Privacy and file safety

Honeymoney keeps financial work local. Managed financial files and directories
allow owner access only. Unsafe paths and symbolic links fail closed. Doctor
warns about unknown managed entries but leaves them alone. Operational output
uses bounded safe labels and does not print amounts, descriptions, raw source
paths, or statement text unless an explicit local inspection command requires
the value.

Never put real statements, generated views, corrections, reports, credentials,
or live Ollama transcripts in git, issues, cloud prompts, or test logs. Use the
small synthetic fixtures under `tests/fixtures/`.

## Clean-start cutover

Keep the old package and full old workspace together as a dated, read-only
rollback unit. Create a fresh 0.2.0 workspace. Carry over only reviewed
user-owned inputs that pass the new schemas, reimport original statements
locally, and recreate corrections against the new IDs. Run a full view rebuild
and doctor before accepting the cutover.

Do not run 0.2.0 against an old workspace. Do not run an old package against a
0.2.0 workspace. See [release and cutover](docs/release-cutover.md).

## Development

Use Python 3.14.6:

```bash
./scripts/bootstrap.sh
python3 -m unittest discover
python3 -m mypy
./scripts/check_coverage.sh
./scripts/check.sh
```

The full gate runs formatting, lint, strict types, tests, at least 87 percent
branch coverage, and package builds. Release checks install both the wheel and
source archive offline in fresh Python 3.14.6 environments and run the same
synthetic setup/import/view/status/doctor flow. See
[quality gates](docs/quality-gates.md) and
[golden datasets](docs/golden-datasets.md).

## Design references

- [Architecture](docs/architecture.md)
- [ADR 0008](docs/adr/0008-clean-start-workspace-storage.md)
- [CSV compatibility](docs/csv-compatibility.md)
- [Workspace storage](docs/workspace-storage.md)
- [Doctor](docs/doctor.md)
- [Backup and restore](docs/backup-restore.md)
- [Release and cutover](docs/release-cutover.md)
