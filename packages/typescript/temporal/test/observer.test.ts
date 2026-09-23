import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "../src/index.js";
import { CollectingObserver, defineActivity, executeActivity, InMemoryCacheStore } from "../src/index.js";

class FakeProvider implements ModelProvider {
  calls = 0;
  constructor(private readonly responses: unknown[]) {}
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    if (this.responses.length === 0) {
      throw new Error("FakeProvider exhausted");
    }
    const response = this.responses.shift();
    if (response instanceof Error) {
      throw response;
    }
    return response;
  }
}

const messages = [{ role: "user", content: "go" }];

function activity(extra: Record<string, unknown> = {}) {
  return defineActivity({
    name: "summarize",
    prompt: { name: "p/summarize", label: "production" },
    input: z.object({ text: z.string() }),
    output: z.object({ summary: z.string() }),
    ...extra,
  });
}

describe("CollectingObserver via executeActivity (#451)", () => {
  it("records a successful execution: activity span + one generation + output", async () => {
    const observer = new CollectingObserver();
    const out = await executeActivity(activity(), { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer,
    });

    expect(out).toEqual({ summary: "ok" });
    expect(observer.activities).toHaveLength(1);
    const record = observer.activities[0]!;
    expect(record.activityName).toBe("summarize");
    expect(record.input).toEqual({ text: "x" });
    expect(record.model).toBe(null);
    expect(record.output).toEqual({ summary: "ok" });
    expect(record.error).toBeUndefined();
    expect(record.ended).toBe(true);
    expect(record.generations).toHaveLength(1);
    expect(record.generations[0]).toMatchObject({ attempt: 0, output: { summary: "ok" } });
    expect(record.hooks).toHaveLength(0); // no hook on this activity
    expect(record.metadata).toMatchObject({ cacheHit: false }); // fresh (non-cache) execution
  });

  it("records a hook span for a hooked activity", async () => {
    const observer = new CollectingObserver();
    const hooked = activity({ hook: (_input: unknown, output: { summary: string }) => ({ summary: output.summary.toUpperCase() }) });
    const out = await executeActivity(hooked, { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer,
    });
    expect(out).toEqual({ summary: "OK" });
    const record = observer.activities[0]!;
    expect(record.hooks).toHaveLength(1);
    expect(record.hooks[0]).toMatchObject({ input: { text: "x" }, output: { summary: "OK" } });
  });

  it("records a provider error on both the generation and the activity", async () => {
    const observer = new CollectingObserver();
    const boom = new Error("provider boom");
    await expect(
      executeActivity(activity(), { text: "x" }, {
        provider: new FakeProvider([boom]),
        messages,
        observer,
      }),
    ).rejects.toThrow(/provider boom/);

    const record = observer.activities[0]!;
    expect(record.error).toBe(boom);
    expect(record.ended).toBe(true);
    expect(record.generations).toHaveLength(1);
    expect(record.generations[0]!.error).toBe(boom);
    expect(record.output).toBeUndefined();
  });

  it("records one generation per validation-repair attempt", async () => {
    const observer = new CollectingObserver();
    // First response fails output validation (missing `summary`), second succeeds.
    const out = await executeActivity(activity({ validationRetries: 1 }), { text: "x" }, {
      provider: new FakeProvider([{ wrong: 1 }, { summary: "fixed" }]),
      messages,
      observer,
    });

    expect(out).toEqual({ summary: "fixed" });
    const record = observer.activities[0]!;
    expect(record.generations).toHaveLength(2);
    expect(record.generations[0]!.error).toBeDefined(); // attempt 0: validation error
    expect(record.generations[0]!.output).toBeUndefined();
    expect(record.generations[1]).toMatchObject({ attempt: 1, output: { summary: "fixed" } });
    expect(record.output).toEqual({ summary: "fixed" });
  });

  it("records a cache hit with no generation", async () => {
    const cacheStore = new InMemoryCacheStore();
    const cached = activity({ cache: { enabled: true } });
    const provider = new FakeProvider([{ summary: "cached" }]);

    // First run populates the cache (records a generation).
    const first = new CollectingObserver();
    await executeActivity(cached, { text: "same" }, { provider, messages, observer: first, cacheStore });
    expect(first.activities[0]!.generations).toHaveLength(1);

    // Second run hits the cache: no generation, cacheHit metadata.
    const second = new CollectingObserver();
    const out = await executeActivity(cached, { text: "same" }, { provider, messages, observer: second, cacheStore });
    expect(out).toEqual({ summary: "cached" });
    expect(provider.calls).toBe(1); // provider not called again
    const record = second.activities[0]!;
    expect(record.generations).toHaveLength(0);
    expect(record.metadata).toMatchObject({ cacheHit: true });
    expect(record.output).toEqual({ summary: "cached" });
    expect(record.ended).toBe(true);
  });
});
