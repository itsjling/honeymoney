# Architecture

Honeymoney is a local-first Python CLI. The filesystem is its integration
boundary: statements and configuration go in, while CSV, JSON, and HTML
artifacts come out. There is no database or cloud AI service. The dedicated
`rates fetch` command may cross one fixed public-data boundary after user
consent; no other command uses it.

## Data flow

```text
config + profiles + statement files + local official-rate cache
                 |
                 v
profile detection and CSV/PDF parsing
                 |
                 v
normalized source occurrences + identity resolution against the source manifest
                 |
                 v
exact-overlap multiset canonicalization
                 |
                 v
deterministic rules -> opt-in local memory -> structural classification -> optional local Ollama
                 |
                 v
validated corrections
                 |
                 v
deterministic flow treatment + cumulative-ledger transfer reconciliation
                 |
                 v
categorized.csv + review_needed.csv + import_report.json
+ hidden source occurrences + identity manifest + overlap manifest
                 |
                 v
status summaries and self-contained HTML reports
```

The optional network path sits before the local rate cache:

```text
explicit rates fetch + public currencies + date range
                 |
                 v
fixed HKMA HTTPS endpoint -> checked complete pages -> rates.json
```

The fetch path receives no ledger rows. Only after every response page passes
the local provider checks does the CLI open identity state and apply the shared
cache-and-ledger generation.

Imports merge source occurrences by source `transaction_id`. Exact same-account
occurrences from distinct sources then form canonical multiset slots. The
public ledger uses stable canonical transaction IDs and never invents pairings
between repeated equal source rows. The identity
resolver uses the hidden manifest, not `source_file`, to find sources for
replacement and reset. `source_file` is display provenance only. Corrections
are persistent overrides keyed by `transaction_id`; rules and Ollama
suggestions run before corrections, so reviewed choices win.
`honeymoney learn` can turn active exact corrections into managed deterministic
rules. It writes only after `--yes`, replaces only its own managed rules, and
never learns owner or payment method. Broad rules require full agreement for an
exact institution, account, normalized description, and direction. Conflicting
groups may split only by exact posted amount and currency.

## Filesystem persistence

`categorized.csv` is the authoritative canonical cumulative ledger.
`.honeymoney-source-occurrences.csv` holds active source evidence and retained
evidence for retired identity records. The identity manifest marks each record
as active or retired. Only active rows enter canonicalization and statement
balance checks.
`.honeymoney-overlap-manifest.json` holds the hidden workspace namespace and
canonical slot tombstones. Its versioned membership records hold keyed
membership digests, membership-bound review group IDs, and explicit duplicate
resolutions without source IDs or transaction values.
`.honeymoney-identity-manifest.json` still owns source and record identity.
`review_needed.csv` is regenerated from the public ledger whenever it changes,
while
`import_report.json` records the last import attempt and is replaced with its
import generation. Corrections and remembered rules remain independent inputs,
but operations that change them and the ledger publish them through the same
recoverable persistence boundary.
The versioned `rates.json` cache is also an input. A config that omits
`rate_cache` resolves it to `rates.json` beside the active config, inside the
identity workspace root. An explicit cache path wins. This default exists only
in loaded config state and never rewrites the user's config. A local HKMA
document import checks and normalizes every observation before it publishes
the cache and revalued ledger through that boundary.
Before the first ledger exists, a rate-only import uses the future ledger path
as its coordination lock. Rate-cache and ledger recovery share that lock.

Each operation writes and flushes complete staged files and prior-file backups
before replacing any public path. Non-ledger artifacts are replaced first and
`categorized.csv` last; that final ledger replacement is the generation commit
point. The containing directories are then synchronized. This ordering is a
recovery protocol, not a claim that several filesystem replacements are atomic.

Fresh setup writes `.honeymoney-managed-files.json` with the installed
HoneyMoney version, bundle origin, workspace-relative path, and SHA-256 digest
for each shipped profile. `setup --upgrade` manages only those profiles. It
updates a file only when the record proves its origin and its current digest
still matches the recorded digest. Missing records, local changes, symbolic
links, and unsafe paths fail closed. Configured input and output paths and all
user-owned state remain outside the write set. Evidence from a newer
HoneyMoney release also fails closed to prevent a downgrade.

An upgrade uses the managed-file record as its generation commit point. It
stages complete profile files and prior-file backups, then publishes the record
last. A pre-commit failure restores the old files. If an interruption happens
after the record commit, the next upgrade completes that generation before it
builds a new plan.

