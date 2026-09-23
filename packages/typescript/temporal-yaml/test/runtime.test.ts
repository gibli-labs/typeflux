import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { CollectingObserver, ProviderRateLimitError, ProviderTransientError } from "@typeflux/temporal";

import {
  assembleYamlRuntime,
  buildRuntime,
  composeProjectPolicies,
  loadPolicySpec,
  loadYamlSpec,
  ProjectPolicyEnforcementError,
} from "../src/index.js";

class StaticProvider implements ModelProvider {
  constructor(private readonly response: unknown) {}
  structuredCall(_params: StructuredCallParams): unknown {
    return this.response;
  }
}

const SPEC = `
project: p
name: claim_review
task_queue: spec-queue
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/review: review the item
      p/consolidate: consolidate the reviews
  provider: { type: openai }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
    - name: consolidate_claim_review
      input: schemas:Batch
      output: schemas:Packet
      prompt: p/consolidate
workflow:
  name: ClaimReviewWorkflow
  input: schemas:Claim
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
        collect: { output: schemas:Batch, field: reviews }
    - id: consolidate
      activity: consolidate_claim_review
`;

const schemas = {
  "schemas:EvidenceItem": z.object({ id: z.string() }),
  "schemas:EvidenceReview": z.object({ ok: z.boolean() }),
  "schemas:Batch": z.object({ reviews: z.array(z.object({ ok: z.boolean() })) }),
  "schemas:Packet": z.object({ count: z.number() }),
};

describe("assembleYamlRuntime cache-erasure requirement (#795)", () => {
  const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
  const plainStore = {
    get: () => undefined,
    set: () => undefined,
  };
  const erasableStore = {
    get: () => undefined,
    set: () => undefined,
    eraseSubject: () => ({ subjectId: "s", dryRun: true, keysFound: 0, keys: [], deleted: 0 }),
  };
  const specWith = (declaration: string) =>
    loadYamlSpec(SPEC.replace("  temporal: {}", `  temporal: {}\n${declaration}`));

  it("fails closed when targeted is declared and the wired store lacks eraseSubject", () => {
    expect(() =>
      assembleYamlRuntime(specWith("  cache_erasure: targeted"), { provider, schemas, cacheStore: plainStore }),
    ).toThrow(/cache_erasure is 'targeted'.*eraseSubject/s);
  });

  it("assembles with an erasable store, with no store (vacuous), and under any", () => {
    expect(() =>
      assembleYamlRuntime(specWith("  cache_erasure: targeted"), { provider, schemas, cacheStore: erasableStore }),
    ).not.toThrow();
    expect(() => assembleYamlRuntime(specWith("  cache_erasure: targeted"), { provider, schemas })).not.toThrow();
    expect(() =>
      assembleYamlRuntime(specWith("  cache_erasure: any"), { provider, schemas, cacheStore: plainStore }),
    ).not.toThrow();
  });
});

