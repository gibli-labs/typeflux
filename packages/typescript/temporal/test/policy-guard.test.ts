/**
 * The executor's per-call policy HOOKS (#454): `providerModelGuard` (checked with
 * the fully-resolved model before the provider call) and `moderationPolicyBlock`
 * (verdict escalation at the output checkpoint). The policy semantics live in
 * @typeflux/temporal-yaml's RuntimePolicyGuard; here we test the core plumbing.
 */

import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, ModerationResult, StructuredCallParams } from "../src/index.js";
import { defineActivity, executeActivity, ModerationBlockedError, ProviderPolicyError } from "../src/index.js";

class FakeProvider implements ModelProvider {
  readonly providerName = "openai";
  calls = 0;
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    return { text: "ok" };
  }
}

const Output = z.object({ text: z.string() });
const messages = [{ role: "user", content: "go" }];
const activity = defineActivity({
  name: "draft",
  prompt: { name: "p/draft", label: "production" },
  input: z.object({ topic: z.string() }),
  output: Output,
});

describe("providerModelGuard (#454)", () => {
  it("sees the resolved model and lets an allowed call run", async () => {
    let seen: { providerName: string; model: string | undefined; activityName: string } | undefined;
    const provider = new FakeProvider();
    const out = await executeActivity(activity, { topic: "x" }, {
      provider,
      messages,
      model: "gpt-4o-mini",
      providerModelGuard: (call) => {
        seen = call;
      },
    });
    expect(out).toEqual({ text: "ok" });
    expect(seen).toMatchObject({ providerName: "openai", model: "gpt-4o-mini", activityName: "draft", promptName: "p/draft" });
    expect(provider.calls).toBe(1);
  });

  it("a guard throw blocks the call BEFORE the provider runs, normalized to ProviderPolicyError", async () => {
    const provider = new FakeProvider();
    await expect(
      executeActivity(activity, { topic: "x" }, {
        provider,
        messages,
        model: "gpt-4o",
        providerModelGuard: (call) => {
          throw new Error(`model ${call.model} not allowed`);
        },
      }),
    ).rejects.toBeInstanceOf(ProviderPolicyError);
    // The provider was never called — the guard runs before it.
    expect(provider.calls).toBe(0);
  });

  it("an already-ProviderPolicyError throw is passed through unwrapped", async () => {
    await expect(
      executeActivity(activity, { topic: "x" }, {
        provider: new FakeProvider(),
        messages,
        providerModelGuard: () => {
          throw new ProviderPolicyError("nope");
        },
      }),
    ).rejects.toThrow("nope");
  });
});

describe("moderationPolicyBlock verdict escalation (#454)", () => {
  const withModerator = (moderator: () => ModerationResult, onViolation: "block" | "flag") =>
    defineActivity({
      name: "draft",
      prompt: { name: "p/draft", label: "production" },
      input: z.object({ topic: z.string() }),
      output: Output,
      moderation: { moderator, onViolation },
    });

  it("escalates a policy block on an UNFLAGGED verdict, even with onViolation=flag", async () => {
    // A lenient moderator clears the output (flagged=false), but the policy blocks
    // the reported category — the policy can only tighten.
    const lenientButViolent = withModerator(() => ({ flagged: false, categories: ["violence"], maxScore: 0.9 }), "flag");
    await expect(
      executeActivity(lenientButViolent, { topic: "x" }, {
        provider: new FakeProvider(),
        messages,
        moderationPolicyBlock: ({ categories }) =>
          categories.includes("violence") ? "policy blocked: disallowed category violence" : undefined,
      }),
    ).rejects.toThrow(/disallowed category violence/);
  });

  it("passes when the policy does not escalate", async () => {
    const clean = withModerator(() => ({ flagged: false }), "block");
    const out = await executeActivity(clean, { topic: "x" }, {
      provider: new FakeProvider(),
      messages,
      moderationPolicyBlock: () => undefined,
    });
    expect(out).toEqual({ text: "ok" });
  });

  it("the escalation error is a ModerationBlockedError carrying the policy reason", async () => {
    const overThreshold = withModerator(() => ({ flagged: false, maxScore: 0.95 }), "flag");
    await expect(
      executeActivity(overThreshold, { topic: "x" }, {
        provider: new FakeProvider(),
        messages,
        moderationPolicyBlock: ({ maxScore }) =>
          maxScore !== undefined && maxScore >= 0.8 ? `score ${maxScore} >= threshold 0.8` : undefined,
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
  });
});
