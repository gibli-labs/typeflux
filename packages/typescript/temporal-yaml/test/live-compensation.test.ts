import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

import { buildRuntime, loadYamlSpec } from "../src/index.js";

// Live proof of first-class YAML compensation (#299 SLICE 1). Runs the saga end to end through
// buildRuntime against a local dev server:
//   temporal server start-dev
//   pnpm -r build
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test live-compensation
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

const promptText = (params: StructuredCallParams): string =>
  params.messages.map((m) => (typeof m.content === "string" ? m.content : "")).join(" ");

/**
 * One provider for the whole saga: routes each activity on its rendered prompt, RECORDS the
 * execution order of the compensating activities (so a test can assert reverse-order unwind),
 * throws for `charge` (the failing step), and — when `failCancelFlight` — throws for
 * `cancel_flight` (the compensation-failure/partial case).
 */
class SagaProvider implements ModelProvider {
  readonly order: string[] = [];
  constructor(private readonly failCancelFlight = false) {}
  structuredCall(params: StructuredCallParams): unknown {
    const text = promptText(params);
    if (text.includes("charge")) {
      throw new Error("charge declined");
    }
    if (text.includes("cancel_flight")) {
      this.order.push("cancel_flight");
      if (this.failCancelFlight) {
        throw new Error("cancel_flight failed");
      }
      return { v: "flight-cancelled" };
    }
    if (text.includes("cancel_hotel")) {
      this.order.push("cancel_hotel");
      return { v: "hotel-cancelled" };
    }
    return { v: "ok" };
  }
}

/** A provider whose `book_flight` is slow, so a mid-run cancel signal lands before the last step. */
class SlowFlightProvider extends SagaProvider {
  constructor(private readonly delayMs: number) {
    super();
  }
  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    if (promptText(params).includes("book_flight")) {
      await new Promise((resolve) => setTimeout(resolve, this.delayMs));
    }
    return super.structuredCall(params);
  }
}

const schemas = { "schemas:S": z.object({ v: z.string() }) };

// A three-step saga: book_hotel -> book_flight -> charge (fails). Each booking declares its
// compensating activity; the failing `charge` triggers the LIFO unwind in reverse. `retry`
// override on cancel_flight fails fast in the partial case.
const sagaSpec = (suffix: string): string => `
project: p
name: saga
task_queue: typeflux-live-compensation-${suffix}
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/book_hotel: book_hotel
      p/book_flight: book_flight
      p/charge: charge
      p/cancel_hotel: cancel_hotel
      p/cancel_flight: cancel_flight
  provider: { type: openai }
activities:
  definitions:
    - name: book_hotel
      input: schemas:S
      output: schemas:S
      prompt: p/book_hotel
    - name: book_flight
      input: schemas:S
      output: schemas:S
      prompt: p/book_flight
      start_to_close_timeout_seconds: 30
    - name: charge
      input: schemas:S
      output: schemas:S
      prompt: p/charge
      retry: { maximum_attempts: 1 }
    - name: cancel_hotel
      input: schemas:S
      output: schemas:S
      prompt: p/cancel_hotel
    - name: cancel_flight
      input: schemas:S
      output: schemas:S
      prompt: p/cancel_flight
      retry: { maximum_attempts: 1 }
workflow:
  name: SagaWorkflow
  input: schemas:S
  output: schemas:S
  lifecycle: { enabled: true }
  steps:
    - id: book_hotel
      activity: book_hotel
      compensate: { activity: cancel_hotel }
    - id: book_flight
      activity: book_flight
      compensate: { activity: cancel_flight, retry: { maximum_attempts: 1 } }
    - id: charge
      activity: charge
`;

interface StatusEvent {
  event: string;
  step_id: string | null;
  compensation_status?: string;
}
interface Status {
  state: string;
  terminal_status: string | null;
  compensation_status: string | null;
  events: StatusEvent[];
}

const compensationEvents = (status: Status): [string, string | null][] =>
  status.events.filter((e) => e.event.startsWith("compensation_")).map((e) => [e.event, e.step_id]);

/** The joined message chain of a WorkflowFailedError (cause -> cause), so a test can assert the
 * ORIGINAL failure surfaced through the durable-execution wrapper. */
function causeChain(err: unknown): string {
  const parts: string[] = [];
  let e: unknown = err;
  while (e !== undefined && e !== null) {
    parts.push(String((e as { message?: unknown }).message ?? e));
    e = (e as { cause?: unknown }).cause;
  }
  return parts.join(" | ");
}

