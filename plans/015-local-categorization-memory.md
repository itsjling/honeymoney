# Plan 015: Design and prototype local categorization memory

> **Historical plan:** Do not execute this document directly. Use
> [the current reconciliation](README.md), its linked issue, and current main.

> **Executor instructions**: This is a direction plan. Resolve the policy questions and prove the behavior with synthetic data before enabling it by default. Update the index when complete.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney/rules.py honeymoney/schema.py tests/test_transaction_categorization.py tests/test_workflow.py README.md spec-v1.md docs/architecture.md`
> Stop if categorization precedence or correction storage changed without updated requirements.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plans 004 and 008
- **Category**: direction
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Corrections currently apply only to an exact transaction ID, so recurring merchants require repeated review unless a person manually authors a JSON rule. The original product intent includes local similarity against known labeled transactions. A small, deterministic correction-derived memory can reduce repeated review while remaining private, explainable, and reversible.

## Current state

- `README.md:96-104` saves interactive choices as exact-ID corrections.
- `README.md:177-180` requires manual reusable rules.
- `spec-v1.md:143-151` defines hybrid ordering: exact rules, keywords, optional local similarity, local LLM, then manual review.
- `_run_pipeline` at `honeymoney/cli.py:219-233` currently runs rules, duplicates, Ollama, then exact corrections.

Manual corrections must remain highest authority. Deterministic explicit rules must outrank learned memory. Ollama remains local-only and should receive only rows still unresolved after memory.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Categorization goldens | `python3 -m unittest tests.test_transaction_categorization` | all pass |
| Workflow tests | `python3 -m unittest tests.test_workflow` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `docs/adr/0002-local-categorization-memory.md` (create), `honeymoney/cli.py`, `honeymoney/rules.py` only if shared matching primitives belong there, `honeymoney/schema.py` only for public flags/config, `tests/test_transaction_categorization.py`, `tests/test_workflow.py`, synthetic fixtures under `tests/fixtures/categorization/memory/` (create), `README.md`, and `docs/architecture.md`.

**Out of scope**: embeddings/vector databases in the first delivery; cloud AI; silently converting corrections into rules; learning from Ollama outputs; owner/category propagation without confidence/review controls.

## Git workflow

- Branch: `advisor/015-local-categorization-memory`
- Separate design/prototype/implementation commits.
- Commit example: `feat: add local correction-derived categorization memory`.

## Steps

1. Write a decision document under `docs/adr/` defining: eligible reviewed corrections; merchant normalization; account/institution/currency scope; conflict resolution; minimum observations; confidence; precedence; provenance flags/reasons; deletion/opt-out; rebuildability; and privacy. Recommend a deterministic normalized merchant signature before embeddings.
   **Verify**: every listed policy has an explicit answer and examples for recurring, ambiguous, and conflicting merchants.
2. Add synthetic golden cases without enabling production behavior: same merchant/new transaction, punctuation/case variants, generic merchants (`APPLE`, transfer-like descriptions), conflicting labels, owner differences, explicit rule override, exact correction override, and memory removal.
   **Verify**: `python3 -m unittest tests.test_transaction_categorization` → new tests initially fail or remain marked as prototype expectations.
3. Implement the smallest local memory store that can be rebuilt from reviewed corrections or an explicit local file. Add a config flag defaulting off for existing workspaces. Apply memory after explicit rules and before Ollama; keep ambiguous/conflicting matches in review.
   **Verify**: categorization and workflow suites pass; Ollama request tests prove memory-resolved rows are not sent.
4. Add explainability: stable provenance flag, reason naming the local memory match, and a command/documented procedure to inspect and remove learned entries.
   **Verify**: golden output includes provenance and deletion prevents future application.
5. Evaluate the prototype with synthetic metrics: repeated-review reduction, false-positive cases, deterministic rebuild. Do not enable by default until the maintainer accepts the documented tradeoff.
   **Verify**: decision note records enable/defer verdict and evidence.

## Test plan

Follow fixture layout in `docs/golden-datasets.md`. Add deterministic memory cases beside rule goldens, plus workflow precedence tests. No live model output or private merchant data.

## Done criteria

- [ ] All policy questions are documented.
- [ ] Memory is local, inspectable, removable, deterministic, and default-off initially.
- [ ] Exact corrections and explicit rules retain precedence.
- [ ] Ambiguous/conflicting matches remain reviewable.
- [ ] No cloud/network dependency is added.
- [ ] Full check passes.

## STOP conditions

- Stable transaction IDs from Plan 008 are unavailable.
- The only viable design requires embeddings or a new heavy dependency before deterministic matching is evaluated.
- Merchant normalization would propagate ambiguous labels without a reliable review safeguard.
- The maintainer has not approved enabling the feature by default.

## Maintenance notes

Never train from Ollama suggestions or low-confidence rows automatically. Review normalization changes like identity migrations: they can change which future transactions inherit a label.
