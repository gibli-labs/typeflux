# Artifact and release policy

The normative policy for what this project publishes, where, under which
names, and with what compatibility promises. Release automation must conform
to this document; changes to it follow the significant-change process in
[GOVERNANCE.md](../GOVERNANCE.md).

## Artifact and registry matrix

| Artifact | Registry | Package name | Status |
|---|---|---|---|
| Python SDK (+ CLI entry points) | PyPI | `typeflux` | staged; first publication via Trusted Publishing |
| TS SDK core | npmjs.org | `@typeflux/temporal` | staged |
| TS YAML runtime | npmjs.org | `@typeflux/temporal-yaml` | staged |
| TS worker | npmjs.org | `@typeflux/temporal-worker` | staged |
| TS control plane | npmjs.org | `@typeflux/temporal-controlplane` | staged |
| MCP server | npmjs.org | `typeflux-mcp` | published |
| Generated control-plane client | npmjs.org | `@typeflux/control-plane-client` | staged (migrated from GitHub Packages, #893); prior `@gibli-labs/control-plane-client` versions remain on GitHub Packages as a frozen historical lineage |
| Console | — | `@typeflux/console` | not published (`private: true`); deployed from source/dist |
| Worker container images | — | (none first-party) | deploy templates build user-owned images; no first-party image registry yet |

Anything not in this table is not a published artifact. New rows require a
policy update in the same PR that adds the publishing automation.

## Namespace ownership

- The `typeflux-mcp` package is owned by the maintainer's npm account. The
  npm `@typeflux` scope and the PyPI project `typeflux` are
  **pre-publication acquisitions**: registering the scope and verifying or
  creating the PyPI publishing account are prerequisites for the first
  `@typeflux/*` and PyPI releases — no automation may assume they exist
  until the boundary record marks them acquired. Two-factor authentication
  is required on every publishing account.
- Publishing rights are not shared outside the maintainer team
  ([MAINTAINERS.md](../MAINTAINERS.md)). A backup owner is a rollout
  checklist item resolved by documented decision: single-maintainer
  operation was accepted for launch (2026-09-23), with backup designation
  revisited in the post-launch quarter — MAINTAINERS.md is authoritative
  for the current state.
- The public repository home is `gibli-labs/typeflux`; package `repository`
  metadata points there.

## Versioning and compatibility

- **Everything is 0.x (beta)** except where a package documents otherwise.
  Under 0.x: a **minor** release may contain features *and* breaking
  changes — every breaking change is called out in the CHANGELOG and release
  notes; a **patch** release contains fixes only, never breaking changes.
- There are **no deprecation windows or compatibility shims pre-1.0**: the
  project removes replaced behavior outright and documents it. The 1.0
  decision gate introduces real deprecation windows and a support policy.
- **Contract/client lockstep**: the generated control-plane client's major
  version equals the control-plane contract major (CI-enforced). Servers
  conform to the contract; clients are generated from it.
- **Cross-package independence**: packages version independently, except
  the four TypeScript SDK packages, which release as one version train
  (below).
- The two SDK editions (Python, TypeScript) promise **behavioral parity
  on the shared, conformance-covered contract surface** — not
  byte-identical output, and not identical feature breadth:
  [editions.md](editions.md) is the authoritative parity table, including
  the TypeScript surfaces that are partial or unsupported.

## Supported runtimes and platforms

| Surface | Supported |
|---|---|
| Python | 3.11, 3.12 (CI matrix) |
| Node.js | 22 (the CI-tested major) |
| OS | Linux (CI), macOS (development); Windows is best-effort, untested in CI |
| Temporal | Server compatibility follows [Temporal's official SDK–server compatibility policy](https://docs.temporal.io/encyclopedia/temporal-sdks) for the pinned SDK ranges (`temporalio>=1.8,<2`, `@temporalio/*` 1.x); the project does not test servers beyond what those SDKs support |

The CI matrix is the arbiter: a runtime is supported when CI tests it.
Widening this table is a policy change and lands together with the CI
coverage that backs it.

## Releases, tags, and evidence

- Every publication is cut from a Git tag on `main` and produces a GitHub
  Release carrying the built artifact(s) and their SHA-256 checksums.
- Tag schemes: `mcp-vX.Y.Z` (MCP), `client-vX.Y.Z` (generated client),
  and — reserved for the SDK release pipelines — `py-vX.Y.Z` (Python SDK).
  The four TypeScript SDK packages release as a **train**: one
  `ts-vX.Y.Z` tag whose version is the train's own (all four packages are
  set to that version at release time — they version together, an
  explicit exception to cross-package independence; if they ever need to
  diverge, this policy must first be amended to per-package tags).
- Tags are immutable once their release exists: a bad release is superseded
  by a new version, never by re-tagging.
- Release workflows must publish the **exact artifact they hashed** (no
  re-packing between evidence and upload). Artifacts on public registries
  are verified **anonymously** post-publish (uncredentialed fetch,
  byte-identity check). (The formerly authenticated-registry client migrated to public npm in
  #893; no authenticated verification path remains.)
- npm provenance and PyPI Trusted Publishing attestations are required on
  every publish. The first SDK/client publications are **launch-coupled**:
  they dispatch only from the public repository (the npm pipelines pass
  `--provenance` unconditionally, so a private-repo dispatch fails
  closed).

**Pipeline compliance status.** The MCP pipeline (`release-mcp.yml`)
implements this section today: exact-bytes tarball publish (pinned npm),
sha256 evidence, anonymous post-publish byte-identity verification, a
GitHub Release with checksummed assets, and a fail-closed packed-content
gate including third-party attribution. The generated-client pipeline
(`release-client.yml`) was brought into compliance with the client's
migration to public npm (#893): main-ancestry gate, tag-before-publish,
exact-tarball publish, anonymous verification, and a GitHub Release with
checksum. The
Python (`release-python.yml`) and TypeScript-train
(`release-typescript.yml`) pipelines exist and conform:
tag-after-gates-before-publish, exact-artifact publish, content gates,
anonymous post-publish verification, and release evidence.

## Rollback policy

Published artifacts are treated as immutable history; rollback means
**superseding**, not erasing:

- **npm**: publish a fixed patch, then `npm deprecate` the bad version with
  a pointer to the fix. `npm unpublish` is reserved for disclosure-class
  incidents (secrets, confidential content) within npm's unpublish policy
  limits.
- **PyPI**: `yank` the bad release (installs by exact pin keep working;
  resolvers skip it) and publish a fixed patch.
- **GitHub Releases**: never deleted; a superseded release is edited to
  point at its replacement.

Every rollback event gets a CHANGELOG entry stating what was wrong and
which version supersedes it.

## Package metadata requirements

Every published package must carry:

- `license` (SPDX: `Apache-2.0`) and the LICENSE file in the artifact;
  bundled third-party code requires reproduced notices in the artifact.
  (The MCP release pipeline is the first to implement this; the SDK
  pipelines adopt the same gate before their first publication.)
- `repository` pointing at the public home, plus `bugs`/`homepage`.
- An explicit `files` allowlist (npm) / build-backend include set (Python)
  — no incidental files in artifacts; content gates verify the packed
  surface.
- A README rendered by the registry page.

## Dependency-license policy

Runtime and bundled dependencies must be permissively licensed;
strong-copyleft licenses (GPL/AGPL/LGPL/SSPL families) are denied by the
dependency-review gate on every PR. Exceptions require a policy change
here first.

## Related documents

- [Licensing & component inventory](licensing.md)
- [Editions & parity](editions.md)
- [Production readiness](production-readiness.md) — release ≠ certification
- [SECURITY.md](../SECURITY.md) — supported-versions security policy
