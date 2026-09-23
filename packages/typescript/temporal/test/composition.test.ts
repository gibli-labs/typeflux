import { describe, expect, it } from "vitest";

import { fanOut, groundWithSearch, withFallback } from "../src/index.js";

const tick = (ms = 0) => new Promise<void>((resolve) => setTimeout(resolve, ms));

describe("fanOut (#450)", () => {
  it("preserves input order despite out-of-order completion", async () => {
    const out = await fanOut(
      [1, 2, 3, 4],
      async (n) => {
        // Later items finish sooner, so completion order != input order.
        await tick((10 - n) * 3);
        return n * 2;
      },
      { concurrency: 4 },
    );
    expect(out).toEqual([2, 4, 6, 8]);
  });

  it("caps concurrency at the configured limit", async () => {
    let inFlight = 0;
    let peak = 0;
    await fanOut(
      Array.from({ length: 12 }, (_, i) => i),
      async (n) => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await tick(5);
        inFlight -= 1;
        return n;
      },
      { concurrency: 3 },
    );
    expect(peak).toBeLessThanOrEqual(3);
  });

  it("rejects concurrency < 1", async () => {
    await expect(fanOut([1], async (n) => n, { concurrency: 0 })).rejects.toThrow(/>= 1/);
  });

  it("propagates the first item error", async () => {
    await expect(
      fanOut(
        [1, 2, 3],
        async (n) => {
          if (n === 2) throw new Error("item 2 failed");
          return n;
        },
        { concurrency: 2 },
      ),
    ).rejects.toThrow(/item 2 failed/);
  });

  it("awaits every in-flight sibling to SETTLE before rejecting (#299 orphan-safety)", async () => {
    // A sibling already in flight when another item fails must finish before fanOut rejects —
    // otherwise it could mutate shared state (e.g. push a compensation) AFTER the rejection has
    // propagated. Item 0 is slow, item 1 fails fast; fanOut must not reject until item 0 settled.
    let slowSettled = false;
    await expect(
      fanOut(
        [0, 1],
        async (n) => {
          if (n === 1) {
            throw new Error("item 1 failed");
          }
          await tick(20);
          slowSettled = true;
          return n;
        },
        { concurrency: 2 },
      ),
    ).rejects.toThrow(/item 1 failed/);
    // The rejection did not surface until the in-flight slow sibling had fully settled.
    expect(slowSettled).toBe(true);
  });

  it("does not start new items after a failure (cancellation intent)", async () => {
    const started: number[] = [];
    await expect(
      fanOut(
        [0, 1, 2, 3],
        async (n) => {
          started.push(n);
          await tick(1);
          if (n === 1) throw new Error("boom");
          return n;
        },
        { concurrency: 1 }, // serial: 0 ok, 1 throws -> 2 and 3 must never start
      ),
    ).rejects.toThrow(/boom/);
    await tick(10);
    expect(started).toEqual([0, 1]);
  });
});

describe("withFallback (#450)", () => {
  it("returns the primary result on success", async () => {
    expect(await withFallback(async () => "primary", async () => "fallback")).toBe("primary");
  });

  it("runs the fallback with the error when the primary rejects", async () => {
    const out = await withFallback<string>(
      async () => {
        throw new Error("down");
      },
      async (err) => `recovered: ${(err as Error).message}`,
    );
    expect(out).toBe("recovered: down");
  });

  it("re-throws an error the predicate rejects", async () => {
    await expect(
      withFallback<string>(
        async () => {
          throw new TypeError("nope");
        },
        async () => "fallback",
        { errors: (e) => e instanceof RangeError },
      ),
    ).rejects.toThrow(TypeError);
  });
});

describe("groundWithSearch (#450)", () => {
  it("folds a sync search's results into the input", async () => {
    const grounded = await groundWithSearch("claim-a", {
      search: (q) => [`doc:${q}:1`, `doc:${q}:2`],
      ground: (q, results) => ({ query: q, context: results }),
    });
    expect(grounded).toEqual({ query: "claim-a", context: ["doc:claim-a:1", "doc:claim-a:2"] });
  });

  it("awaits an async search", async () => {
    const grounded = await groundWithSearch("q", {
      search: async (q) => {
        await tick(0);
        return [`hit:${q}`];
      },
      ground: (q, results) => ({ q, results }),
    });
    expect(grounded).toEqual({ q: "q", results: ["hit:q"] });
  });

  it("handles empty results", async () => {
    const grounded = await groundWithSearch("q", {
      search: () => [],
      ground: (q, results) => ({ q, n: results.length }),
    });
    expect(grounded).toEqual({ q: "q", n: 0 });
  });

  it("materializes a non-array iterable search result (Python list() parity)", async () => {
    const grounded = await groundWithSearch("q", {
      search: () => new Set(["a", "b", "a"]), // a Set, not an array
      ground: (_q, results) => results, // ground receives a real array
    });
    expect(Array.isArray(grounded)).toBe(true);
    expect(grounded).toEqual(["a", "b"]);
  });
});
