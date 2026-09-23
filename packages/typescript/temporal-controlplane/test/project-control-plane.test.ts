import {
  emptyProfileSources,
  loadEnvironmentSpec,
  loadPolicySpec,
  loadProfileSpec,
  loadProjectSpec,
  type LoadedProjectBundle,
} from "@typeflux/temporal-yaml";
import { afterAll, beforeEach, describe, expect, it } from "vitest";

import { z } from "zod";

import { ProjectControlPlane, ProjectControlPlaneError } from "../src/index.js";

/** A valid workflow spec (openai/gpt-4o-mini); `name` sets both the yaml name AND the workflow name. */
const workflowYaml = (name: string) => `
project: p
name: ${name}
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: ${name}
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

const STRICT = "name: strict\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";

const PROJECT = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
  - { id: intake, directory: workflows/intake, profiles: { provider: fast } }
policies:
  strict: strict.policy.yaml
profiles:
  provider:
    fast: profiles/fast.yaml
environments:
  prod: envs/prod.yaml
  dev: envs/dev.yaml
validation:
  targets:
    prod-review: { workflows: [review], environment: prod, policies: [strict] }
`);

const bundle: LoadedProjectBundle = {
  project: PROJECT,
  sources: {
    policies: { strict: loadPolicySpec(STRICT) },
    environments: {
      prod: loadEnvironmentSpec("name: prod\noverrides: { task_queue: prod-queue }\n"),
      dev: loadEnvironmentSpec("name: dev\noverrides: {}\n"),
    },
    workflows: { review: workflowYaml("review"), intake: workflowYaml("intake") },
    profiles: {
      ...emptyProfileSources(),
      provider: { fast: loadProfileSpec("name: fast\nkind: provider\nruntime:\n  provider: { type: openai }\n") },
    },
  },
};

