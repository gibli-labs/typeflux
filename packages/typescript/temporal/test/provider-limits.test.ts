// Fake timers interact badly with the limiter's real-time monotonic measurements,
// so these tests use real (short) delays instead.
import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider } from "../src/index.js";
import {
  CollectingObserver,
  defineActivity,
  executeActivity,
  ProviderCallLimiter,
  ProviderRateLimitController,
  selectProviderPolicy,
} from "../src/index.js";

const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

describe("ProviderCallLimiter (#529 PR B, Python ProviderCallLimiter)", () => {
  it("maxConcurrent bounds overlap and reports queued time", async () => {
    const limiter = new ProviderCallLimiter({ maxConcurrent: 2 });
    let active = 0;
    let peak = 0;
    const waits: number[] = [];
    await Promise.all(
      Array.from({ length: 6 }, () =>
        limiter.limit(async (wait) => {
          waits.push(wait.queuedSeconds);
          active += 1;
          peak = Math.max(peak, active);
          await delay(10);
          active -= 1;
        }),
      ),
    );
    expect(peak).toBe(2);
    // The first two are admitted immediately; later calls queued measurably.
    expect(waits.filter((w) => w >= 0.005).length).toBeGreaterThanOrEqual(2);
  });

  it("minIntervalSeconds spaces admissions and reports throttled time", async () => {
    const limiter = new ProviderCallLimiter({ minIntervalSeconds: 0.02 });
    // Assert on the throttle the limiter *reserves* for each admission, not on
    // observed wall-clock timestamps. The reservation math (`reserveRateSlot`)
    // runs synchronously when the call is admitted, so each successive call is
    // spaced one interval past the previous reservation regardless of when its
    // sleep timer later fires. Observing `performance.now()` inside `fn` was
    // flaky: when a starved event loop (full-workspace `pnpm -r test` load)
    // stalls past two reserved deadlines, both setTimeouts fire in one tick and
    // the recorded admission times collapse to ~0ms apart — a false failure.
    // The reserved throttle can never collapse that way.
    const throttles: number[] = [];
    const admittedAt: number[] = [];
    // Anchor for the real-sleep assertion below, captured SYNCHRONOUSLY before
    // scheduling: every reservation happens in this same synchronous tick (each
    // `limit()` call runs its reservation math before its first real
    // suspension), so `start` precedes every reserved deadline. Anchoring on the
    // first ADMISSION instead would reintroduce the flake — the first admission
    // is a microtask, and a starvation window that opens before it and outlasts
    // the last timer deadline collapses first-to-last elapsed to ~0 exactly
    // like pairwise spacing.
    const start = performance.now();
    await Promise.all(
      Array.from({ length: 3 }, () =>
        limiter.limit(async (wait) => {
          throttles.push(wait.throttledSeconds);
          admittedAt.push(performance.now());
        }),
      ),
    );
    throttles.sort((a, b) => a - b);
    // First admission is free; the next two reserve ~1x and ~2x the interval.
    // Bands (not exact 0.02/0.04) absorb the sub-millisecond monotonic-clock
    // drift between the three synchronous reservations — but every band sits
    // well clear of 0, which is the property that broke under load.
    expect(throttles[0]!).toBe(0);
    expect(throttles[1]!).toBeGreaterThan(0.012);
    expect(throttles[1]!).toBeLessThanOrEqual(0.02 + 1e-9);
    expect(throttles[2]!).toBeGreaterThan(0.025);
    expect(throttles[2]!).toBeLessThanOrEqual(0.04 + 1e-9);
    // The sleep path genuinely RAN: the last admission cannot land before its
    // reserved deadline, so wall-clock elapsed from `start` must cover the
    // largest reserved throttle. Load-immune — timers never fire early, and
    // event-loop starvation only ever pushes the last admission LATER, so this
    // bound cannot collapse the way pairwise spacing does. It fails if a
    // refactor admits `fn` without actually awaiting the throttle sleep. The
    // 5ms epsilon covers timer-granularity truncation of the fractional
    // setTimeout delay plus the sub-ms gap between `start` and the reservations.
    const elapsedMs = Math.max(...admittedAt) - start;
    expect(elapsedMs).toBeGreaterThanOrEqual(Math.max(...throttles) * 1000 - 5);
  });

  it("releases the slot when fn throws", async () => {
    const limiter = new ProviderCallLimiter({ maxConcurrent: 1 });
    await expect(limiter.limit(async () => Promise.reject(new Error("boom")))).rejects.toThrow("boom");
    // A leaked permit would deadlock this second call.
    await expect(limiter.limit(async () => "ok")).resolves.toBe("ok");
  });

  it("rejects invalid limits loudly", () => {
    expect(() => new ProviderCallLimiter({ maxConcurrent: 0 })).toThrow(/maxConcurrent/);
    expect(() => new ProviderCallLimiter({ minIntervalSeconds: -1 })).toThrow(/minIntervalSeconds/);
  });

  it("an abort while QUEUED rejects promptly and passes the permit on (codex P2)", async () => {
    const limiter = new ProviderCallLimiter({ maxConcurrent: 1 });
    let releaseFirst!: () => void;
    const first = limiter.limit(async () => new Promise<void>((resolve) => (releaseFirst = resolve)));
    const controller = new AbortController();
    const reason = new Error("workflow cancelled");
    const queued = limiter.limit(async () => "never", { signal: controller.signal });
    await delay(5);
    controller.abort(reason);
    await expect(queued).rejects.toBe(reason);
    // The cancelled waiter must not consume the hand-off: a live waiter still runs.
    const third = limiter.limit(async () => "ok");
    releaseFirst();
    await first;
    await expect(third).resolves.toBe("ok");
  });

  it("an abort during the THROTTLE sleep rejects with the reason and frees the permit (codex P2)", async () => {
    const limiter = new ProviderCallLimiter({ maxConcurrent: 1, minIntervalSeconds: 0.05 });
    await limiter.limit(async () => "first"); // consumes the interval slot
    const controller = new AbortController();
    const reason = new Error("cancelled during throttle");
    const throttled = limiter.limit(async () => "never", { signal: controller.signal });
    await delay(5); // inside the ~50ms throttle sleep
    controller.abort(reason);
    await expect(throttled).rejects.toBe(reason);
    // The permit held during the throttle sleep was released, not leaked — a
    // leaked permit would deadlock this follower forever.
    await expect(limiter.limit(async () => "ran")).resolves.toBe("ran");
  });
});

