/**
 * Managed-local backend (#326 Phase 0; maintainer amendment 2026-07-16): drive the TypeScript
 * control plane IN-PROCESS from a discovered manifest, keeping the MCP server pure-Node with no
 * Python prerequisite.
 *
 * Mechanism:
 *   1. Synthesize an ephemeral one-project registry from the manifest path via
 *      `ProjectRegistry.single(manifestPath, id, runtime)` — no `typeflux.projects.yaml` file is
 *      written; the registry-of-one is constructed directly.
 *   2. `serve({ registry, schemasFor, port: 0, host: "127.0.0.1" })` — bind loopback on an
 *      EPHEMERAL port (never a public listener).
 *   3. Readiness: poll `GET /api/v1/meta` until 200, bounded (design §9: 10s).
 *   4. Return the base URL + a `dispose()` that closes the server on shutdown.
 *
 * SCHEMAS CAVEAT (design-vs-reality): `serve()` needs a project's activity input/output schemas to
 * project `bundle`/`catalog` (the control plane holds no activity code — the host injects them).
 * A pure-Node MCP server cannot synthesize a real user project's schemas, so the default
 * `schemasFor` returns `undefined`: pure-YAML reads (meta/workflows/environments/policies/
 * validate-of-shape) stay up, and `bundle`/`catalog` answer 422 on the first activity ref — the
 * design's honest degradation. Full `bundle`/`catalog` resolution needs ATTACH mode against a
 * control plane wired with the project's schemas. The optional `schemas` injection seam exists for
 * a host (or the e2e test, via the conformance fixture) that DOES have them.
 */

// TYPES ONLY at the top — erased at compile time, so importing this module does NOT pull in the
// heavy in-process control plane (or its @temporalio/* deps). The runtime values are loaded via a
// LAZY dynamic import inside startManagedLocal (below), so ATTACH mode (TYPEFLUX_CP_URL) never
// loads the serve machinery at all (#326 item 2).
import type { ControlPlaneServer, serve as ServeFn } from "@typeflux/temporal-controlplane";

/** A started managed-local control plane. */
export interface ManagedLocalHandle {
  baseUrl: string;
  port: number;
  dispose(): Promise<void>;
}

export interface ManagedLocalOptions {
  manifestPath: string;
  runtime: "typescript" | "python";
  /** The synthesized project's id (also the default). */
  projectId?: string;
  readinessTimeoutMs?: number;
  /**
   * Optional per-project activity IO schemas for bundle/catalog projection (the `schemasFor` seam
   * of the CP's `RegistryContext`). Default production value is undefined (see the SCHEMAS CAVEAT);
   * a host or test that has real schemas injects them here. Typed loosely to avoid coupling the
   * MCP package to the CP's zod version.
   */
  schemas?: Readonly<Record<string, unknown>>;
  /** Injectable for tests: the serve function (defaults to a lazily-imported in-process serve). */
  serveFn?: typeof ServeFn;
  /** Injectable for tests: the readiness probe (defaults to a real fetch of /api/v1/meta). */
  probe?: (baseUrl: string) => Promise<boolean>;
}

/** Poll `GET /api/v1/meta` until it answers 200 or the deadline passes. */
async function pollReady(
  baseUrl: string,
  timeoutMs: number,
  probe: (baseUrl: string) => Promise<boolean>,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      if (await probe(baseUrl)) return;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(
    `managed-local control plane did not become ready within ${timeoutMs}ms` +
      (lastError instanceof Error ? `: ${lastError.message}` : ""),
  );
}

/** The default readiness probe: a real HTTP GET of /api/v1/meta. */
async function defaultProbe(baseUrl: string): Promise<boolean> {
  const response = await fetch(`${baseUrl}/api/v1/meta`);
  return response.status === 200;
}

/**
 * Start the in-process TS control plane for `manifestPath` and return a handle. The server binds
 * 127.0.0.1 on an ephemeral port; `dispose()` closes it.
 */
export async function startManagedLocal(options: ManagedLocalOptions): Promise<ManagedLocalHandle> {
  const {
    manifestPath,
    runtime,
    projectId = "local",
    readinessTimeoutMs = 10_000,
    schemas,
    serveFn,
    probe = defaultProbe,
  } = options;

  // Lazy load: only reached in managed-local mode, keeping attach mode free of the CP dependency.
  const cp = await import("@typeflux/temporal-controlplane");
  const registry = cp.ProjectRegistry.single(manifestPath, projectId, runtime);
  const serve = serveFn ?? cp.serve;
  let started: ControlPlaneServer;
  try {
    started = await serve({
      registry,
      // Cast: the CP types `schemasFor` as `Record<string, z.ZodType>`; we keep the MCP package
      // decoupled from the CP's zod version and pass the injected schemas (or undefined) through.
      schemasFor: () => schemas as never,
      port: 0,
      host: "127.0.0.1",
    });
  } catch (error) {
    throw new Error(
      `failed to start the managed-local control plane for ${manifestPath}: ` +
        (error instanceof Error ? error.message : String(error)),
    );
  }

  const baseUrl = `http://127.0.0.1:${started.port}`;
  try {
    await pollReady(baseUrl, readinessTimeoutMs, probe);
  } catch (error) {
    await started.close().catch(() => undefined);
    throw error;
  }

  return {
    baseUrl,
    port: started.port,
    dispose: () => started.close(),
  };
}