async function withSaga<T>(
  provider: SagaProvider,
  run: (ctx: { client: import("@temporalio/client").Client; runtime: Awaited<ReturnType<typeof buildRuntime>>; provider: SagaProvider }) => Promise<T>,
): Promise<T> {
  const { NativeConnection } = await import("@temporalio/worker");
  const { Client } = await import("@temporalio/client");
  const connection = await NativeConnection.connect({ address: "localhost:7233" });
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  try {
    const runtime = await buildRuntime(loadYamlSpec(sagaSpec(suffix)), {
      provider,
      schemas,
      worker: { connection },
      workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
    });
    const client = new Client();
    return await runtime.worker.runUntil(run({ client, runtime, provider }));
  } finally {
    await connection.close();
  }
}

describe.skipIf(!LIVE)("live compensation saga (#299)", () => {
  it("unwinds in reverse on failure: cancel_flight then cancel_hotel", async () => {
    const provider = new SagaProvider();
    await withSaga(provider, async ({ client, runtime }) => {
      const workflowId = `saga-fail-${Date.now()}`;
      const err = await runtime.runWorkflow(client, { v: "start" }, { workflowId }).catch((e) => e);
      expect(causeChain(err)).toMatch(/charge declined/);
      // The compensating activities ran in REVERSE step order (LIFO).
      expect(provider.order).toEqual(["cancel_flight", "cancel_hotel"]);
      const status = (await client.workflow.getHandle(workflowId).query("typeflux_lifecycle_status")) as Status;
      expect(status.compensation_status).toBe("complete");
      expect(compensationEvents(status)).toEqual([
        ["compensation_started", "book_flight"],
        ["compensation_completed", "book_flight"],
        ["compensation_started", "book_hotel"],
        ["compensation_completed", "book_hotel"],
      ]);
    });
  });

  it("records partial when a compensation fails, and still surfaces the original error", async () => {
    const provider = new SagaProvider(true); // cancel_flight throws
    await withSaga(provider, async ({ client, runtime }) => {
      const workflowId = `saga-partial-${Date.now()}`;
      // The ORIGINAL failure (charge), never the compensation failure, is what surfaces.
      const err = await runtime.runWorkflow(client, { v: "start" }, { workflowId }).catch((e) => e);
      expect(causeChain(err)).toMatch(/charge declined/);
      expect(causeChain(err)).not.toMatch(/cancel_flight failed/);
      expect(provider.order).toEqual(["cancel_flight", "cancel_hotel"]);
      const status = (await client.workflow.getHandle(workflowId).query("typeflux_lifecycle_status")) as Status;
      expect(status.compensation_status).toBe("partial");
      expect(compensationEvents(status)).toEqual([
        ["compensation_started", "book_flight"],
        ["compensation_failed", "book_flight"],
        ["compensation_started", "book_hotel"],
        ["compensation_completed", "book_hotel"],
      ]);
    });
  });

  it("unwinds on cancellation too (D299-2a): a mid-run cancel triggers the LIFO", async () => {
    const provider = new SlowFlightProvider(3000);
    await withSaga(provider, async ({ client, runtime }) => {
      const workflowId = `saga-cancel-${Date.now()}`;
      const result = runtime.runWorkflow(client, { v: "start" }, { workflowId });
      // Let book_hotel complete and book_flight start, then cancel mid-flight.
      await new Promise((resolve) => setTimeout(resolve, 800));
      await client.workflow.getHandle(workflowId).signal("typeflux_request_cancel", "operator stop");
      const err = await result.catch((e) => e);
      expect(causeChain(err)).toMatch(/TypefluxWorkflowCancelled|cancellation/);
      // Both completed bookings compensated, in reverse order.
      expect(provider.order).toEqual(["cancel_flight", "cancel_hotel"]);
      const status = (await client.workflow.getHandle(workflowId).query("typeflux_lifecycle_status")) as Status;
      expect(status.state).toBe("cancelled");
      expect(status.compensation_status).toBe("complete");
    });
  });
});

// --- Mid-fan-out failure: a map item / a parallel branch that already SUCCEEDED must still be
// compensated when a LATER unit fails (#299 review MUST-FIX 1). ---

class FanSagaProvider implements ModelProvider {
  readonly compensated: string[] = [];
  structuredCall(params: StructuredCallParams): unknown {
    const text = promptText(params);
    if (text.includes("boom")) {
      throw new Error("item boom failed");
    }
    if (text.includes("finalize")) {
      throw new Error("finalize failed");
    }
    const undo = /undo_(\w+)/.exec(text);
    if (undo) {
      this.compensated.push(undo[0]);
      return { v: "undone" };
    }
    return { v: "done" };
  }
}

