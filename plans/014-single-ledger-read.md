# Plan 014: Read the cumulative ledger once per import

> **Executor instructions**: Preserve merge ordering and replacement behavior exactly. Update the plan index after verification.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_workflow.py tests/test_cli_bootstrap.py`
> Stop if the persistence API from Plan 005 no longer reads the ledger in the described path.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: Plans 005 and 009
- **Category**: perf
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

The import pipeline loads the full cumulative CSV before processing, retains it, then `_merge_into_ledger` reads and materializes the same file again. Passing the already-loaded rows removes redundant I/O for every import and gives later cross-ledger operations one consistent snapshot.

## Current state

`honeymoney/cli.py:192` assigns `existing_ledger_rows`. At `cli.py:242`, `_merge_into_ledger` is called with a path, and at `cli.py:947-950` it calls `_read_ledger` again.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Workflow | `python3 -m unittest tests.test_workflow` | all pass |
| CLI | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_workflow.py`, and `tests/test_cli_bootstrap.py`.

**Out of scope**: streaming ledger redesign; database storage; changing row order; duplicate algorithm changes.

## Git workflow

- Branch: `advisor/014-single-ledger-read`
- Commit example: `perf: reuse loaded ledger during import`.

## Steps

1. Add a focused mocked/spied `_read_ledger` test asserting one read per import and preserving source filtering/overwrite/order behavior.
   **Verify**: test fails with two reads before implementation.
2. Change `_merge_into_ledger` to accept the immutable existing-row snapshot rather than a path. Update all callers and avoid mutating the caller's row dictionaries.
   **Verify**: focused workflow/CLI suites pass and read-count is one.
3. Confirm Plan-005 recovery and Plan-010 duplicate logic use the same snapshot.
   **Verify**: `./scripts/check.sh` passes.

## Test plan

Add a spy/mock test that counts `_read_ledger` calls during one import and asserts exactly one. Reuse existing merge/replacement cases to compare row content and order before/after the signature change. Verify with `python3 -m unittest tests.test_workflow tests.test_cli_bootstrap`.

## Done criteria

- [ ] One ledger parse occurs per import command.
- [ ] Merge/replacement output and ordering are unchanged.
- [ ] Existing rows are not mutated unexpectedly.
- [ ] Full check passes.

## STOP conditions

- Plan 005 intentionally reloads after an atomic generation switch for consistency.
- Removing the second read would use a stale snapshot after an authorized concurrent mutation.
- Tests reveal row-order behavior is undefined and externally depended upon.

## Maintenance notes

If multi-process locking is introduced later, reconsider snapshot lifetime rather than reintroducing silent duplicate reads.
