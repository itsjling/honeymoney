# Plan 007: Enforce loopback-only Ollama requests

> **Executor instructions**: Preserve local testing and privacy guarantees. Do not add network services. Update the index after all gates pass.
>
> **Drift check (run first)**: `git diff --stat aa0eedf..HEAD -- honeymoney/ollama.py honeymoney/cli.py tests/test_ollama.py README.md docs/architecture.md examples/config.json`
> Stop if the product now explicitly supports non-local inference.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: Plan 002
- **Category**: security
- **Planned at**: commit `aa0eedf`, 2026-07-11

## Why this matters

The core privacy promise is local-only processing, but an unrestricted configured URL and redirects can send sensitive transaction fields to a remote host. The integration must verify the scheme and loopback destination before constructing or following a request.

## Current state

`honeymoney/ollama.py:194-196` includes transaction description, merchant, dates, amounts, institution, and payment method. At `ollama.py:201-210`, the configured URL goes directly to the default opener. `README.md:183-192` and `docs/architecture.md:58-62` describe local Ollama only.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Ollama tests | `python3 -m unittest tests.test_ollama` | all pass without live network |
| CLI tests | `python3 -m unittest tests.test_cli_bootstrap` | all pass |
| Full verification | `./scripts/check.sh` | exit 0; live smoke is not run |

## Scope

**In scope**: `honeymoney/ollama.py`, `honeymoney/cli.py` only for the Plan-002 validation hook, `tests/test_ollama.py`, `tests/test_agent_cli.py`, `README.md`, and `docs/architecture.md`.

**Out of scope**: LAN/remote Ollama support; cloud inference; credentials in URLs; live smoke execution; changing categorization response schema.

## Git workflow

- Branch: `advisor/007-enforce-local-ollama`
- Commit example: `fix: restrict ollama requests to loopback`.

## Steps

1. Add tests accepting `localhost`, `127.0.0.1`, and `::1` with ephemeral local test servers; reject remote hostnames/IPs, unsafe schemes, URL credentials, malformed ports, DNS resolving to non-loopback, and redirects to non-loopback. Do not contact external hosts.
   **Verify**: `python3 -m unittest tests.test_ollama` → rejection cases fail before implementation.
2. Parse and validate the URL before payload/request creation. Resolve host addresses and require every usable address to be loopback. Use a redirect handler that revalidates each destination or disables redirects. Convert failures to actionable `ValueError`s compatible with Plan 002.
   **Verify**: `python3 -m unittest tests.test_ollama tests.test_agent_cli` → all pass.
3. Review `_ollama_transaction_payload` field-by-field and remove fields not required for categorization only if existing golden expectations permit it. Document the enforced boundary.
   **Verify**: categorization golden tests and full check pass.

## Test plan

Follow the ephemeral `HTTPServer` pattern in `tests/test_ollama.py`. Add loopback IPv4/IPv6, localhost resolution, unsafe scheme, credentials, remote literal IP, mixed DNS answers, and redirect cases; assert rejected destinations receive no request. Verify with `python3 -m unittest tests.test_ollama tests.test_agent_cli`.

## Done criteria

- [ ] No request or redirect can reach a non-loopback address.
- [ ] Loopback IPv4/IPv6 and localhost tests pass.
- [ ] Invalid endpoints fail before transaction payload transmission.
- [ ] No live Ollama/network test is added to default CI.
- [ ] `./scripts/check.sh` passes.

## STOP conditions

- Requirements now explicitly permit LAN or remote endpoints.
- Secure hostname resolution cannot be tested deterministically without external network access.
- The solution relies only on hostname spelling without checking resolved addresses/redirects.

## Maintenance notes

Any future non-loopback mode requires an explicit product/privacy decision and opt-in contract, not a silent relaxation of this validator.
