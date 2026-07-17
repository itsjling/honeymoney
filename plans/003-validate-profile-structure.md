# Plan 003: Reject incomplete import profiles before row processing

> **Historical plan:** Do not execute this document directly. Use
> [the current reconciliation](README.md), its linked issue, and current main.

> **Executor instructions**: Follow each step, preserve public profile compatibility unless explicitly tested, and update the index when complete.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney/data/profiles tests/test_import_profiles.py tests/test_cli_bootstrap.py docs/golden-datasets.md`
> Stop if bundled profile shapes or `_validate_profile` have drifted.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plan 002
- **Category**: bug
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

A profile with missing mappings can currently produce blank dates/descriptions and `0.00` amounts. Profile mistakes must fail before financial output is written, with clear messages that identify the profile and missing invariant.

## Current state

- `honeymoney/cli.py:1272-1290` requires only `account_id` and validates optional owner/payment values.
- `_signed_amount` at `cli.py:2231-2248` returns zero when no amount/debit/credit mapping exists.
- Synthetic golden conventions are defined in `docs/golden-datasets.md`; never use private statements.

Bundled CSV and PDF profiles under `honeymoney/data/profiles/` are compatibility contracts. Validation must support their distinct table, regex, and word-row modes.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Profile goldens | `python3 -m unittest tests.test_import_profiles` | all pass |
| CLI tests | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `tests/test_import_profiles.py`, `tests/test_cli_bootstrap.py`, and `docs/golden-datasets.md` only if invariants need documenting.

**Out of scope**: modifying bundled profiles unless a test proves one violates the agreed invariant; building a profile-authoring command (Plan 016); real statements.

## Git workflow

- Branch: `advisor/003-validate-profile-structure`
- Commit example: `fix: reject incomplete import profiles`.
- Do not push or open a PR unless instructed.

## Steps

1. Inventory bundled profile modes and encode explicit invariants: non-empty ID/account metadata; one supported parser definition; a usable date mapping/extraction; exactly one coherent amount strategy; required parser-specific regex/table/word settings; valid date formats and sign settings.
   **Verify**: `python3 -m unittest tests.test_import_profiles` → existing bundled profiles pass unchanged.
2. Add synthetic tests for missing date source, missing amount strategy, conflicting amount strategies, malformed columns, malformed PDF settings, and mapped headers absent from an actual CSV. Assert failure occurs before output files are changed.
   **Verify**: focused new tests fail against current permissive validation.
3. Expand `_validate_profile` and add per-statement header validation. Produce messages with profile ID, field path, and expected shape.
   **Verify**: `python3 -m unittest tests.test_import_profiles tests.test_cli_bootstrap` → all pass.

## Test plan

Follow `tests/test_import_profiles.py` and `tests/golden_helpers.py`. Use minimal synthetic dictionaries/CSV files; do not regenerate goldens blindly.

## Done criteria

- [ ] Incomplete profiles fail before normalization or output mutation.
- [ ] Every bundled profile passes validation.
- [ ] Error messages identify the exact invalid field.
- [ ] Full check passes with no private fixtures.

## STOP conditions

- Invariants would reject a bundled profile whose behavior is covered by a golden.
- Two existing parser modes require contradictory semantics not expressible cleanly.
- Fixing validation requires redesigning the profile format.

## Maintenance notes

Any future parser mode needs its own validator and golden fixture. Reviewers should reject validation that merely checks dictionary presence without validating the mapped statement headers.
