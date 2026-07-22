# Honeymoney

Local-first household transaction categorization for exported CSV and text-based PDF statements.

Honeymoney runs on local files. It does not call cloud AI APIs. If Ollama is enabled, it talks to your local Ollama endpoint only.

Keep real bank statements out of git. The repo ignores `samples/`, `private_samples/`, and `output/`.

## Quick Start

Install the local package from the repo root:

```bash
python3 -m pip install -e ".[pdf]"
```

Create a starter workspace:

```bash
honeymoney setup
```

The command asks where to create the workspace. Press Enter to use `./money`.

Put exported CSV or PDF files in:

```bash
./money/input
```

Run the import:

```bash
cd ./money
honeymoney run
```

You can also import one file or folder directly:

```bash
honeymoney import
```

Paste the CSV/PDF path when prompted.

Show the short command reference:

```bash
honeymoney help
```

For Codex setup, privacy boundaries, and machine-readable command examples,
see [`docs/agents/codex.md`](docs/agents/codex.md). For the processing pipeline
and source map, see [`docs/architecture.md`](docs/architecture.md).

You can also run without installing:

```bash
python3 -m honeymoney.cli setup
cd ./money
python3 -m honeymoney.cli run
```

## Commands

```bash
honeymoney setup
```

Creates a starter local workspace with:

- `config.json`
- `rules.json`
- `corrections.csv`
- `profile_mappings.json`
- `profiles/` with `starter_csv.json` plus the bundled HSBC One, HSBC credit-card, and Mox bank/card profiles, all linked in `config.json`
- `input/`
- `output/`

Use `--root DIR` to skip the prompt.

When a statement matches more than one profile (or none, as with PDFs), the import prompts you to pick the profile and offers to remember the choice in `profile_mappings.json` so future imports of similarly named files select it automatically.

```bash
honeymoney run
```

Processes the configured input files and writes output files. It reads `config.json` from the current directory unless you pass `--config`.

```bash
honeymoney import [PATH]
```

Processes one pasted file or folder path. If `PATH` is omitted, the command prompts you to paste it.

After each import, any records that could not be auto-categorized are offered for interactive categorization: pick a category number, press Enter to skip one, or enter `q` to skip the rest. Your picks are saved to `corrections.csv` so they stick on future runs. Pass `--no-interactive` to skip the prompts.

Import refuses to process a file whose `source_file` is already present in
`categorized.csv`. Use `--replace` to re-import that source and replace its
existing ledger rows. Use `--reset` to do the same replacement and remove old
`corrections.csv` entries only for sources that were processed successfully.
Failed or skipped sources retain both their ledger rows and corrections.
Correction removal and the replacement ledger use one recoverable generation;
`--reset` supersedes `--replace` if both are present.

A failed reset attempt writes a truthful current `import_report.json` while
preserving the prior ledger, review rows, and corrections. Optional Ollama
unavailability is not a statement-processing failure: parsed rows are committed,
left uncategorized for review, and their prior corrections are cleared as the
requested reset specifies.

```bash
honeymoney profile validate PROFILE
honeymoney profile validate PROFILE [--config CONFIG] \
  [--input SYNTHETIC_OR_LOCAL_FILE]
```

Validates a JSON import profile with the same checks used by normal imports.
It reads `config.json` from the current directory by default; pass `--config`
when the profile relies on custom owners, payment methods, base currency, or
exchange rates from another configuration.
Adding `--input` runs the same production CSV or PDF normalization path and
prints a read-only preview of at most 10 rows. The command never creates or
updates ledgers, corrections, profile mappings, reports, or browser artifacts.
Preview output contains normalized transaction data, so use real statements
only in a trusted local terminal and never paste their output into cloud tasks,
issues, or logs. Profile-only PDF validation does not open a PDF or require the
PDF parser at runtime.

```bash
honeymoney review
honeymoney review --category Other
honeymoney review --category Other --category Shopping
honeymoney review --flow unresolved --direction inflow --month 2026-05
honeymoney review --transaction TRANSACTION_ID --as income
```

