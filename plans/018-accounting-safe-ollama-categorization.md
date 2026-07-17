# PRD: Make Ollama categorization accounting-safe and semantically constrained

Status: DONE — isolated implementation approved; maintainer merge pending  
Priority: P1  
Expected size: M

## Execution Result

- **Implementation commit:** `8d9a857` on
  `codex/improve-plan-018-accounting-safe-ollama`
- **Isolated worktree:** `/tmp/honeymoney-plan-018`
- **Offline verification:** focused policy/config/CLI/Ollama/cash-flow/workflow
  suites pass; `./scripts/check.sh`, `git diff --check`, and commit checks pass.
- **Review verdict:** APPROVE. Implementation and tests satisfy the offline
  contract and remain within scope using synthetic data only.
- **Live benchmark:** `qwen2.5:3b` passed on 2026-07-17 with 100% accounting
  safety and 100% ordinary-category accuracy (required: 100% and at least 90%).
  All five ordinary purchases produced `expense` without review; the synthetic
  unidentified bank credit remained `unresolved` and reviewable.

## Execution Contract

- **Planned at:** `a91db80fe5b3b20cccf3abf4b51f58ec199d3fde`
- **Dependencies:** None. Plan 007's endpoint hardening is complementary but
  does not block this categorization-policy change.
- **Branch:** create `codex/improve-plan-018-accounting-safe-ollama` in the
  isolated worktree and commit the completed implementation there.
- **Privacy:** use only committed synthetic fixtures. Do not open or copy files
  from `samples/`, `private_samples/`, `money/`, or another local workspace.

### Drift check

Before changing code, run:

```sh
git diff --exit-code a91db80fe5b3b20cccf3abf4b51f58ec199d3fde -- \
  honeymoney/cli.py \
  honeymoney/ollama.py \
  honeymoney/reconciliation.py \
  honeymoney/schema.py \
  scripts/live_ollama_categorization_smoke.py \
  tests/test_agent_cli.py \
  tests/test_cash_flow.py \
  tests/test_cli_bootstrap.py \
  tests/test_config_cli.py \
  tests/test_transaction_categorization.py \
  README.md docs/architecture.md docs/golden-datasets.md examples/config.json \
  examples/expected-output/import_report.json
```

Expected result: exit 0 and no output. If it fails, STOP and report the changed
paths; do not adapt this plan to a newer architecture.

### Current state and conventions

The import orchestrator in `honeymoney/cli.py` currently establishes precedence
in this order:

```python
rules = load_rules(config)
apply_rules(transactions, rules, config)
_annotate_duplicate_suspicions(transactions)
ollama_report, ollama_warnings = apply_ollama_fallback(transactions, config, ...)
corrections = load_corrections(config)
apply_corrections(transactions, corrections)
```

Preserve that order while inserting built-in structural classification after
duplicate annotation and before Ollama. Rules must continue to win because the
structural classifier acts only on unresolved rows; corrections must continue
to win because they remain after model application.

`honeymoney/ollama.py` currently sends every configured category and owner in
the response schema, then mutates both fields after validating only vocabulary
membership. Replace that behavior through the central policy; do not add
parallel policy lists inside `ollama.py`.

`honeymoney/reconciliation.py::_derive_flow_type` currently protects only
model-originated `Income`; it still derives transfer, savings, investment, and
credit-card-payment flows from a model-originated category. Generalize the
trusted-provenance check through the central policy while preserving explicit
rule/correction flows and legacy non-model ledger behavior.

Match existing conventions: standard-library Python 3.10, typed dictionaries
and functions, `Decimal` for financial values and thresholds, semicolon-delimited
flags/reasons, deterministic sorted output, `unittest`, local fake HTTP servers,
small manually reviewed JSON fixtures, and additive public JSON fields.

### Files in scope

Production and public documentation:

- `honeymoney/classification_policy.py` (new central policy component)
- `honeymoney/cli.py`
- `honeymoney/ollama.py`
- `honeymoney/reconciliation.py`
- `honeymoney/schema.py` only if a shared category constant or helper is needed
- `README.md`
- `docs/architecture.md`
- `docs/golden-datasets.md`
- `examples/config.json`
- `examples/expected-output/import_report.json` (synthetic additive report field)
- `scripts/live_ollama_categorization_smoke.py`

