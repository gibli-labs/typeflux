/**
 * The control-plane backend the MCP read surface talks to (#326 Phase 0): resolves the configured
 * mode (attach | managed-local) into a live {@link TypefluxControlPlane}, fetches `/meta` once to
 * learn capabilities, and exposes a `dispose()` for shutdown.
 *
 * Resolution is LAZY and cached: the managed-local path needs MCP roots, which only exist after the
 * client has connected, so the backend is resolved on first tool/resource use and memoized. A
 * resolution failure is surfaced as a structured tool/resource error, never a process crash.
 *
 * CAPABILITY GATING (design §9): `/meta.capabilities` says what the caller can do against the
 * routed project. A 403 on `/meta` means no `inspect` grant — the whole live read tier is denied —
 * so the backend records `degraded: true` and the server keeps only the static discovery surface
 * (docs / examples / schema / guide). `can_resolve: false` means resolution-dependent reads will
 * 501; those tools stay registered but return the honest structured `UnsupportedRuntime`.
 */

import {
  type ApiMeta,
  type Capabilities,
  TypefluxControlPlane,
} from "./control-plane/client.js";
import { ApiRequestError } from "./control-plane/errors.js";
import type { BackendMode, ServerConfig } from "./config.js";
import { discoverManifest } from "./discovery.js";
import { startManagedLocal, type ManagedLocalHandle } from "./managed-local.js";

/** A resolved, live backend. */
export interface Backend {
  mode: BackendMode;
  /** The default-project control plane (unprefixed routes, or the TYPEFLUX_PROJECT scope). */
  controlPlane: TypefluxControlPlane;
  /** A control plane scoped to a specific project id (the `{project}` URI dimension, §3.3). */
  scopedControlPlane(project: string): TypefluxControlPlane;
  /** `/meta`, or undefined when it failed (e.g. a 403 → degraded). */
  meta: ApiMeta | undefined;
  capabilities: Capabilities | undefined;
  /** True when the live read tier is denied (403 `inspect`) — only static resources remain. */
  degraded: boolean;
  /** The base URL the backend resolved to (loopback for managed-local). */
  baseUrl: string;
  dispose(): Promise<void>;
}

/** The reserved `{project}` token meaning "the default project" (unprefixed routes, §3.3). */
export const DEFAULT_PROJECT_TOKEN = "default";

export interface BackendDeps {
  /** List the MCP client's roots as `file://` URIs (managed-local discovery). */
  listRoots?: () => Promise<string[]>;
  /**
   * Optional activity IO schemas for the managed-local control plane (see managed-local.ts's
   * SCHEMAS CAVEAT). Production leaves this undefined; the e2e test injects the conformance
   * fixture's schemas so bundle/catalog resolve for real.
   */
  managedSchemas?: Readonly<Record<string, unknown>>;
}

/** Resolve the manifest path for managed-local: explicit config, else roots discovery. */
async function resolveManifestPath(
  config: ServerConfig,
  deps: BackendDeps,
): Promise<string> {
  if (config.manifestPath) return config.manifestPath;
  if (deps.listRoots === undefined) {
    throw new Error(
      "managed-local mode needs a project manifest: set TYPEFLUX_MANIFEST, or run under an MCP " +
        "client that advertises roots so a typeflux.project.yaml can be discovered",
    );
  }
  const roots = await deps.listRoots();
  const discovered = discoverManifest(roots);
  if (discovered === undefined) {
    throw new Error(
      `no typeflux.project.yaml found in the client's roots (${roots.length} root(s) searched); ` +
        "set TYPEFLUX_MANIFEST to point at one explicitly, or use TYPEFLUX_CP_URL to attach to a " +
        "running control plane",
    );
  }
  return discovered.manifestPath;
}

interface ClientFactory {
  baseUrl: string;
  token: string | undefined;
  defaultProject: string | undefined;
}

/** Build the control-plane client and fetch capabilities (degrading on a 403). */
async function withMeta(
  mode: BackendMode,
  factory: ClientFactory,
  dispose: () => Promise<void>,
): Promise<Backend> {
  const controlPlane = new TypefluxControlPlane({
    baseUrl: factory.baseUrl,
    token: factory.token,
    project: factory.defaultProject,
  });
  // A specific `{project}` in a resource URI scopes a fresh client; the reserved `default` token
  // (and an empty value) fall back to the default client above.
  const scopedControlPlane = (project: string): TypefluxControlPlane => {
    if (project === "" || project === DEFAULT_PROJECT_TOKEN) return controlPlane;
    return new TypefluxControlPlane({ baseUrl: factory.baseUrl, token: factory.token, project });
  };

  try {
    const meta = await controlPlane.meta();
    return {
      mode,
      controlPlane,
      scopedControlPlane,
      meta,
      capabilities: meta.capabilities,
      degraded: false,
      baseUrl: factory.baseUrl,
      dispose,
    };
  } catch (error) {
    // A 403 on /meta = no `inspect` grant: the live tier is denied. Degrade rather than crash —
    // the static discovery surface (docs/examples/schema) still serves. Any other error rethrows.
    if (error instanceof ApiRequestError && error.status === 403) {
      return {
        mode,
        controlPlane,
        scopedControlPlane,
        meta: undefined,
        capabilities: undefined,
        degraded: true,
        baseUrl: factory.baseUrl,
        dispose,
      };
    }
    await dispose().catch(() => undefined);
    throw error;
  }
}

