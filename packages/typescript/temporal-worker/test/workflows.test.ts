import { beforeEach, describe, expect, it, vi } from "vitest";

// Pure unit tests for src/workflows.ts orchestration (the live tests prove it in a
// real Temporal sandbox; this guards the logic in CI). workflows.ts binds its
// activities via `proxyActivities` AT MODULE LOAD, so we mock @temporalio/workflow
// at the module boundary (same idiom as heartbeat-loop.test.ts for
// @temporalio/activity): the proxy dispatches by property name to per-test vi.fn
// implementations, `workflowInfo` returns a fixed id, and `executeChild` records
// its start options and runs the child workflow function directly.
const { activities, executeChildMock } = vi.hoisted(() => ({
  activities: {
    classifyDisclosure: vi.fn(),
    substantiateClaim: vi.fn(),
  } as Record<string, ReturnType<typeof vi.fn>>,
  executeChildMock: vi.fn(),
}));

vi.mock("@temporalio/workflow", () => ({
  proxyActivities: () =>
    new Proxy(
      {},
      {
        get:
          (_target, prop) =>
          (...args: unknown[]) => {
            const impl = activities[String(prop)];
            if (!impl) {
              throw new Error(`unproxied activity: ${String(prop)}`);
            }
            return impl(...args);
          },
      },
    ),
  workflowInfo: () => ({ workflowId: "parent-wf" }),
  executeChild: (...args: unknown[]) => executeChildMock(...args),
}));

// Imported after vi.mock so the module under test binds the mocked @temporalio/workflow.
const { batchReviewWorkflow, reviewWorkflow } = await import("../src/workflows.js");

const input = (text: string, claims: string[]) => ({ text, claims });

beforeEach(() => {
  activities.classifyDisclosure!.mockReset();
  activities.substantiateClaim!.mockReset();
  executeChildMock.mockReset();
});

describe("reviewWorkflow (#450)", () => {
  it("classifies first, then substantiates every claim (sequential gate before the fan-out)", async () => {
    const events: string[] = [];
    activities.classifyDisclosure!.mockImplementation(async ({ text }: { text: string }) => {
      events.push(`classify:${text}`);
      return { category: "financial" };
    });
    activities.substantiateClaim!.mockImplementation(async ({ claim }: { claim: string }) => {
      events.push(`substantiate:${claim}`);
      return { verdict: "supported", supported: true };
    });

    const result = await reviewWorkflow(input("disclosure", ["c1", "c2"]));

    expect(result.category).toBe("financial");
    // Classification strictly precedes ALL substantiation calls.
    expect(events[0]).toBe("classify:disclosure");
    expect(events.slice(1)).toEqual(expect.arrayContaining(["substantiate:c1", "substantiate:c2"]));
    expect(activities.substantiateClaim).toHaveBeenCalledTimes(2);
  });

  it("collects verdicts in claim order even when completions arrive out of order", async () => {
    activities.classifyDisclosure!.mockResolvedValue({ category: "x" });
    const resolvers = new Map<string, (v: { verdict: string; supported: boolean }) => void>();
    activities.substantiateClaim!.mockImplementation(
      ({ claim }: { claim: string }) =>
        new Promise((resolve) => {
          resolvers.set(claim, resolve);
        }),
    );

    const pending = reviewWorkflow(input("t", ["a", "b", "c"]));
    // Let the fan-out schedule all three (concurrency 5 > 3), then resolve in reverse.
    await vi.waitFor(() => expect(resolvers.size).toBe(3));
    resolvers.get("c")!({ verdict: "unsupported", supported: false });
    resolvers.get("b")!({ verdict: "supported", supported: true });
    resolvers.get("a")!({ verdict: "supported", supported: true });

    const result = await pending;
    expect(result.verdicts).toEqual([
      { claim: "a", verdict: "supported", supported: true },
      { claim: "b", verdict: "supported", supported: true },
      { claim: "c", verdict: "unsupported", supported: false },
    ]);
  });

  it("bounds substantiation concurrency at 5 in-flight calls", async () => {
    activities.classifyDisclosure!.mockResolvedValue({ category: "x" });
    let inFlight = 0;
    let maxInFlight = 0;
    activities.substantiateClaim!.mockImplementation(async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await Promise.resolve(); // yield so siblings can start
      inFlight -= 1;
      return { verdict: "supported", supported: true };
    });

    const claims = Array.from({ length: 12 }, (_, i) => `claim-${i}`);
    const result = await reviewWorkflow(input("t", claims));

    expect(result.verdicts).toHaveLength(12);
    expect(activities.substantiateClaim).toHaveBeenCalledTimes(12);
    // Exactly the fanOut bound in workflows.ts ({ concurrency: 5 }): with the
    // mock's sync-start-then-yield shape a correct pool deterministically
    // saturates, so this catches regressions in BOTH directions.
    expect(maxInFlight).toBe(5);
  });

  it("propagates a classification failure without starting substantiation", async () => {
    activities.classifyDisclosure!.mockRejectedValue(new Error("provider down"));
    await expect(reviewWorkflow(input("t", ["c1"]))).rejects.toThrow("provider down");
    expect(activities.substantiateClaim).not.toHaveBeenCalled();
  });

  it("propagates the first substantiation failure and stops scheduling new claims", async () => {
    activities.classifyDisclosure!.mockResolvedValue({ category: "x" });
    activities.substantiateClaim!.mockImplementation(async ({ claim }: { claim: string }) => {
      if (claim === "claim-0") {
        throw new Error("substantiation failed: claim-0");
      }
      return { verdict: "supported", supported: true };
    });

    const claims = Array.from({ length: 20 }, (_, i) => `claim-${i}`);
    await expect(reviewWorkflow(input("t", claims))).rejects.toThrow("substantiation failed: claim-0");
    // fanOut stops pulling new items after the failure — with the first item failing
    // synchronously-after-await, far fewer than all 20 are ever attempted.
    expect(activities.substantiateClaim!.mock.calls.length).toBeLessThan(20);
  });
});

describe("batchReviewWorkflow (#450)", () => {
  it("runs each review as a child with a deterministic parent-derived workflowId", async () => {
    executeChildMock.mockImplementation(
      async (_workflow: unknown, options: { args: [{ text: string }]; workflowId: string }) => ({
        category: `cat:${options.args[0].text}`,
        verdicts: [],
      }),
    );

    const results = await batchReviewWorkflow([input("one", []), input("two", []), input("three", [])]);

    expect(results.map((r) => r.category)).toEqual(["cat:one", "cat:two", "cat:three"]);
    const started = executeChildMock.mock.calls.map(
      (call) => (call[1] as { workflowId: string }).workflowId,
    );
    expect(started.sort()).toEqual(["parent-wf/review-0", "parent-wf/review-1", "parent-wf/review-2"]);
    // Every child starts the reviewWorkflow function with the review input as args.
    for (const call of executeChildMock.mock.calls) {
      expect(call[0]).toBe(reviewWorkflow);
      expect((call[1] as { args: unknown[] }).args).toHaveLength(1);
    }
  });

  it("propagates a child failure", async () => {
    executeChildMock.mockImplementation(
      async (_workflow: unknown, options: { workflowId: string }) => {
        if (options.workflowId.endsWith("review-1")) {
          throw new Error("child failed");
        }
        return { category: "ok", verdicts: [] };
      },
    );
    await expect(batchReviewWorkflow([input("a", []), input("b", []), input("c", [])])).rejects.toThrow(
      "child failed",
    );
  });
});