describe("assembleYamlRuntime artifacts (#481 PR4)", () => {
  it("wires runtime.artifacts -> policy -> resolver: a spec activity resolves and attaches a real file", async () => {
    const { mkdtemp, writeFile } = await import("node:fs/promises");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");
    const root = await mkdtemp(join(tmpdir(), "tf-yaml-artifacts-"));
    await writeFile(join(root, "logo.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));

    const spec = loadYamlSpec(
      SPEC.replace("  provider: { type: openai }", `  provider: { type: openai }
  artifacts:
    local_roots: ["${root}"]`).replace(
        "      prompt: p/review",
        `      prompt: p/review
      artifacts:
        - name: evidence_files
          from: input.files
          attach: { role: user, text: "Attached evidence:" }`,
      ),
    );
    let seen: StructuredCallParams | undefined;
    const provider: ModelProvider = {
      structuredCall: (params) => {
        seen = params;
        return { ok: true };
      },
    };
    // The artifact ref field must be part of the declared input schema (zod strips unknown
    // keys at the activity boundary, exactly like Python's input model).
    const artifactSchemas = { ...schemas, "schemas:EvidenceItem": z.object({ id: z.string(), files: z.array(z.string()) }) };
    const { activities } = assembleYamlRuntime(spec, { provider, schemas: artifactSchemas });
    await activities["review_evidence_item"]!({ id: "e1", files: ["logo.png"] });
    // The resolver ran (policy roots honored), the attach message was appended, and the
    // resolved group reached the provider params.
    expect(seen?.artifacts?.[0]?.name).toBe("evidence_files");
    expect(seen?.artifacts?.[0]?.artifacts[0]).toMatchObject({ kind: "image", media_type: "image/png" });
    expect(seen?.messages.at(-1)).toEqual({
      role: "user",
      content: [
        { type: "text", text: "Attached evidence:" },
        { type: "artifact_group", group: "evidence_files" },
      ],
    });
  });

  it("fails loud when a spec declares artifacts but no local_roots admit the file", async () => {
    const spec = loadYamlSpec(
      SPEC.replace(
        "      prompt: p/review",
        "      prompt: p/review\n      artifacts:\n        - name: evidence_files\n          from: input.files",
      ),
    );
    const artifactSchemas = { ...schemas, "schemas:EvidenceItem": z.object({ id: z.string(), files: z.array(z.string()) }) };
    const { activities } = assembleYamlRuntime(spec, { provider: new StaticProvider({ ok: true }), schemas: artifactSchemas });
    // No runtime.artifacts block -> default policy has NO local roots.
    await expect(activities["review_evidence_item"]!({ id: "e1", files: ["logo.png"] })).rejects.toThrow(
      /require at least one configured artifact local_root/,
    );
  });
});

describe("assembleYamlRuntime (#452)", () => {
  it("assembles the activity map + plan + task queue from the spec", async () => {
    const { activities, plan, taskQueue } = assembleYamlRuntime(loadYamlSpec(SPEC), {
      provider: new StaticProvider({ ok: true }),
      schemas,
    });
    expect(Object.keys(activities).sort()).toEqual(["consolidate_claim_review", "review_evidence_item"]);
    expect(taskQueue).toBe("spec-queue");
    expect(plan.steps).toEqual([
      { kind: "map", id: "review_evidence", activity: "review_evidence_item", over: "input.evidence", collectField: "reviews", collectMaxBytes: 1_500_000 },
      { kind: "activity", id: "consolidate", activity: "consolidate_claim_review" },
    ]);
    // The assembled activity is fully wired (provider + inline registry) and runs.
    expect(await activities["review_evidence_item"]!({ id: "e1" })).toEqual({ ok: true });
  });

  it("wires provider_retry class-selection booleans into the executor (#529)", async () => {
    const spec = loadYamlSpec(
      SPEC.replace(
        "  provider: { type: openai }",
        "  provider: { type: openai }\n  provider_retry: { max_attempts: 3, retry_rate_limits: false }",
      ),
    );
    const scripted = (first: Error) => {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) throw first;
          return { ok: true };
        },
      };
      return { provider, calls: () => calls };
    };
    // retry_rate_limits: false — a rate-limit error fails FAST (1 call, no retries)…
    const rateLimited = scripted(new ProviderRateLimitError("429"));
    const failFast = assembleYamlRuntime(spec, { provider: rateLimited.provider, schemas });
    await expect(failFast.activities["review_evidence_item"]!({ id: "e1" })).rejects.toBeInstanceOf(
      ProviderRateLimitError,
    );
    expect(rateLimited.calls()).toBe(1);
    // …while a plain transient error still uses the max_attempts budget.
    const transient = scripted(new ProviderTransientError("503"));
    const retries = assembleYamlRuntime(spec, { provider: transient.provider, schemas });
    expect(await retries.activities["review_evidence_item"]!({ id: "e1" })).toEqual({ ok: true });
    expect(transient.calls()).toBe(2);
  });

  it("wires provider_limits into ONE shared controller across activities (#529)", async () => {
    const spec = loadYamlSpec(
      SPEC.replace(
        "  provider: { type: openai }",
        "  provider: { type: openai }\n  provider_limits: { default: { max_concurrent: 1 } }",
      ),
    );
    let active = 0;
    let peak = 0;
    const provider: ModelProvider = {
      structuredCall: async () => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((resolve) => setTimeout(resolve, 10));
        active -= 1;
        return { ok: true };
      },
    };
    const { activities } = assembleYamlRuntime(spec, { provider, schemas });
    // Concurrency across BOTH registered activities is bounded by the shared limiter.
    await Promise.all([
      activities["review_evidence_item"]!({ id: "e1" }),
      activities["review_evidence_item"]!({ id: "e2" }),
      activities["review_evidence_item"]!({ id: "e3" }),
    ]);
    expect(peak).toBe(1);
  });

  it("an EMPTY provider_limits model tier falls through to the provider tier (codex P2)", async () => {
    const spec = loadYamlSpec(
      SPEC.replace(
        "  provider: { type: openai }",
        "  provider: { type: openai, model: gpt-4o }\n" +
          "  provider_limits: { providers: { openai: { max_concurrent: 1, models: { gpt-4o: {} } } } }",
      ),
    );
    let active = 0;
    let peak = 0;
    const provider: ModelProvider = {
      providerName: "openai",
      structuredCall: async () => {
        active += 1;
        peak = Math.max(peak, active);
        await new Promise((resolve) => setTimeout(resolve, 10));
        active -= 1;
        return { ok: true };
      },
    } as ModelProvider;
    const { activities } = assembleYamlRuntime(spec, { provider, schemas });
    await Promise.all([
      activities["review_evidence_item"]!({ id: "e1" }),
      activities["review_evidence_item"]!({ id: "e2" }),
    ]);
    // The empty gpt-4o tier must NOT register as a model policy that disables limits.
    expect(peak).toBe(1);
  });

  it("threads an observer so the YAML runtime's activities capture executions", async () => {
    const observer = new CollectingObserver();
    const { activities } = assembleYamlRuntime(loadYamlSpec(SPEC), {
      provider: new StaticProvider({ ok: true }),
      schemas,
      observer,
    });
    await activities["review_evidence_item"]!({ id: "e1" });
    expect(observer.activities).toHaveLength(1);
    expect(observer.activities[0]!.activityName).toBe("review_evidence_item");
  });

  it("resolves prompts via a registryTransport for a non-inline (langfuse) spec", async () => {
    const langfuse = loadYamlSpec(SPEC.replace("type: inline", "type: langfuse"));
    const { activities } = assembleYamlRuntime(langfuse, {
      provider: new StaticProvider({ ok: true }),
      schemas,
      registryTransport: { fetchPrompt: () => ({ messages: [{ role: "user", content: "review the item" }] }) },
    });
    // The activity's prompt ref resolves through the transport-backed registry.
    expect(await activities["review_evidence_item"]!({ id: "e1" })).toEqual({ ok: true });
  });

  it("threads moderators so a spec activity's moderation applies", async () => {
    const moderated = loadYamlSpec(
      SPEC.replace("      prompt: p/review", "      prompt: p/review\n      moderation: { provider: openai }"),
    );
    const { activities } = assembleYamlRuntime(moderated, {
      provider: new StaticProvider({ ok: true }),
      schemas,
      moderators: { review_evidence_item: () => ({ flagged: true, categories: ["x"] }) },
    });
    await expect(activities["review_evidence_item"]!({ id: "e1" })).rejects.toThrow(/moderation blocked/i);
  });

  it("honors a taskQueue override", () => {
    const { taskQueue } = assembleYamlRuntime(loadYamlSpec(SPEC), {
      provider: new StaticProvider({ ok: true }),
      schemas,
      taskQueue: "override-queue",
    });
    expect(taskQueue).toBe("override-queue");
  });

  it("throws when a workflow step references an undefined activity", () => {
    const bad = SPEC.replace("      activity: consolidate_claim_review", "      activity: not_defined");
    expect(() => assembleYamlRuntime(loadYamlSpec(bad), { provider: new StaticProvider({ ok: true }), schemas })).toThrow(
      /references activity "not_defined" which is not among/,
    );
  });

  it("builds the provider from spec.runtime.provider via transports (no injected provider)", async () => {
    let seenModel: string | undefined;
    const { activities } = assembleYamlRuntime(loadYamlSpec(SPEC), {
      schemas,
      // SPEC's runtime.provider.type is openai -> assembleYamlRuntime builds an OpenAIProvider.
      transports: {
        openai: {
          chat: {
            completions: {
              create: async (request) => {
                seenModel = request.model;
                return { choices: [{ message: { content: JSON.stringify({ ok: true }) } }] };
              },
            },
          },
        },
      },
    });
    expect(await activities["review_evidence_item"]!({ id: "e1" })).toEqual({ ok: true });
    expect(seenModel).toBe("gpt-4o-mini"); // default model from providerFromSpec
  });

  it("throws when neither provider nor transports is supplied", () => {
    expect(() => assembleYamlRuntime(loadYamlSpec(SPEC), { schemas })).toThrow(/provide either/);
  });

  it("throws when a non-inline spec has no injected registry", () => {
    const langfuse = loadYamlSpec(SPEC.replace("    type: inline", "    type: langfuse"));
    expect(() => assembleYamlRuntime(langfuse, { provider: new StaticProvider({ ok: true }), schemas })).toThrow(
      /no prompt registry/,
    );
  });
});

