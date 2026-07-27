# ADR 0003: Canonical ledger identity for exact statement overlaps

- Status: Accepted by the implementation requests for GitHub #32 and #33
- Date: 2026-07-25
- Issue: GitHub #32
- Narrows: ADR 0001 for the public canonical ledger and ADR 0002 for canonical
  local-memory evidence

## Context

ADR 0001 gives every imported statement occurrence stable source and record
identity. Its public-ledger agreement assumes one active source record per
ledger row. Exact events may also appear in two overlapping statements. Keeping
both source rows in financial totals counts one event twice.

Repeated equal events add a second constraint. If source A and source B each
contain three equal rows, the supported financial multiplicity is three, not
one or six. No financial fact proves which A occurrence pairs with which B
occurrence. ADR 0001 forbids page, row, parser order, lexical order, or a
correction as a matching tie-break.

## Decision

Separate source-occurrence identity from canonical-ledger identity.

ADR 0001 continues to govern source discovery, source replacement, record
matching, allocation origins, source-record IDs, and source transaction IDs.
Its active-row agreement now applies to the hidden source-occurrence artifact,
not to `categorized.csv`.

`categorized.csv` becomes the authoritative active canonical ledger. A
canonical row may represent one source occurrence or a pooled set of exact
source occurrences. Its transaction ID belongs to a stable abstract
multiplicity slot and does not claim a pairwise source match.

This narrows these ADR 0001 rules:

- the four source identity columns remain the schema for source-occurrence
  evidence, but canonical rows leave them empty because no one source owns the
  row;
- the ADR 0001 transaction-ID derivation remains exact for source occurrence
  IDs, while canonical transaction IDs use the derivation below;
- identity-manifest agreement validates hidden source occurrences, while a new
  overlap manifest validates canonical rows; and
- corrections, review, accounting flow, and transfer links use canonical
  transaction IDs after migration.

All other ADR 0001 privacy, no-guessing, source replacement, reset, hash
conflict, and recoverable persistence rules remain in force.
This includes ADR 0001's narrow parser-upgrade replacement exception: repeated
source rows may reallocate without a guessed pairing, while canonical review
history moves only when it is unique or fully agreed.

ADR 0002 required source identity-v2 fields on each local-memory observation.
After this migration, reviewed choices belong to stable canonical transaction
IDs whose public source fields must be empty. ADR 0002 therefore treats a
validated canonical group ID, slot, and transaction ID as current identity for
memory eligibility. It still requires two distinct reviewed canonical
transactions; two source occurrences consolidated into one canonical row count
as one observation, not two.

## Persisted state

### Hidden source occurrences

`<categorized.csv parent>/.honeymoney-source-occurrences.csv` contains the
normalized rows resolved by ADR 0001. It keeps active rows and the evidence for
retired record tombstones. The identity manifest record state decides which
rows are active. The file uses the ADR 0001 categorized columns and retains the
source transaction ID and four source identity fields. It is local financial
evidence and is never listed as a public output artifact.

The identity manifest remains
`.honeymoney-identity-manifest.json`. Its schema stays at version 1. Active
manifest records agree with the hidden source-occurrence rows. Retired manifest
records remain tombstones with retained evidence, but never take part in
canonicalization, balance checks, or public output.
An active evidence row carries the source's current revision. A retired row
carries its allocation-origin revision, which keeps its tombstone checkable
after later source revisions.

The exact hidden source header is:

```text
transaction_id,source_id,source_namespace_id,source_revision,source_record_id,date,transaction_date,posting_date,account_id,account,account_type,institution,country,original_amount,original_currency,posted_amount,posted_currency,amount_hkd,statement_opening_balance,statement_closing_balance,merchant,original_description,category,flow_type,flow_source,transfer_group_id,paired_transaction_id,reconciliation_status,reconciliation_confidence,owner,payment_method,confidence,needs_review,reason,flags,notes,source_file,source_page,source_row
```

### Hidden overlap manifest

`<categorized.csv parent>/.honeymoney-overlap-manifest.json` has this shape:

