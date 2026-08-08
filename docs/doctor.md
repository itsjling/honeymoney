# Workspace doctor

`honeymoney doctor` checks the whole workspace and writes nothing. It does not
open original statements, call Ollama, or use the network.

The audit checks, in order:

1. workspace root and managed paths;
2. symbolic links, unsafe paths, file types, and owner access;
3. lock and publication journal state;
4. workspace-index schema, generation, identity, and input proofs;
5. import records, current snapshots, and complete attempt sequences;
6. saved corrections and agreement between durable authorities;
7. registered view layout and deterministic contents; and
8. disposable summaries and unknown managed entries.

Findings use stable codes. Details contain bounded workspace-relative paths,
IDs, and counts only. They contain no amounts, descriptions, raw source paths,
normalized rows, correction values, or reusable financial digests. Exit 0
means healthy, exit 2 means any warning or error still needs action, and exit 1
means an unexpected program or operating-system failure blocked diagnosis.

`honeymoney doctor --fix` builds the full repair plan before it writes. It
refuses every repair when import records, attempt history, stable identity,
saved corrections, or durable authorities have hard damage. Restore a complete
backup in that case.

With exact proof, `--fix` may:

- settle a stopped publication to the proved old or new generation;
- finalize its unfinished attempts;
- rebuild disposable summaries and generated views;
- create or fix managed directories and owner-only access; and
- remove a stale lock after proving that its owner stopped.

It preserves unknown files and never follows a symbolic link. It publishes one
repair generation, removes recovery state only after all reports and cleanup
finish, and runs the audit again.

Stable finding codes include `workspace_busy`, `lock_owner_unknown`,
`stale_lock`, `publication_recovery_required`, `publication_state_invalid`,
`full_rebuild_required`, `workspace_input_invalid`,
`newer_honeymoney_required`, `import_record_invalid`,
`attempt_history_invalid`, `workspace_index_invalid`, `corrections_invalid`,
`durable_state_conflict`, `summary_invalid`, `generated_view_invalid`,
`managed_metadata_invalid`, `managed_path_unsafe`, and
`unknown_managed_entry`.
