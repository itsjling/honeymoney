# Duplicate decisions

Exact same-account statement transactions from distinct import records may
support one overlap group. The ADR 0001 record fingerprint supplies equality;
display names, paths, revisions, row order, and nearby dates do not.

For each source count, let the largest count be `M` and the second-largest be
`S`. The normal keep-all view has `M` stable slots. An explicit `same-event`
decision retains `S`; `keep-all` retains `M`. Equal repeated rows remain a pool.
Honeymoney does not pair statement transactions with slots.

The workspace index binds a decision to the exact reviewed support membership.
A membership change ignores the old decision, restores duplicate review, and
asks for a new choice. Active and retired group, slot, membership, and decision
history remain for exact recurrence.

All view derivation evaluates duplicate state across the whole workspace before
period partitioning. Saved corrections use stable view-transaction IDs and do
not choose a pairing.

Normal diagnostics contain stable group and view-transaction IDs plus bounded
membership and count data. They omit amounts, descriptions, paths, source IDs,
revisions, fingerprints, and allocation locators. An explicit local duplicate
inspection may show only the bounded fields its public contract permits.
