import { afterEach, describe, expect, it } from "vitest";

import {
  emptyProfileSources,
  loadProfileSpec,
  loadEnvironmentSpec,
  loadPolicySpec,
  loadProjectSpec,
  validateProjectBundle,
  type ProjectBundleSources,
  type ProjectResolvedWorkflowValidation,
} from "../src/index.js";

const PROJECT = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
policies:
  strict: strict.yaml
environments:
  prod: prod.yaml
validation:
  targets:
    prod-review:
      workflows: [review]
      environment: prod
      policies: [strict]
`);

/** A valid `review` workflow (openai/gpt-4o-mini); `extra` appends runtime/observability lines.
 * `workflowName` must be distinct per workflow in a project — the validator now reports
 * duplicate_workflow_name for collisions (#565). */
const reviewYaml = (extra = "", workflowName = "W") => `
project: p
name: n
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
  observability: { type: console${extra} }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: ${workflowName}
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

const PROD_ENV = "name: prod\noverrides: { task_queue: prod-queue }\n";
const STRICT = "name: strict\nproviders: { allowed: { openai: { models: [gpt-4o-mini] } } }\n";
const BLOCKS = "name: strict\nproviders: { allowed: { openai: { models: [gpt-4o] } } }\n"; // forbids the workflow's model

/** Bundle sources for the `review` workflow under `prod`, with a chosen policy + workflow text. */
function sourcesFor(policyYaml: string, workflowYaml: string): ProjectBundleSources {
  return {
    policies: { strict: loadPolicySpec(policyYaml) },
    environments: { prod: loadEnvironmentSpec(PROD_ENV) },
    workflows: { review: workflowYaml },
    profiles: emptyProfileSources(),
  };
}

const check = (wf: ProjectResolvedWorkflowValidation, code: string) => wf.checks.find((c) => c.code === code);

describe("validateProjectBundle — reference-only mode (#454)", () => {
  it("returns a clean structural report when nothing is selected", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, reviewYaml()));
    expect(report).toEqual({
      projectName: "acme",
      ok: true,
      issues: [],
      workflows: [
        {
          workflowId: "review",
          path: "review.yaml",
          yamlProject: "p",
          yamlName: "n",
          workflowName: "W",
          taskQueue: "base-queue",
        },
      ],
      resolvedWorkflows: [],
    });
  });

  it("flags a declared environment with no provided source (missing_environment_source)", () => {
    const sources: ProjectBundleSources = {
      policies: { strict: loadPolicySpec(STRICT) },
      environments: {}, // `prod` is declared by the manifest but not provided
      workflows: { review: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(PROJECT, sources);
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "missing_environment_source",
      message: "environment 'prod' is declared but no source was provided",
      reference: "prod",
    });
  });

  it("flags declared-but-unsourced profiles and dangling workflow selections (#570)", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml, profiles: { provider: anthropic-prod, nonsense: x } }
  - { id: intake, path: intake.yaml, profiles: { runtime: no-such-profile } }
profiles:
  provider:
    anthropic-prod: profiles/anthropic.yaml
