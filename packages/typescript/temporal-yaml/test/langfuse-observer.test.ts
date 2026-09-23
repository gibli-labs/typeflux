/**
 * Out-of-the-box langfuse tracing (`langfuse-observer.ts`): the SDK transport
 * mapping, the streaming writer's per-activity flush + drain, and the
 * spec/env gate `buildRuntime` calls. The SDK client is faked structurally
 * (`LangfuseSdkClient`); the real `langfuse` package is only touched by the
 * construct-only test (no network — the guard would block it anyway).
 */

import { describe, expect, it, vi } from "vitest";

import {
  langfuseCredentialsFromEnv,
  langfuseCredentialsFromSpec,
  langfuseObserverFromSpec,
  LangfuseSdkTransport,
  loadYamlSpec,
  StreamingTraceWriter,
} from "../src/index.js";
import type { LangfuseSdkClient } from "../src/index.js";

const YAML = `
project: obs_demo
name: obs_demo
task_queue: obs-demo
runtime:
  temporal:
    address: localhost:7233
  registry:
    type: inline
    prompts:
      p: "hi {{value}}"
  provider:
    type: openai
  observability:
    type: langfuse
activities:
  definitions:
    - name: act
      input: schemas:Item
      output: schemas:Item
      prompt: p
workflow:
  name: ObsDemoWorkflow
  input: schemas:Item
  output: schemas:Item
  steps:
    - id: act
      activity: act
`;

function specWithObservability(type: string | undefined) {
  const yaml =
    type === undefined
      ? YAML.replace("  observability:\n    type: langfuse\n", "")
      : YAML.replace("type: langfuse", `type: ${type}`);
  return loadYamlSpec(yaml);
}

class FakeClient implements LangfuseSdkClient {
  traces: Record<string, unknown>[] = [];
  generations: Record<string, unknown>[] = [];
  spans: Record<string, unknown>[] = [];
  flushes = 0;
  failFlushes = 0;

  private observationClient() {
    const self = this;
    return {
      generation: (g: Record<string, unknown>) => self.generations.push(g),
      span: (s: Record<string, unknown>) => {
        self.spans.push(s);
        return self.observationClient();
      },
    };
  }

  trace(body: Record<string, unknown>) {
    this.traces.push(body);
    return this.observationClient();
  }

  async flushAsync(): Promise<void> {
    if (this.failFlushes > 0) {
      this.failFlushes -= 1;
      throw new Error("simulated langfuse outage");
    }
    this.flushes += 1;
  }
}

describe("LangfuseSdkTransport", () => {
  it("maps a trace with generations, hooks, error, and tenant to SDK calls", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client);

    await transport.submitTrace({
      name: "act",
      input: { value: "in" },
      output: { value: "out" },
      error: "boom",
      metadata: { "typeflux.prompt": "p" },
      tenant: { org: "acme" },
      observations: [
        {
          type: "generation",
          model: "gpt-x",
          input: [{ role: "user", content: "hi" }],
          output: { value: "raw" },
          attempt: 0,
          metadata: { usage: { cache: true } },
        },
        {
          type: "hook",
          input: { value: "raw" },
          modelOutput: { value: "pre-hook" },
          output: { value: "out" },
          error: "hook failed",
        },
      ],
    });

    const [trace] = client.traces;
    expect(trace!["name"]).toBe("act");
    // Error ships in metadata (the trace body has no status field) + a tag.
    expect(trace!["metadata"]).toMatchObject({ error: "boom", tenant: { org: "acme" } });
    expect(trace!["tags"]).toEqual(["error"]);

    const [generation] = client.generations;
    expect(generation!["model"]).toBe("gpt-x");
    expect(generation!["metadata"]).toEqual({ usage: { cache: true }, validation_attempt: 0 });

    const [span] = client.spans;
    expect(span!["name"]).toBe("act:hook");
    // The pre-hook model output is auditable from the span.
    expect(span!["metadata"]).toMatchObject({ llm_output: { value: "pre-hook" } });
    expect(span!["level"]).toBe("ERROR");
    expect(span!["statusMessage"]).toBe("hook failed");

    // Deterministic egress: one flushAsync per submitted trace.
    expect(client.flushes).toBe(1);
  });
});

function recordOneActivityNamed(writer: StreamingTraceWriter, name: string): void {
  const observation = writer.observeActivity({
    activityName: name,
    input: { value: "in" },
    messages: [],
    model: null,
    tenant: {},
  });
  observation.updateOutput({ value: "out" });
  observation.end();
}

