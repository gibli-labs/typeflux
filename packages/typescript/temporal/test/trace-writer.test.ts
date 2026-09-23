import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { EmittedTrace, ModelProvider, StructuredCallParams, TraceTransport } from "../src/index.js";
import { defineActivity, executeActivity, TraceWriter } from "../src/index.js";

class FakeProvider implements ModelProvider {
  constructor(private readonly responses: unknown[]) {}
  structuredCall(_params: StructuredCallParams): unknown {
    if (this.responses.length === 0) {
      throw new Error("FakeProvider exhausted");
    }
    const response = this.responses.shift();
    if (response instanceof Error) {
      throw response;
    }
    return response;
  }
}

class CapturingTransport implements TraceTransport {
  traces: EmittedTrace[] = [];
  submitTrace(trace: EmittedTrace): void {
    this.traces.push(trace);
  }
}

const messages = [{ role: "user", content: "go" }];

function activity(extra: Record<string, unknown> = {}) {
  return defineActivity({
    name: "summarize",
    prompt: { name: "p/summarize", label: "production" },
    input: z.object({ text: z.string() }),
    output: z.object({ summary: z.string() }),
    ...extra,
  });
}

describe("TraceWriter (#451)", () => {
  it("emits a trace with generation + hook observations on flush", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);
    const hooked = activity({ hook: (_i: unknown, o: { summary: string }) => ({ summary: o.summary.toUpperCase() }) });

    const out = await executeActivity(hooked, { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: writer,
    });
    expect(out).toEqual({ summary: "OK" });

    expect(transport.traces).toHaveLength(0); // nothing sent until flush
    await writer.flush();
    expect(transport.traces).toHaveLength(1);
    const trace = transport.traces[0]!;
    expect(trace).toMatchObject({
      name: "summarize",
      input: { text: "x" },
      output: { summary: "OK" },
      metadata: { cacheHit: false },
      tenant: {},
    });
    expect(trace.observations).toEqual([
      { type: "generation", model: null, input: messages, attempt: 0, output: { summary: "ok" } },
      // The hook span records the pre-hook model output (`modelOutput`) AND the hook result.
      { type: "hook", input: { text: "x" }, modelOutput: { summary: "ok" }, output: { summary: "OK" } },
    ]);
  });

  it("emits provider-reported usage as generation metadata (#478)", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);
    const reporting: ModelProvider = {
      structuredCall(params: StructuredCallParams): unknown {
        params.usageSink?.({ inputTokens: 100, cacheReadTokens: 90 });
        return { summary: "ok" };
      },
    };
    await executeActivity(activity(), { text: "x" }, {
      provider: reporting,
      messages,
      observer: writer,
    });
    await writer.flush();
    const generation = transport.traces[0]?.observations.find((o) => o.type === "generation");
    expect(generation?.metadata).toEqual({ usage: { inputTokens: 100, cacheReadTokens: 90 } });
  });

  it("emits an error trace (serialized) when the activity fails", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);
    await expect(
      executeActivity(activity(), { text: "x" }, {
        provider: new FakeProvider([new Error("provider boom")]),
        messages,
        observer: writer,
      }),
    ).rejects.toThrow(/provider boom/);

    await writer.flush();
    const trace = transport.traces[0]!;
    expect(trace.error).toBe("provider boom"); // Error name "Error" -> bare message
    expect(trace.output).toBeUndefined();
    expect(trace.observations[0]).toMatchObject({ type: "generation", error: "provider boom" });
  });

  it("re-buffers traces on a submitTrace failure so flush can be retried", async () => {
    let down = true;
    const sent: EmittedTrace[] = [];
    const transport: TraceTransport = {
      submitTrace: (trace) => {
        if (down) {
          throw new Error("transport down");
        }
        sent.push(trace);
      },
    };
    const writer = new TraceWriter(transport);
    await executeActivity(activity(), { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: writer,
    });

    await expect(writer.flush()).rejects.toThrow(/transport down/);
    expect(sent).toHaveLength(0);
    down = false;
    await writer.flush(); // the re-buffered trace is retried
    expect(sent).toHaveLength(1);
  });

  it("emits a failed hook with an error and no stale output", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);
    const hooked = activity({
      hook: () => {
        throw new Error("hook boom");
      },
    });
    await expect(
      executeActivity(hooked, { text: "x" }, {
        provider: new FakeProvider([{ summary: "ok" }]),
        messages,
        observer: writer,
      }),
    ).rejects.toThrow(/hook boom/);

    await writer.flush();
    const hook = transport.traces[0]!.observations.find((o) => o.type === "hook")!;
    expect(hook.error).toBe("hook boom");
    expect("output" in hook).toBe(false); // no stale pre-hook output
  });

  it("redacts PII from emitted traces when a redaction config is set", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport, {}); // default redaction (all rules on)
    await executeActivity(activity(), { text: "email me at a@b.com" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: writer,
    });
    await writer.flush();
    expect(transport.traces[0]!.input).toEqual({ text: "email me at [REDACTED_EMAIL]" });
  });

  it("drains the buffer on flush (a second flush sends nothing)", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);
    await executeActivity(activity(), { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: writer,
    });
    await writer.flush();
    await writer.flush();
    expect(transport.traces).toHaveLength(1);
  });
});

