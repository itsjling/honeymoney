# Honeymoney

Local-first household transaction categorization for exported CSV and text-based PDF statements.

Honeymoney keeps statement work local and does not call cloud AI APIs. If
Ollama is enabled, it talks to your local Ollama endpoint only. The one public
network command, `rates fetch`, requests official HKMA rate data only after
clear user consent.

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
- `rates.json`
- `profile_mappings.json`
- `profiles/` with `starter_csv.json` plus the bundled HSBC One, HSBC credit-card, and Mox bank/card profiles, all linked in `config.json`
- `.honeymoney-managed-files.json`, which records the origin and digest of each bundled profile
- `input/`
- `output/`

Use `--root DIR` to skip the prompt.

Upgrade an existing workspace after installing a new HoneyMoney version:

```bash
honeymoney setup --upgrade --root ./money --dry-run
honeymoney setup --upgrade --root ./money
```

The first command prints a plan and changes nothing. The second asks for
confirmation in a terminal. Scripts and JSON clients must pass `--yes`:

```bash
honeymoney setup --upgrade --root ./money --yes --json
```

An upgrade can create a missing bundled profile or update one when the managed
file record proves both its HoneyMoney origin and its unchanged local digest.
It reports local changes as conflicts and leaves them byte-for-byte unchanged.
It also leaves config, rules, corrections, rate data, profile mappings, custom
profiles, statement input, ledgers, and reports unchanged. A workspace made by
an older release without managed-file records does not gain ownership from a
familiar file name; differing files fail closed.
An older HoneyMoney install also refuses evidence written by a newer release,
so `--upgrade` cannot act as a downgrade.

Plans use `create`, `update`, `unchanged`, `conflict`, and `preserved` results.
Profile writes and the managed-file record publish as one recoverable
generation. Repeating a completed upgrade changes no bytes.

`setup --force` remains a separate destructive command. It warns before it
replaces existing starter files and cannot be combined with `--upgrade`.

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

Import refuses to process a source already present in hidden source identity
state. Use `--replace` to re-import that source, replace its source occurrences,
and recompute canonical overlap membership. Use `--reset` to do the same
replacement and remove old `corrections.csv` entries only for sources that were
processed successfully. A canonical correction stays when another active
source still supports its overlap group.
Failed or skipped sources retain both their ledger rows and corrections.
Correction removal and the replacement ledger use one recoverable generation;
`--reset` supersedes `--replace` if both are present.

A failed reset attempt writes a truthful current `import_report.json` while
preserving the prior ledger, review rows, and corrections. Optional Ollama
unavailability is not a statement-processing failure: parsed rows are committed,
left uncategorized for review, and their prior corrections are cleared as the
requested reset specifies.

When `--replace` or `--reset` opens an older review or canonical schema,
Honeymoney makes each pending review reason explicit before it changes source
rows. A proven parser rekey carries that pending review state to the new
canonical ID. The migration and successful replacement use one recoverable
generation, and the import summary reports the migration. If every source
fails, the summary describes the preserved generation. You may run
`honeymoney reconcile` first, but this preflight step is no longer required for
a safe upgrade. Replacement also derives `source_data_issue` from the active
source occurrences. A fixed source clears stale flags and correction tokens;
current parser or balance conflicts stay in review.

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

With no filter, interactively categorizes only transactions with one or more
typed `review_reasons` in `categorized.csv`. `pending`, JSON, the review CSV,
and the HTML report show why each row needs a choice. Pass `--category CATEGORY`
to revisit all ledger rows currently in that category even when they are not
marked for review. Repeat the option to review the union of multiple
categories. Category names must match the configured category vocabulary.

The legacy no-filter and category-only forms keep the category prompt. Period
forms (`MONTH`, `--month`, or `--start`/`--end`) compose with `--category`,
`--flow`, and normalized `--direction inflow|outflow`. Filtered cash-flow review
shows the base and posted amounts, account, description, category, and current
flow. It offers income, refund, transfer/payment, investment transfer, expense,
unresolved, skip, and quit decisions. An empty selection skips one row without
writing it; quit cancels the filtered review without writing any decisions.