describe("LangfuseSdkTransport grouping", () => {
  const RUN = { workflowId: "wf-1", runId: "0199aaaa-bbbb-cccc-dddd-eeeeffff0001" };

  it("groups activities of one workflow run under a single run-scoped trace", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "insurance_claim_review");

    for (const name of ["review_evidence_item", "consolidate_claim_review"]) {
      await transport.submitTrace({
        name,
        input: { value: "in" },
        output: { value: "out" },
        metadata: {},
        tenant: {},
        observations: [
          { type: "generation", model: "m", input: [], attempt: 0, output: { ok: true } },
        ],
        ...RUN,
      });
    }

    // Both submits target the SAME trace id (langfuse upserts the shell) with
    // the cross-edition parent identity: name + the read-path tag.
    expect(client.traces).toHaveLength(2);
    for (const trace of client.traces) {
      expect(trace["id"]).toBe(RUN.runId);
      expect(trace["name"]).toBe("TypefluxWorkflow:insurance_claim_review");
      expect(trace["tags"]).toEqual(["typeflux.workflow:insurance_claim_review"]);
      // The activity payload lives on the SPAN, not the trace shell.
      expect(trace["input"]).toBeUndefined();
    }
    expect(client.spans.map((span) => span["name"])).toEqual([
      "review_evidence_item",
      "consolidate_claim_review",
    ]);
    expect(client.generations).toHaveLength(2);
  });

  it("a failed activity marks its span ERROR without touching the shared trace tags", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");

    await transport.submitTrace({
      name: "act",
      input: {},
      error: "boom",
      metadata: {},
      tenant: {},
      observations: [],
      ...RUN,
    });

    expect(client.traces[0]!["tags"]).toEqual(["typeflux.workflow:wf"]);
    expect(client.spans[0]!).toMatchObject({ level: "ERROR", statusMessage: "boom" });
  });

  it("falls back to an activity-rooted trace without a run id (standalone execute)", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");

    await transport.submitTrace({
      name: "act",
      input: { value: "in" },
      metadata: {},
      tenant: {},
      observations: [],
    });

    expect(client.traces[0]!["name"]).toBe("act");
    expect(client.traces[0]!["id"]).toBeUndefined();
    expect(client.spans).toHaveLength(0);
  });
});

describe("LangfuseSdkTransport recordWorkflowRun", () => {
  it("upserts the parent shell with input at start and output at completion", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");
    const runId = "0199aaaa-bbbb-cccc-dddd-eeeeffff0010";

    await transport.recordWorkflowRun({
      runId,
      workflowId: "case-1",
      input: { claim: "c" },
      metadata: { typeflux_project: "p", workflow_id: "case-1" },
    });
    await transport.recordWorkflowRun({ runId, workflowId: "case-1", output: { verdict: "ok" } });

    expect(client.traces).toHaveLength(2);
    expect(client.traces[0]!).toMatchObject({
      id: runId,
      name: "TypefluxWorkflow:wf",
      input: { claim: "c" },
      metadata: { typeflux_project: "p", workflow_id: "case-1" },
    });
    expect(client.traces[1]!).toMatchObject({ id: runId, output: { verdict: "ok" } });
    // A failed run's error lands in parent metadata.
    await transport.recordWorkflowRun({ runId, workflowId: "case-1", error: "boom" });
    expect(client.traces[2]!["metadata"]).toMatchObject({ error: "boom" });
  });

  it("sets native userId to the primary subject and a tag per subject (#715)", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");
    await transport.recordWorkflowRun({
      runId: "0199aaaa-bbbb-cccc-dddd-eeeeffff0011",
      workflowId: "case-2",
      input: { claim: "c" },
      subjectIds: ["pt-1", "pt-2"],
    });
    expect(client.traces[0]!).toMatchObject({ userId: "pt-1" });
    expect(client.traces[0]!["tags"]).toEqual([
      "typeflux.workflow:wf",
      "typeflux.subject:pt-1",
      "typeflux.subject:pt-2",
    ]);
  });

  it("omits userId when there are no subjects (byte-unchanged from pre-#715)", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");
    await transport.recordWorkflowRun({ runId: "r", workflowId: "case-3", input: {} });
    expect("userId" in client.traces[0]!).toBe(false);
    expect(client.traces[0]!["tags"]).toEqual(["typeflux.workflow:wf"]);
  });

  it("re-applies subject identity on completion/error upserts that omit it (#715 review, finding 3)", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");
    const runId = "0199aaaa-bbbb-cccc-dddd-eeeeffff0012";
    await transport.recordWorkflowRun({ runId, workflowId: "case-4", input: {}, subjectIds: ["pt-1", "pt-2"] });
    // The completion record carries NO subjectIds — a Langfuse upsert replaces the
    // fields it carries, so without the remembered re-apply the final trace would
    // lose userId and the subject tags.
    await transport.recordWorkflowRun({ runId, workflowId: "case-4", output: { ok: true } });
    await transport.recordWorkflowRun({ runId, workflowId: "case-4", error: "boom" });
    for (const trace of client.traces) {
      expect(trace["userId"]).toBe("pt-1");
      expect(trace["tags"]).toEqual(["typeflux.workflow:wf", "typeflux.subject:pt-1", "typeflux.subject:pt-2"]);
    }
  });

  it("a grouped activity's trace-shell upsert re-carries the run's subject identity (#715)", async () => {
    const client = new FakeClient();
    const transport = new LangfuseSdkTransport(client, "wf");
    const runId = "0199aaaa-bbbb-cccc-dddd-eeeeffff0013";
    await transport.recordWorkflowRun({ runId, workflowId: "case-5", input: {}, subjectIds: ["pt-9"] });
    await transport.submitTrace({
      name: "assess",
      runId,
      input: { id: "x" },
      output: { ok: true },
      tenant: {},
      metadata: {},
      observations: [],
    });
    // The activity's grouped upsert of the SAME trace shell must not strip the identity.
    const activityShell = client.traces.at(-1)!;
    expect(activityShell["userId"]).toBe("pt-9");
    expect(activityShell["tags"]).toEqual(["typeflux.workflow:wf", "typeflux.subject:pt-9"]);
  });
});

