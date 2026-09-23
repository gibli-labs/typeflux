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

describe("fetchLangfuseTransport (#573)", () => {
  /** A fake `fetch` returning scripted JSON per URL substring; records the requests it saw. */
  const fakeFetch = (routes: Array<{ match: string; status?: number; json?: unknown }>) => {
    const calls: { url: string; auth: string | null }[] = [];
    const fn = (async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      calls.push({ url: u, auth: (init?.headers as Record<string, string>)?.["authorization"] ?? null });
      const route = routes.find((r) => u.includes(r.match));
      const status = route?.status ?? (route ? 200 : 404);
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => route?.json ?? {},
      } as Response;
    }) as unknown as typeof fetch;
    return { fn, calls };
  };

  it("ping resolves on a 2xx and sends Basic auth from the keys", async () => {
    const { fn, calls } = fakeFetch([{ match: "/api/public/traces", json: { data: [] } }]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    await expect(t.ping({ host: null, environmentId: "local" })).resolves.toBeUndefined();
    expect(calls[0]!.url).toBe("https://lf/api/public/traces?limit=1");
    expect(calls[0]!.auth).toBe(`Basic ${Buffer.from("pk:sk").toString("base64")}`);
  });

  it("ping rejects on a non-2xx WITHOUT leaking the URL/body", async () => {
    const { fn } = fakeFetch([{ match: "/api/public/traces", status: 403 }]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    await expect(t.ping({ host: null, environmentId: "local" })).rejects.toThrow(/status 403/);
    await expect(t.ping({ host: null, environmentId: "local" })).rejects.not.toThrow(/lf|pk|sk/);
  });

  it("promptLabelVersion returns the version as a string, or undefined when absent", async () => {
    const withVersion = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/v2/prompts/", json: { version: 9 } }]).fn,
    });
    expect(await withVersion.promptLabelVersion({ name: "p", label: "prod", host: null })).toBe("9");

    const noVersion = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/v2/prompts/", json: {} }]).fn,
    });
    expect(await noVersion.promptLabelVersion({ name: "p", label: "prod", host: null })).toBeUndefined();
  });

  it("lastRunPromptVersions locates the trace by the typeflux.workflow tag and reconstructs {name: version}", async () => {
    const { fn, calls } = fakeFetch([
      { match: "/api/public/traces?tags=", json: { data: [{ id: "trace-1" }] } },
      {
        match: "/api/public/traces/trace-1",
        json: {
          observations: [
            // CANONICAL nested shape (metadata.typeflux.activity_execution_manifest).
            { metadata: { typeflux: { activity_execution_manifest: { prompt_ref: { name: "assess" }, resolved_prompt_version: 3 } } } },
            // LEGACY flat dotted key + a bare-string prompt_ref (name@version).
            { metadata: { "typeflux.activity_execution_manifest": { prompt_ref: "summarize@2", resolved_prompt_version: 2 } } },
            { metadata: { other: "noise" } },
          ],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "3", summarize: "2" });
    // The trace lookup uses the low-cardinality workflow TAG, not a bare name (the trace name is
    // `TypefluxWorkflow:W`, which a `?name=W` query would miss).
    expect(calls[0]!.url).toContain(`tags=${encodeURIComponent("typeflux.workflow:W")}`);
  });

  it("lastRunPromptVersions also reads the workflow-level execution_manifest rollup (not only observations)", async () => {
    const { fn } = fakeFetch([
      { match: "/api/public/traces?tags=", json: { data: [{ id: "trace-2" }] } },
      {
        match: "/api/public/traces/trace-2",
        json: {
          // Versions live ONLY in the root trace's workflow rollup — no per-observation manifests.
          metadata: {
            typeflux: {
              execution_manifest: {
                activities: [
                  { prompt_ref: { name: "assess" }, resolved_prompt_version: 4 },
                  { prompt_ref: "decide@1", resolved_prompt_version: 1 },
                ],
              },
            },
          },
          observations: [{ metadata: { other: "noise" } }],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "4", decide: "1" });
  });

  it("a MIXED metadata bag (a typeflux object present but WITHOUT the manifest key) still finds the flat fallback", async () => {
    const { fn } = fakeFetch([
      { match: "/api/public/traces?tags=", json: { data: [{ id: "trace-3" }] } },
      {
        match: "/api/public/traces/trace-3",
        json: {
          observations: [
            {
              // `typeflux` object exists (e.g. carries join keys) but the manifest is under the flat key.
              metadata: {
                typeflux: { workflow: "W" },
                "typeflux.activity_execution_manifest": { prompt_ref: { name: "assess" }, resolved_prompt_version: 8 },
              },
            },
          ],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "8" });
  });

  it("on a rollup/observation conflict the AUTHORITATIVE workflow rollup wins (observations only fill gaps)", async () => {
    const { fn } = fakeFetch([
      { match: "/api/public/traces?tags=", json: { data: [{ id: "trace-4" }] } },
      {
        match: "/api/public/traces/trace-4",
        json: {
          metadata: {
            // Rollup is authoritative: `assess` = 9. A stale retry span below says 8 — must NOT win.
            typeflux: { execution_manifest: { activities: [{ prompt_ref: { name: "assess" }, resolved_prompt_version: 9 }] } },
          },
          observations: [
            { metadata: { typeflux: { activity_execution_manifest: { prompt_ref: { name: "assess" }, resolved_prompt_version: 8 } } } },
            // `summarize` is NOT in the rollup → the observation fills the gap.
            { metadata: { typeflux: { activity_execution_manifest: { prompt_ref: { name: "summarize" }, resolved_prompt_version: 2 } } } },
          ],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "9", summarize: "2" });
  });

  it("skips a newer lifecycle trace by NAME and uses the execution trace's manifest (#573)", async () => {
    const { fn } = fakeFetch([
      {
        match: "/api/public/traces?tags=",
        json: {
          data: [
            // Newest: a lifecycle trace sharing the tag — dropped by the name filter.
            { id: "lc-1", name: "TypefluxLifecycleSignal:typeflux_submit_review" },
            { id: "exec-1", name: "TypefluxWorkflow:W" },
          ],
        },
      },
      { match: "/api/public/traces/exec-1", json: { metadata: { typeflux: { execution_manifest: { activities: [{ prompt_ref: { name: "assess" }, resolved_prompt_version: 5 } ] } } } } },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "5" });
  });

  it("when the list omits names, SCANS past a manifest-less newest trace to the one with versions (#573)", async () => {
    const { fn } = fakeFetch([
      { match: "/api/public/traces?tags=", json: { data: [{ id: "lc-2" }, { id: "exec-2" }] } },
      // Newest candidate has no manifest → reconstruction is empty → scan continues.
      { match: "/api/public/traces/lc-2", json: { metadata: { typeflux: { lifecycle_operation: {} } }, observations: [] } },
      { match: "/api/public/traces/exec-2", json: { observations: [{ metadata: { typeflux: { activity_execution_manifest: { prompt_ref: { name: "decide" }, resolved_prompt_version: 4 } } } }] } },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ decide: "4" });
  });

  it("treats a JSON `null` trace name as absent (kept, not filtered out) (#573, Bugbot)", async () => {
    // A list payload commonly serializes an unnamed trace as `name: null` (not `undefined`); it must
    // be kept and reconstructed, not dropped as "not the execution trace".
    const { fn } = fakeFetch([
      { match: "/api/public/traces?tags=", json: { data: [{ id: "t-null", name: null }] } },
      { match: "/api/public/traces/t-null", json: { observations: [{ metadata: { typeflux: { activity_execution_manifest: { prompt_ref: { name: "assess" }, resolved_prompt_version: 3 } } } }] } },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ assess: "3" });
  });

  it("scans to a LATER page when the first full page is all lifecycle traces (bounded pagination) (#573, Bugbot)", async () => {
    // Page 1 is a full page (20) of lifecycle traces sharing the tag — all filtered by name, so the
    // scan must follow `meta.totalPages` to page 2 where the execution trace's manifest lives.
    const lifecycleRows = Array.from({ length: 20 }, (_unused, i) => ({ id: `lc-${i}`, name: "TypefluxLifecycleSignal:x" }));
    const { fn } = fakeFetch([
      { match: "limit=20&page=2", json: { data: [{ id: "exec-2", name: "TypefluxWorkflow:W" }], meta: { totalPages: 2 } } },
      { match: "limit=20&page=1", json: { data: lifecycleRows, meta: { totalPages: 2 } } },
      { match: "/api/public/traces/exec-2", json: { metadata: { typeflux: { execution_manifest: { activities: [{ prompt_ref: { name: "decide" }, resolved_prompt_version: 7 }] } } } } },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({ decide: "7" });
  });

  it("bounds the page scan at 5 pages (does not follow totalPages unboundedly)", async () => {
    // Every page claims 100 pages of lifecycle-only traces; the scan must stop at the cap and return {}
    // rather than paging forever.
    let listCalls = 0;
    const lifecycleRows = Array.from({ length: 20 }, (_unused, i) => ({ id: `lc-${i}`, name: "TypefluxLifecycleSignal:x" }));
    const fn = (async (url: string | URL) => {
      const u = String(url);
      if (u.includes("?tags=")) {
        listCalls += 1;
        return { ok: true, status: 200, json: async () => ({ data: lifecycleRows, meta: { totalPages: 100 } }) } as Response;
      }
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    }) as unknown as typeof fetch;
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({});
    expect(listCalls).toBe(5); // capped, not 100
  });

  it("a per-request timeout is applied: an aborting fetch rejects ping (so the probe degrades)", async () => {
    // The fake honors the AbortSignal by rejecting like a real timed-out fetch would.
    const abortingFetch = (async (_url: string | URL, init?: RequestInit) => {
      return await new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("timed out", "TimeoutError")));
      });
    }) as unknown as typeof fetch;
    const t = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {}, fetch: abortingFetch, timeoutMs: 20,
    });
    await expect(t.ping({ host: null, environmentId: "local" })).rejects.toBeInstanceOf(DOMException);
  });

  it("lastRunPromptVersions degrades to {} on no traces or an unexpected shape (never throws)", async () => {
    const empty = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/traces?tags=", json: { data: [] } }]).fn,
    });
    expect(await empty.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({});

    const broken = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/traces?tags=", status: 500 }]).fn,
    });
    expect(await broken.lastRunPromptVersions({ workflowName: "W", host: null })).toEqual({});
  });

  it("traceSummary matches a trace by execution id across the cross-edition metadata keys (#686)", async () => {
    const { fn, calls } = fakeFetch([
      {
        match: "/api/public/traces?limit=20",
        json: {
          data: [
            { id: "noise", metadata: { workflow_id: "someone-else" } },
            // The TS runtime's grouped-parent metadata: identity memo + BARE workflow_id (#681).
            { id: "trace-9", metadata: { typeflux_project: "p", workflow_id: "exec-1" } },
          ],
        },
      },
      {
        match: "/api/public/traces/trace-9",
        json: {
          name: "TypefluxWorkflow:W",
          timestamp: "2026-07-10T00:00:00.000Z",
          metadata: { workflow_id: "exec-1" },
          observations: [
            {
              metadata: {
                typeflux: {
                  activity_execution_manifest: {
                    activity_name: "assess",
                    prompt_ref: { name: "assess" },
                    resolved_prompt_version: 3,
                    provider_model: "gpt-4o-mini",
                  },
                },
              },
            },
            { metadata: { other: "noise" }, level: "ERROR" },
          ],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.traceSummary({ workflowId: "exec-1", host: null })).toEqual({
      trace_id: "trace-9",
      timestamp: "2026-07-10T00:00:00.000Z",
      status: "error", // an ERROR-level observation marks the trace (Python `_trace_status`)
      workflow_name: "TypefluxWorkflow:W",
      workflow_id: "exec-1",
      activities: ["assess"],
      prompt_refs: ["assess"],
      provider_models: ["gpt-4o-mini"],
      warnings: [],
    });
    // Exactly one detail fetch — only the matched row is followed.
    expect(calls.filter((c) => c.url.includes("/api/public/traces/")).map((c) => c.url)).toEqual([
      "https://lf/api/public/traces/trace-9",
    ]);
  });

  it("traceSummary also matches Python's nested typeflux join keys, and resolves null when nothing matches", async () => {
    const nested = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([
        {
          match: "/api/public/traces?limit=20",
          json: { data: [{ id: "trace-n", metadata: { typeflux: { temporal: { workflow_id: "exec-2" } } } }] },
        },
        { match: "/api/public/traces/trace-n", json: { metadata: {}, observations: [] } },
      ]).fn,
    });
    expect(await nested.traceSummary({ workflowId: "exec-2", host: null })).toMatchObject({
      trace_id: "trace-n",
      status: "ok",
      workflow_id: "exec-2",
    });

    const none = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/traces?limit=20", json: { data: [] } }]).fn,
    });
    expect(await none.traceSummary({ workflowId: "exec-3", host: null })).toBeNull();
  });

  it("traceSummary falls back to OBSERVATION metadata for worker-only traced runs (#686)", async () => {
    // A control-plane-started run has no caller-side parent metadata: the join
    // key lives only on the activity spans' manifest metadata, which the list
    // endpoint does not return — the newest page's details are followed.
    const { fn, calls } = fakeFetch([
      { match: "/api/public/traces?limit=20", json: { data: [{ id: "worker-run", metadata: {} }] } },
      {
        match: "/api/public/traces/worker-run",
        json: {
          name: "TypefluxWorkflow:W",
          observations: [
            {
              metadata: {
                typeflux: {
                  activity_execution_manifest: {
                    activity_name: "assess",
                    workflow_id: "exec-w1",
                    prompt_ref: { name: "assess" },
                    resolved_prompt_version: 1,
                  },
                },
              },
            },
          ],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    expect(await t.traceSummary({ workflowId: "exec-w1", host: null })).toMatchObject({
      trace_id: "worker-run",
      workflow_id: "exec-w1",
      activities: ["assess"],
    });
    // The detail fetch happened in the FALLBACK pass (after the list scan found no trace-level match).
    expect(calls.filter((c) => c.url.includes("/api/public/traces/worker-run"))).toHaveLength(1);
  });

  it("traceSummary REJECTS on a failed list (the correlation route degrades, unlike the drift tier)", async () => {
    const t = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/traces?limit=20", status: 500 }]).fn,
    });
    await expect(t.traceSummary({ workflowId: "exec-4", host: null })).rejects.toThrow(/status 500/);
  });

  // --- searchEnforcementTraces (#723 slice 2) ------------------------------
  const window = { since: new Date("2026-07-01T00:00:00Z"), until: new Date("2026-07-08T00:00:00Z") };

  it("bounds on OBSERVATION start time (queries /observations, not /traces) — surfaces a long-run trace whose ROOT predates the window (#723 P0-2)", async () => {
    const { fn, calls } = fakeFetch([
      { match: "/api/public/observations", json: { data: [{ traceId: "old-run" }] } },
      {
        match: "/api/public/traces/old-run",
        json: {
          timestamp: "2026-01-01T00:00:00+00:00", // root trace far BEFORE the window…
          name: "TypefluxWorkflow:W",
          metadata: { workflow_id: "exec-old" }, // …execution id only in the FLAT key (P1-7).
          observations: [{ startTime: "2026-07-05T00:00:00+00:00", metadata: { typeflux_moderation: { decision: "block", categories: ["hate"] } } }],
        },
      },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    const records = await t.searchEnforcementTraces({ environmentId: "prod", ...window, limit: 50, host: null });
    const obs = calls.find((c) => c.url.includes("/api/public/observations"))!;
    const params = new URL(obs.url).searchParams;
    // The WINDOW bounds the observations query (start-time), not a trace-timestamp query.
    expect(params.get("fromStartTime")).toBe("2026-07-01T00:00:00.000Z");
    expect(params.get("toStartTime")).toBe("2026-07-08T00:00:00.000Z");
    // P0-1: the typeflux environment id is NOT forwarded as Langfuse's native `environment=` param.
    expect(params.get("environment")).toBeNull();
    expect(calls.some((c) => c.url.includes("/api/public/traces?"))).toBe(false); // never the trace-timestamp list
    // The old-root trace is surfaced (its in-window observation was found), execution id from the flat key.
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({ traceId: "old-run", workflowId: "exec-old", workflowName: "W", timestamp: "2026-01-01T00:00:00+00:00" });
    expect(records[0]!.observations[0]!.metadata).toMatchObject({ typeflux_moderation: { decision: "block" } });
  });

  it("dedupes trace ids across the observations page and fetches each detail once (bounded, not per-observation) (#723 P1-8)", async () => {
    const { fn, calls } = fakeFetch([
      { match: "/api/public/observations", json: { data: [{ traceId: "a" }, { traceId: "a" }, { traceId: "b" }] } },
      { match: "/api/public/traces/a", json: { observations: [] } },
      { match: "/api/public/traces/b", json: { observations: [] } },
    ]);
    const t = fetchLangfuseTransport({ publicKey: "pk", secretKey: "sk", host: "https://lf", fetch: fn, env: {} });
    const records = await t.searchEnforcementTraces({ environmentId: null, ...window, limit: 50, host: null });
    expect(records.map((r) => r.traceId)).toEqual(["a", "b"]);
    expect(calls.filter((c) => c.url.includes("/api/public/traces/"))).toHaveLength(2); // `a` fetched once despite two observations
  });

  it("REJECTS (feed degrades to unreachable) on a failed observations list", async () => {
    const t = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([{ match: "/api/public/observations", status: 503 }]).fn,
    });
    await expect(t.searchEnforcementTraces({ environmentId: null, ...window, limit: 50, host: null })).rejects.toThrow(/status 503/);
  });

  it("REJECTS (feed degrades to unreachable) when a detail fetch fails", async () => {
    const t = fetchLangfuseTransport({
      publicKey: "pk", secretKey: "sk", host: "https://lf", env: {},
      fetch: fakeFetch([
        { match: "/api/public/observations", json: { data: [{ traceId: "x" }] } },
        { match: "/api/public/traces/x", status: 500 },
      ]).fn,
    });
    await expect(t.searchEnforcementTraces({ environmentId: null, ...window, limit: 50, host: null })).rejects.toThrow(/status 500/);
  });

  it("reports enforcementCredentialsConfigured from host + public + secret (Python `_langfuse_env_configured`) (#723 P1-3)", () => {
    const cfg = (opts: Parameters<typeof fetchLangfuseTransport>[0]) => fetchLangfuseTransport(opts).enforcementCredentialsConfigured;
    expect(cfg({ publicKey: "pk", secretKey: "sk", host: "https://lf", env: {} })).toBe(true);
    expect(cfg({ publicKey: "", secretKey: "sk", host: "https://lf", env: {} })).toBe(false); // no public key
    expect(cfg({ publicKey: "pk", secretKey: "sk", env: {} })).toBe(false); // no host anywhere
    expect(cfg({ publicKey: "pk", secretKey: "sk", env: { LANGFUSE_BASE_URL: "https://base" } })).toBe(true); // host from env
  });
});
