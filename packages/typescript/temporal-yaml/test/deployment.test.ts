import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterAll, describe, expect, it } from "vitest";

import {
  buildProjectDeploymentPlan,
  loadProjectBundle,
  ObservabilityCompositionError,
  ProjectDeploymentError,
  ProjectPolicyEnforcementError,
  resolveEnvironmentWorkflow,
  validateDeploymentImage,
  type DeploymentResolvedWorkflow,
  type DeploymentWorkflowResolver,
  type LoadedProjectBundle,
  type TypefluxProjectSpec,
  type ProjectBundleSources,
} from "../src/index.js";

const DIGEST = "@sha256:" + "a".repeat(64);
const IMAGE = `registry.example/worker:1${DIGEST}`;

const POLICY = "name: only_mini\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";
const WORKFLOW = (queue = "wf-queue") => `
project: p
name: n
task_queue: ${queue}
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
  provider:
    type: openai
    model: gpt-4o-mini
    api_key: { value_from: { env: OPENAI_API_KEY } }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

/** A workflow declaring an AES-256-GCM payload codec keyed from `TF_CODEC_KEY` (#188). */
const WORKFLOW_WITH_CODEC = (queue = "wf-queue") => `
project: p
name: n
task_queue: ${queue}
runtime:
  temporal:
    payload_codec:
      type: aes
      current: k1
      keys:
        - { id: k1, value_from: { env: TF_CODEC_KEY } }
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
  provider:
    type: openai
    model: gpt-4o-mini
    api_key: { value_from: { env: OPENAI_API_KEY } }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

const createdDirs: string[] = [];
afterAll(() => {
  for (const dir of createdDirs) rmSync(dir, { recursive: true, force: true });
});

function writeProject(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), "tf-deploy-"));
  createdDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const full = join(dir, relative);
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

/** A `DeploymentWorkflowResolver` over a loaded bundle (test twin of the CLI's resolver). */
function makeResolver(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  environmentId: string,
): DeploymentWorkflowResolver {
  return (workflowId: string): DeploymentResolvedWorkflow | undefined => {
    const workflow = project.workflows.find((entry) => entry.id === workflowId);
    const environment = sources.environments[environmentId];
    const text = sources.workflows[workflowId];
    if (workflow === undefined || environment === undefined || text === undefined) return undefined;
    const spec = resolveEnvironmentWorkflow(text, { environment, workflowId, runtimeDefaults: project.defaults.runtime });
    return {
      spec,
      environmentName: environment.name,
      workflowPath: workflow.path ?? "wf.yaml",
      variables: environment.variables,
    };
  };
}

/** A single-workflow project bound to `only_mini`, with a configurable environment block. */
function singleWorkflowBundle(environmentYaml: string, workflowYaml = WORKFLOW(), policyYaml = POLICY): LoadedProjectBundle {
  const dir = writeProject({
    "typeflux.project.yaml": `
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
`,
    "review.yaml": workflowYaml,
    "only_mini.policy.yaml": policyYaml,
    "prod.env.yaml": environmentYaml,
  });
  return loadProjectBundle(join(dir, "typeflux.project.yaml"));
}

/** The all-zeros placeholder digest image (#757 item 5) — format-valid, but resolves to no image. */
const PLACEHOLDER_IMAGE = `registry.example/worker@sha256:${"0".repeat(64)}`;

