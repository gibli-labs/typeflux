import { describe, expect, it } from "vitest";
import { z } from "zod";

import type {
  ActivityContext,
  ModelProvider,
  ResolvedArtifactGroup,
  StructuredCallParams,
} from "../src/index.js";
import {
  ActivityValidationError,
  artifactInput,
  artifactRefSchema,
  defineActivity,
  defineCodeActivity,
  executeActivity,
  InMemoryCacheStore,
} from "../src/index.js";

const Input = z.object({ text: z.string() });
const Output = z.object({
  label: z.enum(["billing", "support"]),
  score: z.number(),
  notes: z.string().optional(),
});
const PROMPT = { name: "support/classify", label: "production" } as const;

function activity(extra?: { cache?: { bypassReadsEnv?: string } }) {
  return defineActivity({
    name: "classify",
    prompt: PROMPT,
    input: Input,
    output: Output,
    ...(extra?.cache ? { cache: extra.cache } : {}),
  });
}

/** Returns queued raw responses; counts calls. */
class FakeProvider implements ModelProvider {
  calls: StructuredCallParams[] = [];
  constructor(private readonly responses: unknown[]) {}
  structuredCall(params: StructuredCallParams): unknown {
    this.calls.push(params);
    if (this.responses.length === 0) {
      throw new Error("FakeProvider has no responses left");
    }
    return this.responses.shift();
  }
}

const MESSAGES = [{ role: "user", content: "Classify: hello" }];

