import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.resetModules(); });

it("preserves a scoped mutation's body, bearer, request options and abort signal", async () => {
  const values: Record<string, string> = { "typeflux.apiBase": "http://example.test",
    "typeflux.project": "project one", "typeflux.apiToken": "fixture-token" };
  vi.stubGlobal("localStorage", { getItem: (key: string) => values[key] ?? null });
  const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    requests.push(request);
    return new Response(null, { status: 204 });
  }));
  const { client } = await import("./api");
  const controller = new AbortController();
  const body = { environment_id: "local", execution_id: "fixture-execution",
    run_id: "fixture-run", policy_ids: [], command: { user_decision: "approve", notes: "fixed fixture" } };
  await client.POST("/api/v1/workflows/{workflow_id}/review", {
    params: { path: { workflow_id: "workflow" } }, body, signal: controller.signal,
    headers: { "x-fixture": "kept" }, credentials: "include", cache: "no-store",
    redirect: "manual", referrerPolicy: "no-referrer", keepalive: true,
  });
  expect(requests).toHaveLength(1);
  const request = requests[0]!;
  expect(request.url).toBe("http://example.test/api/v1/projects/project%20one/workflows/workflow/review");
  expect(request.method).toBe("POST");
  expect(request.headers.get("authorization")).toBe("Bearer fixture-token");
  expect(request.headers.get("content-type")).toBe("application/json");
  expect(request.headers.get("x-fixture")).toBe("kept");
  expect(await request.json()).toEqual(body);
  expect([request.credentials, request.cache, request.redirect, request.referrerPolicy, request.keepalive])
    .toEqual(["include", "no-store", "manual", "no-referrer", true]);
  expect(request.signal.aborted).toBe(false);
  controller.abort();
  expect(request.signal.aborted).toBe(true);
});

it("keeps scoped GETs bodyless and registry discovery unscoped", async () => {
  vi.stubGlobal("localStorage", { getItem: (key: string) => ({ "typeflux.apiBase": "http://example.test",
    "typeflux.project": "fixture" })[key] ?? null });
  const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    requests.push(request);
    return new Response(JSON.stringify([]), { status: 200, headers: { "content-type": "application/json" } });
  }));
  const { client } = await import("./api");
  await client.GET("/api/v1/workflows");
  await client.GET("/api/v1/projects");
  expect(requests.map(request => ({ path: new URL(request.url).pathname, body: request.body }))).toEqual([
    { path: "/api/v1/projects/fixture/workflows", body: null }, { path: "/api/v1/projects", body: null },
  ]);
});

it("keeps a scoped GET bodyless when the Request.body getter is unavailable", async () => {
  vi.stubGlobal("localStorage", { getItem: (key: string) => ({ "typeflux.apiBase": "http://example.test",
    "typeflux.project": "fixture" })[key] ?? null });
  const requests: Request[] = [];
  vi.stubGlobal("fetch", vi.fn(async (request: Request) => {
    requests.push(request);
    expect((await request.arrayBuffer()).byteLength).toBe(0);
    return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
  }));
  vi.spyOn(Request.prototype, "body", "get").mockReturnValue(undefined as unknown as null);
  const { client } = await import("./api");
  await client.GET("/api/v1/workflows");
  expect(requests).toHaveLength(1);
  expect(requests[0]!.method).toBe("GET");
  expect(new URL(requests[0]!.url).pathname).toBe("/api/v1/projects/fixture/workflows");
});