describe("provider-params runtime wiring (#495 A2)", () => {
  it("spec params flow provider-defaults < inline-prompt < activity into the CALL", async () => {
    const seen: StructuredCallParams[] = [];
    const capturing: ModelProvider = {
      structuredCall: (params) => {
        seen.push(params);
        return { ok: true };
      },
    };
    const spec = loadYamlSpec(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/review:
        messages: [{ role: user, content: review it }]
        provider_params: { top_p: 0.8, max_tokens: 500 }
  provider: { type: openai, model: default-model, params: { temperature: 0.2, seed: 7, timeout: 30 } }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
      provider_params: { max_tokens: 900 }
workflow:
  name: W
  input: schemas:Claim
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
`);
    const runtime = assembleYamlRuntime(spec, { provider: capturing, schemas });
    const activity = runtime.activities["review_evidence_item"];
    await activity?.({ id: "e1" });
    expect(seen[0]?.providerParams).toEqual({
      temperature: 0.2, // provider defaults
      seed: 7,
      timeout: 30, // reaches the CALL params (operational — excluded from keys in core)
      top_p: 0.8, // inline-prompt layer
      max_tokens: 900, // activity layer wins
      model: "default-model", // the defaults-layer model fold
    });
    expect(seen[0]?.model).toBe("default-model");
  });
});

describe("model folds (#495 A2 finder edges)", () => {
  it("params.model alone becomes the defaults-layer model (Python spec.py model fold)", async () => {
    const seen: StructuredCallParams[] = [];
    const capturing: ModelProvider = {
      structuredCall: (params) => {
        seen.push(params);
        return { ok: true };
      },
    };
    const spec = loadYamlSpec(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/review: review it } }
  provider: { type: openai, params: { model: params-only-model } }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
workflow:
  name: W
  input: schemas:Claim
  steps:
    - id: review_evidence
      map: { activity: review_evidence_item, over: input.evidence }
`);
    const runtime = assembleYamlRuntime(spec, { provider: capturing, schemas });
    await runtime.activities["review_evidence_item"]?.({ id: "e1" });
    expect(seen[0]?.model).toBe("params-only-model");
  });

  it("a SPEC-BUILT provider with no model folds the type default into the call (stale-key guard)", async () => {
    const requests: { model: string }[] = [];
    const spec = loadYamlSpec(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/review: review it } }
  provider: { type: openai }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
workflow:
  name: W
  input: schemas:Claim
  steps:
    - id: review_evidence
      map: { activity: review_evidence_item, over: input.evidence }
`);
    const runtime = assembleYamlRuntime(spec, {
      schemas,
      transports: {
        openai: {
          chat: {
            completions: {
              create: (request) => {
                requests.push(request as { model: string });
                return Promise.resolve({ choices: [{ message: { content: JSON.stringify({ ok: true }) } }] });
              },
            },
          },
        },
      },
    });
    await runtime.activities["review_evidence_item"]?.({ id: "e1" });
    // The implicit default is EXPLICIT in the request (and thus in cache keys).
    expect(requests[0]?.model).toBe("gpt-4o-mini");
  });
});

describe("extraActivities injection (#496 — the modules replacement)", () => {
  it("code-defined descriptors join the map and serve workflow steps", async () => {
    const { z: zod } = await import("zod");
    const { defineActivity } = await import("@typeflux/temporal");
    const extra = defineActivity({
      name: "code_defined",
      prompt: { name: "p/review", label: "production" },
      input: zod.object({ id: zod.string() }),
      output: zod.object({ ok: zod.boolean() }),
    });
    const spec = loadYamlSpec(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/review: review it } }
  provider: { type: openai }
activities: {}
workflow:
  name: W
  input: schemas:Claim
  steps:
    - id: s
      activity: code_defined
`);
    const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
    const runtime = assembleYamlRuntime(spec, { provider, schemas, extraActivities: { code_defined: extra } });
    expect(Object.keys(runtime.activities)).toContain("code_defined");
    await expect(runtime.activities["code_defined"]?.({ id: "x" })).resolves.toEqual({ ok: true });
  });

  it("a name collision with a spec definition fails LOUD", async () => {
    const { z: zod } = await import("zod");
    const { defineActivity } = await import("@typeflux/temporal");
    const collider = defineActivity({
      name: "review_evidence_item",
      prompt: { name: "p/review", label: "production" },
      input: zod.object({ id: zod.string() }),
      output: zod.object({ ok: zod.boolean() }),
    });
    const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
    expect(() =>
      assembleYamlRuntime(loadYamlSpec(SPEC), { provider, schemas, extraActivities: { review_evidence_item: collider } }),
    ).toThrow(/duplicate activity name/);
  });
});

