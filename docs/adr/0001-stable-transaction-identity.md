# ADR 0001: Stable transaction identity and source provenance

- Status: Accepted (maintainer approved on 2026-07-21 in GitHub #24)
- Date: 2026-07-21
- Issue: GitHub #24
- Supersedes: the first identity-v2 proposal on GitHub #24; this ADR follows
  the later “Superseding identity v2 proposal” and its architecture-review
  corrections
- Related plans: 008 (stable transaction identity), 009 (stable source
  namespace)

## Approval

The maintainer approved the contract defined by this ADR on 2026-07-21 in
GitHub #24 (“the superseding identity v2 proposal”), superseding two earlier
proposals on the same issue. The four persisted public identity columns, the
hidden `.honeymoney-identity-manifest.json`, the batch-global source
resolution, the unique-assignment record reconciliation, and the conservative
ambiguity behavior defined here are the accepted identity-v2 contract for
Honeymoney and may now be implemented against this decision.

## Context

Honeymoney currently derives a transaction ID from normalized financial facts
and adds a suffix only when identical identity bases occur in the same import
batch. The suffix depends on current batch membership and order. Consequently:

- two identical rows imported separately receive the same ID and one silently
  overwrites the other during dictionary merge;
- the same rows imported together receive different, batch-suffixed IDs;
- adding or reordering an identical source can move a persisted correction to
  another source;
- single-file imports reduce source provenance to a basename, so same-named
  files in different directories collide;
- replacement and reset can delete or reassign ambiguous legacy rows and
  corrections before ambiguity is recognized.

Corrections are keyed by `transaction_id`, and `categorized.csv` is the
authoritative cumulative ledger. Identity is therefore persisted public state,
not a temporary parser detail. Source provenance must distinguish logical
statements without exposing absolute paths or statement contents. The design
must also preserve uniquely identifiable legacy IDs and refuse to guess when
old repeated rows cannot be distinguished.

The identity-resolution module will sit between normalized parser output and
all categorization, correction application, reconciliation, and ledger merge.
Its interface consumes the incoming source batch plus the existing ledger list
and returns either a fully resolved list with diagnostics or a validation
failure. Hashing, source assignment, legacy migration, record matching, and
collision checks remain inside that module so callers cannot partially apply
the protocol.

## Decision

Adopt identity version 2 with four persisted source/record fields, stable
transaction IDs derived from logical source and record IDs, batch-global source
resolution, and conservative record reconciliation. `source_file` remains
human-readable display provenance only and is never an identity or replacement
key.

Identity migration operates on ordered lists before any dictionary keyed by
`transaction_id` is constructed. Exact matches preserve prior IDs and
corrections. A match is accepted only when it is the unique maximum assignment
under the rules below. Ambiguity is never resolved with file order, parser
order, page, row, or an arbitrary tie-breaker.

### Canonical digest framing

All v2 digests use this byte-exact function:

```text
digest(domain, component_1, ..., component_n) =
  lowercase_hex(
    SHA-256(
      b"honeymoney.identity\x00" ||
      u32be(len(utf8(domain))) || utf8(domain) ||
      u32be(n) ||
      for each component:
        u64be(len(component)) || component
    )
  )
```

`domain` is the literal ASCII domain named in this ADR. Components are bytes;
text components are UTF-8 after the field-specific normalization below.
Lengths are unsigned big-endian integers. No delimiter joining, JSON string
interpolation, platform-default encoding, or abbreviated digest is permitted.
The full digest is 64 lowercase hexadecimal characters.

### Persisted public schema

Both `categorized.csv` and `review_needed.csv` add these four text columns
immediately after `transaction_id`, in this order:

| Column | Exact format | Invariant |
|---|---|---|
| `source_id` | `src_` plus 64 lowercase hex characters | One logical statement across accepted revisions and renames |
| `source_namespace_id` | `ns_` plus 64 lowercase hex characters | Current normalized logical locator |
| `source_revision` | `rev_` plus 64 lowercase hex characters | Exact current source bytes |
| `source_record_id` | `rec_` plus 64 lowercase hex characters | One logical record within `source_id` |

The validators use these exact regular expressions:

```text
source_id:           ^src_[0-9a-f]{64}$
source_namespace_id: ^ns_[0-9a-f]{64}$
source_revision:     ^rev_[0-9a-f]{64}$
source_record_id:    ^rec_[0-9a-f]{64}$
new transaction_id:  ^txn_[0-9a-f]{32}$
```

Preserved legacy transaction IDs are exempt from the new transaction-ID regex.
The four v2 fields have an all-or-none invariant. Four populated and valid
fields form a v2 row. Four empty fields form an unresolved legacy row and are
valid whether or not that row is targeted by the current import. Any partial
population is `identity_partial_v2_metadata` and fails before persistence.

The exact `categorized.csv` header is:

```text
transaction_id,source_id,source_namespace_id,source_revision,source_record_id,date,transaction_date,posting_date,account_id,account,account_type,institution,country,original_amount,original_currency,posted_amount,posted_currency,amount_hkd,statement_opening_balance,statement_closing_balance,merchant,original_description,category,flow_type,flow_source,transfer_group_id,paired_transaction_id,reconciliation_status,reconciliation_confidence,owner,payment_method,confidence,needs_review,reason,flags,notes,source_file,source_page,source_row
```

The exact `review_needed.csv` header is:

```text
transaction_id,source_id,source_namespace_id,source_revision,source_record_id,date,transaction_date,posting_date,account_id,account,account_type,institution,country,original_amount,original_currency,posted_amount,posted_currency,amount_hkd,statement_opening_balance,statement_closing_balance,merchant,original_description,suggested_category,suggested_flow_type,transfer_group_id,paired_transaction_id,reconciliation_status,suggested_owner,suggested_payment_method,category,flow_type,owner,payment_method,confidence,reason,flags,notes,source_file,source_page,source_row
```

The review CSV copies the four values from its ledger row. `source_file`,
`source_page`, and `source_row` remain display-only fields in their existing
positions. Public CSV serialization and canonical read-back must round-trip all
five new ID formats exactly without adding spreadsheet-safety escape prefixes;
the identifiers begin with ordinary ASCII letters and contain only ASCII
letters, digits, and underscore.

The identifiers are calculated as follows:

```text
source_namespace_id =
  "ns_" + digest("source-namespace-v1", utf8(locator_kind),
                  utf8(logical_locator_v1))

source_revision =
  "rev_" + digest("source-revision-v1", exact_source_bytes)

new source_id =
  "src_" + digest("source-id-v2", ascii(source_namespace_id))

new source_record_id =
  "rec_" + digest("source-record-v2", ...allocation inputs below...)

new transaction_id =
  "txn_" + first_32_hex(
    digest("transaction-id-v2", b"2", ascii(source_id),
           ascii(source_record_id))
  )
```

`first_32_hex` is the first 32 hexadecimal characters, representing 128 digest
bits. An accepted rename reuses the persisted `source_id`; it does not
recalculate it from the new namespace. Preserved legacy transaction IDs are a
supported mixed-version case.

Mutable category, owner, payment method, confidence, reason, notes, flags,
review state, accounting flow, reconciliation state, source display, and source
revision values never enter `transaction_id`.

## Authoritative identity manifest

Ledger rows cannot represent a successfully processed source that yields zero
transactions or retain ownership of a retired transaction ID. A hidden
identity manifest is therefore authoritative for both sources and active or
retired record ownership. Its fixed path is
`<categorized.csv parent>/.honeymoney-identity-manifest.json`.

The file is UTF-8 without a BOM and has this exact schema:

```json
{
  "schema_version": 1,
  "sources": [
    {
      "source_id": "src_<64 lowercase hex>",
      "source_namespace_id": "ns_<64 lowercase hex>",
      "source_revision": "rev_<64 lowercase hex>",
      "extractor_contract_id": "ext_<64 lowercase hex>",
      "records": [
        {
          "source_record_id": "rec_<64 lowercase hex>",
          "transaction_id": "txn_<16 or 32 lowercase hex>",
          "transaction_id_kind": "v2",
          "record_fingerprint": "fp_<64 lowercase hex>",
          "state": "active",
          "current_locator": {
            "adapter_tag": 1,
            "components": [2]
          },
          "allocation_origin": {
            "source_revision": "rev_<64 lowercase hex>",
            "extractor_contract_id": "ext_<64 lowercase hex>",
            "locator": {
              "adapter_tag": 1,
              "components": [2]
            },
            "occurrence_ordinal": 1
          }
        }
      ]
    }
  ]
}
```

The example shows an active v2-derived CSV record. Exact alternatives are:

- `transaction_id_kind` is `v2` or `preserved_legacy`. `v2` requires
  `^txn_[0-9a-f]{32}$` and must equal the derived transaction hash.
  `preserved_legacy` requires the pre-v2 `^txn_[0-9a-f]{16}$` format and is not
  recomputed.
- `state` is `active` or `retired`. An active record requires a non-null
  `current_locator`; a retired record requires `current_locator: null`.
- Locator objects use the exact adapter tags, component counts, and one-based
  uint64 ranges defined under new-record allocation.

No additional top-level, source, record, allocation-origin, or locator keys are
allowed. Sources are sorted by `source_id`; records are sorted by
`source_record_id`. JSON is serialized under the canonical JSON rules below,
followed by one LF.

The manifest stores only identifiers, hashes, enum values, ordinals, and non-
sensitive numeric allocation locator tuples. It never stores a raw relative or
absolute source locator, source bytes, source display, record text, correction,
category, or display `source_page`/`source_row`.

Validation enforces all of these invariants before any dictionary merge:

- source IDs and current namespace IDs are globally unique; equal source
  revisions and extractor contracts are allowed;
- source-record IDs and claimed transaction IDs are globally unique;
- every record fingerprint matches `^fp_[0-9a-f]{64}$`;
- each active current locator is unique within its source, and retired records
  have no current locator;
- each allocation-origin locator/ordinal tuple is unique within its source,
  revision, extractor contract, and fingerprint;
- recomputing `source_record_id` from the parent `source_id`, record
  fingerprint, and complete allocation origin yields the stored ID;
- recomputing `transaction_id` yields the stored ID only when
  `transaction_id_kind` is `v2`; preserved legacy IDs are accepted by their
  legacy regex and ownership record;
- every active record has exactly one ledger row with matching source ID,
  namespace, revision, source-record ID, transaction ID, and fingerprint;
- every v2 ledger row has exactly one active manifest record; retired records
  have no ledger row; and
- ambiguous shared legacy transaction IDs remain unclaimed: they have all four
  public v2 fields empty and no manifest record.

Malformed JSON, unsupported schema, regex failure, duplicate ownership,
derived-ID mismatch, locator failure, row/manifest disagreement, or an
ambiguous legacy ID claimed by the manifest is `identity_manifest_invalid` and
fails before persistence.

An absent ledger and absent manifest are valid pristine state after retained-
generation recovery has confirmed that no interrupted generation exists. This
is the normal first-import case; create an empty in-memory manifest and publish
the first ledger and manifest together. Setup-created corrections or other
non-authoritative starter artifacts do not make the workspace non-pristine.

Missing-manifest bootstrap is also allowed when `categorized.csv` has the exact
pre-v2 header with none of the four identity columns. Treat every such row as
all-empty legacy identity state, preserve the list without claiming record
ownership, and create an empty manifest; targeted rows gain ownership only
through unique migration. An empty pre-v2 ledger also bootstraps an empty
manifest. If the v2 header or any v2 identity metadata exists while the
manifest is absent, including an empty v2 ledger, fail closed with
`identity_manifest_missing`. A manifest without its authoritative ledger is
also invalid. V2 state is never reconstructed from ledger rows.

The manifest is published through the existing recoverable generation
protocol in the same generation as ledger, review, report, and corrections
changes. Its staged content and prior-file backup are flushed before public
replacement, it is restored on pre-commit failure, and retained recovery
completes or rolls it back with the ledger generation. The ledger remains the
generation commit point.

Every ledger-writing path must read, validate, carry forward, and publish the
manifest: ordinary import, replace, reset, interactive import/review, structured
`correct`, one-shot review, `reconcile`, legacy migration, and retained-
generation recovery. A path that changes only mutable ledger fields preserves
all source/record ownership byte-for-byte except active row consistency. No
ledger writer may omit the manifest from its generation.

### Record ownership lifecycle

- **New:** allocate the source-record ID and v2 transaction ID, append an active
  ownership record, set its allocation origin once, and set current locator.
- **Matched active:** preserve source-record ID, transaction ID, kind, and
  allocation origin; update current locator and active fingerprint mapping.
- **Exact-origin retired recurrence:** only when an incoming record's source
  revision, extractor contract, allocation locator, fingerprint, and
  occurrence ordinal exactly equal one retired ownership record's immutable
  allocation origin, preserve that ownership, change state to active, and set
  current locator. A fingerprint-only match cannot reactivate a tombstone.
- **Retire:** when an active record is uniquely unmatched by an accepted
  replacement, remove its ledger row, set state to retired, and set current
  locator to null. Never delete its ownership entry during replace.
- **Reset:** preserve every active and retired ownership entry, clear all
  corrections whose transaction ID is owned by that source, then run the normal
  pipeline for current active rows. Reset does not delete tombstones.
- **Source becomes empty:** retire all uniquely resolved active records and
  retain the source with its current namespace, current zero-record source-
  bytes revision hash, extractor contract, and complete retired ownership set.

The first successful import writes the manifest even when the source contains
zero records. Empty replacement, reset while empty, accepted rename, later
revision, and exact-origin older-revision recurrence therefore retain the same
`source_id` and the ownership needed to clear or restore corrections.

## Locator contract

### Identity workspace root

The identity workspace root is `resolve(strict=True)` of the parent directory
of the active workspace configuration file. It does not depend on whether the
CLI receives a single file or a directory. When configuration is selected by
the current-directory default, the resolved default config path is used first
and its parent is the root.

Moving that root, its config, and its workspace-relative sources together is
the supported workspace-move operation. The resulting relative locators remain
unchanged.

### `logical_locator_v1`

The source path is resolved with `resolve(strict=True)` before classification.
This eliminates `.` and `..` and resolves every symlink. The resolved root is
resolved by the same rule. A path is `workspace` kind only if the resolved
source is a descendant of the resolved root; a symlink inside the workspace
that resolves outside it is `external`. A symlink and its real target therefore
produce the same locator and namespace.

For `workspace` kind, `logical_locator_v1` is the source path relative to the
resolved root. For `external` kind, it is the resolved absolute path. In both
cases:

1. Convert to POSIX `/` separators with no trailing slash.
2. Normalize every path component independently to Unicode NFC.
3. Preserve component case exactly; do not case-fold or lowercase.
4. Reject NUL and unrepresentable path components.
5. Encode the resulting string as UTF-8 for hashing.

On a case-insensitive filesystem, alternate case spellings are not unified by
identity code beyond whatever spelling `resolve` returns. Case remains part of
the locator contract. The locator kind (`workspace` or `external`) is a
separate digest component, so identical text in different kinds cannot alias.

The raw external locator exists only in memory long enough to hash it. It is
never stored in a public artifact, report, diagnostic, retained-generation
state, or log.

## Batch-global source resolution

Resolve all incoming sources together before processing any record. Candidate
claims are one-to-one across the batch: one prior `source_id` may be assigned to
at most one incoming source, and one incoming source may resolve to at most one
prior `source_id`. Input discovery order cannot affect the result.

Prior-source lookup uses the authoritative identity manifest, including entries
for zero-row sources. Ledger rows are consistency evidence, not the source
registry.

Before applying the v2 table below, perform one narrowly scoped legacy source
claim whenever the retained ledger contains unresolved, unowned, all-empty-v2
legacy groups. This applies both during initial pre-v2 bootstrap and on later
commands after unrelated v2 imports have already published the manifest. Group
legacy rows by their exact NFC-normalized `source_file` display, without
collapsing rows or transaction IDs. An incoming source may claim a legacy group
only when its privacy-safe display is equal and the complete batch has a one-
to-one mapping: one incoming source, one legacy group, and no competing claim
on either side. Multiple possible claims are
`identity_legacy_source_ambiguous`.

A unique claim allocates the source's v2 `source_id`, namespace, revision, and
extractor contract from the incoming source and establishes its manifest entry
for the migration transaction. Ordinary import then returns the existing
already-imported error. `--replace` or `--reset` treats that claimed manifest
entry as its explicit target and proceeds to record migration; the v2 target-
not-found rule does not run for that source. If no legacy group is uniquely
claimed, resolution continues through the v2 table, so replace/reset with no
manifest or legacy target still fails rather than creating a source. Legacy
claims are decided batch-globally before any manifest or row mutation.

| Namespace candidates | Intent | Unclaimed equal-revision candidates | Result |
|---|---|---|---|
| Exactly one | Ordinary import | Not consulted | Current already-imported validation error |
| Exactly one | `--replace` or `--reset` | Not consulted | Reuse its `source_id` |
| More than one | Any | Not consulted | `identity_source_namespace_ambiguous` |
| None | Ordinary import | Not consulted | Allocate a new `source_id`; byte-identical copies coexist |
| None | `--replace` or `--reset` | Exactly one | Reuse that `source_id` as an accepted rename/move |
| None | `--replace` or `--reset` | None | `identity_source_target_not_found`; no source is created |
| None | `--replace` or `--reset` | More than one | `identity_source_revision_ambiguous` |

An equal-revision candidate is a prior source whose stored `source_revision`
equals the incoming `source_revision` and which no other incoming source has
claimed. If two incoming sources would claim the same prior source, the whole
claim set is ambiguous even when each incoming file sees one candidate in
isolation.

Revision fallback is allowed only for explicit replacement/reset intent.
Ordinary import never infers a rename from equal bytes, because a copied
statement is a distinct source. Except for the unique legacy-migration claim
above, explicit replace/reset is never a source-creation operation: when
neither namespace nor one unique unchanged revision identifies its target, it
fails with `identity_source_target_not_found` and the user must run an ordinary
import. An external move combined with changed bytes therefore fails under
replace/reset and requires ordinary import as a new source.

After an accepted rename, persist the reused `source_id`, advance its
`source_namespace_id` to the new locator, and persist the incoming revision.
All later revisions at that namespace resolve by exact namespace.

## Record identity and reconciliation

### `record_fingerprint_v2`

The internal record fingerprint is the prefixed full result of:

```text
"fp_" + digest(
  "record-fingerprint-v2",
  account_id,
  date,
  transaction_date,
  posting_date,
  original_amount,
  original_currency,
  posted_amount,
  posted_currency,
  merchant,
  original_description,
)
```

Fields have this exact order and normalization:

| Field | Normalization |
|---|---|
| `account_id` | Unicode NFC, trim Unicode whitespace, collapse internal Unicode whitespace to one ASCII space, then Unicode case-fold |
| `date`, `transaction_date`, `posting_date` | Parsed ISO `YYYY-MM-DD`, or empty bytes when absent |
| `original_amount`, `posted_amount` | Finite decimal; normalize negative zero to zero; emit plain base-10 without exponent or grouping; remove insignificant trailing fractional zeros and a trailing decimal point; absent is empty bytes |
| `original_currency`, `posted_currency` | Unicode NFC, trim, then ASCII uppercase; empty when absent |
| `merchant`, `original_description` | Unicode NFC, trim Unicode whitespace, collapse internal Unicode whitespace to one ASCII space, then Unicode case-fold |

The field names are not encoded because their fixed order is versioned by the
domain. Invalid non-finite amounts cannot enter identity resolution.

Category, owner, payment method, confidence, reasons, notes, flags, flow and
reconciliation state, source display, source revision, source page/row,
extractor order, file order, and batch order are excluded.

### Extractor contract ID

The manifest and new-record allocation use `extractor_contract_id` so exact
source bytes parsed under materially different extraction behavior do not
reuse tokens accidentally. It is:

```text
"ext_" + digest("extractor-contract-v1", utf8(parser_adapter_version),
                 canonical_profile_json)
```

`parser_adapter_version` is a non-empty ASCII source-code constant specific to
the selected live adapter (`csv`, `pdf-table`, `pdf-word`, or `pdf-sectioned`).
It changes whenever that adapter's record extraction, splitting, or immutable-
field normalization semantics change without a profile change.

Start with the whole validated selected profile. Remove only these closed,
top-level presentation/classification keys when present:

```text
account
account_type
category
categories
confidence
country
flags
flow_type
institution
needs_review
notes
owner
payment_method
reason
rules
```

No recursive key removal occurs. Every other top-level key and its complete
nested value is included, including `id`, `account_id`, `account_currency`,
CSV/PDF extraction configuration, date formats, statement-year rules,
description skips, sign rules, and future validated extraction keys. Adding,
removing, or changing an included value changes the extractor contract;
changing only an excluded top-level value does not.

Before canonicalization, normalize every object key and string value to Unicode
NFC and reject duplicate keys created by normalization. Then serialize with
RFC 8785 JSON Canonicalization Scheme rules: object keys in canonical order,
array order preserved, lowercase `true`/`false`/`null`, shortest valid finite
JSON number representation (with negative zero serialized as `0`), required
JSON escaping only, no insignificant whitespace, and UTF-8 output without a
BOM. NaN and infinities are invalid profile values. `canonical_profile_json`
is those exact bytes.

Golden tests pin the canonical bytes and `ext_` digest for Unicode strings,
integers, non-integral numbers, booleans, null, nested objects, and arrays.
Mutation tests cover every excluded key and representative included top-level
and nested extraction keys.

### Unique matching without order-derived context

Reconcile records independently within each resolved `source_id`, but decide
the complete assignment before mutating any row.

For a changed source revision or extractor contract, first compute each
incoming record's complete would-be allocation origin. If its source revision,
extractor contract, allocation locator, fingerprint, and occurrence ordinal
exactly equal one retired manifest record's immutable allocation origin,
reactivate that ownership. Allocation-origin uniqueness makes this a direct
identity proof rather than a tie-breaker. Remove those pairs from further
matching.

Then construct a bipartite graph whose left vertices are the source's remaining
active manifest ownership records and whose right vertices are the remaining
incoming records. An edge always requires equal `record_fingerprint_v2`.

For a fingerprint appearing once on each side, add the one edge. For a
fingerprint repeated on either side of a changed revision, add every possible
old/new edge for that fingerprint. The repeated group is deliberately complete.
Neighbor fingerprints, boundaries, page, row, parser order, file order, batch
order, and any other sequence-derived context are forbidden from pruning it.

Find maximum-cardinality matchings over the complete graph. Accept prior-row
reuse only when exactly one valid maximum matching exists. If two or more
maximum matchings exist, the source is `identity_record_match_ambiguous`; no
lexical, positional, “first seen,” or correction-aware tie-breaker is allowed.
Uniquely unmatched active records are retired, unmatched retired records
remain retired, and uniquely unmatched incoming records are new. Removing one
of several indistinguishable active occurrences is ambiguous because more than
one maximum assignment identifies a different retired record. Retired records
never enter this fingerprint graph, so a genuinely new future charge cannot
inherit an orphan correction merely because it has the same normalized facts.

When incoming `source_revision` and `extractor_contract_id` exactly equal the
manifest's current values, bypass graph matching and use the manifest's exact-
state mapping. Normalize every incoming record and key it by the exact pair
`(allocation_locator_v1, record_fingerprint_v2)`. That set must equal the set
of `(current_locator, record_fingerprint)` pairs for the manifest's active
records, with no duplicate key on either side. A mismatch is
`identity_exact_state_mismatch` and fails before persistence.

For an exact-state replace, reuse each mapped record's stored
`source_record_id`, `transaction_id`, `transaction_id_kind`, and
`allocation_origin` without recomputing or reallocating identity. It is an
identity no-op. For an exact-state reset, use the same retained mapping, clear
corrections for every transaction ID the source owns in active or retired
manifest records, and then run the normal pipeline. Zero-row exact state maps
an empty incoming set to an empty active set while retaining tombstones.

Manifest validation independently recomputes `source_record_id` for every
ownership record and recomputes `transaction_id` only for `v2` records.
`preserved_legacy` transaction IDs are valid retained ownership and are never
re-derived from a v2 token. Thus an unchanged migrated legacy source is also an
identity no-op, not a token-recomputation path.

### New-record token allocation

After accepting the unique matching, allocate each unmatched incoming record:

```text
source_record_id = "rec_" + digest(
  "source-record-v2",
  ascii(source_id),
  ascii(source_revision),
  ascii(extractor_contract_id),
  ascii(record_fingerprint_v2),
  allocation_locator_v1,
  u64be(occurrence_ordinal),
)
```

`allocation_locator_v1` is a tagged binary tuple:

```text
b"honeymoney.record-locator-v1\x00" ||
u8(adapter_tag) || u8(component_count) ||
u64be(component_1) || ... || u64be(component_n)
```

Every component is a canonical one-based unsigned integer in the range
`1..2^64-1`. Zero, missing components, and duplicate tuples within one source
revision/extractor contract are hard extraction errors. The supported live-
adapter tuples are closed and exact:

| Adapter | Tag | Components in order |
|---|---:|---|
| CSV | 1 | `(physical_row)` |
| PDF table | 2 | `(page, table, row, subrow)` |
| PDF word | 3 | `(page, physical_line)` |
| PDF sectioned | 4 | `(page, physical_line)` |

CSV `physical_row` is the one-based starting physical line of the CSV record,
including header and skipped lines in the count. PDF `page` is the physical
one-based page. PDF-table `table` is the one-based table returned on that page,
`row` is the original one-based extracted table row including header rows, and
`subrow` is one for an unsplit row or the one-based segment when one physical
row is split into several transactions. PDF-word and PDF-sectioned
`physical_line` are the original one-based reconstructed physical lines before
filtering, section removal, continuation folding, or transaction selection.

Adapters must plumb these immutable extraction locators through normalization:
the table adapter preserves table index and split subrow; word and sectioned
adapters preserve the original physical line; CSV preserves the record's
starting physical line. `source_page` and `source_row` remain display-only and
cannot substitute for the allocation tuple.

Tuples are ordered lexicographically by unsigned numeric `(adapter_tag,
component_1, ..., component_n)`. `occurrence_ordinal` is one-based among
incoming records with the same fingerprint in that revision and extractor
contract in this numeric locator order. It is a consistency discriminator for
new allocation, not a matching input.

Mutable position is allowed here only because the record has been proven new;
it can never select among prior occurrences or transfer a correction.

Matched active or retired records always reuse the prior `source_record_id`,
`transaction_id`, transaction-ID kind, and allocation origin. A new record's
transaction ID is derived from its newly allocated token. A different source
revision changes the allocation domain for unmatched records, preventing a
genuinely new occurrence from inheriting a retired token.

An older source revision is not exact current state. A retired record can
reactivate only through the exact allocation-origin proof above, preserving
its original IDs and any correction that survived a replace. Otherwise only
active records participate in fingerprint matching; an unmatched incoming
record receives a new identity even if a retired record has the same
fingerprint. This conservative rule prefers losing historical linkage over
transferring an orphan correction to an unproven recurrence.

## Legacy migration and corrections

Read the ledger as a list and retain every row. Do not construct a dictionary
keyed by `transaction_id` until all steps in this section have completed and
all collision checks have passed.

1. Partition complete v2 rows, all-empty unresolved legacy rows, and invalid
   partial-v2 rows. All-empty legacy rows are valid; partial-v2 rows are hard
   validation errors.
2. Group legacy rows by their stored display provenance without collapsing
   duplicate transaction IDs.
3. A legacy source group is eligible for migration only when it maps to exactly
   one incoming resolved source by exact NFC-normalized `source_file` display
   equality and no other legacy group or incoming source claims that mapping.
   Display matching is a migration aid only, never a v2 identity rule.
4. Build legacy fingerprints from the exact v2 field contract and include the
   legacy rows in the same bipartite matching algorithm.
5. A uniquely matched legacy row keeps its existing `transaction_id`, receives
   the resolved v2 source fields and a deterministic `source_record_id`, and
   retains its correction. Its manifest ownership records
   `transaction_id_kind: preserved_legacy`, its immutable allocation origin,
   and its current locator. Later unchanged replace/reset operations reuse that
   ownership exactly; they never replace the legacy transaction ID with a v2
   ID.
6. Retain every row in an ambiguous legacy group as a separate list element.
   Leave unresolved v2 fields empty and apply protected ambiguity state because
   migration of this targeted group was attempted and failed uniquely.
7. Pass non-targeted legacy rows through unchanged until a later import can
   resolve them. They remain all-empty v2 metadata rows and do not receive an
   ambiguity flag merely because another source is imported.

If two retained legacy rows share a transaction ID, that ID is an explicit
ambiguous legacy key, not a merge key. A correction keyed by it remains byte-for-
byte in the corrections document but is not copied, deleted, or newly applied
to any candidate row. Once explicit future tooling resolves the ambiguity, it
may establish individual keys; such tooling is outside #24.

Downstream processing must keep protected ambiguous rows in a list or address
them with an internal per-row handle. It must never insert them into a
transaction-ID dictionary. A new `correct` or one-shot `review` request naming
an ambiguous shared ID fails with
`identity_legacy_transaction_id_ambiguous` before persistence; it cannot choose
one row or fan the correction out to every row.

`identity_migration_ambiguous` is protected state applied after rules and
corrections. Append the exact token `identity_migration_ambiguous` to `flags`
idempotently, set `reason` to `Identity migration is ambiguous; explicit
resolution is required`, and force `needs_review=true` even when an old
correction says false. A category or accounting decision from an existing
unambiguous correction may remain visible, but it cannot clear this review
state. Ordinary import may retain ambiguous rows while adding a genuinely new
source; its import report has status `partial_success` and a deterministic
privacy-safe warning. Existing strict/non-strict partial-success exit semantics
remain unchanged.

Only a source/record group actually considered for migration and found
ambiguous receives the protected flag, reason, and partial-success diagnostic.
The presence of unrelated unresolved legacy rows is not itself an error or
warning.

For unambiguously retired rows, `--replace` retains their correction records so
a later exact allocation-origin recurrence can restore them. `--reset` removes
corrections for every transaction ID owned by the uniquely resolved target
source, including active and retired manifest ownership, only after source and
record resolution has succeeded. Reset while the source is empty therefore
still clears its retired corrections. Neither operation rekeys or copies a
correction.

## Replacement, reset, and persistence

`--replace` and `--reset` are validation-first operations. If any targeted
source assignment, legacy migration, record matching, or hash comparison is
ambiguous or conflicting, return exit code 2 before staging any persistence.
The ledger, review CSV, corrections, rules, import report, and retained
generation state, including the identity manifest, remain byte-for-byte
unchanged.

Only after the complete batch resolves successfully may the existing
recoverable generation mechanism stage outputs. Ledger changes, regenerated
review rows, and any reset correction removals publish in one generation, with
the ledger remaining the commit point.

## Privacy-safe diagnostics

Diagnostics use stable codes and structural counts. They may include the
existing privacy-safe `source_file` display, requested action, candidate count,
and affected record count. They must include a remediation statement such as
“identity is ambiguous; retain the source and request explicit resolution.”

Defined codes are:

- `identity_source_namespace_ambiguous`
- `identity_source_revision_ambiguous`
- `identity_source_target_not_found`
- `identity_manifest_invalid`
- `identity_manifest_missing`
- `identity_exact_state_mismatch`
- `identity_record_match_ambiguous`
- `identity_legacy_source_ambiguous`
- `identity_legacy_transaction_id_ambiguous`
- `identity_partial_v2_metadata`
- `identity_allocation_locator_invalid`
- `identity_hash_conflict`

Other than the required IDs/hashes in the ledger, review CSV, and hidden identity
manifest, diagnostics, JSON/HTML reports, exceptions, logs, and retained
generation metadata must not contain raw absolute locators, logical external
locators, statement bytes, record text, merchant/description text, raw
fingerprint inputs, `source_revision`, revision digests, or extractor-profile
content. Ordinary ambiguity warnings are sorted by `(code, source_file)` and
contain no order-dependent candidate listing.

## Hash collision and conflict behavior

Before any merge:

- compare the complete `(source_id, source_record_id)` tuple for every repeated
  new `transaction_id`;
- compare all available normalized locator inputs for repeated
  `source_namespace_id` values in the current batch;
- compare exact source bytes for repeated `source_revision` values in the
  current batch; and
- compare complete allocation inputs for repeated `source_record_id` values.

Equal identifiers with unequal full inputs are `identity_hash_conflict` and a
hard exit-2 error before persistence. They are never treated as duplicates and
never overwrite. Duplicate legacy transaction IDs are the sole non-error
exception: they remain separate protected ambiguous rows until explicitly
resolved.

Full public source, namespace, revision, and record digests minimize collision
risk. Transaction IDs remain 128-bit for compatibility, so the mandatory full
tuple comparison protects the truncated key.

## Consequences

### Positive

- Identical financial rows from distinct sources coexist with IDs independent
  of import batching and discovery order.
- Same basenames in different directories have distinct namespaces without
  exposing absolute paths.
- Single-file and folder invocation use the same locator root.
- Accepted renames and workspace moves preserve logical source and transaction
  IDs.
- Unique record matches preserve corrections across reordering and revision.
- Ambiguous repeated occurrences cannot silently inherit, lose, or exchange a
  correction.
- Legacy rows are never collapsed as an incidental dictionary operation.
- Zero-row sources retain stable identity and already-imported semantics.

### Costs and limitations

- Four public columns require an approved schema migration and documentation
  updates before implementation.
- Resolution requires retaining ledger rows as lists and performing graph
  matching before the existing merge path.
- The hidden identity manifest becomes authoritative persisted state and must join
  every ledger generation and recovery path.
- External source moves with content changes cannot be inferred by replace or
  reset; they require ordinary import as a new source.
- Case-only path variants follow resolved spelling and are not case-folded.
- Truly indistinguishable repeated records may block replacement/reset until
  separate resolution tooling exists.
- Source revision and locator digests are equality fingerprints, not secrecy;
  privacy relies on excluding inputs and hashes from diagnostics and reports
  outside the required ledger, review, and hidden-manifest state.

## Rejected alternatives

### Batch-local occurrence suffixes

Rejected because separate and batch imports produce different IDs, and
insertion/reordering moves corrections.

### `source_file` as source identity

Rejected because it is display provenance, basenames collide, and changing it
would either break renames or expose private paths.

### Public or persisted absolute paths

Rejected because they leak private directory structure and break workspace
moves.

### Content-only source identity or ordinary-import revision fallback

Rejected because byte-identical copies are legitimate distinct sources and
must coexist.

### Include `source_revision` in transaction identity

Rejected because ordinary statement revisions would churn every transaction ID
and orphan corrections.

### Match existing records by page, row, ordinal, or current order

Rejected because mutable position can assign a corrected old occurrence to a
different new occurrence. Position is allowed only when allocating a proven-new
record.

### Greedy, lexical, or correction-aware tie-breaking

Rejected because any selected winner would be a silent guess. Only a unique
maximum bipartite assignment is accepted.

### Quote a warning and continue replacement/reset

Rejected because persistence would already have removed or reassigned financial
and correction state. Targeted ambiguity is a pre-persistence failure.

### Ledger rows as the only authoritative source state

Rejected because a correctly processed empty source has no ledger row, so its
identity, already-imported state, accepted rename, retired record ownership,
and later recurrence would disappear. The authoritative hidden identity
manifest is required and participates in recoverable persistence. It stores
IDs, hashes, non-sensitive numeric allocation locators, and active/retired
ownership, but no source or transaction contents.

## Verification matrix

| Acceptance case | Setup/action | Required observation |
|---|---|---|
| Separate versus batch identical rows | Import two identical financial rows from two distinct locators separately and in one directory | Both modes retain two rows; the per-source transaction-ID sets are identical across modes |
| Same basename, distinct directories | Import `a/may.csv` then `b/may.csv` as single files | Both coexist; namespaces and source IDs differ; no absolute path is emitted |
| Single-file versus folder invocation | Import the same workspace-relative file through each invocation form | Namespace, source ID, record ID, and transaction ID are unchanged |
| Workspace move | Move config root and relative sources together, then replace | Relative namespace and all logical IDs remain unchanged |
| Explicit unchanged rename | Rename a source and invoke replace/reset with unchanged bytes | Unique revision fallback reuses source ID, advances namespace, and preserves matched transaction IDs |
| Ordinary byte-identical copy | Import a second locator with identical bytes without replace/reset | A new source is allocated and both sources coexist |
| Explicit target not found | Use replace/reset for a changed namespace with no equal revision | Exit 2 with `identity_source_target_not_found`; no artifact changes; ordinary import is required |
| Simultaneous external move plus revision | Move an external source and change bytes | Replace/reset fails target-not-found; ordinary import creates a distinct new source |
| Row reordering | Reorder uniquely fingerprinted rows in one source revision | Unique fingerprint matches preserve record and transaction IDs |
| Changed-revision repeated fingerprints | Insert, remove, or reorder equal repeated fingerprints in a changed revision | Candidate group remains complete; multiple maximum assignment fails before persistence regardless of neighbors |
| New identical source inserted before existing sources | Ordinary-import the new source, then replace the resolved existing-source batch | Existing source IDs and corrections do not move; the new source keeps independent IDs |
| Exact-origin older-revision recurrence | Replace away an occurrence, retain its correction, then restore older bytes with the same extractor and allocation origin | Exact source revision, extractor, locator, fingerprint, and ordinal reactivate its retained IDs and surviving correction without graph matching |
| Same-fingerprint future charge | Retire a corrected record, then import a changed revision containing one equal fingerprint at a different allocation origin | The retired record is not a graph candidate; the new record gets a new ID and inherits no orphan correction |
| Exact unchanged repeated revision | Reprocess unchanged current bytes and extractor contract containing identical repeats | Current-locator/fingerprint manifest mapping reuses every retained ID directly; no token recomputation or graph matching occurs |
| Different-revision new occurrence | Remove then add a same-fingerprint occurrence in different source bytes without a unique old match | A new record token is allocated and no retired correction is inherited |
| CSV multiline locator | Recur an exact CSV revision containing quoted multiline records | Starting physical-row tuples and tokens recur exactly |
| PDF multiple-table locator | Recur an exact PDF-table revision with equal rows in multiple tables on one page | Page/table/row/subrow tuples distinguish records and tokens recur |
| PDF split-subrow locator | Recur an exact PDF-table revision where one row splits into multiple transactions | Preserved subrow tuples produce distinct stable tokens |
| PDF word and section locators | Recur exact word and sectioned revisions after filtering/continuation processing | Original page/physical-line tuples and tokens recur |
| Duplicate allocation locator | Make an adapter emit the same tagged tuple twice | `identity_allocation_locator_invalid`; no persistence |
| Pristine first import | Start with no ledger or manifest after retained recovery, with or without setup-created corrections | Empty in-memory identity state is accepted and the first ledger/manifest generation publishes together |
| Non-colliding legacy row | Replace a legacy source with one unique source/record match | Existing transaction ID and correction survive; four v2 fields are populated |
| Unique legacy source claim | Start with an empty bootstrapped manifest and one legacy display group matching one incoming replace/reset source | The one-to-one legacy claim establishes the v2 source target before record migration; target-not-found does not fire |
| Duplicate legacy transaction IDs | Load repeated legacy rows sharing an ID | Rows remain separate; shared correction is not newly applied; protected ambiguity forces review |
| Ambiguous legacy ordinary import | Import an unrelated genuine source while ambiguous legacy rows exist | New source is added, old rows retained, deterministic partial-success diagnostics emitted |
| Unrelated unresolved legacy row | Ordinary-import another source while an all-empty legacy row is not targeted | Legacy row passes through unchanged without ambiguity flag or warning |
| Partial v2 row | Load a row with one to three populated v2 fields | `identity_partial_v2_metadata`; hard failure before persistence |
| Ambiguous legacy replace | Target an ambiguous legacy source with `--replace` | Exit 2; ledger, review, corrections, report, and generation state do not change |
| Ambiguous legacy reset | Target an ambiguous legacy source with `--reset` | Exit 2; ledger, review, corrections, report, and generation state do not change |
| First import is empty | Ordinary-import a valid source with zero records | Empty ledger plus manifest entry publish successfully; repeat ordinary import is already-imported |
| Empty replacement | Replace a non-empty source with a valid zero-record revision | Rows retire while manifest keeps source ID/current namespace/revision/contract |
| Revision growth then unchanged current state | Import revision 1 containing A, replace with revision 2 containing A+B, then replace and reset unchanged revision 2 | A and B retain their manifest-owned IDs; replace is an identity no-op; reset clears corrections for all active and retired ownership |
| Migrated legacy unchanged replace | Uniquely migrate a pre-v2 row, then replace with unchanged current bytes | The preserved legacy transaction ID and allocation origin are reused; no v2 transaction ID is substituted |
| Correct, empty, reset, exact recurrence | Correct A, replace with an empty revision, reset while empty, then restore the older source with A's exact allocation origin | A's retained IDs recur through exact-origin reactivation, but its correction is absent because reset cleared retired ownership |
| Partial retirement reset | Correct A and B, replace with a revision containing only A, then reset | Corrections owned by active A and retired B are both removed; the B tombstone remains |
| Rename, empty, recurrence | Accept unchanged rename, replace with empty revision, then restore prior bytes at their exact allocation origins | Manifest preserves source ID; exact-origin recurrence reactivates old record IDs |
| Legacy manifest bootstrap | Start with an exact pre-v2 header, with legacy rows or empty, and no manifest | Rows remain all-empty identity state and an empty manifest publishes; ownership is added only by unique targeted migration |
| Deferred legacy source claim | Bootstrap legacy rows, ordinary-import an unrelated v2 source, then uniquely replace/reset the legacy display group | The published manifest does not block the one-to-one legacy claim; ownership is added and migration proceeds before v2 target resolution |
| Missing non-empty v2 manifest | Start with populated v2 identity metadata and no manifest | `identity_manifest_missing`; no reconstruction and no artifact changes |
| Missing empty v2 manifest | Start with an empty ledger carrying the v2 header and no manifest | `identity_manifest_missing`; empty v2 state is not bootstrapped |
| Manifest validation | Corrupt schema, regex, uniqueness, or row agreement | Stable manifest error; no public or hidden artifact changes |
| Manifest persistence recovery | Inject failures before and after ledger commit | Manifest rolls back or completes with the same generation as ledger/corrections |
| Manifest privacy | Inspect manifest and diagnostics for workspace and external sources | Manifest contains only fixed IDs/hashes; diagnostics contain no manifest hashes or raw locators |
| Every ledger writer carries manifest | Exercise ordinary import, replace, reset, interactive import/review, structured correct, one-shot review, reconcile, migration, and recovery | Each successful ledger generation includes the same validated manifest generation; mutable-only paths preserve ownership |
| Extractor canonicalization golden | Canonicalize profile values containing NFC-sensitive Unicode, numbers, booleans, null, arrays, and nested objects | Exact canonical bytes and `ext_` digest match reviewed literals |
| Included profile mutation | Change any non-excluded top-level/nested extraction value or adapter version | Extractor contract changes |
| Excluded profile mutation | Change each closed excluded top-level presentation/classification value | Extractor contract does not change |
| Public header order | Emit ledger and review artifacts | Headers exactly match the two ordered contracts in this ADR |
| ID and all-or-none validation | Exercise each valid/invalid prefix, digest length, character case, and four-field population combination | Only exact regexes and all-empty/all-populated combinations pass |
| Spreadsheet-safe ID round-trip | Write and read every new ID field through the public CSV codec repeatedly | Bytes and canonical values remain exact with no safety prefix or accumulated escape |
| Mutable-field changes | Change category, owner, payment method, confidence, reason, notes, review, flow, or reconciliation fields | Transaction identity remains unchanged |
| Hash conflict | Inject equal stored IDs with unequal full identity inputs | Stable `identity_hash_conflict`; hard failure before merge/persistence |
| Privacy diagnostics | Exercise every ambiguity code with an external synthetic path | Only stable code, display label, action, and counts appear; no absolute locator, revision hash, bytes, or record text appears |
| Existing compatibility suite | Run CLI, workflow, import-profile, and agent tests | Existing non-identity behavior remains green |

Required verification commands after implementation:

```text
python3 -m unittest tests.test_cli_bootstrap tests.test_workflow \
  tests.test_import_profiles tests.test_agent_cli
./scripts/check.sh
```
