import { describe, expect, it } from "vitest";

import { loadYamlSpec } from "../src/index.js";

// A representative spec (the shape of the ported review examples).
const SPEC = `
project: examples.insurance_claim_review
name: insurance_claim_review
task_queue: \${TEMPORAL_TASK_QUEUE:-insurance-claim-review}

runtime:
  temporal:
    address: \${TEMPORAL_ADDRESS:-localhost:7233}
    namespace: default
    tls: \${TEMPORAL_TLS:-false}
    api_key:
      value_from:
        env: TEMPORAL_API_KEY
        required: false
  registry:
    type: langfuse
    label: \${LANGFUSE_PROMPT_LABEL:-production}
  provider:
    type: openai
    model: \${TYPEFLUX_OPENAI_MODEL:-gpt-4o-mini}
    structured_mode: json_schema
  observability:
    type: langfuse
    execution_manifest: true

activities: {}

workflow:
  name: InsuranceClaimReviewWorkflow
  input: schemas:ClaimInput
  output: schemas:ClaimReviewPacket
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
        concurrency: 3
        collect:
          output: schemas:EvidenceReviewBatch
          field: reviews
    - id: consolidate
      activity: consolidate_claim_review
`;

describe("loadYamlSpec (#452)", () => {
  it("parses, env-interpolates, and validates a representative spec", () => {
    const spec = loadYamlSpec(SPEC, { env: { TYPEFLUX_OPENAI_MODEL: "gpt-4o" } });
    expect(spec.project).toBe("examples.insurance_claim_review");
    expect(spec.task_queue).toBe("insurance-claim-review"); // default applied
    expect(spec.runtime.provider.model).toBe("gpt-4o"); // env value applied
    expect(spec.runtime.registry.label).toBe("production"); // default applied
    // activities.modules is a rejected permanent divergence (#496) — nothing to assert.
    expect(spec.workflow.name).toBe("InsuranceClaimReviewWorkflow");
    expect(spec.workflow.steps).toHaveLength(2);
    expect(spec.workflow.steps[0]?.map?.over).toBe("input.evidence");
    expect(spec.workflow.steps[0]?.map?.concurrency).toBe(3);
    expect(spec.workflow.steps[1]?.activity).toBe("consolidate_claim_review");
    // tls came from `${TEMPORAL_TLS:-false}` -> the string "false" -> coerced to a boolean.
    expect(spec.runtime.temporal.tls).toBe(false);
  });

  it("coerces a string-boolean from an env default (tls: ${VAR:-true})", () => {
    const spec = loadYamlSpec(SPEC, { env: { TEMPORAL_TLS: "true" } });
    expect(spec.runtime.temporal.tls).toBe(true);
  });

  it("expands YAML merge keys (<<) so inherited fields are present", () => {
    const merged = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider:
    <<: &base
      type: openai
      model: gpt-4o
    structured_mode: json_schema
activities: {}
workflow:
  name: W
  input: schemas:In
  steps:
    - id: only
      activity: do
`;
    const spec = loadYamlSpec(merged);
    expect(spec.runtime.provider.type).toBe("openai"); // inherited via <<
    expect(spec.runtime.provider.model).toBe("gpt-4o"); // inherited via <<
    expect(spec.runtime.provider.structured_mode).toBe("json_schema"); // explicit
  });

  it("rejects duplicate mapping keys (no silent last-wins)", () => {
    const dup = "project: a\nname: b\ntask_queue: q\nproject: a2\nruntime: {}\nactivities: {}\nworkflow: {}";
    expect(() => loadYamlSpec(dup)).toThrow(/invalid YAML spec/);
  });

  it("throws on an empty document", () => {
    expect(() => loadYamlSpec("   \n")).toThrow(/empty YAML spec/);
  });

  it("throws when the root is not a mapping", () => {
    expect(() => loadYamlSpec("- a\n- b")).toThrow(/must be a mapping/);
  });

  it("rejects a spec missing a required field (Zod validation)", () => {
    const noWorkflow = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities: {}
`;
    expect(() => loadYamlSpec(noWorkflow)).toThrow(); // missing `workflow`
  });

  it("enforces the byte limit", () => {
    const huge = "project: " + "x".repeat(1024 * 1024 + 10);
    expect(() => loadYamlSpec(huge)).toThrow(/exceeds the .* byte limit/);
  });
});

