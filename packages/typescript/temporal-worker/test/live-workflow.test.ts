import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { defineActivity } from "@typeflux/temporal";

import { buildTemporalActivities, createTypefluxWorker, executeWorkflow } from "../src/index.js";

// Skipped in CI. Runs the full worker + workflow loop against a local dev server:
//   temporal server start-dev                              (separate terminal)
//   pnpm -r build                                          (the workflow bundle resolves
//                                                            @typeflux/temporal/composition from dist)
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-worker test
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

class StaticProvider implements ModelProvider {
  constructor(private readonly response: unknown) {}
  structuredCall(_params: StructuredCallParams): unknown {
    return this.response;
  }
}

const messages = [{ role: "user", content: "go" }];

describe.skipIf(!LIVE)("live workflow e2e (#450)", () => {
  it("runs reviewWorkflow end-to-end (classify + durable fan-out substantiation)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const taskQueue = "typeflux-live-wf";
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const classify = defineActivity({
        name: "classifyDisclosure",
        prompt: { name: "p/classify", label: "production" },
        input: z.object({ text: z.string() }),
        output: z.object({ category: z.string() }),
      });
      const substantiate = defineActivity({
        name: "substantiateClaim",
        prompt: { name: "p/substantiate", label: "production" },
        input: z.object({ claim: z.string() }),
        output: z.object({ verdict: z.string(), supported: z.boolean() }),
      });
      const activities = buildTemporalActivities([
        { descriptor: classify, options: { provider: new StaticProvider({ category: "financial" }), messages } },
        {
          descriptor: substantiate,
          options: { provider: new StaticProvider({ verdict: "substantiated", supported: true }), messages },
        },
      ]);

      const worker = await createTypefluxWorker({
        taskQueue,
        activities,
        connection,
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();

      // runUntil starts the worker, awaits the workflow result, then shuts down.
      const result = await worker.runUntil(
        executeWorkflow(client, {
          workflowType: "reviewWorkflow",
          taskQueue,
          workflowId: `review-${Date.now()}`,
          args: [{ text: "an annual disclosure", claims: ["c1", "c2", "c3"] }],
        }),
      );

      expect(result).toMatchObject({
        category: "financial",
        verdicts: [
          { claim: "c1", verdict: "substantiated", supported: true },
          { claim: "c2", verdict: "substantiated", supported: true },
          { claim: "c3", verdict: "substantiated", supported: true },
        ],
      });
    } finally {
      await connection.close();
    }
  });
});