Hidden generation state beside `categorized.csv` contains only paths, modes,
and content digests. If a write fails before the ledger commit point, the old
files are restored. If interruption occurs after it, the next command that
loads the active workspace configuration completes the new generation before
continuing. Recovery removes public files that were absent in the prior
generation. New financial files use owner-only read and write access. Existing
files keep their owner access while losing all group and other access.
Diagnostics do not include transaction values. Retained state also prevents a
new operation from silently proceeding when recovery cannot be completed.

Replacement removes obsolete source occurrences, then recomputes canonical
membership. Reset removes a canonical correction only when every source that
supports its overlap group belongs to the processed reset batch. A remaining
source keeps the canonical decision. The filtered correction
document is held in memory during categorization and is published in the same
generation as the replacement ledger. Failed and skipped sources therefore
retain their rows and corrections; a persistence failure restores both inputs
to the prior generation. Import reports record the requested action and the
ledger action actually committed for each source.

Before replacement or reset changes a workspace with an older review or
canonical schema, the import path derives typed review reasons from the prior
rows in memory. This keeps pending model suggestions and resolved manual choices
attached to their proven canonical IDs. When a parser change rekeys a source,
the replacement path projects that state through the same unique source-row
proof used for corrections. A successful migration and replacement publish as
one generation. A failed replacement leaves the prior financial generation
unchanged, and its report describes that preserved generation. Running
`reconcile` as a separate upgrade step remains safe but is optional.

Replacement, migration, and reconciliation derive `source_data_issue` from
the active source pool after corrections run. The canonical row keeps only
source-data flags that active parser, balance, or provenance evidence supports.
This also repairs stale correction review fields before publication.
`source-data inspect` uses the same checked provenance join for any valuation
state and returns only bounded source context. `source-data resolve` changes
only the named row on a current schema and writes only when that join finds no
active support. If the write also upgrades stored rows, it repairs the whole
migrating generation. It publishes the ledger, corrections, review queue,
source evidence, and identity files as one recoverable generation.

`honeymoney duplicates` reads unresolved count-mismatch groups from the overlap
module. `duplicates resolve` validates the current membership-bound group ID,
then records `same-event` or `keep-all`. Same-event retains the second-largest
per-source count; keep-all retains the maximum. A membership change ignores the
old resolution, restores duplicate review, and emits a value-free warning.
Resolution regenerates the canonical ledger, review queue, transfers, and
reconciliation, uses active source evidence for statement balances, and
repairs source-data state after the overlap decision. It publishes changed
corrections in the same generation and does not rewrite the last import report.

The current import report describes the latest attempted import even when a
source fails, while the authoritative ledger, its derived review rows, and saved
corrections remain on the prior financial generation. Ollama is an optional
post-parse categorizer: its unavailability leaves parsed rows pending review and
does not turn a successfully processed statement into a failed reset.
An invalid response triggers one follow-up request for only missing or invalid
rows. Valid results stay fixed. Raw model confidence does not clear review
unless the config names a locally calibrated acceptance threshold. Import
reports split category provenance into deterministic, memory, exact-correction,
accepted-model, reviewable-model, and unresolved counts.

`needs_review` is derived from the stable tokens in `review_reasons`; it cannot
remain true with no current human decision. The free-text `reason` field keeps
categorization and processing provenance. HKD valuation completeness stays
separate from transaction review. `valuation_source` and `valuation_status`
distinguish statement amounts, matched exchange legs, dated or fixed rate
estimates, imported HKMA reference estimates, and missing values.
`valuation_rate_date` and `valuation_provider` keep the rate evidence on each
valued row. See
[`ADR 0004`](adr/0004-review-state-and-hkd-valuation.md).

`category` is the merchant/budget classification. `flow_type` is the accounting
treatment used by cash-flow totals. Ollama is limited to configured spending
categories and cannot set an owner or protected accounting treatment. Protected
categories are established only by rules, corrections, conservative structural
classification, or reconciliation. After rules, local Ollama, and corrections,
the cumulative ledger is reconciled across owned accounts. Unique opposite-sign,
equal-base-currency candidates within the configured date window receive stable
transfer links derived from their existing transaction IDs. Ambiguous candidates
are never auto-paired. Reports derive old ledgers in memory, and `reconcile`
provides an explicit inspect/rewrite seam.