/** A workflow declaring a tracing observability backend (#757 item 3). */
const WORKFLOW_WITH_OBSERVABILITY = (backend: "langfuse" | "langsmith") => `
project: p
name: n
task_queue: wf-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
  provider:
    type: openai
    model: gpt-4o-mini
    api_key: { value_from: { env: OPENAI_API_KEY } }
  observability:
    type: ${backend}
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

/** A policy that requires an observability backend (#756 runtime gate; #757 item 3 `required` flag). */
const POLICY_REQUIRE_OBSERVABILITY =
  "name: only_mini\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\nobservability: { required: true }\n";

describe("validateDeploymentImage (#687 D687-3)", () => {
  it("accepts a digest-pinned image and reports it pinned", () => {
    expect(validateDeploymentImage(IMAGE)).toBe(true);
  });
  it("rejects a mutable tag unless --allow-mutable-image", () => {
    expect(() => validateDeploymentImage("registry.example/worker:1")).toThrow(/pinned by digest/);
    expect(validateDeploymentImage("registry.example/worker:1", { allowMutableImage: true })).toBe(false);
  });
  it("rejects an empty or untrimmed image", () => {
    expect(() => validateDeploymentImage("")).toThrow(/non-empty/);
    expect(() => validateDeploymentImage(" x ")).toThrow(/trimmed/);
  });
  it("rejects the well-known all-zeros placeholder digest unless allowPlaceholderImage (#757 item 5)", () => {
    // Format-valid + digest-pinned, so the digest-pin check passes — but it resolves to no image.
    expect(() => validateDeploymentImage(PLACEHOLDER_IMAGE)).toThrow(/all-zeros placeholder digest/);
    // Error copy names both the digest and the escape flag.
    expect(() => validateDeploymentImage(PLACEHOLDER_IMAGE)).toThrow(/sha256:0{64}/);
    expect(() => validateDeploymentImage(PLACEHOLDER_IMAGE)).toThrow(/--allow-placeholder-image/);
    expect(validateDeploymentImage(PLACEHOLDER_IMAGE, { allowPlaceholderImage: true })).toBe(true);
  });
});

describe("buildProjectDeploymentPlan (#687 D687-2/D687-3)", () => {
  it("builds a secret-free worker plan with task queue, classified config, and admitted policy", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\nvariables: { TYPEFLUX_ENVIRONMENT: prod }\n");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    expect(plan.image_digest_pinned).toBe(true);
    expect(plan.workers).toHaveLength(1);
    const worker = plan.workers[0]!;
    expect(worker.workflow_id).toBe("review");
    expect(worker.task_queue).toBe("wf-queue");
    // The provider api_key is a typed secret ref → Secret env, never a ConfigMap value.
    expect(worker.secret_env.map((ref) => ref.env_name)).toContain("OPENAI_API_KEY");
    expect(Object.keys(worker.config_map)).toContain("TYPEFLUX_ENVIRONMENT");
    expect(Object.keys(worker.config_map)).toContain("TYPEFLUX_EXPECTED_POLICY_HASH");
    expect(worker.policy.applied_policy_ids).toEqual(["only_mini"]);
    // Secret-free: no api key value nor prompt text leaks into the serialized plan.
    const serialized = JSON.stringify(plan);
    expect(serialized).not.toContain("zztopsecretzz");
  });

  it("scaffolds the payload-codec key as a Secret env ref (#188 FIX 1)", () => {
    const { project, sources } = singleWorkflowBundle(
      "name: prod\nvariables: { TYPEFLUX_ENVIRONMENT: prod }\n",
      WORKFLOW_WITH_CODEC(),
    );
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const worker = plan.workers[0]!;
    // The generated deployment must scaffold the AES key so a codec-enabled worker's
    // buildPayloadCodec resolves TF_CODEC_KEY at startup — same env-var name and slot path
    // secret-references / buildPayloadCodec use (Python parity).
    const codecRef = worker.secret_env.find(
      (ref) => ref.runtime_path === "runtime.temporal.payload_codec.keys[k1].value_from",
    );
    expect(codecRef).toBeDefined();
    expect(codecRef!.env_name).toBe("TF_CODEC_KEY");
    expect(codecRef!.secret_key).toBe("TF_CODEC_KEY");
  });

  it("declared observability credentials claim their slots; no standard-name fallback (#793)", () => {
    const declaredYaml = `
