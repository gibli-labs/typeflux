/**
 * Streamable-HTTP transport (#326 Phase 3; design §8.2). The stdio default (index.ts) serves a
 * single local editor session; this transport serves a SHARED / hosted MCP server in front of a team
 * control plane (CI bots, a review-dashboard agent) with two guarantees the design calls out:
 *
 *   BEARER AUTH (required). Every request must carry `Authorization: Bearer <token>` matching
 *   `TYPEFLUX_MCP_HTTP_TOKEN`. A missing/wrong token is rejected with 401 BEFORE any MCP handling —
 *   and the server refuses to even start without a token (config.ts fail-closed), so a shared server
 *   can never come up unauthenticated. The compare is constant-time (no token-length/tinning oracle).
 *
 *   PER-SESSION ISOLATION. Each MCP session gets its OWN McpServer instance and its OWN control-plane
 *   backend (a fresh `createSessionServer()` per initialize), keyed by the Streamable-HTTP session id.
 *   One team member's session state — subscriptions, resolved backend, capability gating — never
 *   leaks into another's. A session is torn down (backend disposed) on its transport close / DELETE.
 *
 * The SDK's high-level `McpServer` does not own an HTTP listener, so this wires a `node:http` server
 * that maps POST/GET/DELETE `/mcp` onto per-session `StreamableHTTPServerTransport`s (stateful mode:
 * the transport mints a session id on initialize and validates it thereafter).
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { randomUUID, timingSafeEqual } from "node:crypto";

import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";

import type { HttpTransportConfig } from "./config.js";
import type { TypefluxMcpServer } from "./server.js";

/** Build a fresh, isolated MCP server (its own backend) for one session. */
export type CreateSessionServer = () => TypefluxMcpServer;

/** A live session: its transport, the isolated MCP server behind it, and its last-activity stamp. */
interface Session {
  transport: StreamableHTTPServerTransport;
  built: TypefluxMcpServer;
  /** Epoch ms of the last request routed to this session — drives idle eviction. */
  lastSeen: number;
  /**
   * Resolves once the session's `prepare()` (backend resolve + §9 capability gating / degrade
   * removal) has completed; rejects if it failed. Every NON-initialize request awaits this before it
   * is served, so a tool list/call can never run against the un-gated surface (a token without a
   * capability seeing/calling a tool that gating would have removed).
   */
  prepared: Promise<void>;
}

/** Options for the session lifecycle guards (§8.2 — bound resource use on a SHARED server). */
export interface HttpTransportLimits {
  /** Max concurrent sessions; a new initialize past this is rejected 503 (default 256). */
  maxSessions?: number;
  /** Idle timeout (ms) after which a session is swept + disposed (default 30 min). */
  idleTimeoutMs?: number;
  /** How often the idle sweep runs (ms; default 60s). */
  sweepIntervalMs?: number;
}

const DEFAULT_MAX_SESSIONS = 256;
const DEFAULT_IDLE_TIMEOUT_MS = 30 * 60 * 1000;
const DEFAULT_SWEEP_INTERVAL_MS = 60 * 1000;

/** A running Streamable-HTTP MCP listener. */
export interface HttpTransportHandle {
  server: Server;
  host: string;
  port: number;
  /** Close the listener and dispose every live session (each session's backend). */
  close(): Promise<void>;
}

const MCP_PATH = "/mcp";
const SESSION_HEADER = "mcp-session-id";
/** Max accepted POST body — a JSON-RPC MCP message is small; cap it so a client can't exhaust memory. */
const MAX_BODY_BYTES = 4 * 1024 * 1024;

/** Thrown by {@link readJsonBody} when the request body exceeds {@link MAX_BODY_BYTES}. */
class PayloadTooLargeError extends Error {}

