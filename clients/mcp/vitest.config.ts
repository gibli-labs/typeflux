/**
 * Vitest config for typeflux-mcp (#326 Phase 0). This package lives OUTSIDE the pnpm workspace, so
 * it cannot npm-install `@typeflux/temporal-controlplane` (that package uses `workspace:*` deps).
 * The managed-local backend consumes its `serve()` at build time (bundled for publish); for tests
 * and typecheck we alias the specifier to the package's BUILT dist in the monorepo. Node resolves
 * that dist's own deps from the pnpm store adjacent to it, so the real in-process control plane
 * runs unmodified. Build the monorepo TS packages first: `pnpm -r --filter ./packages/typescript/** build`.
 *
 * `@tf/conformance-schemas` aliases the CP's activity IO schema fixture so the e2e test can drive a
 * managed-local control plane that fully resolves bundle/catalog (see managed-local.ts SCHEMAS CAVEAT).
 */

import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const CP_DIST = fileURLToPath(
  new URL("../../packages/typescript/temporal-controlplane/dist/index.js", import.meta.url),
);
const YAML_DIST = fileURLToPath(
  new URL("../../packages/typescript/temporal-yaml/dist/index.js", import.meta.url),
);
const CLIENT_DIST = fileURLToPath(new URL("../typescript/dist/index.js", import.meta.url));
const CP_SCHEMAS = fileURLToPath(
  new URL(
    "../../packages/typescript/temporal-controlplane/dist/http/conformance-schemas.js",
    import.meta.url,
  ),
);

export default defineConfig({
  resolve: {
    alias: {
      "@typeflux/temporal-controlplane": CP_DIST,
      "@typeflux/temporal-yaml": YAML_DIST,
      "@typeflux/control-plane-client": CLIENT_DIST,
      "@tf/conformance-schemas": CP_SCHEMAS,
    },
  },
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
