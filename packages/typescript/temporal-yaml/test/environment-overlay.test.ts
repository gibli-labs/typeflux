import { describe, expect, it } from "vitest";

import {
  environmentInterpolationEnv,
  loadEnvironmentSpec,
  loadProjectSpec,
  mergedEnvironmentOverrides,
  resolveEnvironmentWorkflow,
  resolveEnvironmentWorkflowFromText,
  stringifyEnvValue,
  validateEnvironmentWorkflowReferences,
} from "../src/index.js";

/** A minimal, valid workflow spec with base task_queue + provider, overridable via the environment. */
const WORKFLOW = `
project: p
name: n
task_queue: base-queue
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: hi } }
  provider: { type: openai, model: gpt-4o-mini }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      activity: a
`;

const ENVIRONMENT = `
name: prod
variables:
  NAMESPACE: prod-ns
overrides:
  task_queue: prod-queue
  runtime:
    temporal: { namespace: base-from-env }
workflows:
  W:
    overrides:
      runtime:
        temporal: { address: wf.temporal:7233 }
`;

describe("mergedEnvironmentOverrides (#454; Python _merged_overrides)", () => {
  it("deep-merges the env-wide overrides with the per-workflow block (workflow wins per key)", () => {
    const env = loadEnvironmentSpec(ENVIRONMENT);
    const merged = mergedEnvironmentOverrides(env, "W");
    expect(merged).toEqual({
      task_queue: "prod-queue",
      runtime: { temporal: { namespace: "base-from-env", address: "wf.temporal:7233" } },
    });
  });

  it("returns only the env-wide overrides for a workflow with no per-workflow block", () => {
    const env = loadEnvironmentSpec(ENVIRONMENT);
    expect(mergedEnvironmentOverrides(env, "other")).toEqual({
      task_queue: "prod-queue",
      runtime: { temporal: { namespace: "base-from-env" } },
    });
  });

  it("does not mutate the environment's own override maps across calls", () => {
    const env = loadEnvironmentSpec(ENVIRONMENT);
    mergedEnvironmentOverrides(env, "W");
    // The per-workflow merge must not have leaked `address` back into the env-wide block.
    expect((env.overrides.runtime as Record<string, unknown>).temporal).toEqual({ namespace: "base-from-env" });
  });

  it("never writes through to the environment across successive calls for different workflows", () => {
    const env = loadEnvironmentSpec(ENVIRONMENT);
    // Resolve W (has a per-workflow override, forcing a nested record-record merge) then
    // another workflow; the FUNCTION must not mutate the source on either call. (The
    // merged result may alias one-sided subtrees — the documented read-only contract — so
    // this locks non-mutation of the source, not independence of the aliased result.)
    const w = mergedEnvironmentOverrides(env, "W");
    const other = mergedEnvironmentOverrides(env, "other");
    expect(env.overrides).toEqual({
      task_queue: "prod-queue",
      runtime: { temporal: { namespace: "base-from-env" } },
    });
    expect((env.workflows["W"] as { overrides: unknown }).overrides).toEqual({
      runtime: { temporal: { address: "wf.temporal:7233" } },
    });
    // W merged in its per-workflow address; the other workflow only sees the env-wide block.
    expect((w.runtime as Record<string, unknown>).temporal).toEqual({
      namespace: "base-from-env",
      address: "wf.temporal:7233",
    });
    expect((other.runtime as Record<string, unknown>).temporal).toEqual({ namespace: "base-from-env" });
  });

  it("resolves an own workflow entry only (a 'constructor'-keyed workflow, not the prototype)", () => {
    const env = loadEnvironmentSpec(`
name: e
workflows:
  constructor:
    overrides: { task_queue: ctor-queue }
`);
    expect(mergedEnvironmentOverrides(env, "constructor")).toEqual({ task_queue: "ctor-queue" });
    // A workflow id that is only an inherited Object member must NOT resolve to it.
    expect(mergedEnvironmentOverrides(env, "toString")).toEqual({});
  });
});

describe("stringifyEnvValue (#454; Python _stringify_env_value)", () => {
  it("maps booleans to true/false and stringifies numbers/strings", () => {
    expect(stringifyEnvValue(true)).toBe("true");
    expect(stringifyEnvValue(false)).toBe("false");
    expect(stringifyEnvValue(42)).toBe("42");
    expect(stringifyEnvValue("hi")).toBe("hi");
  });
});