project: p
name: n
task_queue: wf-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider:
    type: openai
    model: gpt-4o-mini
    api_key: { value_from: { env: OPENAI_API_KEY } }
  observability:
    type: langfuse
    langfuse:
      public_key: { value_from: { env: MYTEAM_LANGFUSE_PUBLIC } }
      secret_key: { value_from: { env: MYTEAM_LANGFUSE_SECRET } }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
    const { project, sources } = singleWorkflowBundle(
      "name: prod\nvariables: { TYPEFLUX_ENVIRONMENT: prod }\n",
      declaredYaml,
      POLICY_REQUIRE_OBSERVABILITY,
    );
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const worker = plan.workers[0]!;
    const obsRefs = worker.secret_env.filter((ref) => ref.runtime_path.startsWith("runtime.observability."));
    // The declared custom names are scaffolded under the canonical slot paths; the
    // standard-name fallback for the SAME slots is suppressed — a required secretKeyRef
    // for a key nothing populates would fail the pod at rollout.
    expect(obsRefs.map((ref) => ref.env_name).sort()).toEqual(["MYTEAM_LANGFUSE_PUBLIC", "MYTEAM_LANGFUSE_SECRET"]);
    expect(new Set(obsRefs.map((ref) => ref.runtime_path))).toEqual(
      new Set(["runtime.observability.langfuse.public_key", "runtime.observability.langfuse.secret_key"]),
    );
  });

  it("carries the rendered worker command and the default in-image project path (#687 slice 2)", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\n");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE, manifestPath: "/repo/typeflux.project.yaml" },
      makeResolver(project, sources, "prod"),
    );
    expect(plan.project_manifest_path).toBe("/repo/typeflux.project.yaml");
    // Manifest not under CWD → basename fallback (Python `_default_project_path_in_image`).
    expect(plan.project_path_in_image).toBe("/app/typeflux.project.yaml");
    const worker = plan.workers[0]!;
    expect(worker.project_path_in_image).toBe("/app/typeflux.project.yaml");
    expect(worker.command[0]).toBe("sh");
    expect(worker.command[1]).toBe("-c");
    expect(worker.command[2]).toContain("rm -f /tmp/typeflux-preflight-ok");
    expect(worker.command[2]).toContain(
      "typeflux-yaml-worker /app/typeflux.project.yaml --workflow review --environment prod",
    );
    expect(worker.command[2]).toContain("--policy only_mini");
    expect(worker.command[2]).toMatch(/--expect-policy-hash [0-9a-f]{64}/);
    expect(worker.command[2]).toContain("touch /tmp/typeflux-preflight-ok && exec");
  });

  it("honors an explicit projectPathInImage override and rejects a non-absolute one", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\n");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE, projectPathInImage: "/srv/app/p.yaml" },
      makeResolver(project, sources, "prod"),
    );
    expect(plan.project_path_in_image).toBe("/srv/app/p.yaml");
    expect(plan.workers[0]!.command[2]).toContain("/srv/app/p.yaml");
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["review"], image: IMAGE, projectPathInImage: "relative/p.yaml" },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/must be an absolute image path/);
  });

  it("records an explicit portable projectManifestPath without perturbing the in-image path (#757 item 1)", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\n");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      {
        environmentId: "prod",
        workflowIds: ["review"],
        image: IMAGE,
        // The CLI passes the ABSOLUTE seed (in-image default) + a portable recorded path.
        manifestPath: "/abs/repo/typeflux.project.yaml",
        projectManifestPath: "typeflux.project.yaml",
      },
      makeResolver(project, sources, "prod"),
    );
    // The recorded (annotation) path is the portable value…
    expect(plan.project_manifest_path).toBe("typeflux.project.yaml");
    // …while the in-image path still derives from the absolute seed.
    expect(plan.project_path_in_image).toBe("/app/typeflux.project.yaml");
  });

  it("refuses the all-zeros placeholder image at build unless allowPlaceholderImage (#757 item 5)", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\n");
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["review"], image: PLACEHOLDER_IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/all-zeros placeholder digest/);
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: PLACEHOLDER_IMAGE, allowPlaceholderImage: true },
      makeResolver(project, sources, "prod"),
    );
    expect(plan.image).toBe(PLACEHOLDER_IMAGE);
    expect(plan.image_digest_pinned).toBe(true);
  });

  it("models a langfuse spec's credential NAMES as Secret env refs, required when policy requires observability (#757 item 3)", () => {
    const { project, sources } = singleWorkflowBundle(
      "name: prod\n",
      WORKFLOW_WITH_OBSERVABILITY("langfuse"),
      POLICY_REQUIRE_OBSERVABILITY,
    );
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const worker = plan.workers[0]!;
    const obs = worker.secret_env.filter((ref) => ref.env_name.startsWith("LANGFUSE_"));
    expect(obs.map((ref) => ref.env_name).sort()).toEqual(["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]);
    // Policy marks observability REQUIRED (#756) → the creds are a hard secretKeyRef.
    expect(obs.every((ref) => ref.required)).toBe(true);
    expect(obs.every((ref) => ref.runtime_path.startsWith("runtime.observability.langfuse."))).toBe(true);
    // Names only — no value ever enters the (secret-free) plan.
    expect(JSON.stringify(plan)).not.toContain("zztopsecretzz");
  });

  it("marks observability creds optional when no policy requires observability (#757 item 3)", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\n", WORKFLOW_WITH_OBSERVABILITY("langfuse"));
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const obs = plan.workers[0]!.secret_env.filter((ref) => ref.env_name.startsWith("LANGFUSE_"));
    expect(obs).toHaveLength(2);
    expect(obs.every((ref) => ref.required === false)).toBe(true);
  });

  it("models langsmith's LANGSMITH_API_KEY and no creds for a spec with no backend (#757 item 3)", () => {
    const langsmith = singleWorkflowBundle("name: prod\n", WORKFLOW_WITH_OBSERVABILITY("langsmith"));
    const smithPlan = buildProjectDeploymentPlan(
      langsmith.project,
      langsmith.sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(langsmith.project, langsmith.sources, "prod"),
    );
    expect(smithPlan.workers[0]!.secret_env.filter((ref) => ref.env_name === "LANGSMITH_API_KEY")).toHaveLength(1);

    // No observability block → no observability creds scaffolded.
    const none = singleWorkflowBundle("name: prod\n");
    const nonePlan = buildProjectDeploymentPlan(
      none.project,
      none.sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(none.project, none.sources, "prod"),
    );
    expect(
      nonePlan.workers[0]!.secret_env.filter(
        (ref) => ref.env_name.startsWith("LANGFUSE_") || ref.env_name === "LANGSMITH_API_KEY",
      ),
    ).toHaveLength(0);
  });

  it("does not duplicate an observability cred already declared as an environment variable (#757 item 3)", () => {
    const { project, sources } = singleWorkflowBundle(
      "name: prod\nvariables: { LANGFUSE_SECRET_KEY: sk-placeholder }\n",
      WORKFLOW_WITH_OBSERVABILITY("langfuse"),
    );
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const worker = plan.workers[0]!;
    const secretKeyRefs = worker.secret_env.filter((ref) => ref.env_name === "LANGFUSE_SECRET_KEY");
    expect(secretKeyRefs).toHaveLength(1);
    // The env-var classification owns it (not the observability injection).
    expect(secretKeyRefs[0]!.runtime_path).toBe("environment.variables.LANGFUSE_SECRET_KEY");
    // The other langfuse cred is still added by the observability injection.
    expect(worker.secret_env.filter((ref) => ref.env_name === "LANGFUSE_PUBLIC_KEY")).toHaveLength(1);
  });

  it("routes a secret-like ENV VARIABLE name to a Secret ref, not the ConfigMap", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\nvariables: { CUSTOM_API_KEY: v }\n");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const worker = plan.workers[0]!;
    expect(worker.secret_env.map((ref) => ref.env_name)).toContain("CUSTOM_API_KEY");
    expect(Object.keys(worker.config_map)).not.toContain("CUSTOM_API_KEY");
  });

  it("fails closed on an unclassifiable environment variable", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\nvariables: { RANDOM_THING: v }\n");
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/cannot classify environment variable\(s\).*RANDOM_THING/);
  });

  it("promotes an unclassified variable to the ConfigMap via configEnvNames", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\nvariables: { RANDOM_THING: v }\n");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE, configEnvNames: ["RANDOM_THING"] },
      makeResolver(project, sources, "prod"),
    );
    expect(Object.keys(plan.workers[0]!.config_map)).toContain("RANDOM_THING");
  });

  it("lets a DECLARED variable override the computed fixed ConfigMap keys (Python parity, #687 review)", () => {
    // Python's `_config_map_entries` first loop assigns profile-declared variables
    // unconditionally, overriding the computed TEMPORAL_*/policy-hash entries. A declared
    // TEMPORAL_ADDRESS is always safe; TYPEFLUX_EXPECTED_POLICY_HASH is not in the safe
    // set, so it overrides only when explicitly promoted via --config-env (same as Python).
    const { project, sources } = singleWorkflowBundle(
      "name: prod\nvariables: { TEMPORAL_ADDRESS: 'declared.example:7233', TYPEFLUX_EXPECTED_POLICY_HASH: declared-hash }\n",
    );
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      {
        environmentId: "prod",
        workflowIds: ["review"],
        image: IMAGE,
        configEnvNames: ["TYPEFLUX_EXPECTED_POLICY_HASH"],
      },
      makeResolver(project, sources, "prod"),
    );
    const configMap = plan.workers[0]!.config_map;
    expect(configMap["TEMPORAL_ADDRESS"]).toBe("declared.example:7233");
    expect(configMap["TYPEFLUX_EXPECTED_POLICY_HASH"]).toBe("declared-hash");
    // The other computed keys are untouched by unrelated declared variables.
    expect(configMap["TEMPORAL_TASK_QUEUE"]).toBe("wf-queue");
  });

  it("rejects a mutable image without the escape hatch", () => {
    const { project, sources } = singleWorkflowBundle("name: prod\n");
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["review"], image: "registry.example/worker:1" },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/pinned by digest/);
  });
});