With no filter, interactively categorizes only transactions already marked as needing review in `categorized.csv`. Pass `--category CATEGORY` to revisit all ledger rows currently in that category even when they are not marked for review. Repeat the option to review the union of multiple categories. Category names must match the configured category vocabulary exactly.

The legacy no-filter and category-only forms keep the category prompt. Period
forms (`MONTH`, `--month`, or `--start`/`--end`) compose with `--category`,
`--flow`, and normalized `--direction inflow|outflow`. Filtered cash-flow review
shows the base and posted amounts, account, description, category, and current
flow. It offers income, refund, transfer/payment, investment transfer, expense,
unresolved, skip, and quit decisions. An empty selection skips one row without
writing it; quit cancels the filtered review without writing any decisions.

`--transaction ID --as DECISION` is the non-interactive human seam. Add `--json`
for the versioned JSON envelope. A confirmed income sets `category=Income`,
`flow_type=income`, full confidence, and clears review. Refunds remain refunds;
owned transfers, card payments, and investment transfers stay excluded from
income. All review forms merge corrections by transaction ID, reconcile the
cumulative ledger, and publish `corrections.csv`, `categorized.csv`, and
`review_needed.csv` through the recoverable ledger-generation protocol.
Repeating a review does not append duplicate correction rows.

After interactive income confirmation, review can remember matching future
inflows. For a fully explicit one-shot operation use `--remember --yes`. The
saved local rule requires the same institution, account identity, exact
normalized description, and inflow direction; it never matches by amount. The
rule and correction are validated and persisted together, and deterministic
rules run before the optional local Ollama fallback.

## Accounting-safe Ollama categorization

Ollama is an optional local merchant-category suggester, never an accounting
authority. Import precedence is rules, duplicate annotation, conservative
structural classification, Ollama, then saved corrections. The model sees only
spending categories and cannot change an owner. `Income`, `Credit Card Payment`,
`Internal Transfer`, `Savings`, and `Investments` are protected accounting
categories: only rules, corrections, structural matching, or reconciliation can
establish their flows.

Optional `category_policies` entries give a category a `kind` and model
description. Kinds are `spending`, `accounting`, and `manual_only`; custom
categories default to manual-only, and protected built-ins cannot become
spending. Import reports include additive structural and Ollama outcome metrics.

```bash
honeymoney config
honeymoney config edit
honeymoney config edit ollama
honeymoney config edit ollama --model qwen3.5:4b
honeymoney config edit ollama --enable
honeymoney config edit ollama --disable
```

Configuration is validated completely when it is loaded, before statements are
processed. Path fields and profile/rule/correction references must be non-empty
strings; category, owner, and payment-method vocabularies must be arrays of
unique non-empty strings; exchange rates and Ollama timeouts must be finite and
positive; `review_confidence_threshold` must be from `0` to `1`; and Ollama
batch size must be a positive integer. Invalid fields are reported by their
full config path. Import profiles likewise require stable account metadata,
exactly one CSV or PDF parser definition, usable date and amount mappings, and
valid parser-specific settings. A selected CSV profile must map only headers
present in the statement.

Prints or edits the active `config.json`; pass `--config PATH` to target another file. `config edit` validates a temporary editor copy before replacing the original and uses `$VISUAL`, then `$EDITOR`, then `vi`. With no Ollama edit option, the guided editor lists models installed at the configured local endpoint. Selecting or passing a model also enables the Ollama fallback; `--enable` verifies that the configured model is installed before enabling it. Direct `--model`, `--enable`, and `--disable` edits can use `--json`.

## Structured agent commands

`setup`, `run`, `import`, `status`, `report`, `config`, `profile validate`,
and fully specified one-shot `review` accept `--json`. JSON mode prints exactly
one versioned document to stdout, never prompts, and never opens a browser.
Exit code `0` is success, `1` is strict partial success, and `2` is an input,
configuration, or validation error.