// The bundle's `links` field reads these live — pin them absent so a developer's shell (or
// another suite) can't leak an ambient URL into the expectations below.
const LINK_ENV_VARS = ["TEMPORAL_UI_URL", "LANGFUSE_PROJECT_URL"] as const;
const savedLinkEnv = new Map<string, string | undefined>(LINK_ENV_VARS.map((k) => [k, process.env[k]]));
beforeEach(() => {
  for (const key of LINK_ENV_VARS) delete process.env[key];
});
afterAll(() => {
  for (const [key, value] of savedLinkEnv) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

describe("ProjectControlPlane — bundle (#563)", () => {
  const schemas = {
    "schemas:In": z.object({ text: z.string() }),
    "schemas:Mid": z.object({ mid: z.number() }),
    "schemas:Out": z.object({ out: z.string() }),
  };

  it("assembles the resolved bundle core — identity + safe runtime + policy + topology + validation", () => {
    const b = new ProjectControlPlane(bundle, { schemas, manifestPath: "acme/typeflux.project.yaml" }).bundle("review", "prod");
    expect(b.bundle_version).toBe("1");
    expect(b.project).toEqual({ name: "acme", manifest_path: "acme/typeflux.project.yaml" });
    expect(b.environment).toEqual({ id: "prod", name: "prod", profile_path: "envs/prod.yaml", env_files: [], profile_variable_names: [] });
    expect(b.workflow).toMatchObject({
      id: "review",
      yaml_project: "p",
      yaml_name: "review",
      workflow_name: "review",
      workflow_type: "typefluxYamlWorkflow",
      spec_digest_algorithm: "typeflux-yaml-plan-v1",
      task_queue: "prod-queue", // prod env override reached the resolved spec
      observability_trace_name: "TypefluxWorkflow:review",
    });
    expect(typeof b.workflow.spec_digest).toBe("string");
    expect(b.workflow.input_schema).toMatchObject({ name: "In" });
    expect(b.workflow.input_schema.json_schema).toBeDefined();
    // Secret-safe runtime summary — provider carries type/model + configured flags, NEVER a key.
    expect(b.runtime.provider).toMatchObject({ type: "openai", model: "gpt-4o-mini", api_key_configured: false, base_url_configured: false, vertex: false });
    // Python always emits `params` with at least the effective model — even without an explicit params block.
    expect((b.runtime.provider as { params?: unknown }).params).toMatchObject({ model: "gpt-4o-mini" });
    // Temporal knobs are materialized to the runtime defaults (not bare null) so preflight matches execution.
    expect(b.runtime.temporal).toMatchObject({ address: "localhost:7233", namespace: "default", tls_enabled: false, tls_mode: "disabled", api_key_configured: false });
    expect(b.runtime.registry).toMatchObject({ type: "inline" });
    expect(JSON.stringify(b.runtime)).not.toMatch(/"api_key"|sk-/);
    // Topology + validation reuse the existing projections.
    expect(b.topology.nodes).toEqual([{ id: "s", kind: "activity", activity: "a" }]);
    expect(b.validation.ok).toBe(true);
    // Policy: the prod-review target selects `strict` for review → composed hash.
    expect(b.policy?.selected_policy_ids).toEqual(["strict"]);
    expect(typeof b.policy?.policy_hash).toBe("string");
    // Activities are real now (#568): the planned set with secret-free AI/source metadata, sorted.
    // exclude_none: no task_queue/timeout/retry keys (all None); prompt_ref + validation_retries present (AI).
    expect(b.activities).toEqual([
      {
        name: "a",
        kind: "ai",
        input_schema: { name: "In", hash: expect.any(String) },
        output_schema: { name: "Out", hash: expect.any(String) },
        prompt_ref: { name: "p/x", version: null, label: null },
        definition_source: { kind: "yaml", yaml_project: "p", yaml_name: "review" },
        validation_retries: 1,
        artifact_inputs: [],
        used_by_steps: ["s"],
      },
    ]);
    // `review` selects no component profile → components empty (Python `()` default, present array).
    expect(b.components).toEqual([]);
    // prod env has no inline `variables` → profile_variable_names empty (sorted keys of variables).
    expect(b.environment.profile_variable_names).toEqual([]);
    expect(b).not.toHaveProperty("deployment_preview"); // None in Python → omitted
    // The #575 slice: steps with effective knobs, secret provenance, per-knob source tags, links.
    expect(b.steps).toEqual([
      {
        id: "s",
        kind: "activity",
        activity: "a",
        effective_start_to_close_timeout_seconds: 120,
        effective_retry: {
          maximum_attempts: 5,
          initial_interval_seconds: 1,
          maximum_interval_seconds: 60,
          backoff_coefficient: 2,
        },
      },
    ]);
    expect(b.secret_references).toEqual([]); // no api_key anywhere in the fixture spec
    const effectiveByPath = Object.fromEntries(b.runtime_effective.map((entry) => [entry.path, entry]));
    expect(effectiveByPath["provider.model"]).toEqual({
      path: "provider.model",
      value: "gpt-4o-mini",
      source: "engine_default",
    });
    expect(effectiveByPath["provider_retry.max_attempts"]).toEqual({
      path: "provider_retry.max_attempts",
      value: 1,
      source: "engine_default",
    });
    // The fixture resolves temporal to localhost -> the local dev UI link derives.
    expect(b.links).toEqual({ temporal_ui: "http://localhost:8233" });
  });

  it("surfaces a populated risk_tier when the bound policy constrains tiers, omits it otherwise (#300)", () => {
    // Baseline: `strict` declares no risk_tiers dimension → risk_tier omitted (exclude_none).
    const plain = new ProjectControlPlane(bundle, { schemas }).bundle("review", "prod");
    expect(plain).not.toHaveProperty("risk_tier");

    // A risk-augmented `strict`: an undeclared workflow is lifted to policy_gated by the
    // floor, and its constrain_providers macro is satisfied by the openai/gpt-4o-mini spec.
    // `require_declared: true` is a real admission requirement, so the undeclared workflow
    // surfaces it as an UNSATISFIED requirement entry (first) rather than reading clean.
    const riskStrict =
      "name: strict\n" +
      "providers: { allowed: { openai: { models: [gpt-4o-mini] } } }\n" +
      "risk_tiers:\n" +
      "  min_tier: policy_gated\n" +
      "  require_declared: true\n" +
      "  policy_gated:\n" +
      "    constrain_providers: { openai: { models: [gpt-4o-mini] } }\n";
    const withRisk: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, policies: { strict: loadPolicySpec(riskStrict) } },
    };
    const b = new ProjectControlPlane(withRisk, { schemas }).bundle("review", "prod");
    expect(b.risk_tier).toEqual({
      declared: "safe",
      effective: "policy_gated",
      floor: "policy_gated",
      floor_source: "policy_floor",
      requirements: [
        { name: "require_declared", satisfied: false },
        { name: "constrain_providers", satisfied: true },
      ],
    });
    // The cascade key is absent for a non-composed workflow (exclude_none parity).
    expect(b.risk_tier).not.toHaveProperty("cascade");
  });

  it("derives a final map step's output schema from collect.output, not the mapped activity (codex)", () => {
    const mapWf = [
      "project: p",
      "name: review",
      "task_queue: base-queue",
      "runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: hi } }, provider: { type: openai, model: gpt-4o-mini } }",
      "activities:",
      "  definitions: [{ name: a, input: schemas:In, output: schemas:Mid, prompt: p/x }]",
      "workflow:",
      "  name: review",
      "  input: schemas:In",
      "  steps: [{ id: m, map: { activity: a, over: input.items, collect: { output: schemas:Out, field: results } } }]",
      "",
    ].join("\n");
    const withMap: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: mapWf } },
    };
    const b = new ProjectControlPlane(withMap, { schemas }).bundle("review", "prod");
    // The workflow output is the collect object (schemas:Out), NOT the mapped activity output (schemas:Mid).
    expect(b.workflow.output_schema.name).toBe("Out");
  });

  it("projects lifecycle + review, and reports api keys as configured-flags only (secret-safe; TLS mode)", () => {
    const reviewWf = [
      "project: p",
      "name: review",
      "task_queue: base-queue",
      "runtime:",
      "  temporal: { address: temporal.example:7233, tls: true, api_key: sk-temporal-secret }",
      "  registry: { type: inline, prompts: { p/x: hi } }",
      "  provider: { type: openai, model: gpt-4o-mini, api_key: sk-provider-secret }",
      "activities:",
      "  definitions:",
      "    - { name: a, input: schemas:In, output: schemas:Mid, prompt: p/x }",
      "    - { name: b, input: schemas:Mid, output: schemas:Out, prompt: p/x }",
      "workflow:",
      "  name: review",
      "  input: schemas:In",
      "  steps: [{ id: s1, activity: a }, { id: s2, activity: b }]",
      "  lifecycle:",
      "    enabled: true",
      "    review: { after_step: s1, user_decisions: { approve: { route: s2 } } }",
      "",
    ].join("\n");
    const withReview: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: reviewWf } },
    };
    const b = new ProjectControlPlane(withReview, { schemas }).bundle("review", "prod");
    // Lifecycle: enabled + defaults + the flattened review (decision → route string).
    expect(b.lifecycle).toMatchObject({ enabled: true, progress: true, cancellation: true, status_event_limit: 50 });
    expect(b.lifecycle?.review).toEqual({ after_step: "s1", invalid_user_decision: "warn", user_decisions: { approve: "s2" } });
    // Secret-safe: only *_configured flags, TLS mode, never the keys themselves.
    expect(b.runtime.provider).toMatchObject({ api_key_configured: true });
    expect(b.runtime.temporal).toMatchObject({ address: "temporal.example:7233", api_key_configured: true, tls_enabled: true, tls_mode: "boolean" });
    expect(JSON.stringify(b.runtime)).not.toMatch(/sk-provider-secret|sk-temporal-secret/);
  });

  it("strips credentials from a URL-shaped registry host + reports the params.model as effective (codex)", () => {
    const richWf = [
      "project: p",
      "name: review",
      "task_queue: base-queue",
      "runtime:",
      "  temporal: {}",
      '  registry: { type: langfuse, host: "https://user:sk-secret@lf.example/api?token=abc" }',
      "  provider: { type: openai, params: { model: gpt-4o } }", // model only in params
      "activities:",
      "  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
      "workflow: { name: review, input: schemas:In, steps: [{ id: s, activity: a }] }",
      "",
    ].join("\n");
    const withRich: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: richWf } },
    };
    const b = new ProjectControlPlane(withRich, { schemas }).bundle("review", "prod");
    // Registry host: userinfo + query stripped (secret-safe), like the connections projection.
    expect(b.runtime.registry).toMatchObject({ type: "langfuse", host: "https://lf.example/api" });
    expect(JSON.stringify(b.runtime)).not.toMatch(/sk-secret|token=abc/);
    // Provider effective model comes from params.model when the top-level model is unset.
    expect(b.runtime.provider).toMatchObject({ type: "openai", model: "gpt-4o" });
  });

  it("reports the per-provider DEFAULT model + an unresolved value_from key as NOT configured (codex)", () => {
    const defaultsWf = [
      "project: p",
      "name: review",
      "task_queue: base-queue",
      "runtime:",
      "  temporal: {}",
      "  registry: { type: inline, prompts: { p/x: hi } }",
      "  provider: { type: openai, api_key: { value_from: { env: TF_BUNDLE_TEST_UNSET_KEY } } }", // no model; unset key ref
      "activities:",
      "  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
      "workflow: { name: review, input: schemas:In, steps: [{ id: s, activity: a }] }",
      "",
    ].join("\n");
    const withDefaults: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: defaultsWf } },
    };
    delete process.env["TF_BUNDLE_TEST_UNSET_KEY"];
    const b = new ProjectControlPlane(withDefaults, { schemas }).bundle("review", "prod");
    // Effective model falls back to the per-provider default (matches what executions/validation use).
    expect(typeof (b.runtime.provider as { model: unknown }).model).toBe("string");
    expect((b.runtime.provider as { model: string }).model.length).toBeGreaterThan(0);
    // A value_from whose env var is unset resolves to NOT configured (consistent with policy validation).
    expect(b.runtime.provider).toMatchObject({ api_key_configured: false });
  });

  it("rejects a schema-INCOMPATIBLE step chain (a step's input != the prior step's output) with 422 (codex)", () => {
    const chainWf = [
      "project: p",
      "name: review",
      "task_queue: base-queue",
      "runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: hi } }, provider: { type: openai, model: gpt-4o-mini } }",
      "activities:",
      "  definitions:",
      "    - { name: a, input: schemas:In, output: schemas:Mid, prompt: p/x }",
      "    - { name: b, input: schemas:Other, output: schemas:Out, prompt: p/x }", // b expects Other, not Mid
      "workflow:",
      "  name: review",
      "  input: schemas:In",
      "  steps: [{ id: s1, activity: a }, { id: s2, activity: b }]",
      "",
    ].join("\n");
    const withChain: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: chainWf } },
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(withChain, { schemas }).bundle("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect((caught as ProjectControlPlaneError).status).toBe(422);
    expect((caught as Error).message).toMatch(/expects input/);
  });

  it("rejects an unrunnable graph (a step referencing an undeclared activity) with a 422 (codex)", () => {
    const dangling: LoadedProjectBundle = {
      project: PROJECT,
      sources: {
        ...bundle.sources,
        workflows: { ...bundle.sources.workflows, review: workflowYaml("review").replace("activity: a }", "activity: zzz }") },
      },
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(dangling, { schemas }).bundle("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ProjectControlPlaneError);
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });

  it("resolves an ENVIRONMENT-supplied value_from api_key as configured, without leaking it (finder)", () => {
    const envSecretWf = [
      "project: p",
      "name: review",
      "task_queue: base-queue",
      "runtime:",
      "  temporal: {}",
      "  registry: { type: inline, prompts: { p/x: hi } }",
      "  provider: { type: openai, model: gpt-4o-mini, api_key: { value_from: { env: TF_ENV_KEY } } }",
      "activities:",
      "  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
      "workflow: { name: review, input: schemas:In, steps: [{ id: s, activity: a }] }",
      "",
    ].join("\n");
    const withEnvSecret: LoadedProjectBundle = {
      project: PROJECT,
      sources: {
        ...bundle.sources,
        // The prod env supplies TF_ENV_KEY via its `variables` (NOT the host process.env).
        environments: {
          ...bundle.sources.environments,
          prod: loadEnvironmentSpec("name: prod\noverrides: { task_queue: prod-queue }\nvariables: { TF_ENV_KEY: sk-env-secret }\n"),
        },
        workflows: { ...bundle.sources.workflows, review: envSecretWf },
      },
    };
    delete process.env["TF_ENV_KEY"];
    const b = new ProjectControlPlane(withEnvSecret, { schemas }).bundle("review", "prod");
    // The withEnvironmentContext overlay makes the env-supplied value_from resolve → configured...
    expect(b.runtime.provider).toMatchObject({ api_key_configured: true });
    // ...and the raw secret never enters the bundle, and the overlay is restored afterward.
    expect(JSON.stringify(b.runtime)).not.toMatch(/sk-env-secret/);
    expect(process.env["TF_ENV_KEY"]).toBeUndefined();
  });

  it("excludes inline registry prompt TEXT from the runtime summary (finder)", () => {
    const promptWf = workflowYaml("review").replace("prompts: { p/x: hi }", "prompts: { p/x: SECRET_PROMPT_MARKER }");
    const withPrompt: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: promptWf } },
    };
    const b = new ProjectControlPlane(withPrompt, { schemas }).bundle("review", "prod");
    expect(b.runtime.registry).not.toHaveProperty("prompts"); // only {type, label, host}
    expect(JSON.stringify(b.runtime)).not.toMatch(/SECRET_PROMPT_MARKER/);
  });

  it("rejects a workflow.output that mismatches the terminal step's actual output (codex)", () => {
    // `workflow.output` is declared as schemas:In, but the terminal activity `a` outputs schemas:Out.
    const mismatchWf = workflowYaml("review").replace(
      "workflow:\n  name: review",
      "workflow:\n  output: schemas:In\n  name: review",
    );
    const withMismatch: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { ...bundle.sources.workflows, review: mismatchWf } },
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(withMismatch, { schemas }).bundle("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect((caught as ProjectControlPlaneError).status).toBe(422);
    expect((caught as Error).message).toMatch(/does not match the terminal/);
  });

  it("throws a 404 for an unknown workflow and a 422 when a workflow schema ref is not injected", () => {
    const cp = new ProjectControlPlane(bundle, { schemas });
    let unknown: unknown;
    try {
      cp.bundle("ghost", "prod");
    } catch (error) {
      unknown = error;
    }
    expect(unknown).toBeInstanceOf(ProjectControlPlaneError);
    expect((unknown as ProjectControlPlaneError).status).toBe(404);

    // No schemas → the workflow input schema slot can't be built → 422 config error.
    let missing: unknown;
    try {
      new ProjectControlPlane(bundle).bundle("review", "prod");
    } catch (error) {
      missing = error;
    }
    expect((missing as ProjectControlPlaneError).status).toBe(422);
  });
});