describe("strict spec (#490)", () => {
  const minimal = (patch: (s: string) => string = (s) => s) =>
    patch(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
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
`);

  it("accepts the minimal fully-modeled spec", () => {
    expect(loadYamlSpec(minimal()).workflow.steps).toHaveLength(1);
  });

  it("rejects a typo'd retry field instead of silently applying the default", () => {
    const typo = minimal((s) => s.replace("      prompt: p/x", "      prompt: p/x\n      retry: { maximum_atempts: 3 }"));
    expect(() => loadYamlSpec(typo)).toThrow(/maximum_atempts/);
  });

  it("rejects an unknown top-level key, naming it", () => {
    expect(() => loadYamlSpec(minimal((s) => `${s}unknown_block: {}\n`))).toThrow(/Unrecognized key.*unknown_block/);
  });

  it("accepts an activity cache block (wired, #478) with strict shape + ttl >= 1", () => {
    const cached = minimal((s) =>
      s.replace("      prompt: p/x", "      prompt: p/x\n      cache: { enabled: true, ttl_seconds: 600 }"),
    );
    expect(loadYamlSpec(cached).activities.definitions?.[0]?.cache).toEqual({ enabled: true, ttl_seconds: 600 });
    // enabled defaults true when the block is present (Python SessionCacheSpec parity).
    const defaulted = minimal((s) => s.replace("      prompt: p/x", "      prompt: p/x\n      cache: {}"));
    expect(loadYamlSpec(defaulted).activities.definitions?.[0]?.cache?.enabled).toBe(true);
    const badTtl = minimal((s) => s.replace("      prompt: p/x", "      prompt: p/x\n      cache: { ttl_seconds: 0 }"));
    expect(() => loadYamlSpec(badTtl)).toThrow(/ttl_seconds|small|>=1/i);
    const unknown = minimal((s) => s.replace("      prompt: p/x", "      prompt: p/x\n      cache: { mode: fancy }"));
    expect(() => loadYamlSpec(unknown)).toThrow(/mode|unrecognized/i);
  });

  it("accepts workflow.lifecycle incl. the review gate (wired, #482)", () => {
    const lc = minimal((s) => s.replace("  steps:", "  lifecycle: { enabled: true }\n  steps:"));
    expect(loadYamlSpec(lc).workflow.lifecycle?.enabled).toBe(true);
    // The review gate needs user_decisions — an empty map is rejected at load (Python parity).
    const empty = minimal((s) =>
      s.replace("  steps:", "  lifecycle: { enabled: true, review: { after_step: s, user_decisions: {} } }\n  steps:"),
    );
    expect(() => loadYamlSpec(empty)).toThrow(/user_decisions must not be empty/);
  });

  it("accepts runtime.provider_limits (wired, #529) and rejects bounds violations", () => {
    const at = (block: string) =>
      minimal((s) => s.replace("  provider: { type: openai }", `  provider: { type: openai }\n  provider_limits: ${block}`));
    const spec = loadYamlSpec(
      at(
        "{ default: { max_concurrent: 4 }, providers: { openai: { min_interval_seconds: 0.5, models: { gpt-4o: { max_concurrent: 1 } } } } }",
      ),
    );
    expect(spec.runtime.provider_limits?.default?.max_concurrent).toBe(4);
    expect(spec.runtime.provider_limits?.providers["openai"]?.min_interval_seconds).toBe(0.5);
    expect(spec.runtime.provider_limits?.providers["openai"]?.models["gpt-4o"]?.max_concurrent).toBe(1);
    expect(() => loadYamlSpec(at("{ default: { max_concurrent: 0 } }"))).toThrow(/max_concurrent/);
    expect(() => loadYamlSpec(at("{ default: { min_interval_seconds: -1 } }"))).toThrow(/min_interval_seconds/);
    expect(() => loadYamlSpec(at("{ default: { burst: 2 } }"))).toThrow(/unrecognized|unknown/i);
  });

  it("rejects a registry `class` with the injected-transport guidance", () => {
    const cls = minimal((s) => s.replace("{ type: inline, prompts: { p/x: do it } }", "{ type: custom, class: my.registry.Cls }"));
    expect(() => loadYamlSpec(cls)).toThrow(/registryTransport/);
  });

  it("tightens structured_mode to the json_schema literal", () => {
    const bad = minimal((s) => s.replace("provider: { type: openai }", "provider: { type: openai, structured_mode: json_mode }"));
    expect(() => loadYamlSpec(bad)).toThrow(/json_schema/);
  });

  it("validates inline content parts as the core part union", () => {
    const parts = minimal((s) =>
      s.replace(
        "  registry: { type: inline, prompts: { p/x: do it } }",
        `  registry:
    type: inline
    prompts:
      p/x:
        messages:
          - role: user
            content:
              - { type: text, text: "hi" }
              - { type: provider_extension, provider: gemini, payload: { inlineData: { mimeType: image/png, data: AAAA } } }`,
      ),
    );
    expect(loadYamlSpec(parts).runtime.registry.prompts).toBeDefined();
    // An unknown part type fails loudly.
    const badPart = parts.replace("{ type: text, text: \"hi\" }", "{ type: video, url: x }");
    expect(() => loadYamlSpec(badPart)).toThrow(/invalid Typeflux spec/);
  });

  it("accepts workflow.version with Python's label charset (#530 — the last wired stub)", () => {
    const at = (value: string) => minimal((s) => s.replace("  name: W", `  name: W\n  version: ${value}`));
    expect(loadYamlSpec(at("1.2-rc.1")).workflow.version).toBe("1.2-rc.1");
    expect(loadYamlSpec(at("v2_beta")).workflow.version).toBe("v2_beta");
    expect(loadYamlSpec(minimal((s) => s)).workflow.version).toBeUndefined();
    // Python: must start alphanumeric; only alphanumerics/._- after.
    expect(() => loadYamlSpec(at("'-leading'"))).toThrow(/workflow\.version/);
    expect(() => loadYamlSpec(at("'has space'"))).toThrow(/workflow\.version/);
    expect(() => loadYamlSpec(at("''"))).toThrow(/workflow\.version/);
  });

  it("collect.max_bytes is wired with Python defaults (#495 PR-B); inline provider_params is WIRED (A2)", () => {
    const mb = minimal((s) =>
      s.replace(
        "    - id: s\n      activity: a",
        "    - id: s\n      map: { activity: a, over: input.items, collect: { output: schemas:Out, field: r, max_bytes: 500000 } }",
      ),
    );
    const step = loadYamlSpec(mb).workflow.steps[0];
    expect(step?.map?.collect?.max_bytes).toBe(500000);
    // Python parity: ABSENT defaults the guard ON at 1.5MB; 0 disables; null REJECTS.
    const absent = minimal((s) =>
      s.replace(
        "    - id: s\n      activity: a",
        "    - id: s\n      map: { activity: a, over: input.items, collect: { output: schemas:Out, field: r } }",
      ),
    );
    expect(loadYamlSpec(absent).workflow.steps[0]?.map?.collect?.max_bytes).toBe(1_500_000);
    const disabled = minimal((s) =>
      s.replace(
        "    - id: s\n      activity: a",
        "    - id: s\n      map: { activity: a, over: input.items, collect: { output: schemas:Out, field: r, max_bytes: 0 } }",
      ),
    );
    expect(loadYamlSpec(disabled).workflow.steps[0]?.map?.collect?.max_bytes).toBe(0);
    const nullBytes = minimal((s) =>
      s.replace(
        "    - id: s\n      activity: a",
        "    - id: s\n      map: { activity: a, over: input.items, collect: { output: schemas:Out, field: r, max_bytes: null } }",
      ),
    );
    expect(() => loadYamlSpec(nullBytes)).toThrow();
    const negative = minimal((s) =>
      s.replace(
        "    - id: s\n      activity: a",
        "    - id: s\n      map: { activity: a, over: input.items, collect: { output: schemas:Out, field: r, max_bytes: -1 } }",
      ),
    );
    expect(() => loadYamlSpec(negative)).toThrow(/max_bytes/);
    const pp = minimal((s) =>
      s.replace(
        "  registry: { type: inline, prompts: { p/x: do it } }",
        "  registry:\n    type: inline\n    prompts:\n      p/x:\n        messages: [{ role: user, content: go }]\n        provider_params: { seed: 7 }",
      ),
    );
    const prompt = loadYamlSpec(pp).runtime.registry.prompts?.["p/x"];
    expect(typeof prompt === "object" && prompt !== null && "provider_params" in prompt ? prompt.provider_params : undefined).toEqual({ seed: 7 });
  });

  it("requires exactly one secret source in value_from", () => {
    const none = minimal((s) => s.replace("  temporal: {}", "  temporal: { api_key: { value_from: { required: true } } }"));
    expect(() => loadYamlSpec(none)).toThrow(/exactly one of `env` or `file`/);
  });

  it("rejects an empty steps list", () => {
    const empty = minimal((s) => s.replace(/  steps:[\s\S]*$/, "  steps: []\n"));
    expect(() => loadYamlSpec(empty)).toThrow(/steps must not be empty/);
  });
});

describe("provider params spec (#495 A2)", () => {
  const minimal = (patch: (s: string) => string = (s) => s) =>
    patch(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
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
`);

  it("accepts wired params blocks on provider, inline prompt, and activity", () => {
    const spec = loadYamlSpec(
      minimal((s) =>
        s
          .replace(
            "  provider: { type: openai }",
            "  provider: { type: openai, params: { temperature: 0.2, stop: END, timeout: 30 } }",
          )
          .replace("p/x: do it", "p/x: { messages: [{ role: user, content: do it }], provider_params: { top_p: 0.9 } }")
          .replace("      prompt: p/x", "      prompt: p/x\n      provider_params: { max_tokens: 900 }"),
      ),
    );
    expect(spec.runtime.provider.params).toEqual({ temperature: 0.2, stop: "END", timeout: 30 });
    expect(spec.activities.definitions?.[0]?.provider_params).toEqual({ max_tokens: 900 });
    expect(spec.runtime.provider.allow_prompt_model_override).toBe(false); // Python default
  });

  it("rejects out-of-bounds values with Python's messages", () => {
    const bad = (block: string) =>
      minimal((s) => s.replace("  provider: { type: openai }", `  provider: { type: openai, params: ${block} }`));
    expect(() => loadYamlSpec(bad("{ temperature: 3 }"))).toThrow(/temperature/);
    expect(() => loadYamlSpec(bad("{ top_p: 1.5 }"))).toThrow(/top_p/);
    expect(() => loadYamlSpec(bad("{ max_tokens: 0 }"))).toThrow(/>= 1/);
    expect(() => loadYamlSpec(bad("{ thinking_budget: -1 }"))).toThrow(/thinking_budget/);
    expect(() => loadYamlSpec(bad("{ timeout: 0 }"))).toThrow(/timeout/);
    expect(() => loadYamlSpec(bad("{ frequency_penalty: -3 }"))).toThrow(/penalt/);
    expect(() => loadYamlSpec(bad('{ stop: ["", ok] }')))
      .toThrow(/non-empty and trimmed/);
    expect(() => loadYamlSpec(bad("{ tempratur: 1 }"))).toThrow(/tempratur|unrecognized/i);
  });

  it("ports the legacy model/temperature match errors", () => {
    const providerMismatch = minimal((s) =>
      s.replace(
        "  provider: { type: openai }",
        "  provider: { type: openai, model: gpt-a, params: { model: gpt-b } }",
      ),
    );
    expect(() => loadYamlSpec(providerMismatch)).toThrow(/must match/);
    const promptMismatch = minimal((s) =>
      s.replace(
        "p/x: do it",
        "p/x: { messages: [{ role: user, content: do it }], temperature: 0.2, provider_params: { temperature: 0.9 } }",
      ),
    );
    expect(() => loadYamlSpec(promptMismatch)).toThrow(/must match/);
  });

  it("providerSpec rejects the phantom provider_params key (Python has only `params`)", () => {
    const phantom = minimal((s) =>
      s.replace("  provider: { type: openai }", "  provider: { type: openai, provider_params: { temperature: 1 } }"),
    );
    expect(() => loadYamlSpec(phantom)).toThrow(/provider_params|unrecognized/i);
  });

  it("base_url/vertex are rejected as PERMANENT divergences naming the adapter", () => {
    const baseUrl = minimal((s) =>
      s.replace("  provider: { type: openai }", "  provider: { type: openai, base_url: 'http://x' }"),
    );
    expect(() => loadYamlSpec(baseUrl)).toThrow(/adapter/);
  });
});