```bash
honeymoney import ./statement.csv --config ./money/config.json --json
honeymoney status 2026-05 --config ./money/config.json --json
honeymoney pending 2026-05 --config ./money/config.json --json
honeymoney config --config ./money/config.json --json
honeymoney config edit ollama --config ./money/config.json --model qwen3.5:4b --json
honeymoney profile validate ./money/profiles/starter_csv.json \
  --config ./money/config.json --json
```

`pending` returns transactions requiring review. Apply reviewed corrections as
one validated JSON batch:

```bash
honeymoney correct --config ./money/config.json --file corrections.json --json
```

The batch is validated in full before any output changes and merges fields by
`transaction_id`; omitted fields remain unchanged. An explicit empty `notes`
string clears notes. Empty or whitespace-only values for every other correction
field are rejected. An `Unknown` or empty category cannot be marked resolved
unless the correction also preserves or supplies an explicit accounting flow
decision. Use `--file -` to read the JSON array from stdin. The interactive
`review` command remains available for human review.

```bash
honeymoney status
honeymoney status june
honeymoney status --month 2026-05
honeymoney status --start 2026-05-01 --end 2026-06-15
```

Shows how many statements and records have been processed for the period (default: the current calendar month), plus how many records are categorized, uncategorized, and needing review. Accepts a month name (`june`), `YYYY-MM`, or explicit `--start`/`--end` dates.

```bash
honeymoney report
honeymoney report june --no-open
```

Writes a self-contained `output/report.html` with transactions for the selected period and a pie chart of the category distribution with per-category sums, then opens it in your browser. Headline income includes only confirmed `income`; spending includes confirmed `expense` net of `refund`. Transfers, card payments, and investment movements are excluded, while unresolved inflows and outflows have separate visible tiles. Accepts the same period arguments and default as `status` (default: the current calendar month); `--no-open` writes the file without opening it. The page loads nothing from the network.

```bash
honeymoney reconcile
honeymoney reconcile --dry-run
honeymoney reconcile --json
```

Recomputes cash-flow treatment and transfer pairing across the entire cumulative ledger. Matching uses opposite signs, equal absolute base-currency amounts, distinct owned `account_id` values, account types, and `reconciliation.date_window_days` (default `3`). Only a unique mutual best match is paired; ambiguous candidates remain reviewable. `--dry-run` inspects without rewriting the ledger.

Useful run options:

```bash
honeymoney run --strict --no-interactive
honeymoney run --config ./money/config.json
honeymoney import "/path/to/statement.pdf"
honeymoney run --input ./samples --output ./output/categorized.csv
```

## Outputs

Each run writes three files next to the configured categorized CSV:

- `categorized.csv`: normalized transactions with stable identity metadata, categories, accounting flow treatment, transfer links, owners, payment methods, confidence, flags, and source traceability. This file is a cumulative ledger: each import reconciles persisted occurrences and merges by `transaction_id`, so reconciliation, `status`, and `report` see everything imported so far. Older ledgers without the newer columns are hydrated and safely rewritten; transaction IDs do not depend on the new fields.
- `review_needed.csv`: only ledger rows that need review, with editable correction columns.
- `import_report.json`: processed files, selected profiles, warnings, duplicate counts, review counts, ledger totals, and Ollama status.

`category` describes merchant or budget purpose. `flow_type` separately controls accounting treatment and is one of `income`, `expense`, `refund`, `internal_transfer`, `credit_card_payment`, `investment_transfer`, or `unresolved`. Reports never infer income from a positive sign alone.

Cashflow signs use the household perspective:

- spending and card purchases are negative
- salary, refunds, and credits are positive

### Stable transaction identity

Repeated transactions with identical financial details remain separate. The
ledger stores identity version, canonical and source fingerprints, and an
occurrence number so importing repeats one at a time produces the same distinct
IDs as importing them together. Renaming an unchanged statement or changing
directory discovery order does not move a reviewed correction to another
occurrence.

