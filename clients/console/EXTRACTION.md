# Console extraction runbook (#621 slice 4)

The console is a planned repository boundary. This runbook makes the
extraction executable in under a day **when the owners decide to do it** — the
extraction itself is a human-gated release decision and is deliberately not
automated. Nothing here needs to be rehearsed in advance: every prerequisite is
already true on `main` (verified by the slice-4 bare-checkout proof).

## What is already true (the boundary holds today)

- **Zero `file:`/`workspace:` references**: the console consumes the published
  `@gibli-labs/control-plane-client` from GitHub Packages (`.npmrc` routes the
  scope); the lockfile pins a release. CI verifies the client major tracks the
  contract major (`scripts/assert_client_contract_lockstep.mjs`).
- **No repo-root tooling**: `npm ci && npm run typecheck && npm test && npm run
  build` succeed in a bare checkout of this directory alone. The one external
  requirement is registry auth for `npm ci` (`read:packages` on the scope) —
  post-extraction CI provides it exactly like the monorepo Web job does.
  (Local dev without a `read:packages` token: `npm link ../typescript`-style
  symlinking of the in-repo client works but is machine-local state — never
  commit it; the lockfile stays pinned to the registry release.)
- **Contract-only server knowledge**: capability flags from `/meta`, honest
  degradation for `UnsupportedRuntime`, and the e2e suite runs against BOTH
  control-plane editions (`E2E_SERVER=python-cp|ts-cp`).

## The one in-tree coupling: the e2e server harness

`playwright.config.ts` SPAWNS the servers under test from the monorepo
(`packages/python` via uv, `packages/typescript/temporal-controlplane` via
node) and the e2e registries reference the conformance fixture project by
relative path. That is dev-test infrastructure, not build tooling. Post
extraction, replace the two `webServer` commands with released artifacts:

- python-cp: `pip install typeflux[api]` (or the published container)
  and serve a checked-in copy of the fixture project;
- ts-cp: the published control-plane server package/image with
  `--conformance-schemas`;
- vendor `e2e/` fixture projects into the new repo (they are small YAML trees;
  copy `contracts/controlplane/conformance/project/{python,typescript}` or pin
  the servers' own example projects).

Until the servers are published artifacts, an alternative is a scheduled
cross-repo e2e job in THIS monorepo that checks out the extracted console —
decide at extraction time.

## Day-of checklist

1. **History-preserving split** (from a fresh clone, never the working repo):

   ```bash
   git clone git@github.com:gibli-labs/typeflux.git console-extract
   cd console-extract
   pip install git-filter-repo
   git filter-repo --path clients/console --path-rename clients/console/:
   git remote add origin git@github.com:gibli-labs/<console-repo>.git
   git push -u origin main
   ```

   `git filter-repo` rewrites history to just this directory, preserving every
   commit that touched it. (`--path-rename` hoists the contents to the root.)

2. **License**: retain the Apache-2.0 `LICENSE` and matching
   `package.json`/lockfile metadata. If the extracted repository changes
   license, treat that as a separate owner/legal decision and update the
   license file, manifest, and lockfile together.

3. **CI bootstrap** — translate the monorepo Web job (`.github/workflows/ci.yml`,
   `web` job) into the new repo's workflow:
   - `actions/setup-node` (node 22, npm cache) + `npm ci` with
     `NODE_AUTH_TOKEN` and `permissions: packages: read`;
   - `npm run typecheck && npm test && npm run build`;
   - the split Playwright setup steps (apt deps uncapped, browser cached +
     retried — copy them verbatim, the comments explain why);
   - both e2e lanes per the harness replacement above.

4. **Docker image** (the delivery target; no Dockerfile exists yet — create in
   the new repo): multi-stage `node:22` build into an nginx/static stage
   serving `dist/`, with `/api` proxying left to the deployment (the console
   is a static SPA; the Vite dev proxy is dev-only). Registry auth for
   `npm ci` must use a **BuildKit secret mount** (`RUN --mount=type=secret,id=npmrc
   npm ci`), never a build ARG — args persist in image metadata/provenance and
   cached layers.

5. **Client bump flow** (unchanged post-extraction): contract PR in the
   monorepo → client release workflow publishes → **console bump PR in the new
   repo** updates the pinned version. The lockstep gate script travels with the
   client, not the console.

6. **Monorepo cleanup PR**: remove `clients/console/`, drop the console steps
   from the Web job (keep the client regen/lockstep steps), update
   `contracts/controlplane/README.md` pointers.

## Rollback

The monorepo history is untouched by `git filter-repo` on a clone. Abandoning
the extraction = deleting the new repo and the cleanup PR; nothing in the
monorepo changes until step 6 merges.
