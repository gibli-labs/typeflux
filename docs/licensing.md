# Licensing and component inventory

Every tracked component of this repository, its origin classification, and its
license. The repository-level license is **Apache-2.0** (root `LICENSE`); the
root `NOTICE` file accompanies it. There is no vendored third-party source
code anywhere in the tree — third-party libraries are consumed as declared
package-manager dependencies under their own licenses, and the remaining
externally fetched components (container images, CI actions, build/test
downloads) are inventoried below.

Classification legend:

- **original** — authored in this repository.
- **generated** — produced by tooling in this repository from original
  sources (the generator inputs are original; the output is committed).
- **derived-synthetic** — fixture/asset content authored or synthesized for
  this repository; contains no third-party or personal material.

| Component | Path | Classification | License |
|---|---|---|---|
| Python SDK (`typeflux`) | `packages/python/` | original | Apache-2.0 (`packages/python/LICENSE`, `pyproject.toml`) |
| Python examples | `packages/python/examples/` | original | Apache-2.0 (repository license) |
| TS SDK core (`@typeflux/temporal`) | `packages/typescript/temporal/` | original | Apache-2.0 |
| TS YAML runtime (`@typeflux/temporal-yaml`) | `packages/typescript/temporal-yaml/` | original | Apache-2.0 |
| TS worker (`@typeflux/temporal-worker`) | `packages/typescript/temporal-worker/` | original | Apache-2.0 |
| TS control plane (`@typeflux/temporal-controlplane`) | `packages/typescript/temporal-controlplane/` | original + generated (`src/http/contract.ts` from `contracts/controlplane/openapi.v1.json`) | Apache-2.0 |
| TS test setup (workspace-internal) | `packages/typescript/test-setup/` | original | Apache-2.0 (repository license; unpublished) |
| Console (`@typeflux/console`) | `clients/console/` | original | Apache-2.0 (`clients/console/LICENSE`; unpublished, `private: true`) |
| MCP server (`typeflux-mcp`) | `clients/mcp/` | original + generated (`src/schema/*.schema.json` from `scripts/generate-schemas.mjs`) | Apache-2.0 (published to npmjs.org) |
| Generated control-plane client (`@typeflux/control-plane-client`) | `clients/typescript/` | original + generated (`src/schema.ts` from `contracts/controlplane/openapi.v1.json`) | Apache-2.0 |
| Contracts (schemas, goldens, conformance fixtures) | `contracts/` | original + generated — `contracts/controlplane/openapi.v1.json` is the hand-governed normative contract (servers conform to it; clients are generated *from* it); committed golden JSON files are generated from the Python baseline | Apache-2.0 (repository license) |
| Documentation | `docs/`, `README.md`, `CHANGELOG.md` | original | Apache-2.0 (repository license) |
| Deployment templates | `deploy/` | original | Apache-2.0 (repository license) |
| Repo scripts | `scripts/` | original | Apache-2.0 (repository license) |
| CI/automation | `.github/` | original | Apache-2.0 (repository license) |

## Third-party software consumed outside package managers

Beyond library dependencies declared in `pyproject.toml`, `package.json`, and
lockfiles, the tree references these third-party components at build/CI/deploy
time (none are vendored; each is pulled from its upstream registry under its
own license):

- Container base images: `python:3.12-slim-bookworm`, `node:22-bookworm-slim`
  (Docker Official Images), `temporalio/temporal` (MIT, digest-pinned in
  the compose file), and `temporalio/auto-setup:1.26` (MIT, tag-pinned in
  example run instructions).
- Containerized build/CI tools: `ghcr.io/astral-sh/uv` (MIT/Apache-2.0,
  binary copied in `deploy/yaml-worker/Dockerfile`) and
  `ghcr.io/gitleaks/gitleaks` (MIT, run by CI and pre-commit).
- Build/test-time downloads: the `docker/dockerfile:1` BuildKit frontend
  (Apache-2.0, referenced by both Dockerfiles); `pnpm` fetched by
  `corepack prepare` in `deploy/ts-yaml-worker/Dockerfile` (MIT); and the
  Chromium browser binary fetched by `npx playwright install chromium` in
  console CI (BSD-style Chromium license, via Playwright, Apache-2.0); and
  `reportlab==5.0.1` (BSD-3-Clause), fetched only by the documented one-off
  command that regenerates the synthetic `sample-contract.pdf` fixture.
- GitHub Actions: `actions/checkout`, `actions/cache`, `actions/setup-node`,
  `actions/upload-artifact`, `actions/dependency-review-action`,
  `github/codeql-action`, `astral-sh/setup-uv`, `pnpm/action-setup`.

## Fixture and asset provenance

The tree contains four non-code fixture assets:

| Asset | Provenance | Disposition |
|---|---|---|
| `packages/python/examples/multimodal_claim_review/fixtures/repair-estimate.pdf` | Hand-written 140-byte placeholder PDF authored for this repository | derived-synthetic; ships as-is |
| `packages/python/examples/multimodal_claim_review/fixtures/kitchen-photo.svg` | Hand-written SVG authored for this repository | derived-synthetic; ships as-is |
| `packages/python/examples/multimodal_claim_review/fixtures/claim-note.txt` | Hand-written synthetic adjuster note authored for this repository | derived-synthetic; ships as-is |
| `packages/python/examples/contract_risk_review/fixtures/sample-contract.pdf` | Fully synthetic Master Services Agreement generated by the committed `generate_sample_contract.py` (fictional parties, neutral metadata) | derived-synthetic; ships as-is |

Conformance vectors under `contracts/` use patterned/sequential synthetic
inputs and textual test keys; historical synthetic-key blobs are documented in
`.gitleaksignore`.

## MCP tarball attribution (resolved)

`typeflux-mcp`'s published tarball bundles its runtime dependencies via
esbuild. Since 0.5.4 the tarball carries the package LICENSE, NOTICE, and a
generated `THIRD-PARTY-NOTICES.md` reproducing every bundled dependency's
license text (27 packages), enforced fail-closed by the build and the
`verify:pack` gate in CI and the release workflow.

## Release policy

Publication targets, versioning, rollback, and package-metadata
requirements are defined in [release-policy.md](release-policy.md).

## Contribution terms

Contributions are accepted under **DCO** (Developer Certificate of Origin,
`Signed-off-by`) — chosen over a CLA for lower contributor friction. The
enforcement workflow and CONTRIBUTING.md land with the community policy pack.

## Not a certification

Open-source availability of this software is **not** a claim of
regulated-production certification. The production and compliance readiness
documents (`docs/production-readiness.md`, `docs/compliance-readiness.md`)
remain authoritative about what is and is not certified.