Existing non-colliding ledgers retain their v1 transaction IDs as identity
metadata is added. Some collision changes are inherently unknowable: if an
identical occurrence is inserted or removed, or an old collision has no saved
occurrence metadata, Honeymoney assigns fresh IDs, adds
`identity_reconciliation_ambiguous`, emits a warning, and keeps the affected
rows in review. It never guesses which old correction belongs to which row.
`--reset` removes the old source's corrections with the ledger update, while
`--replace` leaves unmatched old corrections inert.

### Spreadsheet-safe CSV values

Honeymoney protects generated CSV text cells from being interpreted as formulas
when opened in spreadsheet software. Text cells that could trigger formula
parsing are written with a self-identifying, Honeymoney-versioned escape
prefix. Honeymoney decodes this presentation encoding when it reads its own
ledger and correction files, so replacements and persistent corrections keep
the original value without accumulating prefixes.

Canonical columns (amounts, numeric identity fields, confidence, review flags,
and parser coordinates) are never escaped: negative amounts and other numeric
values remain directly usable as numbers. This policy applies only to generated
CSV artifacts; canonical in-memory values and JSON/HTML output are unchanged.
When adding a generated CSV column, classify it explicitly in
`honeymoney/csv_artifacts.py` (`CANONICAL_CSV_COLUMNS`) so the export boundary
remains safe.
## Configuration

Start with the files created by `honeymoney setup`.

Common edits:

- Add or edit profiles in `profiles/`.
- Add deterministic categorization rules in `rules.json`.
- Feed reviewed rows back through `corrections.csv`.
- Set `ollama.enabled` to `true` only when you want local Ollama fallback.
- Add filename mappings in `profile_mappings.json` when automatic detection is ambiguous.

Profiles may set `account_type` to `bank`, `credit_card`, `investment`, or `unknown`; omission remains compatible and common payment methods are inferred. CSV/PDF column mappings may optionally expose `statement_opening_balance` and `statement_closing_balance`. Reconciliation reports an explicit `unavailable` balance status when the source does not supply both rather than inventing balances.

Rules may assign `flow_type` as well as `category`. For institution-specific treatment, use `conditions` to combine exact, keyword, or regex matches on fields such as `institution`, `account_id`, `account_type`, and `original_description`. The derived `direction` condition supports exact `inflow` or `outflow` matching without changing transaction identity. These deterministic rules run before local Ollama; Ollama can suggest spending merchant categories but does not set an owner or replace `flow_type`.

### Ollama fallback

Set `ollama.enabled` to `true` to categorize remaining unknown transactions with a local Ollama model. Options in the `ollama` config section:

- `url`: an `http` URL whose hostname resolves only to loopback addresses
  (`localhost`, `127.0.0.1`, or `[::1]` are typical). Remote/LAN addresses,
  URL credentials, malformed URLs, and redirects away from loopback are
  rejected before transaction data is sent.
- `model`: must be a model you have pulled locally (check with `ollama list`).
- `timeout_seconds`: request timeout per batch (default 120).
- `batch_size`: transactions per request (default 5). Local inference is generation-bound, so total time is roughly constant regardless of batch size (~1-2s per transaction); a smaller batch just means the status line updates more often and any one request has less to lose if it fails.
- `think`: allow thinking models to reason before answering (default `false`; slower and unnecessary since responses are schema-constrained).

Requests constrain the response to model-eligible spending categories, with definitions and accounting-boundary guidance; they never include owners. The status line shows which batch is in flight (`batch 2/20 (transactions 6-10 of 98, 4s)`) and ticks up every second while waiting, so a slow local model doesn't look stuck. If Ollama is unreachable, the model is missing, or a categorization is rejected, the import prints a warning explaining why and the affected rows stay uncategorized for interactive or manual review.
When an interactive import reaches uncategorized rows while the fallback is disabled, the prompt explains that `ollama.enabled` must be set to `true` in `config.json`.

