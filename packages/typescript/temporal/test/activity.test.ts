import { describe, expect, expectTypeOf, it } from "vitest";
import { z } from "zod";

import type { ActivityContext } from "../src/index.js";
import {
  defineActivity,
  lintProviderSafe,
  ProviderSchemaError,
  schemaHash,
} from "../src/index.js";

const Input = z.object({ text: z.string() });
const Output = z.object({
  label: z.enum(["billing", "support"]),
  score: z.number().nullable(),
  notes: z.string().optional(),
});

const PROMPT = { name: "support/classify", label: "production" } as const;

describe("defineActivity (#400)", () => {
  it("maps the Zod output to a provider-safe schema", () => {
    const activity = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    expect(lintProviderSafe(activity.outputProviderSchema)).toEqual([]);
    // Optional `notes` becomes required + nullable; objects are closed.
    expect(activity.outputProviderSchema["additionalProperties"]).toBe(false);
    expect(activity.outputProviderSchema["required"]).toEqual(["label", "score", "notes"]);
    expect(JSON.stringify(activity.outputProviderSchema).includes("$schema")).toBe(false);
  });

  it("exposes deterministic schema hashes matching schemaHash of the raw JSON schema", () => {
    const a = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    const b = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    expect(a.inputSchemaHash).toBe(b.inputSchemaHash);
    expect(a.inputSchemaHash).toBe(schemaHash(a.inputJsonSchema));
    expect(a.outputSchemaHash).toBe(schemaHash(a.outputJsonSchema));
    expect(a.inputSchemaHash).toHaveLength(64);
  });

  it("carries prompt, cache, and defaults validationRetries to 1", () => {
    const plain = defineActivity({ name: "classify", prompt: PROMPT, input: Input, output: Output });
    expect(plain.validationRetries).toBe(1);
    expect(plain.prompt).toEqual(PROMPT);
    expect(plain.cache).toBeUndefined();

    const cached = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: { bypassReadsEnv: "MLR_DISABLE_CACHE" },
      validationRetries: 3,
    });
    expect(cached.validationRetries).toBe(3);
    // `enabled` defaults to true (Python parity) so a bypass-only config is opt-in.
    expect(cached.cache).toEqual({ bypassReadsEnv: "MLR_DISABLE_CACHE", enabled: true });
  });

  it("defaults cache.enabled to true, preserving an explicit false", () => {
    const onByDefault = defineActivity({
      name: "c",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: {},
    });
    expect(onByDefault.cache).toEqual({ enabled: true });

    const optedOut = defineActivity({
      name: "c",
      prompt: PROMPT,
      input: Input,
      output: Output,
      cache: { enabled: false },
    });
    expect(optedOut.cache).toEqual({ enabled: false });
  });

  it("throws ProviderSchemaError when the output type is not provider-safe", () => {
    // An open record (dict[str, number]) maps to an open map -> rejected.
    const OpenOut = z.object({ labels: z.record(z.string(), z.number()) });
    expect(() =>
      defineActivity({ name: "bad", prompt: PROMPT, input: Input, output: OpenOut }),
    ).toThrow(ProviderSchemaError);
  });

  it("runs the typed hook with input, output, and context", async () => {
    const activity = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      hook: (input, output, ctx) => {
        expectTypeOf(input.text).toBeString();
        expectTypeOf(output.label).toEqualTypeOf<"billing" | "support">();
        expectTypeOf(ctx).toEqualTypeOf<ActivityContext>();
        return { ...output, notes: `${ctx.tenant["company_id"]}:${input.text}` };
      },
    });
    const ctx: ActivityContext = { activityName: "classify", tenant: { company_id: "co-1" }, deps: null };
    const out = await activity.hook?.({ text: "hi" }, { label: "billing", score: 1 }, ctx);
    expect(out?.notes).toBe("co-1:hi");
  });

  it("accepts a 2-arg hook (context optional at the call site)", () => {
    const activity = defineActivity({
      name: "classify",
      prompt: PROMPT,
      input: Input,
      output: Output,
      hook: (_input, output) => output,
    });
    expect(activity.hook).toBeDefined();
  });

  it("a .nullish() output field round-trips a provider null through output.parse", () => {
    // toProviderSafe makes optionals required+nullable, so the provider may emit
    // null; .nullish() (unlike bare .optional()) accepts it back.
    const NullishOut = z.object({ label: z.string(), notes: z.string().nullish() });
    const activity = defineActivity({
      name: "n",
      prompt: PROMPT,
      input: Input,
      output: NullishOut,
    });
    expect(lintProviderSafe(activity.outputProviderSchema)).toEqual([]);
    expect(activity.output.parse({ label: "x", notes: null })).toEqual({ label: "x", notes: null });
  });

  it("input .default() fields are optional in the input schema (io: input)", () => {
    const WithDefault = z.object({ text: z.string(), lang: z.string().default("en") });
    const activity = defineActivity({
      name: "n",
      prompt: PROMPT,
      input: WithDefault,
      output: Output,
    });
    expect(activity.inputJsonSchema["required"]).toEqual(["text"]);
  });
});