```json
{
  "schema_version": 2,
  "namespace_key": "ovns_<64 lowercase hex>",
  "groups": [
    {
      "memberships": [
        {
          "group_id": "ovr_<64 lowercase hex>",
          "membership_digest": "ovm_<64 lowercase hex>",
          "overlap_group_id": "ovg_<64 lowercase hex>",
          "resolution": "unresolved"
        }
      ],
      "overlap_group_id": "ovg_<64 lowercase hex>",
      "record_fingerprint": "fp_<64 lowercase hex>",
      "slots": [
        {
          "slot": 1,
          "transaction_id": "txn_<32 lowercase hex>",
          "state": "active"
        }
      ]
    }
  ]
}
```

The namespace key is generated once with 256 bits from the operating-system
random source. It is hidden and never appears in diagnostics. Groups sort by
`overlap_group_id`. Membership records sort by `membership_digest` and have
exactly `group_id`, `membership_digest`, `overlap_group_id`, and `resolution`,
in canonical JSON key order. `resolution` is `unresolved`, `same-event`, or
`keep-all`; there is no decision revision field. Slots sort by positive integer
slot and remain as active or retired tombstones. The manifest stores no paths,
source display, source ID, revision, record value, correction patch, or
statement text.

Schema 1 remains a valid migration input. A read checks its exact bytes, builds
schema 2 in memory from active hidden evidence, and leaves disk unchanged. The
next ledger generation publishes schema 2 through the recoverable generation
boundary.

Missing one hidden file from a migrated workspace fails closed. Neither hidden
file is reconstructed from canonical rows.

### Public canonical rows

The exact `categorized.csv` header is:

```text
transaction_id,canonical_group_id,canonical_slot,provenance_status,source_occurrence_count,source_id,source_namespace_id,source_revision,source_record_id,date,transaction_date,posting_date,account_id,account,account_type,institution,country,original_amount,original_currency,posted_amount,posted_currency,amount_hkd,statement_opening_balance,statement_closing_balance,merchant,original_description,category,flow_type,flow_source,transfer_group_id,paired_transaction_id,reconciliation_status,reconciliation_confidence,owner,payment_method,confidence,needs_review,reason,flags,notes,source_file,source_page,source_row
```

The exact `review_needed.csv` header is:

```text
transaction_id,canonical_group_id,canonical_slot,provenance_status,source_occurrence_count,source_id,source_namespace_id,source_revision,source_record_id,date,transaction_date,posting_date,account_id,account,account_type,institution,country,original_amount,original_currency,posted_amount,posted_currency,amount_hkd,statement_opening_balance,statement_closing_balance,merchant,original_description,suggested_category,suggested_flow_type,transfer_group_id,paired_transaction_id,reconciliation_status,suggested_owner,suggested_payment_method,category,flow_type,owner,payment_method,confidence,reason,flags,notes,source_file,source_page,source_row
```

Canonical rows populate all four. Their four source identity fields and three
source display fields are empty. Unresolved legacy rows populate neither the
canonical nor source identity fields and remain unconsolidated.

`provenance_status` is one of:

- `single_source`
- `exact_one_to_one`
- `pooled_equal_count`
- `ambiguous_count_mismatch`

Statement opening and closing balances remain source-occurrence evidence and
are empty on canonical rows.

## Canonical IDs

Use HMAC-SHA-256 with the hidden namespace key and explicit byte framing:

```text
group_id =
  "ovg_" + hmac_sha256(namespace_key,
    frame("canonical-overlap-group-v1", record_fingerprint))

canonical transaction_id =
  "txn_" + first_32_hex(
    hmac_sha256(namespace_key,
      frame("canonical-overlap-slot-v1", group_id, u64be(slot))))
```

The HMAC key is the 32 raw bytes decoded from the namespace suffix. `frame`
starts with the bytes `honeymoney.overlap` and one zero byte. It then writes a
four-byte big-endian domain byte length, the ASCII domain, a four-byte
big-endian component count, and, for each component, an eight-byte big-endian
length followed by the component bytes. The slot component is an eight-byte
big-endian positive integer.

The fixed test namespace
`ovns_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` and
fingerprint
`fp_654c440b50757a0bde1a276a892598f2436e2fb3d1d42634f9884cf1167b4eaf`
produce group
`ovg_5f5f4ebc1ea6bfdbfd6850cd67789f01d75526dda3adadaa5be4f7a8ccdf7eb5`
and slot-one transaction `txn_625c12709363b08f99b2645cb612fc91`.
The public IDs do not expose an unsalted equality hash of financial facts.
Validation recomputes both IDs and rejects collisions with source or canonical
IDs.