`--transaction ID --as DECISION` is the non-interactive human seam. Add `--json`
for the versioned JSON envelope. Use `--file decisions.json` or
`--file decisions.csv` to apply a decision batch. JSON entries contain only
`transaction_id` and `decision`; CSV files use those exact two columns. The
command checks every ID and decision before it writes. It reports applied,
unchanged, and rejected counts without transaction or statement text. A
confirmed income sets `category=Income`,
`flow_type=income`, full confidence, and clears review. Refunds remain refunds;
owned transfers, card payments, and investment transfers stay excluded from
income. All review forms merge corrections by transaction ID, reconcile the
cumulative ledger, and publish `corrections.csv`, `categorized.csv`, and
`review_needed.csv` through the recoverable ledger-generation protocol.
Repeating a review does not append duplicate correction rows.

To link two confirmed, same-account cash movements that automatic matching
must leave alone, name both current IDs and confirm the choice:

```bash
honeymoney review pair TRANSACTION_ID TRANSACTION_ID --yes --json
```

The rows must have opposite, equal posted amounts in one currency and compatible
owners. A valid pair gets one stable manual transfer group, stays paired on
later reconciliation and proven replacement, and remains outside income and
spending. A changed or missing member makes the remaining row unresolved and
returns it to review. A later accounting decision clears both pair links in
the same write and returns the other member to review. Repeating the same
nomination returns `result=already_paired` and `changed=false`, even when the
first pair write retired the nominated source IDs. This no-op reports the
current pair and transaction IDs and writes no files. It succeeds only when
each old ID maps to one current row and those two rows form the whole stored
pair.

```json
[
  {"transaction_id": "txn_example", "decision": "expense"},
  {"transaction_id": "txn_example_2", "decision": "income"}
]
```

After interactive income confirmation, review can remember matching future
inflows. For a fully explicit one-shot operation use `--remember --yes`. The
saved local rule requires the same institution, account identity, exact
normalized description, and inflow direction; it never matches by amount. The
rule and correction are validated and persisted together, and deterministic
rules run before the optional local Ollama fallback.

```bash
honeymoney learn
honeymoney learn --yes
honeymoney learn --json
```

Builds exact deterministic rules from active reviewed corrections. The command
is a dry run unless you pass `--yes`. It learns a broad rule only when every
active row with the same institution, account ID, normalized description, and
direction has an agreeing review. Conflicting groups may split by exact posted
amount and currency. Managed rules never set owner or payment method, and
hand-written rules stay first. Output contains counts only.

## Accounting-safe Ollama categorization

Ollama is an optional local merchant-category suggester, never an accounting
authority. Import precedence is rules, opt-in local memory, conservative
structural classification, Ollama, then saved corrections. Protected overlap
review state is reapplied last. The model sees only
spending categories and cannot change an owner. `Income`, `Credit Card Payment`,
`Internal Transfer`, `Savings`, and `Investments` are protected accounting
categories: only rules, corrections, structural matching, or reconciliation can
establish their flows.

Optional `category_policies` entries give a category a `kind` and model
description. Kinds are `spending`, `accounting`, and `manual_only`; custom
categories default to manual-only, and protected built-ins cannot become
spending. Import reports count deterministic, memory, exact-correction,
accepted-model, reviewable-model, and unresolved results.

```bash
honeymoney config
honeymoney config edit
honeymoney config edit ollama
honeymoney config edit ollama --model qwen3.5:4b
honeymoney config edit ollama --enable
honeymoney config edit ollama --disable
```

Configuration is validated completely when it is loaded, before statements are
processed. Path fields and profile, rule, correction, and rate-cache references
must be non-empty strings. Category, owner, and payment-method vocabularies must
be arrays of unique non-empty strings. Exchange rates and Ollama timeouts must
be finite and positive; `review_confidence_threshold` must be from `0` to `1`;
and Ollama batch size must be a positive integer. Invalid fields are reported
by their full config path. Import profiles likewise require stable account
metadata, exactly one CSV or PDF parser definition, usable date and amount
mappings, and valid parser-specific settings. A selected CSV profile must map
only headers present in the statement.

`exchange_rates` holds fixed fallbacks. `dated_exchange_rates` maps a currency
to exact ISO dates and rates, for example
`{"EUR": {"2026-07-02": 8.90}}`. A same-row posted HKD amount wins, followed by
a matched exchange leg, an exact-date configured rate, an imported HKMA rate,
and a fixed fallback. A missing rate leaves `amount_hkd` empty and adds a
valuation-completeness item, not a category-review item.

Import a downloaded official HKMA daily-rate JSON file without network access:

```bash
honeymoney rates import ./er-eeri-daily.json
honeymoney rates import ./er-eeri-daily.json --json
```

