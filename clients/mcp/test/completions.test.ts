/**
 * Completions (#326 Phase 2; design §4). Proves the ID completers return the right candidates from
 * the list endpoints, narrow to the typed value, are project-scope aware, and yield NOTHING on a
 * degraded backend — driven both as a unit (makeCompleters) and end-to-end through a real MCP client
 * against BOTH a prompt argument and a resource-template variable.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { afterEach, describe, expect, it } from "vitest";

import type { Backend } from "../src/backend.js";
import { makeCompleters, narrow } from "../src/completions.js";
import { loadConfig } from "../src/config.js";
import { createTypefluxMcpServer, type BackendProvider } from "../src/server.js";

/** A control plane whose list endpoints return the given ids; records the project it was built for. */
function stubCp(project: string): Backend["controlPlane"] {
  return {
    workflows: async () => [{ id: `${project}-wfA` }, { id: `${project}-wfB` }, { id: `${project}-other` }],
    environments: async () => [{ id: "local" }, { id: "staging" }],
    policies: async () => [{ id: "base" }, { id: "strict" }],
    projects: async () => [{ id: "demo" }, { id: "demo2" }],
    deployments: async () => [{ plan_id: "plan-1" }, { plan_id: "plan-2" }],
  } as unknown as Backend["controlPlane"];
}

function stubBackend(options: { degraded?: boolean } = {}): Backend {
  const controlPlane = stubCp("default");
  return {
    mode: "attach",
    controlPlane,
    scopedControlPlane: (project: string) => stubCp(project),
    meta: options.degraded ? undefined : ({ project: "demo" } as never),
    capabilities: undefined,
    degraded: options.degraded ?? false,
    baseUrl: "http://127.0.0.1:0",
    dispose: async () => undefined,
  };
}

describe("narrow (candidate ranking)", () => {
  it("returns prefix matches first, then substring, de-duplicated", () => {
    expect(narrow("wf", ["wfA", "other-wf", "wfB", "wfA"])).toEqual(["wfA", "wfB", "other-wf"]);
  });
  it("returns the full list (capped) for an empty value", () => {
    expect(narrow("", ["a", "b"])).toEqual(["a", "b"]);
  });
  it("is case-insensitive", () => {
    expect(narrow("STAG", ["local", "staging"])).toEqual(["staging"]);
  });
});

describe("makeCompleters (unit)", () => {
  it("completes each ID from its list endpoint", async () => {
    const c = makeCompleters(async () => stubBackend());
    expect(await c.workflow_id("")).toEqual(["default-wfA", "default-wfB", "default-other"]);
    expect(await c.environment_id("stag")).toEqual(["staging"]);
    expect(await c.policy_id("")).toEqual(["base", "strict"]);
    expect(await c.project("")).toEqual(["demo", "demo2"]);
    expect(await c.plan_id("plan")).toEqual(["plan-1", "plan-2"]);
    expect(await c.plan_id("plan-2")).toEqual(["plan-2"]);
  });

  it("scopes candidates to the already-filled project argument", async () => {
    const c = makeCompleters(async () => stubBackend());
    expect(await c.workflow_id("", { arguments: { project: "p2" } })).toEqual([
      "p2-wfA",
      "p2-wfB",
      "p2-other",
    ]);
  });

  it("completes NOTHING on a degraded backend (design §4)", async () => {
    const c = makeCompleters(async () => stubBackend({ degraded: true }));
    expect(await c.workflow_id("")).toEqual([]);
    expect(await c.project("")).toEqual([]);
    expect(await c.plan_id("")).toEqual([]);
  });

  it("completes NOTHING when backend resolution throws (never surfaces an error)", async () => {
    const c = makeCompleters(async () => {
      throw new Error("no control plane");
    });
    expect(await c.environment_id("")).toEqual([]);
  });

  it("forVariable resolves the design §4 variables and nothing else", () => {
    const c = makeCompleters(async () => stubBackend());
    for (const name of ["workflow_id", "environment_id", "policy_id", "project", "plan_id"]) {
      expect(c.forVariable(name)).toBeTypeOf("function");
    }
    expect(c.forVariable("execution_id")).toBeUndefined();
    expect(c.forVariable("kind")).toBeUndefined();
  });
});

const disposers: Array<() => Promise<void>> = [];
afterEach(async () => {
  while (disposers.length) await disposers.pop()!();
});

async function connect(backend: Backend, prepare = false): Promise<Client> {
  const provider: BackendProvider = { get: async () => backend, dispose: async () => undefined };
  const built = createTypefluxMcpServer(loadConfig({}), { backend: provider });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test", version: "0" });
  await Promise.all([built.server.connect(serverTransport), client.connect(clientTransport)]);
  if (prepare) await built.prepare();
  disposers.push(async () => {
    await client.close();
    await built.dispose();
  });
  return client;
}

describe("completions through the MCP client (§4)", () => {
  it("completes a PROMPT argument (workflow_id on /typeflux:diagnose-run)", async () => {
    const client = await connect(stubBackend());
    const result = await client.complete({
      ref: { type: "ref/prompt", name: "typeflux:diagnose-run" },
      argument: { name: "workflow_id", value: "default-wf" },
    });
    expect(result.completion.values).toEqual(["default-wfA", "default-wfB"]);
  });

  it("completes a RESOURCE-TEMPLATE variable (workflow_id on the bundle template), project-scoped", async () => {
    const client = await connect(stubBackend());
    const result = await client.complete({
      ref: {
        type: "ref/resource",
        uri: "typeflux://{project}/workflows/{workflow_id}/bundle{?environment_id}",
      },
      argument: { name: "workflow_id", value: "" },
      context: { arguments: { project: "acme" } },
    });
    expect(result.completion.values).toEqual(["acme-wfA", "acme-wfB", "acme-other"]);
  });

  it("completes the {project} variable on a live resource template", async () => {
    const client = await connect(stubBackend());
    const result = await client.complete({
      ref: { type: "ref/resource", uri: "typeflux://{project}/meta" },
      argument: { name: "project", value: "demo" },
    });
    expect(result.completion.values).toEqual(["demo", "demo2"]);
  });

  it("a degraded backend completes nothing over the wire", async () => {
    const client = await connect(stubBackend({ degraded: true }), true);
    const result = await client.complete({
      ref: { type: "ref/prompt", name: "typeflux:diagnose-run" },
      argument: { name: "workflow_id", value: "" },
    });
    expect(result.completion.values).toEqual([]);
  });
});
