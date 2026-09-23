import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, ModerationResult, StructuredCallParams } from "../src/index.js";
import {
  CollectingObserver,
  defineActivity,
  executeActivity,
  InMemoryCacheStore,
  ModerationBlockedError,
} from "../src/index.js";

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

const Output = z.object({ text: z.string() });
const messages = [{ role: "user", content: "go" }];

function activityWith(
  moderator: (o: { text: string }) => ModerationResult | Promise<ModerationResult>,
  onViolation: "block" | "flag" = "block",
  extra: Record<string, unknown> = {},
) {
  return defineActivity({
    name: "draft",
    prompt: { name: "p/draft", label: "production" },
    input: z.object({ topic: z.string() }),
    output: Output,
    moderation: { moderator, onViolation },
    ...extra,
  });
}

const clean = (): ModerationResult => ({ flagged: false });
const flag = (): ModerationResult => ({ flagged: true, categories: ["policy"] });

describe("moderation checkpoint (#453)", () => {
  it("passes a clean verdict through", async () => {
    const activity = activityWith(clean);
    const out = await executeActivity(activity, { topic: "x" }, {
      provider: new FakeProvider([{ text: "ok" }]),
      messages,
    });
    expect(out).toEqual({ text: "ok" });
  });

  it("blocks a flagged verdict (on_violation=block) with ModerationBlockedError", async () => {
    const activity = activityWith(flag, "block");
    await expect(
      executeActivity(activity, { topic: "x" }, {
        provider: new FakeProvider([{ text: "bad" }]),
        messages,
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
  });

  it("lets a flagged verdict through when on_violation=flag", async () => {
    const activity = activityWith(flag, "flag");
    const out = await executeActivity(activity, { topic: "x" }, {
      provider: new FakeProvider([{ text: "borderline" }]),
      messages,
    });
    expect(out).toEqual({ text: "borderline" });
  });

  it("awaits an async moderator", async () => {
    const activity = activityWith(async () => {
      await Promise.resolve();
      return { flagged: true };
    }, "block");
    await expect(
      executeActivity(activity, { topic: "x" }, {
        provider: new FakeProvider([{ text: "bad" }]),
        messages,
      }),
    ).rejects.toThrow(/moderation blocked/);
  });

  it("moderates the hook's output (validate -> hook -> moderate)", async () => {
    // The hook rewrites text to a banned value; moderation sees the post-hook output.
    const activity = defineActivity({
      name: "draft",
      prompt: { name: "p/draft", label: "production" },
      input: z.object({ topic: z.string() }),
      output: Output,
      hook: (_input, output) => ({ ...output, text: `${output.text}-edited` }),
      moderation: { moderator: (o) => ({ flagged: o.text.endsWith("-edited") }), onViolation: "block" },
    });
    await expect(
      executeActivity(activity, { topic: "x" }, {
        provider: new FakeProvider([{ text: "draft" }]),
        messages,
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
  });

  it("fails closed: an unknown on_violation action blocks a flagged verdict", async () => {
    // A typo from untyped/dynamic config must not silently bypass the gate.
    const activity = activityWith(flag, "blok" as "block");
    await expect(
      executeActivity(activity, { topic: "x" }, {
        provider: new FakeProvider([{ text: "bad" }]),
        messages,
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
  });

  it("still moderates an output served from cache", async () => {
    // First run: moderator passes, output is cached.
    const cache = new InMemoryCacheStore();
    let block = false;
    const activity = activityWith((o) => ({ flagged: block }), "block", {
      cache: { enabled: true },
    });
    const provider = new FakeProvider([{ text: "cached" }]);

    const first = await executeActivity(activity, { topic: "x" }, { provider, messages, cacheStore: cache });
    expect(first).toEqual({ text: "cached" });
    expect(provider.calls).toBe(1);

    // Second run: cache HIT (provider not called again) but the moderator now blocks
    // — the cached output must still be gated.
    block = true;
    await expect(
      executeActivity(activity, { topic: "x" }, { provider, messages, cacheStore: cache }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
    expect(provider.calls).toBe(1); // served from cache, yet still blocked
  });
});

describe("moderation verdict trace record (#454 — Python _record_moderation_verdict)", () => {
  const verdictOf = (observer: CollectingObserver) => observer.activities[0]?.metadata["typeflux_moderation"];

  it("records an ALLOW verdict with the moderator name, categories, and score", async () => {
    const observer = new CollectingObserver();
    const safe = (): ModerationResult => ({ flagged: false, categories: [], maxScore: 0.1 });
    const out = await executeActivity(activityWith(safe), { topic: "x" }, {
      provider: new FakeProvider([{ text: "ok" }]),
      messages,
      observer,
    });
    expect(out).toEqual({ text: "ok" });
    expect(verdictOf(observer)).toEqual({ decision: "allow", categories: [], max_score: 0.1, moderator: "safe" });
  });

  it("records a FLAG verdict when on_violation=flag lets the output through", async () => {
    const observer = new CollectingObserver();
    await executeActivity(activityWith(flag, "flag"), { topic: "x" }, {
      provider: new FakeProvider([{ text: "borderline" }]),
      messages,
      observer,
    });
    // An unscored moderator records max_score: null (JSON-stable, present in the trace).
    expect(verdictOf(observer)).toEqual({ decision: "flag", categories: ["policy"], max_score: null, moderator: "flag" });
  });

  it("records a BLOCK verdict on the trace even though the activity throws (recorded before the raise)", async () => {
    const observer = new CollectingObserver();
    await expect(
      executeActivity(activityWith(flag, "block"), { topic: "x" }, {
        provider: new FakeProvider([{ text: "bad" }]),
        messages,
        observer,
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
    // The verdict landed on the trace despite the block — audit evidence survives.
    expect(verdictOf(observer)).toMatchObject({ decision: "block", categories: ["policy"], moderator: "flag" });
  });

  it("a POLICY escalation records decision=block even when the moderator did not flag", async () => {
    const observer = new CollectingObserver();
    const lenient = (): ModerationResult => ({ flagged: false, categories: ["violence"], maxScore: 0.9 });
    await expect(
      executeActivity(activityWith(lenient, "flag"), { topic: "x" }, {
        provider: new FakeProvider([{ text: "bad" }]),
        messages,
        observer,
        moderationPolicyBlock: ({ categories }) => (categories.includes("violence") ? "disallowed category violence" : undefined),
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
    expect(verdictOf(observer)).toEqual({
      decision: "block",
      categories: ["violence"],
      max_score: 0.9,
      moderator: "lenient",
    });
  });
});