For duplicate review, the keyed membership digest frames these current inputs:

1. sorted active `(source_id, source_record_id)` pairs;
2. sorted `(source_id, occurrence_count)` pairs;
3. the keep-all canonical slot IDs `1..M`; and
4. the ambiguous tail slot IDs `K+1..M`.

Each section starts with its literal ASCII tag and an unsigned 64-bit
big-endian item count. Text values are separate ASCII components under the
same HMAC framing. The domain is `duplicate-membership-v1`; the result is
`"ovm_" + hmac_sha256(...)`. Source IDs take part only as HMAC input and never
appear in the manifest.

The public review group ID is:

```text
"ovr_" + hmac_sha256(namespace_key,
  frame("duplicate-review-group-v1",
        overlap_group_id, membership_digest))
```

With the fixed namespace, overlap group, and synthetic membership in
`tests/test_overlap.py`, the digest is
`ovm_714ca8c7e9462c590f454d32ccd434805c82f84f66e5caab900325dd3c3822a9`
and the review group is
`ovr_b7c20c5ed9091bea3b85c91b57935a7fac92e5ba73b3ae24fcb92d32a3524596`.

## Multiset rule

For every accepted record fingerprint, count active occurrences per
`source_id`. Let `M` be the maximum source count. The canonical ledger contains
slots `1..M`. A prior slot above `M` retires; later growth reactivates the same
ID.

Slot number is an abstract cardinality layer. It is never derived from a
source row, parser row, locator, or input order. Source occurrence IDs may be
sorted for canonical output, but sorting cannot assign them to slots.

Under the state supplied by ADR 0001 and issue #31, exact normalized
same-account equality across distinct active source occurrences selects an
overlap candidate. Canonical identity fields use the same normalized folded
text, ISO date, decimal, and currency values as the record fingerprint.
`amount_hkd` must agree by numeric value and uses that normalized decimal form.
When every source has the same stored spelling, date, decimal scale, or
currency case, the canonical row keeps it. When raw forms differ but normalize
to the same value, the canonical row stores the normalized value.
The display-only `account`, `account_type`, `institution`, and `country` fields
keep their value only when every source agrees byte for byte; a conflict yields
an empty canonical cell. A conflict in identity or totals fails closed. Equal
eligible counts consolidate without a prompt. Different eligible counts stay
pooled and ambiguous. No reliable statement-period field exists in that state,
so this decision adds no period gate. Adding one without source evidence would
guess. A later ADR may add such a gate if import profiles supply a checked
statement period.

- One contributing source produces `M` `single_source` rows.
- Several sources with one row each produce one `exact_one_to_one` row.
- Several sources with the same count greater than one produce `M`
  `pooled_equal_count` rows.
- Different source counts produce `M` `ambiguous_count_mismatch` rows.

For a count mismatch, let `K` be the second-largest source count: the largest
multiplicity supported by at least two sources. `honeymoney duplicates` lists
only unresolved count mismatches. `keep-all` retains `M`. `same-event` retains
`K` and never fewer. Equal-count groups never enter this list.

For slot `k`, the support count is the number of sources whose count is at
least `k`. This identifies overlap and unmatched cardinality without selecting
which raw occurrence is unmatched.

One-to-one provenance may list all contributing source occurrence IDs for slot
1. Repeated equal groups expose only a pooled group of source occurrence IDs.
They never expose or persist an invented pairwise association.

Count mismatch forces review with the idempotent
`overlap_count_ambiguous` flag. Equal pooled counts do not force review because
their supported multiplicity is exact.

Every observed mismatch membership gets one exact manifest membership record.
Resolving it changes only `resolution`. Repeating the same resolution changes
no bytes. A different resolution for the same active membership fails. If
replacement, reset, or import changes any bound membership input, the old
record remains a tombstone, its decision does not apply, and the new membership
gets a new public group ID with `unresolved` review state. Commands distinguish
unknown and stale IDs with stable error codes.

## Mutable state

Existing canonical state and corrections join by canonical transaction ID.
Source-ID corrections from the pre-canonical ledger remain migration aliases.