The command checks the whole provider document before it writes. It stores
HKD-per-unit-of-foreign-currency observations in the versioned local
`rates.json` cache. If an older config has no `rate_cache` field, Honeymoney
uses `rates.json` beside that config without editing the config. An explicit
path still wins. Human and JSON rate-command output say which path was resolved
and whether it came from this default. An exact date wins. For a weekend or
holiday, the latest prior observation may serve a transaction for up to seven
calendar days. A future or older rate cannot fill the value. The cache records
the requested transaction date, observation date, raw rate, provider,
currencies, and a document digest. Provider fields already use one
foreign-currency unit, including JPY, KRW, and IDR. Later statement imports add
their requested-date resolutions. The cache stores no statement text or input
path.

Imported HKMA values use `valuation_source=hkma_daily_reference_rate` and
`valuation_status=estimated`. `valuation_rate_date` and `valuation_provider`
show the evidence. This is an HKD reference estimate, not a claim about a
bank's conversion cost. Repeating the import gives the same cache and ledger
bytes. The cache and affected ledger files publish as one recoverable
generation.

You may instead fetch named public currencies and dates from the fixed official
HKMA endpoint:

```bash
honeymoney rates fetch EUR USD \
  --start 2026-07-01 --end 2026-07-31 --allow-network
honeymoney rates fetch EUR \
  --start 2026-07-01 --end 2026-07-31 --allow-network --json
```

The command prints the provider, public endpoint, currencies, and date range
before access. An interactive terminal may omit `--allow-network` and approve
the shown request. JSON and other non-interactive runs require that flag.
Ordinary import, review, reconciliation, status, and report commands never use
this HTTP path.

The request contains only the fixed public endpoint, selected public currency
fields, date range, sort order, page size, and page offset. HKD is fixed by the
data set as the base currency. It never contains transaction amounts,
descriptions, account data, statement paths, ledger rows, or model prompts.
The command rejects redirects and other hosts. It verifies the host and
certificate with the local system trust plus Honeymoney's packaged CA bundle,
then validates all pages. It writes nothing until the full response passes the
same checks and persistence path as `rates import`. See
[`docs/network-boundary.md`](docs/network-boundary.md).

Fetch failures use stable codes for certificate verification, name resolution,
connection setup, timeout, HTTP status, malformed response, oversized response,
and incomplete pagination. The output omits headers, response bodies, and
private local data. A failure leaves the prior cache and ledger generation
unchanged, so it is safe to fix the local network or trust problem and retry.

Prints or edits the active `config.json`; pass `--config PATH` to target another file. `config edit` validates a temporary editor copy before replacing the original and uses `$VISUAL`, then `$EDITOR`, then `vi`. With no Ollama edit option, the guided editor lists models installed at the configured local endpoint. Selecting or passing a model also enables the Ollama fallback; `--enable` verifies that the configured model is installed before enabling it. Direct `--model`, `--enable`, and `--disable` edits can use `--json`.

## Structured agent commands

`setup`, `run`, `import`, `status`, `report`, `config`, `profile validate`,
`evaluate`, `learn`, `valuation missing`, `source-data inspect`, `source-data
resolve`, `rates import`, `rates fetch`, and fully specified one-shot `review`
accept `--json`. JSON mode prints exactly one versioned document to stdout,
never prompts, and never opens a browser. Exit code `0` is success, `1` is
strict partial success, and `2` is an input, configuration, or validation
error.

Import, status, pending, report, and reconcile data distinguish
`source_occurrence_count` from `canonical_occurrence_count`. Their `overlap`
object reports consolidation, provenance, and ambiguity. The old
`duplicate_count`, `duplicate_group_count`, and `duplicate_candidates` fields
remain as derived compatibility fields.

Inspect every canonical row that lacks an HKD value, with its active statement
evidence:

```bash
honeymoney valuation missing
honeymoney valuation missing 2026-05
honeymoney valuation missing --transaction TRANSACTION_ID --json
```

The default covers all dates. Period options use the same month and date forms
as status and report. Each result shows original and posted values, accounting
flow, valuation state and source, source-occurrence count, and
file, page, and row evidence. `source_file` is workspace-relative only when the
current location matches the stored source namespace. `source_display` keeps a
short stored label when no path can be proved. Repeated evidence stays repeated
so its length matches the source-occurrence count. The command validates the
ledger, identity
manifest, overlap manifest, and active source rows without writing or
recovering files. Ambiguous or inconsistent provenance stops with a value-free
error.

