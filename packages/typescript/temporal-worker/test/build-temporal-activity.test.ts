import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { AnthropicMessagesTransport, CachedSessionHandle, ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { ApplicationFailure, CancelledFailure } from "@temporalio/common";
import { ActivityCancelledError, AnthropicProvider, defineActivity, InMemoryCacheStore, ModerationBlockedError, ProviderCacheUnavailableError, ProviderConfigError } from "@typeflux/temporal";

import { buildCachePrepActivity, buildCacheReleaseActivity, buildTemporalActivities, buildTemporalActivity } from "../src/index.js";

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

const messages = [{ role: "user", content: "summarize: {{ text }}" }];

const summarize = defineActivity({
  name: "summarize",
  prompt: { name: "p/summarize", label: "production" },
  input: z.object({ text: z.string() }),
  output: z.object({ summary: z.string(), words: z.number() }),
  // A normalization hook: trims the summary, proving the hook runs through the adapter.
  hook: (_input, output) => ({ ...output, summary: output.summary.trim() }),
});

describe("buildTemporalActivity (#450)", () => {
  it("runs the full executeActivity pipeline (validate -> provider -> validate -> hook)", async () => {
    const provider = new FakeProvider([{ summary: "  done  ", words: 1 }]);
    const activity = buildTemporalActivity(summarize, { provider, messages });

    const out = await activity({ text: "hello world" });

    expect(out).toEqual({ summary: "done", words: 1 }); // hook trimmed the summary
    expect(provider.calls).toBe(1);
  });

  it("forwards a cross-run cacheStore to executeActivity: a second call is memoized (#398/#753)", async () => {
    // The worker wrapper must carry `cacheStore` from its options into executeActivity, or a
    // YAML/worker activity that declared cross_run_cache could never reach the memoization gate.
    const cached = defineActivity({
      name: "summarize",
      prompt: { name: "p/summarize", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string(), words: z.number() }),
      cache: { enabled: true },
    });
    const provider = new FakeProvider([{ summary: "done", words: 1 }]);
    const cacheStore = new InMemoryCacheStore();
    const activity = buildTemporalActivity(cached, { provider, messages, cacheStore });
    const first = await activity({ text: "hello" });
    const second = await activity({ text: "hello" });
    expect(second).toEqual(first);
    expect(provider.calls).toBe(1); // the second call served from cache — the store was forwarded
  });

  it("names the activity function for the descriptor (worker registration / stack traces)", () => {
    const provider = new FakeProvider([]);
    const activity = buildTemporalActivity(summarize, { provider, messages });
    expect(activity.name).toBe("summarize");
  });

  it("rejects an off-shape Temporal payload before reaching the provider (input validation)", async () => {
    const provider = new FakeProvider([{ summary: "x", words: 1 }]);
    const activity = buildTemporalActivity(summarize, { provider, messages });
    // A deserialized payload that violates descriptor.input (number, not string) —
    // the compile-time type is erased at the Temporal boundary, so the parse guards it.
    await expect(activity({ text: 123 } as unknown as { text: string })).rejects.toThrow();
    expect(provider.calls).toBe(0); // never reached the provider
  });

  it("propagates a plain provider error unwrapped (so the worker's RetryPolicy can act)", async () => {
    const provider = new FakeProvider([]); // exhausted -> structuredCall throws
    const activity = buildTemporalActivity(summarize, { provider, messages });
    const failure = await activity({ text: "x" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toMatch(/exhausted/);
    expect(failure).not.toBeInstanceOf(ApplicationFailure); // not terminal -> retryable
  });

  it("translates a moderation block into a non-retryable ApplicationFailure (#493)", async () => {
    const blocked = defineActivity({
      name: "blocked",
      prompt: { name: "p/blocked", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string(), words: z.number() }),
      moderation: { moderator: () => ({ flagged: true, categories: ["unsafe"] }) },
    });
    const provider = new FakeProvider([{ summary: "bad", words: 1 }]);
    const activity = buildTemporalActivity(blocked, { provider, messages });
    // A blocked verdict is terminal — the same output reproduces it, so Temporal retries only
    // multiply provider spend (parity with Python _raise_temporal_non_retryable_if_needed).
    const failure = await activity({ text: "x" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(failure).toBeInstanceOf(ApplicationFailure);
    expect((failure as ApplicationFailure).nonRetryable).toBe(true);
    expect((failure as ApplicationFailure).type).toBe("ModerationBlockedError");
    expect((failure as ApplicationFailure).cause).toBeInstanceOf(ModerationBlockedError);
  });

  it("translates a ProviderConfigError into a non-retryable ApplicationFailure (#493)", async () => {
    const provider = {
      structuredCall: () => {
        throw new ProviderConfigError("bad provider config", { provider: "openai" });
      },
    };
    const activity = buildTemporalActivity(summarize, { provider, messages });
    const failure = await activity({ text: "x" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(failure).toBeInstanceOf(ApplicationFailure);
    expect((failure as ApplicationFailure).nonRetryable).toBe(true);
    expect((failure as ApplicationFailure).type).toBe("ProviderConfigError");
  });

  it("fails a truncated provider response on attempt 1 instead of retrying it (#784)", async () => {
    // A real provider's truncation error (not a hand-built ProviderConfigError): the
    // same token limit truncates the identical call on every retry, so the adapter
    // must classify it non-retryable — parity with Python raise_if_truncated +
    // _raise_temporal_non_retryable_if_needed.
    const transport: AnthropicMessagesTransport = {
      messages: {
        create: () =>
          Promise.resolve({
            content: [{ type: "text", text: JSON.stringify({ summary: "partial", words: 1 }) }],
            stop_reason: "max_tokens",
          }),
      },
    };
    const provider = new AnthropicProvider(transport, { model: "claude-sonnet-4-6" });
    const activity = buildTemporalActivity(summarize, { provider, messages });
    const failure = await activity({ text: "x" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(failure).toBeInstanceOf(ApplicationFailure);
    expect((failure as ApplicationFailure).nonRetryable).toBe(true);
    expect((failure as ApplicationFailure).type).toBe("ProviderConfigError");
    expect((failure as ApplicationFailure).message).toMatch(/truncated/);
  });

  it("threads deps + tenant through to the activity-context hook", async () => {
    // The adapter's job is to forward options into executeActivity; prove deps/tenant
    // reach the context-aware hook's ActivityContext (parity with build_temporal_activity).
    const withCtx = defineActivity({
      name: "withCtx",
      prompt: { name: "p/ctx", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string() }),
      hook: (_input, output, ctx) => ({
        summary: `${output.summary}|${ctx?.tenant?.["company_id"] ?? ""}|${(ctx?.deps as { label?: string } | undefined)?.label ?? ""}`,
      }),
    });
    const provider = new FakeProvider([{ summary: "base" }]);
    const activity = buildTemporalActivity(withCtx, {
      provider,
      messages,
      deps: { label: "D" },
      tenant: { company_id: "acme" },
    });

    const out = await activity({ text: "x" });
    expect(out).toEqual({ summary: "base|acme|D" });
  });

  it("starts the injected heartbeater and stops it after a successful run (#484)", async () => {
    const provider = new FakeProvider([{ summary: "done", words: 1 }]);
    let started = 0;
    let stopped = 0;
    const activity = buildTemporalActivity(summarize, { provider, messages }, {
      heartbeater: () => {
        started += 1;
        return () => {
          stopped += 1;
        };
      },
    });
    await activity({ text: "x" });
    expect(started).toBe(1);
    expect(stopped).toBe(1);
  });

  it("stops the heartbeater even when the activity body throws (#484)", async () => {
    const provider = new FakeProvider([]); // exhausted -> structuredCall throws
    let stopped = 0;
    const activity = buildTemporalActivity(summarize, { provider, messages }, {
      heartbeater: () => () => {
        stopped += 1;
      },
    });
    await expect(activity({ text: "x" })).rejects.toThrow(/exhausted/);
    expect(stopped).toBe(1); // the finally cleared the loop despite the failure
  });

  it("runs normally when the heartbeater resolves to undefined (no heartbeat timeout) (#484)", async () => {
    const provider = new FakeProvider([{ summary: "done", words: 1 }]);
    const activity = buildTemporalActivity(summarize, { provider, messages }, { heartbeater: () => undefined });
    await expect(activity({ text: "x" })).resolves.toEqual({ summary: "done", words: 1 });
  });
});

describe("buildTemporalActivity cancellation (#487)", () => {
  it("threads the injected cancellationSignal into executeActivity (pre-cancelled -> no provider call)", async () => {
    const provider = new FakeProvider([{ summary: "x", words: 1 }]);
    const controller = new AbortController();
    controller.abort();
    const activity = buildTemporalActivity(summarize, { provider, messages }, {
      cancellationSignal: () => controller.signal,
    });
    const failure = await activity({ text: "x" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    // A standalone abort (plain AbortController) keeps the typed core error.
    expect(failure).toBeInstanceOf(ActivityCancelledError);
    expect(provider.calls).toBe(0);
  });

  it("rethrows Temporal's CancelledFailure so the activity is recorded cancelled, not failed", async () => {
    const provider = new FakeProvider([{ summary: "x", words: 1 }]);
    const cancelled = new CancelledFailure("workflow cancelled");
    const controller = new AbortController();
    controller.abort(cancelled); // Temporal's cancellationSignal reason IS a CancelledFailure
    const activity = buildTemporalActivity(summarize, { provider, messages }, {
      cancellationSignal: () => controller.signal,
    });
    const failure = await activity({ text: "x" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(failure).toBe(cancelled); // the boundary unwraps to Temporal's own failure
    expect(failure).not.toBeInstanceOf(ApplicationFailure); // cancellation is not a terminal wrap
  });

  it("runs unchanged when the cancellation injection yields undefined (standalone)", async () => {
    const provider = new FakeProvider([{ summary: "ok", words: 1 }]);
    const activity = buildTemporalActivity(summarize, { provider, messages }, {
      cancellationSignal: () => undefined,
    });
    await expect(activity({ text: "x" })).resolves.toEqual({ summary: "ok", words: 1 });
  });
});

describe("session-cache activities (#478 PR6)", () => {
  const cachedActivity = defineActivity({
    name: "review_item",
    prompt: { name: "p/review", label: "production" },
    input: z.object({ text: z.string() }),
    output: z.object({ summary: z.string(), words: z.number() }),
    sessionCache: { ttlSeconds: 600 },
  });

  const engagedReference = (identityHash: string): CachedSessionHandle => ({
    provider: "fake-reference",
    identity_hash: identityHash,
    supported: true,
    style: "reference",
    cache_id: "cachedContents/abc",
    model: null,
    created_at: null,
    ttl_seconds: 600,
    reference_cached: false,
    prefix_stable_messages: null,
    per_item_artifact_messages: false,
  });

  it("buildCachePrepActivity stamps created_at in the activity and returns the handle", async () => {
    const prepared: unknown[] = [];
    const provider: ModelProvider = {
      structuredCall: () => ({ summary: "x", words: 1 }),
      supportsSessionCache: true,
      sessionCacheStyle: "prefix",
      prepareCachedSession: (params) => {
        prepared.push(params);
        return {
          provider: "fake",
          identity_hash: params.identityHash,
          supported: true,
          style: "prefix",
          cache_id: null,
          model: null,
          created_at: null,
          ttl_seconds: null,
          reference_cached: false,
          prefix_stable_messages: null,
          per_item_artifact_messages: false,
        };
      },
    };
    const prep = buildCachePrepActivity(cachedActivity, {
      provider,
      messages: [{ role: "system", content: "You are careful." }],
    });
    expect(prep.name).toBe("review_item.__prepare_cache__");
    const before = Date.now();
    const handle = (await prep({ text: "representative" })) as CachedSessionHandle;
    expect(handle.supported).toBe(true);
    expect(handle.created_at).not.toBeNull();
    expect(Date.parse(handle.created_at as string)).toBeGreaterThanOrEqual(before - 1000);
    expect(prepared).toHaveLength(1);
  });

  it("a templated system prefix fails as a NON-RETRYABLE ApplicationFailure", async () => {
    const provider: ModelProvider = {
      structuredCall: () => ({ summary: "x", words: 1 }),
      supportsSessionCache: true,
      sessionCacheStyle: "prefix",
      prepareCachedSession: () => {
        throw new Error("unreachable");
      },
    };
    const prep = buildCachePrepActivity(cachedActivity, {
      provider,
      messages: [{ role: "system", content: "Review for {{ customer }}." }],
    });
    const failure = await prep({ text: "x" }).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApplicationFailure);
    expect((failure as ApplicationFailure).nonRetryable).toBe(true);
    expect((failure as ApplicationFailure).type).toBe("UnstableCachePrefixError");
  });

  it("buildCacheReleaseActivity parses the wire handle and probes the provider", async () => {
    const released: unknown[] = [];
    const provider: ModelProvider = {
      structuredCall: () => ({}),
      releaseCachedSession: (handle) => {
        released.push(handle);
      },
    };
    const release = buildCacheReleaseActivity(cachedActivity, { provider, messages });
    expect(release.name).toBe("review_item.__release_cache__");
    await release(engagedReference("i".repeat(64)));
    expect(released).toHaveLength(1);
    // A provider WITHOUT the capability is a no-op, never a throw.
    const bare = buildCacheReleaseActivity(cachedActivity, { provider: { structuredCall: () => ({}) }, messages });
    await expect(bare(engagedReference("i".repeat(64)))).resolves.toBeUndefined();
    // An off-shape handle rejects at the boundary.
    await expect(release({ nonsense: true })).rejects.toThrow();
  });

  it("the main activity parses its second-arg handle and threads it to the provider", async () => {
    const seen: (CachedSessionHandle | undefined)[] = [];
    const provider: ModelProvider = {
      structuredCall: (params: StructuredCallParams) => {
        seen.push(params.cachedSession);
        return { summary: "ok", words: 1 };
      },
    };
    const activity = buildTemporalActivity(cachedActivity, { provider, messages });
    await activity({ text: "hi" }, engagedReference("i".repeat(64)));
    expect(seen[0]?.identity_hash).toBe("i".repeat(64));
    await activity({ text: "hi" });
    expect(seen[1]).toBeUndefined();
    await expect(activity({ text: "hi" }, { bad: "handle" })).rejects.toThrow();
  });

  it("re-runs ONCE uncached when the referenced cache vanished (ProviderCacheUnavailableError)", async () => {
    const seen: (CachedSessionHandle | undefined)[] = [];
    const provider: ModelProvider = {
      structuredCall: (params: StructuredCallParams) => {
        seen.push(params.cachedSession);
        if (params.cachedSession !== undefined) {
          throw new ProviderCacheUnavailableError("cache gone", { provider: "fake", statusCode: 403 });
        }
        return { summary: "recovered", words: 1 };
      },
    };
    const activity = buildTemporalActivity(cachedActivity, { provider, messages });
    const out = await activity({ text: "hi" }, engagedReference("i".repeat(64)));
    expect(out).toEqual({ summary: "recovered", words: 1 });
    expect(seen).toHaveLength(2);
    expect(seen[1]).toBeUndefined(); // the re-run stripped the handle

    // A PREFIX-style (or fail-soft) handle does NOT recover — and re-running the
    // identical call with the same fixed handle is doomed, so it surfaces as a
    // NON-RETRYABLE ApplicationFailure (Python retryable=False parity).
    const prefixHandle: CachedSessionHandle = { ...engagedReference("i".repeat(64)), style: "prefix", cache_id: null };
    const failure = await activity({ text: "hi" }, prefixHandle).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApplicationFailure);
    expect((failure as ApplicationFailure).nonRetryable).toBe(true);
    expect((failure as ApplicationFailure).type).toBe("ProviderCacheUnavailableError");
  });

  it("buildTemporalActivities registers the prep/release companions for session-cached descriptors", () => {
    const provider: ModelProvider = { structuredCall: () => ({}) };
    const activities = buildTemporalActivities([
      { descriptor: cachedActivity, options: { provider, messages } },
      { descriptor: summarize, options: { provider, messages } },
    ]);
    expect(Object.keys(activities).sort()).toEqual([
      "review_item",
      "review_item.__prepare_cache__",
      "review_item.__release_cache__",
      "summarize",
    ]);
  });
});

describe("companion registration guards (#478 Bugbot/codex)", () => {
  const provider: ModelProvider = { structuredCall: () => ({}) };

  it("a user activity colliding with a companion name fails LOUD in either order", () => {
    const cached = defineActivity({
      name: "review_item",
      prompt: { name: "p/review", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string(), words: z.number() }),
      sessionCache: {},
    });
    const collider = defineActivity({
      name: "review_item.__prepare_cache__",
      prompt: { name: "p/x", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string(), words: z.number() }),
    });
    // Collider first: the companion registration must throw, not overwrite.
    expect(() =>
      buildTemporalActivities([
        { descriptor: collider, options: { provider, messages } },
        { descriptor: cached, options: { provider, messages } },
      ]),
    ).toThrow(/duplicate activity name/);
    // Companion first: the main-loop guard throws on the collider.
    expect(() =>
      buildTemporalActivities([
        { descriptor: cached, options: { provider, messages } },
        { descriptor: collider, options: { provider, messages } },
      ]),
    ).toThrow(/duplicate activity name/);
  });

  it("a DISABLED session cache registers no companions", () => {
    const disabled = defineActivity({
      name: "review_item",
      prompt: { name: "p/review", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string(), words: z.number() }),
      sessionCache: { enabled: false },
    });
    const activities = buildTemporalActivities([{ descriptor: disabled, options: { provider, messages } }]);
    expect(Object.keys(activities)).toEqual(["review_item"]);
  });
});
