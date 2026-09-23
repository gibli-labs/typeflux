import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import {
  verifyPlanMergedToDefaultBranch,
  deploymentPlanId,
  listDeploymentPlans,
  loadDeploymentPlan,
  loadProjectBundle,
  ObservabilityCompositionError,
  PLACEHOLDER_IMAGE_MISMATCH_PATH,
  ProjectDeploymentError,
  ProjectPolicyEnforcementError,
  projectSubworkflowResolver,
  resolveEnvironmentWorkflow,
  verifyDeploymentPlan,
  writeDeploymentPlan,
  type DeploymentPlanResolver,
  type LoadedProjectBundle,
  type ProjectBundleSources,
  type TypefluxProjectSpec,
} from "../src/index.js";

const DIGEST = "@sha256:" + "b".repeat(64);
const IMAGE = `registry.example/worker:1${DIGEST}`;
const PINNED = new Date("2026-01-02T03:04:05.000Z");

const POLICY = (models: string) => `name: only_mini\nproviders: { allowed: { openai: { models: [${models}] } } }\n`;
const WORKFLOW = (extraStep = false) => `
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
  steps: [{ id: s, activity: a }${extraStep ? ", { id: s2, activity: a }" : ""}]
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

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

function writeProject(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-plans-"));
  createdDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

function planResolver(project: TypefluxProjectSpec, sources: ProjectBundleSources): DeploymentPlanResolver {
  return (workflowId, environmentId) => {
    const environment = sources.environments[environmentId];
    const text = sources.workflows[workflowId];
    if (environment === undefined || text === undefined) return undefined;
    const spec = resolveEnvironmentWorkflow(text, { environment, workflowId, runtimeDefaults: project.defaults.runtime });
    const subworkflows = projectSubworkflowResolver(workflowId, (siblingId) => {
      const siblingText = sources.workflows[siblingId];
      return siblingText !== undefined
        ? resolveEnvironmentWorkflow(siblingText, { environment, workflowId: siblingId, runtimeDefaults: project.defaults.runtime })
        : undefined;
    });
    return { spec, subworkflows };
  };
}

function setup(workflow = WORKFLOW(), policyModels = "gpt-4o-mini"): { dir: string } & LoadedProjectBundle {
  const dir = writeProject({
    "typeflux.project.yaml": MANIFEST,
    "review.yaml": workflow,
    "only_mini.policy.yaml": POLICY(policyModels),
    "prod.env.yaml": "name: prod\n",
  });
  return { dir, ...loadProjectBundle(join(dir, "typeflux.project.yaml")) };
}

describe("writeDeploymentPlan (#687 D687-1)", () => {
  it("writes an immutable plan, round-trips, and names it workflow.env.<hash12>", () => {
    const { dir, project, sources } = setup();
    const planDir = join(dir, "deployments");
    const { path, plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir,
      now: PINNED,
    });
    expect(plan.plan_version).toBe("1");
    expect(plan.identity.workflow_type).toBe("typefluxYamlWorkflow");
    expect(plan.identity.environment_id).toBe("prod");
    expect(plan.policy.applied_policy_ids).toEqual(["only_mini"]);
    expect(deploymentPlanId(plan)).toBe(`review.prod.${plan.plan_hash.slice(0, 12)}`);
    expect(path.endsWith(`${deploymentPlanId(plan)}.yaml`)).toBe(true);
    const reloaded = loadDeploymentPlan(path);
    expect(reloaded.plan_hash).toBe(plan.plan_hash);
  });

  it("is deterministic for a pinned timestamp and identical composition", () => {
    const a = setup();
    const b = setup();
    const planA = writeDeploymentPlan(a.project, a.sources, planResolver(a.project, a.sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(a.dir, "deployments"),
      now: PINNED,
    }).plan;
    const planB = writeDeploymentPlan(b.project, b.sources, planResolver(b.project, b.sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(b.dir, "deployments"),
      now: PINNED,
    }).plan;
    expect(planA.plan_hash).toBe(planB.plan_hash);
  });

  it("re-writing identical content is a harmless no-op, but different content at the same path is refused", () => {
    const { dir, project, sources } = setup();
    const planDir = join(dir, "deployments");
    const { path } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir,
      now: PINNED,
    });
    // Same inputs + timestamp → same file, no throw.
    expect(() =>
      writeDeploymentPlan(project, sources, planResolver(project, sources), {
        workflowId: "review",
        environmentId: "prod",
        image: IMAGE,
        planDir,
        now: PINNED,
      }),
    ).not.toThrow();
    // Tamper the on-disk file (an edited image with the stale hash left in place) → the loader's
    // integrity recompute rejects it, so the writer refuses to overwrite it.
    const tampered = readFileSync(path, "utf-8").replace(/image: .*/, "image: attacker.example/evil@sha256:" + "f".repeat(64));
    writeFileSync(path, tampered);
    expect(() =>
      writeDeploymentPlan(project, sources, planResolver(project, sources), {
        workflowId: "review",
        environmentId: "prod",
        image: IMAGE,
        planDir,
        now: PINNED,
      }),
    ).toThrow(/failed its integrity check/);
  });

  it("ADMITS before writing: a policy-violating workflow is refused (review item 3)", () => {
    // The plan file is an approval artifact — a direct caller of the exported writer runs the
    // same compliance + closure admission deploy generation runs, fail-closed.
    const { dir, project, sources } = setup(WORKFLOW(), "gpt-4o"); // policy admits ONLY gpt-4o; the workflow uses gpt-4o-mini
    expect(() =>
      writeDeploymentPlan(project, sources, planResolver(project, sources), {
        workflowId: "review",
        environmentId: "prod",
        image: IMAGE,
        planDir: join(dir, "deployments"),
        now: PINNED,
      }),
    ).toThrow(ProjectPolicyEnforcementError);
    expect(listDeploymentPlans(join(dir, "deployments"))).toEqual([]);
  });

  it("refuses to write a plan for a workflow bound to no policy", () => {
    const dir = writeProject({
      "typeflux.project.yaml": `
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
environments:
  prod: prod.env.yaml
