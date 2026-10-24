import { createHash } from "node:crypto";
import { trace } from "@opentelemetry/api";
import { InMemorySpanExporter, type ReadableSpan } from "@opentelemetry/sdk-trace";
import { describe, expect, it } from "vitest";
import { createLangfuseOtelClient } from "../src/langfuse-otel.js";
import { LangfuseSdkTransport } from "../src/langfuse-observer.js";
import { StreamingTraceWriter } from "../src/streaming-trace-writer.js";

const credentials = { publicKey: "pk-test", secretKey: "sk-test", baseUrl: "http://localhost:9" };

describe("Langfuse 6 real OTel export", () => {
  it("exports redacted root IO, parented generations and updated subject identity without replacing global tracing", async () => {
    const globalProvider = trace.getTracerProvider();
    const exporter = new InMemorySpanExporter();
    const client = await createLangfuseOtelClient(credentials, exporter);
    const transport = new LangfuseSdkTransport(client, "review");
    const writer = new StreamingTraceWriter(transport, {});
    try {
      await writer.recordWorkflowRun({ runId: "run-1", workflowId: "wf-1", input: { email: "person@example.com" }, subjectIds: ["s1"] });
      await writer.drain();
      await transport.submitTrace({ name: "assess", runId: "run-1", input: {}, output: { ok: true }, metadata: {}, tenant: {},
        observations: [{ type: "generation", input: [], output: "ok", model: "gpt-test", attempt: 0 }] });
      await writer.recordWorkflowRun({ runId: "run-1", workflowId: "wf-1", output: { email: "person@example.com" } });
      await writer.drain();
      const spans = exporter.getFinishedSpans();
      expect(spans).toHaveLength(3);
      const root = spans.find((s) => s.name === "TypefluxWorkflow:review")!;
      const activity = spans.find((s) => s.name === "assess")!;
      const generation = spans.find((s) => s.name === "assess:generation")!;
      expect(root.parentSpanContext).toBeUndefined();
      expect(activity.parentSpanContext?.spanId).toBe(root.spanContext().spanId);
      expect(generation.parentSpanContext?.spanId).toBe(activity.spanContext().spanId);
      expect(new Set(spans.map((s) => s.spanContext().traceId))).toEqual(new Set([createHash("sha256").update("run-1").digest("hex").slice(0, 32)]));
      expect(root.attributes["langfuse.trace.tags"]).toEqual(["typeflux.workflow:review", "typeflux.subject:s1"]);
      expect(JSON.stringify(root.attributes)).not.toContain("person@example.com");
      expect(root.attributes["langfuse.observation.input"]).toBeDefined();
      expect(root.attributes["langfuse.observation.output"]).toBeDefined();
      expect(generation.attributes["langfuse.observation.type"]).toBe("generation");
      expect(trace.getTracerProvider()).toBe(globalProvider);
    } finally { await writer.shutdown(); }
  });

  it("updates a worker-created root when workflow identity arrives, and isolates providers", async () => {
    const exporter = new InMemorySpanExporter();
    const otherExporter = new InMemorySpanExporter();
    const closed: ReadableSpan[] = [];
    const closeExporter = exporter.shutdown.bind(exporter);
    exporter.shutdown = async () => { closed.push(...exporter.getFinishedSpans()); await closeExporter(); };
    const client = await createLangfuseOtelClient(credentials, exporter);
    const other = await createLangfuseOtelClient({ ...credentials, publicKey: "pk-other" }, otherExporter);
    const transport = new LangfuseSdkTransport(client, "review");
    try {
      await transport.submitTrace({ runId: "run-late", name: "act", input: {}, metadata: {}, tenant: {}, observations: [] });
      await transport.recordWorkflowRun({ runId: "run-late", workflowId: "wf", input: {}, subjectIds: ["s2"] });
      await transport.shutdown();
      const root = closed.find((s) => s.name === "TypefluxWorkflow:review")!;
      expect(root.attributes["langfuse.trace.tags"]).toContain("typeflux.subject:s2");
      expect(root.attributes["user.id"]).toBe("s2");
      expect(closed).toHaveLength(2);
      expect(otherExporter.getFinishedSpans()).toHaveLength(0);
    } finally { await other.shutdown(); }
  });
});