Inspect or clear one source-data review without opening a CSV:

```bash
honeymoney source-data inspect TRANSACTION_ID
honeymoney source-data resolve TRANSACTION_ID --json
```

Inspection works for actual, estimated, and missing HKD valuations. It reports
only the transaction ID, valuation state, active occurrence count, and safe
source, page, statement section, field, flag, and evidence status. It omits
amounts and statement text. `resolve` clears only a stale flag or
`source_data_issue` token on the named row. If that write also upgrades an old
stored schema, it repairs all rows in the migrating generation. Active parser,
balance, or provenance evidence returns `source_data_evidence_active` and
changes nothing. Unresolved overlap count and history conflicts count as typed
provenance evidence. An already clear row returns `changed=false`. A successful
repair publishes the ledger, correction review fields, review queue, hidden
source rows, and both identity files in one recoverable generation. Reconcile,
source replacement, duplicate resolution, and any command that migrates stored
rows apply the same repair rule.

```bash
honeymoney import ./statement.csv --config ./money/config.json --json
honeymoney status 2026-05 --config ./money/config.json --json
honeymoney pending 2026-05 --config ./money/config.json --json
honeymoney config --config ./money/config.json --json
honeymoney config edit ollama --config ./money/config.json --model qwen3.5:4b --json
honeymoney profile validate ./money/profiles/starter_csv.json \
  --config ./money/config.json --json
honeymoney evaluate ./money/output/categorized.csv \
  --reference ./money/corrections.csv --json
```

