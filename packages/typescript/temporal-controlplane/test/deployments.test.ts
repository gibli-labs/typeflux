import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import {
  loadProjectBundle,
  projectSubworkflowResolver,
  resolveEnvironmentWorkflow,
  writeDeploymentPlan,
  type DeploymentPlanResolver,
} from "@typeflux/temporal-yaml";
import { afterAll, describe, expect, it } from "vitest";

import { ProjectControlPlane } from "../src/project-control-plane.js";
import { ProjectControlPlaneError } from "../src/errors.js";

const IMAGE = "registry.example/worker@sha256:" + "d".repeat(64);
const PINNED = new Date("2026-02-03T04:05:06.000Z");

const WORKFLOW = `
project: p
name: n
task_queue: wf-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
const MANIFEST = `
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
policies:
  only_mini: only_mini.policy.yaml
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [only_mini] }
`;
const POLICY = "name: only_mini\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

function project(): { manifest: string; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), "tf-cp-deploy-"));
  createdDirs.push(dir);
  for (const [relative, content] of Object.entries({
    "typeflux.project.yaml": MANIFEST,
    "review.yaml": WORKFLOW,
    "only_mini.policy.yaml": POLICY,
    "prod.env.yaml": "name: prod\n",
  })) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return { manifest: join(dir, "typeflux.project.yaml"), dir };
}

function planResolver(bundle: ReturnType<typeof loadProjectBundle>): DeploymentPlanResolver {
  const { project: proj, sources } = bundle;
  return (workflowId, environmentId) => {
    const environment = sources.environments[environmentId];
    const text = sources.workflows[workflowId];
    if (environment === undefined || text === undefined) return undefined;
    const spec = resolveEnvironmentWorkflow(text, { environment, workflowId, runtimeDefaults: proj.defaults.runtime });
    return { spec, subworkflows: projectSubworkflowResolver(workflowId, () => undefined) };
  };
}

describe("ProjectControlPlane deployments (#687 D687-5)", () => {
  it("lists written plans with live verification + a promote command", () => {
    const { manifest, dir } = project();
    const bundle = loadProjectBundle(manifest);
    const { plan } = writeDeploymentPlan(bundle.project, bundle.sources, planResolver(bundle), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });

    const cp = new ProjectControlPlane(bundle, { manifestPath: manifest });
    const entries = cp.deployments();
    expect(entries).toHaveLength(1);
    const entry = entries[0]!;
    expect(entry.plan_id).toBe(`review.prod.${plan.plan_hash.slice(0, 12)}`);
    expect(entry.verification.ok).toBe(true);
    expect(entry.path).toBe(`deployments/${entry.plan_id}.yaml`);
    expect(entry.promote_command).toBe(`typeflux-project deploy ${manifest} --apply deployments/${entry.plan_id}.yaml`);
    // Detail by id round-trips; an unknown id is a 404 with the Python-parity message.
    expect(cp.deployment(entry.plan_id).plan_id).toBe(entry.plan_id);
    try {
      cp.deployment("nope");
      throw new Error("expected a 404");
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectControlPlaneError);
      expect((error as ProjectControlPlaneError).status).toBe(404);
      expect((error as ProjectControlPlaneError).message).toBe("unknown deployment plan: nope");
    }
  });

  it("reports drift in the verification when the spec changes under a written plan", () => {
    const { manifest, dir } = project();
    const bundle = loadProjectBundle(manifest);
    writeDeploymentPlan(bundle.project, bundle.sources, planResolver(bundle), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    // Edit the workflow spec and reload → the plan's spec_digest drifts.
    writeFileSync(join(dir, "review.yaml"), WORKFLOW.replace("[{ id: s, activity: a }]", "[{ id: s, activity: a }, { id: s2, activity: a }]"));
    const reloaded = loadProjectBundle(manifest);
    const cp = new ProjectControlPlane(reloaded, { manifestPath: manifest });
    const entry = cp.deployments()[0]!;
    expect(entry.verification.ok).toBe(false);
    expect(entry.verification.mismatches.map((m) => m.path)).toContain("identity.spec_digest");
  });

  it("answers [] for a project with no deployments dir", () => {
    const { manifest } = project();
    const cp = new ProjectControlPlane(loadProjectBundle(manifest), { manifestPath: manifest });
    expect(cp.deployments()).toEqual([]);
  });

  it("surfaces a malformed/tampered plan file as an ERROR ENTRY, never a shorter listing", () => {
    const { manifest, dir } = project();
    const bundle = loadProjectBundle(manifest);
    const { plan } = writeDeploymentPlan(bundle.project, bundle.sources, planResolver(bundle), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    writeFileSync(join(dir, "deployments", "garbage.yaml"), "not: [a, valid, plan\n");
    const cp = new ProjectControlPlane(bundle, { manifestPath: manifest });
    const entries = cp.deployments();
    expect(entries).toHaveLength(2);
    const error = entries.find((entry) => entry.plan_id === "garbage")!;
    expect(error.verification.ok).toBe(false);
    expect(error.verification.mismatches[0]!.path).toBe("parse");
    expect(String(error.verification.mismatches[0]!.current_value)).toMatch(/failed to parse/);
    expect(error.path).toBe("deployments/garbage.yaml");
    // The valid plan is still listed and verifies clean beside the corrupt file.
    const valid = entries.find((entry) => entry.plan_id === `review.prod.${plan.plan_hash.slice(0, 12)}`)!;
    expect(valid.verification.ok).toBe(true);
    // The error entry is addressable by id too (the console can drill into it).
    expect(cp.deployment("garbage").verification.mismatches[0]!.path).toBe("parse");
  });

  it("degrades a STALE plan (removed workflow) to a per-plan resolution mismatch, keeping the listing", () => {
    const { manifest, dir } = project();
    const bundle = loadProjectBundle(manifest);
    writeDeploymentPlan(bundle.project, bundle.sources, planResolver(bundle), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    // Remove the workflow from the manifest (rename its id) — the plan is now stale.
    writeFileSync(join(dir, "typeflux.project.yaml"), MANIFEST.replaceAll("review", "renamed"));
    writeFileSync(join(dir, "renamed.yaml"), WORKFLOW);
    const cp = new ProjectControlPlane(loadProjectBundle(manifest), { manifestPath: manifest });
    const entries = cp.deployments();
    expect(entries).toHaveLength(1);
    expect(entries[0]!.verification.ok).toBe(false);
    expect(entries[0]!.verification.mismatches[0]!.path).toBe("resolution");
    expect(String(entries[0]!.verification.mismatches[0]!.current_value)).toMatch(/cannot resolve workflow 'review'/);
  });

  it("shell-quotes promote_command components (injection defense in depth)", () => {
    const dir = mkdtempSync(join(tmpdir(), "tf-cp deploy spaced-"));
    createdDirs.push(dir);
    for (const [relative, content] of Object.entries({
      "typeflux.project.yaml": MANIFEST,
      "review.yaml": WORKFLOW,
      "only_mini.policy.yaml": POLICY,
      "prod.env.yaml": "name: prod\n",
    })) {
      const full = join(dir, relative);
      mkdirSync(dirname(full), { recursive: true });
      writeFileSync(full, content);
    }
    const manifest = join(dir, "typeflux.project.yaml");
    const bundle = loadProjectBundle(manifest);
    writeDeploymentPlan(bundle.project, bundle.sources, planResolver(bundle), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    const entry = new ProjectControlPlane(bundle, { manifestPath: manifest }).deployments()[0]!;
    // The manifest path contains a space → it must arrive single-quoted in the copyable command.
    expect(entry.promote_command).toContain(`'${manifest}'`);
    expect(entry.promote_command).toMatch(/^typeflux-project deploy '/);
  });

  it("propagates a policy COMPOSITION error (unknown policy_id) as a 422, not an advisory preview", () => {
    // Python `_deployment_preview` swallows only ProjectDeploymentError; an unknown policy id raises
    // ProjectPolicyError, which must 422 the whole bundle rather than degrade to {error} at 200.
    const { manifest } = project();
    // No schemas needed: composition throws before the bundle's io-schema projection runs.
    const cp = new ProjectControlPlane(loadProjectBundle(manifest), { manifestPath: manifest });
    try {
      cp.bundle("review", "prod", ["does-not-exist"], IMAGE);
      throw new Error("expected a 422");
    } catch (error) {
      expect(error).toBeInstanceOf(ProjectControlPlaneError);
      expect((error as ProjectControlPlaneError).status).toBe(422);
    }
  });
});
