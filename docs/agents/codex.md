# Codex setup

## Local Codex

Open the repository root so Codex loads `AGENTS.md`. Use an activated virtual
environment when desired, then run:

```bash
./scripts/bootstrap.sh
./scripts/check.sh
```

Set `PYTHON=/path/to/python` when Codex should use a specific interpreter.
Bootstrap resolves the reviewed direct and transitive versions from
`constraints/dev.txt`; CI proves that resolution on Python 3.10 and 3.13.

## Codex cloud

Connect `itsjling/honeymoney` in Codex settings and configure an environment
with:

- Python 3.10 or newer;
- setup script: `./scripts/bootstrap.sh`;
- maintenance script: `./scripts/bootstrap.sh`;
- no secrets;
- agent-phase internet access disabled.

Run `./scripts/bootstrap.sh` during environment setup while package-index access
is available. The required editable-install extras are `pdf` and `dev`
(`.[pdf,dev]`); the bootstrap script installs both under the reviewed
constraints. The subsequent `./scripts/check.sh` agent phase is offline: it
uses the installed environment for Ruff, tests, `pip check`, package builds,
and distribution-metadata verification. Its default test runner rejects socket
creation and non-local DNS lookup; Ollama tests inject fake transports rather
than starting listeners. It does not perform advisory lookup.

Maintainers can run `./scripts/dependency-health.sh` separately when network
access is allowed. That command audits package names and versions only; it does
not open statement files. CI keeps this online audit in its own job after the
offline-compatible Python-version matrix succeeds.

Launch cloud work manually from a decision-complete GitHub issue carrying the
`ready-for-agent` label. Cloud tasks must use only committed synthetic fixtures;
they must not receive real statements or live Ollama access.

Live Ollama smoke and benchmark scripts remain explicit local-only commands in
`docs/golden-datasets.md`; they are never run by test discovery or CI.

## Operating Honeymoney locally

JSON mode never prompts or opens a browser:

```bash
honeymoney import /path/to/statement.csv --config ./money/config.json --json
honeymoney status 2026-05 --config ./money/config.json --json
honeymoney pending 2026-05 --config ./money/config.json --json
honeymoney report 2026-05 --config ./money/config.json --json
honeymoney config --config ./money/config.json --json
honeymoney config edit ollama --config ./money/config.json --model qwen3.5:4b --json
honeymoney review --transaction TRANSACTION_ID --as income --config ./money/config.json --json
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
preserves the transaction's current review state. An explicit `"notes": ""`
clears notes and remains a clear operation after correction reload and later
imports. Empty or whitespace-only `category`, `flow_type`, `owner`,
`payment_method`, `confidence`, `reason`, and `needs_review` values are invalid.
An empty or `Unknown` category may be marked resolved only when an explicit
accounting flow decision already exists or is included in the patch.

Human one-shot review uses the same validated correction merge and atomic
ledger/review rewrite as `correct`. `--remember --yes` is valid for safe income
decisions and atomically adds a local exact-match rule constrained by
institution, account identity, normalized description, and inflow direction.
Interactive review does not support JSON because prompting and a single JSON
stdout document are incompatible.
