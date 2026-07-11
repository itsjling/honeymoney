# Plan 016: Design and add local profile validation and preview tooling

> **Executor instructions**: This is a direction plan for a new public CLI surface. Finalize command contracts in tests before implementation and update the index when done.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_agent_cli.py tests/test_import_profiles.py README.md docs/agents/codex.md docs/golden-datasets.md`
> Stop if Plan 003 did not expose reusable profile validators.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plan 003
- **Category**: direction
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Users are told to add or edit JSON profiles manually, while malformed mappings can create bad financial rows. Reusable validation and a synthetic/local preview command would make new institutions safer to support and shorten the fixture-authoring loop without sending statement data anywhere.

## Current state

- `README.md:173-181` directs users to edit profiles.
- `honeymoney/cli.py:1262-1290` loads/validates profiles only as part of running the main pipeline.
- `docs/golden-datasets.md:29-69` defines synthetic import cases and expected normalized rows.
- Structured CLI responses are versioned and must emit exactly one JSON document on stdout.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Agent CLI | `python3 -m unittest tests.test_agent_cli` | all pass |
| Profile goldens | `python3 -m unittest tests.test_import_profiles` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_agent_cli.py`, `tests/test_import_profiles.py`, `README.md`, `docs/agents/codex.md`, and `docs/golden-datasets.md`.

**Out of scope**: GUI/profile wizard; automatic profile inference from private statements; uploading statements; OCR; modifying bundled profiles automatically.

## Git workflow

- Branch: `advisor/016-profile-validation-command`
- Commit example: `feat: add profile validation command`.

## Steps

1. Specify command grammar and structured output before implementation. Recommended minimum: `honeymoney profile validate PROFILE [--input SYNTHETIC_OR_LOCAL_FILE] [--json]`; preview should be included only if it can guarantee no output artifact mutation. Define exit 0 valid, exit 2 invalid, stdout/stderr behavior, warnings, parser mode, and normalized-row preview limits.
   **Verify**: contract tests in `tests/test_agent_cli.py` describe valid/invalid/missing-file cases and JSON purity.
2. Refactor Plan-003 validators/importers into callable read-only functions if needed. Validation must never create ledger, correction, mapping, report, or browser artifacts.
   **Verify**: tests snapshot the temporary workspace before/after command and show only explicitly requested preview output on stdout.
3. Implement the command using the same validators and normalization path as real import; avoid a divergent second parser. Redact/minimize preview fields in JSON and document that users should operate only on local data.
   **Verify**: agent CLI and profile golden suites pass.
4. Add a documented workflow for creating a minimal synthetic golden from a profile preview. Never generate expected outputs blindly.
   **Verify**: docs commands match `honeymoney help` and subprocess tests.

## Test plan

Follow structured subprocess tests in `tests/test_agent_cli.py` and profile goldens in `tests/test_import_profiles.py`. Cover valid/invalid profiles, missing files, CSV/PDF modes without live dependencies, JSON purity, text diagnostics, bounded preview, and zero workspace mutations. Verify both focused suites.

## Done criteria

- [ ] Profile validation uses production validators without writing workspace artifacts.
- [ ] Text and JSON contracts have subprocess tests.
- [ ] Invalid profiles identify exact field paths.
- [ ] Preview, if shipped, is bounded and local-only.
- [ ] Full check passes.

## STOP conditions

- Plan 003 validators remain coupled to mutation-heavy pipeline state.
- The command would need to ingest or commit private fixtures.
- Preview requires a second normalization implementation.
- Command naming conflicts with an existing public CLI surface.

## Maintenance notes

Every new profile parser mode must work through this validator. Keep preview explicitly diagnostic; it must never become an alternate import path.
