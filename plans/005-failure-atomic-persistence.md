# Plan 005: Make ledger and correction persistence recoverable

> **Historical plan:** Do not execute this document directly. Use
> [the current reconciliation](README.md), its linked issue, and current main.

> **Executor instructions**: Treat this as a financial persistence change. Add failure tests before implementation, run every gate, and update the plan index.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_agent_cli.py tests/test_workflow.py docs/architecture.md`
> Stop if persistence functions or authoritative-artifact documentation have changed.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plans 001 and 004
- **Category**: tech-debt
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Normal imports truncate and rewrite ledger artifacts directly. `correct` stages files but replaces three targets sequentially without rollback. Interruption, disk exhaustion, or permission failure can therefore corrupt the cumulative ledger or leave corrections and derived review output inconsistent.

## Current state

```python
# honeymoney/cli.py:958-970
_write_csv(categorized_path, CATEGORIZED_COLUMNS, ledger_rows)
review_rows = [
    _to_review_row(row)
    for row in ledger_rows
    if row.get("needs_review") == "true"
]
_write_csv(review_needed_path, REVIEW_NEEDED_COLUMNS, review_rows)

# honeymoney/cli.py:853-854
for temporary_path, target in staged:
    os.replace(temporary_path, target)
```

`docs/architecture.md:10-28` defines the filesystem as the integration boundary. Preserve `categorized.csv` as the authoritative ledger and treat `review_needed.csv` and `import_report.json` as reproducible derivatives unless the implementation introduces a documented generation/manifest protocol.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Correction tests | `python3 -m unittest tests.test_agent_cli` | all pass |
| Workflow tests | `python3 -m unittest tests.test_workflow` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_agent_cli.py`, `tests/test_workflow.py`, and `docs/architecture.md`.

**Out of scope**: SQLite/database migration; transaction-ID changes; concurrency between multiple running processes unless needed for correctness; private data.

## Git workflow

- Branch: `advisor/005-failure-atomic-persistence`
- Use logical conventional commits: tests first, persistence protocol second, docs last.
- Do not push or open a PR unless instructed.

## Steps

1. Add deterministic fault-injection tests that fail staging, fsync, and the second/third replacement. Cover existing and previously absent targets. Assert no authoritative ledger truncation and either complete old generation or complete new generation after recovery.
   **Verify**: `python3 -m unittest tests.test_agent_cli tests.test_workflow` → new tests fail on current partial commits.
2. Choose and document one protocol: (a) atomically replace the authoritative ledger, then regenerate derivatives on demand/startup; or (b) stage a generation directory and atomically switch a manifest/pointer. Prefer (a) unless public paths make it impossible. Do not claim multi-file atomicity from sequential `os.replace`.
   **Verify**: add a focused recovery test → interruption yields a readable authoritative ledger and deterministically rebuilt derivatives.
3. Route import, interactive review, and `correct` through the common persistence boundary. Fsync files and, where supported, the containing directory after replacement. Clean stale temporary files safely.
   **Verify**: focused suites pass, including fault injection.
4. Update architecture documentation with authority, commit order, and recovery behavior.
   **Verify**: `rg -n 'authoritative|recover|temporary|atomic' docs/architecture.md` → protocol is explicit.

## Test plan

Model normal command tests on `tests/test_agent_cli.py:274`. Add mocked `os.replace`/write failures after each commit point, startup recovery, empty ledger, and successful operation byte-for-byte behavior.

## Done criteria

- [ ] Direct writes never truncate the live ledger before a complete replacement exists.
- [ ] Every injected failure has deterministic old/new state and recovery.
- [ ] Derived files can be reconciled from the authoritative state.
- [ ] All public output paths and columns remain unchanged.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- A correct design requires changing public artifact paths or formats.
- Cross-platform fsync/replace guarantees cannot meet the documented claim.
- The plan expands into a database migration.

## Maintenance notes

Reviewers should scrutinize crash points, cleanup ordering, and behavior when targets did not previously exist. Future artifacts must declare whether they are authoritative or derived.
