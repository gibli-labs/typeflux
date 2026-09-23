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
| TypeScript SDK (4 packages) | `packages/typescript/` | Node 22, pnpm |
| Language-neutral contracts | `contracts/` | JSON Schema / golden fixtures |
| Control-plane client (generated) | `clients/typescript/` | Node 22, npm |
| Console | `clients/console/` | Node 22, npm, Vite |
| MCP server | `clients/mcp/` | Node 22, npm |
| Docs | `docs/` | Markdown |

## Development setup

```bash
# Python
cd packages/python
uv sync --all-extras

# TypeScript workspace (from the repo root)
pnpm install
pnpm -r build
```

## Validating changes

Run the checks for every area you touched (CI runs the same commands):

**Python** (from `packages/python/`):

```bash
uv run ruff check . ../../scripts
uv run ruff format --check . ../../scripts
uv run mypy src/typeflux
uv run python -m build
uv run pytest -q -m "not live"
```

**TypeScript workspace** (from the repo root):

```bash
pnpm -r build && pnpm -r typecheck && pnpm -r test
```

**Generated client** (from `clients/typescript/`): `npm ci && npm run generate`
must leave `src/schema.ts` unchanged (`git diff --exit-code -- src/schema.ts`),
then `npm run typecheck && npm run build`.

**Console** (from `clients/console/`): `npm ci && npm run typecheck && npm test
&& npm run build`. The console's pinned `@gibli-labs/control-plane-client`
installs from GitHub Packages: `npm ci` needs `NODE_AUTH_TOKEN` set to a
token with `read:packages` (`gh auth refresh -s read:packages` then
`export NODE_AUTH_TOKEN=$(gh auth token)` works), otherwise it fails
with a 401.

**MCP server** (from `clients/mcp/` — reproduce CI's build order; the MCP
bundle inlines the in-repo control-plane builds):

```bash
pnpm -r --filter "./packages/typescript/**" build   # from the repo root
(cd clients/typescript && npm install && npm run build)
cd clients/mcp && npm ci   # public npm only — no token needed
npm test && npm run build && npm pack --dry-run
```

Any change under `clients/mcp/` must also bump the package version (CI
enforces this).

Tests marked `live` need real provider keys and a local Temporal dev server —
they are optional for contributors; CI's non-live suite is the merge gate.

## Contract-first rules

`contracts/` is the language-neutral source of truth. Changing the
control-plane OpenAPI contract, golden fixtures, or conformance schemas
requires the conforming server implementations, regenerated clients, and
version bumps to land in the same PR. Both SDK editions (Python and
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