Tests and synthetic data:

- `tests/test_classification_policy.py` (new focused unit tests)
- `tests/test_transaction_categorization.py`
- `tests/test_cash_flow.py`
- `tests/test_cli_bootstrap.py`
- `tests/test_config_cli.py`
- `tests/test_agent_cli.py` only if needed to assert the additive JSON contract
- new minimal files under `tests/fixtures/categorization/ollama/accounting_safety/`
- one new synthetic benchmark corpus at
  `tests/fixtures/categorization/ollama/live_benchmark.json`
- existing Ollama fixture files under `tests/fixtures/categorization/ollama/`
  only when their expected response contract must lose `owner` or gain additive
  report fields

Everything else is out of scope. In particular, do not change transaction
identity, duplicate matching, parsers/profiles, corrections persistence,
endpoint/network enforcement, report HTML, output CSV columns, issue metadata,
or private/local data.

### Ordered implementation

1. **Create the central classification policy.** In
   `honeymoney/classification_policy.py`, define the built-in protected
   accounting categories, built-in merchant-category definitions, custom
   `category_policies` resolution, config validation, model-eligible category
   descriptions, trusted accounting provenance, conservative structural
   classification, and model-suggestion evaluation. Use one public policy
   interface from prompt construction, response application, review decisions,
   and flow derivation. Built-in protected categories cannot be relaxed through
   config; configured custom categories without a policy resolve to
   `manual_only`.

   Structural matching must normalize whitespace/case and require the amount
   sign specified in the PRD. Use narrowly reviewed word/phrase markers: do not
   match generic `cash`, `payment`, `credit`, `transfer`, or merchant substrings
   on their own. A structural match sets the category/flow described in the
   Implementation Decisions, `needs_review=false`, confidence `1.00`, the public
   `structural_classification` flag, and a concise deterministic reason. Rows
   already resolved by a rule or marked as duplicate-suspected are not
   structurally auto-approved.

   Verification:

   ```sh
   python3 -m unittest tests.test_classification_policy
   ```

   Expected: all focused policy tests pass, covering every category kind,
   config override boundary, each structural predicate with positive and
   negative examples, unknown owner, amount sign/absence, duplicate review,
   and trusted provenance.

2. **Validate the public configuration and wire structural decisions.** Call
   the policy validator from `honeymoney/cli.py::_validate_config_document`.
   Reject non-object `category_policies`, unknown category keys, non-object
   entries, unsupported `kind`, non-string/empty descriptions, and attempts to
   make a protected built-in category `spending`. Add the structural-classifier
   call at the import seam identified above. Put `structural_count` in a new
   additive `categorization` object in `import_report.json`; do not describe it
   as Ollama work and do not remove or rename existing fields.

   Verification:

   ```sh
   python3 -m unittest tests.test_config_cli tests.test_cli_bootstrap
   ```

   Expected: malformed policy config exits through the established validation
   path, valid policy config loads, precedence tests pass, and import reports
   contain deterministic structural metrics.

3. **Constrain the Ollama request and response contract.** Change
   `honeymoney/ollama.py` so the response schema contains only `id`, `category`,
   `confidence`, and `reason`; owner is neither requested nor applied. Build
   its category enum and prompt definitions from the central policy. Include
   the PRD's boundary guidance and keep the existing minimized per-transaction
   payload exactly as narrow as it is now.

   Treat schema/shape failures as `invalid_count`. Treat well-formed but unsafe
   suggestions as policy outcomes: protected/manual-only suggestions are
   rejected, leave category `Unknown`, add `ollama_policy_rejected`, keep review
   enabled, and append a stable reason naming only the category. Safe spending
   suggestions may be applied as reviewable when confidence is low or another
   policy condition requires review. Only a known-owner, non-duplicate,
   non-zero negative outflow at/above threshold may auto-clear review.

   Every enabled non-empty run returns additive `candidate_count`,
   `accepted_count`, `reviewable_count`, and `rejected_count`. Keep
   `applied_count == accepted_count + reviewable_count`; keep `invalid_count`
   for malformed responses. A batch containing only policy rejections is still
   a successfully processed model response, not `invalid_response`.

   Verification:

   ```sh
   python3 -m unittest tests.test_transaction_categorization tests.test_ollama
   ```

   Expected: protected categories are absent from captured schemas, fake
   protected responses are rejected at confidence 1.0, owner remains unchanged,
   definitions/boundaries are present, payload privacy assertions still pass,
   and old unavailable/invalid behaviors remain non-fatal.

