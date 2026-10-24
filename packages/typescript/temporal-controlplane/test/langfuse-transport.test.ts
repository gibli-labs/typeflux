/**
 * The injected langfuse transport seam (#573): the connection-probe wrapper + the fetch-based
 * reference adapter. All network is faked (an injected `fetch`) — no live langfuse is touched.
 */

import { describe, expect, it } from "vitest";

import {
  fetchLangfuseTransport,
  langfuseConnectionProbe,
  type LangfuseControlPlaneTransport,
} from "../src/index.js";

describe("langfuseConnectionProbe (#573)", () => {
  const okTransport: LangfuseControlPlaneTransport = {
    ping: async () => undefined,
    promptLabelVersion: async () => undefined,
    lastRunPromptVersions: async () => ({}),
    traceSummary: async () => null,
    searchEnforcementTraces: async () => [],
  };

  it("non-langfuse backends are reachable with no host (matches the network-free default)", async () => {
    const probe = langfuseConnectionProbe(okTransport, { env: {} });
    expect(await probe({ type: "inline", configuredHost: null, environmentId: "local" })).toEqual({
      reachable: true,
      host: null,
    });
  });

  it("langfuse: a successful ping is reachable with the resolved host", async () => {
    const probe = langfuseConnectionProbe(okTransport, { env: {} });
    expect(
      await probe({ type: "langfuse", configuredHost: "https://lf.example", environmentId: "local" }),
    ).toEqual({ reachable: true, host: "https://lf.example" });
  });

  it("langfuse: a ping that throws degrades to reachable:false with a host-free sanitized detail", async () => {
    const probe = langfuseConnectionProbe(
      { ...okTransport, ping: async () => { throw new Error("connect https://user:sk@lf.example failed"); } },
      { env: {} },
    );
    const result = await probe({ type: "langfuse", configuredHost: "https://lf.example", environmentId: "local" });
    expect(result.reachable).toBe(false);
    // Fixed class string — never the raw error text (which embedded a credentialed URL).
    expect(result.detail).toBe("langfuse probe failed");
    expect(JSON.stringify(result)).not.toContain("sk@");
  });

  it("host resolution: configured host wins, else LANGFUSE_HOST, else LANGFUSE_BASE_URL, else null", async () => {
    const seen: (string | null)[] = [];
    const recording: LangfuseControlPlaneTransport = {
      ...okTransport,
      ping: async ({ host }) => { seen.push(host); },
    };
    const run = (configuredHost: string | null, env: Record<string, string | undefined>) =>
      langfuseConnectionProbe(recording, { env })({ type: "langfuse", configuredHost, environmentId: "local" });
    await run("https://configured", { LANGFUSE_HOST: "https://env-host" });
    await run("", { LANGFUSE_HOST: "https://env-host" });
    await run("", { LANGFUSE_BASE_URL: "https://base" });
    await run("", {});
    expect(seen).toEqual(["https://configured", "https://env-host", "https://base", null]);
  });
});

/** The fixture speaks v2 wire shapes, including opaque cursors and unfiltered detail reads. */
function backend(handler: (query: URLSearchParams, path: string) => unknown, status = 200) {
  const calls: { url: URL; auth: string | undefined }[] = [];
  const fn: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    calls.push({ url, auth: (init?.headers as Record<string, string>)?.["authorization"] });
    return new Response(JSON.stringify(handler(url.searchParams, url.pathname)), { status });
  };
  return { calls, transport: fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", env: {}, fetch: fn }) };
}
const page = (data: unknown[], cursor: string | null = null) => ({ data, meta: { cursor } });
const activity = (name = "assess", version = 3) => ({ activity_name: name, prompt_ref: { name }, resolved_prompt_version: version, provider_model: "gpt-x" });
const root = (traceId: string, metadata: unknown = {}) => ({ traceId, id: `${traceId}-root`, name: "TypefluxWorkflow:W", isRootObservation: true, startTime: "2026-07-01T00:00:00Z", metadata });
const child = (traceId: string, metadata: unknown) => ({ traceId, parentObservationId: `${traceId}-root`, metadata });
const window = { since: new Date("2026-07-01T00:00:00Z"), until: new Date("2026-07-08T00:00:00Z") };