describe("environmentInterpolationEnv (#454; build_project_environment_application)", () => {
  it("layers base < .env files < inline variables (variables win)", () => {
    const env = loadEnvironmentSpec(`
name: e
variables:
  SHARED: from-variables
  ONLY_VAR: v
`);
    const context = environmentInterpolationEnv(env, {
      base: { SHARED: "from-base", ONLY_BASE: "b" },
      envFileValues: { SHARED: "from-dotenv", ONLY_DOTENV: "d" },
    });
    expect(context.SHARED).toBe("from-variables"); // variables beat .env and base
    expect(context.ONLY_DOTENV).toBe("d"); // .env beats base
    expect(context.ONLY_BASE).toBe("b"); // base retained
    expect(context.ONLY_VAR).toBe("v");
  });

  it("stringifies non-string variable values", () => {
    const env = loadEnvironmentSpec(`
name: e
variables:
  ENABLED: true
  COUNT: 7
`);
    const context = environmentInterpolationEnv(env, { base: {} });
    expect(context.ENABLED).toBe("true");
    expect(context.COUNT).toBe("7");
  });

  it("defaults its base to process.env", () => {
    const marker = "TF_OVERLAY_TEST_MARKER";
    process.env[marker] = "present";
    try {
      const env = loadEnvironmentSpec(`name: e\nvariables: { X: y }`);
      const context = environmentInterpolationEnv(env);
      expect(context[marker]).toBe("present");
      expect(context.X).toBe("y");
    } finally {
      delete process.env[marker];
    }
  });

  it("sets a caller-provided '__proto__' .env value as an own property (no pollution)", () => {
    // The caller-read `.env` values are arbitrary keys — a `__proto__` entry must land
    // as an own data property (readable back by the interpolator), never routing through
    // the prototype setter. (A `__proto__` *variable* is instead dropped upstream: zod's
    // record sanitizes it at parse, the same class as `.strict()` ignoring `__proto__`.)
    const env = loadEnvironmentSpec(`name: e\nvariables: { NORMAL: ok }`);
    const context = environmentInterpolationEnv(env, {
      base: {},
      envFileValues: JSON.parse('{"__proto__":"injected","OK":"v"}') as Record<string, string>,
    });
    expect(Object.hasOwn(context, "__proto__")).toBe(true);
    expect(Object.getOwnPropertyDescriptor(context, "__proto__")?.value).toBe("injected");
    // defineProperty keeps it an inert OWN property — Object.prototype is untouched.
    expect(({} as Record<string, unknown>).injected).toBeUndefined();
    expect(Object.getPrototypeOf(context)).toBe(Object.prototype);
    expect(context.OK).toBe("v");
  });

  it("returns the base BY REFERENCE when there is nothing to overlay (no copy)", () => {
    const env = loadEnvironmentSpec(`name: e`); // no variables, no env_files
    const base = { A: "1" };
    // No variables and no .env values → the base is passed through untouched (mirrors
    // loadYamlSpec's own `env ?? process.env` default; no wasted full-env copy).
    expect(environmentInterpolationEnv(env, { base })).toBe(base);
    // An empty envFileValues map is still "nothing to overlay".
    expect(environmentInterpolationEnv(env, { base, envFileValues: {} })).toBe(base);
  });
});

