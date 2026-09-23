/**
 * Runtime configuration for the Typeflux MCP server (#326 Phase 0). Two mutually-exclusive modes,
 * selected purely by environment (design §11 decision 2):
 *
 *   ATTACH        — `TYPEFLUX_CP_URL` is set: talk to an already-running control plane (local or
 *                   hosted, Python or TS) at that URL, with an optional bearer token. Skips all
 *                   managed-local machinery. This is the path to FULL resolution against a control
 *                   plane wired with the project's activity schemas.
 *   MANAGED-LOCAL — default: discover the project manifest via MCP roots, synthesize a one-project
 *                   registry, and drive the TypeScript control plane in-process on loopback
 *                   (torn down on shutdown). Pure-Node, no Python prerequisite.
 *
 * Nothing here reads a secret into a log. The token is carried only on the outgoing Authorization
 * header (client.ts).
 */

export type BackendMode = "attach" | "managed-local";

/** The MCP transport the server listens on (design §8.2). */
export type TransportKind = "stdio" | "http";

/**
 * Streamable-HTTP transport config (#326 Phase 3; design §8.2). Present only when
 * `TYPEFLUX_MCP_TRANSPORT=http`. A shared/hosted server REQUIRES a bearer token — `loadConfig`
 * fails closed when the token is missing, so an unauthenticated shared server can never start.
 */
export interface HttpTransportConfig {
  /** Bind host (`TYPEFLUX_MCP_HTTP_HOST`, default 127.0.0.1 — loopback unless deliberately widened). */
  host: string;
  /** Bind port (`TYPEFLUX_MCP_HTTP_PORT`, default 8765). */
  port: number;
  /** Required bearer token (`TYPEFLUX_MCP_HTTP_TOKEN`). Every request must present it; never logged. */
  token: string;
}

export interface ServerConfig {
  mode: BackendMode;
  /** The transport to listen on. `stdio` (default) or `http` (Streamable HTTP, design §8.2). */
  transport: TransportKind;
  /** Present iff `transport === "http"` — the shared-deployment listener config. */
  http?: HttpTransportConfig;
  /** ATTACH: the control-plane base URL (`TYPEFLUX_CP_URL`). */
  attachUrl?: string;
  /** ATTACH: optional bearer token (`TYPEFLUX_CP_TOKEN`). Never logged. */
  attachToken?: string;
  /** Optional project id to scope to (`TYPEFLUX_PROJECT`) — for a multi-project attached CP. */
  project?: string;
  /**
   * MANAGED-LOCAL: an explicit `typeflux.project.yaml` path (`TYPEFLUX_MANIFEST`), bypassing roots
   * discovery. Useful when the MCP client advertises no roots.
   */
  manifestPath?: string;
  /**
   * MANAGED-LOCAL: the project's declared runtime (`TYPEFLUX_RUNTIME`, default `typescript`). A
   * `python` project stays inspectable (pure-YAML reads) and answers 501 on resolution-dependent
   * reads — the design's documented degradation.
   */
  runtime: "typescript" | "python";
  /** Readiness timeout for the managed-local control plane, ms (design §9: 10s-bounded). */
  readinessTimeoutMs: number;
}

/**
 * Parse the Streamable-HTTP transport config (design §8.2). Returns undefined for the default stdio
 * transport. In http mode a bearer token is MANDATORY: an unset `TYPEFLUX_MCP_HTTP_TOKEN` throws so a
 * shared/hosted server is never brought up unauthenticated (fail-closed, §9).
 */
export function loadHttpConfig(env: NodeJS.ProcessEnv = process.env): HttpTransportConfig | undefined {
  if (env.TYPEFLUX_MCP_TRANSPORT?.trim().toLowerCase() !== "http") return undefined;
  const token = env.TYPEFLUX_MCP_HTTP_TOKEN?.trim();
  if (!token) {
    throw new Error(
      "TYPEFLUX_MCP_TRANSPORT=http requires TYPEFLUX_MCP_HTTP_TOKEN — a shared/hosted MCP server " +
        "must authenticate every request (design §8.2). Set a bearer token, or use the default " +
        "stdio transport for a local single-user session.",
    );
  }
  const portRaw = env.TYPEFLUX_MCP_HTTP_PORT?.trim();
  const port = portRaw ? Number(portRaw) : 8765;
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error(`TYPEFLUX_MCP_HTTP_PORT must be a valid port (0-65535); got ${portRaw}`);
  }
  return {
    host: env.TYPEFLUX_MCP_HTTP_HOST?.trim() || "127.0.0.1",
    port,
    token,
  };
}

/** Parse the process environment into a {@link ServerConfig}. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): ServerConfig {
  const attachUrl = env.TYPEFLUX_CP_URL?.trim();
  const runtimeRaw = env.TYPEFLUX_RUNTIME?.trim();
  const runtime = runtimeRaw === "python" ? "python" : "typescript";
  const http = loadHttpConfig(env);
  const base = {
    transport: (http ? "http" : "stdio") as TransportKind,
    ...(http ? { http } : {}),
    project: env.TYPEFLUX_PROJECT?.trim() || undefined,
    runtime,
    readinessTimeoutMs: 10_000,
  } as const;

  if (attachUrl) {
    return {
      mode: "attach",
      attachUrl,
      attachToken: env.TYPEFLUX_CP_TOKEN?.trim() || undefined,
      ...base,
    };
  }
  return {
    mode: "managed-local",
    manifestPath: env.TYPEFLUX_MANIFEST?.trim() || undefined,
    ...base,
  };
}
