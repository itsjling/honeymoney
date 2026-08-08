# CLI module boundaries

The public CLI is the main integration seam. Command parsing and presentation
compose typed services; they do not define file schemas or financial rules.

Import discovery reads only the explicit `PATH`, selects checked profiles and
account bindings, and gives normalized statement transactions to import-record
storage. Parsers own immutable row locators. Normalization stays pure and does
no path or file work.

Import-record storage owns source packages, attempt allocation and reports,
current successful snapshots, readiness, and disposable summaries. Workspace-
index storage owns value-free source, statement-transaction, view-transaction,
overlap, duplicate-decision, registered-view, generation, and proof state.

Identity owns source and statement-transaction resolution. Overlap owns stable
view-transaction groups and slots. Neither accepts display paths as proof.

View derivation reads every ready import record plus checked user inputs. It
resolves the full workspace before period partitioning. CSV and report
serializers own byte-exact output. They do not read the filesystem.

Publication owns the exclusive lock, journal, staging, file and directory
sync, index-last commit, attempt finalization, and cleanup. No other module may
publish a managed file.

Doctor owns the full read-only audit and proof-based repair plan. Its repair
path may call publication but never parsing, categorization, Ollama, valuation,
reconciliation, or report logic while settling a journal.

Corrections, rules, local memory, Ollama, rates, valuation, duplicate and
source-data repair, review state, reconciliation, and reports remain separate
domain modules. The HKMA fetch module keeps the sole public network boundary
and has no import-record or financial-row API.
