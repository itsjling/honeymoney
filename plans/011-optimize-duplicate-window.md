# Plan 011: Make near-date duplicate detection scale linearly after sorting

> **Executor instructions**: Preserve exact duplicate semantics. Establish output equivalence before optimizing and update the index afterward.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_cli_bootstrap.py tests/test_workflow.py`
> Stop if Plan 010 changed duplicate grouping without an updated equivalence baseline.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plan 010
- **Category**: perf
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Each same-key transaction is compared with every later transaction. Synthetic measurements grew from 0.136 seconds at 1,000 rows to 2.132 seconds at 4,000, consistent with quadratic work. Cross-ledger comparison will increase input size, so this should be fixed immediately after Plan 010.

## Current state

At `honeymoney/cli.py:2393-2404`, nested loops compare all pairs in each non-date key group and parse dates repeatedly. Existing near-date tests around `tests/test_cli_bootstrap.py:3162` use only tiny groups.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Duplicate tests | `python3 -m unittest tests.test_cli_bootstrap tests.test_workflow` | all pass |
| Synthetic benchmark | `python3 -m unittest tests.test_duplicate_performance` | pass within a generous deterministic bound; create this module if chosen |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_cli_bootstrap.py`, `tests/test_workflow.py`, and `tests/test_duplicate_performance.py` if a separate deterministic performance module is created.

**Out of scope**: changing key fields, time window, review policy, transaction identity, external benchmark dependencies.

## Git workflow

- Branch: `advisor/011-optimize-duplicate-window`
- Commit example: `perf: bound duplicate date comparisons`.

## Steps

1. Build an oracle test using the current pairwise implementation semantics on synthetic rows: invalid dates, identical dates, ±1 day, ±2 days, many matches, and multiple keys. Record flags/reasons, not timing only.
   **Verify**: oracle tests pass before optimization.
2. Parse each date once, group by duplicate key, sort valid dates, and scan a bounded one-day window. Preserve invalid-date behavior and idempotent marking.
   **Verify**: oracle equivalence tests pass.
3. Add a generous scaling regression using deterministic data. Prefer an operation-count seam if wall-clock timing is flaky; otherwise assert 4,000 rows complete under a conservative CI-safe ceiling and document why.
   **Verify**: focused performance test passes repeatedly.

## Test plan

Create an oracle-equivalence matrix for invalid dates, date boundaries, multiple keys, and large groups. Use an operation-count assertion rather than timing when possible; otherwise isolate a conservative timing test. Verify with the focused duplicate modules three consecutive times, then `./scripts/check.sh`.

## Done criteria

- [ ] Output matches the pairwise oracle for all edge cases.
- [ ] Date parsing occurs once per row, not per pair.
- [ ] Large same-key groups no longer exhibit quadratic comparisons.
- [ ] Full check passes without flaky thresholds.

## STOP conditions

- Plan 010 changes the comparison semantics while this work is underway.
- A timing assertion is unstable across three local runs; switch to operation-count instrumentation.
- Optimization changes which rows are flagged.

## Maintenance notes

If the duplicate window changes from one day, update the bounded scan and oracle cases together.
