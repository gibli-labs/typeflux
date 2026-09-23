/**
 * Streamable-HTTP transport (#326 Phase 3; design §8.2). Proves the two guarantees the design names:
 *   - BEARER AUTH is enforced before any MCP handling: no token / wrong token → 401; valid → 200.
 *   - PER-SESSION ISOLATION: each MCP session gets its own McpServer + backend (a fresh
 *     createSessionServer per initialize), and a session close disposes only that session's backend.
 * The happy path is driven with the SDK's real StreamableHTTP client; the auth gate is asserted with
 * raw fetch (deterministic, independent of client retry/backoff).
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { afterEach, describe, expect, it } from "vitest";

import type { Backend } from "../src/backend.js";
import { loadConfig, loadHttpConfig } from "../src/config.js";
import {
  bearerAuthorized,
  startHttpTransport,
  type HttpTransportHandle,
  type HttpTransportLimits,
} from "../src/http-transport.js";
import { createTypefluxMcpServer, type BackendProvider, type TypefluxMcpServer } from "../src/server.js";

const TOKEN = "team-secret-token";

/** A stub backend (canned reads) so a session's tools/list works with no live control plane. */
function stubBackend(degraded = false): Backend {
  const controlPlane = {
    meta: async () => ({ project: "demo", runtime: "typescript", capabilities: { can_resolve: true } }),
    workflows: async () => [{ id: "wf", name: "Demo" }],
  } as unknown as Backend["controlPlane"];
  return {
    mode: "attach",
    controlPlane,
    scopedControlPlane: () => controlPlane,
    meta: degraded ? undefined : ({ project: "demo" } as never),
    capabilities: degraded ? undefined : ({ can_resolve: true } as never),
    degraded,
    baseUrl: "http://127.0.0.1:0",
    dispose: async () => undefined,
  };
}

const handles: HttpTransportHandle[] = [];
const clients: Client[] = [];
afterEach(async () => {
  while (clients.length) await clients.pop()!.close().catch(() => undefined);
  while (handles.length) await handles.pop()!.close().catch(() => undefined);
});

/** A started transport plus live counters over the per-session builds/disposes. */
interface Started {
  handle: HttpTransportHandle;
  url: string;
  readonly created: number;
  readonly disposed: number;
}

/** Test-only knobs for a session build: a custom backend, and a barrier that gates prepare(). */
interface StartOpts {
  backend?: () => Backend;
  /** When set, each session's prepare() awaits this before running (to test the pre-prepare gate). */
  prepareGate?: Promise<void>;
}

/** Start the HTTP transport on an ephemeral port, counting each session build/dispose. */
async function start(limits: HttpTransportLimits = {}, opts: StartOpts = {}): Promise<Started> {
  const state = { created: 0, disposed: 0 };
  const config = loadConfig({
    TYPEFLUX_CP_URL: "http://127.0.0.1:1",
    TYPEFLUX_MCP_TRANSPORT: "http",
    TYPEFLUX_MCP_HTTP_TOKEN: TOKEN,
    TYPEFLUX_MCP_HTTP_PORT: "0",
  } as never);
  const handle = await startHttpTransport(
    config.http!,
    (): TypefluxMcpServer => {
      state.created += 1;
      const provider: BackendProvider = {
        get: async () => (opts.backend ? opts.backend() : stubBackend()),
        dispose: async () => undefined,
      };
      const built = createTypefluxMcpServer(config, { backend: provider });
      const realDispose = built.dispose;
      built.dispose = async () => {
        state.disposed += 1;
        await realDispose();
      };
      if (opts.prepareGate) {
        const realPrepare = built.prepare;
        built.prepare = async () => {
          await opts.prepareGate;
          await realPrepare();
        };
      }
      return built;
    },
    limits,
  );
  handles.push(handle);
  return {
    handle,
    url: `http://127.0.0.1:${handle.port}/mcp`,
    get created() {
      return state.created;
    },
    get disposed() {
      return state.disposed;
    },
  };
}