describe("one-worker-per-task-queue (#687 D687-3)", () => {
  // Distinct workflow names so the structural preflight does not flag duplicate_workflow_name.
  const namedWorkflow = (workflowName: string, queue: string): string =>
    WORKFLOW(queue).replace("name: W", `name: ${workflowName}`);
  function twoWorkflowBundle(sharedQueue: boolean): LoadedProjectBundle {
    const dir = writeProject({
      "typeflux.project.yaml": `
version: "1"
name: acme
workflows:
  - { id: alpha, path: alpha.yaml }
  - { id: beta, path: beta.yaml }
policies:
  only_mini: only_mini.policy.yaml
environments:
  prod: prod.env.yaml
validation:
  targets:
    prod-all: { workflows: [alpha, beta], environment: prod, policies: [only_mini] }
`,
      "alpha.yaml": namedWorkflow("Alpha", "alpha-queue"),
      "beta.yaml": namedWorkflow("Beta", sharedQueue ? "alpha-queue" : "beta-queue"),
      "only_mini.policy.yaml": POLICY,
      "prod.env.yaml": "name: prod\n",
    });
    return loadProjectBundle(join(dir, "typeflux.project.yaml"));
  }

  it("rejects two workflows sharing a task queue by default", () => {
    const { project, sources } = twoWorkflowBundle(true);
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["alpha", "beta"], image: IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/shared task queue 'alpha-queue'/);
  });

  it("allows a shared task queue with the escape flag", () => {
    const { project, sources } = twoWorkflowBundle(true);
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["alpha", "beta"], image: IMAGE, allowSharedTaskQueue: true },
      makeResolver(project, sources, "prod"),
    );
    expect(plan.workers.map((w) => w.workflow_id)).toEqual(["alpha", "beta"]);
  });

  it("plans distinct queues without the flag", () => {
    const { project, sources } = twoWorkflowBundle(false);
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["alpha", "beta"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    expect(plan.workers).toHaveLength(2);
  });
});

