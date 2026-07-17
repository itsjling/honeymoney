# Plan 012: Define spreadsheet-safe CSV exports

> **Historical plan:** Do not execute this document directly. Use
> [the current reconciliation](README.md), its linked issue, and current main.

> **Executor instructions**: Treat CSV columns as public contracts. Do not alter numeric semantics or destroy raw transaction text without a documented compatibility decision.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney/schema.py tests/test_cli_bootstrap.py tests/test_agent_cli.py README.md`
> Stop if an export-escaping policy already exists or public columns changed.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 005
- **Category**: security
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Statement-controlled merchant and description text is written directly into CSV files users are expected to open in spreadsheet software. Formula-leading text may be evaluated by some spreadsheet applications. The fix must protect spreadsheet consumers without turning legitimate negative numeric amounts into text or losing canonical raw values.

## Current state

- `honeymoney/cli.py:2043-2044` imports merchant/description from source rows.
- `_clean_text` at `cli.py:2203-2210` preserves `=`, `+`, `-`, `@`, tab, and leading whitespace.
- `_write_csv` at `cli.py:2471-2476` writes values unchanged.

`categorized.csv` and `review_needed.csv` columns are public interfaces. Apply escaping only to text cells at the export boundary; do not mutate in-memory identity inputs or numeric amount fields.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| CSV tests | `python3 -m unittest tests.test_cli_bootstrap tests.test_agent_cli` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `honeymoney/schema.py` if text-column classification belongs there, `tests/test_cli_bootstrap.py`, `tests/test_agent_cli.py`, and `README.md`.

**Out of scope**: HTML report XSS (already uses JSON plus `textContent`); changing amount fields to strings with apostrophes; sanitizing source data before identity generation; private fixtures.

## Git workflow

- Branch: `advisor/012-safe-spreadsheet-exports`
- Commit example: `fix: neutralize formulas in csv text cells`.

## Steps

1. Decide and document canonical-versus-display behavior. Recommended: preserve canonical text in memory and neutralize formula-leading text only when serializing spreadsheet-facing text columns, with a reversible prefix policy. Confirm whether re-import/corrections must remove that prefix.
   **Verify**: policy enumerates `=`, `+`, `-`, `@`, tab, carriage return, and leading whitespace; numeric amount columns are excluded.
2. Add synthetic tests for every dangerous prefix in merchant, description, notes, reason, category-like custom values, and safe negative amounts. Assert CSV bytes and read-back semantics.
   **Verify**: new tests fail against raw serialization.
3. Implement a centralized cell serializer used by `_write_csv` and `_csv_document`, driven by explicit text-column sets. Keep HTML report behavior unchanged.
   **Verify**: focused tests and correction round trips pass.

## Test plan

Add synthetic CSV rows for each dangerous prefix, leading whitespace/control variants, safe ordinary text, and negative/positive numeric amounts. Exercise both normal ledger output and `correct`'s `_csv_document` path, then read outputs back with `csv.DictReader`. Verify with `python3 -m unittest tests.test_cli_bootstrap tests.test_agent_cli`.

## Done criteria

- [ ] Dangerous text cells are neutralized in both CSV artifacts and correction writes.
- [ ] Numeric values remain numeric-looking and unchanged.
- [ ] Canonical identity/categorization does not include escape prefixes.
- [ ] Policy is documented and regression-tested.
- [ ] Full check passes.

## STOP conditions

- No reversible policy can preserve the existing CSV round-trip contract.
- A proposed fix modifies raw transaction identity inputs.
- The change escapes all `-`-prefixed numeric amounts.

## Maintenance notes

Every new free-text CSV column must be added to the safe-export classification. Review with Excel/LibreOffice behavior in mind, but keep tests deterministic and offline.
