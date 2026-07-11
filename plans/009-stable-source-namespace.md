# Plan 009: Give statements a stable source namespace

> **Executor instructions**: Build on the identity decision in Plan 008. Preserve privacy and replacement behavior; update the index when done.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney/schema.py tests/test_workflow.py tests/test_cli_bootstrap.py README.md docs/architecture.md`
> Stop if Plan 008 did not define the source/identity boundary this plan expects.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: Plan 008
- **Category**: migration
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

A single-file import stores only the basename as `source_file`. Distinct statements named `may.csv` in different directories therefore collide during already-imported detection and replacement. The solution must distinguish sources without exposing unnecessary absolute private paths.

## Current state

`_relative_source` at `honeymoney/cli.py:2335-2340` uses the input file's parent as root for single-file imports, returning `path.name`. `_processed_source_files` and `_merge_into_ledger` compare that display string directly.

`source_file` is a public CSV column and human-readable traceability field. Introduce a separate stable source identity if needed; do not replace it with an absolute path containing usernames or private directories.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Workflow | `python3 -m unittest tests.test_workflow` | all pass |
| CLI | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `honeymoney/schema.py` if a source-ID column is approved, `tests/test_workflow.py`, `tests/test_cli_bootstrap.py`, `README.md`, and `docs/architecture.md`.

**Out of scope**: content-uploading/cloud registries; storing absolute private paths; transaction occurrence policy beyond Plan 008.

## Git workflow

- Branch: `advisor/009-stable-source-namespace`
- Commit example: `fix: namespace imported statement sources`.

## Steps

1. Add tests for same basename in different directories, folder versus single-file import, filename rename, replacement/reset, moved workspace, and legacy ledgers with basename-only sources.
   **Verify**: focused tests expose current rejection/deletion behavior.
2. Define a privacy-preserving stable source ID, such as a versioned digest of normalized statement provenance/content plus account identity, while keeping `source_file` human-readable. Reuse Plan-008 metadata where possible.
   **Verify**: design distinguishes same basenames and does not include absolute paths in output.
3. Update source matching/replacement and add deterministic legacy handling. Ambiguous basename-only legacy rows must stop or warn for explicit review; never bulk-delete guesses.
   **Verify**: all new and existing replace/reset tests pass.
4. Document source display versus source identity.
   **Verify**: README and architecture use the exact implemented field names/semantics.

## Test plan

Use temporary synthetic directories in `tests/test_workflow.py`. Import identical basenames from different parents, replace/reset each independently, move the workspace, rename a file, and load a legacy basename-only ledger. Verify with `python3 -m unittest tests.test_workflow tests.test_cli_bootstrap`.

## Done criteria

- [ ] Same-named files from different locations coexist.
- [ ] Replacement targets exactly one stable source.
- [ ] No absolute private path is added to public artifacts.
- [ ] Legacy ambiguity cannot cause silent deletion.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- Plan 008 selected an incompatible identity model.
- A new public CSV column is required without maintainer approval/migration plan.
- The only proposed discriminator leaks absolute paths or statement contents.

## Maintenance notes

Source identity must remain stable across supported workspace moves and explicit rename behavior. Reviewers should test both single-file and directory imports.
