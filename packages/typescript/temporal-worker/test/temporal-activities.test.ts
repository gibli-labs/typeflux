import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { defineActivity } from "@typeflux/temporal";

import { buildTemporalActivities, buildTemporalActivity } from "../src/index.js";

class FakeProvider implements ModelProvider {
  calls = 0;
  constructor(private readonly responses: unknown[]) {}
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    if (this.responses.length === 0) {
      throw new Error("FakeProvider exhausted");
    }
    return this.responses.shift();
  }
}

const messages = [{ role: "user", content: "go" }];

function makeActivity(name: string) {
  return defineActivity({
    name,
    prompt: { name: `p/${name}`, label: "production" },
    input: z.object({ text: z.string() }),
    output: z.object({ summary: z.string() }),
  });
}

describe("buildTemporalActivities (#450)", () => {
  it("assembles the { [name]: fn } activities map and runs each via executeActivity", async () => {
    const a = makeActivity("alpha");
    const b = makeActivity("beta");
    const activities = buildTemporalActivities([
      { descriptor: a, options: { provider: new FakeProvider([{ summary: "A" }]), messages } },
      { descriptor: b, options: { provider: new FakeProvider([{ summary: "B" }]), messages } },
    ]);

    expect(Object.keys(activities).sort()).toEqual(["alpha", "beta"]);
    expect(await activities["alpha"]!({ text: "x" })).toEqual({ summary: "A" });
    expect(await activities["beta"]!({ text: "y" })).toEqual({ summary: "B" });
  });

  it("registers descriptors that define a context-aware hook (no type error)", async () => {
    // A concretely-typed hooked descriptor must be assignable to the registration —
    // these context-aware hooks are exactly what the map exists to register.
    const hooked = defineActivity({
      name: "hooked",
      prompt: { name: "p/hooked", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string() }),
      hook: (_input, output, c) => ({ summary: `${output.summary}|${c.activityName}` }),
    });
    const activities = buildTemporalActivities([
      { descriptor: hooked, options: { provider: new FakeProvider([{ summary: "H" }]), messages } },
    ]);
    expect(await activities["hooked"]!({ text: "x" })).toEqual({ summary: "H|hooked" });
  });

  it("handles a '__proto__' activity name as an ordinary key (no silent drop)", () => {
    const activities = buildTemporalActivities([
      { descriptor: makeActivity("__proto__"), options: { provider: new FakeProvider([{ summary: "P" }]), messages } },
    ]);
    expect(Object.keys(activities)).toContain("__proto__"); // registered, not dropped
    expect(() =>
      buildTemporalActivities([
        { descriptor: makeActivity("__proto__"), options: { provider: new FakeProvider([]), messages } },
        { descriptor: makeActivity("__proto__"), options: { provider: new FakeProvider([]), messages } },
      ]),
    ).toThrow(/duplicate activity name/); // dedupe still fires
  });

  it("throws on a duplicate activity name (Temporal registers by name)", () => {
    const a = makeActivity("dup");
    const b = makeActivity("dup");
    expect(() =>
      buildTemporalActivities([
        { descriptor: a, options: { provider: new FakeProvider([]), messages } },
        { descriptor: b, options: { provider: new FakeProvider([]), messages } },
      ]),
    ).toThrow(/duplicate activity name: "dup"/);
  });
});

describe("buildTemporalActivity context enrichment (#450)", () => {
  it("threads the contextProvider's fields into the hook's ActivityContext", async () => {
    const activity = defineActivity({
      name: "ctx",
      prompt: { name: "p/ctx", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string() }),
      // The hook folds the durable Temporal context into the output, proving it arrived.
      hook: (_input, output, c) => ({ summary: `${output.summary}|${c.workflowId ?? "-"}|${c.attempt ?? "-"}` }),
    });
    const fn = buildTemporalActivity(activity, { provider: new FakeProvider([{ summary: "base" }]), messages }, {
      contextProvider: () => ({ workflowId: "wf-9", runId: "run-9", attempt: 2, taskQueue: "q", activityId: "a" }),
    });
    expect(await fn({ text: "x" })).toEqual({ summary: "base|wf-9|2" });
  });

  it("runs unchanged when the contextProvider yields undefined (standalone)", async () => {
    const activity = defineActivity({
      name: "ctx2",
      prompt: { name: "p/ctx2", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string() }),
      hook: (_input, output, c) => ({ summary: `${output.summary}|${c.workflowId ?? "none"}` }),
    });
    const fn = buildTemporalActivity(activity, { provider: new FakeProvider([{ summary: "base" }]), messages }, {
      contextProvider: () => undefined,
    });
    expect(await fn({ text: "x" })).toEqual({ summary: "base|none" });
  });
});