describe("executeActivity (#387 provider dispatch)", () => {
  it("returns the validated typed output and sends the provider-safe schema", async () => {
    const provider = new FakeProvider([{ label: "billing", score: 0.9 }]);
    const out = await executeActivity(activity(), { text: "hi" }, { provider, messages: MESSAGES });
    expect(out).toEqual({ label: "billing", score: 0.9 });
    expect(provider.calls).toHaveLength(1);
    expect(provider.calls[0]?.outputSchema["additionalProperties"]).toBe(false);
  });

  it("repairs on an invalid first response then succeeds", async () => {
    const provider = new FakeProvider([
      { label: "nope", score: "high" }, // invalid: bad enum + non-number
      { label: "support", score: 1 },
    ]);
    const out = await executeActivity(activity(), { text: "hi" }, { provider, messages: MESSAGES });
    expect(out.label).toBe("support");
    expect(provider.calls).toHaveLength(2);
    // The repair message was appended for the 2nd call.
    expect(provider.calls[1]?.messages.at(-1)?.content).toContain("failed output validation");
  });

  it("throws ActivityValidationError after exhausting retries", async () => {
    const provider = new FakeProvider([{ bad: 1 }, { bad: 2 }]); // validationRetries defaults to 1 → 2 attempts
    await expect(
      executeActivity(activity(), { text: "hi" }, { provider, messages: MESSAGES }),
    ).rejects.toBeInstanceOf(ActivityValidationError);
    expect(provider.calls).toHaveLength(2);
  });

  it("coerces a provider null for an optional (non-nullable) field (#427)", async () => {
    // The provider-safe schema makes `notes` required+nullable, so the model emits null;
    // bare .optional() rejects null, so the executor drops it before validation.
    const provider = new FakeProvider([{ label: "billing", score: 1, notes: null }]);
    const out = await executeActivity(activity(), { text: "hi" }, { provider, messages: MESSAGES });
    expect(out).toEqual({ label: "billing", score: 1 });
  });

  it("runs the context hook with deps + tenant", async () => {
    const writes: string[] = [];
    const hooked = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      hook: (input, output, ctx: ActivityContext) => {
        writes.push(`${ctx.tenant["company_id"]}:${input.text}:${output.label}`);
        return output;
      },
    });
    const provider = new FakeProvider([{ label: "support", score: 1 }]);
    await executeActivity(hooked, { text: "hi" }, {
      provider,
      messages: MESSAGES,
      tenant: { company_id: "co-1" },
      deps: null,
    });
    expect(writes).toEqual(["co-1:hi:support"]);
  });

  it("a cache hit skips the provider but still runs the hook", async () => {
    const store = new InMemoryCacheStore();
    const writes: string[] = [];
    const cachedActivity = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      hook: (_input, output) => {
        writes.push(output.label);
        return output;
      },
    });
    const provider = new FakeProvider([{ label: "billing", score: 1 }]); // a single response
    const opts = { provider, messages: MESSAGES, cacheStore: store, tenant: { company_id: "co-1" } };

    const first = await executeActivity(cachedActivity, { text: "hi" }, opts);
    expect(provider.calls).toHaveLength(1);

    const second = await executeActivity(cachedActivity, { text: "hi" }, opts);
    expect(provider.calls).toHaveLength(1); // hit → provider not called again
    expect(first).toEqual(second);
    expect(writes).toEqual(["billing", "billing"]); // hook ran on both
  });

  it("an artifact bytes swap under the same group + prompt busts the cache (#504)", async () => {
    const store = new InMemoryCacheStore();
    const act = defineActivity({
      name: "review_claim",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      artifacts: [
        artifactInput({ name: "docs", from_path: "input.text", attach: { role: "user", text: "Use the attached docs." } }),
      ],
    });
    // Pre-resolved groups differing ONLY in the artifact's sha256 — the rendered
    // messages (group name + preamble) are identical across all three runs.
    const groupsWith = (sha256: string): ResolvedArtifactGroup[] => [
      {
        name: "docs",
        artifacts: [
          {
            group: "docs",
            index: 0,
            ref: artifactRefSchema.parse({ source: "claim.txt" }),
            source_kind: "local_path",
            kind: "document",
            media_type: "text/plain",
            sha256,
            size_bytes: 11,
            local_path: "/tmp/claim.txt",
          },
        ],
      },
    ];
    const provider = new FakeProvider([
      { label: "billing", score: 1 },
      { label: "support", score: 2 },
    ]);
    const base = { provider, messages: MESSAGES, cacheStore: store };

    const first = await executeActivity(act, { text: "hi" }, { ...base, artifacts: groupsWith("a".repeat(64)) });
    expect(first.label).toBe("billing");
    expect(provider.calls).toHaveLength(1);

    // Same bytes → hit (the provider is not called again).
    const second = await executeActivity(act, { text: "hi" }, { ...base, artifacts: groupsWith("a".repeat(64)) });
    expect(second.label).toBe("billing");
    expect(provider.calls).toHaveLength(1);

    // Different bytes → miss + regeneration (pre-#504 this served the stale "billing").
    const third = await executeActivity(act, { text: "hi" }, { ...base, artifacts: groupsWith("f".repeat(64)) });
    expect(third.label).toBe("support");
    expect(provider.calls).toHaveLength(2);
  });

  it("bypassReadsEnv forces a re-call but still writes", async () => {
    const store = new InMemoryCacheStore();
    const act = activity({ cache: { bypassReadsEnv: "TF_TEST_NO_CACHE" } });
    const provider = new FakeProvider([
      { label: "billing", score: 1 },
      { label: "support", score: 2 },
    ]);
    const opts = { provider, messages: MESSAGES, cacheStore: store };

    await executeActivity(act, { text: "hi" }, opts);
    expect(provider.calls).toHaveLength(1);

    process.env["TF_TEST_NO_CACHE"] = "1";
    try {
      const second = await executeActivity(act, { text: "hi" }, opts);
      expect(second.label).toBe("support");
      expect(provider.calls).toHaveLength(2);
    } finally {
      delete process.env["TF_TEST_NO_CACHE"];
    }
  });

  it("treats a cached record with a stale output schema hash as a miss", async () => {
    const store = new InMemoryCacheStore();
    // Same name/input/messages -> same cache key, but different output schemas.
    const v1 = defineActivity({
      name: "x",
      prompt: PROMPT,
      input: Input,
      output: z.object({ label: z.string() }),
      cache: {},
    });
    const v2 = defineActivity({
      name: "x",
      prompt: PROMPT,
      input: Input,
      output: z.object({ label: z.string(), score: z.number() }),
      cache: {},
    });
    await executeActivity(v1, { text: "hi" }, {
      provider: new FakeProvider([{ label: "a" }]),
      messages: MESSAGES,
      cacheStore: store,
    });
    const p2 = new FakeProvider([{ label: "b", score: 1 }]);
    const out = await executeActivity(v2, { text: "hi" }, {
      provider: p2,
      messages: MESSAGES,
      cacheStore: store,
    });
    // v2 must regenerate (stale schema hash), not return v1's incompatible output.
    expect(out).toEqual({ label: "b", score: 1 });
    expect(p2.calls).toHaveLength(1);
  });

  it("coerces nested optional nulls in objects and arrays (#427)", async () => {
    const NestedOut = z.object({
      label: z.string(),
      profile: z.object({ notes: z.string().optional() }),
      items: z.array(z.object({ tag: z.string().optional() })),
    });
    const nested = defineActivity({ name: "n", prompt: PROMPT, input: Input, output: NestedOut });
    const provider = new FakeProvider([
      { label: "x", profile: { notes: null }, items: [{ tag: null }, { tag: "a" }] },
    ]);
    const out = await executeActivity(nested, { text: "hi" }, { provider, messages: MESSAGES });
    expect(out).toEqual({ label: "x", profile: {}, items: [{}, { tag: "a" }] });
  });

  it("includes the model in the cache key even when providerParams is given", async () => {
    const store = new InMemoryCacheStore();
    const act = activity({ cache: {} });
    await executeActivity(act, { text: "hi" }, {
      provider: new FakeProvider([{ label: "billing", score: 1 }]),
      messages: MESSAGES,
      cacheStore: store,
      model: "model-a",
      providerParams: { temperature: 0 },
    });
    // Same providerParams, different model -> distinct key -> miss, not a cross-model hit.
    const pB = new FakeProvider([{ label: "support", score: 2 }]);
    const out = await executeActivity(act, { text: "hi" }, {
      provider: pB,
      messages: MESSAGES,
      cacheStore: store,
      model: "model-b",
      providerParams: { temperature: 0 },
    });
    expect(out.label).toBe("support");
    expect(pB.calls).toHaveLength(1);
  });

  it("re-validates the hook output (rejects a hook returning the wrong shape)", async () => {
    const badHook = defineActivity({
      name: "c",
      prompt: PROMPT,
      input: Input,
      output: Output,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      hook: () => ({ label: "not-an-enum", score: "bad" }) as any,
    });
    await expect(
      executeActivity(badHook, { text: "hi" }, {
        provider: new FakeProvider([{ label: "billing", score: 1 }]),
        messages: MESSAGES,
      }),
    ).rejects.toThrow();
  });
});

