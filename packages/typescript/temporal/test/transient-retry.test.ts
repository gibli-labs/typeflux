import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "../src/index.js";
import {
  defineActivity,
  executeActivity,
  ProviderRateLimitError,
  ProviderTransientError,
  retryAfterSecondsFrom,
} from "../src/index.js";

const activity = defineActivity({
  name: "classify",
  prompt: { name: "p/classify", label: "production" },
  input: z.object({ text: z.string() }),
  output: z.object({ label: z.string() }),
});

/** A provider whose calls resolve/reject from a queued script (error or value). */
function scriptedProvider(script: (Error | Record<string, unknown>)[]): {
  provider: ModelProvider;
  calls: () => number;
} {
  let calls = 0;
  const provider: ModelProvider = {
    structuredCall: (_params: StructuredCallParams) => {
      const next = script[calls];
      calls += 1;
      if (next instanceof Error) {
        return Promise.reject(next);
      }
      return Promise.resolve(next);
    },
  };
  return { provider, calls: () => calls };
}

const run = (provider: ModelProvider, extra: Record<string, unknown> = {}) =>
  executeActivity(activity, { text: "hi" }, {
    provider,
    messages: [{ role: "user", content: "Classify" }],
    ...extra,
  });

describe("executeActivity provider transient-retry (#430)", () => {
  it("retries a transient error then succeeds", async () => {
    const { provider, calls } = scriptedProvider([
      new ProviderTransientError("rate limited"),
      { label: "billing" },
    ]);
    const out = await run(provider, { transientRetries: 1 });
    expect(out).toEqual({ label: "billing" });
    expect(calls()).toBe(2);
  });

  it("rethrows once the transient budget is exhausted", async () => {
    const { provider, calls } = scriptedProvider([
      new ProviderTransientError("1"),
      new ProviderTransientError("2"),
      new ProviderTransientError("3"),
    ]);
    await expect(run(provider, { transientRetries: 2 })).rejects.toThrow(ProviderTransientError);
    expect(calls()).toBe(3); // initial + 2 retries
  });

  it("does NOT retry a non-transient error", async () => {
    const { provider, calls } = scriptedProvider([
      new Error("bad request"),
      { label: "never" },
    ]);
    await expect(run(provider, { transientRetries: 3 })).rejects.toThrow(/bad request/);
    expect(calls()).toBe(1);
  });

  it("does not retry by default (transientRetries unset)", async () => {
    const { provider, calls } = scriptedProvider([
      new ProviderTransientError("blip"),
      { label: "never" },
    ]);
    await expect(run(provider)).rejects.toThrow(ProviderTransientError);
    expect(calls()).toBe(1);
  });

  it("honors a custom isTransientError classifier", async () => {
    const { provider, calls } = scriptedProvider([
      new Error("HTTP 503"),
      { label: "ok" },
    ]);
    const out = await run(provider, {
      transientRetries: 1,
      isTransientError: (err: unknown) => err instanceof Error && err.message.includes("503"),
    });
    expect(out).toEqual({ label: "ok" });
    expect(calls()).toBe(2);
  });
});

