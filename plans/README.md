# Improvement plan reconciliation

This index was reconciled on 2026-07-17 against current `main` commit
`a91db80fe5b3b20cccf3abf4b51f58ec199d3fde`. The original plans were written
on 2026-07-11 against `aa0eedf`; their source excerpts, branch names, and line
numbers are historical context, not execution instructions for the live
architecture.

Current `main` is the only implementation source of truth. A plan is `DONE`
only when its observable acceptance criteria pass there. A commit on another
branch is reference material, never status evidence. The command below returns
exit 1 for every local `codex/improve-plan-*` tip and `codex/improve-all`, which
means none is an ancestor of the reconciled commit:

```sh
git merge-base --is-ancestor BRANCH 5ef77af167be6dca2ceeb47ca4462d9538e83764
```

Do not merge, rebase, cherry-pick, publish, or delete those branches as part of
this reconciliation. Any useful implementation must be ported independently
through the linked issue and revalidated against the current cumulative-ledger,
correction, reconciliation, review, PDF, and JSON contracts.

## Status vocabulary

- `DONE`: every observable acceptance criterion passes on the recorded main.
- `PARTIAL`: current main provides useful related behavior but still misses a
  stated acceptance criterion.
- `TODO`: the planned behavior is observably absent.
- `SUPERSEDED`: current architecture or a narrower accepted design replaced the
  original objective; the rationale names where the useful intent went.
- `REJECTED`: the proposal conflicts with an accepted boundary or lacks enough
  value to justify a public contract.
- `BLOCKED`: the objective remains valid but cannot proceed until the named
  dependency or decision is resolved.

## Reconciled status

### Executed specifications

- [018](018-accounting-safe-ollama-categorization.md) — Make Ollama
  categorization accounting-safe and semantically constrained. **DONE** in
  isolated execution: implementation commit `8d9a857` passed independent
  offline review and the `qwen2.5:3b` benchmark passed with 100% accounting
  safety and 100% ordinary-category accuracy. Worktree:
  `/tmp/honeymoney-plan-018`; maintainer merge pending; not published to the
  issue tracker.

### Historical-plan reconciliation

