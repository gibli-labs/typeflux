import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ActivityContext, ModelProvider, StructuredCallParams } from "../src/index.js";
import {
  ActivityValidationError,
  CollectingObserver,
  defineActivity,
  defineCodeActivity,
  executeActivity,
} from "../src/index.js";

const Input = z.object({ items: z.array(z.string()) });
const Output = z.object({ count: z.number(), joined: z.string() });

/** A provider that fails if the code path ever calls it — proves no provider dispatch. */
class ThrowingProvider implements ModelProvider {
  calls = 0;
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    throw new Error("a code activity must never call the provider");
  }
}

function refine() {
  return defineCodeActivity({
    name: "refine",
    input: Input,
    output: Output,
    handler: (input) => ({ count: input.items.length, joined: input.items.join(",") }),
  });
}

describe("defineCodeActivity (#746 descriptor)", () => {
  it("carries the code discriminant, a handler, and no prompt", () => {
    const descriptor = refine();
    expect(descriptor.kind).toBe("code");
    expect(typeof descriptor.handler).toBe("function");
    // The CodeActivityDescriptor member has NO `prompt` field at all (not even optional).
    expect("prompt" in descriptor).toBe(false);
    // Deterministic code: no repair loop.
    expect(descriptor.validationRetries).toBe(0);
  });

  it("defineActivity keeps prompt REQUIRED on its returned type (type-level, no optional chaining)", () => {
    const ai = defineActivity({ name: "refine", prompt: { name: "p" }, input: Input, output: Output });
    // Compiles WITHOUT `?.` — the discriminated union keeps `prompt: PromptRef` required on the
    // AI member, so pre-#746 consumer code reading `.prompt.name` is not a strict-mode regression.
    const promptName: string = ai.prompt.name;
    expect(promptName).toBe("p");
  });

  it("the union narrows on the kind discriminant", () => {
    const descriptors = [refine(), defineActivity({ name: "ai", prompt: { name: "p" }, input: Input, output: Output })];
    for (const descriptor of descriptors) {
      if (descriptor.kind === "code") {
        // Narrowed: handler is required here (calling it type-checks without a guard).
        expect(typeof descriptor.handler).toBe("function");
      } else {
        // Narrowed: prompt is required here.
        expect(descriptor.prompt.name).toBe("p");
      }
    }
  });

  it("shares slot JSON-Schema/hash identity with a defineActivity over the same schemas", () => {
    const code = refine();
    const ai = defineActivity({ name: "refine", prompt: { name: "p" }, input: Input, output: Output });
    expect(code.inputSchemaHash).toBe(ai.inputSchemaHash);
    expect(code.outputSchemaHash).toBe(ai.outputSchemaHash);
    expect(code.inputJsonSchema).toEqual(ai.inputJsonSchema);
    expect(code.outputJsonSchema).toEqual(ai.outputJsonSchema);
  });

  it("rejects provider-only options with a clear error", () => {
    const base = { name: "refine", input: Input, output: Output, handler: () => ({ count: 0, joined: "" }) };
    for (const bad of [
      { prompt: { name: "p" } },
      { validationRetries: 3 },
      { cache: {} },
      { sessionCache: { enabled: true } },
      { moderation: { moderator: async () => ({ flagged: false }) } },
      { providerParams: { temperature: 0 } },
      { artifacts: [] },
    ]) {
      const key = Object.keys(bad)[0];
      expect(() => defineCodeActivity({ ...base, ...(bad as object) } as never)).toThrowError(
        new RegExp(`option "${key}" is not supported`),
      );
    }
  });

  it("rejects a non-function handler", () => {
    expect(() =>
      defineCodeActivity({ name: "refine", input: Input, output: Output, handler: 42 as never }),
    ).toThrowError(/handler must be a function/);
  });

  it("allows an open-record output a provider-backed activity could not (no toProviderSafe gate)", () => {
    // z.record is an open map — defineActivity's toProviderSafe rejects it; defineCodeActivity must not.
    const OpenOut = z.object({ tags: z.record(z.string(), z.number()) });
    expect(() =>
      defineCodeActivity({
        name: "tagger",
        input: Input,
        output: OpenOut,
        handler: () => ({ tags: {} }),
      }),
    ).not.toThrow();
    expect(() =>
      defineActivity({ name: "tagger", prompt: { name: "p" }, input: Input, output: OpenOut }),
    ).toThrow();
  });
});