/**
 * Whether the POST body carries a JSON-RPC REQUEST (a message with both `method` and `id`) — i.e. a
 * tools/list, tools/call, resources/list, … that could touch the surface capability gating governs.
 * NOTIFICATIONS (`method`, no `id`) and RESPONSES (`id`, no `method`) return false, so they are never
 * gated on prepare(): a notification (e.g. `notifications/initialized`) must not block connect, and a
 * client→server RESPONSE (e.g. answering the `roots/list` prepare() itself issues) must pass through
 * or prepare() would deadlock waiting on a response its own gate is blocking.
 */
function bodyHasRequest(body: unknown): boolean {
  const messages = Array.isArray(body) ? body : [body];
  return messages.some(
    (m) => m !== null && typeof m === "object" && "method" in (m as object) && "id" in (m as object),
  );
}

/** Constant-time bearer-token check. Returns false for a missing/malformed/mismatched token. */
export function bearerAuthorized(header: string | undefined, expected: string): boolean {
  if (typeof header !== "string") return false;
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (match === null) return false;
  const provided = Buffer.from(match[1]!, "utf8");
  const want = Buffer.from(expected, "utf8");
  // timingSafeEqual requires equal lengths; compare lengths first (leaks only the length, which the
  // operator controls), then a constant-time byte compare.
  if (provided.length !== want.length) return false;
  return timingSafeEqual(provided, want);
}

/**
 * Read and JSON-parse a request body; returns undefined for an empty/invalid body. Bounded: throws
 * {@link PayloadTooLargeError} (→ 413) once the accumulated body exceeds {@link MAX_BODY_BYTES}, so a
 * post-auth client can't stream an unbounded body to exhaust memory.
 */
async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    total += buf.length;
    if (total > MAX_BODY_BYTES) {
      req.destroy();
      throw new PayloadTooLargeError();
    }
    chunks.push(buf);
  }
  if (chunks.length === 0) return undefined;
  const raw = Buffer.concat(chunks).toString("utf8");
  if (raw.trim() === "") return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

/** A JSON-RPC error envelope (the shape MCP clients expect on a transport-level rejection). */
function rpcError(status: number, code: number, message: string) {
  return { jsonrpc: "2.0" as const, error: { code, message }, id: null };
}

/**
 * Start the Streamable-HTTP listener. `createSessionServer` is invoked once per NEW session to build
 * an isolated MCP server; the returned handle closes the listener and disposes every session.
 *
 * The session map is BOUNDED (design §8.2 — a shared server behind one team token must not be an OOM
 * lever): concurrent sessions are capped (a new initialize past the cap is rejected 503), and an idle
 * sweep disposes sessions unused beyond `idleTimeoutMs`. Each request refreshes its session's
 * activity stamp; eviction disposes the backend exactly like a DELETE/close.
 *
 * DISCONNECT-WITHOUT-DELETE: a client that just drops its connection (never sends DELETE) does not
 * fire the SDK's onclose/onsessionclosed, so the idle sweep is the authoritative reaper. We do NOT
 * dispose on the GET/SSE request's `close` event: in Streamable HTTP that stream legitimately closes
 * and REOPENS (resumability), so disposing on it would kill a session that is merely reconnecting.
 * The TTL sweep (refreshed on every real request) is the correct, race-free backstop.
 */
