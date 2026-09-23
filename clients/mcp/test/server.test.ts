/**
 * Server wiring tests (#326): resources and tools registered against a STUBBED backend, driven
 * through a real MCP client over an in-memory transport. Proves the read-shape, the structured
 * error mapping (item 3: error in `content`, no `structuredContent`), and the §9 degrade (item 5:
 * a 403-on-/meta backend exposes only the static surface) — all without a live control plane.
 */

import { readFileSync } from "node:fs";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { afterEach, describe, expect, it } from "vitest";

import type { Backend } from "../src/backend.js";
import { loadConfig } from "../src/config.js";
import { ApiRequestError, describeError } from "../src/control-plane/errors.js";
import { createTypefluxMcpServer, type BackendProvider } from "../src/server.js";

/** A stub control plane: canned reads, and a bundle that 501s to exercise the error path. */
function stubControlPlane(): Backend["controlPlane"] {
  return {
    meta: async () => ({ project: "demo", runtime: "typescript", capabilities: { can_resolve: true } }),
    workflows: async () => [{ id: "wf", name: "Demo" }],
    environments: async () => [{ id: "local" }],
    policies: async () => [{ id: "base" }],
    annotations: async () => [{ insight_id_pattern: "policy.drift.*", reason: "tracked upstream" }],
    profiles: async () => [],
    projects: async () => [{ id: "demo", default: true }],
    deployments: async () => [],
    githubProvenance: async () => ({ plans: [], partial: { github: "not_configured" } }),
    validate: async () => ({ ok: true, issues: [] }),
    bundle: async () => {
      throw new ApiRequestError(
        describeError(501, { error: "UnsupportedRuntime", message: "cannot resolve python" }),
      );
    },
  } as unknown as Backend["controlPlane"];
}

function stubBackend(options: { degraded?: boolean } = {}): Backend {
  const controlPlane = stubControlPlane();
  return {
    mode: "managed-local",
    controlPlane,
    scopedControlPlane: () => controlPlane,
    meta: options.degraded ? undefined : ({ project: "demo" } as never),
    capabilities: undefined,
    degraded: options.degraded ?? false,
    baseUrl: "http://127.0.0.1:0",
    dispose: async () => undefined,
  };
}

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

