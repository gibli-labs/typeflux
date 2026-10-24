# Contributing to Typeflux Temporal

Thanks for your interest in contributing! This document covers how to set up a
development environment, validate changes, and get a pull request merged.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Sign-off (DCO)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO). Every commit must carry a `Signed-off-by` line matching the commit
author:

```bash
git commit -s -m "fix: ..."
```

The DCO check on pull requests enforces this. There is no CLA.

## Repository layout

| Area | Path | Toolchain |
|---|---|---|
| Python SDK | `packages/python/` | Python ≥ 3.11, [uv](https://docs.astral.sh/uv/) |
| TypeScript SDK (4 packages) | `packages/typescript/` | Node 24.15+, pnpm 12.15 |
| Language-neutral contracts | `contracts/` | JSON Schema / golden fixtures |
| Control-plane client (generated) | `clients/typescript/` | Node 24.15+, npm |
| Console | `clients/console/` | Node 24.15+, npm, Vite |
| MCP server | `clients/mcp/` | Node 24.15+, npm |
| Docs | `docs/` | Markdown |

## Development and validation

Follow [the development loop](docs/development.md) for pinned tool versions, frozen
setup, focused checks and the full local/CI profile. From the repository root:

```sh
uv run --project packages/python python -m tools.development sync all
uv run --project packages/python python -m tools.development run default
```

The console uses the candidate generated client in this checkout. Contract, server,
client and console changes are verified together; no registry token or intermediate
client release is needed. Package versions change when preparing a release, rather
than on every MCP test/documentation edit. The full profile includes isolated local
Temporal with fake providers; optional live-provider suites still require their own
credentials and are outside this profile.

## Contract-first rules

`contracts/` is the language-neutral source of truth. Changing the
control-plane OpenAPI contract, golden fixtures, or conformance schemas
requires the conforming server implementations, regenerated clients, to land in the same PR. Prepare release version bumps when publishing. Both SDK editions (Python and
TypeScript) must keep behavioral parity: a feature or fix that changes
observable behavior in one edition needs the matching change (or a tracked
issue) in the other.

## Pull requests

- Keep PRs focused and complete: implementation, tests, and docs together.
- Fill in the PR template — including the API/YAML-compatibility, security,
  and release-impact statements.
- All CI checks must pass; review feedback is addressed with commits plus a
  reply on the thread.
- Maintainers squash-merge by default.

## Reporting issues

Open a GitHub issue with the affected package/version, environment, and a
minimal reproduction (see [SUPPORT.md](SUPPORT.md)). For anything
security-sensitive, **do not open a public issue** — see
[SECURITY.md](SECURITY.md).