describe("injected sessionCache reaches the plan (#496 codex)", () => {
  it("a cache-enabled extra descriptor on a map step carries sessionCache in the plan", async () => {
    const { z: zod } = await import("zod");
    const { defineActivity } = await import("@typeflux/temporal");
    const cached = defineActivity({
      name: "code_cached",
      prompt: { name: "p/review", label: "production" },
      input: zod.object({ id: zod.string() }),
      output: zod.object({ ok: zod.boolean() }),
      sessionCache: { ttlSeconds: 600 },
    });
    const spec = loadYamlSpec(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/review: review it } }
  provider: { type: openai }
activities: {}
workflow:
  name: W
  input: schemas:Claim
  steps:
    - id: s
      map: { activity: code_cached, over: input.evidence }
`);
    const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
    const runtime = assembleYamlRuntime(spec, { provider, schemas, extraActivities: { code_cached: cached } });
    expect(runtime.plan.steps[0]).toMatchObject({ kind: "map", sessionCache: { enabled: true, ttlSeconds: 600 } });
    // The companions were registered alongside.
    expect(Object.keys(runtime.activities).sort()).toEqual([
      "code_cached",
      "code_cached.__prepare_cache__",
      "code_cached.__release_cache__",
    ]);
  });
});

describe("assembleYamlRuntime policy pre-flight (#454 slice 2)", () => {
  const policyOf = (yaml: string) =>
    composeProjectPolicies([{ id: "org", spec: loadPolicySpec(`name: org\n${yaml}`) }], ["org"]);

  it("a compliant spec assembles under a governing policy", () => {
    // SPEC's provider is openai with no model → effective default gpt-4o-mini.
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    const runtime = assembleYamlRuntime(loadYamlSpec(SPEC), {
      provider: new StaticProvider({ ok: true }),
      schemas,
      policy,
    });
    expect(runtime.taskQueue).toBe("spec-queue");
  });

  it("a non-compliant spec refuses to assemble (fail-closed, before any build)", () => {
    const policy = policyOf("providers: { allowed: { anthropic: {} } }"); // openai not allowed
    expect(() =>
      assembleYamlRuntime(loadYamlSpec(SPEC), { provider: new StaticProvider({ ok: true }), schemas, policy }),
    ).toThrow(ProjectPolicyEnforcementError);
  });

  it("omitting the policy skips governance entirely", () => {
    const runtime = assembleYamlRuntime(loadYamlSpec(SPEC), {
      provider: new StaticProvider({ ok: true }),
      schemas,
    });
    expect(runtime.plan.steps.length).toBeGreaterThan(0);
  });
});

describe("assembleYamlRuntime runtime policy guard (#454 slice 3)", () => {
  const policyOf = (yaml: string) =>
    composeProjectPolicies([{ id: "org", spec: loadPolicySpec(`name: org\n${yaml}`) }], ["org"]);

  it("the per-call guard catches a code-defined activity's disallowed model that admission cannot see", async () => {
    const { z: zod } = await import("zod");
    const { defineActivity } = await import("@typeflux/temporal");
    // SPEC's provider is openai (default gpt-4o-mini, allowed) — admission passes. A
    // code-defined activity pins gpt-4o via providerParams; it is NOT in the spec, so
    // admission never sees it, but the RUNTIME guard checks the resolved model.
    const rogue = defineActivity({
      name: "rogue_model",
      prompt: { name: "p/review", label: "production" },
      input: zod.object({ id: zod.string() }),
      output: zod.object({ ok: zod.boolean() }),
      providerParams: { model: "gpt-4o" },
    });
    const policy = policyOf("providers: { allowed: { openai: { models: [gpt-4o-mini] } } }");
    const { activities } = assembleYamlRuntime(loadYamlSpec(SPEC), {
      provider: new StaticProvider({ ok: true }),
      schemas,
      policy,
      extraActivities: { rogue_model: rogue },
    });
    // The worker boundary surfaces the guard's ProviderPolicyError as a NON-RETRYABLE
    // ApplicationFailure (it is terminal — the same call reproduces it).
    const error = await activities["rogue_model"]!({ id: "e1" }).then(
      () => undefined,
      (e: unknown) => e as { message: string; type?: string; nonRetryable?: boolean },
    );
    expect(error?.message).toMatch(/provider model 'gpt-4o' is not allowed/);
    expect(error?.type).toBe("ProviderPolicyError");
    expect(error?.nonRetryable).toBe(true);
    // A spec activity (no pinned model → the injected provider's default) still runs.
    await expect(activities["review_evidence_item"]!({ id: "e1" })).resolves.toBeDefined();
  });

  it("the moderation guard escalates a disallowed category even when the moderator does NOT flag", async () => {
    // semantics.categories is a RUNTIME-only control (admission skips it). A lenient
    // moderator clears the output (flagged: false) but reports a category the policy
    // forbids, so the guard escalates it to a block.
    const moderated = loadYamlSpec(
      SPEC.replace("      prompt: p/review", "      prompt: p/review\n      moderation: { provider: openai }"),
    );
    const policy = policyOf("semantics: { categories: [violence] }");
    const { activities } = assembleYamlRuntime(moderated, {
      provider: new StaticProvider({ ok: true }),
      schemas,
      policy,
      // A lenient moderator: NOT flagged, but reports the disallowed category.
      moderators: { review_evidence_item: () => ({ flagged: false, categories: ["violence"] }) },
    });
    await expect(activities["review_evidence_item"]!({ id: "e1" })).rejects.toThrow(/disallowed category violence/);
  });

  it("a code-defined activity missing required moderation is REFUSED by the pre-flight (semantics.required)", async () => {
    const { z: zod } = await import("zod");
    const { defineActivity } = await import("@typeflux/temporal");
    // A spec whose single activity IS moderated, so admission (which sees only spec
    // definitions) passes — leaving the code-defined activity as the one to catch.
    const moderatedSpec = loadYamlSpec(`
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/review: review it } }
  provider: { type: openai }
activities:
  definitions:
    - name: spec_act
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
      moderation: { provider: openai }
workflow:
  name: W
  input: schemas:EvidenceItem
  steps:
    - id: s
      activity: spec_act
`);
    const unmoderated = defineActivity({
      name: "no_moderator",
      prompt: { name: "p/review", label: "production" },
      input: zod.object({ id: zod.string() }),
      output: zod.object({ ok: zod.boolean() }),
    });
    const policy = policyOf("semantics: { required: true }");
    // Admission's validateSemantics only sees spec_act (moderated → passes); the
    // pre-flight's extraActivities check catches the code-defined no_moderator.
    expect(() =>
      assembleYamlRuntime(moderatedSpec, {
        provider: new StaticProvider({ ok: true }),
        schemas,
        policy,
        extraActivities: { no_moderator: unmoderated },
      }),
    ).toThrow(/'no_moderator' must declare moderation/);
  });
});

describe("buildRuntime codec/dataConverter ambiguity guard (#188 FIX 3)", () => {
  const CODEC_SPEC = SPEC.replace(
    "  temporal: {}",
    `  temporal:
    payload_codec:
      type: aes
      current: k1
      keys:
        - { id: k1, value_from: { env: TF_CODEC_KEY } }`,
  );

  it("rejects a NESTED worker.workerOptions.dataConverter alongside a spec-declared codec", async () => {
    // FAIL-CLOSED (D188-3): the ambiguity guard must catch a caller's converter at EITHER
    // level. The nested `worker.workerOptions.dataConverter` passthrough previously escaped
    // the top-level check and the codec would silently override it — dropping the caller's
    // converter. It must now throw the same fail-closed error before any worker is built.
    process.env.TF_CODEC_KEY = ("01234567" + "89abcdef").repeat(2); // exactly 32 bytes
    try {
      await expect(
        buildRuntime(loadYamlSpec(CODEC_SPEC), {
          provider: new StaticProvider({ ok: true }),
          schemas,
          worker: { workerOptions: { dataConverter: { payloadCodecs: [] } } },
        }),
      ).rejects.toThrow(/must not be silently overridden/);
    } finally {
      delete process.env.TF_CODEC_KEY;
    }
  });
});
