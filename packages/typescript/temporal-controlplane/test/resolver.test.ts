// The TypeScript in-process resolver (#619 slice 4): contract-surface pinning,
// the first manifest-path round trip in this package, and fail-closed gaps.

import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, describe, expect, it } from "vitest";
import * as z from "zod";

import { homedir } from "node:os";

import { InProcessTypescriptResolver, normalizeManifestPath } from "../src/resolver.js";
import { ProjectControlPlaneError } from "../src/errors.js";

const here = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(
  readFileSync(resolve(here, "../../../../contracts/resolver/resolver.v1.json"), "utf-8"),
) as { operations: Record<string, { params: Record<string, string> }> };

// The canonical conformance fixture project: its YAML dialect parses in both
// editions by parity design; the module refs are Python-binding values the
// TS loader carries as data.
const fixtureManifest = resolve(
  here,
  "../../../../contracts/controlplane/conformance/project/python/typeflux.project.yaml",
);

const CAMEL: Record<string, string> = {
  resolve_bundle: "resolveBundle",
  resolve_catalog: "resolveCatalog",
  validate_project: "validateProject",
  prompt_status: "promptStatus",
  resolve_plan: "resolvePlan",
};


const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** A minimal TS-dialect project (no activities.modules — Python-only, #496). */
function writeTsDialectProject(): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-resolver-"));
  createdDirs.push(dir);
  writeFileSync(
    join(dir, "typeflux.project.yaml"),
    "version: '1'\nname: ts-resolver-demo\nworkflows:\n  - id: workflow\n    path: workflow.yaml\nenvironments:\n  local: environments/local.yaml\n",
  );
  mkdirSync(join(dir, "environments"));
  writeFileSync(join(dir, "environments/local.yaml"), "version: '1'\nname: local\n");
  writeFileSync(
    join(dir, "workflow.yaml"),
    [
      "project: ts_resolver_demo",
      "name: ts_resolver_demo_yaml",
      "task_queue: ts-resolver-demo",
      "runtime:",
      "  temporal:",
      "    address: localhost:7233",
      "  registry:",
      "    type: inline",
      "    prompts:",
      "      assess: assess {{value}}",
      "  provider:",
      "    type: fake",
      "  observability:",
      "    type: none",
      "activities:",
      "  definitions:",
      "    - name: assess",
      "      input: schemas:Item",
      "      output: schemas:Item",
      "      prompt: assess",
      "workflow:",
      "  name: TsResolverDemoWorkflow",
      "  input: schemas:Item",
      "  output: schemas:Item",
      "  steps:",
      "    - id: assess",
      "      activity: assess",
      "",
    ].join("\n"),
  );
  return join(dir, "typeflux.project.yaml");
}

describe("InProcessTypescriptResolver (#619)", () => {
  it("covers exactly the contract's operations", () => {
    const resolver = new InProcessTypescriptResolver();
    for (const operation of Object.keys(contract.operations)) {
      const method = CAMEL[operation];
      expect(method, operation).toBeDefined();
      expect(typeof (resolver as never as Record<string, unknown>)[method as string]).toBe(
        "function",
      );
    }
    expect(resolver.runtime).toBe("typescript");
  });

  it("validates a project from a manifest path (full binding)", async () => {
    const resolver = new InProcessTypescriptResolver();
    const report = await resolver.validateProject(fixtureManifest, {
      environmentId: "local",
      workflowIds: ["workflow"],
    });
    expect(report.project_name).toBe("conformance-fixture");
    expect(Array.isArray(report.issues)).toBe(true);
  });

  it("resolves a bundle when schemas are injected (TS-dialect project)", async () => {
    const manifest = writeTsDialectProject();
    const resolver = new InProcessTypescriptResolver({
      schemas: { "schemas:Item": z.object({ value: z.string() }) },
    });
    const bundle = await resolver.resolveBundle(manifest, {
      workflowId: "workflow",
      environmentId: "local",
    });
    expect(bundle.project.name).toBe("ts-resolver-demo");
    expect(bundle.workflow.id).toBe("workflow");
    // The ts-plan-argument profile: the registered type is the constant
    // generic workflow — identity lives in the memo, not the type name
    // (contracts/temporal-binding). The resolver surfaces exactly that.
    expect(bundle.workflow.workflow_type).toBe("typefluxYamlWorkflow");
    expect(bundle.workflow.workflow_name).toBe("TsResolverDemoWorkflow");
  });

  it("fails closed without schemas instead of emitting stub garbage", async () => {
    // A TS-dialect project that PARSES — so the failure exercised here is
    // genuinely the missing-schemas 422, not a spec-dialect rejection.
    const manifest = writeTsDialectProject();
    const resolver = new InProcessTypescriptResolver();
    await expect(
      resolver.resolveBundle(manifest, { workflowId: "workflow", environmentId: "local" }),
    ).rejects.toThrowError(/schema/i);
  });

  it("consumes deployment_image into a deployment_preview (#687)", async () => {
    const manifest = writeTsDialectProject();
    const resolver = new InProcessTypescriptResolver({
      schemas: { "schemas:Item": z.object({ value: z.string() }) },
    });
    // No image: the copyable promote command; WITH an image: the secret-free preview (they are
    // mutually exclusive, Python exclude_none parity).
    const plain = await resolver.resolveBundle(manifest, { workflowId: "workflow", environmentId: "local" });
    expect(plain).toHaveProperty("deployment_preview_reference");
    expect(plain).not.toHaveProperty("deployment_preview");
    const previewed = await resolver.resolveBundle(manifest, {
      workflowId: "workflow",
      environmentId: "local",
      deploymentImage: "registry.example/image@sha256:" + "a".repeat(64),
    });
    expect(previewed).toHaveProperty("deployment_preview");
    expect(previewed).not.toHaveProperty("deployment_preview_reference");
  });

  it("normalizes manifest paths incl. home expansion (Bugbot catch)", () => {
    expect(normalizeManifestPath("~/projects/typeflux.project.yaml")).toBe(
      join(homedir(), "projects/typeflux.project.yaml"),
    );
    expect(normalizeManifestPath("~")).toBe(homedir());
    expect(normalizeManifestPath("relative/x.yaml")).toContain("/relative/x.yaml");
  });

  it("prompt_status binds the drift projection for an inline registry (#639)", async () => {
    const manifest = writeTsDialectProject();
    const resolver = new InProcessTypescriptResolver({
      schemas: { "schemas:Item": z.object({ value: z.string() }) },
    });
    const status = await resolver.promptStatus(manifest, {
      workflowId: "workflow",
      environmentId: "local",
    });
    expect(status).toEqual({
      workflow_id: "workflow",
      environment_id: "local",
      registry_type: "inline",
      prompts: [
        {
          name: "assess",
          mode: "inline",
          selector: "inline",
          status: "in_sync",
          used_by_activities: ["assess"],
          template: "assess {{value}}",
        },
      ],
    });
  });
});
