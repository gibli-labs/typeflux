/**
 * The Phase-0 end-to-end proof (#326; design §10 "makes an agent fluent, zero governance risk").
 * Spins the REAL TypeScript control plane in-process (managed-local) against a real example project
 * — the canonical conformance fixture — connects the actual MCP server over an in-memory transport,
 * and reads meta + a bundle resource + runs validate_project, asserting real structured results.
 * A second setup drives a python-runtime project to prove a real 501 UnsupportedRuntime tool call
 * comes back as a well-formed structured error (item 3).
 *
 * The conformance fixture's activity IO schemas are injected as the managed-local `schemas` seam so
 * bundle/catalog fully resolve (see managed-local.ts SCHEMAS CAVEAT). Requires the monorepo TS
 * packages to be built (pnpm -r --filter ./packages/typescript/** build).
 */

import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

// Resolved via the vitest alias to the built conformance-schema fixture.
import { CONFORMANCE_SCHEMAS } from "@tf/conformance-schemas";

import { LazyBackend } from "../src/backend.js";
import { loadConfig } from "../src/config.js";
import { createTypefluxMcpServer } from "../src/server.js";

const MANIFEST = fileURLToPath(
  new URL(
    "../../../contracts/controlplane/conformance/project/typescript/typeflux.project.yaml",
    import.meta.url,
  ),
);
const CP_DIST = fileURLToPath(
  new URL("../../../packages/typescript/temporal-controlplane/dist/index.js", import.meta.url),
);

const ready = existsSync(CP_DIST);

/** Start a managed-local MCP server for the given env, connected to a client. */
async function startServer(env: Record<string, string>) {
  const config = loadConfig(env);
  const backend = new LazyBackend(config, { managedSchemas: CONFORMANCE_SCHEMAS });
  const built = createTypefluxMcpServer(config, { backend });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "e2e", version: "0" });
  await Promise.all([built.server.connect(serverTransport), client.connect(clientTransport)]);
  await built.prepare();
  return {
    client,
    dispose: async () => {
      await client.close();
      await built.dispose();
    },
  };
}