4. **Enforce accounting provenance during reconciliation.** Replace the
   one-off `Income` flag check in `honeymoney/reconciliation.py` with the central
   trusted-provenance policy for all protected categories. Explicit rules,
   corrections, structural classifications, and reconciliation may establish
   protected flows. A category carrying only Ollama provenance cannot. Preserve
   non-model legacy rows as the existing compatibility comment promises.

   Verification:

   ```sh
   python3 -m unittest tests.test_cash_flow
   ```

   Expected: every protected model-originated category stays unresolved, while
   rule/correction/structural/reconciled cases retain their expected flows and
   ordinary negative model categories derive `expense`.

5. **Add a synthetic user-boundary regression.** Extend the existing fake
   loopback Ollama harness in `tests/test_cli_bootstrap.py`; do not invent a
   second HTTP test framework. Import a minimal synthetic CSV containing an
   ordinary card purchase, explicit cashback/rebate, interest, ATM withdrawal,
   explicit card settlement, positive unidentified bank credit, unknown owner,
   and a dangerous high-confidence model answer. Assert categorized CSV,
   review CSV, import-report metrics, flow types, public flags/reasons, owner
   stability, and idempotent output for a repeated replacement run. Include a
   paired transfer case only if it can reuse the existing reconciliation fixture
   without widening parser/identity scope.

   Verification:

   ```sh
   python3 -m unittest tests.test_cli_bootstrap tests.test_agent_cli tests.test_workflow
   ```

   Expected: the end-to-end public artifacts satisfy the Acceptance Criteria
   without changing CSV columns, JSON envelope version, or exit-code behavior.

6. **Update the offline benchmark and docs.** Replace the two-row smoke corpus
   with the tracked synthetic benchmark file. The script must report total
   cases, accounting-safety compliance, ordinary-category accuracy, per-case
   category/review outcomes, and exit nonzero below 100% safety or 90% ordinary
   accuracy. It must still use only the configured local endpoint and remain
   outside test discovery. Document the policy/config shape, precedence,
   metrics, benchmark command, and the distinction between merchant category
   suggestion and accounting-flow authority in the listed README/docs/example
   files.

   Verification without live inference:

   ```sh
   python3 scripts/live_ollama_categorization_smoke.py --help
   python3 -m unittest tests.test_transaction_categorization tests.test_config_cli
   ```

   Expected: help exits 0, documentation matches the public contract, and the
   benchmark corpus contains only synthetic reviewed cases. Do not run the live
   benchmark unless the configured local model is present; if it is absent,
   record that manual gate plainly instead of substituting another model or a
   remote endpoint.

7. **Run repository verification and commit.** Bootstrap the isolated
   worktree, run the focused suites, then the full repository check:

   ```sh
   ./scripts/bootstrap.sh
   python3 -m unittest tests.test_transaction_categorization tests.test_ollama
   python3 -m unittest tests.test_cash_flow tests.test_workflow tests.test_agent_cli
   ./scripts/check.sh
   git diff --check
   ```

   Expected: every command exits 0. Inspect `git status --short` and confirm
   every changed file is within this plan's scope and contains only synthetic
   data. Commit with a Conventional Commit message such as
   `feat(categorization): constrain Ollama accounting decisions`.

### STOP conditions

- The drift check fails for an in-scope production or test file.
- Meeting an acceptance criterion requires changing a file outside the scope
  above or changing a public CSV column, JSON schema version, or exit code.
- Existing tests establish that Ollama is intentionally allowed to change
  owner or establish a protected accounting flow.
- Structural recognition cannot be made conservative without institution-
  specific private statement evidence.
- A fixture, log, prompt, or failure contains real statement or transaction
  data.
- The implementation would require network/cloud inference, a third-party
  dependency, or relaxing the local-only product boundary.

### Review and maintenance notes