describe("ProjectControlPlane — meta (#563)", () => {
  it("projects the contract versions, project name, runtime, and open capabilities by default", () => {
    expect(new ProjectControlPlane(bundle).meta()).toEqual({
      api_version: "1",
      bundle_version: "1",
      catalog_version: "1",
      project: "acme",
      manifest_path: "typeflux.project.yaml",
      // Defaults to the runtime this server resolves (#619/#620): TypeScript, hence can_resolve true.
      runtime: "typescript",
      // Base meta() carries no trusted proxy identity — the meta route fills caller_identity in (#577).
      caller_identity: null,
      // enforcement_events follows resolvable now (#723 slice 2): the default TS project is resolvable → true.
      capabilities: { can_start: true, can_review: true, can_cancel: true, can_refresh_project: true, can_resolve: true, enforcement_events: true, github_provenance: false },
    });
  });

  it("honors an explicit manifestPath + runtime + capabilities", () => {
    const cp = new ProjectControlPlane(bundle, {
      manifestPath: "/srv/acme/typeflux.project.yaml",
      runtime: "python",
      capabilities: { can_start: false, can_review: true, can_cancel: false, can_refresh_project: false, can_resolve: false, enforcement_events: false, github_provenance: false },
    });
    const meta = cp.meta();
    expect(meta.manifest_path).toBe("/srv/acme/typeflux.project.yaml");
    expect(meta.runtime).toBe("python");
    expect(meta.capabilities).toEqual({ can_start: false, can_review: true, can_cancel: false, can_refresh_project: false, can_resolve: false, enforcement_events: false, github_provenance: false });
  });
});