describe("selectProviderPolicy (#529, Python ProviderRateLimitPolicy.select)", () => {
  const policy = {
    default: { maxConcurrent: 8 },
    providers: {
      openai: { limits: { maxConcurrent: 4 }, models: { "gpt-4o": { maxConcurrent: 1 } } },
      gemini: { models: { "gemini-2.5-flash": { minIntervalSeconds: 0.1 } } },
    },
  };

  it("model > provider > default > none, with Python's policy keys", () => {
    expect(selectProviderPolicy(policy, { providerName: "openai", providerModel: "gpt-4o" })).toMatchObject({
      policySource: "model",
      policyKey: "provider:openai/model:gpt-4o",
      limits: { maxConcurrent: 1 },
    });
    expect(selectProviderPolicy(policy, { providerName: "openai", providerModel: "gpt-4.1" })).toMatchObject({
      policySource: "provider",
      policyKey: "provider:openai",
      limits: { maxConcurrent: 4 },
    });
    // A models-only provider entry falls through to default for other models.
    expect(selectProviderPolicy(policy, { providerName: "gemini", providerModel: "gemini-2.5-pro" })).toMatchObject({
      policySource: "default",
      policyKey: "default",
    });
    expect(selectProviderPolicy({}, { providerName: "anthropic" })).toMatchObject({
      policySource: "none",
      policyKey: "none:anthropic:*",
      limits: undefined,
    });
  });

  it("the controller shares ONE limiter per policy key and none for unlimited selections", () => {
    const controller = new ProviderRateLimitController(policy);
    const a = controller.limiterFor(controller.select({ providerName: "openai", providerModel: "gpt-4o" }));
    const b = controller.limiterFor(controller.select({ providerName: "openai", providerModel: "gpt-4o" }));
    expect(a).toBeDefined();
    expect(a).toBe(b); // same key -> the SAME semaphore, or the quota multiplies
    expect(controller.limiterFor(selectProviderPolicy({}, { providerName: "x" }))).toBeUndefined();
    // Present-but-empty limits are "no limiter", not a zero-limit lock.
    const empty = new ProviderRateLimitController({ default: {} });
    expect(empty.limiterFor(empty.select({ providerName: "x" }))).toBeUndefined();
  });

  it("the controller validates every tier at construction", () => {
    expect(() => new ProviderRateLimitController({ default: { maxConcurrent: 0 } })).toThrow(/maxConcurrent/);
    expect(
      () => new ProviderRateLimitController({ providers: { x: { models: { m: { minIntervalSeconds: -1 } } } } }),
    ).toThrow(/minIntervalSeconds/);
  });
});