describe("provider params — codex A2 review edges (#495)", () => {
  const minimal = (patch: (s: string) => string = (s) => s) =>
    patch(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
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
`);

  it("null and empty-string param values are ABSENT, never coerced to 0", () => {
    // YAML `temperature: null` / an interpolated `${TEMP:-}` empty default must not
    // silently select temperature 0 (the max_bytes null->0 trap).
    const withNull = loadYamlSpec(
      minimal((s) =>
        s.replace(
          "  provider: { type: openai }",
          "  provider: { type: openai, params: { temperature: null, seed: '', max_tokens: 100 } }",
        ),
      ),
    );
    expect(withNull.runtime.provider.params).toEqual({ max_tokens: 100 });
  });

  it("rejects params the SELECTED provider does not support (Python validate_provider_params_supported)", () => {
    const geminiSeed = minimal((s) =>
      s.replace("  provider: { type: openai }", "  provider: { type: gemini, params: { seed: 7 } }"),
    );
    expect(() => loadYamlSpec(geminiSeed)).toThrow(/"gemini" does not support provider params: seed/);
    const openaiThinking = minimal((s) =>
      s
        .replace("  provider: { type: openai }", "  provider: { type: openai }")
        .replace("      prompt: p/x", "      prompt: p/x\n      provider_params: { thinking_budget: 100 }"),
    );
    expect(() => loadYamlSpec(openaiThinking)).toThrow(/"openai" does not support provider params: thinking_budget/);
    // An injected custom provider skips the check (its capabilities are unknown).
    const custom = minimal((s) =>
      s.replace("  provider: { type: openai }", "  provider: { type: custom, params: { thinking_budget: 100 } }"),
    );
    expect(loadYamlSpec(custom).runtime.provider.params).toEqual({ thinking_budget: 100 });
  });
});

describe("provider params — finder A2 review edges (#495)", () => {
  const minimal = (patch: (s: string) => string = (s) => s) =>
    patch(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
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
`);

  it("thinking_budget: null is ABSENT (never 0 = silently disabled thinking)", () => {
    const spec = loadYamlSpec(
      minimal((s) =>
        s.replace("  provider: { type: openai }", "  provider: { type: gemini, params: { thinking_budget: null } }"),
      ),
    );
    expect(spec.runtime.provider.params).toEqual({});
  });

  it("rejects an inline-prompt MODEL mismatch and the vertex block", () => {
    const modelMismatch = minimal((s) =>
      s.replace(
        "p/x: do it",
        "p/x: { messages: [{ role: user, content: do it }], model: m-a, provider_params: { model: m-b } }",
      ),
    );
    expect(() => loadYamlSpec(modelMismatch)).toThrow(/must match/);
    const vertex = minimal((s) =>
      s.replace("  provider: { type: openai }", "  provider: { type: gemini, vertex: { project: p } }"),
    );
    expect(() => loadYamlSpec(vertex)).toThrow(/adapter/);
  });

  it("params model/stop errors carry provider-params messages, not artifact wording", () => {
    const untrimmed = minimal((s) =>
      s.replace("  provider: { type: openai }", '  provider: { type: openai, params: { model: " gpt " } }'),
    );
    expect(() => loadYamlSpec(untrimmed)).toThrow(/provider params model/);
    const badStop = minimal((s) =>
      s.replace("  provider: { type: openai }", '  provider: { type: openai, params: { stop: [" x"] } }'),
    );
    expect(() => loadYamlSpec(badStop)).toThrow(/provider params stop entries/);
  });
});