describe("StreamingTraceWriter recordWorkflowRun", () => {
  it("redacts workflow input/output before the transport and never throws", async () => {
    const client = new FakeClient();
    const writer = new StreamingTraceWriter(new LangfuseSdkTransport(client, "wf"), {});

    await writer.recordWorkflowRun({
      runId: "0199aaaa-bbbb-cccc-dddd-eeeeffff0011",
      workflowId: "case-1",
      input: { contact: "pii@example.com" },
    });
    // Fire-and-forget: the record ships via the chain — drain awaits it.
    await writer.drain();
    expect(JSON.stringify(client.traces[0]!["input"])).not.toContain("pii@example.com");

    // The failure message is redacted too (codex P1) — a workflow error
    // carrying PII never reaches the backend raw.
    await writer.recordWorkflowRun({
      runId: "0199aaaa-bbbb-cccc-dddd-eeeeffff0012",
      workflowId: "case-1",
      error: "lookup failed for pii@example.com",
    });
    await writer.drain();
    const errored = client.traces.at(-1)!;
    expect(JSON.stringify(errored["metadata"])).not.toContain("pii@example.com");

    // A transport without the recorder capability is a silent no-op.
    const bare = new StreamingTraceWriter({ submitTrace: async () => {} }, {});
    await expect(
      bare.recordWorkflowRun({ runId: "r", workflowId: "w", input: {} }),
    ).resolves.toBeUndefined();
  });
});

describe("StreamingTraceWriter", () => {
  const recordOneActivity = (writer: StreamingTraceWriter): void =>
    recordOneActivityNamed(writer, "act");

  it("streams each completed activity without a manual flush", async () => {
    const client = new FakeClient();
    const writer = new StreamingTraceWriter(new LangfuseSdkTransport(client));

    recordOneActivity(writer);
    await writer.drain();

    expect(client.traces).toHaveLength(1);
    expect(client.traces[0]!["output"]).toEqual({ value: "out" });
  });

  it("logs flush failures without breaking activities, and drain retries", async () => {
    const client = new FakeClient();
    client.failFlushes = 1; // the streamed flush fails once
    const writer = new StreamingTraceWriter(new LangfuseSdkTransport(client));
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      recordOneActivity(writer); // must not throw despite the outage
      await writer.drain(); // re-buffered trace ships on the retry

      expect(errors).toHaveBeenCalledWith("langfuse trace flush failed:", expect.any(Error));
      expect(client.traces.map((trace) => trace["name"])).toEqual(["act", "act"]);
      // The failed attempt re-buffered; exactly one DURABLE flush landed it.
      expect(client.flushes).toBe(1);
    } finally {
      errors.mockRestore();
    }
  });
});