`,
      "review.yaml": WORKFLOW(),
      "prod.env.yaml": "name: prod\n",
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(() =>
      writeDeploymentPlan(project, sources, planResolver(project, sources), {
        workflowId: "review",
        environmentId: "prod",
        image: IMAGE,
        planDir: join(dir, "deployments"),
        now: PINNED,
      }),
    ).toThrow(/resolves under no policy/);
  });

  it("refuses to write a plan pinning the all-zeros placeholder digest unless allowPlaceholderImage (#757 item 5)", () => {
    const { dir, project, sources } = setup();
    const placeholder = `registry.example/worker@sha256:${"0".repeat(64)}`;
    const options = {
      workflowId: "review",
      environmentId: "prod",
      image: placeholder,
      planDir: join(dir, "deployments"),
      now: PINNED,
    };
    expect(() => writeDeploymentPlan(project, sources, planResolver(project, sources), options)).toThrow(
      /all-zeros placeholder digest/,
    );
    // Opt in → the placeholder plan writes.
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      ...options,
      allowPlaceholderImage: true,
    });
    expect(plan.deployment.image).toBe(placeholder);
  });

  it("refuses to write a plan for a closure with divergent observability backends (#757 review / #756)", () => {
    // Parent langfuse + child langsmith: each spec individually policy-compliant, but a composed
    // worker builds ONE observer — an approvable plan here would crash-loop at pod boot.
    const workflowWith = (name: string, backend: string, step: string) => `
project: p
name: ${name}
task_queue: ${name}-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
  observability:
    type: ${backend}
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: ${name}W
  input: schemas:In
  steps: [${step}]
`;
    const dir = writeProject({
      "typeflux.project.yaml": `
version: "1"
name: closure
workflows:
  - { id: parent, path: parent.yaml }
  - { id: child, path: child.yaml }