export async function startHttpTransport(
  config: HttpTransportConfig,
  createSessionServer: CreateSessionServer,
  limits: HttpTransportLimits = {},
): Promise<HttpTransportHandle> {
  const maxSessions = limits.maxSessions ?? DEFAULT_MAX_SESSIONS;
  const idleTimeoutMs = limits.idleTimeoutMs ?? DEFAULT_IDLE_TIMEOUT_MS;
  const sweepIntervalMs = limits.sweepIntervalMs ?? DEFAULT_SWEEP_INTERVAL_MS;
  const sessions = new Map<string, Session>();
  // Slots RESERVED by in-flight initializes that haven't yet registered a session (fired
  // onsessioninitialized). Counted against the cap SYNCHRONOUSLY at the check, so concurrent inits
  // can't collectively overshoot maxSessions before any of them inserts into `sessions`.
  let pending = 0;

  const disposeSession = async (sessionId: string): Promise<void> => {
    const session = sessions.get(sessionId);
    if (session === undefined) return;
    sessions.delete(sessionId);
    await session.built.dispose().catch(() => undefined);
  };

  // Idle-session sweep: dispose sessions unused beyond the timeout so an abandoned (never-DELETE'd)
  // session can't pin a backend forever. `.unref()` so it never keeps the process alive.
  const sweep = setInterval(() => {
    const cutoff = Date.now() - idleTimeoutMs;
    for (const [id, session] of [...sessions.entries()]) {
      if (session.lastSeen < cutoff) void disposeSession(id);
    }
  }, sweepIntervalMs);
  sweep.unref?.();

  const handle = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const url = new URL(req.url ?? "/", `http://${config.host}`);
    if (url.pathname !== MCP_PATH) {
      sendJson(res, 404, rpcError(404, -32601, "not found — POST/GET/DELETE /mcp"));
      return;
    }

    // AUTH GATE — enforced before any MCP handling, on every method (design §8.2).
    if (!bearerAuthorized(req.headers.authorization, config.token)) {
      res.setHeader("WWW-Authenticate", 'Bearer realm="typeflux-mcp"');
      sendJson(res, 401, rpcError(401, -32001, "unauthorized — a valid Bearer token is required"));
      return;
    }

    const sessionId = req.headers[SESSION_HEADER];
    const existing = typeof sessionId === "string" ? sessions.get(sessionId) : undefined;
    if (existing !== undefined) existing.lastSeen = Date.now(); // refresh activity on every request

    if (req.method === "POST") {
      let body: unknown;
      try {
        body = await readJsonBody(req);
      } catch (error) {
        if (error instanceof PayloadTooLargeError) {
          sendJson(res, 413, rpcError(413, -32000, "payload too large"));
          return;
        }
        throw error;
      }
      if (existing !== undefined) {
        // §9 GATE: a JSON-RPC REQUEST (tools/list, tools/call, …) must NOT be served until the
        // session's prepare() — backend resolve + capability gating / degrade removal — has completed,
        // else a tool list/call could race the gate and hit the un-gated surface (a token seeing/
        // calling a tool gating would have removed). Notifications + responses pass through ungated
        // (see bodyHasRequest) so connect and prepare()'s own client round-trips never block/deadlock.
        if (bodyHasRequest(body)) {
          try {
            await existing.prepared;
          } catch {
            sendJson(res, 503, rpcError(503, -32000, "session initialization failed; re-initialize"));
            return;
          }
          // Re-resolve after the await: prepare-failure disposal or a concurrent DELETE may have removed it.
          const live = typeof sessionId === "string" ? sessions.get(sessionId) : undefined;
          if (live === undefined) {
            sendJson(res, 404, rpcError(404, -32001, "unknown or expired session"));
            return;
          }
          await live.transport.handleRequest(req, res, body);
          return;
        }
        await existing.transport.handleRequest(req, res, body);
        return;
      }
      // A new session may only be opened by an INITIALIZE request (no prior session id).
      if (sessionId === undefined && isInitializeRequest(body)) {
        // Bound concurrent sessions — count live sessions AND in-flight reservations, so simultaneous
        // inits can't collectively overshoot the cap. Reject (not silently accept) once full.
        if (sessions.size + pending >= maxSessions) {
          res.setHeader("Retry-After", "30");
          sendJson(res, 503, rpcError(503, -32000, "server at capacity — too many concurrent sessions; retry later"));
          return;
        }
        pending += 1;
        let slotReleased = false;
        const releaseSlot = (): void => {
          if (!slotReleased) {
            slotReleased = true;
            pending -= 1;
          }
        };

        // The session's prepare() gate (see Session.prepared): existing-session requests await this.
        let resolvePrepared!: () => void;
        let rejectPrepared!: (reason: unknown) => void;
        const prepared = new Promise<void>((resolvePromise, rejectPromise) => {
          resolvePrepared = resolvePromise;
          rejectPrepared = rejectPromise;
        });
        prepared.catch(() => undefined); // never an unhandled rejection (the gate handles it per-request)

        const built = createSessionServer();
        const transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          onsessioninitialized: (id) => {
            sessions.set(id, { transport, built, lastSeen: Date.now(), prepared });
            releaseSlot(); // reservation became a real session — free the pending slot
          },
          // A client DELETE terminates the session — dispose its isolated backend. (The SDK fires
          // onsessionclosed on DELETE and onclose on a transport-level close; disposeSession is
          // idempotent — it removes the entry first — so wiring both never double-disposes.)
          onsessionclosed: (id) => void disposeSession(id),
        });
        transport.onclose = () => {
          if (transport.sessionId !== undefined) void disposeSession(transport.sessionId);
        };
        // Whether THIS build ever got registered as a live session (onsessioninitialized fired).
        const registered = (): boolean =>
          transport.sessionId !== undefined && sessions.has(transport.sessionId);
        try {
          await built.server.connect(transport);
          await transport.handleRequest(req, res, body);
        } catch (error) {
          // connect/handleRequest threw. If the session was never registered, nothing else will ever
          // dispose this build (it's not in the map, so the sweep can't reach it) — dispose it here
          // before the error unwinds to the 500 handler, so a throwing init can't leak a backend.
          releaseSlot();
          rejectPrepared(error);
          if (!registered()) await built.dispose().catch(() => undefined);
          throw error;
        }
        // Prepare the backend + apply capability gating (§9) ONLY AFTER a successful initialize:
        //   - the client's capabilities (incl. roots) are known only post-init, so a managed-local
        //     session resolves its manifest via roots here — preparing before would cache a no-roots
        //     failure;
        //   - a REJECTED initialize (e.g. a bad Accept header → the SDK never fires
        //     onsessioninitialized) leaves the build unregistered, so we dispose it rather than leak
        //     an orphaned McpServer + backend.
        releaseSlot(); // idempotent — already freed in onsessioninitialized on the success path
        if (registered()) {
          try {
            await built.prepare();
            resolvePrepared();
          } catch (error) {
            // Gating failed → the session can't be served safely. Reject the gate and dispose it, so
            // subsequent requests get 503/404 instead of an un-gated surface.
            rejectPrepared(error);
            if (transport.sessionId !== undefined) await disposeSession(transport.sessionId);
          }
        } else {
          rejectPrepared(new Error("session was not initialized"));
          await built.dispose().catch(() => undefined);
        }
        return;
      }
      sendJson(res, 400, rpcError(400, -32000, "bad request — no valid session id; initialize first"));
      return;
    }

    if (req.method === "GET" || req.method === "DELETE") {
      if (existing === undefined) {
        sendJson(res, 404, rpcError(404, -32001, "unknown or expired session"));
        return;
      }
      await existing.transport.handleRequest(req, res);
      return;
    }

    sendJson(res, 405, rpcError(405, -32601, "method not allowed"));
  };

  const server = createServer((req, res) => {
    handle(req, res).catch((error: unknown) => {
      if (!res.headersSent) {
        sendJson(res, 500, rpcError(500, -32603, error instanceof Error ? error.message : "internal error"));
      }
    });
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(config.port, config.host, () => {
      server.removeListener("error", reject);
      resolve();
    });
  });

  const address = server.address();
  const port = typeof address === "object" && address !== null ? address.port : config.port;

  return {
    server,
    host: config.host,
    port,
    close: async () => {
      clearInterval(sweep);
      for (const id of [...sessions.keys()]) await disposeSession(id);
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
}