const INIT_BODY = JSON.stringify({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "raw", version: "0" } },
});
const MCP_HEADERS = { "content-type": "application/json", accept: "application/json, text/event-stream" };

describe("bearerAuthorized (unit)", () => {
  it("accepts the exact token and rejects everything else", () => {
    expect(bearerAuthorized(`Bearer ${TOKEN}`, TOKEN)).toBe(true);
    expect(bearerAuthorized(`bearer ${TOKEN}`, TOKEN)).toBe(true); // scheme is case-insensitive
    expect(bearerAuthorized(undefined, TOKEN)).toBe(false);
    expect(bearerAuthorized("Bearer wrong", TOKEN)).toBe(false);
    expect(bearerAuthorized(`Bearer ${TOKEN}x`, TOKEN)).toBe(false); // different length
    expect(bearerAuthorized(TOKEN, TOKEN)).toBe(false); // missing scheme
  });
});

describe("config: http transport is fail-closed without a token", () => {
  it("throws when TYPEFLUX_MCP_TRANSPORT=http but no token is set", () => {
    expect(() => loadHttpConfig({ TYPEFLUX_MCP_TRANSPORT: "http" } as never)).toThrow(/TYPEFLUX_MCP_HTTP_TOKEN/);
  });
  it("returns undefined (stdio) when the transport is not http", () => {
    expect(loadHttpConfig({} as never)).toBeUndefined();
  });
  it("defaults host to loopback and port to 8765", () => {
    const cfg = loadHttpConfig({ TYPEFLUX_MCP_TRANSPORT: "http", TYPEFLUX_MCP_HTTP_TOKEN: "t" } as never)!;
    expect(cfg.host).toBe("127.0.0.1");
    expect(cfg.port).toBe(8765);
  });
});

describe("auth gate (raw HTTP)", () => {
  it("rejects an initialize with NO token (401)", async () => {
    const { url } = await start();
    const res = await fetch(url, { method: "POST", headers: MCP_HEADERS, body: INIT_BODY });
    expect(res.status).toBe(401);
    expect(res.headers.get("www-authenticate")).toContain("Bearer");
  });

  it("rejects a WRONG token (401)", async () => {
    const { url } = await start();
    const res = await fetch(url, { method: "POST", headers: { ...MCP_HEADERS, authorization: "Bearer nope" }, body: INIT_BODY });
    expect(res.status).toBe(401);
  });

  it("accepts a VALID token and opens a session (200 + a session id header)", async () => {
    const { url } = await start();
    const res = await fetch(url, { method: "POST", headers: { ...MCP_HEADERS, authorization: `Bearer ${TOKEN}` }, body: INIT_BODY });
    expect(res.status).toBe(200);
    expect(res.headers.get("mcp-session-id")).toBeTruthy();
    await res.body?.cancel();
  });

  it("404s a path other than /mcp even with a valid token", async () => {
    const { handle } = await start();
    const res = await fetch(`http://127.0.0.1:${handle.port}/other`, {
      method: "POST",
      headers: { ...MCP_HEADERS, authorization: `Bearer ${TOKEN}` },
      body: INIT_BODY,
    });
    expect(res.status).toBe(404);
  });
});

