/**
 * LazyBackend caching semantics (#785): a successful backend is cached for the life of the
 * process; a FAILED resolution is cached only for the retry cooldown (doubling per consecutive
 * failure up to the cap), so a transient first-connect failure heals on a later tool call
 * instead of poisoning a long-lived stdio session until restart. `dispose()` is terminal.
 *
 * No settle-sleeps here on purpose: the internal bookkeeping handler is reaction #0 on each
 * attempt (attached before any caller can), so it runs before the caller's continuation —
 * these tests pin that ordering invariant.
 */
import { describe, expect, it } from "vitest";

import {
  BACKEND_RETRY_COOLDOWN_CAP_MS,
  LazyBackend,
  type Backend,
  type BackendResolver,
} from "../src/backend.js";
import type { ServerConfig } from "../src/config.js";

const config = { mode: "attach", attachUrl: "http://127.0.0.1:1" } as ServerConfig;

function fakeBackend(tag: string, onDispose?: () => void): Backend {
  return {
    mode: "attach",
    baseUrl: tag,
    dispose: async () => onDispose?.(),
  } as unknown as Backend;
}

/** A resolver scripted per call: an Error entry rejects, a string entry resolves. */
function scriptedResolver(
  script: (string | Error)[],
  onDispose?: () => void,
): { resolver: BackendResolver; calls: () => number } {
  let calls = 0;
  return {
    resolver: async () => {
      const step = script[Math.min(calls, script.length - 1)];
      calls += 1;
      if (step instanceof Error) throw step;
      return fakeBackend(step as string, onDispose);
    },
    calls: () => calls,
  };
}

/** A manually-advanced clock injected as the `now` seam. */
function fakeClock(start = 0): { now: () => number; advance: (ms: number) => void } {
  let t = start;
  return { now: () => t, advance: (ms) => (t += ms) };
}

describe("LazyBackend (#785)", () => {
  it("caches a successful backend forever — no re-resolution however much time passes", async () => {
    const { resolver, calls } = scriptedResolver(["ok"]);
    const clock = fakeClock();
    const lazy = new LazyBackend(config, {}, 1_000, resolver, clock.now);
    const first = await lazy.get();
    clock.advance(10 * BACKEND_RETRY_COOLDOWN_CAP_MS);
    const second = await lazy.get();
    expect(second).toBe(first);
    expect(calls()).toBe(1);
  });

  it("serves the SAME rejection within the cooldown, then re-attempts at the boundary", async () => {
    const { resolver, calls } = scriptedResolver([new Error("port race"), "recovered"]);
    const clock = fakeClock();
    const lazy = new LazyBackend(config, {}, 1_000, resolver, clock.now);
    await expect(lazy.get()).rejects.toThrow("port race");
    clock.advance(999); // inside the window: same structured error, no restart storm
    await expect(lazy.get()).rejects.toThrow("port race");
    expect(calls()).toBe(1);
    clock.advance(1); // exactly at the boundary: retry
    const backend = await lazy.get();
    expect((backend as unknown as { baseUrl: string }).baseUrl).toBe("recovered");
    expect(calls()).toBe(2);
  });

  it("backs off exponentially on consecutive failures, up to the cap", async () => {
    const failures = [new Error("f1"), new Error("f2"), new Error("f3")];
    const { resolver, calls } = scriptedResolver([...failures, "up"]);
    const clock = fakeClock();
    const lazy = new LazyBackend(config, {}, 1_000, resolver, clock.now);
    await expect(lazy.get()).rejects.toThrow("f1");
    clock.advance(1_000); // 1st cooldown: base
    await expect(lazy.get()).rejects.toThrow("f2");
    clock.advance(1_000); // 2nd cooldown doubled to 2s: not elapsed yet
    await expect(lazy.get()).rejects.toThrow("f2");
    expect(calls()).toBe(2);
    clock.advance(1_000);
    await expect(lazy.get()).rejects.toThrow("f3");
    clock.advance(4_000); // 3rd cooldown: 4s
    const backend = await lazy.get();
    expect((backend as unknown as { baseUrl: string }).baseUrl).toBe("up");
    expect(calls()).toBe(4);
  });

  it("dispose() is terminal — a torn-down session can never resurrect a backend", async () => {
    let disposed = 0;
    const { resolver, calls } = scriptedResolver([new Error("boot fail"), "zombie"], () => {
      disposed += 1;
    });
    const clock = fakeClock();
    const lazy = new LazyBackend(config, {}, 0, resolver, clock.now);
    await expect(lazy.get()).rejects.toThrow("boot fail");
    await lazy.dispose();
    clock.advance(1_000_000);
    await expect(lazy.get()).rejects.toThrow(/disposed/);
    expect(calls()).toBe(1); // the resolver never ran again — no orphaned control plane
    expect(disposed).toBe(0);
  });

  it("dispose() after success closes the cached backend and get() stays terminal", async () => {
    let disposed = 0;
    const { resolver } = scriptedResolver(["ok"], () => {
      disposed += 1;
    });
    const lazy = new LazyBackend(config, {}, 0, resolver);
    await lazy.get();
    await lazy.dispose();
    expect(disposed).toBe(1);
    await expect(lazy.get()).rejects.toThrow(/disposed/);
  });
});