describe("fetchLangfuseTransport Observations v2", () => {
  it("pings v2 with Basic auth, a bounded window, and the most specific host", async () => {
    const { transport, calls } = backend(() => page([]));
    await transport.ping({ host: "https://specific/", environmentId: "local" });
    expect(calls[0]!.url.origin).toBe("https://specific");
    expect(calls[0]!.url.pathname).toBe("/api/public/v2/observations");
    expect(calls[0]!.url.searchParams.get("limit")).toBe("1");
    expect(calls[0]!.url.searchParams.has("fromStartTime")).toBe(true);
    expect(calls[0]!.auth).toBe(`Basic ${Buffer.from("pk:sk").toString("base64")}`);
  });

  it("rejects HTTP failures without echoing secrets or response bodies", async () => {
    const { transport } = backend(() => ({ message: "secret" }), 403);
    await expect(transport.ping({ host: null, environmentId: "local" })).rejects.toThrow(/status 403/);
    await expect(transport.ping({ host: null, environmentId: "local" })).rejects.not.toThrow(/secret|https/);
  });

  it("uses the v2 prompt label endpoint and returns unknown when version is absent", async () => {
    const { transport, calls } = backend((q) => q.get("label") === "production" ? { version: 4 } : {});
    expect(await transport.promptLabelVersion({ name: "a/b", label: "production", host: null })).toBe("4");
    expect(calls[0]!.url.pathname).toBe("/api/public/v2/prompts/a%2Fb");
    expect(await transport.promptLabelVersion({ name: "a", label: "missing", host: null })).toBeUndefined();
  });

  it.each(["nested", "flat", "mixed"])("reconstructs versions from %s metadata and preserves authoritative root rollups", async (form) => {
    const manifest = { activities: [activity("assess", 9)] };
    const rootMetadata = form === "nested" ? { typeflux: { execution_manifest: manifest } } : {
      ...(form === "mixed" ? { typeflux: { other: true } } : {}), "typeflux.execution_manifest": manifest,
    };
    const { transport, calls } = backend((q) => q.has("traceId") ? page([
      root("t"), // A worker shell must not hide the caller's authoritative rollup.
      child("t", { typeflux: { activity_execution_manifest: activity("assess", 8) } }),
      root("t", rootMetadata), child("t", { "typeflux.activity_execution_manifest": activity("decide", 2) }),
    ]) : page([{ traceId: "t" }]));
    expect(await transport.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "9", decide: "2" });
    const filters = JSON.parse(calls[0]!.url.searchParams.get("filter")!);
    expect(filters).toContainEqual({ column: "traceTags", type: "arrayOptions", operator: "all of", value: ["typeflux.workflow:W"] });
    expect(filters.filter((f: { column: string }) => f.column === "startTime")).toHaveLength(2);
    expect(calls[1]!.url.searchParams.get("expandMetadata")).toContain("typeflux_moderation");
    expect(calls[1]!.url.searchParams.has("filter")).toBe(false);
  });

  it("follows a cursor on a short page, skips lifecycle traces, and handles unnamed observations", async () => {
    const { transport, calls } = backend((q) => {
      if (q.has("traceId")) return page([root("t", { typeflux: { execution_manifest: { activities: [activity()] } } })]);
      return q.has("cursor") ? page([{ traceId: "t", traceName: null }]) : page([{ traceId: "lc", traceName: "TypefluxLifecycleSignal:x" }], "opaque+/=");
    });
    expect(await transport.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "3" });
    expect(calls[1]!.url.searchParams.get("cursor")).toBe("opaque+/=");
    expect(calls.filter((c) => c.url.searchParams.has("traceId"))).toHaveLength(1);
  });

  it("caps discovery at five pages and reports unknown for malformed or failed drift reads", async () => {
    let count = 0;
    const { transport } = backend(() => page([{ traceId: "lc", traceName: "TypefluxLifecycleSignal:x" }], `next-${++count}`));
    expect(await transport.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({});
    expect(count).toBe(5);
    expect(await backend(() => ({})).transport.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({});
    expect(await backend(() => ({}), 500).transport.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({});
  });

  it.each([
    { workflow_id: "exec" },
    { typeflux: { temporal: { workflow_id: "exec" } } },
    { typeflux: { activity_execution_manifest: { ...activity(), workflow_id: "exec" } } },
  ])("correlates workflow and worker-only observation metadata: %j", async (join) => {
    const { transport, calls } = backend((q) => q.has("traceId") ? page([
      root("t"), { ...child("t", { typeflux: { activity_execution_manifest: activity() } }), level: "ERROR" },
    ]) : page([{ traceId: "noise", metadata: {} }, child("t", join)]));
    expect(await transport.traceSummary({ workflowId: "exec", host: null })).toEqual({
      trace_id: "t", timestamp: "2026-07-01T00:00:00Z", status: "error", workflow_name: "TypefluxWorkflow:W",
      workflow_id: "exec", activities: ["assess"], prompt_refs: ["assess"], provider_models: ["gpt-x"], warnings: [],
    });
    expect(calls.filter((c) => c.url.searchParams.has("traceId"))).toHaveLength(1);
  });

  it("returns null for no correlation and rejects invalid or repeated-cursor detail reads", async () => {
    expect(await backend(() => page([])).transport.traceSummary({ workflowId: "exec", host: null })).toBeNull();
    const { transport } = backend((q) => q.has("traceId") ? page([root("t")], "same") : page([root("t", { workflow_id: "exec" })]));
    await expect(transport.traceSummary({ workflowId: "exec", host: null })).rejects.toThrow(/cursor/);
    await expect(backend(() => ({})).transport.traceSummary({ workflowId: "exec", host: null })).rejects.toThrow(/completeness/);
  });

  it("hydrates every detail page and retains old roots with in-window enforcement observations", async () => {
    const { transport, calls } = backend((q) => {
      if (!q.has("traceId")) return page([{ traceId: "t" }, { traceId: "t" }]);
      if (!q.has("cursor")) return page([{ ...root("t", { workflow_id: "exec-old" }), startTime: "2026-01-01T00:00:00Z" }], "detail-2");
      return page([{ ...child("t", { typeflux_moderation: { decision: "block" } }), startTime: "2026-07-05T00:00:00Z" }]);
    });
    const records = await transport.searchEnforcementTraces({ ...window, limit: 50, host: null, environmentId: "prod" });
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({ traceId: "t", workflowId: "exec-old", workflowName: "W", timestamp: "2026-01-01T00:00:00Z" });
    expect(records[0]!.observations[1]!.metadata).toMatchObject({ typeflux_moderation: { decision: "block" } });
    const scan = calls[0]!.url.searchParams;
    expect(scan.get("fromStartTime")).toBe(window.since.toISOString());
    expect(scan.get("toStartTime")).toBe(window.until.toISOString());
    expect(scan.has("environment")).toBe(false);
    expect(calls.slice(1).every((c) => !c.url.searchParams.has("fromStartTime"))).toBe(true);
  });

  it("rejects failed enforcement reads and incomplete detail retrieval", async () => {
    const options = { ...window, limit: 50, host: null, environmentId: null };
    await expect(backend(() => ({}), 503).transport.searchEnforcementTraces(options)).rejects.toThrow(/503/);
    const { transport } = backend((q) => q.has("traceId") ? {} : page([{ traceId: "t" }]));
    await expect(transport.searchEnforcementTraces(options)).rejects.toThrow(/completeness/);
  });

  it("enforces request timeouts", async () => {
    const aborting: typeof fetch = async (_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("timed out", "TimeoutError")));
    });
    const transport = fetchLangfuseTransport({ fetch: aborting, timeoutMs: 20, env: {} });
    await expect(transport.ping({ host: null, environmentId: "local" })).rejects.toBeInstanceOf(DOMException);
  });

  it("reports configured enforcement only with host and both credentials", () => {
    const cfg = (opts: Parameters<typeof fetchLangfuseTransport>[0]) => fetchLangfuseTransport(opts).enforcementCredentialsConfigured;
    expect(cfg({ publicKey: "pk", secretKey: "sk", host: "https://lf", env: {} })).toBe(true);
    expect(cfg({ publicKey: "", secretKey: "sk", host: "https://lf", env: {} })).toBe(false);
    expect(cfg({ publicKey: "pk", secretKey: "sk", env: {} })).toBe(false);
    expect(cfg({ publicKey: "pk", secretKey: "sk", env: { LANGFUSE_BASE_URL: "https://base" } })).toBe(true);
  });
});