Reviewers must inspect the captured prompt schema, not merely final rows;
otherwise protected categories or owner can remain model-visible unnoticed.
They must also inspect negative structural examples because broad merchant-text
regexes are the primary false-positive risk. Future category additions must
choose an explicit policy kind and description before becoming model eligible.
Future reconciliation changes must continue to require trusted provenance for
protected accounting flows. The live benchmark measures prompt/model quality;
mocked tests remain the enforcement boundary and must be sufficient even for a
malicious, schema-valid local response.

## Problem Statement

HoneyMoney currently validates that an Ollama response is well-formed, uses an
allowed category, names an allowed owner, and supplies an in-range confidence
score. Those checks prevent malformed output, but they do not prevent a
schema-valid answer from being financially wrong.

A local household validation run exposed systematic failures. Ordinary merchant
charges were classified as `Credit Card Payment` merely because the payment
method was a credit card. Cashback was variously treated as cash, income,
unknown activity, or another spending category. Food, communications, and other
recognizable merchants were assigned unrelated categories, while the model
reported enough confidence to clear review. Owner values were also inferred
without evidence.

The most serious failure is architectural: category choices such as `Credit
Card Payment`, `Internal Transfer`, `Savings`, and `Investments` deterministically
produce non-spending accounting flows. A local model can therefore exclude real
purchases from spending totals even though the architecture intends accounting
flow to be established by deterministic evidence, reconciliation, or human
confirmation. Model confidence is not a sufficient control for consequential
accounting treatment.

## Solution

Treat Ollama as an untrusted merchant-category suggester, not an accounting
decision-maker.

HoneyMoney will define a central category policy that distinguishes ordinary
spending categories from protected accounting categories. Ollama will receive
only model-eligible merchant categories. It will not be asked to infer owner,
income, transfers, investments, savings movements, refunds, or credit-card
payments. Those decisions will come only from explicit rules, deterministic
structural evidence, transfer reconciliation, or human corrections.

Before Ollama runs, a small deterministic structural classifier will recognize
unambiguous positive cashback/rebate credits, positive interest credits, ATM
withdrawals, and explicit credit-card payment markers. It will be deliberately
conservative: ambiguity stays unresolved rather than being guessed.

After Ollama responds, a policy validator will decide whether each suggestion
is safe to accept, must remain reviewable, or must be rejected. High model
confidence will never bypass this validator. An accepted ordinary category may
establish `expense` only for a valid negative outflow. Positive bank inflows and
all protected accounting flows remain unresolved unless a deterministic or
human source establishes them.

The prompt will include concise category definitions and boundary examples,
including that a credit card is a payment method rather than a purchase
category, cashback is not cash spending, food delivery is dining rather than
transport, and internet service is a utility rather than transport. A tracked
synthetic benchmark will measure the configured local model without putting
live inference in CI.

## User Stories

1. As a household budgeter, I want ordinary card purchases to remain spending,
   so that my expenditure is not understated.
2. As a household budgeter, I want credit-card payments excluded only when
   deterministic evidence identifies a settlement, so that purchases are not
   confused with payments.
3. As a household budgeter, I want internal transfers established only by
   reconciliation, explicit rules, or review, so that income and spending are
   not distorted.
4. As a household budgeter, I want savings and investment movements protected
   from model guesses, so that cash-flow totals remain auditable.
5. As a household budgeter, I want model-suggested income to remain unresolved,
   so that a positive credit is not silently counted as earnings.
6. As a household budgeter, I want positive cashback and cash rebates treated
   as refunds when the statement description is explicit, so that rebates do
   not inflate income.
7. As a household budgeter, I want positive interest credits recognized
   deterministically, so that explicit bank interest does not need model
   interpretation.
8. As a household budgeter, I want ATM withdrawals treated as cash spending
   when the description is explicit, so that cashback is not confused with
   physical cash.
9. As a household budgeter, I want ambiguous inflows and transfers to remain
   reviewable, so that uncertainty is visible instead of hidden.
10. As a household budgeter, I want category errors to affect at most the
    spending breakdown, not whether a transaction counts as spending at all.
11. As a household budgeter, I want `Other` and `Unknown` model suggestions to
    remain reviewable, so that vagueness is not mistaken for certainty.
12. As a household budgeter, I want an unknown owner to force review, so that
    ownership is not silently invented.
