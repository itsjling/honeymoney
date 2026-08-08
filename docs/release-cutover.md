# Release, cutover, and rollback

## Breaking changes

Version 0.2.0 uses `honeymoney import PATH` and removes `run`, `--input`, and
ledger-path `--output`. The config directory is the workspace root, and new
configs have no `paths` section. Durable state moves from `categorized.csv`
and separate hidden manifests into import records and the value-free workspace
index. Generated output moves to month and undated units under `views/`.

JSON schema version 3 replaces ledger terms with `import_count`,
`statement_transaction_count`, `view_transaction_count`, `import_records`,
`views`, and `report_html`. It does not accept or emit schema version 2 forms.
Period-aware commands now share month, range, undated, and all selectors.
There is no migration or old-workspace reader.

## Release 0.2.0

Build and test with Python 3.14.6. The package accepts Python versions from
3.14 through, but not including, 3.15. Run the full offline gate, then install
both the wheel and source archive into separate fresh environments. In each,
run setup, check the exact empty layout and access modes, import one synthetic
source, inspect it with `imports`, build its month view, run `status --json`,
and run doctor.

Create an annotated `v0.2.0` tag. Publish one GitHub Release with the checked
wheel, source archive, and SHA-256 checksum file. Do not publish to PyPI. Do not
move a published tag or replace an asset. Use a patch release for a later fix.

## Cutover

1. Stop the old package and keep its full workspace idle.
2. Copy that package and workspace to a dated, read-only archive.
3. Install 0.2.0 without changing the archive.
4. Create a fresh workspace with `honeymoney setup`.
5. Review and copy only user-owned config, profiles, rules, rates, and account
   mappings that pass the new schemas.
6. Reimport each original statement locally.
7. Recreate saved corrections against the new view-transaction IDs.
8. Run `views rebuild --all`, the required period checks, and doctor.

Normal pending review does not block cutover. Stop if source or transaction
identity changes across a supported retry, counts disagree with the reviewed
source set, publication remains unsettled, generated views differ from a clean
full rebuild, or doctor reports a fault.

## Rollback

Stop 0.2.0 and leave its workspace unchanged for diagnosis. Restore the old
package and its matching complete archived workspace together. Never run the
old package against the 0.2.0 workspace or the new package against the archive.
Do not delete the archive as part of cutover.