describe("exponential transient backoff (#495 PR-B, Python ProviderRetrySpec)", () => {
  const Input = z.object({ text: z.string() });
  const Output = z.object({ label: z.string() });
  const activity = defineActivity({
    name: "classify",
    prompt: { name: "p/classify", label: "production" },
    input: Input,
    output: Output,
  });

  it("delays follow initial * multiplier^attempt capped at maxMs (jitter 0 = deterministic)", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls <= 3) {
            throw new ProviderTransientError(`blip ${calls}`);
          }
          return { label: "ok" };
        },
      };
      const pending = executeActivity(activity, { text: "hi" }, {
        provider,
        messages: [{ role: "user", content: "go" }],
        transientRetries: 3,
        transientBackoff: { initialMs: 100, multiplier: 2, maxMs: 300, jitterRatio: 0 },
      });
      // Schedule: attempt0 fail -> 100ms, attempt1 fail -> 200ms, attempt2 fail -> min(400,300)=300ms.
      await vi.advanceTimersByTimeAsync(100);
      expect(calls).toBe(2);
      await vi.advanceTimersByTimeAsync(200);
      expect(calls).toBe(3);
      await vi.advanceTimersByTimeAsync(300);
      await expect(pending).resolves.toEqual({ label: "ok" });
      expect(calls).toBe(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it("jitter stays within ±jitterRatio and backoff wins over transientRetryDelayMs", async () => {
    vi.useFakeTimers();
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(1); // max positive jitter
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            throw new ProviderTransientError("blip");
          }
          return { label: "ok" };
        },
      };
      const pending = executeActivity(activity, { text: "hi" }, {
        provider,
        messages: [{ role: "user", content: "go" }],
        transientRetries: 1,
        transientRetryDelayMs: 9999, // must LOSE to the backoff config
        transientBackoff: { initialMs: 100, multiplier: 2, jitterRatio: 0.1 },
      });
      // POSITIVE-ONLY jitter (Python floor semantics): delay = 100 * (1 + 0.1*1) = 110ms.
      await vi.advanceTimersByTimeAsync(109);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
      expect(calls).toBe(2);
    } finally {
      randomSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("jitter never retries SOONER than the configured backoff (random 0 -> exactly raw)", async () => {
    vi.useFakeTimers();
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(0);
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            throw new ProviderTransientError("blip");
          }
          return { label: "ok" };
        },
      };
      const pending = executeActivity(activity, { text: "hi" }, {
        provider,
        messages: [{ role: "user", content: "go" }],
        transientRetries: 1,
        transientBackoff: { initialMs: 100, multiplier: 2, jitterRatio: 0.5 },
      });
      // The configured backoff is a FLOOR: random 0 gives exactly 100ms, never less.
      await vi.advanceTimersByTimeAsync(99);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
    } finally {
      randomSpy.mockRestore();
      vi.useRealTimers();
    }
  });
});

