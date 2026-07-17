# Plan 001: Preserve ledger rows when replacement imports fail

> **Historical plan:** Do not execute this document directly. Use
> [the current reconciliation](README.md), its linked issue, and current main.

> **Executor instructions**: Follow every step and verification gate. Update this plan's row in `plans/README.md` when complete. Do not push or open a PR unless instructed.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py tests/test_workflow.py tests/test_cli_bootstrap.py`
> If the cited replacement flow has changed, stop and report rather than adapting this plan silently.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

`--replace` currently removes old ledger rows for every discovered source, including PDFs that were skipped or failed to parse. A failed replacement must preserve the last known-good ledger rows and return partial success with warnings.

## Current state

- `honeymoney/cli.py:188-191` builds `source_files` before parsing.
- `honeymoney/cli.py:1436-1460` records PDF failure and continues with no rows.
- `honeymoney/cli.py:241-243` passes the full discovered set into `_merge_into_ledger`.

```python
replace_sources = source_files if args.replace or args.reset else None
ledger_rows = _merge_into_ledger(categorized_path, transactions, replace_sources)
```

The ledger and `import_report.json` are public filesystem contracts. Failed sources must remain reported as failed; do not disguise failure as success.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `python3 -m unittest tests.test_workflow tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | formatting, lint, all tests, and build pass |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_workflow.py`, `tests/test_cli_bootstrap.py`.

**Out of scope**: transaction-ID redesign; source-name redesign; atomic multi-file persistence; private statement fixtures.

## Git workflow

- Branch: `advisor/001-preserve-failed-replacements`
- Use conventional commits, e.g. `fix: preserve ledger rows on failed replacement`.
- Do not push or open a PR unless instructed.

## Steps

1. Add synthetic regression tests for a previously imported PDF replaced when PDF support is disabled and when parsing raises. Assert old ledger bytes/rows remain, the source report is failed/skipped, warnings remain, and unrelated successful sources still replace normally.
   **Verify**: `python3 -m unittest tests.test_workflow` → new tests fail for the expected row-deletion reason.
2. Have `_import_transactions` expose the set of source identities that completed processing, or derive it from `file_reports` using only `status == "processed"`. Use that set—not all discovered inputs—as `replace_sources`.
   **Verify**: `python3 -m unittest tests.test_workflow` → new and existing replacement tests pass.
3. Confirm mixed-folder replacement preserves failed sources while replacing successful ones and keeps the JSON report accurate.
   **Verify**: `python3 -m unittest tests.test_cli_bootstrap` → all pass.

## Test plan

Model tests on existing replace/reset cases in `tests/test_workflow.py`. Cover disabled PDF, raised PDF parser failure, mixed success/failure, and successful replacement unchanged.

## Done criteria

- [ ] Failed/skipped replacements retain prior ledger rows.
- [ ] Successful replacements retain current behavior.
- [ ] JSON warnings and file statuses remain truthful.
- [ ] `./scripts/check.sh` passes.
- [ ] Only in-scope files and `plans/README.md` changed.

## STOP conditions

- Replacement is found to be intentionally destructive on parser failure in a newer ADR/spec.
- The fix requires changing transaction IDs or `source_file` format.
- A focused test fails twice for an unrelated reason.

## Maintenance notes

Future importers must emit the same processed/failed/skipped status vocabulary. Reviewers should ensure a zero-row but successfully parsed statement is distinguishable from a failed statement.