describe.skipIf(!ready)("end-to-end: managed-local + MCP server + real control plane", () => {
  let client: Client;
  let dispose: () => Promise<void>;

  beforeAll(async () => {
    ({ client, dispose } = await startServer({ TYPEFLUX_MANIFEST: MANIFEST }));
  });

  afterAll(async () => {
    await dispose?.();
  });

  it("advertises server instructions and the read tools", async () => {
    const { tools } = await client.listTools();
    expect(tools.length).toBeGreaterThanOrEqual(15);
    expect(tools.map((t) => t.name)).toContain("validate_project");
  });

  it("reads meta from the real in-process control plane", async () => {
    const read = await client.readResource({ uri: "typeflux://default/meta" });
    const meta = JSON.parse(read.contents[0]!.text as string) as {
      project: string;
      runtime: string;
      capabilities: Record<string, boolean>;
    };
    expect(meta.project).toBe("conformance-fixture");
    expect(meta.runtime).toBe("typescript");
    expect(meta.capabilities.can_resolve).toBe(true);
  });

  it("resolves a real bundle resource (fully resolved with injected schemas)", async () => {
    const read = await client.readResource({
      uri: "typeflux://default/workflows/workflow/bundle?environment_id=local",
    });
    const body = JSON.parse(read.contents[0]!.text as string) as {
      workflow?: { id: string };
      topology?: { nodes: unknown[] };
      error?: unknown;
    };
    expect(body.error).toBeUndefined();
    expect(body.topology?.nodes.length).toBeGreaterThan(0);
  });

  it("resolves a real bundle via the get_bundle tool with structured content", async () => {
    const result = await client.callTool({
      name: "get_bundle",
      arguments: { workflow_id: "workflow", environment_id: "local" },
    });
    expect(result.isError).toBeFalsy();
    const bundle = (result.structuredContent as { bundle: { workflow: { id: string } } }).bundle;
    expect(bundle.workflow.id).toBe("workflow");
  });

  it("runs validate_project and returns a real structured report", async () => {
    const result = await client.callTool({
      name: "validate_project",
      arguments: { environment_id: "local" },
    });
    expect(result.isError).toBeFalsy();
    const report = (result.structuredContent as { report: Record<string, unknown> }).report;
    expect(report).toBeTypeOf("object");
    expect(Object.keys(report).length).toBeGreaterThan(0);
  });

  it("projects topology from the bundle as its own resource", async () => {
    const read = await client.readResource({
      uri: "typeflux://default/workflows/workflow/topology?environment_id=local",
    });
    const topology = JSON.parse(read.contents[0]!.text as string) as { nodes: unknown[]; edges: unknown[] };
    expect(Array.isArray(topology.nodes)).toBe(true);
    expect(Array.isArray(topology.edges)).toBe(true);
  });

  it("autocompletes workflow_id and environment_id against the REAL project (§4)", async () => {
    const wf = await client.complete({
      ref: { type: "ref/prompt", name: "typeflux:diagnose-run" },
      argument: { name: "workflow_id", value: "" },
    });
    expect(wf.completion.values).toContain("workflow");

    const env = await client.complete({
      ref: { type: "ref/resource", uri: "typeflux://{project}/workflows/{workflow_id}/bundle{?environment_id}" },
      argument: { name: "environment_id", value: "lo" },
    });
    expect(env.completion.values).toContain("local");
  });

  it("registers the six recipes and renders prepare-deployment against the live CP (§7)", async () => {
    const { prompts } = await client.listPrompts();
    expect(prompts.map((p) => p.name)).toEqual(
      expect.arrayContaining([
        "typeflux:scaffold-activity",
        "typeflux:add-workflow",
        "typeflux:diagnose-run",
        "typeflux:review-gate",
        "typeflux:prepare-deployment",
        "typeflux:port-to-ts-edition",
      ]),
    );

    // prepare-deployment composes a real (pure-YAML) validate + deployments read and shows the
    // never-promote rule — no Temporal-tier probe, so it renders fast.
    const prep = await client.getPrompt({
      name: "typeflux:prepare-deployment",
      arguments: { environment_id: "local" },
    });
    const prepText = (prep.messages[0]!.content as { text: string }).text;
    expect(prepText).toContain("### validation");
    expect(prepText.toLowerCase()).toContain("never promote");
  });

  it("renders diagnose-run against the live CP, embedding the signals and disclosing the trace gap (§7)", async () => {
    // With no Temporal, the status/workers probes answer 503 within the API's 10s bound; the recipe
    // embeds each as a structured signal note (never throws) and still discloses the trace_diff gap.
    const diag = await client.getPrompt({
      name: "typeflux:diagnose-run",
      arguments: { workflow_id: "workflow", environment_id: "local", execution_id: "no-such-run" },
    });
    const diagText = (diag.messages[0]!.content as { text: string }).text;
    expect(diagText).toContain("### status");
    expect(diagText).toContain("### connections");
    expect(diagText).toContain("### workers");
    expect(diagText).toContain("### prompt-status");
    expect(diagText).toContain("trace_diff");
  }, 30_000);

  it("doctor reports the live control plane up (managed-local, resolvable) — §6.4", async () => {
    const result = await client.callTool({ name: "doctor", arguments: {} });
    expect(result.isError).toBeFalsy();
    const report = result.structuredContent as {
      ready: boolean;
      mode: string;
      checks: { id: string; status: string; detail: string }[];
    };
    expect(report.mode).toBe("managed-local");
    const cp = report.checks.find((c) => c.id === "control_plane")!;
    // The real in-process CP resolves this TS project, so the ping is a clean 'ok'.
    expect(cp.status).toBe("ok");
    expect(cp.detail).toContain("conformance-fixture");
    // No secret value is ever echoed into the checklist.
    for (const check of report.checks) expect(check.detail).not.toMatch(/sk-[A-Za-z0-9]/);
  }, 20_000);

  it("scaffold_ai_activity returns valid, applyable content (never writes) — §6.4", async () => {
    const result = await client.callTool({
      name: "scaffold_ai_activity",
      arguments: {
        name: "assess_item",
        input_fields: [{ name: "value", type: "string" }],
        output_fields: [{ name: "score", type: "number" }],
        style: "yaml",
      },
    });
    expect(result.isError).toBeFalsy();
    const structured = result.structuredContent as { content: string; wrote_file: boolean; style: string };
    expect(structured.wrote_file).toBe(false);
    expect(structured.style).toBe("yaml");
    expect(structured.content).toContain("- name: assess_item");
    expect(structured.content).toContain("input: schemas:AssessItemInput");
  });
});

describe.skipIf(!ready)("end-to-end: a real 501 UnsupportedRuntime is a well-formed structured tool error", () => {
  let client: Client;
  let dispose: () => Promise<void>;

  beforeAll(async () => {
    // Declaring the (TS) fixture as a python runtime makes the control plane treat it as
    // unresolvable: pure-YAML reads stay up, resolution-dependent reads answer 501.
    ({ client, dispose } = await startServer({ TYPEFLUX_MANIFEST: MANIFEST, TYPEFLUX_RUNTIME: "python" }));
  });

  afterAll(async () => {
    await dispose?.();
  });

  it("meta still resolves (pure-YAML read stays up) and reports can_resolve=false", async () => {
    const read = await client.readResource({ uri: "typeflux://default/meta" });
    const meta = JSON.parse(read.contents[0]!.text as string) as { capabilities: Record<string, boolean> };
    expect(meta.capabilities.can_resolve).toBe(false);
  });

  it("get_bundle returns isError + a structured UnsupportedRuntime error the client can read", async () => {
    const result = await client.callTool({
      name: "get_bundle",
      arguments: { workflow_id: "workflow", environment_id: "local" },
    });
    expect(result.isError).toBe(true);
    expect(result.structuredContent).toBeUndefined();
    const error = JSON.parse((result.content as { type: string; text: string }[])[0]!.text) as {
      code: string;
      status: number;
    };
    expect(error.code).toBe("UnsupportedRuntime");
    expect(error.status).toBe(501);
  });
});
