# Plan 017: Extract import, normalization, and ledger modules from the CLI

> **Executor instructions**: This is a behavior-preserving final consolidation. Move one seam at a time, keep compatibility adapters, and update the index only after full verification.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney tests`
> Because this plan intentionally runs last, compare every current-state symbol with live code after Plans 001–016. Any mismatch requires refreshing this plan rather than improvising.

## Status

- **Priority**: P3
- **Effort**: L
- **Risk**: MED
- **Depends on**: Plans 001–016
- **Category**: tech-debt
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

`honeymoney/cli.py` combines command dispatch, workspace setup, profile selection, CSV/PDF parsing, normalization, identity, persistence, corrections, and reporting in roughly 2,500 lines. Core tests import its private functions, so isolated changes carry a broad regression surface. After behavior stabilizes, cohesive deep modules can make future parser and ledger work safer.

## Current state

- Command routing begins at `honeymoney/cli.py:132`.
- Import orchestration begins at `cli.py:1392`; normalization at `cli.py:2026`; persistence helpers are spread across `cli.py:837`, `903`, `942`, and `2471`.
- `tests/golden_helpers.py:9` and `tests/test_workflow.py:17` import private CLI internals.
- `docs/architecture.md:37-44` currently maps most behavior to `cli.py`.

Public CLI text, JSON envelopes, exit codes, CSV columns, config fields, corrections, and profiles must remain byte/behavior compatible. This plan does not redesign them.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| CLI contract | `python3 -m unittest tests.test_agent_cli tests.test_cli_bootstrap` | all pass |
| Goldens/workflow | `python3 -m unittest tests.test_import_profiles tests.test_transaction_categorization tests.test_workflow` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 after every extraction seam |

## Scope

**In scope**: `honeymoney/ledger.py` (create), `honeymoney/normalization.py` (create), `honeymoney/importers.py` or a same-named package (create), `honeymoney/cli.py`, `tests/golden_helpers.py`, `tests/test_workflow.py`, `tests/test_cli_bootstrap.py`, `tests/test_import_profiles.py`, and `docs/architecture.md`.

**Out of scope**: new behavior; public schema changes; dependency changes; parser rewrites; moving `rules.py`, `ollama.py`, or `report.py` merely for symmetry.

## Git workflow

- Branch: `advisor/017-extract-cli-boundaries`
- One conventional commit per seam, e.g. `refactor: extract ledger persistence`.
- Do not push/open a PR unless instructed.

## Steps

1. Capture a module dependency map and choose deep interfaces. Recommended modules: `ledger.py` owns read/merge/commit/recovery; `normalization.py` owns row normalization/amounts/identity; `importers.py` or a package owns profile selection plus CSV/PDF extraction. Avoid a generic `utils.py`.
   **Verify**: written design lists each moved symbol, callers, allowed dependency direction, and public compatibility adapter.
2. Extract ledger persistence first because Plan 005 defines its interface. Leave delegating imports/wrappers in `cli.py` temporarily where tests depend on them.
   **Verify**: full check passes; `rg -n '^def (_read_ledger|_merge_into_ledger|_atomic_write)' honeymoney/cli.py` finds only documented compatibility wrappers or none.
3. Extract normalization/identity as pure functions with explicit config/profile inputs. Move tests to import the owning module while retaining compatibility where external callers may exist.
   **Verify**: goldens, workflow, identity, and full checks pass.
4. Extract profile loading and CSV/PDF import orchestration without changing optional dependency behavior or warning strings.
   **Verify**: profile goldens and full check pass.
5. Reduce `cli.py` to command parsing, user interaction, progress, and orchestration. Remove wrappers only after all internal callers/tests migrate and no documented external usage exists.
   **Verify**: `wc -l honeymoney/cli.py` is materially lower; full check passes; CLI subprocess output remains identical.
6. Update `docs/architecture.md` source map and add module-boundary guidance to `AGENTS.md` only if needed.
   **Verify**: every new module is documented with its responsibility and dependency direction.

## Test plan

No behavior snapshots should be regenerated. Existing 141+ tests are the characterization suite; add only module-interface tests needed to enforce dependency boundaries and purity.

## Done criteria

- [ ] Each extracted module has one cohesive responsibility and narrow interface.
- [ ] `cli.py` contains commands/interactions/orchestration, not parser or persistence internals.
- [ ] Public text/JSON/CSV/config behavior is unchanged.
- [ ] No generic junk-drawer module or circular imports.
- [ ] Full check passes after every seam and at completion.

## STOP conditions

- Any earlier plan remains TODO or behavior is still changing in an extraction area.
- A move requires changing a public contract to make progress.
- Circular imports appear or a proposed module has multiple unrelated responsibilities.
- Full verification fails twice after one seam.

## Maintenance notes

Review this as a series of behavior-preserving changes, not one giant diff. Future parser additions should depend on normalization interfaces and persistence only through orchestration, never import CLI internals.
