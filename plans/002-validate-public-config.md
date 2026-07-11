# Plan 002: Validate all public configuration structures at load time

> **Executor instructions**: Follow this plan exactly, run each verification, and update `plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/cli.py honeymoney/ollama.py honeymoney/schema.py tests/test_agent_cli.py tests/test_cli_bootstrap.py`
> Stop if config loading or the JSON error envelope no longer matches the excerpts below.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

Only `paths` receives structural validation. Malformed public sections can raise `AttributeError` or `TypeError`, bypassing the promised JSON document and exit code 2. All malformed user configuration should fail early with an actionable `ValueError`.

## Current state

`honeymoney/cli.py:1243-1258` validates the root and paths only. `honeymoney/ollama.py:30` calls `.get` on `config["ollama"]`; `honeymoney/cli.py:2295` does the same for `exchange_rates`. `run()` catches only `OSError` and `ValueError`.

Public fields are documented in `README.md:171-192` and `docs/architecture.md:47-53`; do not silently coerce invalid values.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Agent contract tests | `python3 -m unittest tests.test_agent_cli` | all pass |
| CLI tests | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | exit 0 |

## Scope

**In scope**: `honeymoney/cli.py`, `honeymoney/schema.py` if shared validators belong there, `tests/test_agent_cli.py`, `tests/test_cli_bootstrap.py`, and config documentation in `README.md` only if behavior needs clarification.

**Out of scope**: profile-schema validation (Plan 003); dependency changes; broad exception catching; new config formats.

## Git workflow

- Branch: `advisor/002-validate-public-config`
- Commit example: `fix: validate public config structures`.
- Do not push or open a PR unless instructed.

## Steps

1. Add table-driven tests for wrong types in `paths`, `profiles`, `profile_mappings`, `rules`, `corrections`, `pdf`, `ollama`, `exchange_rates`, `categories`, `owners`, and `payment_methods`; include invalid nested scalar types and non-finite/invalid numeric thresholds. For `--json`, assert one JSON object, empty stderr where required, error status, and exit 2.
   **Verify**: `python3 -m unittest tests.test_agent_cli` → new malformed cases fail without tracebacks being normalized.
2. Add a single config-validation boundary called by `_load_config`. It must raise field-specific `ValueError`s, preserve defaults, reject booleans where numbers are expected, and validate Ollama batch/timeout and review threshold ranges without changing valid configuration.
   **Verify**: `python3 -m unittest tests.test_agent_cli tests.test_cli_bootstrap` → all pass.
3. Document any newly explicit constraints using existing config vocabulary.
   **Verify**: `rg -n 'ollama|exchange_rates|review_confidence_threshold' README.md` → descriptions match enforced behavior.

## Test plan

Use subprocess patterns in `tests/test_agent_cli.py`, especially structured-error tests. Include text mode and JSON mode, one valid minimal config, and every malformed public section.

## Done criteria

- [ ] No public config type error can escape as a traceback in tested paths.
- [ ] JSON mode emits one versioned error document and exits 2.
- [ ] Existing valid configs and bundled examples still pass.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- Validation requires rejecting a currently bundled profile/config.
- A public field's intended type cannot be established from code, README, examples, or tests.
- The implementation starts catching broad `Exception` instead of validating inputs.

## Maintenance notes

Every new config field must be added to the validation matrix and structured-error tests. Keep validation independent of optional PDF packages and live Ollama.
