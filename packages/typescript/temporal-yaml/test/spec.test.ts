import { describe, expect, it } from "vitest";
import type { z } from "zod";

import { typefluxYamlSpec } from "../src/index.js";
// Sub-schema values are deliberately not re-exported from the barrel (#499
// export curation) — reach into the module directly for white-box tests.
import { activityRetrySpec, moderationSpec, promptRefSpec } from "../src/spec.js";

// Direct Zod-schema tests for spec.ts (#452, strict per #490), on plain objects —
// loader.test.ts exercises the same schema through YAML text + env interpolation.
//
// Scope notes on what spec.ts does NOT enforce (asserted below as accepted-by-schema):
// - provider.type is an open z.string() — provider-name validity lives in
//   providerFromSpec, not the schema.
// - Step referential integrity ("exactly one of activity/map", "activity exists")
//   lives in workflowPlanFromSpec / assembleYamlRuntime, not the schema.
// - There is no top-level spec-version field: any versioning key (`spec_version`)
//   is rejected as an unrecognized key, and `workflow.version` is rejected with a
//   pointer error because Python wires it to versioned workflow types.

const minimalSpec = () => ({
  project: "p",
  name: "n",
  task_queue: "q",
  runtime: {
    temporal: {} as Record<string, unknown>,
    registry: { type: "inline", prompts: { "p/x": "do it" } } as Record<string, unknown>,
    provider: { type: "openai" } as Record<string, unknown>,
  },
  activities: {
    definitions: [
      { name: "a", input: "schemas:In", output: "schemas:Out", prompt: "p/x" } as Record<
        string,
        unknown
      >,
    ],
  },
  workflow: {
    name: "W",
    input: "schemas:In",
    steps: [{ id: "s", activity: "a" }] as Record<string, unknown>[],
  } as Record<string, unknown> & { steps: Record<string, unknown>[] },
});

/** Assert the parse failed and return the joined issue messages for matching. */
function rejectionMessages(result: z.ZodSafeParseResult<unknown>): string {
  expect(result.success).toBe(false);
  if (result.success) {
    throw new Error("expected the spec to be rejected");
  }
  return result.error.issues.map((issue) => issue.message).join("\n");
}

describe("typefluxYamlSpec: valid specs", () => {
  it("parses a minimal spec and applies field defaults", () => {
    const spec = typefluxYamlSpec.parse(minimalSpec());
    expect(spec.project).toBe("p");
    expect(spec.activities.definitions?.[0]?.validation_retries).toBe(1); // default
    expect(spec.workflow.steps).toHaveLength(1);
  });

  it("coerces env-interpolated string forms (tls boolean, concurrency int, version int)", () => {
    const raw = minimalSpec();
    raw.runtime.temporal = { tls: "false" };
    raw.workflow.steps = [{ id: "s", map: { activity: "a", over: "input.items", concurrency: "3" } }];
    raw.activities.definitions[0]!.prompt = { name: "p/x", version: "2" };
    const spec = typefluxYamlSpec.parse(raw);
    expect(spec.runtime.temporal.tls).toBe(false);
    expect(spec.workflow.steps[0]?.map?.concurrency).toBe(3);
    expect((spec.activities.definitions?.[0]?.prompt as { version: number }).version).toBe(2);
  });

  it("does NOT validate provider-type names or step references (enforced downstream)", () => {
    const raw = minimalSpec();
    raw.runtime.provider = { type: "not-a-real-provider" };
    // Both `activity` and `map` on one step, referencing an undefined activity —
    // schema-valid; workflowPlanFromSpec is where this fails.
    raw.workflow.steps = [
      { id: "s", activity: "no_such_activity", map: { activity: "also_missing", over: "input.x" } },
    ];
    expect(typefluxYamlSpec.safeParse(raw).success).toBe(true);
  });
});