The repo also includes fuller examples:

- `examples/config.json`
- `examples/rules.json`
- `examples/profiles/`
- `examples/expected-output/`

## PDFs

PDF support is for text-based statement PDFs. Install the PDF extra:

```bash
python3 -m pip install -e ".[pdf]"
```

Current example profiles cover HSBC One, HSBC credit-card, and Mox bank/card statement shapes. `hsbc_one_pdf` is the sole HSBC bank-statement profile: it separates HKD Savings, HKD Current, and Foreign Currency Savings transactions into stable account identities, preserves each transaction currency, and retains the original PDF as source provenance. Select that profile when prompted and optionally save the filename mapping for future statements. Real private samples should stay in `samples/` or `private_samples/`.

Migration: remove `hsbc_hk_bank` and `hsbc_hk_bank_pdf` paths or mappings from
existing configurations. Use `hsbc_one_pdf` for HSBC One PDF statements. For
CSV exports, use `starter_csv` when its signed `Amount` columns fit, or keep a
custom local profile for institution-specific debit/credit columns.

To verify extraction against real statements without committing them, use the
private PDF acceptance workflow in
[`docs/golden-datasets.md`](docs/golden-datasets.md#checking-real-pdfs-locally).
It prepares parser-only CSV snapshots under the ignored `private_samples/`
directory for manual approval and repeatable local checks.

## Review Loop

1. Run Honeymoney.
2. Run `honeymoney review` to categorize transactions needing review, or use `honeymoney review --flow unresolved --direction inflow` for human cash-flow decisions.
3. For manual review, open `review_needed.csv`.
4. Fill correction fields such as `category`, `flow_type`, `owner`, `payment_method`, `confidence`, `reason`, or `notes`. Blank cells are omitted patches; use structured `correct` with `"notes": ""` to explicitly clear notes.
5. Save those rows as `corrections.csv` or point config at the edited file.
6. Run Honeymoney again.

Corrections apply by exact `transaction_id`. Omitted fields, including review
state, remain unchanged.

## Tests

Development and CI installs use the reviewed resolution in
`constraints/dev.txt` while published PDF requirements remain compatible
ranges. Bootstrap from any directory with Python 3.10 or 3.13:

```bash
PYTHON=python3.10 ./scripts/bootstrap.sh
PYTHON=python3.10 ./scripts/check.sh
```

The offline verification command runs formatting, linting, unit tests,
`pip check`, a wheel/source build, and distribution-metadata checks. The test
runner forbids in-process socket creation and DNS lookups; Ollama behavior is
exercised through injected in-memory transports. Once the bootstrap install is
available, the command does not query dependency indexes or advisory services.

Refresh the reviewed resolution intentionally on Python 3.10:

```bash
PYTHON=python3.10 ./scripts/refresh-constraints.sh
git diff -- pyproject.toml constraints/dev.txt
```

The refresh uses a clean temporary environment and rewrites the complete direct
and transitive resolution. Never hand-edit individual transitive pins. Before
accepting the diff, bootstrap clean environments on both Python 3.10 and 3.13,
run the import-profile goldens, and run the full verification command:

```bash
clean_python=/path/to/clean-environment/bin/python
PYTHON="$clean_python" ./scripts/bootstrap.sh
"$clean_python" -m unittest tests.test_import_profiles
PYTHON="$clean_python" ./scripts/check.sh
```

Dependency advisory lookup is deliberately separate because it needs network
access. It checks installed-package consistency first, then fails for any known
advisory (which is stricter than checking only high-severity findings):

```bash
./scripts/dependency-health.sh
```

This command sends only package names and versions to the public advisory
service; it never reads statement inputs or generated ledgers.

Focused golden suites:

```bash
python3 -m unittest tests.test_import_profiles
python3 -m unittest tests.test_transaction_categorization
```

Verify accepted private PDFs locally without exposing their values:

```bash
python3 scripts/check_private_pdfs.py check
```