| Plan | Title | Priority | Reconciled status | Follow-up |
|---|---|---:|---|---|
| [001](001-preserve-failed-replacements.md) | Preserve failed replacement rows | P1 | TODO | [#19](https://github.com/itsjling/honeymoney/issues/19) |
| [002](002-validate-public-config.md) | Validate public config | P1 | DONE | [#20](https://github.com/itsjling/honeymoney/issues/20) |
| [003](003-validate-profile-structure.md) | Validate profile structure | P1 | DONE | [#20](https://github.com/itsjling/honeymoney/issues/20) |
| [004](004-define-empty-corrections.md) | Define empty correction semantics | P1 | DONE | [#21](https://github.com/itsjling/honeymoney/issues/21) |
| [005](005-failure-atomic-persistence.md) | Make persistence recoverable | P1 | PARTIAL | [#22](https://github.com/itsjling/honeymoney/issues/22) |
| [006](006-transactional-reset.md) | Make reset transactional | P1 | TODO | [#23](https://github.com/itsjling/honeymoney/issues/23) |
| [007](007-enforce-local-ollama.md) | Enforce local-only Ollama | P1 | TODO | [#18](https://github.com/itsjling/honeymoney/issues/18) |
| [008](008-stable-transaction-identity.md) | Stabilize transaction identity | P1 | PARTIAL | [#24](https://github.com/itsjling/honeymoney/issues/24) |
| [009](009-stable-source-namespace.md) | Stabilize source namespace | P1 | PARTIAL | [#24](https://github.com/itsjling/honeymoney/issues/24) |
| [010](010-cross-import-duplicates.md) | Detect cross-import duplicates | P2 | TODO | [#25](https://github.com/itsjling/honeymoney/issues/25) |
| [011](011-optimize-duplicate-window.md) | Optimize duplicate scanning | P2 | TODO | [#25](https://github.com/itsjling/honeymoney/issues/25) |
| [012](012-safe-spreadsheet-exports.md) | Make CSV exports spreadsheet-safe | P2 | TODO | [#26](https://github.com/itsjling/honeymoney/issues/26) |
| [013](013-pin-ci-toolchain.md) | Stabilize CI dependency resolution | P3 | TODO | [#28](https://github.com/itsjling/honeymoney/issues/28) |
| [014](014-single-ledger-read.md) | Read ledger once per import | P3 | SUPERSEDED | [#29](https://github.com/itsjling/honeymoney/issues/29) |
| [015](015-local-categorization-memory.md) | Add local categorization memory | P2 | SUPERSEDED | — |
| [016](016-profile-validation-command.md) | Add profile validation tooling | P2 | SUPERSEDED | [#20](https://github.com/itsjling/honeymoney/issues/20), [#16](https://github.com/itsjling/honeymoney/issues/16) |
| [017](017-extract-cli-boundaries.md) | Extract CLI module boundaries | P3 | PARTIAL | [#29](https://github.com/itsjling/honeymoney/issues/29) |

## Evidence by plan

### 001 — TODO

Current import orchestration builds `source_files` before parsing and passes the
entire discovered set to `_merge_into_ledger` for `--replace` and `--reset`.
Failed or skipped inputs can therefore delete their last known-good ledger
rows. Verify the live flow with:

```sh
sed -n '200,265p' honeymoney/cli.py
python3 -m unittest tests.test_workflow tests.test_cli_bootstrap
```

The focused suites preserve successful replacement behavior but do not cover
failed mixed-source replacement. Issue #19 adds that observable contract using
synthetic CSV/PDF failures.

### 002 — DONE

Configuration loading now validates every public container and nested scalar
used by the CLI, including path references, vocabularies, PDF settings,
exchange rates, thresholds, reconciliation, category policies, and Ollama
numeric limits. Boolean-as-number, non-finite, out-of-range, duplicate, and
empty values fail with field-specific errors before statement processing.

```sh
python3 -m unittest tests.test_config_cli tests.test_agent_cli
```

Structured commands preserve the versioned single-document error envelope and
exit 2 contract. Checked-in examples and starter configuration remain valid.

### 003 — DONE

Profiles now validate account metadata and controlled values, exactly one CSV
or PDF parser mode, coherent date and amount strategies, sign configuration,
regular expressions, and word/sectioned-word parser structure. The selected
CSV profile's mapped headers are checked against the statement before row
normalization, and profiles/mappings are loaded before reset can change saved
corrections.

```sh
python3 -m unittest tests.test_import_profiles tests.test_cli_bootstrap
```

Malformed-profile regression cases assert field paths and unchanged artifacts;
all bundled profile goldens pass unchanged.

### 004 — DONE

Structured corrections now reject empty or whitespace-only non-note fields
before any artifact changes. Omitted fields preserve saved values and review
state, while an explicit empty note is persisted as a durable clear operation
that survives correction reload and later imports. Empty or `Unknown`
categories cannot be marked resolved without an explicit accounting flow.

```sh
python3 -m unittest tests.test_agent_cli tests.test_workflow tests.test_cash_flow_review
```

The machine and human documentation now states the omitted-versus-empty
contract explicitly.

### 005 — PARTIAL

Correction and review operations stage files, fsync file contents, keep
backups, and attempt rollback. Normal imports still write the ledger, review
CSV, and report separately; directory fsync, startup recovery, and a documented
authoritative/derived protocol are absent.

```sh
sed -n '326,400p' honeymoney/corrections.py
sed -n '1650,1705p' honeymoney/cli.py
rg -n 'authoritative|recover|commit order' docs/architecture.md
```

Issue #22 must reconcile both persistence paths and current reconciliation
artifacts. Sequential `os.replace` calls must not be described as multi-file
atomicity.

### 006 — TODO

`--reset` now validates configuration, profiles, mappings, selected CSV headers,
and statement parsing before calling `_remove_corrections`. It still removes
saved corrections before rules, Ollama, reconciliation, and output persistence;
a failure in those later phases can therefore lose reviewed state.

```sh
sed -n '208,265p' honeymoney/cli.py
sed -n '1810,1850p' honeymoney/cli.py
python3 -m unittest tests.test_workflow tests.test_agent_cli
```

Issue #23 is correctly blocked by the common persistence boundary in #22.

### 007 — TODO

The Ollama client minimizes transaction payloads and defaults to localhost, but
configured URLs go directly to `urllib.request.urlopen`; schemes, credentials,
resolved addresses, and redirects are not constrained to loopback.

```sh
sed -n '20,55p' honeymoney/ollama.py
sed -n '199,255p' honeymoney/ollama.py
python3 -m unittest tests.test_ollama
```

Issue #18 is the highest-priority privacy follow-up. The live smoke test remains
excluded unless a maintainer explicitly requests it.

### 008 — PARTIAL

Non-colliding IDs are deterministic, exclude source filenames, and have golden
coverage. Collision occurrence suffixes are still assigned from the current
batch, so separate versus combined imports can collapse rows or change IDs; no
legacy ambiguity migration contract exists.

```sh
sed -n '3070,3110p' honeymoney/cli.py
python3 -m unittest tests.test_import_profiles tests.test_cli_bootstrap tests.test_workflow
```

Issue #24 requires the identity ADR and migration behavior before code changes.

### 009 — PARTIAL

`source_file` remains relative and avoids absolute-path disclosure, and it is
excluded from transaction identity. For a single-file import it is still only
the basename, and the same display string drives already-imported and
replacement matching. Same-named statements from different directories can
therefore collide.

```sh
sed -n '1640,1670p' honeymoney/cli.py
sed -n '3320,3330p' honeymoney/cli.py
python3 -m unittest tests.test_workflow tests.test_cli_bootstrap
```

Issue #24 owns privacy-safe source provenance together with occurrence
identity; a second public source-ID migration issue would duplicate that seam.

### 010 — TODO

Duplicate detection handles exact and one-day matches only within the current
import batch. Existing cumulative-ledger rows are loaded but never passed to
the detector.

```sh
sed -n '238,265p' honeymoney/cli.py
sed -n '3330,3390p' honeymoney/cli.py
python3 -m unittest tests.test_cli_bootstrap tests.test_workflow
```

Issue #25 keeps the current advisory policy: flag new rows, never delete or
merge transactions.

### 011 — TODO

Near-date groups still use nested pairwise comparisons and parse dates inside
the inner loop. There is no operation-count or scaling regression test.

```sh
sed -n '3330,3380p' honeymoney/cli.py
test ! -e tests/test_duplicate_performance.py
```

The optimization is folded into #25 so output equivalence and cumulative-ledger
correctness land in one vertical slice.

### 012 — TODO

Both normal output and correction documents pass canonical text directly to
`csv.DictWriter`; there is no text-column formula neutralization or reversible
read-back policy. Numeric columns are currently unchanged and must stay so.

```sh
sed -n '326,335p' honeymoney/corrections.py
sed -n '3420,3440p' honeymoney/cli.py
python3 -m unittest tests.test_agent_cli tests.test_cli_bootstrap tests.test_workflow
```

Issue #26 covers every spreadsheet-facing CSV path after #22 stabilizes the
writer boundary.

### 013 — TODO

Runtime PDF dependencies remain unbounded, bootstrap installs directly from
`pyproject.toml`, and CI caches only against that file. There is no committed
constraints/lock input or reviewed refresh workflow.

```sh
sed -n '1,80p' pyproject.toml
sed -n '1,40p' scripts/bootstrap.sh
sed -n '1,80p' .github/workflows/ci.yml
```

Issue #28 adds reproducible development resolution and dependency health while
keeping end-user metadata appropriately compatible.

### 014 — SUPERSEDED

The redundant read remains observable: import loads `existing_ledger_rows`,
then `_merge_into_ledger` reads the file again. The old private-helper signature
optimization should not land independently now that corrections and
reconciliation also own ledger behavior and #22 will define the persistence
snapshot. Its useful intent moves to #29's single ledger module and narrow
read/merge/commit interface after integrity work stabilizes.

```sh
rg -n 'existing_ledger_rows = read_ledger|def _merge_into_ledger|read_ledger\(categorized_path\)' honeymoney/cli.py
```

Do not port the old branch's helper-only change ahead of #22 and #29.

### 015 — SUPERSEDED

The broad correction-derived merchant memory was a direction proposal, not an
approved default. Current architecture instead supports explicit, deterministic
remembered income rules keyed by institution, account identity, exact normalized
description, and inflow direction. It does not learn from Ollama or propagate
arbitrary categories.

```sh
rg -n 'remembered income|--remember|_remembered_income_rule' README.md docs/architecture.md honeymoney/cli.py tests/test_cash_flow_review.py
python3 -m unittest tests.test_cash_flow_review
```

This narrower human-authorized design satisfies the accepted repeated-income
use case without introducing ambiguous generic memory. Embeddings, automatic
learning, and broad correction propagation remain rejected until a new PRD
provides evidence and explicit policy approval.

### 016 — SUPERSEDED

There is no `profile validate` or preview command, and creating a new public
diagnostic surface is no longer the selected slice. Reusable startup validation
belongs to #20; parser fidelity belongs to the existing end-to-end synthetic
PDF-byte golden issue #16. The latter is retained rather than duplicated.

```sh
honeymoney help
rg -n 'profile validate|profile preview' honeymoney README.md docs tests
python3 -m unittest tests.test_import_profiles
```

Preview remains rejected because it would add another public output contract
without resolving the more important startup-validation and real-parser golden
gaps.

### 017 — PARTIAL

Rules, Ollama, reporting, corrections, and cumulative reconciliation have
cohesive modules. Import/profile selection, normalization, transaction identity,
ledger merging, and most persistence remain in the 3,456-line CLI, whose tests
still import private helpers.

```sh
wc -l honeymoney/cli.py
rg -n '^def (_load_profiles|_import_transactions|_normalize_transaction|_assign_transaction_ids|_merge_into_ledger)' honeymoney/cli.py
python3 -m unittest tests.test_agent_cli tests.test_cli_bootstrap tests.test_import_profiles tests.test_transaction_categorization tests.test_workflow
```

Issue #29 runs last, after product behavior and persistence are stable, so it
extracts current contracts rather than moving unresolved bugs.

## Additional findings from reconciliation

- [#27](https://github.com/itsjling/honeymoney/issues/27) makes the default
  verification path work in restricted agent environments. On the reconciled
  commit, the combined focused run executed 172 tests and produced 20 errors,
  all `PermissionError: [Errno 1] Operation not permitted` while loopback test
  servers attempted to bind. The exact command was:

  ```sh
  python3 -m unittest tests.test_agent_cli tests.test_cli_bootstrap tests.test_import_profiles tests.test_transaction_categorization tests.test_workflow tests.test_ollama tests.test_cash_flow tests.test_cash_flow_review
  ```

- [#30](https://github.com/itsjling/honeymoney/issues/30) adds static typing and
  branch-coverage regression gates only after the core modules stabilize. The
  current `scripts/check.sh` contains formatting, lint, unittest discovery, and
  a package build, but no type or coverage command.

- [#16](https://github.com/itsjling/honeymoney/issues/16) remains the single
  owner for deterministic PDF-byte fixtures. No duplicate issue was created.

## Dependency order

Privacy and cumulative-ledger correctness precede convenience and refactoring.
Execute ready issues in this order unless a newer ADR or issue update changes a
dependency:

1. Privacy and safe inputs: #18, #19, #20.
2. Correction and persistence integrity: #21 after #20; #22 after #19 and #21;
   #23 after #22.
3. Persisted identity and derived behavior: #24 after #19, #22, and #23; #25
   after #24; #26 after #22.
4. Independent verification/tooling: #16, #27, and #28 may proceed without
   financial persistence changes.
5. Boundaries: #29 only after #16 and #18–#28 are complete.
6. Ratcheting gates: #30 after #27 and #29.

Every issue is independently decision-complete, names synthetic-only
verification, and excludes private statements, generated ledgers, live Ollama
transcripts, and cloud inference.

## STOP conditions for historical plans

Stop and report drift instead of improvising when any of these is true:

- a plan's named symbol, output contract, or dependency no longer matches live
  code;
- implementation would change JSON envelopes, exit codes, CSV columns,
  configuration, profiles, corrections, or identity without its issue's
  migration decision;
- identity or source changes lack an accepted ADR and deterministic legacy
  behavior;
- persistence work claims cross-file atomicity without tested crash recovery;
- a fixture or failure diagnostic would expose real transaction data;
- a refactor begins before the behavior-owning issues are complete;
- a focused failure repeats for an unrelated reason.

## Findings considered and rejected

- **Merge the old improvement branches**: rejected. Their tips are not
  ancestors of current main and predate review, reconciliation, and current PDF
  behavior.
- **Generic learned categorization memory**: superseded by explicit remembered
  income rules. Automatic broad propagation and embeddings remain rejected.
- **Profile preview as a new public command**: superseded by startup validation
  (#20) and real-parser synthetic goldens (#16).
- **HTML report script injection**: rejected. `honeymoney/report.py` JSON-encodes
  data, escapes `</`, and inserts transaction values with DOM `textContent`.
- **Replace filesystem storage with a database**: rejected. The local
  filesystem boundary is an explicit architecture decision; #22 hardens it.
- **Cloud categorization or sync**: rejected because it violates the core
  privacy boundary.

## How to reconcile this index again

1. Record `git rev-parse HEAD` and treat that commit's observable contracts as
   authoritative.
2. Read `docs/architecture.md`, the live domain guidance, and relevant ADRs.
3. Evaluate every plan's done criteria using its focused synthetic suites plus
   public command/artifact behavior. Do not infer status from branch ancestry.
4. Reclassify changed plans with the vocabulary above and attach concise,
   reproducible evidence to every non-`TODO` result.
5. Link each remaining gap to one decision-complete issue; search open and
   closed issues first and preserve the existing owner when scopes overlap.
6. Recompute dependencies with privacy first, ledger integrity second, then
   identity/derived behavior, tooling, and refactoring.
7. Record superseded, deferred, and rejected proposals with rationale so they
   are not rediscovered as unqualified improvements.
8. Run focused suites and `./scripts/check.sh`. Never run the live Ollama smoke
   test unless explicitly requested, and never use private fixtures as cloud
   evidence.

The original audit covered all Python modules, key synthetic tests and fixtures,
packaging/CI configuration, README/spec/architecture documentation, and recent
git churn. This reconciliation did not inspect private statements, private
acceptance snapshots, generated local workspaces, or live Ollama behavior.
