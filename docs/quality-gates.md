# Quality gates

`./scripts/check.sh` runs strict static types and branch-covered tests on
Python 3.11 and 3.13 in CI. Both gates work after bootstrap with network access
disabled.

## Static types

Run:

```bash
python3 -m mypy
```

The named scope in `pyproject.toml` checks normalization, category policy,
duplicate evaluation, overlap planning and decisions, persistence,
identity-state loading, corrections, reconciliation, CSV/schema helpers, and
report assembly and the shared public contracts. It rejects explicit `Any`,
untyped definitions, and unused ignores.

The implementation modules not yet checked are:

- `honeymoney/categorization_memory.py`
- `honeymoney/cli.py`
- `honeymoney/identity.py`
- `honeymoney/importers.py`
- `honeymoney/ollama.py`
- `honeymoney/rules.py`

The checked `identity_contracts.py`, `overlap_contracts.py`, and
`parser_contracts.py` define the boundaries used by identity, overlap, and
parser code. Checked stubs for `identity.py`, `importers.py`, and `rules.py`
keep the exact callable edges visible to mypy while their implementations
remain outside the first scope. Their full internal conversions stay as
bounded follow-up steps because the modules mix parsing, validation, command
policy, and file lifecycle code across a large surface.

The next bounded identity and import conversion will move resolved-path locator
selection and stable source-byte capture into a checked `source_snapshot.py`
module. It will add that module to the strict scope and leave bank-specific CSV
and PDF parsing in `importers.py`.

The first gate does not require complete typing of embedded HTML or every
command-formatting helper. It also does not add runtime schema libraries or
replace the transaction representation. Coverage work must test behavior, not
private implementation details, and it must use only committed synthetic
fixtures.

The next bounded CLI conversion will extract the JSON envelope, command-error
mapping, and rate and source-data result shapes from `cli.py` into a checked
`cli_contracts.py` module. It will add that module to the strict scope and keep
the existing command tests at its boundary. Adding all of `cli.py` in this
feature stack would also require unrelated conversions in the imported
identity, parser, rule, and Ollama implementations.

This is a ratchet:

- never remove a checked file or weaken a rule;
- add each new financial-core module when it is created; and
- add a changed excluded module in the same change, or record the next bounded
  conversion when that change cannot finish it.

## Branch coverage

Run:

```bash
./scripts/check_coverage.sh
```

The command runs the default synthetic unittest suite once. Coverage starts in
child CLI processes, writes parallel data, combines it, and then applies the
`fail_under` value in `pyproject.toml`. The conservative checked floor remains
87%. CI applies it to Python 3.11 and 3.13. Raise it only after reviewing the
lower exact result from both interpreters and rounding down to a whole percent.

The percentage gate supports, but does not replace, critical-path tests:

| Path | Behavioral tests |
|---|---|
| replace and reset | `tests/test_workflow.py` |
| identity and manifest agreement | `tests/test_identity.py`, `tests/test_identity_state.py` |
| persistence rollback and recovery | `tests/test_workflow.py`, `tests/test_identity_state.py` |
| correction validation and atomic output | `tests/test_agent_cli.py` |
| transfer and statement reconciliation | `tests/test_cash_flow.py` |
| profile validation and CSV/PDF parsing | `tests/test_import_profiles.py` |
| duplicate evidence and stable order | `tests/test_duplicates.py`, `tests/test_workflow.py` |
| canonical overlap and manifest agreement | `tests/test_overlap.py`, `tests/test_overlap_state.py` |
| duplicate decisions and recoverable correction updates | `tests/test_workflow.py` |
| offline child CLI execution | `tests/test_offline_test_runner.py` |

Use only committed synthetic fixtures. The gate forbids sockets and non-local
DNS lookup in the test process and child Python processes.