13. As a household budgeter, I want Ollama to leave the profile- or rule-derived
    owner unchanged, so that merchant text cannot reassign a transaction to a
    person.
14. As a household budgeter, I want food delivery, restaurants, utilities,
    transport, subscriptions, and other common categories to have clear model
    definitions, so that category boundaries are consistent.
15. As a household budgeter, I want a credit card to be understood as a payment
    method rather than a category, so that merchant purchases retain their
    economic purpose.
16. As a household budgeter, I want high-confidence but policy-invalid model
    responses rejected, so that confidence cannot override accounting safety.
17. As a household budgeter, I want rejected model suggestions flagged with a
    concise explanation, so that I can understand why review is required.
18. As a household budgeter, I want accepted and rejected suggestion counts in
    the import report, so that model quality is observable.
19. As a household budgeter, I want deterministic classifications to state
    their provenance, so that I can distinguish rules from model suggestions.
20. As a household budgeter, I want corrections to retain absolute precedence,
    so that reviewed decisions always win.
21. As a household budgeter, I want explicit user rules to beat built-in
    structural fallback and Ollama, so that local policy remains controllable.
22. As a household budgeter, I want repeated runs with the same inputs and
    mocked model responses to produce the same ledger, so that results are
    reproducible.
23. As a household budgeter, I want custom categories to default to review
    unless they are explicitly declared safe for model auto-application, so
    that configuration extensions fail safely.
24. As a CLI user, I want the existing categorized CSV and JSON envelopes to
    remain compatible, so that scripts do not break.
25. As a CLI user, I want new model-policy metrics to be additive, so that old
    consumers can ignore them.
26. As a CLI user, I want Ollama-unavailable and invalid-response behavior to
    remain non-fatal, so that imports still complete locally.
27. As a maintainer, I want category definitions and accounting sensitivity
    centralized, so that prompts, response validation, review policy, and flow
    derivation cannot drift apart.
28. As a maintainer, I want synthetic regression cases for dangerous model
    answers, so that a valid-looking response cannot regress accounting safety.
29. As a maintainer, I want a local synthetic benchmark for the supported
    model, so that prompt changes can be evaluated without private statements.
30. As a maintainer, I want live-model benchmarking kept outside CI, so that
    tests remain deterministic and offline.
31. As an implementation agent, I want one policy interface used by prompt
    construction, model-response application, review derivation, and flow
    derivation, so that the safety rules have a single source of truth.
32. As an implementation agent, I want the highest-level regression test to
    exercise a CLI import and inspect all generated artifacts, so that the
    feature is verified at the user-visible boundary.

## Implementation Decisions

- Introduce one cohesive classification-policy component. It owns category
  families, model eligibility, category definitions, protected accounting
  semantics, review requirements, and post-model validation.
- Protected accounting categories are `Income`, `Credit Card Payment`,
  `Internal Transfer`, `Savings`, and `Investments`. Ollama must not receive
  them in its response enum and must not be able to apply them if an old,
  malicious, or non-conforming endpoint returns them anyway.
- Ollama classifies merchant purpose only. Remove owner from the model response
  contract. Profiles, explicit rules, and human corrections remain the sources
  of owner assignment.
- Keep the minimized transaction payload and local-only endpoint boundary. Do
  not add statement context, source filenames, balances, account suffixes, or
  unrelated transactions to improve accuracy.
- Provide concise definitions for every model-eligible built-in category. The
  prompt must include negative boundary guidance for recurring failure modes,
  not only category names.
- Run deterministic user rules before the built-in structural classifier.
  Apply built-in structural behavior only to rows still unresolved, preserving
  user control.
- Recognize cashback/refund structurally only when the normalized description
  explicitly contains a reviewed cashback or cash-rebate marker and the amount
  is positive. Assign `flow_type=refund`; use the existing `Other` category
  unless an explicit user rule supplies a more specific merchant category.
- Recognize interest structurally only when the description explicitly denotes
  interest and the amount is positive. Assign `Income` with confirmed
  `flow_type=income`. Negative interest or fee-like rows remain outside this
  rule.
- Recognize ATM cash withdrawals only from explicit withdrawal markers and a
  negative amount. Assign `Cash` with `flow_type=expense`.