async function withFanSpec<T>(
  specYaml: string,
  provider: FanSagaProvider,
  run: (ctx: { client: import("@temporalio/client").Client; runtime: Awaited<ReturnType<typeof buildRuntime>> }) => Promise<T>,
): Promise<T> {
  const { NativeConnection } = await import("@temporalio/worker");
  const { Client } = await import("@temporalio/client");
  const connection = await NativeConnection.connect({ address: "localhost:7233" });
  try {
    const runtime = await buildRuntime(loadYamlSpec(specYaml), {
      provider,
      schemas: { "schemas:S": z.object({ v: z.string() }), "schemas:SList": z.object({ results: z.array(z.object({ v: z.string() })) }), "schemas:Pair": z.object({ a: z.object({ v: z.string() }), b: z.object({ v: z.string() }) }) },
      worker: { connection },
      workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
    });
    return await runtime.worker.runUntil(run({ client: new Client(), runtime }));
  } finally {
    await connection.close();
  }
}

const mapSagaSpec = (suffix: string): string => `
project: p
name: mapsaga
task_queue: typeflux-live-mapsaga-${suffix}
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/process: "process {{v}}", p/undo: "undo_item" } }
  provider: { type: openai }
activities:
  definitions:
    - { name: process_item, input: schemas:S, output: schemas:S, prompt: p/process, retry: { maximum_attempts: 1 } }
    - { name: undo_item, input: schemas:S, output: schemas:S, prompt: p/undo }
workflow:
  name: MapSagaWorkflow
  input: schemas:SList
  output: schemas:SList
  lifecycle: { enabled: true }
  steps:
    - id: process
      map: { activity: process_item, over: input.results, concurrency: 1, collect: { output: schemas:SList, field: results } }
      compensate: { activity: undo_item }
`;

const parallelSagaSpec = (suffix: string): string => `
project: p
name: parsaga
task_queue: typeflux-live-parsaga-${suffix}
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/a: "task a", p/b: "task b", p/fin: "finalize", p/ua: "undo_a", p/ub: "undo_b" } }
  provider: { type: openai }
activities:
  definitions:
    - { name: task_a, input: schemas:S, output: schemas:S, prompt: p/a }
    - { name: task_b, input: schemas:S, output: schemas:S, prompt: p/b }
    - { name: finalize, input: schemas:Pair, output: schemas:S, prompt: p/fin, retry: { maximum_attempts: 1 } }
    - { name: undo_a, input: schemas:S, output: schemas:S, prompt: p/ua }
    - { name: undo_b, input: schemas:S, output: schemas:S, prompt: p/ub }
workflow:
  name: ParallelSagaWorkflow
  input: schemas:S
  output: schemas:S
  lifecycle: { enabled: true }
  steps:
    - id: fan
      parallel:
        branches:
          - { id: a, steps: [ { id: task_a, activity: task_a, compensate: { activity: undo_a } } ] }
          - { id: b, steps: [ { id: task_b, activity: task_b, compensate: { activity: undo_b } } ] }
        collect: { output: schemas:Pair }
    - id: finalize
      activity: finalize
`;

describe.skipIf(!LIVE)("live compensation mid-fan-out (#299)", () => {
  it("map: an item that succeeded before another item failed is still compensated", async () => {
    const provider = new FanSagaProvider();
    await withFanSpec(mapSagaSpec(`${Date.now()}-${Math.floor(Math.random() * 1e6)}`), provider, async ({ client, runtime }) => {
      const workflowId = `mapsaga-${Date.now()}`;
      // Serial (concurrency 1): item 0 succeeds, item 1 ("boom") fails -> item 0 still compensated.
      const err = await runtime
        .runWorkflow(client, { results: [{ v: "ok0" }, { v: "boom" }, { v: "ok2" }] }, { workflowId })
        .catch((e) => e);
      // The map failed (item 1 "boom"); the exact activity-failure wrapping is Temporal's, so the
      // fix proof is the COMPENSATION below, not the error string.
      expect(err).toBeDefined();
      expect(provider.compensated).toEqual(["undo_item"]); // exactly item 0's compensation ran
      const status = (await client.workflow.getHandle(workflowId).query("typeflux_lifecycle_status")) as Status;
      expect(status.compensation_status).toBe("complete");
      expect(compensationEvents(status)).toEqual([
        ["compensation_started", "process"],
        ["compensation_completed", "process"],
      ]);
    });
  });

  it("parallel: both completed branches are compensated when a later step fails", async () => {
    const provider = new FanSagaProvider();
    await withFanSpec(parallelSagaSpec(`${Date.now()}-${Math.floor(Math.random() * 1e6)}`), provider, async ({ client, runtime }) => {
      const workflowId = `parsaga-${Date.now()}`;
      const err = await runtime.runWorkflow(client, { v: "start" }, { workflowId }).catch((e) => e);
      expect(causeChain(err)).toMatch(/finalize failed/);
      // Both branches completed and pushed; the later finalize failure unwinds BOTH.
      expect([...provider.compensated].sort()).toEqual(["undo_a", "undo_b"]);
      const status = (await client.workflow.getHandle(workflowId).query("typeflux_lifecycle_status")) as Status;
      expect(status.compensation_status).toBe("complete");
    });
  });
});
