# Plan 006: Commit reset corrections only after successful re-import

> **Executor instructions**: Build on the persistence boundary from Plan 005. Run every verification and update the index.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_workflow.py tests/test_agent_cli.py README.md`
> Compare against the live post-Plan-005 persistence API; stop if it cannot support staged correction state.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plan 005
- **Category**: bug
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

`--reset` currently deletes saved corrections before parsing and categorization succeed. Reset must be one logical operation: either the replacement ledger and filtered corrections both commit, or neither does.

## Current state

At `honeymoney/cli.py:200-207`, `_remove_corrections(config, reset_ids)` mutates the live file before `_load_profiles`, statement import, rule loading, or Ollama. `_remove_corrections` at `cli.py:1112-1132` rewrites the target directly.

README semantics at `README.md:98` say reset re-imports and removes old corrections; they do not authorize correction loss on failed re-import.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Workflow | `python3 -m unittest tests.test_workflow` | all pass |
| Agent CLI | `python3 -m unittest tests.test_agent_cli` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_workflow.py`, `tests/test_agent_cli.py`, and `README.md` only if reset failure semantics need clarification.

**Out of scope**: changing replace semantics; ID/source redesign; new correction format.

## Git workflow

- Branch: `advisor/006-transactional-reset`
- Commit example: `fix: commit reset corrections with reimport`.

## Steps

1. Add tests that snapshot corrections and ledger bytes, then fail reset through invalid profile JSON, invalid rules, CSV failure, PDF failure, and persistence failure. All old state must remain.
   **Verify**: `python3 -m unittest tests.test_workflow` → new tests fail against pre-fix behavior.
2. Refactor correction removal into a pure function returning filtered rows/document. Stage that result in memory and pass it to the Plan-005 commit boundary only after import/categorization is ready.
   **Verify**: reset failure tests pass; no live correction write occurs before commit.
3. Test successful reset removes only IDs belonging to successfully replaced sources and preserves unrelated corrections.
   **Verify**: `python3 -m unittest tests.test_workflow tests.test_agent_cli` → all pass.

## Test plan

Model successful reset on existing cases in `tests/test_workflow.py`, then add byte-snapshot failure cases for profiles, rules, CSV/PDF parsing, Ollama failure behavior, and final commit failure. Verify with `python3 -m unittest tests.test_workflow tests.test_agent_cli`.

## Done criteria

- [ ] Failed reset preserves ledger, review output, report, and corrections.
- [ ] Successful reset removes only intended corrections.
- [ ] Mixed-source failures respect Plan 001.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- Plan 005 is not complete or exposes no way to stage corrections with ledger state.
- Stable source-to-transaction association cannot be established for reset.
- Success semantics conflict with a newer public contract.

## Maintenance notes

Reset regression tests should accompany every new failure point in the import pipeline. Keep reset distinct from replace: only reset removes saved corrections.
