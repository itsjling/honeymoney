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
- `profiles/starter_csv.json`
- `input/`
- `output/`

Use `--root DIR` to skip the prompt.

```bash
honeymoney run
```

Processes the configured input files and writes output files. It reads `config.json` from the current directory unless you pass `--config`.

```bash
honeymoney import [PATH]
```

Processes one pasted file or folder path. If `PATH` is omitted, the command prompts you to paste it.

Useful run options:

```bash
honeymoney run --strict --no-interactive
honeymoney run --config ./money/config.json
honeymoney import "/path/to/statement.pdf"
honeymoney run --input ./samples --output ./output/categorized.csv
```

## Outputs

Each run writes three files next to the configured categorized CSV:

- `categorized.csv`: normalized transactions with categories, owners, payment methods, confidence, flags, and source traceability.
- `review_needed.csv`: only rows that need review, with editable correction columns.
- `import_report.json`: processed files, selected profiles, warnings, duplicate counts, review counts, and Ollama status.

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
2. Open `review_needed.csv`.
3. Fill correction fields such as `category`, `owner`, `payment_method`, `confidence`, `reason`, or `notes`.
4. Save those rows as `corrections.csv` or point config at the edited file.
5. Run Honeymoney again.

Corrections apply by exact `transaction_id` and clear review by default.

## Tests

```bash
python3 -m unittest discover
```