describe("executeActivity outputCheck (#745 input-aware repair)", () => {
  // The output's `score` must equal the input text's length (a cross-field contract
  // zod cannot express: it needs the INPUT). A hallucinated score should get repaired.
  const groundedCheck = (input: { text: string }, output: { score: number }) =>
    output.score === input.text.length ? [] : [{ message: "score must equal input.text length", path: ["score"] }];

  it("a violation feeds the repair loop and the model self-corrects on attempt 2", async () => {
    const provider = new FakeProvider([
      { label: "support", score: 99 }, // schema-valid but violates the grounding check
      { label: "support", score: 2 }, // corrected: "hi".length === 2
    ]);
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      outputCheck: groundedCheck,
    });
    const out = await executeActivity(act, { text: "hi" }, { provider, messages: MESSAGES });
    expect(out).toEqual({ label: "support", score: 2 });
    expect(provider.calls).toHaveLength(2);
    // The repair message presented the violation legibly to the model.
    const repair = provider.calls[1]?.messages.at(-1)?.content ?? "";
    expect(repair).toContain("failed output validation");
    expect(repair).toContain("score must equal input.text length");
  });

  it("throws ActivityValidationError naming the violations once retries are exhausted", async () => {
    const provider = new FakeProvider([
      { label: "support", score: 99 },
      { label: "support", score: 98 }, // still wrong on the retry
    ]);
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      outputCheck: groundedCheck,
    });
    let caught: unknown;
    await executeActivity(act, { text: "hi" }, { provider, messages: MESSAGES }).catch((e) => (caught = e));
    expect(caught).toBeInstanceOf(ActivityValidationError);
    const err = caught as ActivityValidationError;
    expect(err.outputCheckViolations).toEqual([
      { message: "score must equal input.text length", path: ["score"] },
    ]);
    expect(err.zodError).toBeUndefined();
    expect(err.message).toContain("score must equal input.text length");
    expect(provider.calls).toHaveLength(2); // validationRetries defaults to 1 → 2 attempts
  });

  it("sees the PARSED input (schema defaults materialized)", async () => {
    const InputWithDefault = z.object({ text: z.string(), threshold: z.number().default(5) });
    let seenThreshold: number | undefined;
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: InputWithDefault,
      output: Output,
      outputCheck: (input, _output) => {
        seenThreshold = input.threshold;
        return [];
      },
    });
    const provider = new FakeProvider([{ label: "support", score: 1 }]);
    // `threshold` omitted by the caller (executeActivity types input as z.infer, i.e. the
    // OUTPUT type with the default already applied — the cast reflects a real wire caller
    // that omits it). The executor's safeParse must still materialize the default (5).
    await executeActivity(
      act,
      { text: "hi" } as z.infer<typeof InputWithDefault>,
      { provider, messages: MESSAGES },
    );
    expect(seenThreshold).toBe(5);
  });

  it("a code-activity outputCheck failure is terminal (no repair loop)", async () => {
    const CodeIn = z.object({ items: z.array(z.string()) });
    const CodeOut = z.object({ count: z.number() });
    const act = defineCodeActivity({
      name: "refine",
      input: CodeIn,
      output: CodeOut,
      handler: (input) => ({ count: input.items.length + 1 }), // deliberately off by one
      outputCheck: (input, output) =>
        output.count === input.items.length ? [] : [{ message: "count must equal item count" }],
    });
    let caught: unknown;
    await executeActivity(act, { items: ["a", "b"] }, { provider: new FakeProvider([]) }).catch(
      (e) => (caught = e),
    );
    expect(caught).toBeInstanceOf(ActivityValidationError);
    expect((caught as ActivityValidationError).outputCheckViolations).toEqual([
      { message: "count must equal item count" },
    ]);
  });

  it("does NOT cache an outputCheck-rejected output (next execution calls the provider again)", async () => {
    const store = new InMemoryCacheStore();
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      validationRetries: 0, // reject terminally on the first attempt
      outputCheck: () => [{ message: "always rejected" }],
    });
    const provider = new FakeProvider([
      { label: "support", score: 1 },
      { label: "support", score: 1 },
    ]);
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    await expect(executeActivity(act, { text: "hi" }, opts)).rejects.toBeInstanceOf(ActivityValidationError);
    expect(provider.calls).toHaveLength(1);
    // Nothing was cached, so the next execution must call the provider again.
    await expect(executeActivity(act, { text: "hi" }, opts)).rejects.toBeInstanceOf(ActivityValidationError);
    expect(provider.calls).toHaveLength(2);
  });

  it("caches an accepted output exactly once (second execution is a cache hit)", async () => {
    const store = new InMemoryCacheStore();
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      outputCheck: () => [], // always accepts
    });
    const provider = new FakeProvider([{ label: "support", score: 1 }]); // one response only
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    const first = await executeActivity(act, { text: "hi" }, opts);
    const second = await executeActivity(act, { text: "hi" }, opts);
    expect(first).toEqual(second);
    expect(provider.calls).toHaveLength(1); // second served from cache
  });

  it("runs outputCheck BEFORE the hook, and a hook rejection is not cached", async () => {
    const store = new InMemoryCacheStore();
    const order: string[] = [];
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      outputCheck: (_input, _output) => {
        order.push("check");
        return [];
      },
      hook: (_input, _output): never => {
        order.push("hook");
        throw new Error("hook rejects");
      },
    });
    const provider = new FakeProvider([
      { label: "support", score: 1 },
      { label: "support", score: 1 },
    ]);
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    await expect(executeActivity(act, { text: "hi" }, opts)).rejects.toThrow("hook rejects");
    // outputCheck (pre-acceptance) ran before the hook (post-acceptance).
    expect(order).toEqual(["check", "hook"]);
    // The hook rejected AFTER the (old) cache-write site, so the fix must have withheld
    // the write — the next execution calls the provider again.
    await expect(executeActivity(act, { text: "hi" }, opts)).rejects.toThrow("hook rejects");
    expect(provider.calls).toHaveLength(2);
  });

  it("a tightened outputCheck invalidates a stale cache hit (regenerates + re-caches)", async () => {
    const store = new InMemoryCacheStore();
    const base = { name: "classify", prompt: PROMPT, input: Input, output: Output, cache: {} } as const;
    // Cache under NO check: score=99 is stored.
    const unchecked = defineActivity({ ...base });
    const provider = new FakeProvider([
      { label: "support", score: 99 },
      { label: "support", score: 2 }, // the regeneration after the check rejects the hit
    ]);
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    await executeActivity(unchecked, { text: "hi" }, opts);
    expect(provider.calls).toHaveLength(1);
    // "Redeploy" with a rejecting check: the hit (score=99) now violates → treated as a
    // MISS → provider called again with the full repair loop; the fresh output passes.
    const checked = defineActivity({ ...base, outputCheck: groundedCheck });
    const regenerated = await executeActivity(checked, { text: "hi" }, opts);
    expect(regenerated).toEqual({ label: "support", score: 2 });
    expect(provider.calls).toHaveLength(2);
    // The corrected output re-cached over the stale entry: a third run is a passing
    // HIT (no provider call — the provider has no responses left and would throw).
    const third = await executeActivity(checked, { text: "hi" }, opts);
    expect(third).toEqual({ label: "support", score: 2 });
    expect(provider.calls).toHaveLength(2);
  });

  it("a hit under a PASSING outputCheck stays a hit (no provider call)", async () => {
    const store = new InMemoryCacheStore();
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      outputCheck: groundedCheck,
    });
    const provider = new FakeProvider([{ label: "support", score: 2 }]); // one response only
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    const first = await executeActivity(act, { text: "hi" }, opts);
    const second = await executeActivity(act, { text: "hi" }, opts);
    expect(first).toEqual(second);
    expect(provider.calls).toHaveLength(1);
  });

  it("an in-place-mutating hook does not contaminate the cache (stores the PRE-hook value)", async () => {
    const store = new InMemoryCacheStore();
    const hookInputScores: number[] = [];
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
      hook: (_input, output) => {
        hookInputScores.push(output.score);
        output.score += 1; // mutate IN PLACE (the aliasing hazard), then return the same object
        return output;
      },
    });
    const provider = new FakeProvider([{ label: "support", score: 1 }]); // one response only
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    const first = await executeActivity(act, { text: "hi" }, opts);
    expect(first.score).toBe(2); // hook transformed the returned value
    // The HIT must re-run the hook on the PRE-hook value (1), exactly once — a
    // contaminated cache would hand the hook 2 and yield 3.
    const second = await executeActivity(act, { text: "hi" }, opts);
    expect(second.score).toBe(2);
    expect(hookInputScores).toEqual([1, 1]);
    expect(provider.calls).toHaveLength(1);
  });

  it("the hook receives the PARSED input on fresh + cache-hit paths (parity with outputCheck)", async () => {
    // Bugbot #749: the hook must see the same materialized-defaults input the
    // outputCheck validated — on the generation path AND the hit path (Python
    // passes prepared.input_value to both).
    const InputWithDefault = z.object({ text: z.string(), threshold: z.number().default(5) });
    const store = new InMemoryCacheStore();
    const checkThresholds: (number | undefined)[] = [];
    const hookThresholds: (number | undefined)[] = [];
    const act = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: InputWithDefault,
      output: Output,
      cache: {},
      outputCheck: (input, _output) => {
        checkThresholds.push(input.threshold);
        return [];
      },
      hook: (input, output) => {
        hookThresholds.push(input.threshold);
        return output;
      },
    });
    const provider = new FakeProvider([{ label: "support", score: 1 }]); // one response only
    // Direct call with `threshold` OMITTED (the cast reflects a real wire caller).
    const partial = { text: "hi" } as z.infer<typeof InputWithDefault>;
    const opts = { provider, messages: MESSAGES, cacheStore: store };
    await executeActivity(act, partial, opts); // fresh generation
    await executeActivity(act, partial, opts); // cache hit
    expect(provider.calls).toHaveLength(1);
    // Both consumers saw the materialized default, on both paths — and agree.
    expect(checkThresholds).toEqual([5, 5]);
    expect(hookThresholds).toEqual([5, 5]);
  });

  it("normalizes outputCheck return shapes (single violation coerced; junk is a pointed TypeError)", async () => {
    const run = (returned: unknown) =>
      executeActivity(
        defineActivity({
          name: "classify",
          prompt: PROMPT,
          input: Input,
          output: Output,
          validationRetries: 0,
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          outputCheck: (() => returned) as any,
        }),
        { text: "hi" },
        { provider: new FakeProvider([{ label: "support", score: 1 }]), messages: MESSAGES },
      );
    // void / empty array → pass.
    await expect(run(undefined)).resolves.toEqual({ label: "support", score: 1 });
    await expect(run([])).resolves.toEqual({ label: "support", score: 1 });
    // A SINGLE violation object (a common slip for [violation]) → coerced → rejection.
    let caught: unknown;
    await run({ message: "bare violation" }).catch((e) => (caught = e));
    expect(caught).toBeInstanceOf(ActivityValidationError);
    expect((caught as ActivityValidationError).outputCheckViolations).toEqual([
      { message: "bare violation" },
    ]);
    // A string / number / truthy junk / junk-bearing array → pointed TypeError, not a
    // silent pass (the pre-fix fail-open) and not a garbled repair turn.
    for (const junk of ["looks truthy", 1, { nope: true }, [{ message: 42 }]]) {
      await expect(run(junk)).rejects.toThrow(
        "outputCheck must return void or OutputCheckViolation[]",
      );
    }
  });
});