describe("resolveEnvironmentWorkflow (#454; injection-based resolve_project_workflow)", () => {
  it("applies the environment's merged overrides onto the workflow spec", () => {
    const environment = loadEnvironmentSpec(ENVIRONMENT);
    const spec = resolveEnvironmentWorkflow(WORKFLOW, { environment, workflowId: "W", baseEnv: {} });
    expect(spec.task_queue).toBe("prod-queue"); // env override beats the base YAML
    expect(spec.runtime.temporal.namespace).toBe("base-from-env"); // env-wide override
    expect(spec.runtime.temporal.address).toBe("wf.temporal:7233"); // per-workflow override
    expect(spec.runtime.provider.model).toBe("gpt-4o-mini"); // untouched base survives
  });

  it("interpolates ${VAR} in an override against the environment's variables", () => {
    const environment = loadEnvironmentSpec(`
name: prod
variables:
  NS: interpolated-ns
overrides:
  runtime:
    temporal: { namespace: "\${NS}" }
`);
    // baseEnv `{}` proves the value came from the environment's variables, not process.env.
    const spec = resolveEnvironmentWorkflow(WORKFLOW, { environment, workflowId: "W", baseEnv: {} });
    expect(spec.runtime.temporal.namespace).toBe("interpolated-ns");
  });

  it("does not resolve an inherited name from the interpolation context (hermetic base; codex)", () => {
    // With `baseEnv: {}` and no such variable declared, `${constructor}` must be a
    // missing-variable error — never the inherited Object.prototype member.
    const environment = loadEnvironmentSpec(`
name: prod
overrides:
  runtime:
    temporal: { namespace: "\${constructor}" }
`);
    expect(() => resolveEnvironmentWorkflow(WORKFLOW, { environment, workflowId: "W", baseEnv: {} })).toThrow(
      /missing environment variable: constructor/,
    );
  });

  it("layers runtimeDefaults BENEATH the workflow YAML and the env overrides", () => {
    const environment = loadEnvironmentSpec(`name: prod\noverrides: {}`);
    const spec = resolveEnvironmentWorkflow(WORKFLOW, {
      environment,
      workflowId: "W",
      runtimeDefaults: { provider_retry: { max_attempts: 5 } },
      baseEnv: {},
    });
    // The YAML omits provider_retry → the project default fills it.
    expect(spec.runtime.provider_retry?.max_attempts).toBe(5);
    // The YAML's provider still wins over any default.
    expect(spec.runtime.provider.type).toBe("openai");
  });

  it("resolves an override's ${VAR} from a caller-provided .env value (end-to-end)", () => {
    const environment = loadEnvironmentSpec(`
name: prod
overrides:
  runtime:
    temporal: { namespace: "\${DEPLOY_NS}" }
`);
    // The override references a var supplied only via envFileValues (what the caller read
    // from the env's .env files) — proves the resolver threads envFileValues into interpolation.
    const spec = resolveEnvironmentWorkflow(WORKFLOW, {
      environment,
      workflowId: "W",
      baseEnv: {},
      envFileValues: { DEPLOY_NS: "ns-from-dotenv" },
    });
    expect(spec.runtime.temporal.namespace).toBe("ns-from-dotenv");
  });

  it("an inline variable beats a .env value of the same name through the resolver", () => {
    const environment = loadEnvironmentSpec(`
name: prod
variables:
  NS: from-variables
overrides:
  runtime:
    temporal: { namespace: "\${NS}" }
`);
    const spec = resolveEnvironmentWorkflow(WORKFLOW, {
      environment,
      workflowId: "W",
      baseEnv: {},
      envFileValues: { NS: "from-dotenv" },
    });
    expect(spec.runtime.temporal.namespace).toBe("from-variables");
  });

  it("a runtimeDefaults of {} is a no-op (does not inject an empty runtime layer)", () => {
    const environment = loadEnvironmentSpec(`name: prod\noverrides: {}`);
    const spec = resolveEnvironmentWorkflow(WORKFLOW, {
      environment,
      workflowId: "W",
      runtimeDefaults: {},
      baseEnv: {},
    });
    expect(spec.task_queue).toBe("base-queue");
    expect(spec.runtime.provider.type).toBe("openai");
  });

  it("threads sourceLabel into the loader's error messages", () => {
    const environment = loadEnvironmentSpec(`
name: prod
overrides:
  runtime:
    temporal: { namespace: "\${MISSING_VAR}" }
`);
    expect(() =>
      resolveEnvironmentWorkflow(WORKFLOW, {
        environment,
        workflowId: "W",
        baseEnv: {},
        sourceLabel: "envs/prod/wf.yaml",
      }),
    ).toThrow(/envs\/prod\/wf\.yaml/);
  });

  it("still enforces the override allow-list (an environment cannot rewrite the graph)", () => {
    const environment = loadEnvironmentSpec(`name: prod\nworkflows: { W: { overrides: {} } }`);
    // Craft an env whose merged overrides reach a forbidden key by bypassing the spec's
    // own allow-list check — the loader re-validates, so the graph stays protected.
    const evil = { ...environment, overrides: { activities: {} } as Record<string, unknown> };
    expect(() => resolveEnvironmentWorkflow(WORKFLOW, { environment: evil, workflowId: "W", baseEnv: {} })).toThrow(
      /activities is not an allowed environment override/,
    );
  });
});

describe("resolveEnvironmentWorkflowFromText (#454; text-only convenience)", () => {
  it("parses the environment text and resolves in one call", () => {
    const spec = resolveEnvironmentWorkflowFromText(WORKFLOW, ENVIRONMENT, { workflowId: "W", baseEnv: {} });
    expect(spec.task_queue).toBe("prod-queue");
    expect(spec.runtime.temporal.address).toBe("wf.temporal:7233");
  });
});

describe("validateEnvironmentWorkflowReferences (#454; _validate_environment_workflow_ids)", () => {
  const PROJECT = `
name: proj
workflows:
  - { id: wf-a, path: a.yaml }
  - { id: wf-b, path: b.yaml }
`;

  it("returns nothing when every per-workflow override targets a declared workflow", () => {
    const project = loadProjectSpec(PROJECT);
    const environment = loadEnvironmentSpec(`
name: e
workflows:
  wf-a: { overrides: {} }
  wf-b: { overrides: {} }
`);
    expect(validateEnvironmentWorkflowReferences(project, environment)).toEqual([]);
  });

  it("flags a per-workflow block that references an undeclared workflow (typed issue, like the policy checks)", () => {
    const project = loadProjectSpec(PROJECT);
    const environment = loadEnvironmentSpec(`
name: e
workflows:
  wf-a: { overrides: {} }
  ghost: { overrides: {} }
`);
    expect(validateEnvironmentWorkflowReferences(project, environment)).toEqual([
      {
        code: "unknown_environment_workflow",
        message: "environment 'e' references unknown workflow: ghost",
        reference: "e",
      },
    ]);
  });

  it("reports one issue per unknown id, in workflows-key order (completeness)", () => {
    const project = loadProjectSpec(PROJECT);
    const environment = loadEnvironmentSpec(`
name: e
workflows:
  ghost1: { overrides: {} }
  wf-a: { overrides: {} }
  ghost2: { overrides: {} }
`);
    const issues = validateEnvironmentWorkflowReferences(project, environment);
    expect(issues.map((issue) => issue.code)).toEqual(["unknown_environment_workflow", "unknown_environment_workflow"]);
    expect(issues.map((issue) => issue.message)).toEqual([
      "environment 'e' references unknown workflow: ghost1",
      "environment 'e' references unknown workflow: ghost2",
    ]);
  });
});
