import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { defineActivity } from "@typeflux/temporal";

import { buildTemporalActivities, createTypefluxWorker } from "../src/index.js";

// Skipped in CI. Run against a local dev server with:
//   temporal server start-dev      (separate terminal)
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-worker test
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

class FakeProvider implements ModelProvider {
  structuredCall(_params: StructuredCallParams): unknown {
    return { summary: "live" };
  }
}

describe.skipIf(!LIVE)("live worker bootstrap (#450)", () => {
  it("connects to the dev server and creates an activity-only worker", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const echo = defineActivity({
        name: "echo",
        prompt: { name: "p/echo", label: "production" },
        input: z.object({ text: z.string() }),
        output: z.object({ summary: z.string() }),
      });
      const activities = buildTemporalActivities([
        {
          descriptor: echo,
          options: { provider: new FakeProvider(), messages: [{ role: "user", content: "go" }] },
        },
      ]);
      const worker = await createTypefluxWorker({
        taskQueue: "typeflux-live-test",
        activities,
        connection,
      });
      // runUntil starts the worker, runs the callback, then gracefully shuts it down —
      // releasing the native reference on `connection` so `connection.close()` (in the
      // finally) succeeds (a created-but-never-run worker would block the close).
      let started = false;
      await worker.runUntil(async () => {
        started = true;
      });
      expect(started).toBe(true);
      // The full worker.run() + executeWorkflow() loop is exercised once code-defined
      // workflows land (PR4); this proves the bootstrap connects, registers, and shuts down.
    } finally {
      await connection.close();
    }
  });
});
