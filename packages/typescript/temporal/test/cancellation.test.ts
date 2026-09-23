import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "../src/index.js";
import {
  ActivityCancelledError,
  AnthropicProvider,
  defineActivity,
  executeActivity,
  GeminiProvider,
  OpenAIProvider,
  ProviderTransientError,
} from "../src/index.js";

const echo = defineActivity({
  name: "echo",
  prompt: { name: "p/echo", label: "production" },
  input: z.object({ text: z.string() }),
  output: z.object({ out: z.string() }),
});

const messages = [{ role: "user", content: "echo: {{ text }}" }];

class CountingProvider implements ModelProvider {
  calls = 0;
  lastParams: StructuredCallParams | undefined;
  constructor(private readonly responses: unknown[]) {}
  structuredCall(params: StructuredCallParams): unknown {
    this.calls += 1;
    this.lastParams = params;
    const next = this.responses.shift();
    if (next instanceof Error) {
      throw next;
    }
    return next;
  }
}

describe("cooperative cancellation (#487)", () => {
  it("aborts at the entry checkpoint — a pre-cancelled activity never reaches the provider", async () => {
    const provider = new CountingProvider([{ out: "x" }]);
    const controller = new AbortController();
    controller.abort();
    await expect(
      executeActivity(echo, { text: "hi" }, { provider, messages, cancellationSignal: controller.signal }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
    expect(provider.calls).toBe(0);
  });

  it("forwards the signal to the provider call", async () => {
    const provider = new CountingProvider([{ out: "x" }]);
    const controller = new AbortController();
    await executeActivity(echo, { text: "hi" }, { provider, messages, cancellationSignal: controller.signal });
    expect(provider.lastParams?.signal).toBe(controller.signal);
  });

  it("runs unchanged with no signal (params carry no signal key)", async () => {
    const provider = new CountingProvider([{ out: "x" }]);
    await executeActivity(echo, { text: "hi" }, { provider, messages });
    expect(provider.lastParams !== undefined && "signal" in provider.lastParams).toBe(false);
  });

  it("aborts between validation retries — a cancel during attempt 1 stops attempt 2", async () => {
    const controller = new AbortController();
    const provider = new (class implements ModelProvider {
      calls = 0;
      structuredCall(): unknown {
        this.calls += 1;
        controller.abort(); // the cancel lands while attempt 1 is in flight
        return { wrong: true }; // fails output validation -> would normally re-prompt
      }
    })();
    const retried = defineActivity({
      name: "retried",
      prompt: { name: "p/retried", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ out: z.string() }),
      validationRetries: 3,
    });
    await expect(
      executeActivity(retried, { text: "hi" }, { provider, messages, cancellationSignal: controller.signal }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
    expect(provider.calls).toBe(1); // the repair attempt never ran
  });

  it("classifies a mid-call abort as cancellation, whatever the transport threw", async () => {
    const controller = new AbortController();
    const provider = new (class implements ModelProvider {
      structuredCall(): unknown {
        controller.abort();
        // An abort-aware transport surfaces its own error shape on abort.
        throw Object.assign(new Error("The user aborted a request."), { name: "AbortError" });
      }
    })();
    await expect(
      executeActivity(echo, { text: "hi" }, { provider, messages, cancellationSignal: controller.signal }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
  });

  it("aborts after a transient-retry backoff instead of spending the next attempt", async () => {
    const controller = new AbortController();
    const provider = new (class implements ModelProvider {
      calls = 0;
      structuredCall(): unknown {
        this.calls += 1;
        // Cancel while the backoff sleep runs; the post-sleep checkpoint must abort.
        setTimeout(() => controller.abort(), 5);
        throw new ProviderTransientError("blip");
      }
    })();
    await expect(
      executeActivity(echo, { text: "hi" }, {
        provider,
        messages,
        cancellationSignal: controller.signal,
        transientRetries: 3,
        transientRetryDelayMs: 30,
      }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
    expect(provider.calls).toBe(1); // the retry never spent a second call
  });

  it("does not serve a cache hit when the cancel landed during the async lookup", async () => {
    const cached = defineActivity({
      name: "cachedEcho",
      prompt: { name: "p/cached", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ out: z.string() }),
      cache: { enabled: true },
      // The hook is the side effect the checkpoint exists to prevent after a cancel.
      hook: () => {
        throw new Error("hook must not run for a cancelled activity");
      },
    });
    const controller = new AbortController();
    const provider = new CountingProvider([{ out: "fresh" }]);
    await expect(
      executeActivity(cached, { text: "hi" }, {
        provider,
        messages,
        cancellationSignal: controller.signal,
        cacheStore: {
          get: async () => {
            controller.abort(); // the cancel lands while the remote cache lookup is in flight
            return {
              key: { activity: "cachedEcho", input_hash: "h", scope: {} },
              output: { out: "cached" },
              created_at: "2026-01-01T00:00:00Z",
              output_schema_hash: cached.outputSchemaHash,
            };
          },
          set: async () => undefined,
        },
      }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
    expect(provider.calls).toBe(0); // neither the hit was served nor a fresh call spent
  });

  it("wins over a non-abort-aware provider that completes after the cancel landed", async () => {
    const controller = new AbortController();
    const provider = new (class implements ModelProvider {
      structuredCall(): unknown {
        controller.abort(); // cancellation delivered mid-call...
        return { out: "done" }; // ...but the provider ignores the signal and completes
      }
    })();
    // The success path must not validate/cache/finalize a result the cancelled workflow will
    // never consume (asyncio parity: CancelledError fires at the next await after the call).
    await expect(
      executeActivity(echo, { text: "hi" }, { provider, messages, cancellationSignal: controller.signal }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
  });

  it("ends a long backoff early on cancel instead of sleeping it out", async () => {
    const controller = new AbortController();
    const provider = new (class implements ModelProvider {
      structuredCall(): unknown {
        setTimeout(() => controller.abort(), 10);
        throw new ProviderTransientError("blip");
      }
    })();
    const started = Date.now();
    await expect(
      executeActivity(echo, { text: "hi" }, {
        provider,
        messages,
        cancellationSignal: controller.signal,
        transientRetries: 1,
        transientRetryDelayMs: 60_000, // would blow the test timeout if the sleep were not abortable
      }),
    ).rejects.toBeInstanceOf(ActivityCancelledError);
    // The property under test is that the 60s retry delay was NOT slept — any
    // duration far below it proves the sleep aborted. 10s (not 2s) keeps that
    // proof while surviving scheduler starvation when the whole workspace's
    // suites, builds, and dev servers run concurrently (the only conditions the
    // old bound was ever observed to trip under).
    expect(Date.now() - started).toBeLessThan(10_000);
  });

  it("carries the signal reason as the cause (the worker boundary rethrows a CancelledFailure)", async () => {
    const provider = new CountingProvider([{ out: "x" }]);
    const controller = new AbortController();
    const reason = new Error("workflow cancelled");
    controller.abort(reason);
    const failure = await executeActivity(echo, { text: "hi" }, {
      provider,
      messages,
      cancellationSignal: controller.signal,
    }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(failure).toBeInstanceOf(ActivityCancelledError);
    expect((failure as { cause?: unknown }).cause).toBe(reason);
  });
});

describe("transport signal forwarding (#487)", () => {
  it("OpenAIProvider passes the signal as the transport call options", async () => {
    let seen: { signal?: AbortSignal } | undefined;
    const provider = new OpenAIProvider(
      {
        chat: {
          completions: {
            create: async (_request, options) => {
              seen = options;
              return { choices: [{ message: { content: JSON.stringify({ out: "x" }) } }] };
            },
          },
        },
      },
      { model: "gpt-test" },
    );
    const controller = new AbortController();
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema: { type: "object", properties: {}, additionalProperties: false },
      signal: controller.signal,
    });
    expect(seen?.signal).toBe(controller.signal);
  });

  it("AnthropicProvider passes the signal as the transport call options", async () => {
    let seen: { signal?: AbortSignal } | undefined;
    const provider = new AnthropicProvider(
      {
        messages: {
          create: async (_request: unknown, options?: { signal?: AbortSignal }) => {
            seen = options;
            return { content: [{ type: "output_json", parsed_output: { out: "x" } }] };
          },
        },
      } as never,
      { model: "claude-test" },
    );
    const controller = new AbortController();
    await provider.structuredCall({
      messages: [{ role: "user", content: "go" }],
      outputSchema: { type: "object", properties: {}, additionalProperties: false },
      signal: controller.signal,
    });
    expect(seen?.signal).toBe(controller.signal);
  });

  it("GeminiProvider passes the signal (and omits options entirely when unset)", async () => {
    const seen: ({ signal?: AbortSignal } | undefined)[] = [];
    const provider = new GeminiProvider({
      generateContent: async (_request, options) => {
        seen.push(options);
        return { candidates: [{ content: { parts: [{ text: JSON.stringify({ out: "x" }) }] }, finishReason: "STOP" }] };
      },
    });
    const outputSchema = { type: "object" as const, properties: {}, additionalProperties: false };
    const controller = new AbortController();
    await provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema, signal: controller.signal });
    await provider.structuredCall({ messages: [{ role: "user", content: "go" }], outputSchema });
    expect(seen[0]?.signal).toBe(controller.signal);
    expect(seen[1]).toBeUndefined(); // single-arg transports keep seeing the old call shape
  });
});