describe("rate-limit retry selection + Retry-After floor (#529, Python ProviderRetryPolicy)", () => {
  it("retryRateLimits: false rethrows a rate-limit error immediately; plain transient still retries", async () => {
    const rateLimited = scriptedProvider([new ProviderRateLimitError("429"), { label: "never" }]);
    await expect(
      run(rateLimited.provider, { transientRetries: 3, retryRateLimits: false }),
    ).rejects.toThrow(ProviderRateLimitError);
    expect(rateLimited.calls()).toBe(1);

    const transient = scriptedProvider([new ProviderTransientError("503"), { label: "ok" }]);
    const out = await run(transient.provider, { transientRetries: 3, retryRateLimits: false });
    expect(out).toEqual({ label: "ok" });
    expect(transient.calls()).toBe(2);
  });

  it("retryTransientErrors: false rethrows plain transient immediately; a rate limit still retries", async () => {
    const transient = scriptedProvider([new ProviderTransientError("503"), { label: "never" }]);
    await expect(
      run(transient.provider, { transientRetries: 3, retryTransientErrors: false }),
    ).rejects.toThrow(ProviderTransientError);
    expect(transient.calls()).toBe(1);

    const rateLimited = scriptedProvider([new ProviderRateLimitError("429"), { label: "ok" }]);
    const out = await run(rateLimited.provider, { transientRetries: 3, retryTransientErrors: false });
    expect(out).toEqual({ label: "ok" });
    expect(rateLimited.calls()).toBe(2);
  });

  it("defaults retry BOTH classes (both booleans unset)", async () => {
    const { provider, calls } = scriptedProvider([
      new ProviderRateLimitError("429"),
      new ProviderTransientError("503"),
      { label: "ok" },
    ]);
    const out = await run(provider, { transientRetries: 2 });
    expect(out).toEqual({ label: "ok" });
    expect(calls()).toBe(3);
  });

  it("Retry-After FLOORS the backoff and is never capped by maxMs (server truth)", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            // Hint (1s) far above both initialMs (100) and the maxMs cap (200).
            throw new ProviderRateLimitError("429", { retryAfterSeconds: 1 });
          }
          return { label: "ok" };
        },
      };
      const pending = run(provider, {
        transientRetries: 1,
        transientBackoff: { initialMs: 100, multiplier: 2, maxMs: 200, jitterRatio: 0 },
      });
      await vi.advanceTimersByTimeAsync(999);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
      expect(calls).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a hint SMALLER than the backoff loses (the backoff is a floor too)", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            throw new ProviderRateLimitError("429", { retryAfterSeconds: 0.05 });
          }
          return { label: "ok" };
        },
      };
      const pending = run(provider, {
        transientRetries: 1,
        transientBackoff: { initialMs: 100, multiplier: 2, jitterRatio: 0 },
      });
      await vi.advanceTimersByTimeAsync(99);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("jitter extends the floored hint (Python: jitter applies to the whole delay)", async () => {
    vi.useFakeTimers();
    const randomSpy = vi.spyOn(Math, "random").mockReturnValue(1);
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            throw new ProviderRateLimitError("429", { retryAfterSeconds: 0.5 });
          }
          return { label: "ok" };
        },
      };
      const pending = run(provider, {
        transientRetries: 1,
        transientBackoff: { initialMs: 100, multiplier: 2, jitterRatio: 0.1 },
      });
      // delay = max(100, 500) * (1 + 0.1*1) = 550ms.
      await vi.advanceTimersByTimeAsync(549);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
    } finally {
      randomSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("a PLAIN transient error's hint floors the backoff too (Python forwards it for both classes)", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            // A 503 "overloaded" can carry Retry-After — not just 429s.
            throw new ProviderTransientError("503", { retryAfterSeconds: 0.5 });
          }
          return { label: "ok" };
        },
      };
      const pending = run(provider, {
        transientRetries: 1,
        transientBackoff: { initialMs: 100, multiplier: 2, jitterRatio: 0 },
      });
      await vi.advanceTimersByTimeAsync(499);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("the hint also floors the fixed transientRetryDelayMs path", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const provider: ModelProvider = {
        structuredCall: () => {
          calls += 1;
          if (calls === 1) {
            throw new ProviderRateLimitError("429", { retryAfterSeconds: 0.3 });
          }
          return { label: "ok" };
        },
      };
      const pending = run(provider, { transientRetries: 1, transientRetryDelayMs: 10 });
      await vi.advanceTimersByTimeAsync(299);
      expect(calls).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toEqual({ label: "ok" });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("retryAfterSecondsFrom (#529, Python _shared.retry_after_seconds)", () => {
  it("numeric retryAfter/retryAfterSeconds attributes win", () => {
    expect(retryAfterSecondsFrom({ retryAfter: 7 })).toBe(7);
    expect(retryAfterSecondsFrom({ retryAfterSeconds: 2.5 })).toBe(2.5);
    // Attribute beats the header (probe order parity).
    expect(
      retryAfterSecondsFrom({ retryAfter: 3, response: { headers: new Map([["retry-after", "9"]]) } }),
    ).toBe(3);
  });

  it("falls back to the retry-after header — Headers-like .get() or a plain record", () => {
    const headersLike = { get: (name: string) => (name === "retry-after" ? "12" : null) };
    expect(retryAfterSecondsFrom({ response: { headers: headersLike } })).toBe(12);
    expect(retryAfterSecondsFrom({ response: { headers: { "retry-after": "0.25" } } })).toBe(0.25);
  });

  it("reads TOP-LEVEL error.headers (OpenAI/Anthropic JS SDK APIError shape) and Retry-After casing", () => {
    // openai-node/anthropic-sdk-typescript expose headers directly on the error.
    expect(retryAfterSecondsFrom({ status: 429, headers: new Map([["retry-after", "30"]]) })).toBe(30);
    expect(retryAfterSecondsFrom({ status: 429, headers: { "Retry-After": "5" } })).toBe(5);
    // A dud top-level source doesn't mask a valid response.headers one.
    expect(
      retryAfterSecondsFrom({ headers: {}, response: { headers: { "retry-after": "8" } } }),
    ).toBe(8);
  });

  it("negative, unparsable, empty, and absent values are undefined", () => {
    expect(retryAfterSecondsFrom({ retryAfter: -1 })).toBeUndefined();
    expect(retryAfterSecondsFrom({ response: { headers: { "retry-after": "soon" } } })).toBeUndefined();
    expect(retryAfterSecondsFrom({ response: { headers: { "retry-after": "" } } })).toBeUndefined();
    expect(retryAfterSecondsFrom({ response: { headers: {} } })).toBeUndefined();
    expect(retryAfterSecondsFrom(new Error("no shape"))).toBeUndefined();
    expect(retryAfterSecondsFrom(null)).toBeUndefined();
    // Python float() parity: whitespace-only and hex are rejected (Number() would
    // coerce them to 0 and 16); scientific notation is accepted.
    expect(retryAfterSecondsFrom({ response: { headers: { "retry-after": "   " } } })).toBeUndefined();
    expect(retryAfterSecondsFrom({ response: { headers: { "retry-after": "0x10" } } })).toBeUndefined();
    expect(retryAfterSecondsFrom({ response: { headers: { "retry-after": "1e2" } } })).toBe(100);
    // A throwing exotic headers object degrades to undefined, never throws.
    const throwing = {
      get: () => {
        throw new Error("exotic");
      },
    };
    expect(retryAfterSecondsFrom({ response: { headers: throwing } })).toBeUndefined();
  });
});