describe("deploy-time policy admission (#687 D687-3)", () => {
  it("evaluates env-backed policy values under the RESOLVED environment overlay (review item 4)", () => {
    // A policy requiring a Temporal api key, satisfied by the deployment ENVIRONMENT's
    // variables — absent from the operator's shell. Admission must resolve against the
    // environment file's overlay, not host process.env.
    delete process.env["DEPLOY_TEMPORAL_KEY"];
    const workflow = `
project: p
name: n
task_queue: wf-queue
runtime:
  temporal:
    api_key: { value_from: { env: DEPLOY_TEMPORAL_KEY } }
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
    const policy =
      "name: only_mini\n" +
      "providers: { allowed: { openai: { models: [gpt-4o-mini] } } }\n" +
      "runtime: { temporal: { require_api_key: true } }\n";
    const bundleFor = (envYaml: string) => {
      const dir = writeProject({
        "typeflux.project.yaml": `
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
`,
        "review.yaml": workflow,
        "only_mini.policy.yaml": policy,
        "prod.env.yaml": envYaml,
      });
      return loadProjectBundle(join(dir, "typeflux.project.yaml"));
    };

    // The environment file carries the key → admitted, even though the host env lacks it.
    const withKey = bundleFor("name: prod\nvariables: { DEPLOY_TEMPORAL_KEY: configured-by-env-file }\n");
    const plan = buildProjectDeploymentPlan(
      withKey.project,
      withKey.sources,
      { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
      makeResolver(withKey.project, withKey.sources, "prod"),
    );
    expect(plan.workers[0]!.secret_env.map((ref) => ref.env_name)).toContain("DEPLOY_TEMPORAL_KEY");

    // Negative control: without the environment-file value the same policy fails closed —
    // proving the overlay (not some ambient state) is what satisfied the check above.
    const withoutKey = bundleFor("name: prod\n");
    expect(() =>
      buildProjectDeploymentPlan(
        withoutKey.project,
        withoutKey.sources,
        { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
        makeResolver(withoutKey.project, withoutKey.sources, "prod"),
      ),
    ).toThrow(ProjectPolicyEnforcementError);
  });

  it("requires at least one selected policy", () => {
    // No validation.targets bind the workflow → no policy selected.
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
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["review"], image: IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/requires at least one selected policy/);
  });

  it("blocks the plan build when a referenced sub-workflow violates the parent's policy (closure)", () => {
    const child = (model: string): string => `
project: p
name: child
task_queue: child-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
  provider: { type: openai, model: ${model} }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: Child
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
    const parent = `
project: p
name: parent
task_queue: parent-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: Parent
  input: schemas:In
  steps: [{ id: assess_one, workflow: child }]
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
      "parent.yaml": parent,
      "child.yaml": child("gpt-4o"), // NOT in only_mini's allow-list → closure violation
      "only_mini.policy.yaml": POLICY,
      "prod.env.yaml": "name: prod\n",
    });
    const { project, sources } = loadProjectBundle(join(dir, "typeflux.project.yaml"));
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["parent"], image: IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(ProjectPolicyEnforcementError);
  });

  it("rejects a closure with divergent observability backends at plan BUILD (#757 review / #756)", () => {
    // The finder's exact composition: parent langfuse + child langsmith. Each spec is individually
    // policy-compliant, so before this gate the plan built cleanly with only the parent's creds
    // scaffolded — and the pod crash-looped at boot on ObservabilityCompositionError.
    const { project, sources } = observabilityClosureBundle("langfuse", "langsmith");
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["parent"], image: IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(ObservabilityCompositionError);
    expect(() =>
      buildProjectDeploymentPlan(
        project,
        sources,
        { environmentId: "prod", workflowIds: ["parent"], image: IMAGE },
        makeResolver(project, sources, "prod"),
      ),
    ).toThrow(/observability composition/);
  });

  it("builds a consistent closure fine, scaffolding the parent's verified backend creds (#757 review)", () => {
    // Child declares none → inherits the parent's langfuse observer; the closure's single
    // effective backend is the parent's, and exactly its creds are scaffolded.
    const { project, sources } = observabilityClosureBundle("langfuse", "none");
    const plan = buildProjectDeploymentPlan(
      project,
      sources,
      { environmentId: "prod", workflowIds: ["parent"], image: IMAGE },
      makeResolver(project, sources, "prod"),
    );
    const worker = plan.workers[0]!;
    expect(worker.secret_env.filter((ref) => ref.env_name.startsWith("LANGFUSE_"))).toHaveLength(2);
    expect(worker.secret_env.some((ref) => ref.env_name === "LANGSMITH_API_KEY")).toBe(false);
  });
});

/** A parent (declaring `parentBackend`) that references a child declaring `childBackend`. */
function observabilityClosureBundle(parentBackend: string, childBackend: string): LoadedProjectBundle {
  const workflowWith = (name: string, backend: string, step: string) => `
project: p
name: ${name}
task_queue: ${name}-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: zztopsecretzz } }
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
    "parent.yaml": workflowWith("parent", parentBackend, "{ id: assess, workflow: child }"),
    "child.yaml": workflowWith("child", childBackend, "{ id: s, activity: a }"),
    "only_mini.policy.yaml": POLICY,
    "prod.env.yaml": "name: prod\n",
  });
  return loadProjectBundle(join(dir, "typeflux.project.yaml"));
}
