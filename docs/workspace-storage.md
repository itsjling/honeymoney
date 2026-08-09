# Workspace storage

ADR 0008 defines the binding storage rules. This page gives the working layout.

```text
workspace/
  config.json
  corrections.csv
  profile_mappings.json
  rates.json
  rules.json
  profiles/
  .honeymoney/
    workspace-index.json
    import-records/
      <source-id>/
        summary.json
        transactions.csv
        attempts/
          00000001.json
    report-preview.html       # disposable range or all-period report
  views/
    YYYY-MM/
      transactions.csv
      review_needed.csv
      report.html
    undated/
      transactions.csv
      review_needed.csv
      report.html
```

The exact names and schemas in code must follow ADR 0008. Setup creates config,
starter user inputs, `.honeymoney/workspace-index.json`, and
`.honeymoney/import-records/`. It
does not create `views/`, a statement input folder, import records, a journal,
or a lock.

User-owned inputs and corrections are durable authority. Each import record
owns one source lifecycle and its latest successful normalized facts. The
value-free index owns workspace identity. Views are replaceable output.

Honeymoney accepts only paths contained by the resolved workspace root for
managed state. It rejects symbolic links and unsafe path components. Unknown
files remain untouched and produce a doctor warning. Financial files and
managed directories allow owner access only.

Never edit `.honeymoney/`. Edit user-owned inputs only while no command runs,
then run `honeymoney views rebuild --all`. A narrow command will fail with
`full_rebuild_required` when its stored input proofs no longer match.

One command owns the workspace lock from journal creation through attempt
finalization and cleanup. If a journal remains, run `honeymoney doctor`, then
`honeymoney doctor --fix`. Do not delete the journal or lock by hand.