- Recognize a credit-card payment without a matched opposite-side transaction
  only when the card-account row is a positive credit and contains an explicit,
  reviewed payment marker. Bank-side settlement debits continue to rely on
  reconciliation, rules, or correction.
- Transfer reconciliation remains the preferred source for paired credit-card
  payments, internal transfers, and investment transfers. Ollama does not
  participate in pairing.
- A model suggestion is auto-applicable only when it is a model-eligible
  ordinary category, the amount is a valid negative outflow, the unchanged
  owner is known, confidence meets the configured threshold, and no existing
  condition already requires review.
- `Other`, `Unknown`, protected-category responses, unknown owners, missing or
  zero base amounts, positive bank inflows, duplicate suspicions, and policy
  contradictions always remain reviewable regardless of model confidence.
- A rejected protected-category response leaves the category `Unknown`, the
  flow unresolved, and review enabled. Add a stable policy-rejection flag and a
  reason that identifies the rejected category without exposing model prompt
  content.
- Flow derivation must enforce provenance for every protected accounting
  category, generalizing the current protection applied to model-originated
  income. A category produced only by Ollama can never establish a protected
  flow.
- Accepted model categories on negative outflows may deterministically produce
  `expense`. Category errors can therefore affect a draft category breakdown,
  but cannot hide the outflow from total spending through a protected flow.
- Preserve existing output columns and existing structured command fields.
  Extend the Ollama report additively with candidate, accepted, reviewable,
  rejected, and structurally-resolved counts. Retain the existing applied count
  with documented compatibility semantics.
- Add stable flags for structural classification and model-policy rejection.
  Reasons must be concise, deterministic for structural rules, and suitable for
  CSV and JSON output.
- Use `structural_classification` for accepted built-in structural decisions
  and `ollama_policy_rejected` for model responses blocked by policy. These flag
  names are public output contracts.
- Add an optional public `category_policies` configuration object keyed by
  category name. A custom category policy supports `description` and a `kind`
  enum of `spending`, `accounting`, or `manual_only`. Only `spending` is model
  eligible. Custom categories without a policy default to `manual_only` but
  remain valid for explicit rules and corrections. Built-in protected
  accounting categories cannot be relaxed to `spending` through configuration;
  descriptions for model-eligible built-ins may be overridden locally.
- Extend the existing Ollama JSON report with `candidate_count`,
  `accepted_count`, `reviewable_count`, and `rejected_count`. Keep
  `applied_count` equal to accepted plus reviewable suggestions, and retain
  `invalid_count` for malformed responses. Add `structural_count` to the import
  categorization summary rather than presenting structural decisions as model
  work.
- Manual corrections remain highest precedence, followed by explicit user
  rules, built-in structural classification, safe Ollama suggestions, and
  unresolved fallback. Reconciliation continues after categorization.
- Update the architecture and user documentation to distinguish merchant
  category suggestion from accounting-flow authority.

## Acceptance Criteria

- [ ] A fake Ollama response of `Credit Card Payment` for a restaurant purchase
  cannot produce `flow_type=credit_card_payment`, even at confidence 1.0.
- [ ] Fake Ollama responses using any protected accounting category are rejected
  safely, flagged, and left reviewable with unresolved flow.
- [ ] Protected categories are absent from the schema enum sent to Ollama.
- [ ] Ollama can no longer change owner, and an existing unknown owner keeps the
  row in review.
- [ ] A normal negative merchant purchase assigned a safe spending category is
  retained as an expense regardless of whether it was paid by credit card.
- [ ] A positive bank inflow cannot become income, refund, transfer, savings, or
  investment movement from Ollama alone.
- [ ] Explicit positive cashback and cash-rebate fixtures become `Other` with
  refund flow before Ollama, and are absent from the model request.
- [ ] Explicit positive interest fixtures become confirmed income before
  Ollama, and are absent from the model request.
- [ ] Explicit ATM withdrawal fixtures become cash expenses before Ollama.
- [ ] An explicit positive card payment fixture and a reconciled bank-to-card
  pair become credit-card payments without Ollama.
- [ ] `Other`, `Unknown`, policy rejection, unknown owner, duplicate suspicion,
  and missing base amount all force review.
- [ ] The prompt carries category definitions and the documented boundary
  guidance while retaining the existing minimized payload.
