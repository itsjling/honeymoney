# Improvement plan reconciliation

Reconciled on 2026-08-06 at commit
`afdbbbb22c414ea8e9d009e8681d3f1e831aab92` on branch
`codex/harden-import-persistence`.

The prior index named `db60208` as its base but gave the wrong full object ID.
The valid commit is `db60208eabeed87222d8268c70d0ca6fdd998777`.
Current `HEAD` is the source of truth for this reconcile. The old plan excerpts
and branch names remain as history and must not guide new work without a fresh
drift check.

## Status terms

- `DONE`: the plan's live behavior exists on current `HEAD`.
- `SUPERSEDED`: an accepted design replaced a key part of the old plan. Do not
  run the old steps.
- `TODO`: the live gap still exists and the plan remains fit to run.
- `BLOCKED`: the goal still matters but needs the named decision or dependency.
- `IN PROGRESS`: an executor owns the work now.
- `REJECTED`: the proposal no longer merits work.

## Current status

| Plan | Title | Status | Current owner or decision |
|---|---|---|---|
| [001](001-preserve-failed-replacements.md) | Preserve failed replacement rows | DONE | [#19](https://github.com/itsjling/honeymoney/issues/19) |
| [002](002-validate-public-config.md) | Validate public config | DONE | [#20](https://github.com/itsjling/honeymoney/issues/20) |
| [003](003-validate-profile-structure.md) | Validate profile structure | DONE | [#20](https://github.com/itsjling/honeymoney/issues/20) |
| [004](004-define-empty-corrections.md) | Define empty correction semantics | DONE | [#21](https://github.com/itsjling/honeymoney/issues/21) |
| [005](005-failure-atomic-persistence.md) | Make persistence recoverable | DONE | [#22](https://github.com/itsjling/honeymoney/issues/22) |
| [006](006-transactional-reset.md) | Make reset transactional | DONE | [#23](https://github.com/itsjling/honeymoney/issues/23) |
| [007](007-enforce-local-ollama.md) | Enforce local-only Ollama | DONE | [#18](https://github.com/itsjling/honeymoney/issues/18) |
| [008](008-stable-transaction-identity.md) | Stabilize transaction identity | SUPERSEDED | ADR 0001 plus ADR 0003 |
| [009](009-stable-source-namespace.md) | Stabilize source namespace | DONE | ADR 0001 / [#24](https://github.com/itsjling/honeymoney/issues/24) |
| [010](010-cross-import-duplicates.md) | Detect cross-import duplicates | SUPERSEDED | ADR 0003 and the identity-backed duplicate policy |
| [011](011-optimize-duplicate-window.md) | Optimize duplicate scanning | SUPERSEDED | The old near-date scan was removed |
| [012](012-safe-spreadsheet-exports.md) | Make CSV exports spreadsheet-safe | DONE | [#26](https://github.com/itsjling/honeymoney/issues/26) |
| [013](013-pin-ci-toolchain.md) | Stabilize CI dependency resolution | DONE | [#28](https://github.com/itsjling/honeymoney/issues/28) |
| [014](014-single-ledger-read.md) | Read ledger once per import | SUPERSEDED | The persistence and CLI-boundary work replaced the old helper seam |
| [015](015-local-categorization-memory.md) | Add local categorization memory | DONE | ADR 0002 and the opt-in memory path |
| [016](016-profile-validation-command.md) | Add profile validation tooling | DONE | `profile validate` and bounded preview |
| [017](017-extract-cli-boundaries.md) | Extract CLI module boundaries | DONE | `importers`, `normalization`, `identity`, `duplicates`, and `persistence` modules |
| [018](018-accounting-safe-ollama-categorization.md) | Make Ollama categorization accounting-safe | DONE | Merged in `9648274` |

There are no `TODO`, `BLOCKED`, or `IN PROGRESS` plans. None of these files is
ready to execute. Start a new audit or write a new plan for later work.

## Reconciliation evidence

### Plans 001-007 — DONE

Current import, config, correction, persistence, reset, and Ollama transport
tests passed. The live architecture keeps failed source rows and corrections,
validates public input before writes, publishes financial files through the
recoverable generation protocol, and sends Ollama traffic only to checked
loopback endpoints.

Key current files:

- `honeymoney/persistence.py`
- `honeymoney/corrections.py`
- `honeymoney/identity_state.py`
- `honeymoney/ollama.py`
- `tests/test_workflow.py`
- `tests/test_agent_cli.py`
- `tests/test_config_cli.py`
- `tests/test_ollama_transport.py`

### Plan 008 — SUPERSEDED

ADR 0001 delivered stable source-occurrence and record identity. ADR 0003 then
replaced Plan 008's public-row premise with a canonical overlap ledger. Two
equal source occurrences keep separate hidden identities, while the public
ledger may hold one canonical slot when they prove the same event. Same-source
repeats keep their supported count. Combined and sequential imports produce
the same canonical state.

The useful goal is complete, but the old done criterion that every separate
source occurrence must remain a separate public ledger row conflicts with the
accepted overlap design. Do not run Plan 008.

Evidence:

```sh
python3 -m unittest tests.test_identity tests.test_identity_state \
  tests.test_overlap tests.test_overlap_state tests.test_workflow
```

### Plan 009 — DONE

The hidden identity manifest owns source identity. `source_file` remains display
text. Same-named files from distinct locations can coexist, replacement targets
one proven source, accepted renames keep ownership, and public output does not
gain an absolute path identity field.

### Plans 010-011 — SUPERSEDED

The old pairwise, near-date duplicate scan no longer exists. The live policy
uses identity-backed, same-account exact fingerprints across distinct v2
sources, then canonical overlap handling and explicit duplicate resolution.
It does not use Plan 010's one-day heuristic.

The new evaluator has a direct linear-work check over 8,000 rows and does not
rescan candidate groups for each occurrence:

```sh
python3 -m unittest tests.test_duplicates tests.test_duplicate_performance \
  tests.test_overlap tests.test_workflow
```

Both old plans are unfit to run because their input rules and data path are
gone.

### Plan 012 — DONE

Spreadsheet-facing CSV files share the versioned, reversible text-cell codec.
The focused round-trip and formula-prefix suite passed:

```sh
python3 -m unittest tests.test_spreadsheet_safe_csv
```

### Plan 013 — DONE, local tool check needs bootstrap

The repo has exact development constraints, bounded package metadata, cache
keys tied to the constraints, distribution checks, and a written refresh path.
`python3 -m pip check` passed on 2026-08-06.

`python3 scripts/check_constraints.py` did not pass in the current shell because
four installed tools lag the checked-in versions: `coverage` 7.15.2 vs 7.15.3,
`cryptography` 49.0.0 vs 50.0.0, `filelock` 3.32.0 vs 3.32.2, and `ruff` 0.16.0
vs 0.16.1. This is local environment drift, not a missing plan change. Run
`./scripts/bootstrap.sh` before the next full check.

### Plan 014 — SUPERSEDED

The old `_merge_into_ledger(path, ...)` seam is gone. Current imports resolve
identity, overlaps, duplicate state, and persistence through their owning
modules and one loaded workspace state. A helper-only read-count change would
target code that no longer exists.

### Plan 015 — DONE

ADR 0002 defines opt-in, local, correction-derived memory. It rebuilds in
memory, uses exact guarded keys, rejects weak or conflicting evidence, keeps
rules and exact corrections above it, and sends no data to a new network path.
The later `learn` command adds an explicit managed-rule path; it does not replace
the opt-in memory contract.

Evidence:

```sh
python3 -m unittest tests.test_transaction_categorization tests.test_learning
```

### Plan 016 — DONE

`honeymoney profile validate PROFILE [--config CONFIG] [--input FILE]` uses the
production validators and parser path. Preview is bounded, read-only, and
covered in the agent and profile suites.

### Plan 017 — DONE

The planned import seams now have named modules and checked boundaries:

- `honeymoney/importers.py` owns profile choice and CSV/PDF extraction.
- `honeymoney/normalization.py` owns pure normalized row creation.
- `honeymoney/identity.py` and `honeymoney/identity_state.py` own source and
  record identity.
- `honeymoney/duplicates.py` and `honeymoney/overlap.py` own duplicate and
  canonical overlap rules.
- `honeymoney/persistence.py` owns recoverable file generations.

`tests/test_module_boundaries.py` passed and checks that importers do not depend
on the CLI and normalization stays free of filesystem work.

### Plan 018 — DONE

Implementation commit `8d9a857` merged to `main` in `9648274`. Current
classification-policy, Ollama, cash-flow, workflow, and agent suites passed.
The saved 2026-07-17 local benchmark result remains the last live-model check:
100% accounting safety and 100% ordinary-category accuracy for its synthetic
corpus. This reconciliation did not rerun live Ollama.

## Checks run on 2026-08-06

This focused command ran 533 tests in 114.387 seconds and passed:

```sh
python3 -m unittest \
  tests.test_workflow tests.test_agent_cli tests.test_cli_bootstrap \
  tests.test_config_cli tests.test_import_profiles tests.test_identity \
  tests.test_identity_state tests.test_overlap tests.test_overlap_state \
  tests.test_duplicates tests.test_duplicate_performance \
  tests.test_spreadsheet_safe_csv tests.test_learning \
  tests.test_module_boundaries tests.test_classification_policy \
  tests.test_transaction_categorization tests.test_ollama \
  tests.test_ollama_transport tests.test_cash_flow \
  tests.test_cash_flow_review tests.test_environment_smoke
```

Also run:

```sh
python3 -m pip check
python3 scripts/check_constraints.py
```

The first passed. The second found the local tool drift recorded under Plan
013. This reconcile did not run `./scripts/check.sh`: the advisor workflow bars
installs and build steps in the user's worktree. It also did not run live
Ollama, inspect private statements, inspect generated household ledgers, or run
a new full-code audit.

## Dependency state

The old plan chain is closed. ADR 0001, ADR 0002, and ADR 0003 now own the
identity, memory, and overlap choices. Later work must treat those records and
the current public CLI, JSON, CSV, config, and filesystem contracts as fixed
unless a new approved plan changes them.

## Findings kept out of the backlog

- Merging old `codex/improve-plan-*` branches: rejected. Current `main` already
  contains reviewed implementations or newer designs.
- Restoring the near-date duplicate heuristic: rejected. It conflicts with the
  accepted identity-backed overlap policy.
- Requiring one public ledger row per source occurrence: rejected. ADR 0003
  keeps source evidence in hidden state and publishes canonical slots.
- Generic embeddings or cloud learning: rejected. They breach the local,
  deterministic memory boundary.
- Adding a second profile parser for preview: rejected. The shipped command
  reuses the production path.
- Replacing local files with a database: rejected. The filesystem boundary is
  an accepted product choice.

## Next reconcile

1. Record the current full HEAD.
2. Read this index, every plan, the architecture guide, and all ADRs that touch
   a changed plan.
3. Spot-check `DONE` behavior with cheap synthetic tests.
4. Check `TODO` plans for drift; refresh them or close them if the gap is gone.
5. Check any `BLOCKED` reason against current code and decisions.
6. Flag stale `IN PROGRESS` work and inspect its worktree if one still exists.
7. Keep private files, live model output, and generated financial data out of
   plans and reports.
