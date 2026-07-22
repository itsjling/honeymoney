# ADR 0002: Local correction-derived categorization memory

- Status: Accepted for an opt-in prototype
- Date: 2026-07-13
- Default-enable verdict: Deferred

## Context

Exact-ID corrections apply to one transaction. Repeated review of the same
merchant is costly, but a reusable rule may be too broad. This feature offers a
small, local, deterministic middle ground without embeddings or a database.

## Decision

Memory rebuilds in memory for every import from `corrections.csv` and the
validated authoritative ledger loaded with its identity manifest. It stores no
sidecar and sends nothing over the network.

Evidence needs two distinct current identity-v2 rows. Each must have all four
ADR 0001 identity fields and a v2 transaction ID. Legacy rows and rows flagged
`identity_migration_ambiguous` are excluded. An observation also needs an
explicit non-`Unknown` correction that explicitly sets `needs_review=false`
and, where given, meets the review confidence threshold. Rule and Ollama output
never become evidence. Both evidence and applied categories must resolve to a
`spending` category policy. Accounting and manual-only categories never enter
or leave local memory.

The key is `(account_id, institution, posted_currency, normalized_merchant)`.
Merchant normalization applies Unicode NFKC, case folding, punctuation-to-space
replacement, and whitespace collapse. Exact matching only is used. The generic
signatures `apple`, `payment`, `card payment`, `credit card payment`,
`transfer`, `fps`, `ach`, and `wire`, plus signatures containing transfer-like
tokens, are excluded.

Evidence must agree on one category. Any conflict, fewer than two observations,
or a removed correction disables the key. A match assigns category confidence
`0.90`, leaves owner unchanged, adds `local_memory_categorized`, and records the
number of supporting reviews. It clears review only when `0.90` meets the
configured threshold.

The order is explicit rules, local memory, duplicate and structural checks,
optional local Ollama, then exact corrections. Rules therefore win over memory,
and exact corrections remain the final authority.

## Configuration and privacy

The feature defaults off. Missing configuration also means off.

```json
{"categorization_memory": {"enabled": false}}
```

Set `enabled` to `true` to opt in. Removing a correction removes its evidence on
the next import. No new network path or dependency is added.
