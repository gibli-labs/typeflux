import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end smoke (#293): a real served control-plane API + the rendered
 * console agree on routing (including project-scoped routes) and the key
 * read-only pages render. Non-live — read endpoints need no Temporal, and the
 * smoke never touches operations pages.
 *
 * Two web servers are started:
 *  1. the control-plane API over the e2e registry (the examples manifest under
 *     two ids, so the project switcher + scoped routing are exercised);
 *  2. the console dev server, whose Vite proxy forwards /api to the API — so no
 *     CORS and the proxy path itself is part of what's tested.
 *
 * The Python package is at packages/python (three levels up from this config at
 * clients/console); the API backend runs from there so the example projects'
 * `examples.*` schema modules are importable when the bundle is resolved.
 */
const PACKAGE_DIR = "../../packages/python";
const API_PORT = 8400;
const CONSOLE_PORT = 5173;

/**
 * The lane (#621 slice 2): which control-plane edition backs the suite.
 * `python-cp` (default) serves the examples project; `ts-cp` serves the canonical
 * conformance fixture project's TS binding via the #620 server (build the TS
 * workspace first: `pnpm -r build`). Specs read the matching expectations from
 * `e2e/expectations.ts`.
 */
const LANE = process.env.E2E_SERVER ?? "python-cp";
const API_COMMAND =
  LANE === "ts-cp"
    ? `node ../../packages/typescript/temporal-controlplane/dist/http/serve.js ` +
      `--registry ./e2e/typeflux.projects.ts-cp.yaml --port ${API_PORT} --conformance-schemas`
    : `uv run --extra api python -m typeflux.controlplane serve ` +
      `--registry ../../clients/console/e2e/typeflux.projects.yaml --port ${API_PORT}`;
// The Python API runs from packages/python so the examples' schema modules import;
// the TS server is self-contained and runs from the console directory.
const API_CWD = LANE === "ts-cp" ? "." : PACKAGE_DIR;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${CONSOLE_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // Serve the lane's fixture registry under two ids; read endpoints are non-live.
      command: API_COMMAND,
      cwd: API_CWD,
      port: API_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      // Dev server: its Vite proxy forwards /api to the API on 8400. Bind
      // 127.0.0.1 explicitly so the readiness check and baseURL agree (Vite's
      // default `localhost` can resolve to IPv6 only, refusing 127.0.0.1).
      command: `npm run dev -- --host 127.0.0.1 --port ${CONSOLE_PORT}`,
      port: CONSOLE_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