`);
    const sources: ProjectBundleSources = {
      policies: {},
      environments: {},
      workflows: { review: reviewYaml(), intake: reviewYaml() },
      profiles: emptyProfileSources(), // `anthropic-prod` declared but not provided
    };
    const report = validateProjectBundle(project, sources);
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "missing_profile_source",
      message: "provider profile 'anthropic-prod' is declared but no source was provided",
      reference: "anthropic-prod",
    });
    // Python `_validate_profiles`: an unknown kind is invalid_profile_selection; a known kind
    // selecting an undeclared id is unknown_profile_reference.
    // First unknown kind stops the whole selection's checks with ONE issue (Python raises there).
    expect(report.issues).toContainEqual({
      code: "invalid_profile_selection",
      message:
        "workflow 'review' profile selection selects unknown profile kind 'nonsense'; valid kinds: provider, registry, runtime",
      reference: "review",
    });
    expect(report.issues).toContainEqual({
      code: "unknown_profile_reference",
      message: "workflow 'intake' profile selection selects unknown runtime profile: no-such-profile",
      reference: "intake",
    });
  });

  it("profile-selection issues carry the manifest `path` when the caller provides one (#643)", () => {
    // Python attaches `path=project.manifest_path` to BOTH profile-selection codes; the CP
    // threads its manifestPath through so /validate matches the conformance golden's shape.
    const project = loadProjectSpec(
      'version: "1"\nname: acme\nworkflows:\n' +
        "  - { id: review, path: review.yaml, profiles: { nonsense: x } }\n" +
        "  - { id: intake, path: review.yaml, profiles: { runtime: no-such-profile } }\n",
    );
    const sources: ProjectBundleSources = {
      policies: {},
      environments: {},
      workflows: { review: reviewYaml(), intake: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(project, sources, { manifestPath: "/abs/typeflux.project.yaml" });
    expect(report.issues).toContainEqual({
      code: "invalid_profile_selection",
      message:
        "workflow 'review' profile selection selects unknown profile kind 'nonsense'; valid kinds: provider, registry, runtime",
      reference: "review",
      path: "/abs/typeflux.project.yaml",
    });
    expect(report.issues).toContainEqual({
      code: "unknown_profile_reference",
      message: "workflow 'intake' profile selection selects unknown runtime profile: no-such-profile",
      reference: "intake",
      path: "/abs/typeflux.project.yaml",
    });
  });

  it("flags an injected profile source whose self-declared kind mismatches its section (codex)", () => {
    const project = loadProjectSpec(
      'version: "1"\nname: acme\nworkflows:\n  - { id: review, path: review.yaml }\n' +
        "profiles:\n  provider:\n    foo: profiles/foo.yaml\n",
    );
    const sources: ProjectBundleSources = {
      policies: {},
      environments: {},
      workflows: { review: reviewYaml() },
      profiles: {
        ...emptyProfileSources(),
        // Parsed WITHOUT declaredKind (injection API) — self-declares runtime under provider.
        provider: { foo: loadProfileSpec("name: foo\nkind: runtime\n") },
      },
    };
    const report = validateProjectBundle(project, sources);
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "invalid_component_profile",
      message: "profile 'foo' is referenced under profiles.provider but declares kind: runtime",
      reference: "foo",
    });
  });

  it("checks environment-level workflow_profiles selections too (#570, codex)", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
environments:
  prod: envs/prod.yaml
`);
    const sources: ProjectBundleSources = {
      policies: {},
      environments: {
        prod: loadEnvironmentSpec(
          "name: prod\nworkflows:\n  review:\n    profiles: { runtime: no-such-profile }\n",
        ),
      },
      workflows: { review: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(project, sources);
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "unknown_profile_reference",
      message:
        "environment 'prod' profile selection for workflow 'review' selects unknown runtime profile: no-such-profile",
      reference: "prod",
    });
  });

  it("ignores an over-provided environment source the project does not declare (codex)", () => {
    const extraSources: ProjectBundleSources = {
      policies: { strict: loadPolicySpec(STRICT) },
      environments: {
        prod: loadEnvironmentSpec(PROD_ENV),
        // An unrelated, undeclared environment with a dangling per-workflow override.
        staging: loadEnvironmentSpec("name: staging\nworkflows: { ghost: { overrides: {} } }\n"),
      },
      workflows: { review: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(PROJECT, extraSources, { environmentId: "prod" });
    expect(report.issues.some((i) => i.code === "unknown_environment_workflow")).toBe(false);
    expect(report.ok).toBe(true);
  });

  it("surfaces a structural reference issue (an undeclared target policy) and does not resolve", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows: [{ id: review, path: review.yaml }]
policies: { strict: strict.yaml }
environments: { prod: prod.yaml }
validation:
  targets:
    t: { workflows: [review], policies: [ghost] }
`);
    const report = validateProjectBundle(project, sourcesFor(STRICT, reviewYaml()), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues.some((i) => i.code === "unknown_target_policy")).toBe(true);
    // A reference issue short-circuits resolution.
    expect(report.resolvedWorkflows).toEqual([]);
  });
});

describe("validateProjectBundle — selection guards (#454)", () => {
  it("requires an environmentId when workflows/policies are selected", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, reviewYaml()), { workflowIds: ["review"] });
    expect(report.issues).toEqual([
      { code: "validation_environment_required", message: "workflowIds requires an environmentId for resolved project validation" },
    ]);
    expect(report.resolvedWorkflows).toEqual([]);
  });

  it("flags unknown environment / workflow / policy ids and skips resolution", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, reviewYaml()), {
      environmentId: "ghost-env",
      workflowIds: ["ghost-wf"],
      policyIds: ["ghost-pol"],
    });
    const codes = report.issues.map((i) => i.code).sort();
    expect(codes).toEqual(["unknown_validation_environment", "unknown_validation_policy", "unknown_validation_workflow"]);
    expect(report.resolvedWorkflows).toEqual([]);
  });
});

describe("validateProjectBundle — resolved bundle, compliant workflow (#454)", () => {
  it("runs every per-workflow check and reports ok for a compliant workflow", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, reviewYaml()), { environmentId: "prod" });
    expect(report.ok).toBe(true);
    expect(report.issues).toEqual([]);
    expect(report.resolvedWorkflows).toHaveLength(1);
    const wf = report.resolvedWorkflows[0]!;
    expect(wf).toMatchObject({ workflowId: "review", environmentId: "prod", ok: true, yamlName: "n", workflowName: "W" });
    // The env override reached the resolved spec.
    expect(wf.taskQueue).toBe("prod-queue");
    // Resolution + observability + policy pass; the constructibility graph builds.
    expect(check(wf, "environment_workflow_resolution")?.status).toBe("passed");
    expect(check(wf, "observability_config")?.status).toBe("passed");
    expect(check(wf, "policy_provider")?.status).toBe("passed");
    expect(check(wf, "workflow_graph")).toMatchObject({ status: "passed", details: { step_count: 1 } });
    // Import checks are N/A in the injection model (skipped, documented).
    expect(check(wf, "provider_import_policy")?.status).toBe("skipped");
    expect(check(wf, "activity_imports")?.status).toBe("skipped");
    // The manifest check defaults ON (Python `execution_manifest = True`) → built with a digest.
    const manifest = check(wf, "execution_manifest");
    expect(manifest).toMatchObject({ status: "passed", details: { activity_count: 1, map_step_count: 0 } });
    expect(typeof manifest?.details?.plan_digest).toBe("string");
  });

  it("skips the manifest only when observability disables it explicitly", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, reviewYaml(", execution_manifest: false")), {
      environmentId: "prod",
    });
    expect(check(report.resolvedWorkflows[0]!, "execution_manifest")).toMatchObject({
      status: "skipped",
      message: /disabled/,
    });
  });

  it("counts distinct activity definitions and map steps in the manifest details", () => {
    const mapWorkflow = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
  observability: { type: console, execution_manifest: true }
activities:
  definitions:
    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }
    - { name: b, input: schemas:In, output: schemas:Out, prompt: p/x }
workflow:
  name: W
  input: schemas:In
  steps:
    - { id: s, activity: a }
    - { id: m, map: { activity: b, over: s } }
`;
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, mapWorkflow), { environmentId: "prod" });
    expect(report.ok).toBe(true);
    // Two definitions (a, b); one of the two steps is a map step.
    expect(check(report.resolvedWorkflows[0]!, "execution_manifest")).toMatchObject({
      status: "passed",
      details: { activity_count: 2, map_step_count: 1 },
    });
  });

  it("treats an entirely-absent observability block as manifest-enabled (Python default)", () => {
    const noObservability = `
project: p
name: n
task_queue: q
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
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, noObservability), { environmentId: "prod" });
    const wf = report.resolvedWorkflows[0]!;
    expect(check(wf, "observability_config")?.status).toBe("passed");
    expect(check(wf, "execution_manifest")?.status).toBe("passed");
  });

  it("defaults to every declared workflow when none is named", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, reviewYaml()), { environmentId: "prod" });
    expect(report.resolvedWorkflows.map((w) => w.workflowId)).toEqual(["review"]);
  });
});

describe("validateProjectBundle — conditional #796/#797 checks", () => {
  const tlsYaml = (tls: string) => `
project: p
name: n
task_queue: base-queue
runtime:
  temporal:
    api_key: { value_from: { env: TLS_CHECK_TEMPORAL_KEY } }${tls}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
  observability: { type: console }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;

  afterEach(() => {
    delete process.env["TLS_CHECK_TEMPORAL_KEY"];
  });

  it("fails temporal_tls_invariant when the reference resolves and tls is disabled (#796)", () => {
    process.env["TLS_CHECK_TEMPORAL_KEY"] = "tmprl-live-key";
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, tlsYaml("")), { environmentId: "prod" });
    const wf = report.resolvedWorkflows[0]!;
    const tls = check(wf, "temporal_tls_invariant");
    expect(tls?.status).toBe("failed");
    expect(tls?.message).not.toContain("tmprl-live-key");
    expect(report.ok).toBe(false);
  });

  it("defers explicitly when the reference source is unresolvable (#796)", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, tlsYaml("")), { environmentId: "prod" });
    const wf = report.resolvedWorkflows[0]!;
    const tls = check(wf, "temporal_tls_invariant");
    expect(tls?.status).toBe("skipped");
    expect(tls?.message).toContain("enforced at client connect");
    expect(report.ok).toBe(true);
  });

  it("emits nothing when tls is enabled", () => {
    process.env["TLS_CHECK_TEMPORAL_KEY"] = "tmprl-live-key";
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, tlsYaml("\n    tls: true")), {
      environmentId: "prod",
    });
    const wf = report.resolvedWorkflows[0]!;
    expect(check(wf, "temporal_tls_invariant")).toBeUndefined();
  });

  it("states the codec's runtime fail-closed semantics for required: false keys (#797)", () => {
    const codecYaml = `
project: p
name: n
task_queue: base-queue
runtime:
  temporal:
    payload_codec:
      type: aes
      current: k1
      keys:
        - { id: k1, value_from: { env: CODEC_KEY_ONE, required: false } }
        - { id: k2, value_from: { env: CODEC_KEY_TWO } }
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
  observability: { type: console }
activities:
  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, codecYaml), { environmentId: "prod" });
    const wf = report.resolvedWorkflows[0]!;
    const notice = check(wf, "payload_codec_presence");
    expect(notice?.status).toBe("passed");
    expect(notice?.details).toMatchObject({ deferred_keys: ["k1"] });
    expect(String((notice?.details as Record<string, unknown>)["runtime_behavior"])).toContain(
      "fail-closes at runtime",
    );
  });
});

describe("validateProjectBundle — failing per-workflow checks become issues (#454)", () => {
  it("a policy violation fails the workflow and skips the downstream constructibility checks", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(BLOCKS, reviewYaml()), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "resolved_policy_provider_failed",
      message: expect.stringMatching(/gpt-4o-mini/),
      reference: "prod:review",
      path: "review.yaml",
    });
    const wf = report.resolvedWorkflows[0]!;
    expect(wf.ok).toBe(false);
    // Python dependency ordering: graph + manifest are skipped once policy enforcement failed.
    expect(check(wf, "workflow_graph")).toMatchObject({ status: "skipped", message: /policy enforcement failed/ });
    expect(check(wf, "execution_manifest")).toMatchObject({ status: "skipped", message: /policy enforcement failed/ });
  });

  it("a workflow whose graph does not build fails workflow_graph and skips the manifest", () => {
    // A step id of `input` is a valid string (loads) but reserved for the plan (throws).
    const badGraph = reviewYaml().replace("{ id: s, activity: a }", "{ id: input, activity: a }");
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, badGraph), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues.some((i) => i.code === "resolved_workflow_graph_failed")).toBe(true);
    const wf = report.resolvedWorkflows[0]!;
    expect(check(wf, "workflow_graph")).toMatchObject({ status: "failed", message: /reserved/ });
    expect(check(wf, "execution_manifest")).toMatchObject({ status: "skipped", message: /graph validation failed/ });
  });

  it("fails workflow_graph when a step references an activity the spec does not declare (codex)", () => {
    // A governed bundle must declare every activity it references; a dangling ref is what
    // `assembleYamlRuntime` rejects at runtime (code-first injected activities are out of scope).
    const danglingRef = reviewYaml().replace("activity: a }", "activity: b }"); // step → `b`, only `a` is defined
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, danglingRef), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues.some((i) => i.code === "resolved_workflow_graph_failed")).toBe(true);
    const wf = report.resolvedWorkflows[0]!;
    expect(check(wf, "workflow_graph")).toMatchObject({ status: "failed", message: /not defined in the spec: b/ });
    // A non-constructible graph skips the manifest.
    expect(check(wf, "execution_manifest")).toMatchObject({ status: "skipped", message: /graph validation failed/ });
  });

  it("fails workflow_graph on a duplicate activity definition name (assembly would reject it; codex)", () => {
    const dupDef = reviewYaml().replace(
      "definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
      "definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }, { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
    );
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, dupDef), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(check(report.resolvedWorkflows[0]!, "workflow_graph")).toMatchObject({
      status: "failed",
      message: /duplicate activity definition name/,
    });
  });

  it("fails workflow_graph on a schema-incompatible step chain (a step input != the prior output; codex)", () => {
    // Two steps whose refs don't chain: `a` outputs schemas:Mid, but `b` declares input schemas:Other.
    const chained = reviewYaml()
      .replace(
        "definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
        "definitions: [{ name: a, input: schemas:In, output: schemas:Mid, prompt: p/x }, { name: b, input: schemas:Other, output: schemas:Out, prompt: p/x }]",
      )
      .replace("steps: [{ id: s, activity: a }]", "steps: [{ id: s1, activity: a }, { id: s2, activity: b }]");
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, chained), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(check(report.resolvedWorkflows[0]!, "workflow_graph")).toMatchObject({ status: "failed", message: /expects input/ });
    // A non-type-checking chain skips the manifest, like any non-constructible graph.
    expect(check(report.resolvedWorkflows[0]!, "execution_manifest")).toMatchObject({ status: "skipped", message: /graph validation failed/ });
  });

  it("fails workflow_graph when a step consumes a collect-less map's array output (Bugbot)", () => {
    // A map WITHOUT collect yields an array; the following activity `b` cannot consume it as a single input.
    const collectlessMap = reviewYaml()
      .replace(
        "definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]",
        "definitions:\n    - { name: a, input: schemas:In, output: schemas:Mid, prompt: p/x }\n    - { name: b, input: schemas:Mid, output: schemas:Out, prompt: p/x }",
      )
      .replace(
        "steps: [{ id: s, activity: a }]",
        "steps:\n    - { id: m, map: { activity: a, over: input.items } }\n    - { id: s2, activity: b }",
      );
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, collectlessMap), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(check(report.resolvedWorkflows[0]!, "workflow_graph")).toMatchObject({ status: "failed", message: /cannot consume the array/ });
  });

  it("fails workflow_graph when workflow.output does not match the terminal step's output (codex)", () => {
    // Activity `a` outputs schemas:Out, but the workflow declares output schemas:Wrong.
    const badOutput = reviewYaml().replace("input: schemas:In\n  steps", "input: schemas:In\n  output: schemas:Wrong\n  steps");
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, badOutput), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(check(report.resolvedWorkflows[0]!, "workflow_graph")).toMatchObject({ status: "failed", message: /does not match the terminal/ });
  });

  it("resolves and tags every selected workflow independently (multi-workflow)", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
  - { id: summary, path: summary.yaml }
policies: { strict: strict.yaml }
environments: { prod: prod.yaml }
validation:
  targets:
    all: { workflows: [review, summary], environment: prod, policies: [strict] }
`);
    const sources: ProjectBundleSources = {
      policies: { strict: loadPolicySpec(STRICT) },
      environments: { prod: loadEnvironmentSpec(PROD_ENV) },
      workflows: {
        review: reviewYaml(), // compliant (gpt-4o-mini)
        summary: reviewYaml("", "W2").replace("model: gpt-4o-mini", "model: gpt-4o"), // violates strict
      },
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(project, sources, { environmentId: "prod" });
    expect(report.resolvedWorkflows.map((w) => w.workflowId)).toEqual(["review", "summary"]);
    expect(report.resolvedWorkflows[0]!.ok).toBe(true);
    expect(report.resolvedWorkflows[1]!.ok).toBe(false);
    // The failing issue is tagged to the offending workflow only.
    expect(report.issues).toContainEqual({
      code: "resolved_policy_provider_failed",
      message: expect.stringMatching(/gpt-4o/),
      reference: "prod:summary",
      path: "summary.yaml",
    });
    expect(report.ok).toBe(false);
  });

  it("evaluates policy under the selected environment's variables, not just host process.env (codex)", () => {
    // The policy allow-lists a Temporal region that `validateTemporal` reads from
    // `process.env.TYPEFLUX_TEMPORAL_REGION`; the environment (not the host) supplies it.
    const regionPolicy = "name: strict\nruntime: { temporal: { allowed_regions: [us-east] } }\n";
    const envWithRegion = "name: prod\nvariables: { TYPEFLUX_TEMPORAL_REGION: us-east }\noverrides: {}\n";
    const sources: ProjectBundleSources = {
      policies: { strict: loadPolicySpec(regionPolicy) },
      environments: { prod: loadEnvironmentSpec(envWithRegion) },
      workflows: { review: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    expect(process.env["TYPEFLUX_TEMPORAL_REGION"]).toBeUndefined(); // host does not provide it
    const report = validateProjectBundle(PROJECT, sources, { environmentId: "prod" });
    expect(check(report.resolvedWorkflows[0]!, "policy_temporal")?.status).toBe("passed");
    expect(report.ok).toBe(true);
    // The overlay is restored afterward — the host env is left untouched.
    expect(process.env["TYPEFLUX_TEMPORAL_REGION"]).toBeUndefined();
  });

  it("does not corrupt process.env when a .env value uses a prototype-member key (no leak)", () => {
    const sources: ProjectBundleSources = {
      policies: { strict: loadPolicySpec(STRICT) },
      environments: { prod: loadEnvironmentSpec(PROD_ENV) },
      workflows: { review: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    validateProjectBundle(PROJECT, sources, {
      environmentId: "prod",
      envFileValues: JSON.parse('{"__proto__":"x","TF_TEMP_MARKER":"y"}') as Record<string, string>,
    });
    // No corrupt own `__proto__`, no Object.prototype pollution, and the temp key is restored (gone).
    expect(Object.hasOwn(process.env, "__proto__")).toBe(false);
    expect(({} as Record<string, unknown>).x).toBeUndefined();
    expect(process.env["TF_TEMP_MARKER"]).toBeUndefined();
  });

  it("applies a VALID explicit policyIds override to the resolved workflow's compliance", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows: [{ id: review, path: review.yaml }]
policies: { strict: strict.yaml, blocks: blocks.yaml }
environments: { prod: prod.yaml }
validation:
  targets:
    t: { workflows: [review], environment: prod, policies: [strict] }
`);
    const sources: ProjectBundleSources = {
      policies: {
        strict: loadPolicySpec(STRICT),
        blocks: loadPolicySpec("name: blocks\nproviders: { allowed: { openai: { models: [gpt-4o] } } }\n"),
      },
      environments: { prod: loadEnvironmentSpec(PROD_ENV) },
      workflows: { review: reviewYaml() },
      profiles: emptyProfileSources(),
    };
    // Target-derived (strict) → compliant; the explicit `blocks` override forbids the model.
    expect(validateProjectBundle(project, sources, { environmentId: "prod" }).ok).toBe(true);
    const overridden = validateProjectBundle(project, sources, { environmentId: "prod", policyIds: ["blocks"] });
    expect(overridden.ok).toBe(false);
    expect(overridden.issues.some((i) => i.code === "resolved_policy_provider_failed")).toBe(true);
  });

  it("a malformed workflow source is a reference-level parse issue that bails resolution (#565)", () => {
    const report = validateProjectBundle(PROJECT, sourcesFor(STRICT, "::: not valid yaml :::"), { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "invalid_workflow_yaml",
      message: expect.stringContaining("workflow 'review' failed to load"),
      reference: "review",
      path: "review.yaml",
    });
    expect(report.resolvedWorkflows).toEqual([]);
  });

  it("reference-parses each workflow BARE — no defaults layer (Python loader parity)", () => {
    // Python's reference-level `load_yaml_spec(workflow_path)` parses the raw file with NO
    // `defaults.runtime` merge (that layer applies at RESOLUTION), so a workflow relying on
    // project defaults for the required `runtime.provider` fails reference validation on
    // both sides alike — with the failing file's manifest reference on the issue.
    const project = loadProjectSpec(`
version: "1"
name: acme
defaults:
  runtime:
    provider: { type: openai, model: gpt-4o-mini }
workflows:
  - { id: review, path: review.yaml }
`);
    const workflowNeedingDefaults = reviewYaml().replace(
      "  provider: { type: openai, model: gpt-4o-mini }\n",
      "",
    );
    const report = validateProjectBundle(project, {
      policies: {},
      environments: {},
      workflows: { review: workflowNeedingDefaults },
      profiles: emptyProfileSources(),
    });
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "invalid_workflow_yaml",
      message: expect.stringContaining("workflow 'review' failed to load"),
      reference: "review",
      path: "review.yaml",
    });
  });

  it("colliding loaded workflow names are ONE aggregated duplicate_workflow_name issue (#565)", () => {
    const project = loadProjectSpec(`
version: "1"
name: acme
workflows:
  - { id: review, path: review.yaml }
  - { id: summary, path: summary.yaml }
environments: { prod: prod.yaml }
`);
    const sources: ProjectBundleSources = {
      policies: {},
      environments: { prod: loadEnvironmentSpec(PROD_ENV) },
      workflows: { review: reviewYaml(), summary: reviewYaml() }, // both load as workflow.name W
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(project, sources, { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "duplicate_workflow_name",
      message: "duplicate YAML workflow name(s): W",
    });
    expect(report.issues.filter((i) => i.code === "duplicate_workflow_name")).toHaveLength(1);
    expect(report.resolvedWorkflows).toEqual([]); // reference issues bail resolution
  });

  it("a workflow that fails to RESOLVE under the environment reports environment_workflow_resolution failed", () => {
    // Parses at the reference level (the host env supplies ${TF_TEST_REF_ONLY}) but fails to
    // resolve under the environment: baseEnv {} plus a profile without the variable makes the
    // resolution-time interpolation miss it. Reference-parse OK, resolution failed — the branch
    // the (#565) reference checks must NOT have swallowed.
    process.env["TF_TEST_REF_ONLY"] = "host-queue";
    try {
      const sources: ProjectBundleSources = {
        policies: { strict: loadPolicySpec(STRICT) },
        // No task_queue override — overrides layer BEFORE interpolation and would mask the ref.
        environments: { prod: loadEnvironmentSpec("name: prod\noverrides: {}\n") },
        workflows: { review: reviewYaml().replace("task_queue: base-queue", "task_queue: ${TF_TEST_REF_ONLY}") },
        profiles: emptyProfileSources(),
      };
      const report = validateProjectBundle(PROJECT, sources, { environmentId: "prod", baseEnv: {} });
      expect(report.ok).toBe(false);
      expect(report.issues.some((i) => i.code === "resolved_environment_workflow_resolution_failed")).toBe(true);
      const wf = report.resolvedWorkflows[0]!;
      expect(wf.ok).toBe(false);
      expect(check(wf, "environment_workflow_resolution")?.status).toBe("failed");
      // No further checks once resolution failed.
      expect(wf.checks).toHaveLength(1);
    } finally {
      delete process.env["TF_TEST_REF_ONLY"];
    }
  });

  it("reports a declared workflow with no provided source and short-circuits resolution", () => {
    const sources: ProjectBundleSources = {
      policies: { strict: loadPolicySpec(STRICT) },
      environments: { prod: loadEnvironmentSpec(PROD_ENV) },
      workflows: {}, // no source for `review`
      profiles: emptyProfileSources(),
    };
    const report = validateProjectBundle(PROJECT, sources, { environmentId: "prod" });
    expect(report.ok).toBe(false);
    expect(report.issues).toContainEqual({
      code: "missing_workflow_source",
      message: "workflow 'review' is declared but no source was provided",
      reference: "review",
    });
    // A reference-level gap short-circuits resolution (Python parity).
    expect(report.resolvedWorkflows).toEqual([]);
  });
});