describe("langfuseObserverFromSpec", () => {
  const KEYS = { LANGFUSE_PUBLIC_KEY: "pk-test", LANGFUSE_SECRET_KEY: "sk-test" };

  it("returns undefined for none/unset and for non-langfuse types", async () => {
    await expect(langfuseObserverFromSpec(specWithObservability("none"), KEYS)).resolves.toBeUndefined();
    await expect(langfuseObserverFromSpec(specWithObservability(undefined), KEYS)).resolves.toBeUndefined();
    // langsmith has its own module; custom keeps the injected-transport seam.
    await expect(langfuseObserverFromSpec(specWithObservability("langsmith"), KEYS)).resolves.toBeUndefined();
  });

  it("degrades to untraced with one warning when the credentials are absent", async () => {
    const warnings = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const observer = await langfuseObserverFromSpec(specWithObservability("langfuse"), {});
      expect(observer).toBeUndefined();
      expect(warnings).toHaveBeenCalledOnce();
      expect(String(warnings.mock.calls[0])).toContain("LANGFUSE_PUBLIC_KEY");
    } finally {
      warnings.mockRestore();
    }
  });

  it("spec-declared credentials win field-by-field with env fallback (#793)", () => {
    const spec = loadYamlSpec(
      YAML.replace(
        "  observability:\n    type: langfuse\n",
        "  observability:\n    type: langfuse\n    langfuse:\n" +
          "      public_key: pk-from-spec\n" +
          "      secret_key: { value_from: { env: TEAM_LF_SECRET } }\n",
      ),
    );
    // Declared fields resolve from the spec; the undeclared host falls back to env.
    expect(
      langfuseCredentialsFromSpec(spec, {
        TEAM_LF_SECRET: " sk-from-team ",
        LANGFUSE_PUBLIC_KEY: "pk-env-loses",
        LANGFUSE_HOST: "https://env-host",
      }),
    ).toEqual({ publicKey: "pk-from-spec", secretKey: "sk-from-team", baseUrl: "https://env-host" });
    // A declared-but-unresolvable required source THROWS — never a silent env fallthrough.
    expect(() => langfuseCredentialsFromSpec(spec, { LANGFUSE_PUBLIC_KEY: "pk", LANGFUSE_SECRET_KEY: "sk" })).toThrow(
      /missing required secret for runtime\.observability\.langfuse\.secret_key/,
    );
    // No spec block: behaves exactly like the env detection.
    expect(
      langfuseCredentialsFromSpec(specWithObservability("langfuse"), {
        LANGFUSE_PUBLIC_KEY: "pk-env",
        LANGFUSE_SECRET_KEY: "sk-env",
      }),
    ).toEqual({ publicKey: "pk-env", secretKey: "sk-env" });
  });

  it("treats whitespace-only credentials as unconfigured (#715 Bugbot)", async () => {
    // A padded/blank key must never build a client that fails at runtime; the
    // configured-detection strips before checking.
    expect(langfuseCredentialsFromEnv({})).toBeUndefined();
    expect(
      langfuseCredentialsFromEnv({ LANGFUSE_PUBLIC_KEY: "   ", LANGFUSE_SECRET_KEY: "\t" }),
    ).toBeUndefined();
    expect(
      langfuseCredentialsFromEnv({ LANGFUSE_PUBLIC_KEY: "pk-test", LANGFUSE_SECRET_KEY: "  " }),
    ).toBeUndefined();
    // Padded-but-real keys resolve TRIMMED — check and use are the same values.
    expect(
      langfuseCredentialsFromEnv({
        LANGFUSE_PUBLIC_KEY: " pk-test ",
        LANGFUSE_SECRET_KEY: " sk-test ",
        LANGFUSE_HOST: " http://localhost:9 ",
      }),
    ).toEqual({ publicKey: "pk-test", secretKey: "sk-test", baseUrl: "http://localhost:9" });
    // The observer gate uses the same detection: whitespace-only degrades untraced.
    const warnings = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const observer = await langfuseObserverFromSpec(specWithObservability("langfuse"), {
        LANGFUSE_PUBLIC_KEY: "   ",
        LANGFUSE_SECRET_KEY: "   ",
      });
      expect(observer).toBeUndefined();
      expect(warnings).toHaveBeenCalledOnce();
    } finally {
      warnings.mockRestore();
    }
  });

  it("builds a streaming writer over the official SDK when the spec + env opt in", async () => {
    // Construct-only: no flush is triggered, so no network leaves the test.
    const observer = await langfuseObserverFromSpec(specWithObservability("langfuse"), {
      ...KEYS,
      LANGFUSE_HOST: "http://localhost:9",
    });
    expect(observer).toBeInstanceOf(StreamingTraceWriter);
  });
});

describe("StreamingTraceWriter under concurrency", () => {
  it("a streamed flush never ships a concurrent sibling that has not ended", async () => {
    const client = new FakeClient();
    const writer = new StreamingTraceWriter(new LangfuseSdkTransport(client));

    const sibling = writer.observeActivity({
      activityName: "sibling",
      input: { n: 2 },
      messages: [],
      model: null,
      tenant: {},
    });
    recordOneActivityNamed(writer, "first"); // ends → streams a flush
    await writer.drain();
    // Only the ended activity shipped; the in-flight sibling stayed buffered.
    expect(client.traces.map((trace) => trace["name"])).toEqual(["first"]);

    sibling.updateOutput({ n: 2 });
    sibling.end();
    await writer.drain();
    expect(client.traces.map((trace) => trace["name"])).toEqual(["first", "sibling"]);
    expect(client.traces[1]!["output"]).toEqual({ n: 2 });
  });
});