describe("TraceWriter ended-only flushing", () => {
  it("leaves in-flight activities buffered so a mid-run flush cannot emit them half-complete", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);

    // Two overlapping activities (a map step's siblings): one ends, one is
    // still executing when a flush runs.
    const done = writer.observeActivity({
      activityName: "done",
      input: { n: 1 },
      messages: [],
      model: null,
      tenant: {},
    });
    done.updateOutput({ n: 1 });
    done.end();
    const inFlight = writer.observeActivity({
      activityName: "in-flight",
      input: { n: 2 },
      messages: [],
      model: null,
      tenant: {},
    });

    await writer.flush();
    expect(transport.traces.map((trace) => trace.name)).toEqual(["done"]);

    // The sibling ships COMPLETE once it ends — never dropped, never partial.
    inFlight.updateOutput({ n: 2 });
    inFlight.end();
    await writer.flush();
    expect(transport.traces.map((trace) => trace.name)).toEqual(["done", "in-flight"]);
    expect(transport.traces[1]!.output).toEqual({ n: 2 });
  });
});

describe("execution manifest on observation metadata (Python parity)", () => {
  it("attaches typeflux.activity_execution_manifest + join keys to the activity trace", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);

    await executeActivity(activity(), { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: writer,
    });
    await writer.flush();

    const metadata = transport.traces[0]!.metadata as Record<string, unknown>;
    const manifest = metadata["typeflux.activity_execution_manifest"] as Record<string, unknown>;
    expect(manifest).toBeDefined();
    // The reconstruction-critical fields: prompt provenance + identity.
    expect(manifest["activity_name"]).toBe("summarize");
    expect(manifest["prompt_ref"]).toMatchObject({ name: "p/summarize", label: "production" });
    // Pre-rendered messages path: no registry resolution — the null version
    // is DROPPED from the payload (Python to_dict drops nulls).
    expect(manifest["resolved_prompt_version"]).toBeUndefined();
    expect((manifest["input_schema"] as Record<string, unknown>)["hash"]).toBeTypeOf("string");
    expect((manifest["output_schema"] as Record<string, unknown>)["hash"]).toBeTypeOf("string");
    expect(manifest["rendered_messages_hash"]).toBeTypeOf("string");
    expect(manifest["manifest_hash"]).toBeTypeOf("string");
    // Flat join keys (Python semantic-metadata parity).
    expect(metadata["typeflux.activity_name"]).toBe("summarize");
    expect(metadata["typeflux.manifest_hash"]).toBe(manifest["manifest_hash"]);
  });

  it("stamps the Temporal identity join keys from the worker context (#686)", async () => {
    // A control-plane-started run has no caller-side parent metadata: the
    // activity spans' `temporal.workflow_id` (Python's own flat key) is what a
    // correlation lookup joins the trace to its execution id from.
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);

    await executeActivity(activity(), { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: writer,
      context: { workflowId: "exec-42", runId: "run-42" },
    });
    await writer.flush();

    const metadata = transport.traces[0]!.metadata as Record<string, unknown>;
    expect(metadata["temporal.workflow_id"]).toBe("exec-42");
    expect(metadata["temporal.run_id"]).toBe("run-42");

    // Without a worker context there is no identity to stamp — the keys are absent, never null.
    const bare = new CapturingTransport();
    const bareWriter = new TraceWriter(bare);
    await executeActivity(activity(), { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      messages,
      observer: bareWriter,
    });
    await bareWriter.flush();
    const bareMetadata = bare.traces[0]!.metadata as Record<string, unknown>;
    expect(Object.hasOwn(bareMetadata, "temporal.workflow_id")).toBe(false);
    expect(Object.hasOwn(bareMetadata, "temporal.run_id")).toBe(false);
  });

  it("records the registry's resolved prompt version and schema names", async () => {
    const transport = new CapturingTransport();
    const writer = new TraceWriter(transport);
    const named = defineActivity({
      name: "summarize",
      prompt: { name: "p/summarize", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ summary: z.string() }),
      inputSchemaName: "Ticket",
      outputSchemaName: "Summary",
      definitionSource: { kind: "yaml", yamlName: "demo", yamlProject: "proj" },
    });

    await executeActivity(named, { text: "x" }, {
      provider: new FakeProvider([{ summary: "ok" }]),
      registry: {
        resolve: async () => ({
          ref: { name: "p/summarize", label: "production" },
          messages: [{ role: "user", content: "summarize {{text}}" }],
          resolvedVersion: "7",
        }),
      },
      observer: writer,
    });
    await writer.flush();

    const manifest = (transport.traces[0]!.metadata as Record<string, unknown>)[
      "typeflux.activity_execution_manifest"
    ] as Record<string, unknown>;
    expect(manifest["resolved_prompt_version"]).toBe("7");
    expect((manifest["input_schema"] as Record<string, unknown>)["name"]).toBe("Ticket");
    expect((manifest["output_schema"] as Record<string, unknown>)["name"]).toBe("Summary");
    expect(manifest["definition_source"]).toMatchObject({ kind: "yaml", yaml_name: "demo", yaml_project: "proj" });
    // The template hash differs from the rendered hash (the input rendered in).
    expect(manifest["prompt_messages_hash"]).not.toBe(manifest["rendered_messages_hash"]);
  });
});