- One canonical slot may accept one unique alias correction.
- A repeated pool may accept a correction only when every applicable alias
  patch is equal and can safely apply to every slot.
- Conflicting or partial repeated history remains pooled, forces
  `overlap_history_ambiguous` review, and is not assigned to a slot.

Canonical corrections take precedence. Retired-slot corrections remain so an
exact slot recurrence restores the reviewed decision.

`keep-all` leaves corrections unchanged. `same-event` removes tail corrections
only when the retained state proves that no reviewed choice or flow history
will be lost. At floor `K=1`, one compatible tail correction may move to the
sole retained slot when that slot has no conflict. At `K>1`, no correction
moves between abstract slots. Non-overlap `needs_review`, reason, flag, note,
category, flow, transfer, reconciliation, owner, payment, and confidence state
is protected. Any doubt fails before persistence.

Reset removes corrections owned only by reset source occurrences. It removes a
canonical correction only when every active source supporting its group belongs
to the reset batch. A remaining source keeps the canonical decision.

Transfer links and reconciliation state are recomputed from canonical IDs.
Statement balance reconciliation runs against source occurrences. Cash-flow and
report totals run against canonical rows.

## Persistence and migration

The source-occurrence CSV, identity manifest, overlap manifest, canonical
ledger, review CSV, and changed corrections publish in one recoverable
generation. `categorized.csv` remains the generation commit point. Duplicate
resolution does not rewrite the last import report.

Replacement and reset retain normalized evidence for retired identity records
in the hidden source-occurrence CSV. The identity manifest remains the source
of active or retired state. A restore can therefore check old facts without
putting retired rows back into balances or the canonical ledger.

The only automatic migration input is the exact ADR 0001/#31 state: a valid v2
public ledger and identity manifest with neither new hidden artifact. Migration
reads leave that public view and its source IDs unchanged. The first write
copies the old rows into source-occurrence evidence, creates a random namespace
and canonical groups and slots, clears generated duplicate-candidate state,
applies only proven mutable-state assignments, and publishes all new state
together. Thus read-only commands never show temporary canonical IDs.

If that first write replaces or resets a source, it builds the canonical
baseline before source-record matching. Exact locator and fingerprint matches
keep their source owners, followed by unique fingerprint matches. Repeated
unmatched records retire and receive new source owners without pairing old and
new occurrences. Compatible corrections and review history remain on the
stable canonical slots.

If a parser repair changes account or currency fields during that first write,
correction projection may compare old and new rows within the same `source_id`.
The comparison uses normalized dates, original and posted amounts, merchant,
and description; it does not use the repaired account or currency fields. A
unique old and new row may carry one correction. A repeated group may carry a
correction only when its counts match, every old row has a correction, and all
patches agree. Partial or conflicting groups stay pooled and require review.
Corrections never choose source owners or canonical slots. The published
correction file uses final canonical IDs and removes the retired source-ID
aliases.

Any partial canonical state fails before persistence. Repeated migration,
import, replacement, reset, correction, and reconciliation are idempotent.

## Public diagnostics

Human and JSON output distinguish parsed source occurrences from canonical
ledger occurrences. Privacy-safe overlap diagnostics may include:

- canonical group and transaction IDs;
- provenance status;
- structural source, occurrence, slot, and ambiguity counts; and
- sorted pools of opaque source occurrence transaction IDs.

Operational warnings never include the namespace key, record fingerprint,
source ID, source revision, path, source display, allocation locator,
transaction values, or statement text. The explicit local `duplicates` list
may show bounded account ID, account, institution, date, merchant, posted
amount and currency, safe source basename and locator, opaque source occurrence
IDs, and the separate pooled canonical slot IDs. It never pairs a source
occurrence with a canonical slot. It never shows raw descriptions, paths, the
private namespace, or source IDs.

## Consequences

The canonical ledger prevents overlapping statements from inflating totals and
keeps genuine multiplicity. Repeated equal occurrences remain honest pooled
evidence. A user may correct stable canonical slots after creation, but
pre-canonical per-occurrence decisions cannot move into an indistinguishable
pool without proof.

The cost is two hidden generation files, a public CSV schema migration, and a
split between statement reconciliation and financial-ledger reconciliation.
