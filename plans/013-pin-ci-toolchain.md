# Plan 013: Make CI and PDF dependency resolution reproducible

> **Executor instructions**: Keep published runtime requirements appropriately broad while making development/CI resolution repeatable. Update the index when complete.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- pyproject.toml scripts/bootstrap.sh scripts/check.sh .github/workflows/ci.yml README.md`
> Stop if the repository has adopted another lock/constraints workflow.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: migration
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

`pdfplumber` and `PyMuPDF` are unconstrained and bootstrap resolves fresh versions on every machine and CI run. Parser behavior can therefore change for the same commit. End-user package metadata should stay compatible, while CI/dev use a reviewed resolution.

## Current state

`pyproject.toml:9-13` declares unbounded PDF dependencies. `scripts/bootstrap.sh:6` installs editable `.[pdf,dev]`. CI caches only by `pyproject.toml` at `.github/workflows/ci.yml:20-25`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Bootstrap | `./scripts/bootstrap.sh` | exit 0 using documented constraints |
| Full verification | `./scripts/check.sh` | exit 0 on Python 3.10+ |
| Package metadata | `python3 -m build --no-isolation` | wheel and sdist build |

## Scope

**In scope**: `pyproject.toml`, `constraints/dev.txt` (create), `scripts/bootstrap.sh`, `.github/workflows/ci.yml`, `README.md`, and `docs/agents/codex.md`.

**Out of scope**: changing PDF libraries; vendoring packages; runtime internet access; committing virtual environments/build artifacts.

## Git workflow

- Branch: `advisor/013-pin-ci-toolchain`
- Commit example: `chore: constrain ci dependency resolution`.

## Steps

1. Choose a standard constraints mechanism compatible with pip and editable extras. Record exact direct/transitive versions tested on Python 3.10 and 3.13, with a documented refresh command. Do not hand-edit unexplained transitive pins.
   **Verify**: clean temporary virtual environments for both supported Python versions resolve successfully where available.
2. Keep compatible ranges in `pyproject.toml`; make bootstrap/CI consume the constraints file deterministically. Include the constraints file in CI cache keys.
   **Verify**: bootstrap output shows constrained versions; repeated resolution is identical.
3. Document update cadence and verification, then run full checks/build.
   **Verify**: `./scripts/check.sh` → exit 0; `git status --short` shows no tracked build artifacts.

## Test plan

This is primarily a resolution/CI plan. Test clean constrained installs on each supported Python version, run import-profile goldens to catch parser drift, run `./scripts/check.sh`, and inspect built wheel metadata. Expected: identical constrained versions and all checks pass.

## Done criteria

- [ ] Same commit resolves the same CI/dev dependency versions.
- [ ] Published metadata does not unnecessarily hard-pin end users.
- [ ] Python 3.10 and 3.13 remain supported.
- [ ] Constraint refresh workflow is documented.

## STOP conditions

- Network/package indexes are unavailable, preventing a trustworthy resolution.
- A selected version breaks either supported Python version.
- The repo already adopts a different lock tool during execution.

## Maintenance notes

Refresh constraints intentionally with parser goldens and full verification; automated blind updates are inappropriate for statement parsers.