describe("per-session isolation (real MCP client)", () => {
  it("a bearer-authed client connects and lists tools over Streamable HTTP", async () => {
    const started = await start();
    const client = new Client({ name: "http-e2e", version: "0" });
    clients.push(client);
    const transport = new StreamableHTTPClientTransport(new URL(started.url), {
      requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
    });
    await client.connect(transport);
    const { tools } = await client.listTools();
    // The full local surface is reachable over HTTP (e.g. the authoring aids).
    expect(tools.map((t) => t.name)).toContain("scaffold_ai_activity");
    expect(started.created).toBe(1);
  });

  it("each client gets its OWN session/server, and terminating one disposes only that session", async () => {
    const started = await start();
    const connectOne = async (): Promise<{ client: Client; transport: StreamableHTTPClientTransport }> => {
      const client = new Client({ name: "http-e2e", version: "0" });
      const transport = new StreamableHTTPClientTransport(new URL(started.url), {
        requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
      });
      await client.connect(transport);
      await client.listTools();
      return { client, transport };
    };
    const a = await connectOne();
    const b = await connectOne();
    clients.push(a.client, b.client);
    // Two initializes → two isolated session servers built (each with its own backend).
    expect(started.created).toBe(2);

    // Terminating one session (a DELETE) tears down exactly one session's server (onsessionclosed →
    // disposeSession); the other stays live and still answers.
    await a.transport.terminateSession();
    await new Promise((r) => setTimeout(r, 100));
    expect(started.disposed).toBe(1);
    const stillWorks = await b.client.listTools();
    expect(stillWorks.tools.length).toBeGreaterThan(0);
  });
});

/** Open a raw session via an initialize POST; returns its status + session id (no client, no DELETE). */
async function rawInit(url: string): Promise<{ status: number; sessionId: string | null }> {
  const res = await fetch(url, {
    method: "POST",
    headers: { ...MCP_HEADERS, authorization: `Bearer ${TOKEN}` },
    body: INIT_BODY,
  });
  const sessionId = res.headers.get("mcp-session-id");
  await res.body?.cancel();
  return { status: res.status, sessionId };
}

describe("session lifecycle guards (§8.2 — bound resource use, finder #2 / codex #6+#7)", () => {
  it("caps concurrent sessions — the N+1th initialize is rejected 503", async () => {
    const started = await start({ maxSessions: 2 });
    const a = await rawInit(started.url);
    const b = await rawInit(started.url);
    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
    // Two live sessions (neither DELETE'd); the third initialize is refused.
    const c = await fetch(started.url, {
      method: "POST",
      headers: { ...MCP_HEADERS, authorization: `Bearer ${TOKEN}` },
      body: INIT_BODY,
    });
    expect(c.status).toBe(503);
    expect(c.headers.get("retry-after")).toBeTruthy();
    await c.body?.cancel();
  });

  it("reaps an idle session that disconnected WITHOUT a DELETE (TTL sweep) — codex #6", async () => {
    const started = await start({ idleTimeoutMs: 40, sweepIntervalMs: 20 });
    const s = await rawInit(started.url);
    expect(s.status).toBe(200);
    expect(started.created).toBe(1);
    // The client never sent DELETE and never polls again — the idle sweep must dispose its backend.
    await new Promise((r) => setTimeout(r, 150));
    expect(started.disposed).toBe(1);
  });

  it("a REJECTED initialize disposes the orphaned build — no leak (codex #7)", async () => {
    const started = await start();
    // Missing `text/event-stream` in Accept → the SDK rejects the initialize (never fires
    // onsessioninitialized), so the session is never registered and the build must be disposed.
    const res = await fetch(started.url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json", authorization: `Bearer ${TOKEN}` },
      body: INIT_BODY,
    });
    expect(res.status).toBeGreaterThanOrEqual(400);
    await res.body?.cancel();
    await new Promise((r) => setTimeout(r, 50));
    // The build was created for the attempt, then disposed (not leaked); no live session remains.
    expect(started.created).toBe(1);
    expect(started.disposed).toBe(1);
  });

  it("an init whose connect/handleRequest THROWS disposes the orphan (500, no leak) — verify #2", async () => {
    // A build whose server.connect throws before any session can be registered — control unwinds to
    // the 500 handler; the throwing-init path must still dispose the orphaned build.
    let created = 0;
    let disposed = 0;
    const config = loadConfig({
      TYPEFLUX_CP_URL: "http://127.0.0.1:1",
      TYPEFLUX_MCP_TRANSPORT: "http",
      TYPEFLUX_MCP_HTTP_TOKEN: TOKEN,
      TYPEFLUX_MCP_HTTP_PORT: "0",
    } as never);
    const handle = await startHttpTransport(config.http!, (): TypefluxMcpServer => {
      created += 1;
      return {
        server: {
          connect: async () => {
            throw new Error("connect boom");
          },
          close: async () => undefined,
        },
        backend: { get: async () => ({}) as never, dispose: async () => undefined },
        prepare: async () => undefined,
        dispose: async () => {
          disposed += 1;
        },
      } as unknown as TypefluxMcpServer;
    });
    handles.push(handle);
    const res = await fetch(`http://127.0.0.1:${handle.port}/mcp`, {
      method: "POST",
      headers: { ...MCP_HEADERS, authorization: `Bearer ${TOKEN}` },
      body: INIT_BODY,
    });
    expect(res.status).toBe(500);
    await res.body?.cancel();
    await new Promise((r) => setTimeout(r, 30));
    expect(created).toBe(1);
    expect(disposed).toBe(1); // orphan disposed despite the throw → nothing leaked into the sweep-less void
  });
});

