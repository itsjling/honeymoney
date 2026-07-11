# Plan 010: Detect duplicates against the cumulative ledger

> **Executor instructions**: Use the stable identities from Plans 008–009. Preserve the rule that duplicates are flagged, never auto-deleted. Update the index after verification.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_workflow.py tests/test_cli_bootstrap.py spec-v1.md`
> Stop if duplicate policy or identity semantics have drifted.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plans 008 and 009
- **Category**: bug
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Duplicate detection currently sees only transactions imported in the current invocation. The v1 specification explicitly includes matches across different imported files/accounts, so sequential imports can add unreviewed duplicates to the cumulative ledger.

## Current state

`honeymoney/cli.py:192` loads existing rows, but `cli.py:223` calls `_annotate_duplicate_suspicions(transactions)` with new rows only. `spec-v1.md:380-389` says possible duplicates use same/near date, amount, merchant/description, and different files/accounts, and must be flagged rather than deleted.

Corrections run after duplicate annotation and currently win. Preserve that ordering unless an explicit test documents why a reviewed row should be reflagged.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Workflow | `python3 -m unittest tests.test_workflow` | all pass |
| CLI duplicate tests | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_workflow.py`, and `tests/test_cli_bootstrap.py`.

**Out of scope**: auto-deleting/merging duplicates; identity/source migration; fuzzy merchant ML; performance optimization beyond correctness (Plan 011).

## Git workflow

- Branch: `advisor/010-cross-import-duplicates`
- Commit example: `fix: detect duplicates across imports`.

## Steps

1. Add sequential-import tests covering exact match, ±1-day match, different accounts, same account, unrelated row, source replacement, corrected historical row, and duplicate report counts. Decide in assertions whether only the new row or both rows are flagged; prefer flagging the new row to avoid mutating historical data unexpectedly.
   **Verify**: new tests fail because current code sees only the batch.
2. Compare new transactions against retained ledger rows after excluding rows from sources being replaced. Keep current exact/near-date key semantics and append flags/reasons idempotently.
   **Verify**: focused duplicate tests pass.
3. Ensure repeated execution does not duplicate reasons/flags and replacement does not compare a statement against its obsolete version.
   **Verify**: workflow tests pass after two repeated synthetic imports/replacements.

## Test plan

Model key/date cases on duplicate tests around `tests/test_cli_bootstrap.py:3030`. Add sequential workflow cases for exact and near-date matches, cross-account rows, replacement exclusion, corrected rows, false positives, idempotency, and report counts. Verify with `python3 -m unittest tests.test_workflow tests.test_cli_bootstrap`.

## Done criteria

- [ ] Sequential duplicates are flagged for review and never deleted.
- [ ] Replacement excludes obsolete source rows from comparison.
- [ ] Report counts and diagnostics are correct.
- [ ] Re-running is idempotent.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- Plans 008–009 are incomplete.
- Product policy cannot decide whether historical rows should be mutated.
- Correct behavior would auto-delete or auto-merge duplicates.

## Maintenance notes

All future import paths must compare against the retained ledger. Reviewer attention should focus on false positives and correction precedence.