Manual same-account cash pairs use a shared `manual_pair_id` in both correction
rows. Correction projection carries that ID only through proven identity
mapping. Each reconciliation rebuilds the pair from its two current members and
checks account, posted currency, amount, sign, and owner. Missing, extra, or
changed members fail closed as unresolved accounting review. Valid manual pairs
run before automatic matching and keep `flow_source=correction`. A later
non-transfer review clears both pair markers atomically and returns the other
member to review. Pair replay maps a retired nominated source ID only through a
single active canonical slot with the same checked identity fingerprint. Both
mapped rows must have reciprocal links and make up the full ledger and
correction membership for one pair. An exact replay returns `changed=false`
before any generation write.

PDF profiles may map statement opening and closing balance lines. The importer
scans raw word or table lines, then puts each balance on the first or last
transaction for the mapped account, statement section, and posted currency. It
never turns a balance line into a transaction. Statement-balance reconciliation
groups hidden source occurrences by source identity, account, statement
section, and posted currency. It uses
`source_file` only for legacy rows that
lack `source_id`. The existing `status` field stays `reconciled`, `difference`,
or `unavailable`. Each statement result also says whether safe opening and
closing evidence was found. Its outcome is `missing_opening`,
`missing_closing`, `missing_both`, `conflicting_evidence`, `matched`, or
`mismatched`. If both endpoints are safe but posted currency or activity input
is unusable, its outcome remains `unavailable` and its reason names that input.
Partial, conflicting, and unavailable results contain no calculated values.
Conflicting balance values add a safe row flag. Conflict output names the
source, page, section, and field but omits the values. Missing evidence alone
does not mark a transaction for source-data review.

Canonical cash-flow and report totals use the public ledger. The maximum
per-source multiplicity sets the canonical count for each exact overlap group.
Equal counts consolidate. Different counts keep that maximum, stay pooled, and
force review. JSON keeps the old duplicate count fields as derived compatibility
data, while `overlap` is the full provenance contract.
Direction uses the base-currency amount when present. If conversion is missing,
it may use a valid non-zero posted amount for direction only. Transfer matching
and report totals still require base-currency amounts. Reports split missing
values by cash-flow impact without transaction text.

Official HKMA daily observations use HKD per unit of foreign currency. A local
import stores the raw observation and a resolution for each requested
transaction date. Resolution uses the exact date or the latest prior date
within seven calendar days. It never uses a future observation. Valuation order
is statement-posted value, matched exchange leg, configured exact-date rate,
HKMA cache, configured fixed rate, then missing. See
[`ADR 0005`](adr/0005-local-hkma-rate-cache.md).
The provider fields already use one foreign-currency unit, including JPY, KRW,
and IDR. Normal statement imports add new requested-date resolutions when the
cache already holds a matching observation.

Valuation summaries group missing values by accounting impact. Missing income,
expense, and refund values block confirmed cash-flow totals unless the posted
amount is exactly zero. Missing transfer, card-payment, and investment-transfer
values do not block those totals. Missing unresolved flows stay separate.
Summaries also split income, spending, refunds, and net cash flow into actual,
estimated, and combined-estimate HKD values. Source counts keep each estimate
kind distinct. See
[`ADR 0006`](adr/0006-valuation-impact-reporting.md).

## Transaction identity

Identity v2 gives each hidden source-occurrence row four fields: `source_id`,
`source_namespace_id`, `source_revision`, and `source_record_id`. A v2 source
row has all four fields. Partial metadata fails validation. New source
transaction IDs use a
128-bit, domain-separated digest of the source and record IDs. Source IDs use
the `src_`, `ns_`, `rev_`, and `rec_` prefixes plus full SHA-256 digests; new
transaction IDs use `txn_` plus 32 lowercase hexadecimal characters.
`source_file`, source page, and source row remain source display fields and
never form identity or replacement keys.

Public canonical rows add `canonical_group_id`, `canonical_slot`,
`provenance_status`, and `source_occurrence_count` after `transaction_id`.
Their source identity, source display, and statement-balance fields stay empty.
Canonical IDs come from a hidden random workspace namespace, the normalized
record fingerprint, and the abstract slot number.

Missing-valuation inspection emits a workspace-relative source path only after
the path produces the stored source namespace. A matching file name alone is
not proof. Evidence keeps one item per active source occurrence, including
equal display, page, and row values.