policies:
  only_mini: only_mini.policy.yaml
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-parent: { workflows: [parent], environment: prod, policies: [only_mini] }
`,
      "parent.yaml": workflowWith("parent", "langfuse", "{ id: assess, workflow: child }"),
      "child.yaml": workflowWith("child", "langsmith", "{ id: s, activity: a }"),
      "only_mini.policy.yaml": POLICY("gpt-4o-mini"),
      "prod.env.yaml": "name: prod\n",
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(() =>
      writeDeploymentPlan(project, sources, planResolver(project, sources), {
        workflowId: "parent",
        environmentId: "prod",
        image: IMAGE,
        planDir: join(dir, "deployments"),
        now: PINNED,
      }),
    ).toThrow(ObservabilityCompositionError);

    // A consistent closure (child inherits via `none`) writes fine.
    writeFileSync(join(dir, "child.yaml"), workflowWith("child", "none", "{ id: s, activity: a }"));
    const { project: p2, sources: s2 } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    const { plan } = writeDeploymentPlan(p2, s2, planResolver(p2, s2), {
      workflowId: "parent",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    expect(plan.identity.workflow_id).toBe("parent");
  });
});

describe("verifyDeploymentPlan (#687 D687-1)", () => {
  it("verifies clean against the resolution it was written from", () => {
    const { dir, project, sources } = setup();
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    const verification = verifyDeploymentPlan(project, sources, planResolver(project, sources), plan);
    expect(verification.ok).toBe(true);
    expect(verification.mismatches).toEqual([]);
  });

  it("fails closed at promote/verify time on a placeholder-digest plan unless allowPlaceholderImage (#757 item 5)", () => {
    const { dir, project, sources } = setup();
    const placeholder = `registry.example/worker@sha256:${"0".repeat(64)}`;
    // Author a placeholder plan (opt-in at write) — it must still not PROMOTE by default.
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: placeholder,
      planDir: join(dir, "deployments"),
      now: PINNED,
      allowPlaceholderImage: true,
    });
    const blocked = verifyDeploymentPlan(project, sources, planResolver(project, sources), plan);
    expect(blocked.ok).toBe(false);
    // The refusal is its OWN synthetic check code (#757 review): the literal offending image in
    // plan_value, the explanation in current_value — never prose in a literal diff slot.
    const imageMismatch = blocked.mismatches.find((m) => m.path === PLACEHOLDER_IMAGE_MISMATCH_PATH);
    expect(imageMismatch).toBeDefined();
    expect(imageMismatch!.path).toBe("deployment.image_placeholder");
    expect(imageMismatch!.plan_value).toBe(placeholder);
    expect(String(imageMismatch!.current_value)).toMatch(/all-zeros placeholder digest/);
    expect(String(imageMismatch!.current_value)).toMatch(/--allow-placeholder-image/);
    // The literal diff paths stay same-typed literals — no prose rides deployment.image.
    expect(blocked.mismatches.some((m) => m.path === "deployment.image")).toBe(false);
    // Opt in → promotes clean (spec/policy unchanged).
    const allowed = verifyDeploymentPlan(project, sources, planResolver(project, sources), plan, {
      allowPlaceholderImage: true,
    });
    expect(allowed.ok).toBe(true);
  });

  it("names identity.spec_digest drift when the workflow spec changes", () => {
    const { dir, project, sources } = setup();
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    // Edit the spec on disk (an extra step) and reload → the plan digest drifts.
    writeFileSync(join(dir, "review.yaml"), WORKFLOW(true));
    const { project: p2, sources: s2 } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    const verification = verifyDeploymentPlan(p2, s2, planResolver(p2, s2), plan);
    expect(verification.ok).toBe(false);
    expect(verification.mismatches.map((m) => m.path)).toContain("identity.spec_digest");
  });

  it("names policy.policy_hash drift when the composed policy changes", () => {
    const { dir, project, sources } = setup();
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    // Broaden the policy's allow-list → its composed hash drifts.
    writeFileSync(join(dir, "only_mini.policy.yaml"), POLICY("gpt-4o-mini, gpt-4o"));
    const { project: p2, sources: s2 } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    const verification = verifyDeploymentPlan(p2, s2, planResolver(p2, s2), plan);
    expect(verification.ok).toBe(false);
    expect(verification.mismatches.map((m) => m.path)).toContain("policy.policy_hash");
  });

  it("flags a hand-edited workflow_type as drift (constant assertion, D687-1)", () => {
    const { dir, project, sources } = setup();
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir: join(dir, "deployments"),
      now: PINNED,
    });
    const forged = { ...plan, identity: { ...plan.identity, workflow_type: "SomeOtherWorkflow_v2" } };
    const verification = verifyDeploymentPlan(project, sources, planResolver(project, sources), forged);
    expect(verification.ok).toBe(false);
    expect(verification.mismatches.map((m) => m.path)).toContain("identity.workflow_type");
  });
});

describe("listDeploymentPlans (#687)", () => {
  it("lists valid plans and skips a malformed file", () => {
    const { dir, project, sources } = setup();
    const planDir = join(dir, "deployments");
    const { plan } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir,
      now: PINNED,
    });
    writeFileSync(join(planDir, "garbage.yaml"), "not: [a, valid, plan\n");
    const plans = listDeploymentPlans(planDir);
    expect(plans.map(deploymentPlanId)).toEqual([deploymentPlanId(plan)]);
  });

  it("returns [] for a project with no deployments dir", () => {
    const { dir } = setup();
    expect(listDeploymentPlans(join(dir, "deployments"))).toEqual([]);
  });
});

it("loadDeploymentPlan rejects a non-mapping file", () => {
  const dir = writeProject({ "deployments/x.yaml": "- a\n- b\n" });
  expect(() => loadDeploymentPlan(join(dir, "deployments/x.yaml"))).toThrow(ProjectDeploymentError);
});

describe("plan integrity + strict shape at load (review items 1/2/5)", () => {
  /** Write a plan, apply `mutate` to its YAML text (leaving the stale hash), and return its path. */
  function tamperedPlanPath(mutate: (text: string) => string): string {
    const { dir, project, sources } = setup();
    const planDir = join(dir, "deployments");
    const { path } = writeDeploymentPlan(project, sources, planResolver(project, sources), {
      workflowId: "review",
      environmentId: "prod",
      image: IMAGE,
      planDir,
      now: PINNED,
    });
    writeFileSync(path, mutate(readFileSync(path, "utf-8")));
    return path;
  }

  it("rejects a tampered image with the stale hash left in place", () => {
    const path = tamperedPlanPath((text) =>
      text.replace(/image: .*/, "image: attacker.example/evil@sha256:" + "f".repeat(64)),
    );
    expect(() => loadDeploymentPlan(path)).toThrow(/failed its integrity check/);
  });

  it("rejects tampered policy ids with the stale hash left in place", () => {
    const path = tamperedPlanPath((text) =>
      text.replace(/selected_policy_ids:\n\s+- only_mini/, "selected_policy_ids:\n    - weaker_policy"),
    );
    expect(() => loadDeploymentPlan(path)).toThrow(/failed its integrity check/);
  });

  it("rejects a tampered spec_digest with the stale hash left in place", () => {
    const path = tamperedPlanPath((text) => text.replace(/spec_digest: .*/, "spec_digest: " + "f0".repeat(32)));
    expect(() => loadDeploymentPlan(path)).toThrow(/failed its integrity check/);
  });

  it("rejects contract-shape violations pydantic-strictly (empty deployment, missing field, unknown key)", () => {
    for (const mutate of [
      (text: string) => text.replace(/deployment:[\s\S]*$/, "deployment: {}\n"),
      (text: string) => text.replace(/ *workflow_name: .*\n/, ""),
      (text: string) => text + "sneaky_extra_key: true\n",
    ]) {
      expect(() => loadDeploymentPlan(tamperedPlanPath(mutate))).toThrow(/failed validation/);
    }
  });

  it("rejects identity ids outside the shell-safe manifest charset (injection guard)", () => {
    const path = tamperedPlanPath((text) => text.replace(/workflow_id: .*/, 'workflow_id: "x; touch /tmp/pwn"'));
    expect(() => loadDeploymentPlan(path)).toThrow(/failed validation/);
  });
});

describe("verifyPlanMergedToDefaultBranch (#790)", () => {
  it("passes a merged byte-identical plan; refuses tampered, unmerged, and non-repo plans", () => {
    const { execFileSync } = require("node:child_process") as typeof import("node:child_process");
    const { mkdtempSync, mkdirSync, writeFileSync } = require("node:fs") as typeof import("node:fs");
    const { join } = require("node:path") as typeof import("node:path");
    const { tmpdir } = require("node:os") as typeof import("node:os");
    const base = mkdtempSync(join(tmpdir(), "tf-790-"));
    const env = {
      ...process.env,
      GIT_AUTHOR_NAME: "T",
      GIT_AUTHOR_EMAIL: "t@example.com",
      GIT_COMMITTER_NAME: "T",
      GIT_COMMITTER_EMAIL: "t@example.com",
    };
    const git = (cwd: string, ...args: string[]) => execFileSync("git", ["-C", cwd, ...args], { env, stdio: "pipe" });
    const origin = join(base, "origin.git");
    execFileSync("git", ["init", "--bare", "-b", "main", origin], { env, stdio: "pipe" });
    const repo = join(base, "repo");
    execFileSync("git", ["init", "-b", "main", repo], { env, stdio: "pipe" });
    git(repo, "remote", "add", "origin", origin);
    mkdirSync(join(repo, "deployments"));
    const plan = join(repo, "deployments", "p.yaml");
    writeFileSync(plan, "plan: merged\n");
    git(repo, "add", "deployments/p.yaml");
    git(repo, "commit", "-m", "plan");
    git(repo, "push", "-u", "origin", "main");
    git(repo, "remote", "set-head", "origin", "--auto");

    expect(verifyPlanMergedToDefaultBranch(plan, repo)).toBeUndefined();
    writeFileSync(plan, "plan: tampered\n");
    expect(verifyPlanMergedToDefaultBranch(plan, repo)).toContain("differs from the version merged");
    const unmerged = join(repo, "deployments", "new.yaml");
    writeFileSync(unmerged, "plan: new\n");
    expect(verifyPlanMergedToDefaultBranch(unmerged, repo)).toContain("has not been merged");
    const { symlinkSync } = require("node:fs") as typeof import("node:fs");
    const link = join(repo, "deployments", "link.yaml");
    symlinkSync(plan, link);
    expect(verifyPlanMergedToDefaultBranch(link, repo)).toContain("symlink");
    const { symlinkSync: symlinkDir } = require("node:fs") as typeof import("node:fs");
    mkdirSync(join(repo, "approved"));
    writeFileSync(join(repo, "approved", "a.yaml"), "plan: merged\n");
    git(repo, "add", "approved/a.yaml");
    git(repo, "commit", "-m", "approved");
    git(repo, "push", "origin", "main");
    symlinkDir(join(repo, "approved"), join(repo, "alias"));
    expect(verifyPlanMergedToDefaultBranch(join(repo, "alias", "a.yaml"), repo)).toContain("path component");
    const loose = join(base, "loose.yaml");
    writeFileSync(loose, "plan: loose\n");
    expect(verifyPlanMergedToDefaultBranch(loose, repo)).toContain("outside the project's git checkout");
    const nogit = join(base, "nogit");
    mkdirSync(nogit);
    writeFileSync(join(nogit, "p.yaml"), "plan: x\n");
    expect(verifyPlanMergedToDefaultBranch(join(nogit, "p.yaml"), nogit)).toContain("not inside a git checkout");
  });
});