describe("typefluxYamlSpec: rejection", () => {
  it("rejects a non-string provider type (type errors, not name validation)", () => {
    const raw = minimalSpec();
    raw.runtime.provider = { type: 42 };
    expect(typefluxYamlSpec.safeParse(raw).success).toBe(false);
  });

  it("rejects missing required runtime fields (registry, provider)", () => {
    const noProvider = minimalSpec();
    delete (noProvider.runtime as Record<string, unknown>).provider;
    expect(typefluxYamlSpec.safeParse(noProvider).success).toBe(false);

    const noRegistry = minimalSpec();
    delete (noRegistry.runtime as Record<string, unknown>).registry;
    expect(typefluxYamlSpec.safeParse(noRegistry).success).toBe(false);
  });

  it("rejects an unknown top-level key by name (strict, #490)", () => {
    const messages = rejectionMessages(typefluxYamlSpec.safeParse({ ...minimalSpec(), spec_version: 2 }));
    expect(messages).toMatch(/Unrecognized key.*spec_version/);
  });

  it("rejects unknown nested keys (temporal connection, step, retry typo)", () => {
    const badTemporal = minimalSpec();
    badTemporal.runtime.temporal = { adress: "localhost:7233" };
    expect(typefluxYamlSpec.safeParse(badTemporal).success).toBe(false);

    const badStep = minimalSpec();
    badStep.workflow.steps = [{ id: "s", activity: "a", when: "always" }];
    expect(typefluxYamlSpec.safeParse(badStep).success).toBe(false);

    const typoRetry = minimalSpec();
    typoRetry.activities.definitions[0]!.retry = { maximum_atempts: 3 };
    expect(rejectionMessages(typefluxYamlSpec.safeParse(typoRetry))).toMatch(/maximum_atempts/);
  });

  it("accepts a valid workflow.version and rejects Python's invalid label shapes (#530)", () => {
    const raw = minimalSpec();
    raw.workflow.version = "1.2";
    expect(typefluxYamlSpec.parse(raw).workflow.version).toBe("1.2");
    raw.workflow.version = "-leading";
    expect(rejectionMessages(typefluxYamlSpec.safeParse(raw))).toMatch(/workflow\.version/);
  });

  it("rejects known-pending config blocks with issue pointers, not generic key errors", () => {
    const imports = minimalSpec();
    (imports.runtime as Record<string, unknown>).imports = { allow: ["x"] };
    expect(rejectionMessages(typefluxYamlSpec.safeParse(imports))).toMatch(/imports.*#49[56]/);
  });

  it("accepts an activity cache block (session cache wired, #478)", () => {
    const cached = minimalSpec();
    cached.activities.definitions[0]!.cache = { enabled: true };
    expect(typefluxYamlSpec.safeParse(cached).success).toBe(true);
  });

  it("accepts a cross_run_cache block and defaults enabled true (#398/#753)", () => {
    const raw = minimalSpec();
    raw.activities.definitions[0]!.cross_run_cache = {};
    const spec = typefluxYamlSpec.parse(raw);
    // `enabled` materializes to the Python-parity default so a bare block still engages.
    expect(spec.activities.definitions?.[0]?.cross_run_cache).toEqual({ enabled: true });
  });

  it("carries cross_run_cache.bypass_reads_env through the spec (#753)", () => {
    const raw = minimalSpec();
    raw.activities.definitions[0]!.cross_run_cache = { enabled: false, bypass_reads_env: "DISABLE_X" };
    const spec = typefluxYamlSpec.parse(raw);
    expect(spec.activities.definitions?.[0]?.cross_run_cache).toEqual({
      enabled: false,
      bypass_reads_env: "DISABLE_X",
    });
  });

  it("lets session cache and cross_run_cache coexist independently (#478 + #753)", () => {
    const raw = minimalSpec();
    // The two blocks live on different axes (provider prefix cache vs cross-run memoization);
    // declaring both is valid — they are wired to distinct descriptor fields.
    raw.activities.definitions[0]!.cache = { enabled: true, ttl_seconds: 600 };
    raw.activities.definitions[0]!.cross_run_cache = { enabled: true };
    expect(typefluxYamlSpec.safeParse(raw).success).toBe(true);
  });

  it("rejects an empty bypass_reads_env and unknown keys in cross_run_cache (#753)", () => {
    const empty = minimalSpec();
    empty.activities.definitions[0]!.cross_run_cache = { bypass_reads_env: "" };
    expect(typefluxYamlSpec.safeParse(empty).success).toBe(false);

    const unknown = minimalSpec();
    unknown.activities.definitions[0]!.cross_run_cache = { ttl_seconds: 3 };
    expect(typefluxYamlSpec.safeParse(unknown).success).toBe(false);
  });

  it("rejects an empty steps list and an empty inline-prompt messages list", () => {
    const emptySteps = minimalSpec();
    emptySteps.workflow.steps = [];
    expect(rejectionMessages(typefluxYamlSpec.safeParse(emptySteps))).toMatch(/steps must not be empty/);

    const emptyPrompt = minimalSpec();
    emptyPrompt.runtime.registry = { type: "inline", prompts: { "p/x": { messages: [] } } };
    expect(rejectionMessages(typefluxYamlSpec.safeParse(emptyPrompt))).toMatch(
      /messages must not be empty/,
    );
  });
});

describe("sub-schema refinements", () => {
  it("promptRefSpec: version and label are mutually exclusive", () => {
    expect(promptRefSpec.safeParse({ name: "p", version: 1 }).success).toBe(true);
    expect(promptRefSpec.safeParse({ name: "p", label: "prod" }).success).toBe(true);
    expect(rejectionMessages(promptRefSpec.safeParse({ name: "p", version: 1, label: "prod" }))).toMatch(
      /mutually exclusive/,
    );
  });

  it("activityRetrySpec: bounded defaults; 0 attempts is the explicit unlimited sentinel", () => {
    expect(activityRetrySpec.parse({})).toEqual({
      maximum_attempts: 5,
      initial_interval_seconds: 1,
      maximum_interval_seconds: 60,
      backoff_coefficient: 2,
    });
    expect(activityRetrySpec.parse({ maximum_attempts: 0 }).maximum_attempts).toBe(0);
    expect(activityRetrySpec.parse({ maximum_interval_seconds: null }).maximum_interval_seconds).toBeNull();
    expect(activityRetrySpec.safeParse({ maximum_attempts: -1 }).success).toBe(false);
    expect(activityRetrySpec.safeParse({ backoff_coefficient: 0.5 }).success).toBe(false);
  });

  it("moderationSpec: exactly one of provider/moderator; on_violation defaults to block", () => {
    expect(moderationSpec.parse({ provider: "openai" }).on_violation).toBe("block");
    // #382: shared YAML declaring the Python-side gemini provider loads in the TS
    // edition too (provider is intent here; the injected moderator supplies behavior).
    expect(moderationSpec.parse({ provider: "gemini" }).on_violation).toBe("block");
    expect(moderationSpec.safeParse({ provider: "anthropic" }).success).toBe(false);
    expect(moderationSpec.parse({ moderator: "my_mod", on_violation: "flag" }).on_violation).toBe("flag");
    expect(moderationSpec.safeParse({}).success).toBe(false);
    expect(moderationSpec.safeParse({ provider: "openai", moderator: "m" }).success).toBe(false);
    // `model` belongs to the injected moderator, never the spec.
    expect(rejectionMessages(moderationSpec.safeParse({ provider: "openai", model: "omni" }))).toMatch(
      /injected moderator/,
    );
  });

  it("runtime.cache_erasure accepts the two declared values only (#795)", () => {
    const good = minimalSpec();
    (good.runtime as Record<string, unknown>)["cache_erasure"] = "targeted";
    expect(typefluxYamlSpec.safeParse(good).success).toBe(true);
    const bad = minimalSpec();
    (bad.runtime as Record<string, unknown>)["cache_erasure"] = "sometimes";
    expect(typefluxYamlSpec.safeParse(bad).success).toBe(false);
  });

  it("observability credential blocks require their own backend type (#793)", () => {
    const bad = minimalSpec();
    (bad.runtime as Record<string, unknown>)["observability"] = {
      type: "langsmith",
      langfuse: { public_key: "pk-dead" },
    };
    const parsed = typefluxYamlSpec.safeParse(bad);
    expect(rejectionMessages(parsed)).toMatch(/only valid with type: langfuse/);

    const good = minimalSpec();
    (good.runtime as Record<string, unknown>)["observability"] = {
      type: "langfuse",
      langfuse: { public_key: "pk-live", secret_key: { value_from: { env: "LF_SECRET" } } },
    };
    expect(typefluxYamlSpec.safeParse(good).success).toBe(true);
  });

  it("custom-extension config is stub-rejected with a pointer (#792)", () => {
    // The TS SDK rejects Python `class:` extensions; `config` accompanies them, so it
    // gets the same pointer-error treatment instead of a bare unknown-key rejection.
    const cases: Array<[string, RegExp]> = [
      ["provider", /injected provider or transport/],
      ["registry", /injected `registryTransport`/],
      ["observability", /injected observer transport/],
    ];
    for (const [section, pointer] of cases) {
      const bad = minimalSpec();
      const runtime = bad.runtime as Record<string, Record<string, unknown>>;
      runtime[section] = { ...(runtime[section] ?? { type: "none" }), config: { api_key: "x" } };
      const parsed = typefluxYamlSpec.safeParse(bad);
      expect(parsed.success).toBe(false);
      expect(rejectionMessages(parsed)).toMatch(pointer);
    }
  });

  it("providerSpec (via full spec): structured_mode is the json_schema literal only", () => {
    const good = minimalSpec();
    good.runtime.provider = { type: "openai", structured_mode: "json_schema" };
    expect(typefluxYamlSpec.safeParse(good).success).toBe(true);

    const bad = minimalSpec();
    bad.runtime.provider = { type: "openai", structured_mode: "json_mode" };
    expect(typefluxYamlSpec.safeParse(bad).success).toBe(false);
  });
});
