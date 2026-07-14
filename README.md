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
- `profiles/` with `starter_csv.json` plus the bundled HSBC HK, HSBC One, and Mox bank/card profiles (CSV and PDF), all linked in `config.json`
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

Import refuses to process a file whose `source_file` is already present in `categorized.csv`. Use `--replace` to re-import that source and replace its existing ledger rows. Use `--reset` to do the same replacement and also remove old `corrections.csv` entries for that source before categorization; `--reset` supersedes `--replace` if both are present.

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
cumulative ledger, and atomically replace `corrections.csv`, `categorized.csv`,
and `review_needed.csv`. Repeating a review does not append duplicate correction
rows.

After interactive income confirmation, review can remember matching future
inflows. For a fully explicit one-shot operation use `--remember --yes`. The
saved local rule requires the same institution, account identity, exact
normalized description, and inflow direction; it never matches by amount. The
rule and correction are validated and persisted together, and deterministic
rules run before the optional local Ollama fallback.

```bash
honeymoney config
honeymoney config edit
honeymoney config edit ollama
honeymoney config edit ollama --model qwen3.5:4b
honeymoney config edit ollama --enable
honeymoney config edit ollama --disable
```

Prints or edits the active `config.json`; pass `--config PATH` to target another file. `config edit` validates a temporary editor copy before replacing the original and uses `$VISUAL`, then `$EDITOR`, then `vi`. With no Ollama edit option, the guided editor lists models installed at the configured local endpoint. Selecting or passing a model also enables the Ollama fallback; `--enable` verifies that the configured model is installed before enabling it. Direct `--model`, `--enable`, and `--disable` edits can use `--json`.

## Structured agent commands

`setup`, `run`, `import`, `status`, `report`, `config`, and fully specified
one-shot `review` accept `--json`. JSON mode
prints exactly one versioned document to stdout, never prompts, and never opens
a browser. Exit code `0` is success, `1` is strict partial success, and `2` is
an input, configuration, or validation error.

```bash
honeymoney import ./statement.csv --config ./money/config.json --json
honeymoney status 2026-05 --config ./money/config.json --json
honeymoney pending 2026-05 --config ./money/config.json --json
honeymoney config --config ./money/config.json --json
honeymoney config edit ollama --config ./money/config.json --model qwen3.5:4b --json
```

`pending` returns transactions requiring review. Apply reviewed corrections as
one validated JSON batch:

```bash
honeymoney correct --config ./money/config.json --file corrections.json --json
```

The batch is validated in full before any output changes and merges fields by
`transaction_id`; omitted fields remain unchanged. Use `--file -` to read the
JSON array from stdin. The interactive `review` command remains available for
human review.

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

- `categorized.csv`: normalized transactions with categories, accounting flow treatment, transfer links, owners, payment methods, confidence, flags, and source traceability. This file is a cumulative ledger: each import merges into it by `transaction_id`, so reconciliation, `status`, and `report` see everything imported so far. Older ledgers without the newer columns are hydrated and safely rewritten; transaction IDs do not depend on the new fields.
- `review_needed.csv`: only ledger rows that need review, with editable correction columns.
- `import_report.json`: processed files, selected profiles, warnings, duplicate counts, review counts, ledger totals, and Ollama status.

`category` describes merchant or budget purpose. `flow_type` separately controls accounting treatment and is one of `income`, `expense`, `refund`, `internal_transfer`, `credit_card_payment`, `investment_transfer`, or `unresolved`. Reports never infer income from a positive sign alone.

Cashflow signs use the household perspective:

- spending and card purchases are negative
- salary, refunds, and credits are positive

## Configuration

Start with the files created by `honeymoney setup`.

Common edits:

- Add or edit profiles in `profiles/`.
- Add deterministic categorization rules in `rules.json`.
- Feed reviewed rows back through `corrections.csv`.
- Set `ollama.enabled` to `true` only when you want local Ollama fallback.
- Add filename mappings in `profile_mappings.json` when automatic detection is ambiguous.

Profiles may set `account_type` to `bank`, `credit_card`, `investment`, or `unknown`; omission remains compatible and common payment methods are inferred. CSV/PDF column mappings may optionally expose `statement_opening_balance` and `statement_closing_balance`. Reconciliation reports an explicit `unavailable` balance status when the source does not supply both rather than inventing balances.

Rules may assign `flow_type` as well as `category`. For institution-specific treatment, use `conditions` to combine exact, keyword, or regex matches on fields such as `institution`, `account_id`, `account_type`, and `original_description`. The derived `direction` condition supports exact `inflow` or `outflow` matching without changing transaction identity. These deterministic rules run before local Ollama; Ollama can suggest merchant categories but does not set or replace `flow_type`.

### Ollama fallback

Set `ollama.enabled` to `true` to categorize remaining unknown transactions with a local Ollama model. Options in the `ollama` config section:

- `model`: must be a model you have pulled locally (check with `ollama list`).
- `timeout_seconds`: request timeout per batch (default 120).
- `batch_size`: transactions per request (default 5). Local inference is generation-bound, so total time is roughly constant regardless of batch size (~1-2s per transaction); a smaller batch just means the status line updates more often and any one request has less to lose if it fails.
- `think`: allow thinking models to reason before answering (default `false`; slower and unnecessary since responses are schema-constrained).

Requests constrain the response to the allowed categories and owners. The status line shows which batch is in flight (`batch 2/20 (transactions 6-10 of 98, 4s)`) and ticks up every second while waiting, so a slow local model doesn't look stuck. If Ollama is unreachable, the model is missing, or a categorization is rejected, the import prints a warning explaining why and the affected rows stay uncategorized for interactive or manual review.
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

Current example profiles cover HSBC Hong Kong and Mox bank/card statement shapes. The `hsbc_one_pdf` profile imports HSBC One combined-account statements directly: it separates HKD Savings and HKD Current transactions into stable account identities while retaining the original PDF as source provenance. Select that profile when prompted and optionally save the filename mapping for future statements. Real private samples should stay in `samples/` or `private_samples/`.

## Review Loop

1. Run Honeymoney.
2. Run `honeymoney review` to categorize transactions needing review, or use `honeymoney review --flow unresolved --direction inflow` for human cash-flow decisions.
3. For manual review, open `review_needed.csv`.
4. Fill correction fields such as `category`, `flow_type`, `owner`, `payment_method`, `confidence`, `reason`, or `notes`.
5. Save those rows as `corrections.csv` or point config at the edited file.
6. Run Honeymoney again.

Corrections apply by exact `transaction_id` and clear review by default.

## Tests

```bash
python3 -m unittest discover
```
