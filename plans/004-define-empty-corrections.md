# Plan 004: Make empty correction values consistent and safe

> **Executor instructions**: Implement only the selected correction semantics below, verify every public boundary, and update `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_agent_cli.py tests/test_workflow.py docs/agents/codex.md README.md`
> Stop if correction patch semantics have changed.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: MED
- **Depends on**: Plan 002
- **Category**: bug
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

The JSON correction command accepts stripped empty strings, applies them to the ledger, then drops them when reloading the corrections CSV. This creates state that changes again on the next import and can leave a blank category marked as not needing review.

## Current state

`_normalize_json_correction` at `honeymoney/cli.py:798-826` retains empty strings. `_load_corrections` at `cli.py:1314-1329` discards empty fields. `_apply_corrections` at `cli.py:2362-2374` applies any present field and defaults review to false.

The documented contract in `docs/agents/codex.md:66-70` says omitted fields remain unchanged; it does not define empty values. Adopt the conservative rule: reject empty strings for `category`, `owner`, `payment_method`, `confidence`, `reason`, and `needs_review`; allow an explicit empty `notes` value only if tests and docs define it as clearing notes.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Agent CLI | `python3 -m unittest tests.test_agent_cli` | all pass |
| Workflow | `python3 -m unittest tests.test_workflow` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_agent_cli.py`, `tests/test_workflow.py`, `docs/agents/codex.md`, and `README.md`.

**Out of scope**: nullable JSON schema, deleting correction records, persistence transaction protocol, category redesign.

## Git workflow

- Branch: `advisor/004-define-empty-corrections`
- Commit example: `fix: reject ambiguous empty corrections`.

## Steps

1. Add tests for empty/whitespace values in every correction field, both file and stdin input. Assert rejection before all three artifacts change. Add one documented notes-clearing test only if that behavior is retained.
   **Verify**: `python3 -m unittest tests.test_agent_cli` → new tests expose current inconsistency.
2. Enforce the chosen semantics in normalization/validation and add a post-patch invariant: a blank/Unknown category cannot have `needs_review == false` unless an existing documented rule explicitly permits it.
   **Verify**: `python3 -m unittest tests.test_agent_cli tests.test_workflow` → all pass.
3. Document empty versus omitted behavior.
   **Verify**: `rg -n 'empty|omitted|notes' docs/agents/codex.md README.md` → contract is explicit.

## Test plan

Extend the correction subprocess cases in `tests/test_agent_cli.py`. Cover every field, whitespace-only input, stdin and file input, unchanged artifact bytes on rejection, notes clearing if supported, and persistence after a subsequent import. Verify with `python3 -m unittest tests.test_agent_cli tests.test_workflow`.

## Done criteria

- [ ] Accepted correction values persist identically across reload/import.
- [ ] Invalid empty patches change no artifacts and exit 2 in JSON mode.
- [ ] Review/category invariant has regression coverage.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- Existing tests or docs intentionally use empty strings to clear non-notes fields.
- Correct semantics require a versioned correction schema migration.
- The change expands into deleting correction rows.

## Maintenance notes

Future clear/reset operations should use an explicit operation or null semantics, never overload empty strings silently.