describe("provider_retry spec (#495 PR-B)", () => {
  const minimal = (patch: (s: string) => string = (s) => s) =>
    patch(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
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
`);

  it("accepts the wired shape with Python defaults", () => {
    const spec = loadYamlSpec(
      minimal((s) =>
        s.replace(
          "  provider: { type: openai }",
          "  provider: { type: openai }\n  provider_retry: { max_attempts: 3, initial_backoff_seconds: 0.5, max_backoff_seconds: 10, backoff_multiplier: 2, jitter_ratio: 0.1 }",
        ),
      ),
    );
    expect(spec.runtime.provider_retry?.max_attempts).toBe(3);
    expect(spec.runtime.provider_retry?.retry_rate_limits).toBe(true);
  });

  it("rejects bounds violations; the class-selection booleans are wired (#529)", () => {
    const bad = (block: string) =>
      minimal((s) => s.replace("  provider: { type: openai }", `  provider: { type: openai }\n  provider_retry: ${block}`));
    expect(() => loadYamlSpec(bad("{ max_attempts: 0 }"))).toThrow(/max_attempts/);
    expect(() => loadYamlSpec(bad("{ jitter_ratio: 1.5 }"))).toThrow(/jitter_ratio/);
    expect(() => loadYamlSpec(bad("{ backoff_multiplier: 0.5 }"))).toThrow(/backoff_multiplier/);
    // false is honored now that ProviderRateLimitError exists (#529) — it reaches
    // the executor as retryRateLimits/retryTransientErrors (runtime test).
    expect(loadYamlSpec(bad("{ retry_rate_limits: false }")).runtime.provider_retry?.retry_rate_limits).toBe(false);
    expect(
      loadYamlSpec(bad("{ retry_transient_errors: false }")).runtime.provider_retry?.retry_transient_errors,
    ).toBe(false);
    expect(loadYamlSpec(bad("{ retry_rate_limits: true }")).runtime.provider_retry?.retry_rate_limits).toBe(true);
  });

  it("max_backoff_seconds '' is ABSENT, null is no-cap, 0 is an explicit cap (codex)", () => {
    const at = (value: string) =>
      minimal((s) =>
        s.replace(
          "  provider: { type: openai }",
          `  provider: { type: openai }\n  provider_retry: { max_backoff_seconds: ${value} }`,
        ),
      );
    expect(loadYamlSpec(at("''")).runtime.provider_retry?.max_backoff_seconds).toBeUndefined();
    expect(loadYamlSpec(at("null")).runtime.provider_retry?.max_backoff_seconds).toBeNull();
    expect(loadYamlSpec(at("0")).runtime.provider_retry?.max_backoff_seconds).toBe(0);
  });

  it("validates workflow_search_attribute names", () => {
    const badName = minimal((s) => s.replace("  temporal: {}", "  temporal: { workflow_search_attribute: 9bad }"));
    expect(() => loadYamlSpec(badName)).toThrow(/must start with a letter/);
    const good = minimal((s) => s.replace("  temporal: {}", "  temporal: { workflow_search_attribute: TypefluxWorkflow }"));
    expect(loadYamlSpec(good).runtime.temporal.workflow_search_attribute).toBe("TypefluxWorkflow");
  });
});

describe("module-loading permanent divergences (#496)", () => {
  it("rejects activities.modules and runtime.imports with injection-first pointers", () => {
    const base = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
activities: {}
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      activity: a
`;
    expect(() => loadYamlSpec(base.replace("activities: {}", "activities: { modules: [my.module] }"))).toThrow(
      /extraActivities/,
    );
    expect(() =>
      loadYamlSpec(base.replace("  temporal: {}", "  temporal: {}\n  imports: { allow: [x] }")),
    ).toThrow(/no module loading to police/);
  });
});