describe("executeActivity (code path, #746)", () => {
  it("runs the handler and returns the parsed output WITHOUT calling the provider", async () => {
    const provider = new ThrowingProvider();
    const out = await executeActivity(refine(), { items: ["a", "b"] }, { provider });
    expect(out).toEqual({ count: 2, joined: "a,b" });
    expect(provider.calls).toBe(0);
  });

  it("fails with ActivityValidationError when the handler output does not match the schema", async () => {
    const bad = defineCodeActivity({
      name: "refine",
      input: Input,
      output: Output,
      handler: () => ({ count: "nope", joined: 7 }) as never,
    });
    await expect(
      executeActivity(bad, { items: [] }, { provider: new ThrowingProvider() }),
    ).rejects.toBeInstanceOf(ActivityValidationError);
  });

  it("runs the post-output hook with the activity context", async () => {
    const seen: string[] = [];
    const hooked = defineCodeActivity({
      name: "refine",
      input: Input,
      output: Output,
      handler: (input) => ({ count: input.items.length, joined: input.items.join(",") }),
      hook: (input, output, ctx: ActivityContext) => {
        seen.push(`${ctx.tenant["company_id"]}:${input.items.length}:${output.count}`);
        return { ...output, joined: output.joined.toUpperCase() };
      },
    });
    const out = await executeActivity(hooked, { items: ["x", "y"] }, {
      provider: new ThrowingProvider(),
      tenant: { company_id: "co-9" },
    });
    expect(seen).toEqual(["co-9:2:2"]);
    // The hook's transform is returned (and re-validated).
    expect(out.joined).toBe("X,Y");
  });

  it("fails the activity when the hook throws", async () => {
    const hooked = defineCodeActivity({
      name: "refine",
      input: Input,
      output: Output,
      handler: (input) => ({ count: input.items.length, joined: "" }),
      hook: () => {
        throw new Error("hook boom");
      },
    });
    await expect(
      executeActivity(hooked, { items: [] }, { provider: new ThrowingProvider() }),
    ).rejects.toThrowError(/hook boom/);
  });

  it("parses the input on the DIRECT path, so schema defaults apply before the handler", async () => {
    // Outside the worker wrapper (which parses at the Temporal boundary), the direct
    // executeActivity path must still apply schema defaults/coercions — the handler is typed
    // against z.infer<In>, where defaulted fields are non-optional.
    const WithDefault = z.object({ items: z.array(z.string()).default([]), mode: z.string().default("fast") });
    const seen: unknown[] = [];
    const act = defineCodeActivity({
      name: "defaulted",
      input: WithDefault,
      output: z.object({ mode: z.string(), count: z.number() }),
      handler: (input) => {
        seen.push(input);
        return { mode: input.mode, count: input.items.length };
      },
    });
    const observer = new CollectingObserver();
    const out = await executeActivity(act, {} as never, { provider: new ThrowingProvider(), observer });
    expect(out).toEqual({ mode: "fast", count: 0 });
    // The handler AND the observation record both see the PARSED input (defaults materialized).
    expect(seen).toEqual([{ items: [], mode: "fast" }]);
    expect(observer.activities[0]!.input).toEqual({ items: [], mode: "fast" });
  });

  it("rejects an off-shape input with a ZodError on the direct path (the worker boundary's taxonomy)", async () => {
    await expect(
      executeActivity(refine(), { items: "not-an-array" } as never, { provider: new ThrowingProvider() }),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  it("emits an activity observation span with NO generation child", async () => {
    const observer = new CollectingObserver();
    await executeActivity(refine(), { items: ["a"] }, { provider: new ThrowingProvider(), observer });
    expect(observer.activities).toHaveLength(1);
    const activity = observer.activities[0]!;
    expect(activity.activityName).toBe("refine");
    expect(activity.generations).toHaveLength(0);
    expect(activity.messages).toEqual([]);
    expect(activity.model).toBeNull();
    expect(activity.ended).toBe(true);
    expect(activity.output).toEqual({ count: 1, joined: "a" });
  });

  it("records a hook observation on the code path", async () => {
    const observer = new CollectingObserver();
    const hooked = defineCodeActivity({
      name: "refine",
      input: Input,
      output: Output,
      handler: (input) => ({ count: input.items.length, joined: "" }),
      hook: (_input, output) => output,
    });
    await executeActivity(hooked, { items: ["a"] }, { provider: new ThrowingProvider(), observer });
    expect(observer.activities[0]!.hooks).toHaveLength(1);
    expect(observer.activities[0]!.generations).toHaveLength(0);
  });
});