describe("MCP server surface (stubbed backend)", () => {
  it("SERVER_VERSION matches package.json (the published handshake identity)", async () => {
    const { SERVER_VERSION } = await import("../src/server.js");
    const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as { version: string };
    expect(SERVER_VERSION).toBe(pkg.version);
  });


  it("registers the read tools with readOnly/closed-world hints", async () => {
    const client = await connect(stubBackend());
    const { tools } = await client.listTools();
    const byName = new Map(tools.map((t) => [t.name, t]));
    expect(byName.has("list_workflows")).toBe(true);
    expect(byName.has("get_bundle")).toBe(true);
    expect(byName.get("list_workflows")!.annotations?.readOnlyHint).toBe(true);
    expect(byName.get("list_workflows")!.annotations?.openWorldHint).toBe(false);
    // Phase-3 local authoring aids are registered alongside the read/operate tiers.
    for (const local of ["scaffold_ai_activity", "scaffold_workflow_yaml", "scaffold_project_entry", "doctor"]) {
      expect(byName.has(local)).toBe(true);
    }
  });

  it("validate_project / get_bundle advertise the API-supported scoping args (item 6)", async () => {
    const client = await connect(stubBackend());
    const { tools } = await client.listTools();
    const validate = tools.find((t) => t.name === "validate_project")!;
    expect(Object.keys(validate.inputSchema.properties ?? {})).toEqual(
      expect.arrayContaining(["environment_id", "workflow_id", "policy_id"]),
    );
    const bundle = tools.find((t) => t.name === "get_bundle")!;
    expect(Object.keys(bundle.inputSchema.properties ?? {})).toEqual(
      expect.arrayContaining(["workflow_id", "environment_id", "policy_id", "deployment_image"]),
    );
  });

  it("a read tool returns structured content from the control plane", async () => {
    const client = await connect(stubBackend());
    const result = await client.callTool({ name: "list_workflows", arguments: {} });
    expect(result.isError).toBeFalsy();
    expect((result.structuredContent as { workflows: unknown[] }).workflows).toEqual([
      { id: "wf", name: "Demo" },
    ]);
  });

  it("get_github_provenance wraps the control plane surface, partial marker included (#727)", async () => {
    const client = await connect(stubBackend());
    const result = await client.callTool({ name: "get_github_provenance", arguments: {} });
    expect(result.isError).toBeFalsy();
    expect(result.structuredContent as { github_provenance: unknown }).toEqual({
      github_provenance: { plans: [], partial: { github: "not_configured" } },
    });
  });

  it("list_annotations wraps the in-repo insight-ack projection (#733)", async () => {
    const client = await connect(stubBackend());
    const result = await client.callTool({ name: "list_annotations", arguments: {} });
    expect(result.isError).toBeFalsy();
    expect((result.structuredContent as { annotations: unknown[] }).annotations).toEqual([
      { insight_id_pattern: "policy.drift.*", reason: "tracked upstream" },
    ]);
  });

  it("a failing read maps to a structured tool error in content, with NO structuredContent (item 3)", async () => {
    const client = await connect(stubBackend());
    const result = await client.callTool({
      name: "get_bundle",
      arguments: { workflow_id: "wf", environment_id: "local" },
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

  it("serves the live collection resources under the {project} scheme as application/json", async () => {
    const client = await connect(stubBackend());
    const read = await client.readResource({ uri: "typeflux://default/workflows" });
    expect(read.contents[0]!.mimeType).toBe("application/json");
    // A live resource returns the control plane's JSON verbatim (the array), unwrapped.
    expect(JSON.parse(read.contents[0]!.text as string)).toEqual([{ id: "wf", name: "Demo" }]);
  });

  it("a failing live resource returns a structured error body, not a crash", async () => {
    const client = await connect(stubBackend());
    const read = await client.readResource({
      uri: "typeflux://default/workflows/wf/bundle?environment_id=local",
    });
    const body = JSON.parse(read.contents[0]!.text as string) as { error: { code: string } };
    expect(body.error.code).toBe("UnsupportedRuntime");
  });

  it("serves the static docs / schema / guide resources without a backend", async () => {
    const client = await connect(stubBackend());
    const guide = await client.readResource({ uri: "typeflux://guide/authoring-checklist" });
    expect(guide.contents[0]!.text).toContain("authoring checklist");

    const schema = await client.readResource({ uri: "typeflux://schema/typeflux-yaml" });
    const parsed = JSON.parse(schema.contents[0]!.text as string) as { properties: Record<string, unknown> };
    expect(parsed.properties).toHaveProperty("workflow");

    const { resources } = await client.listResources();
    expect(resources.some((r) => r.uri === "typeflux://guide/authoring-checklist")).toBe(true);
  });

  it("serves the project-layout & adoption guide (#865)", async () => {
    const client = await connect(stubBackend());
    const guide = await client.readResource({ uri: "typeflux://guide/project-layout" });
    expect(guide.contents[0]!.mimeType).toBe("text/markdown");
    const text = guide.contents[0]!.text as string;
    // The three things the per-activity checklist does NOT cover: modes, layout, the engine pin.
    expect(text).toContain("authoring mode");
    expect(text).toContain("typeflux.project.yaml");
    expect(text).toContain("engine.lock");
    const { resources } = await client.listResources();
    expect(resources.some((r) => r.uri === "typeflux://guide/project-layout")).toBe(true);
  });

  it("serves the TypeScript-edition docs and examples under their own namespace (#862/#863)", async () => {
    const client = await connect(stubBackend());

    // The TS doc mirror resolves under docs/typescript/ …
    const tutorial = await client.readResource({ uri: "typeflux://docs/typescript/tutorial" });
    expect(tutorial.contents[0]!.mimeType).toBe("text/markdown");
    expect(tutorial.contents[0]!.text).toContain("TypeScript");

    // … and does NOT leak into the language-neutral namespace: the neutral set has no `tutorial`.
    const missing = await client.readResource({ uri: "typeflux://docs/tutorial" });
    const body = JSON.parse(missing.contents[0]!.text as string) as { error: { code: string } };
    expect(body.error.code).toBe("NotFound");

    // A TS example blob is distilled with TypeScript fences (zod/defineActivity code shape).
    const example = await client.readResource({ uri: "typeflux://examples/typescript/policy-governed-review" });
    expect(example.contents[0]!.text).toContain("```typescript");
    expect(example.contents[0]!.text).toContain("policies/"); // governance examples keep their policies inline

    // Both namespaces enumerate side by side, distinguishable by URI.
    const { resources } = await client.listResources();
    expect(resources.some((r) => r.uri === "typeflux://docs/typescript/yaml")).toBe(true);
    expect(resources.some((r) => r.uri === "typeflux://docs/yaml")).toBe(true);
    expect(resources.some((r) => r.uri === "typeflux://examples/typescript/lifecycle-review")).toBe(true);

    const { resourceTemplates } = await client.listResourceTemplates();
    const templates = resourceTemplates.map((t) => t.uriTemplate);
    expect(templates).toEqual(
      expect.arrayContaining([
        "typeflux://docs/typescript/{slug}",
        "typeflux://examples/typescript/{name}",
      ]),
    );

  });

  it("degrades to STATIC discovery only when the caller lacks inspect (403 on /meta) — item 5", async () => {
    const client = await connect(stubBackend({ degraded: true }), true);

    // No LIVE tools survive (every read/operate tool is a live CP call). The local Phase-3 authoring
    // aids remain — they are workspace-local (a scaffold is offline; doctor tells you WHY it's down).
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual(
      ["doctor", "scaffold_ai_activity", "scaffold_project_entry", "scaffold_workflow_yaml"].sort(),
    );

    // resources/list shows only the static set (docs/examples/schema enumerations + the guide),
    // never a live resource like the projects registry.
    const { resources } = await client.listResources();
    expect(resources.some((r) => r.uri === "typeflux://projects")).toBe(false);
    expect(resources.some((r) => r.uri === "typeflux://guide/authoring-checklist")).toBe(true);

    // resources/templates/list shows only static templates (docs/examples/schema), no live ones.
    const { resourceTemplates } = await client.listResourceTemplates();
    const templates = resourceTemplates.map((t) => t.uriTemplate);
    expect(templates).toEqual(
      expect.arrayContaining(["typeflux://docs/{slug}", "typeflux://schema/{name}"]),
    );
    expect(templates.some((t) => t.includes("{project}"))).toBe(false);
  });
});