describe("ProjectControlPlane — listings (#563)", () => {
  it("lists workflows in declaration order with path / directory / profiles", () => {
    // Both `path` and `directory` are always present (null for the unused one) — Python contract.
    expect(new ProjectControlPlane(bundle).workflows()).toEqual({
      workflows: [
        { id: "review", path: "review.yaml", directory: null, profiles: {} },
        { id: "intake", path: null, directory: "workflows/intake", profiles: { provider: "fast" } },
      ],
    });
  });

  it("lists environments sorted by id", () => {
    expect(new ProjectControlPlane(bundle).environments()).toEqual({
      environments: [
        { id: "dev", path: "envs/dev.yaml" },
        { id: "prod", path: "envs/prod.yaml" },
      ],
    });
  });

  it("validate() returns the snake_case report DTO with a loaded workflows summary (reference-only)", () => {
    const report = new ProjectControlPlane(bundle, { manifestPath: "acme/typeflux.project.yaml" }).validate();
    expect(report).toMatchObject({
      project_name: "acme",
      manifest_path: "acme/typeflux.project.yaml",
      ok: true,
      issues: [],
      resolved_workflows: [],
    });
    expect(report.workflows).toEqual([
      { id: "review", path: "review.yaml", yaml_project: "p", yaml_name: "review", workflow_name: "review", task_queue: "base-queue" },
      {
        id: "intake",
        path: "workflows/intake/typeflux.yaml", // directory + defaults.workflow_filename
        yaml_project: "p",
        yaml_name: "intake",
        workflow_name: "intake",
        task_queue: "base-queue",
      },
    ]);
  });

  it("validate() reports duplicate_workflow_name when two workflows load to the same workflow.name (codex)", () => {
    const dupNames: LoadedProjectBundle = {
      project: PROJECT,
      // Both workflows use workflow.name "W" — Python `validate_unique_yaml_workflow_names`.
      sources: { ...bundle.sources, workflows: { review: workflowYaml("W"), intake: workflowYaml("W") } },
    };
    const report = new ProjectControlPlane(dupNames).validate();
    expect(report.ok).toBe(false);
    expect(report.issues.some((i) => i.code === "duplicate_workflow_name")).toBe(true);
  });

  it("validate() keeps selector checks even when a workflow fails to load (codex)", () => {
    const malformed: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { review: workflowYaml("review"), intake: "::: bad :::" } },
    };
    // A malformed workflow AND an unknown environment → BOTH issues (Python reports selectors before bailing).
    const report = new ProjectControlPlane(malformed).validate({ environmentId: "ghost" });
    expect(report.issues.some((i) => i.code === "invalid_workflow_yaml")).toBe(true);
    expect(report.issues.some((i) => i.code === "unknown_validation_environment")).toBe(true);
  });

  it("validate() maps resolved workflows to snake_case under a selected environment (no camelCase leaks)", () => {
    const report = new ProjectControlPlane(bundle).validate({ environmentId: "prod", workflowIds: ["review"] });
    expect(report.ok).toBe(true);
    expect(report.resolved_workflows).toHaveLength(1);
    expect(report.resolved_workflows[0]).toMatchObject({
      workflow_id: "review",
      environment_id: "prod",
      ok: true,
      task_queue: "prod-queue", // env override reached the resolved spec
    });
    expect(report.resolved_workflows[0]?.checks.some((c) => c.code === "environment_workflow_resolution" && c.status === "passed")).toBe(true);
    expect(JSON.stringify(report)).not.toMatch(/workflowId|resolvedWorkflows|taskQueue|yamlName|projectName/);
  });

  it("validate() omits an unsourced workflow from the summary and reports it as an issue", () => {
    const partial: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { review: workflowYaml("review") } }, // intake source omitted
    };
    const report = new ProjectControlPlane(partial).validate();
    expect(report.ok).toBe(false);
    expect(report.workflows.map((w) => w.id)).toEqual(["review"]);
    expect(report.issues.some((i) => i.code === "missing_workflow_source" && i.reference === "intake")).toBe(true);
  });

  it("validate() surfaces invalid_workflow_yaml for a malformed source and bails resolution (codex)", () => {
    const malformed: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { review: workflowYaml("review"), intake: "::: not valid yaml :::" } },
    };
    // Reference-only (no environment) must NOT silently pass a malformed workflow.
    const report = new ProjectControlPlane(malformed).validate();
    expect(report.ok).toBe(false);
    expect(report.issues.some((i) => i.code === "invalid_workflow_yaml" && i.reference === "intake")).toBe(true);
    expect(report.workflows.map((w) => w.id)).toEqual(["review"]); // malformed one omitted from the summary
    // A parse failure bails the resolved validation too (Python parity) — no resolution failure double-report.
    const resolved = new ProjectControlPlane(malformed).validate({ environmentId: "prod" });
    expect(resolved.resolved_workflows).toEqual([]);
    expect(resolved.issues.some((i) => i.code === "invalid_workflow_yaml")).toBe(true);
  });

  it("returns independent DTOs — mutating a response never leaks into shared state or the bundle (Bugbot)", () => {
    // Default capabilities: a mutation of one response must not affect a later instance.
    const meta = new ProjectControlPlane(bundle).meta();
    meta.capabilities.can_start = false;
    expect(new ProjectControlPlane(bundle).meta().capabilities.can_start).toBe(true);
    // Workflow profiles: a mutation of the DTO must not rewrite the loaded project state.
    const summary = new ProjectControlPlane(bundle).workflows().workflows[1]!;
    summary.profiles["provider"] = "mutated";
    expect(new ProjectControlPlane(bundle).workflows().workflows[1]?.profiles).toEqual({ provider: "fast" });
  });
});

