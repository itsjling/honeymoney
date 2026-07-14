# Codex setup

## Local Codex

Open the repository root so Codex loads `AGENTS.md`. Use an activated virtual
environment when desired, then run:

```bash
./scripts/bootstrap.sh
./scripts/check.sh
```

Set `PYTHON=/path/to/python` when Codex should use a specific interpreter.

## Codex cloud

Connect `itsjling/honeymoney` in Codex settings and configure an environment
with:

- Python 3.10 or newer;
- setup script: `./scripts/bootstrap.sh`;
- maintenance script: `./scripts/bootstrap.sh`;
- no secrets;
- agent-phase internet access disabled.

Launch cloud work manually from a decision-complete GitHub issue carrying the
`ready-for-agent` label. Cloud tasks must use only committed synthetic fixtures;
they must not receive real statements or live Ollama access.

## Operating Honeymoney locally

JSON mode never prompts or opens a browser:

```bash
honeymoney import /path/to/statement.csv --config ./money/config.json --json
honeymoney status 2026-05 --config ./money/config.json --json
honeymoney pending 2026-05 --config ./money/config.json --json
honeymoney report 2026-05 --config ./money/config.json --json
honeymoney config --config ./money/config.json --json
honeymoney config edit ollama --config ./money/config.json --model qwen3.5:4b --json
```

Every response is one JSON object with `schema_version`, `command`, `status`,
`data`, `artifacts`, `warnings`, and `errors`. Progress and diagnostics remain
on stderr so stdout can be parsed directly.

Apply a reviewed batch from a file:

```bash
honeymoney correct --config ./money/config.json --file corrections.json --json
```

The input is a JSON array. `transaction_id` is required; all other fields are
optional, but each item must set at least one correction:

```json
[
  {
    "transaction_id": "example-id",
    "category": "Groceries",
    "owner": "Household",
    "confidence": 1,
    "reason": "Reviewed locally",
    "needs_review": false
  }
]
```

Use `--file -` to read the array from stdin. The entire batch is rejected before
any files change if an ID, field, or value is invalid. Corrections are field-wise
patches: omitted fields keep their saved values, and omitting `needs_review`
preserves the transaction's current review state.
