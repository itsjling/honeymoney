# Plan 008: Preserve distinct repeated transactions with stable identities

> **Historical plan:** Do not execute this document directly. Use
> [the current reconciliation](README.md), its linked issue, and current main.

> **Executor instructions**: This changes a persisted public key. Complete characterization and migration design before code. Update the index only after all compatibility gates pass.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney/schema.py tests/test_cli_bootstrap.py tests/test_workflow.py docs/architecture.md README.md`
> Stop if transaction-ID format or correction keying changed after `aa0eedf`.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plans 001, 005, and 006
- **Category**: migration
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Identity collision counts are batch-local. Two identical charges imported separately receive the same unsuffixed ID, so the later row overwrites the first; importing them together instead creates two different suffixed IDs. Reordering or changing collision count can also attach a persisted correction to the wrong occurrence.

## Current state

```python
# honeymoney/cli.py:2103-2115
base_counts[base] = base_counts.get(base, 0) + 1
suffix = f":{seen[base]}" if base_counts[base] > 1 else ""
digest = hashlib.sha256(f"{base}{suffix}".encode("utf-8")).hexdigest()[:16]
```

`_transaction_identity_base` excludes source/row metadata to keep IDs stable across filename changes. Corrections and ledger merges key by exact `transaction_id`; preserve reviewed choices deliberately rather than incidentally.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Identity tests | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Workflow tests | `python3 -m unittest tests.test_workflow` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `honeymoney/schema.py` if version metadata is required, `tests/test_cli_bootstrap.py`, `tests/test_workflow.py`, `docs/architecture.md`, `docs/adr/0001-stable-transaction-identity.md` (create), and `README.md`.

**Out of scope**: source display naming (Plan 009); duplicate review policy (Plan 010); database migration; private data.

## Git workflow

- Branch: `advisor/008-stable-transaction-identity`
- Separate commits for characterization, design/migration, implementation, and docs.
- Do not push/open a PR unless instructed.

## Steps

1. Add characterization tests for: identical rows imported separately; together; source rename; directory ordering change; insertion/removal of an earlier collision; replace/reset with corrections; legacy ledger IDs. Assert current failures explicitly.
   **Verify**: focused tests demonstrate silent overwrite/misassociation without relying on private fixtures.
2. Write an ADR or dedicated identity section in `docs/architecture.md` defining canonical identity, occurrence discrimination, filename-rename behavior, statement replacement, legacy-ID migration, and collision probability. STOP for maintainer review if these goals cannot all coexist.
   **Verify**: design maps every characterization case to an expected stable result.
3. Implement ledger-aware occurrence reconciliation or a versioned stable source/occurrence discriminator. Never use current batch order alone. Preserve or migrate correction associations and avoid silent row deletion.
   **Verify**: all characterization tests pass, including legacy ledger input.
4. Add migration diagnostics: detect ambiguous legacy collisions and require review rather than guessing. Document any version transition.
   **Verify**: ambiguous synthetic case emits deterministic warning/review state.

## Test plan

Extend identity tests near `tests/test_cli_bootstrap.py:1657` and workflow replacement/correction tests. Cover separate versus batch imports, reordered files, inserted/removed collisions, rename, legacy IDs, correction association, and ambiguous migration. Verify with `python3 -m unittest tests.test_cli_bootstrap tests.test_workflow`.

## Done criteria

- [ ] Sequential identical imports retain two rows with stable distinct IDs.
- [ ] Reordering/renaming does not swap corrections.
- [ ] Existing non-colliding IDs remain compatible or migrate deterministically.
- [ ] Ambiguous migration never silently guesses.
- [ ] Full check passes.

## STOP conditions

- No identity design can preserve both rename stability and occurrence association without new persisted metadata.
- Existing ledgers require irreversible migration without explicit maintainer approval.
- Any approach uses mutable list order as the sole discriminator.

## Maintenance notes

Identity fields are permanent public contracts. Any parser normalization change must run identity regression tests and consider existing correction keys.