describe("ProjectControlPlane — bundleTopology (#563 slice 2b)", () => {
  it("resolves a workflow under an environment and returns its topology", () => {
    const topology = new ProjectControlPlane(bundle).bundleTopology("review", "prod");
    expect(topology.nodes).toEqual([{ id: "s", kind: "activity", activity: "a" }]);
    expect(topology.edges).toEqual([]);
  });

  it("throws a 404 ProjectControlPlaneError for an unknown workflow or environment", () => {
    const cp = new ProjectControlPlane(bundle);
    for (const [workflowId, environmentId, message] of [
      ["ghost", "prod", /unknown project workflow: ghost/],
      ["review", "nope-env", /unknown project environment: nope-env/],
    ] as const) {
      let caught: unknown;
      try {
        cp.bundleTopology(workflowId, environmentId);
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(ProjectControlPlaneError);
      expect((caught as ProjectControlPlaneError).status).toBe(404);
      expect((caught as Error).message).toMatch(message);
    }
  });

  it("maps a bad workflow graph (reserved step id) to a 422 config error (codex)", () => {
    const badGraph: LoadedProjectBundle = {
      project: PROJECT,
      // `input` is a reserved step id — loads fine but workflowPlanFromSpec rejects the graph.
      sources: {
        ...bundle.sources,
        workflows: { review: workflowYaml("review").replace("id: s,", "id: input,"), intake: workflowYaml("intake") },
      },
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(badGraph).bundleTopology("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ProjectControlPlaneError);
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });

  it("maps a declared-but-unsourced workflow to a 422 config error, not 404 (codex)", () => {
    const noSource: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, workflows: { intake: workflowYaml("intake") } }, // `review` declared but unsourced
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(noSource).bundleTopology("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });
});

describe("ProjectControlPlane — connections (#563 slice 2b)", () => {
  it("resolves a workflow under an environment and projects its connection status", async () => {
    const conns = await new ProjectControlPlane(bundle).connections("review", "prod");
    expect(conns).toEqual({
      workflow_id: "review",
      environment_id: "prod",
      registry: { type: "inline", reachable: true },
      observability: { type: "none", reachable: true, execution_manifest: true, redaction_enabled: true },
    });
  });

  it("routes reachability through an injected connectionProbe", async () => {
    const cp = new ProjectControlPlane(bundle, {
      connectionProbe: ({ type }) => (type === "inline" ? { reachable: true } : { reachable: false, detail: "down" }),
    });
    expect((await cp.connections("review", "prod")).registry).toMatchObject({ type: "inline", reachable: true });
  });

  it("rejects with a 404 for an unknown workflow or environment (shared resolve path)", async () => {
    const cp = new ProjectControlPlane(bundle);
    let caught: unknown;
    try {
      await cp.connections("ghost", "prod");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ProjectControlPlaneError);
    expect((caught as ProjectControlPlaneError).status).toBe(404);
  });

  it("the injected `langfuse` option lights up BOTH the probe and prompt-status drift (#573)", async () => {
    // A workflow whose runtime uses a langfuse registry + observer, so both live tiers engage.
    const langfuseBundle: LoadedProjectBundle = {
      project: loadProjectSpec('version: "1"\nname: acme\nworkflows:\n  - { id: review, path: review.yaml }\nenvironments:\n  prod: envs/prod.yaml\n'),
      sources: {
        policies: {},
        environments: { prod: loadEnvironmentSpec("name: prod\noverrides: {}\n") },
        workflows: {
          review:
            "project: p\nname: review\ntask_queue: q\n" +
            "runtime:\n  temporal: {}\n  registry: { type: langfuse, label: prod }\n  observability: { type: langfuse }\n" +
            "  provider: { type: openai, model: gpt-4o-mini }\n" +
            "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: { name: lab, label: prod } }\n" +
            "workflow:\n  name: W\n  input: schemas:In\n  steps: [{ id: s, activity: a }]\n",
        },
        profiles: emptyProfileSources(),
      },
    };
    const langfuse = {
      ping: async () => undefined,
      promptLabelVersion: async () => "5",
      lastRunPromptVersions: async () => ({ lab: "5" }),
      traceSummary: async () => null,
      searchEnforcementTraces: async () => [],
    };
    const cp = new ProjectControlPlane(langfuseBundle, { langfuse });
    // Connections: the derived langfuse probe reports reachable for both the registry and observer.
    const conns = await cp.connections("review", "prod");
    expect(conns.registry).toMatchObject({ type: "langfuse", reachable: true });
    expect(conns.observability).toMatchObject({ type: "langfuse", reachable: true });
    // Prompt-status: the label ref resolves in_sync (registry 5 == last-run 5).
    const status = await cp.promptStatus("review", "prod");
    expect(status.prompts[0]).toMatchObject({ name: "lab", status: "in_sync", registry_version: "5", last_run_version: "5" });
  });
});

describe("ProjectControlPlane — profiles (#570)", () => {
  const profileBundle: LoadedProjectBundle = {
    project: loadProjectSpec(
      'version: "1"\nname: acme\nworkflows:\n' +
        "  - { id: review, path: review.yaml, profiles: { provider: anthropic-prod } }\n" +
        "profiles:\n  provider:\n    anthropic-prod: profiles/anthropic.yaml\n",
    ),
    sources: {
      policies: {},
      environments: {},
      workflows: {},
      profiles: {
        provider: {
          "anthropic-prod": loadProfileSpec(
            "name: anthropic-prod\nkind: provider\nruntime:\n  provider: { type: anthropic }\n",
          ),
        },
        registry: {},
        runtime: {},
      },
    },
  };

  it("lists profile summaries and serves detail with used_by", () => {
    const cp = new ProjectControlPlane(profileBundle);
    expect(cp.profiles()).toEqual([
      {
        kind: "provider",
        id: "anthropic-prod",
        name: "anthropic-prod",
        content_hash: expect.stringMatching(/^[0-9a-f]{64}$/) as unknown as string,
        path: "profiles/anthropic.yaml",
      },
    ]);
    const detail = cp.profileDetail("provider", "anthropic-prod");
    expect(detail.used_by).toEqual(["review"]);
    expect(detail.runtime["provider"]).toMatchObject({ type: "anthropic" });
  });

  it("404s an unknown kind and an undeclared id with Python's combined message", () => {
    const cp = new ProjectControlPlane(profileBundle);
    for (const [kind, id] of [
      ["nonsense", "anthropic-prod"],
      ["provider", "nope"],
    ] as const) {
      let caught: unknown;
      try {
        cp.profileDetail(kind, id);
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(ProjectControlPlaneError);
      expect((caught as ProjectControlPlaneError).status).toBe(404);
      expect((caught as Error).message).toBe(`unknown project profile: ${kind}/${id}`);
    }
  });

  it("422s a declared-but-unsourced profile", () => {
    const cp = new ProjectControlPlane({
      project: profileBundle.project,
      sources: { ...profileBundle.sources, profiles: emptyProfileSources() },
    });
    let caught: unknown;
    try {
      cp.profileDetail("provider", "anthropic-prod");
    } catch (error) {
      caught = error;
    }
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });
});

describe("ProjectControlPlane — policies (#563 slice 2b)", () => {
  it("lists declared policies as summaries (description present as null when absent)", () => {
    expect(new ProjectControlPlane(bundle).policies()).toEqual([
      { id: "strict", name: "strict", description: null, path: "strict.policy.yaml" },
    ]);
  });

  it("projects a policy's full definition with composed hash + used_by, omitting an absent description", () => {
    const def = new ProjectControlPlane(bundle).policyDetail("strict");
    expect(def).toMatchObject({ id: "strict", name: "strict", extends: [], used_by: ["review"] });
    expect("description" in def).toBe(false); // exclude_none on the detail route; STRICT declares none
    expect(typeof def.policy_hash).toBe("string");
    expect(def.rules).toHaveProperty("providers");
  });

  it("throws a 404 for an unknown policy and a 422 for a declared-but-unsourced one", () => {
    const cp = new ProjectControlPlane(bundle);
    let unknown: unknown;
    try {
      cp.policyDetail("ghost");
    } catch (error) {
      unknown = error;
    }
    expect(unknown).toBeInstanceOf(ProjectControlPlaneError);
    expect((unknown as ProjectControlPlaneError).status).toBe(404);

    // `strict` is declared by PROJECT but its source is omitted here → 422 config error.
    const unsourced: LoadedProjectBundle = {
      project: PROJECT,
      sources: { ...bundle.sources, policies: {} },
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(unsourced).policyDetail("strict");
    } catch (error) {
      caught = error;
    }
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });
});

describe("ProjectControlPlane — activityCatalog (#563 slice 2b)", () => {
  const schemas = { "schemas:In": z.object({ text: z.string() }), "schemas:Out": z.object({ out: z.string() }) };

  it("resolves a workflow and projects its activity catalog keyed by the MANIFEST project", () => {
    const catalog = new ProjectControlPlane(bundle, { schemas }).activityCatalog("review", "prod");
    // `project` is the manifest name "acme" (matching meta()), not the workflow YAML's `project` ("p").
    expect(catalog).toMatchObject({ catalog_version: "1", project: "acme", workflow_id: "review", environment_id: "prod" });
    expect(catalog.activities.map((a) => a.name)).toEqual(["a"]);
    expect(catalog.activities[0]?.used_by_steps).toEqual(["s"]);
    expect(catalog.activities[0]?.input_schema.json_schema).toBeDefined();
  });

  it("throws a 404 for an unknown workflow (shared resolve path)", () => {
    let caught: unknown;
    try {
      new ProjectControlPlane(bundle).activityCatalog("ghost", "prod");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ProjectControlPlaneError);
    expect((caught as ProjectControlPlaneError).status).toBe(404);
  });

  it("maps a missing injected schema to a 422 config error (contract requires hash+json_schema) (codex)", () => {
    // No schemas provided → the catalog cannot render the required slots → 422, not a partial payload.
    let caught: unknown;
    try {
      new ProjectControlPlane(bundle).activityCatalog("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });

  it("maps a workflow that references an undeclared activity to a 422 config error (codex)", () => {
    const dangling: LoadedProjectBundle = {
      project: PROJECT,
      // Step `s` points at activity `zzz`, which no definition declares — must not silently drop it.
      sources: {
        ...bundle.sources,
        workflows: { review: workflowYaml("review").replace("activity: a }", "activity: zzz }"), intake: workflowYaml("intake") },
      },
    };
    let caught: unknown;
    try {
      new ProjectControlPlane(dangling, { schemas }).activityCatalog("review", "prod");
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ProjectControlPlaneError);
    expect((caught as ProjectControlPlaneError).status).toBe(422);
  });
});

describe("ProjectControlPlane — operate tier (#563)", () => {
  const schemas = { "schemas:In": z.object({ text: z.string() }), "schemas:Out": z.object({ out: z.string() }) };

  /** A fake operations client that records the start and returns a canned describe/status. */
  const fakeFactory = (
    calls: { starts: unknown[]; signals: unknown[] },
    describe: { type: string; runId?: string; memo?: Record<string, unknown> },
  ) =>
    async () => ({
      getHandle: () => ({
        describe: async () => describe,
        query: async <Ret>() => ({ state: "running" }) as Ret,
        signal: async (type: string, ...args: unknown[]) => {
          calls.signals.push({ type, args });
        },
      }),
      start: async (opts: unknown) => {
        calls.starts.push(opts);
        return { runId: "run-1" };
      },
      close: async () => undefined,
    });

  const cp = () => new ProjectControlPlane(bundle, { schemas, manifestPath: "acme/typeflux.project.yaml" });

  it("start resolves under the environment and dispatches the plan-as-argument shape", async () => {
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    const receipt = await cp().start("review", "prod", "exec-1", { text: "hi" }, {
      clientFactory: fakeFactory(calls, { type: "typefluxYamlWorkflow" }) as never,
    });
    expect(receipt.workflow_id).toBe("exec-1");
    expect(receipt.workflow_type).toBe("typefluxYamlWorkflow");
    // prod env overrides task_queue to prod-queue — the resolved spec's queue reaches the start.
    expect(receipt.task_queue).toBe("prod-queue");
    expect((calls.starts[0] as { args: unknown[] }).args).toHaveLength(2);
  });

  it("start 404s an unknown workflow and an unknown environment (resolveWorkflow guard)", async () => {
    await expect(cp().start("nope", "prod", "e", {})).rejects.toMatchObject({ status: 404 });
    await expect(cp().start("review", "nope", "e", {})).rejects.toMatchObject({ status: 404 });
  });

  it("start with an explicit policy selection + matching expected hash composes, admits, and dispatches (#663)", async () => {
    // The read-side bundle computes the same composed closure hash the operate gate verifies.
    const hash = cp().bundle("review", "prod").policy?.policy_hash;
    expect(typeof hash).toBe("string");
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    const receipt = await cp().start("review", "prod", "exec-1", { text: "hi" }, {
      policyIds: ["strict"],
      expectedPolicyHash: hash as string,
      clientFactory: fakeFactory(calls, { type: "typefluxYamlWorkflow" }) as never,
    });
    expect(receipt.workflow_id).toBe("exec-1");
    expect(calls.starts).toHaveLength(1);
  });

  it("start with a wrong expected policy hash is a 422 BEFORE any Temporal dispatch (#663)", async () => {
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    await expect(
      cp().start("review", "prod", "e", { text: "x" }, {
        policyIds: ["strict"],
        expectedPolicyHash: "deadbeef",
        clientFactory: fakeFactory(calls, { type: "typefluxYamlWorkflow" }) as never,
      }),
    ).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
      message: expect.stringMatching(
        /^selected project policy hash does not match expected deployment policy hash \(expected=deadbeef, actual=[0-9a-f]+\)$/,
      ),
    });
    expect(calls.starts).toHaveLength(0);
  });

  it("an expected hash with NOTHING selected is the Python no-policy 422 (#663)", async () => {
    // `dev` has no validation target → target-derived selection is empty; the provided hash
    // then has nothing to pin (Python `_verify_expected_policy_hash`, message parity).
    await expect(
      cp().start("review", "dev", "e", { text: "x" }, { expectedPolicyHash: "deadbeef" }),
    ).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
      message: "expected project policy hash was provided, but no project policy was selected",
    });
  });

  it("the TARGET-derived selection is honored too: no explicit ids, wrong hash → mismatch 422 (#663)", async () => {
    // prod-review binds `strict` to review/prod, so the gate composes even without policyIds.
    await expect(
      cp().start("review", "prod", "e", { text: "x" }, { expectedPolicyHash: "deadbeef" }),
    ).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
      message: expect.stringMatching(/does not match expected deployment policy hash/),
    });
  });

  it("an unknown explicit policy id is a 422 ProjectPolicyError (composition over DECLARED policies)", async () => {
    await expect(
      cp().start("review", "prod", "e", { text: "x" }, { policyIds: ["nope"] }),
    ).rejects.toMatchObject({ status: 422, errorName: "ProjectPolicyError" });
  });

  it("a policy the resolved spec violates fails ADMISSION closed — 422 before Temporal (#663)", async () => {
    // `tight` allows only gpt-4; the review spec runs gpt-4o-mini → provider/model check fails.
    // The explicit `policyIds: ['tight']` below drives selection directly (it short-circuits
    // target matching), so this project deliberately declares NO validation targets.
    const tightened: LoadedProjectBundle = {
      project: loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
policies:
  tight: tight.policy.yaml
environments:
  prod: envs/prod.yaml
`),
      sources: {
        ...bundle.sources,
        policies: { tight: loadPolicySpec("name: tight\nproviders: { allowed: { openai: { models: [gpt-4] } } }\n") },
        workflows: { review: workflowYaml("review") },
      },
    };
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    await expect(
      new ProjectControlPlane(tightened, { schemas }).start("review", "prod", "e", { text: "x" }, {
        policyIds: ["tight"],
        clientFactory: fakeFactory(calls, { type: "typefluxYamlWorkflow" }) as never,
      }),
    ).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
      message: expect.stringMatching(/^project policy enforcement failed: /),
    });
    expect(calls.starts).toHaveLength(0);
  });

  it("the policy gate guards EVERY operate op via the shared prelude — status/review/cancel too (#663)", async () => {
    const mismatch = { policyIds: ["strict"] as const, expectedPolicyHash: "deadbeef" };
    await expect(cp().status("review", "prod", "e", { ...mismatch })).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
    });
    await expect(
      cp().submitReview("review", "prod", "e", { user_decision: "approve" }, { ...mismatch }),
    ).rejects.toMatchObject({ status: 422, errorName: "ProjectPolicyEnforcementError" });
    await expect(cp().requestCancel("review", "prod", "e", "stop", { ...mismatch })).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
    });
  });

  it("status memo-verifies: a wrong-project memo is a 409 LifecycleBindingError", async () => {
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    await expect(
      cp().status("review", "prod", "exec-1", {
        clientFactory: fakeFactory(calls, {
          type: "typefluxYamlWorkflow",
          memo: { typeflux_project: "other", typeflux_workflow: "review" },
        }) as never,
      }),
    ).rejects.toMatchObject({ status: 409, errorName: "LifecycleBindingError" });
  });

  it("status returns the snapshot + null runtime_pin when the memo binds", async () => {
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    const result = await cp().status("review", "prod", "exec-1", {
      clientFactory: fakeFactory(calls, {
        type: "typefluxYamlWorkflow",
        runId: "r",
        memo: { typeflux_project: "p", typeflux_workflow: "review" },
      }) as never,
    });
    expect(result.runtime_pin).toBeNull();
    // Normalized to the always-present-[] waiting_gates convention (item 2).
    expect(result.status).toEqual({ state: "running", waiting_gates: [] });
  });

  it("review/cancel memo-verify then signal (204 at the route)", async () => {
    const calls = { starts: [] as unknown[], signals: [] as unknown[] };
    const memo = { typeflux_project: "p", typeflux_workflow: "review" };
    await cp().submitReview("review", "prod", "exec-1", { user_decision: "approve" }, {
      clientFactory: fakeFactory(calls, { type: "typefluxYamlWorkflow", memo }) as never,
    });
    await cp().requestCancel("review", "prod", "exec-1", "stop", {
      clientFactory: fakeFactory(calls, { type: "typefluxYamlWorkflow", memo }) as never,
    });
    expect(calls.signals).toEqual([
      { type: "typeflux_submit_review", args: [{ user_decision: "approve" }] },
      { type: "typeflux_request_cancel", args: ["stop"] },
    ]);
  });

  it("a runtime-kind profile selection is HONORED — the profiled temporal config reaches resolution (#568)", () => {
    // `review` selects a runtime profile that overrides runtime.temporal.namespace; composition now
    // merges it into the overlay, so the operate/read tier resolves the PROFILED cluster — no 501.
    const runtimeProfiled: LoadedProjectBundle = {
      project: loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml, profiles: { runtime: prod-temporal } }
profiles:
  runtime:
    prod-temporal: profiles/rt.yaml
environments:
  prod: envs/prod.yaml
`),
      sources: {
        policies: {},
        environments: { prod: loadEnvironmentSpec("name: prod\noverrides: {}\n") },
        workflows: { review: workflowYaml("review") },
        profiles: {
          ...emptyProfileSources(),
          runtime: { "prod-temporal": loadProfileSpec("name: prod-temporal\nkind: runtime\nruntime:\n  temporal: { namespace: prod-ns }\n") },
        },
      },
    };
    const b = new ProjectControlPlane(runtimeProfiled, { schemas, manifestPath: "acme/typeflux.project.yaml" }).bundle("review", "prod");
    // The profile's runtime.temporal.namespace landed in the resolved runtime summary (honored).
    expect(b.runtime.temporal).toMatchObject({ namespace: "prod-ns" });
    // …and the applied-profile provenance is recorded in `components` (Python AppliedComponentProfile).
    expect(b.components).toEqual([
      {
        kind: "runtime",
        id: "prod-temporal",
        name: "prod-temporal",
        content_hash: expect.any(String),
        source_path: "profiles/rt.yaml",
        override_paths: ["runtime.temporal.namespace"],
      },
    ]);
  });
});

describe("ProjectControlPlane — operate-gate transitive-closure admission (#55 §9)", () => {
  const schemas = { "schemas:In": z.object({ text: z.string() }), "schemas:Out": z.object({ out: z.string() }) };

  const fakeFactory = (calls: { starts: unknown[] }) =>
    async () => ({
      getHandle: () => ({
        describe: async () => ({ type: "typefluxYamlWorkflow" }),
        query: async <Ret>() => ({ state: "running" }) as Ret,
        signal: async () => undefined,
      }),
      start: async (opts: unknown) => {
        calls.starts.push(opts);
        return { runId: "run-1" };
      },
      close: async () => undefined,
    });

  /** A parent whose `assess` step runs the sibling `child` as a sub-workflow. */
  const parentYaml = `
project: p
name: parent
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions: []
workflow:
  name: parent
  input: schemas:In
  output: schemas:Out
  steps: [{ id: assess, workflow: child }]
`;
  const childYaml = (model: string) => `
project: p
name: child
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: ${model} }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: child
  input: schemas:In
  output: schemas:Out
  steps: [{ id: s, activity: a }]
`;
  const composedProject = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: parent, path: parent.yaml }
  - { id: child, path: child.yaml }
policies:
  strict: strict.policy.yaml
environments:
  prod: envs/prod.yaml
validation:
  targets:
    prod-parent: { workflows: [parent], environment: prod, policies: [strict] }
`);
  const composedBundle = (childModel: string): LoadedProjectBundle => ({
    project: composedProject,
    sources: {
      policies: { strict: loadPolicySpec(STRICT) },
      environments: { prod: loadEnvironmentSpec("name: prod\noverrides: {}\n") },
      workflows: { parent: parentYaml, child: childYaml(childModel) },
      profiles: emptyProfileSources(),
    },
  });

  it("start of a parent whose CHILD violates the composed policy is a 422 BEFORE any dispatch", async () => {
    const calls = { starts: [] as unknown[] };
    const cp = new ProjectControlPlane(composedBundle("gpt-4o"), { schemas });
    await expect(
      cp.start("parent", "prod", "exec-1", { text: "hi" }, { clientFactory: fakeFactory(calls) as never }),
    ).rejects.toMatchObject({
      status: 422,
      errorName: "ProjectPolicyEnforcementError",
      message: expect.stringContaining("policy_subworkflow_closure"),
    });
    await expect(
      cp.start("parent", "prod", "exec-1", { text: "hi" }, { clientFactory: fakeFactory(calls) as never }),
    ).rejects.toMatchObject({ message: expect.stringContaining("sub-workflow 'child'") });
    expect(calls.starts).toHaveLength(0); // the gate fired before the Temporal tier
  });

  it("start of the compliant composed parent admits transitively and dispatches", async () => {
    const calls = { starts: [] as unknown[] };
    const receipt = await new ProjectControlPlane(composedBundle("gpt-4o-mini"), { schemas }).start(
      "parent",
      "prod",
      "exec-1",
      { text: "hi" },
      { clientFactory: fakeFactory(calls) as never },
    );
    expect(receipt.workflow_id).toBe("exec-1");
    expect(calls.starts).toHaveLength(1);
  });
});