The shared active-provenance index also supports source-data review. It checks
canonical group membership and source-occurrence counts before either
inspection path returns evidence. Source-data evidence maps each supported flag
to a parser, balance, or provenance conflict and gives its safe source, page,
statement section, and field without returning statement values. A valid
unresolved overlap count or history conflict remains loadable and appears as
typed provenance evidence; malformed persisted identity state still fails
closed.

The resolver runs for the whole input batch before categorization, correction
application, reconciliation, or any transaction-ID dictionary. It resolves a
logical source from its normalized workspace-safe locator and exact source
bytes. An ordinary import creates a new source when its namespace is new. A
replace or reset reuses a source only through one exact namespace match, or one
unclaimed equal-revision match for an accepted rename. It never guesses from a
file name, directory order, or an ambiguous match.

Within each source, the resolver matches records only on the accepted
fingerprint and manifest ownership. An unchanged source uses its exact stored
locator mapping. A changed source can reuse records only when there is one
maximum matching; otherwise it stops with an identity ambiguity error. New
records receive a stable allocation origin from immutable parser locators.
Retired records keep their ownership, so they cannot pass a correction to a
later similar transaction. Legacy IDs survive only when migration proves one
owner; shared legacy IDs stay unowned and require review. The full contract is
in [`ADR 0001`](adr/0001-stable-transaction-identity.md).

The one-time move from an identity-v2 ledger to the canonical overlap ledger
binds proven review history to canonical slots before a replacement or reset.
Exact locator and fingerprint matches keep their source owners, followed by
unique fingerprint matches. Repeated unmatched records retire and receive new
source owners as a pool, without pairing old and new occurrences. Compatible
corrections and review history stay on the canonical slots.
When a parser repair changes account or currency identity during this move,
the migration joins old and new source rows only on one unique normalized
source-local record shape. A repeated group moves only when every old row has
the same correction. Conflicts stay in review. The published correction file
keeps final active canonical IDs and drops retired source aliases.

## Persistence authority and recovery

`categorized.csv` is the authoritative canonical ledger. `review_needed.csv` is a
deterministic view of its rows whose `needs_review` value is `true`.
`import_report.json` describes the latest attempted import, including an attempt
that failed while the prior ledger stayed in place. Review and correction
commands do not rewrite that import record. `corrections.csv` remains durable
input for applying reviewed choices to future imports, but it is not a second
ledger.

The hidden `<categorized.csv parent>/.honeymoney-identity-manifest.json` is the
authoritative source and record ownership store. It records IDs, hashes,
allocation locators, and active or retired state, but never source paths,
statement text, or display values. Hidden source rows and the identity manifest
must agree. Retired hidden rows stay out of the public ledger. Canonical rows
and the overlap manifest must agree.
The first import writes both, including for a zero-record source. A missing
manifest can bootstrap only an exact pre-v2 ledger header. An exact issue #31
ledger keeps its old IDs for read-only commands; its first write publishes both
new hidden files and the canonical public schema in one generation. Any partial
canonical state fails closed.

The manifest joins every recoverable ledger generation, including import,
replace, reset, correction, review, reconcile, and recovery. A change that
only updates mutable ledger fields carries validated ownership forward without
changing it.

## Source map

- `honeymoney/cli.py`: command routing, workspace setup, identity resolution,
  duplicate review, ledger generation, categorization, review filtering, and
  JSON output.
- `honeymoney/importers.py`: input discovery, profile validation and selection,
  CSV/PDF parsing, parser locators, and private source identity inputs.
- `honeymoney/normalization.py`: pure row/date/amount/text normalization and
  compatibility helpers.
- `honeymoney/overlap.py`: canonical multiset slots, membership-bound duplicate
  review, resolution safety, manifest validation, and privacy-safe evidence.
- `honeymoney/contracts.py`, `honeymoney/identity_contracts.py`,
  `honeymoney/overlap_contracts.py`, and `honeymoney/parser_contracts.py`:
  checked static contracts shared by the financial core, identity and overlap
  boundaries, and statement parsers.
- `honeymoney/duplicates.py`: pure identity-backed duplicate evaluation,
  idempotent candidate annotation, and privacy-safe diagnostics.
- `honeymoney/identity.py`: identity-v2 digests, validation, source and record
  resolution, manifest ownership, and safe identity diagnostics.
