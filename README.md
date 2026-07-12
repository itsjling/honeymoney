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
- `profiles/` with `starter_csv.json` plus the bundled HSBC HK and Mox bank/card profiles (CSV and PDF), all linked in `config.json`
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
```

Interactively categorizes transactions already marked as needing review in `categorized.csv`. It uses the same category prompt as import, saves your choices to `corrections.csv`, updates `categorized.csv`, and rewrites `review_needed.csv`.

## Structured agent commands

`setup`, `run`, `import`, `status`, and `report` accept `--json`. JSON mode
prints exactly one versioned document to stdout, never prompts, and never opens
a browser. Exit code `0` is success, `1` is strict partial success, and `2` is
an input, configuration, or validation error.

```bash
honeymoney import ./statement.csv --config ./money/config.json --json
honeymoney status 2026-05 --config ./money/config.json --json
honeymoney pending 2026-05 --config ./money/config.json --json
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

Writes a self-contained `output/report.html` with transactions for the selected period and a pie chart of the category distribution with per-category sums, then opens it in your browser. Accepts the same period arguments and default as `status` (default: the current calendar month); `--no-open` writes the file without opening it. The page loads nothing from the network.

Useful run options:

```bash
honeymoney run --strict --no-interactive
honeymoney run --config ./money/config.json
honeymoney import "/path/to/statement.pdf"
honeymoney run --input ./samples --output ./output/categorized.csv
```

## Outputs

Each run writes three files next to the configured categorized CSV:

- `categorized.csv`: normalized transactions with categories, owners, payment methods, confidence, flags, and source traceability. This file is a cumulative ledger: each import merges into it by `transaction_id`, so `status` and `report` see everything imported so far.
- `review_needed.csv`: only ledger rows that need review, with editable correction columns.
- `import_report.json`: processed files, selected profiles, warnings, duplicate counts, review counts, ledger totals, and Ollama status.

Spending summaries should use `amount_hkd` and usually exclude `Credit Card Payment` and `Internal Transfer`.

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

### Ollama fallback

Set `ollama.enabled` to `true` to categorize remaining unknown transactions with a local Ollama model. Options in the `ollama` config section:

- `model`: must be a model you have pulled locally (check with `ollama list`).
- `timeout_seconds`: request timeout per batch (default 120).
- `batch_size`: transactions per request (default 5). Local inference is generation-bound, so total time is roughly constant regardless of batch size (~1-2s per transaction); a smaller batch just means the status line updates more often and any one request has less to lose if it fails.
- `think`: allow thinking models to reason before answering (default `false`; slower and unnecessary since responses are schema-constrained).

Requests constrain the response to the allowed categories and owners. The status line shows which batch is in flight (`batch 2/20 (transactions 6-10 of 98, 4s)`) and ticks up every second while waiting, so a slow local model doesn't look stuck. If Ollama is unreachable, the model is missing, or a categorization is rejected, the import prints a warning explaining why and the affected rows stay uncategorized for interactive or manual review.

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

Current example profiles cover HSBC Hong Kong and Mox bank/card statement shapes. Real private samples should stay in `samples/` or `private_samples/`.

## Review Loop

1. Run Honeymoney.
2. Run `honeymoney review` to categorize transactions needing review interactively.
3. For manual review, open `review_needed.csv`.
4. Fill correction fields such as `category`, `owner`, `payment_method`, `confidence`, `reason`, or `notes`.
5. Save those rows as `corrections.csv` or point config at the edited file.
6. Run Honeymoney again.

Corrections apply by exact `transaction_id` and clear review by default.

## Tests

```bash
python3 -m unittest discover
```

## Development / CI dependency constraints

`pyproject.toml` keeps compatible (unpinned or ranged) requirements so
published package metadata does not unnecessarily hard-pin end users.
Development and CI installs additionally apply `constraints/dev.txt`, which
pins every direct and transitive dependency of the `pdf` and `dev` extras to
versions reviewed together, so the same commit resolves the same set of
package versions on every machine and CI run. `./scripts/bootstrap.sh`
applies it automatically via `pip install -c constraints/dev.txt -e ".[pdf,dev]"`.

Both supported Python versions (3.10 and 3.13) resolve to the same pinned
versions from `constraints/dev.txt`; a couple of transitive packages
(`tomli`, `typing_extensions`) are installed only on Python <3.11 via
environment markers, which is expected.

To refresh the constraints (run on Python 3.10, the oldest supported
version):

```bash
python3.10 -m venv /tmp/honeymoney-constraints-refresh
/tmp/honeymoney-constraints-refresh/bin/pip install --disable-pip-version-check -e ".[pdf,dev]"
/tmp/honeymoney-constraints-refresh/bin/pip freeze --exclude-editable > constraints/dev.txt
rm -rf /tmp/honeymoney-constraints-refresh
```

Then restore the header comment at the top of `constraints/dev.txt`, confirm
the same versions also resolve cleanly in a clean Python 3.13 venv, run the
parser import-profile goldens and `./scripts/check.sh` on both versions, and
commit the refreshed file deliberately (never as an automated/blind update —
see `constraints/dev.txt` for the full refresh workflow and rationale).