`evaluate` joins rows by exact transaction ID. It reports category coverage,
exact accuracy across the full reference set, accuracy among labeled rows, and
confusion counts. It does not print merchant or statement text.

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
honeymoney status --owner Justin --owner Franchesca
```

Shows how many statements and records have been processed for the period (default: the current calendar month), plus how many records are categorized, uncategorized, and needing review. Accepts a month name (`june`), `YYYY-MM`, or explicit `--start`/`--end` dates.

Repeat `--owner` to include the union of several configured owners. The names
must match the config exactly. With no owner filter, status keeps the combined
household view. Status JSON records the selected names under `filters.owners`.

Status also splits missing HKD values by accounting impact. Missing `income`,
`expense`, or `refund` values block cash-flow totals. Missing internal
transfers, card payments, and investment transfers stay visible but do not
block those totals. Unresolved flows remain a separate group. Actual,
estimated, and combined-estimate HKD income, spending, refunds, and net cash
flow use the same period and canonical rows.

```bash
honeymoney report
honeymoney report june --no-open
honeymoney report 2026-05 --owner Justin --no-open
```

Writes a self-contained `output/report.html` with transactions for the selected
period and a category chart, then opens it in your browser. Headline income
includes only confirmed `income`; spending includes confirmed `expense` net of
`refund`. Transfers, card payments, and investment movements are excluded.
Rows without HKD valuation stay out of totals and trigger a visible warning.
Each row shows the original amount and currency, HKD reporting value, valuation
source, rate date and provider when present, and actual, estimated, or missing
status. The page loads nothing from the network.

Report owner filters use the same rules and transaction set as status. The
HTML lists each owner in the generated report. Its checkboxes can show one
owner, any selected set, or the combined view without writing a new report.
The owner choice updates the headline totals, valuation counts and totals,
category chart, transaction counts, and transaction rows together. Report JSON
records the command-line selection under `filters.owners`; an empty list means
the combined household view.

The HTML and report JSON show total missing values, cash-flow blockers,
excluded-flow gaps, unresolved-flow gaps, and zero cash-flow rows. They also
split income, spending, refunds, and net cash flow into actual, estimated, and
combined-estimate HKD values. Provider-backed, configured exact-date, and fixed
estimates keep distinct source counts. Combined estimates are budget aids, not
exact bank conversion costs or tax valuations.

The HTML and report JSON also show statement-balance coverage by account,
source, section, and posted currency. Each result says whether safe opening and
closing evidence was found. Outcomes are `missing_opening`, `missing_closing`,
`missing_both`, `conflicting_evidence`, `matched`, or `mismatched`. Missing or
conflicting results stay unavailable and omit all balance values. If both
endpoints are safe but the posted currency or statement activity is unusable,
the result is `unavailable`; its reason names the missing input and no balance
value or difference is returned.

```bash
honeymoney reconcile
honeymoney reconcile --dry-run
honeymoney reconcile --json
```

Recomputes cash-flow treatment and transfer pairing across the full ledger.
Automatic matching still excludes same-account rows. A confirmed
`review pair` decision supplies the explicit link for those rows.
Same-currency matching uses opposite signs, equal absolute HKD amounts, distinct
owned `account_id` values, account types, and
`reconciliation.date_window_days` (default `3`). Cross-currency matching uses
same-date, same-institution exchange-debit and foreign-deposit evidence. Paired
currency-conversion legs become internal transfers, so reports do not count
either leg as spending. `--dry-run` inspects without writing.

Useful run options:

```bash
honeymoney run --strict --no-interactive
honeymoney run --config ./money/config.json
honeymoney import "/path/to/statement.pdf"
honeymoney run --input ./samples --output ./output/categorized.csv
honeymoney duplicates --json
honeymoney duplicates resolve ovr_<group> --as same-event
```

## Outputs

Each run writes three public files next to the configured categorized CSV:

- `categorized.csv`: canonical transactions with `canonical_group_id`,
  `canonical_slot`, `provenance_status`, and `source_occurrence_count`, plus
  categories, flow treatment, transfer links, owners, confidence, typed review
  reasons, and HKD valuation source, status, rate date, and provider.
  Source identity, display, and statement-balance cells are empty here.
- `review_needed.csv`: only ledger rows that need review, with typed reasons,
  plain reason labels, and editable correction columns.
- `import_report.json`: processed files, selected profiles, warnings, source and
  canonical counts, overlap provenance, compatibility duplicate counts, review
  counts, ledger totals, and Ollama status for the latest attempted import. A
  failed attempt can replace this report while the prior ledger stays in place.

Three hidden files join each recoverable ledger generation:

- `.honeymoney-source-occurrences.csv`: active normalized source evidence and
  retained evidence for retired identity records, including source identity,
  display locators, and statement balances. Only active rows affect balances
  and canonical output.
- `.honeymoney-identity-manifest.json`: source and record ownership.
- `.honeymoney-overlap-manifest.json`: the private canonical-ID namespace and
  active or retired multiset slots, keyed membership history, and explicit
  duplicate resolutions.

`rates.json` is a versioned local input cache named by `config.rate_cache`.
Legacy configs without that field use `rates.json` beside `config.json`; this
in-memory default never edits the config. Rate imports publish the cache with
the ledger generation when a ledger exists. Before the first ledger exists,
rate imports use the future ledger path as their coordination lock.

An exact issue #31 ledger keeps its old source IDs during read-only commands.
Its first write publishes the canonical CSV and both new hidden files together.
Partial canonical state fails closed.

When exact overlap sources have different repeat counts, `honeymoney
duplicates` lists the unresolved groups with bounded local evidence. Resolve a
current group with `--as keep-all` to retain the largest source count, or
`--as same-event` to retain the largest count supported by at least two
sources. Equal counts need no prompt. A later membership change gets a new
group ID, ignores the old choice, and returns the group to review. Resolution
keeps source evidence and does not rewrite `import_report.json`.

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
- Set `categorization_memory.enabled` to `true` to reuse a conservative local
  category match from two agreeing reviewed rows. It is off by default.
- Set `ollama.enabled` to `true` only when you want local Ollama fallback.
- Add filename mappings in `profile_mappings.json` when automatic detection is ambiguous.

Profiles may set `account_type` to `bank`, `credit_card`, `investment`, or `unknown`; omission remains compatible and common payment methods are inferred. CSV/PDF column mappings may optionally expose `statement_opening_balance`, `statement_closing_balance`, and `statement_section`. Reconciliation reports whether each endpoint was found. A missing or unsafe endpoint keeps the balance status `unavailable` and produces no calculated balance or difference.

Rules may assign `flow_type` as well as `category`. For institution-specific treatment, use `conditions` to combine exact, keyword, or regex matches on fields such as `institution`, `account_id`, `account_type`, and `original_description`. The derived `direction` condition supports exact `inflow` or `outflow` matching without changing transaction identity. These deterministic rules run before local Ollama; Ollama can suggest spending merchant categories but does not set an owner or replace `flow_type`.

### Local categorization memory

Local memory is opt-in and rebuilt for each import from exact corrections and
the validated ledger. It uses only two or more agreeing current identity-v2
rows with the same account, institution, currency, and normalized merchant.
It skips generic and transfer-like merchants, legacy or migration-ambiguous
rows, accounting or manual-only categories, and any conflicting evidence. It runs after explicit rules and before
Ollama; exact corrections still run last. It stores no learned sidecar and
sends no data anywhere.

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
- `calibrated_acceptance_threshold`: an optional threshold from `0` to `1`
  backed by a local accuracy check. Without it, model labels stay in review
  even when the model reports high confidence.

Requests constrain the response to model-eligible spending categories, with definitions and accounting-boundary guidance; they never include owners. When a response omits a row or gives an invalid item, Honeymoney retries only those rows once and keeps valid results. The status line shows which batch is in flight (`batch 2/20 (transactions 6-10 of 98, 4s)`) and ticks up every second while waiting, so a slow local model doesn't look stuck. If Ollama is unreachable, the model is missing, or a categorization is rejected, the import prints a warning explaining why and the affected rows stay uncategorized for interactive or manual review.
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

Each CSV statement file may contain at most 64 MiB. Each PDF may contain at
most 64 MiB, 500 pages, 20 million extracted text characters, and 100,000
transaction rows. Honeymoney stops the import when a file crosses a limit.
Balance and transaction parsing share each page's word and table extraction.

Current example profiles cover HSBC One, HSBC credit-card, and Mox bank/card statement shapes. `hsbc_one_pdf` is the sole HSBC bank-statement profile: it separates HKD Savings, HKD Current, and Foreign Currency Savings transactions into stable account identities, preserves each transaction currency, and retains the original PDF as source provenance. Select that profile when prompted and optionally save the filename mapping for future statements. Real private samples should stay in `samples/` or `private_samples/`.

The bundled HSBC and Mox PDF profiles also read statement opening and closing
balances. Reconciliation checks each source, account, and posted currency and
statement section. It keeps the existing `status` values. The `result` field
reports `missing_opening`, `missing_closing`, `missing_both`,
`conflicting_evidence`, `matched`, or `mismatched`. It reports `unavailable`
when both endpoints are safe but the posted currency or statement activity
cannot support a calculation. The balance lines do not become ledger
transactions. A true conflict reports the source, page, section, and field
without exposing either balance value.

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
4. Fill correction fields such as `category`, `flow_type`, `owner`,
   `payment_method`, `confidence`, `reason`, `review_reasons`, or `notes`.
   `reason` records provenance; `review_reasons` records any choice that must
   remain pending. Blank cells are omitted patches; use structured `correct`
   with `"notes": ""` to clear notes.
5. Save those rows as `corrections.csv` or point config at the edited file.
6. Run Honeymoney again.

Corrections apply by exact `transaction_id`. A valid category clears stale
category review. Other current review reasons, such as an identity conflict,
remain.

## Tests

Development and CI installs use the reviewed resolution in
`constraints/dev.txt` while published PDF requirements remain compatible
ranges. Bootstrap from any directory with Python 3.11 or 3.13:

```bash
PYTHON=python3.11 ./scripts/bootstrap.sh
PYTHON=python3.11 ./scripts/check.sh
```

The offline verification command runs formatting, linting, static types,
branch-covered unit tests, `pip check`, a wheel/source build, and
distribution-metadata checks. The test runner forbids socket creation and
non-local DNS lookup in both the main test process and child Python processes.
Ollama behavior uses injected in-memory transports. Once the bootstrap install
is available, the command does not query dependency indexes or advisory
services.
HKMA fetch behavior also uses an injected in-memory HTTP boundary. The default
test suite never calls the live HKMA endpoint.

Run either gate on its own:

```bash
python3 -m mypy
./scripts/check_coverage.sh
```

The coverage command runs the default synthetic unittest suite once, combines
data from child CLI processes, and enforces the threshold in `pyproject.toml`.
See [`docs/quality-gates.md`](docs/quality-gates.md) for the checked type scope,
the expansion rule, and the coverage policy.

Refresh the reviewed resolution intentionally on Python 3.11:

```bash
PYTHON=python3.11 ./scripts/refresh-constraints.sh
git diff -- pyproject.toml constraints/dev.txt
```

The refresh uses a clean temporary environment and rewrites the complete direct
and transitive resolution. Never hand-edit individual transitive pins. Before
accepting the diff, bootstrap clean environments on both Python 3.11 and 3.13,
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