- [ ] The import report distinguishes accepted, reviewable, rejected, and
  structurally resolved decisions without removing existing JSON fields.
- [ ] Existing rules and corrections override the new structural and model
  policies.
- [ ] A full CLI import using synthetic transactions and a fake loopback Ollama
  endpoint produces the expected categorized CSV, review CSV, import report,
  flow types, flags, and reasons.
- [ ] A manually run synthetic benchmark for the configured local model reports
  100% accounting-safety compliance and at least 90% correct ordinary-category
  suggestions on the reviewed benchmark corpus before the prompt change is
  accepted.
- [ ] No private transaction, statement, generated ledger, or live model output
  is added to tests, documentation, issues, or logs.
- [ ] The focused categorization, Ollama, cash-flow, reconciliation, workflow,
  and agent-CLI suites pass, followed by the full repository check.

## Testing Decisions

- The primary test seam is an end-to-end CLI import of a small synthetic CSV
  through a fake loopback Ollama endpoint. Assert the generated categorized
  ledger, review export, import report, and accounting flows. This is the
  highest existing seam that covers prompt policy, response application,
  reconciliation, persistence, and public artifacts together.
- Add focused policy tests underneath the CLI seam for category-family lookup,
  custom-category defaults, structural predicates, response acceptance,
  rejection reasons, review derivation, and provenance-aware flow derivation.
- Extend the existing fake-Ollama tests rather than introducing a second model
  test harness. Fake responses should intentionally be schema-valid and
  semantically dangerous.
- Add synthetic cases for a restaurant purchase on a card, food delivery,
  broadband service, ride hailing, pharmacy spending, cashback, cash rebate,
  positive interest, ATM withdrawal, explicit card settlement, paired transfer,
  ambiguous person-to-person transfer, positive unidentified bank credit,
  `Other`, `Unknown`, and unknown owner.
- Test every protected category at high confidence. The expected behavior is
  policy rejection or deterministic provenance, never model-established
  accounting flow.
- Test that explicit rules and corrections retain precedence and that resolved
  structural rows are omitted from Ollama requests.
- Test backward-compatible JSON output: existing fields and meanings remain,
  while new metrics are additive and deterministic.
- Test idempotence with identical inputs and fake responses.
- Maintain a separate synthetic live-model benchmark with expected merchant
  categories and accounting-safety assertions. It is manually invoked, never
  part of CI, and contains no real merchant or statement data.
- Do not assert exact natural-language model reasons in the live benchmark.
  Assert category, review state, and accounting safety. Mocked tests may assert
  deterministic policy reasons.
- Verification commands for implementation:
  - `python3 -m unittest tests.test_transaction_categorization tests.test_ollama`
  - `python3 -m unittest tests.test_cash_flow tests.test_workflow tests.test_agent_cli`
  - `./scripts/check.sh`

## Out of Scope

- Reclassifying or publishing the current private household ledger.
- Copying real merchants, amounts, statement descriptions, model transcripts,
  or generated reports into tracked fixtures.
- Cloud AI, remote inference, training, fine-tuning, embeddings, or vector
  databases.
- Guaranteeing perfect merchant-category accuracy for every local model.
- A comprehensive merchant encyclopedia or bundled user-specific rules.
- Learning reusable rules automatically from unreviewed Ollama output.
- Changing transaction identity, duplicate detection, statement parsing,
  exchange-rate sourcing, or transaction splitting.
- Adding new budget categories solely for cashback, interest, fees, or refunds.
- Inferring owner from merchant text or model output.
- Automatically approving ambiguous transfers or positive bank credits.
- Running live Ollama inference in CI.

## Further Notes

- This PRD tightens the original v1 contract: `Credit Card Payment` and
  `Internal Transfer` remain visible categories, but their accounting meaning
  requires deterministic or human provenance.
- The current protection that prevents Ollama-originated `Income` from becoming
  income flow is the precedent. The implementation should generalize that
  invariant instead of adding isolated special cases.
- Prompt improvements are necessary but not sufficient. Accounting safety must
  remain correct even when the model returns a confident, schema-valid, and
  semantically wrong answer.
- Publication to the GitHub issue tracker and application of the
  `ready-for-agent` label require explicit approval because issue creation is an
  external action.