- `honeymoney/identity_state.py`: ledger and manifest loading, bootstrap rules,
  cross-file validation, and manifest path handling.
- `honeymoney/corrections.py`: correction validation, merge-by-transaction-ID,
  cumulative reconciliation, and correction/ledger/review/rule generation content.
- `honeymoney/csv_artifacts.py`: reversible spreadsheet-safe serialization and
  canonical read-back for public CSV text cells; see
  [CSV compatibility](csv-compatibility.md).
- `honeymoney/persistence.py`: staged filesystem generation commits, authoritative
  ledger replacement, directory synchronization, and retained-state recovery.
- `honeymoney/workspace_upgrade.py`: bundled-profile ownership evidence,
  privacy-safe upgrade plans, protected-path checks, and recoverable profile
  publication.
- `honeymoney/rules.py`: deterministic rule validation and application.
- `honeymoney/learning.py`: conservative managed-rule planning from active exact
  human corrections.
- `honeymoney/categorization_memory.py`: opt-in, correction-derived local
  spending-category matches rebuilt from validated identity state.
- `honeymoney/ollama.py`: optional local-only categorization fallback. Its
  shared model-listing and generation transport accepts only `http` endpoints
  that resolve exclusively to loopback addresses, pins the connection to a
  validated numeric address, bypasses proxies, and revalidates redirects before
  following them.
- `honeymoney/schema.py`: public ledger/review columns and allowed values.
- `honeymoney/report.py`: offline HTML report generation.
- `honeymoney/rate_fetch.py`: fixed HKMA HTTPS request construction, explicit
  public query allowlist, response limits, and complete-page collection.
- `honeymoney/rates.py`: official HKMA document checks, versioned cache, and
  safe prior-date resolution.
- `honeymoney/reconciliation.py`: deterministic flow derivation, transfer pairing,
  and optional statement balance checks.
- `honeymoney/review_state.py`: review-reason tokens, plain labels, and the
  boolean invariant.
- `honeymoney/valuation.py`: HKD value source, configured and cached rates, and
  completeness counts.
- `honeymoney/valuation_inspection.py`: read-only canonical-to-active-source
  joins for missing valuation diagnosis.
- `honeymoney/data/profiles/`: bundled institution profiles copied by setup.
- `tests/fixtures/`: synthetic golden inputs and expected behavior.

## Public boundaries

Treat CLI text behavior, the versioned JSON envelope, exit codes, configuration
fields, corrections, bundled profiles, and output columns as compatibility
contracts. JSON commands emit one document on stdout; progress belongs on
stderr. Exit `0` means success, `1` means strict partial success, and `2` means
usage, configuration, or validation failure.

`pending` exposes review rows. `correct` remains the structured machine/agent
seam. `review` is the human seam: period/category/flow/direction filters feed
interactive accounting decisions, while `--transaction ID --as DECISION` is a
fully specified one-shot form and `--file FILE` accepts a decision-only CSV or
JSON batch. Batch review checks all entries and current review state before it
calls the correction operation. Each form merges saved corrections by
transaction ID, reconciles the cumulative ledger, and replaces all derived
files through temporary files. JSON review is accepted only for non-prompting
one-shot and batch forms.

Remembered income rules are deterministic exact matches on institution,
account identity, normalized description, and the virtual inflow direction.
Direction uses `amount_hkd` when present, then the posted amount. It is not part
of transaction identity.
Human corrections, deterministic rules, and conservative structural matching
may establish protected flows; reconciliation may establish owned-account
transfers. Refunds and owned-account flows remain distinct, and Ollama cannot
set flow treatment.

## Privacy boundary

Only synthetic fixtures may enter git or cloud Codex. Real statement files,
local workspaces, generated outputs, and live Ollama transcripts stay local.
Ollama is disabled by default and is never part of CI.
The only public network exception is `rates fetch`. It sends currency codes,
dates, and page controls to the fixed HKMA endpoint after consent. It does not
load ledger rows before the response has passed all page checks. Ordinary
commands remain offline. The full boundary is in
[`network-boundary.md`](network-boundary.md).

## Quality gates

The offline project check runs strict mypy over a named financial-core scope
and measures branch coverage over the default synthetic unittest suite.
Coverage includes child CLI processes and fails below the reviewed threshold in
`pyproject.toml`. The checked scope, excluded modules, expansion rule, baseline,
and critical-path test map are in [`quality-gates.md`](quality-gates.md).