describe("executeActivity provider-limits admission (#529 PR B)", () => {
  const activity = defineActivity({
    name: "classify",
    prompt: { name: "p/classify", label: "production" },
    input: z.object({ text: z.string() }),
    output: z.object({ label: z.string() }),
  });

  it("holds one slot per execution and records providerControls on the observation", async () => {
    let active = 0;
    let peak = 0;
    const provider: ModelProvider = {
      providerName: "fake",
      structuredCall: async () => {
        active += 1;
        peak = Math.max(peak, active);
        await delay(10);
        active -= 1;
        return { label: "ok" };
      },
    } as ModelProvider;
    const controller = new ProviderRateLimitController({ providers: { fake: { limits: { maxConcurrent: 1 } } } });
    const observer = new CollectingObserver();
    const run = () =>
      executeActivity(activity, { text: "hi" }, {
        provider,
        messages: [{ role: "user", content: "go" }],
        providerLimitController: controller,
        observer,
      });
    await Promise.all([run(), run(), run()]);
    expect(peak).toBe(1);
    const controls = observer.activities.map(
      (a) => (a.metadata as Record<string, Record<string, unknown>>)["typeflux.provider_controls"],
    );
    expect(controls).toHaveLength(3);
    for (const entry of controls) {
      expect(entry).toMatchObject({ policy_source: "provider", policy_key: "provider:fake", max_concurrent: 1 });
    }
    // The queued executions record a measurable wait; at least one was admitted instantly.
    expect(controls.filter((entry) => entry?.["queued"] === true).length).toBeGreaterThanOrEqual(1);
  });

  it("a cancel while QUEUED surfaces as cooperative cancellation, not a raw abort (codex P2)", async () => {
    let releaseFirst!: () => void;
    const provider: ModelProvider = {
      providerName: "fake",
      structuredCall: async () => {
        await new Promise<void>((resolve) => (releaseFirst = resolve));
        return { label: "ok" };
      },
    } as ModelProvider;
    const controller = new ProviderRateLimitController({ providers: { fake: { limits: { maxConcurrent: 1 } } } });
    const first = executeActivity(activity, { text: "a" }, {
      provider,
      messages: [{ role: "user", content: "go" }],
      providerLimitController: controller,
    });
    await delay(5); // first holds the permit inside its provider call
    const abort = new AbortController();
    const queued = executeActivity(activity, { text: "b" }, {
      provider,
      messages: [{ role: "user", content: "go" }],
      providerLimitController: controller,
      cancellationSignal: abort.signal,
    });
    await delay(5);
    abort.abort(new Error("workflow cancelled"));
    await expect(queued).rejects.toMatchObject({ name: "ActivityCancelledError" });
    releaseFirst();
    await expect(first).resolves.toEqual({ label: "ok" });
  });

  it("an unlimited selection runs unthrottled but still records the policy decision", async () => {
    const provider: ModelProvider = {
      providerName: "fake",
      structuredCall: async () => ({ label: "ok" }),
    } as ModelProvider;
    const observer = new CollectingObserver();
    await executeActivity(activity, { text: "hi" }, {
      provider,
      messages: [{ role: "user", content: "go" }],
      providerLimitController: new ProviderRateLimitController({ providers: { other: { limits: { maxConcurrent: 1 } } } }),
      observer,
    });
    const controls = (observer.activities[0]!.metadata as Record<string, Record<string, unknown>>)["typeflux.provider_controls"];
    expect(controls).toMatchObject({ policy_source: "none", queued: false, throttled: false });
  });
});