/**
 * Resolve the backend for the given config. For managed-local this starts the in-process control
 * plane; for attach it points a client at the configured URL. The caller memoizes the promise.
 */
export async function resolveBackend(config: ServerConfig, deps: BackendDeps = {}): Promise<Backend> {
  if (config.mode === "attach") {
    return withMeta(
      "attach",
      { baseUrl: config.attachUrl!, token: config.attachToken, defaultProject: config.project },
      async () => undefined,
    );
  }

  const manifestPath = await resolveManifestPath(config, deps);
  let handle: ManagedLocalHandle;
  try {
    handle = await startManagedLocal({
      manifestPath,
      runtime: config.runtime,
      readinessTimeoutMs: config.readinessTimeoutMs,
      schemas: deps.managedSchemas,
    });
  } catch (error) {
    throw error instanceof Error ? error : new Error(String(error));
  }
  // The synthesized registry uses "local" as both the id and the default, so the default mount
  // serves it — no project scoping needed on the default client.
  return withMeta("managed-local", { baseUrl: handle.baseUrl, token: undefined, defaultProject: undefined }, handle.dispose);
}

/** Base cooldown a failed resolution is served from cache before `get()` re-attempts (#785). */
export const BACKEND_RETRY_COOLDOWN_MS = 15_000;
/** Ceiling for the exponential retry cooldown (#785): consecutive failures back off to this. */
export const BACKEND_RETRY_COOLDOWN_CAP_MS = 300_000;

/** The `resolveBackend` shape, for injecting a scripted resolver in tests. */
export type BackendResolver = typeof resolveBackend;

/**
 * A lazily-resolved, memoized backend holder. `get()` resolves once (on first use) and a
 * SUCCESSFUL backend stays cached for the life of the process. A FAILED resolution is cached
 * only for a cooldown window (#785): within it, repeated calls report the same structured
 * error rather than restarting a broken control plane on every tool call; after it, the next
 * call re-resolves — so a transient first-connect failure (port race, readiness timeout under
 * load, /meta blip) heals without restarting a long-lived stdio session, and a roots-discovered
 * manifest added later is picked up too. Consecutive failures back off exponentially (base
 * cooldown doubling up to the cap) so an unattended poller — a status subscription re-arming
 * every second — cannot turn the retry into a background control-plane restart timer.
 * `dispose()` is terminal: a disposed holder never resolves again (a session torn down by the
 * idle sweep must not resurrect a control plane nothing can reach or dispose).
 */
export class LazyBackend {
  private resolved: Promise<Backend> | undefined;
  private failedAt: number | undefined;
  private consecutiveFailures = 0;
  private disposed = false;
  constructor(
    private readonly config: ServerConfig,
    private readonly deps: BackendDeps = {},
    private readonly retryCooldownMs: number = BACKEND_RETRY_COOLDOWN_MS,
    // Test seams: inject a scripted resolver / clock.
    private readonly resolver: BackendResolver = resolveBackend,
    private readonly now: () => number = Date.now,
  ) {}

  private currentCooldownMs(): number {
    const doublings = Math.max(0, this.consecutiveFailures - 1);
    return Math.min(this.retryCooldownMs * 2 ** doublings, BACKEND_RETRY_COOLDOWN_CAP_MS);
  }

  get(): Promise<Backend> {
    if (this.disposed) {
      return Promise.reject(new Error("backend disposed: the session owning it was torn down"));
    }
    if (
      this.resolved !== undefined &&
      this.failedAt !== undefined &&
      this.now() - this.failedAt >= this.currentCooldownMs()
    ) {
      // The cached attempt rejected and the cooldown elapsed — drop it and retry.
      this.resolved = undefined;
      this.failedAt = undefined;
    }
    if (this.resolved === undefined) {
      const attempt = this.resolver(this.config, this.deps);
      this.resolved = attempt;
      attempt.then(
        () => {
          if (this.resolved === attempt) {
            this.failedAt = undefined;
            this.consecutiveFailures = 0;
          }
        },
        () => {
          if (this.resolved === attempt) {
            this.failedAt = this.now();
            this.consecutiveFailures += 1;
          }
        },
      );
    }
    return this.resolved;
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    if (this.resolved === undefined) return;
    try {
      const backend = await this.resolved;
      await backend.dispose();
    } catch {
      // Resolution failed; nothing to dispose.
    }
  }
}