describe("§9 capability gating over HTTP (Bugbot #1 — must gate before serving)", () => {
  it("a degraded (no-inspect) session's tools/list shows ONLY the static/local surface", async () => {
    const started = await start({}, { backend: () => stubBackend(true) });
    const client = new Client({ name: "gate", version: "0" });
    clients.push(client);
    const transport = new StreamableHTTPClientTransport(new URL(started.url), {
      requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
    });
    await client.connect(transport);
    const names = (await client.listTools()).tools.map((t) => t.name).sort();
    // The gate held tools/list until prepare() ran its §9 degrade removal, so the LIVE + OPERATE
    // tools are gone; only the workspace-local authoring aids remain.
    expect(names).toEqual(["doctor", "scaffold_ai_activity", "scaffold_project_entry", "scaffold_workflow_yaml"]);
    expect(names).not.toContain("get_bundle");
    expect(names).not.toContain("start_workflow");
  });

  it("a tools/list issued BEFORE prepare() resolves is not served until prepared (the ordering gate)", async () => {
    // Gate prepare() on a barrier the test controls, so we can prove tools/list blocks until it clears.
    let release!: () => void;
    const barrier = new Promise<void>((r) => (release = r));
    const started = await start({}, { backend: () => stubBackend(true), prepareGate: barrier });
    const client = new Client({ name: "order", version: "0" });
    clients.push(client);
    const transport = new StreamableHTTPClientTransport(new URL(started.url), {
      requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } },
    });
    await client.connect(transport); // init completes; prepare() is now blocked on the barrier

    let settled = false;
    const listing = client.listTools().then((r) => {
      settled = true;
      return r;
    });
    // While prepare() is blocked, tools/list must NOT have been served (the gate is holding it).
    await new Promise((r) => setTimeout(r, 120));
    expect(settled).toBe(false);

    release(); // prepare() runs its §9 degrade removal, resolves prepared → the gate opens
    const names = (await listing).tools.map((t) => t.name).sort();
    expect(settled).toBe(true);
    // ...and what it finally serves is the GATED surface (degrade removed live/operate tools).
    expect(names).toEqual(["doctor", "scaffold_ai_activity", "scaffold_project_entry", "scaffold_workflow_yaml"]);
  });
});

describe("session cap has no overshoot under concurrent inits (Bugbot #2)", () => {
  it("exactly maxSessions of N simultaneous initializes succeed; the rest 503", async () => {
    const MAX = 3;
    const started = await start({ maxSessions: MAX });
    // Fire 8 initializes SIMULTANEOUSLY (no awaits between) so they all pass the cap check before any
    // registers — the synchronous pending reservation must still hold the line at MAX.
    const attempts = await Promise.all(Array.from({ length: 8 }, () => rawInit(started.url)));
    const ok = attempts.filter((a) => a.status === 200).length;
    const full = attempts.filter((a) => a.status === 503).length;
    expect(ok).toBe(MAX);
    expect(full).toBe(8 - MAX);
    expect(started.created).toBe(MAX); // no build past the cap — reservations prevented the overshoot
  });
});
