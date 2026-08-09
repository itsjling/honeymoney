# Honeymoney agent guide

Honeymoney is a local-first Python CLI for importing household CSV and
text-based PDF statements, normalizing transactions, categorizing them, and
maintaining durable import records and replaceable monthly views. It must not
send financial data to cloud AI services. The optional Ollama integration
talks only to the configured local endpoint.

## Start here

1. Read `docs/architecture.md` for the data flow and source map.
2. Read any ADR in `docs/adr/` that touches the work area, when present.
3. Bootstrap the active Python environment with `./scripts/bootstrap.sh`.
4. Use synthetic fixtures under `tests/fixtures/`; never copy real statements
   into tracked files, logs, issues, prompts, or test failures.

## Commands

- Full verification: `./scripts/check.sh`
- Static types: `python3 -m mypy`
- Branch coverage: `./scripts/check_coverage.sh`
- Full tests: `python3 -m unittest discover`
- Agent CLI tests: `python3 -m unittest tests.test_agent_cli`
- Import-profile goldens: `python3 -m unittest tests.test_import_profiles`
- Categorization goldens:
  `python3 -m unittest tests.test_transaction_categorization`
- Live Ollama smoke test: run only when explicitly requested, using the command
  in `docs/golden-datasets.md`.

Python 3.14.6, Ruff formatting, Ruff linting, static types, branch coverage, the
full unittest suite, package builds, and offline wheel and source-archive smoke
tests are required before release handoff. Prefer a focused test during
implementation, followed by `./scripts/check.sh` once at the end.

## Privacy and execution boundaries

- Cloud Codex tasks may use only committed synthetic examples and fixtures.
- Real files under `samples/`, `private_samples/`, `money/`, or other local
  workspaces may be opened only in a local task after the user explicitly asks
  to operate on them.
- Never commit statement data, generated views, reports, credentials, or live
  Ollama transcripts.
- Do not add cloud inference. Product network access is limited to the
  user-gated, public-data-only HKMA rate fetch in ADR 0007; no financial row or
  statement field may enter that request.
- Avoid reproducing private transaction descriptions or amounts in the final
  response unless they are essential to the user's explicit request.

## Change conventions

- Preserve the human-readable CLI unless a task explicitly changes it.
- Treat `--json` output, exit codes, CSV columns, configuration fields, and
  bundled profiles as public interfaces. Add regression tests for changes.
- Golden fixtures must be synthetic, minimal, and manually reviewed. Do not
  blindly regenerate expected output.
- Keep generated files out of git. Preserve unrelated worktree changes.
- A change is done when focused tests and `./scripts/check.sh` pass, relevant
  docs are updated, and the diff contains no private data.

## Agent skills

### Issue tracker

Issues and specs live in GitHub for `itsjling/honeymoney`. See
`docs/agents/issue-tracker.md`.

### Triage labels

The repo uses the five default triage labels. See
`docs/agents/triage-labels.md`.

### Domain docs

This repo uses a single-context layout. See `docs/agents/domain.md`.

## Issue-to-PR workflow

Issues and PRDs live in GitHub for `itsjling/honeymoney`; see
`docs/agents/issue-tracker.md`. New agent tasks start with `needs-triage`. Apply
`ready-for-agent` only after the objective, acceptance criteria, exclusions,
and verification commands are decision-complete. Cloud Codex tasks are
launched manually from those issues.

The vendored skill catalog remains available, but default to the smallest
workflow that fits:

- `triage` for issue readiness;
- `to-issues` for splitting an approved plan;
- `implement` or `tdd` for delivery;
- `diagnose` for bugs and regressions;
- `code-review` for standards/spec review;
- `handoff` for unfinished work.

Use design, writing, migration, and other specialized skills only when the task
actually calls for them.

## Domain documentation

This is a single-context repo. Read root `CONTEXT.md` and `docs/adr/` when they
exist; see `docs/agents/domain.md`. Use the established domain vocabulary and
surface any conflict with an ADR rather than silently overriding it.
