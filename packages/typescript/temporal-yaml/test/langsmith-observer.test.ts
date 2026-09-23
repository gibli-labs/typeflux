/**
 * Out-of-the-box LangSmith tracing (`langsmith-observer.ts`): the run-tree
 * transport mapping and the spec/env gate `buildRuntime` calls — the mirror
 * of `langfuse-observer.test.ts`. The SDK is faked structurally
 * (`LangsmithSdkModule`); the real `langsmith` package is only touched by the
 * construct-only test (no network).
 */

import { describe, expect, it, vi } from "vitest";

import {
  langsmithObserverFromSpec,
  LangsmithSdkTransport,
  loadYamlSpec,
  StreamingTraceWriter,
} from "../src/index.js";
import type { LangsmithRunTree, LangsmithSdkClient } from "../src/index.js";

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
    type: langsmith
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
      ? YAML.replace("  observability:\n    type: langsmith\n", "")
      : YAML.replace("type: langsmith", `type: ${type}`);
  return loadYamlSpec(yaml);
}

interface RecordedRun {
  config: Record<string, unknown>;
  outputs?: Record<string, unknown> | undefined;
  error?: string | undefined;
  posted: boolean;
  patched: boolean;
  children: RecordedRun[];
}

class FakeRunTree implements LangsmithRunTree {
  readonly run: RecordedRun;
  /** Mimics the SDK: createChild links the child onto the parent. */
  child_runs: FakeRunTree[] = [];
  /** Mimics the SDK: end() keeps the FIRST value; assignment extends. */
  end_time?: number;
  /** Assignable run fields, patched by patchRun (SDK parity). */
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  error?: string;
  extra?: Record<string, unknown>;

  constructor(config: Record<string, unknown>, parentChildren?: RecordedRun[]) {
    this.run = { config, posted: false, patched: false, children: [] };
    const extra = config["extra"] as Record<string, unknown> | undefined;
    if (extra !== undefined) {
      this.extra = extra;
    }
    const inputs = config["inputs"] as Record<string, unknown> | undefined;
    if (inputs !== undefined) {
      this.inputs = inputs;
    }
    parentChildren?.push(this.run);
  }

  createChild(config: Record<string, unknown>): FakeRunTree {
    const child = new FakeRunTree(config, this.run.children);
    this.child_runs.push(child);
    return child;
  }

  async postRun(): Promise<void> {
    this.run.posted = true;
  }

  async patchRun(): Promise<void> {
    this.run.patched = true;
  }

  end(outputs?: Record<string, unknown>, error?: string): void {
    this.run.outputs = outputs;
    this.run.error = error;
    this.end_time = this.end_time ?? Date.now();
  }
}

class FakeClient implements LangsmithSdkClient {
  batchesAwaited = 0;
  async awaitPendingTraceBatches(): Promise<void> {
    this.batchesAwaited += 1;
  }
}

function transportWith(
  client: FakeClient,
  workflowName?: string,
): { transport: LangsmithSdkTransport; roots: FakeRunTree[] } {
  const roots: FakeRunTree[] = [];
  class CapturingRunTree extends FakeRunTree {
    constructor(config: Record<string, unknown>) {
      super(config);
      roots.push(this);
    }
  }
  const transport = new LangsmithSdkTransport(
    client,
    CapturingRunTree as unknown as new (config: Record<string, unknown>) => LangsmithRunTree,
    "proj",
    workflowName,
  );
  return { transport, roots };
}

describe("LangsmithSdkTransport grouping", () => {
  const RUN = { workflowId: "wf-1", runId: "0199aaaa-bbbb-cccc-dddd-eeeeffff0002" };

  it("groups activities of one workflow run under a single posted root run", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client, "insurance_claim_review");

    for (const name of ["review_evidence_item", "consolidate_claim_review"]) {
      await transport.submitTrace({
        name,
        input: { value: "in" },
        output: { value: "out" },
        metadata: {},
        tenant: {},
        observations: [],
        ...RUN,
      });
    }

    // ONE root run tree, posted once, carrying the run id + parent identity;
    // both activities are its children.
    expect(roots).toHaveLength(1);
    expect(roots[0]!.run.config).toMatchObject({
      id: RUN.runId,
      name: "TypefluxWorkflow:insurance_claim_review",
      run_type: "chain",
    });
    expect(roots[0]!.run.posted).toBe(true);
    expect(roots[0]!.run.children.map((child) => child.config["name"])).toEqual([
      "review_evidence_item",
      "consolidate_claim_review",
    ]);
    // Activities NEVER patch the root — LangSmith allows ONE update per run,
    // and that update belongs to the completion record.
    expect(roots[0]!.run.patched).toBe(false);
  });

  it("detaches posted activity subtrees from the retained root (memory bound)", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client, "wf");

    for (let i = 0; i < 3; i++) {
      await transport.submitTrace({
        name: `act-${i}`,
        input: {},
        metadata: {},
        tenant: {},
        observations: [],
        ...RUN,
      });
    }

    // The recorded children prove delivery, but the LIVE root object holds
    // none of them — a long-running workflow cannot grow the transport heap.
    expect(roots).toHaveLength(1);
    expect(roots[0]!.child_runs).toHaveLength(0);
  });

  it("recordWorkflowRun seeds the root's CREATE and spends ONE final patch", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client, "wf");

    // Start: the root is CREATED with the workflow input + identity metadata
    // (no patch — LangSmith allows one update per run, saved for completion).
    await transport.recordWorkflowRun({
      runId: RUN.runId,
      workflowId: "case-1",
      input: { claim: "c" },
      metadata: { typeflux_project: "p" },
    });
    expect(roots).toHaveLength(1);
    const root = roots[0]!;
    expect(root.inputs).toEqual({ claim: "c" });
    expect((root.extra?.["metadata"] as Record<string, unknown>)["typeflux_project"]).toBe("p");
    expect(root.run.patched).toBe(false);

    // An activity attaches to the SAME enriched root without patching it.
    await transport.submitTrace({
      name: "act",
      input: {},
      metadata: {},
      tenant: {},
      observations: [],
      ...RUN,
    });
    expect(roots).toHaveLength(1);
    expect(root.run.patched).toBe(false);

    // Completion: outputs + end time on the SINGLE allowed update.
    await transport.recordWorkflowRun({ runId: RUN.runId, workflowId: "case-1", output: { ok: true } });
    expect(root.outputs).toEqual({ ok: true });
    expect(root.end_time).toBeTypeOf("number");
    expect(root.run.patched).toBe(true);

    // A second completion is a no-op — the single update is spent.
    root.run.patched = false;
    await transport.recordWorkflowRun({ runId: RUN.runId, workflowId: "case-1", output: { again: true } });
    expect(root.run.patched).toBe(false);
    expect(root.outputs).toEqual({ ok: true });
  });

  it("a late start record's seed rides the single final patch", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client, "wf");

    // A fast first activity creates the root BEFORE the start record lands
    // (delayed start ack): the create ships unseeded.
    await transport.submitTrace({
      name: "act",
      input: {},
      metadata: {},
      tenant: {},
      observations: [],
      ...RUN,
    });
    const root = roots[0]!;
    expect(root.inputs).toEqual({});

    // The late start record cannot patch (one update per run) — it stashes.
    await transport.recordWorkflowRun({
      runId: RUN.runId,
      workflowId: "case-1",
      input: { claim: "late" },
      metadata: { typeflux_project: "p" },
    });
    expect(root.run.patched).toBe(false);

    // Completion: the stashed seed + outputs ride the ONE allowed update.
    await transport.recordWorkflowRun({ runId: RUN.runId, workflowId: "case-1", output: { ok: true } });
    expect(root.run.patched).toBe(true);
    expect(root.inputs).toEqual({ claim: "late" });
    expect((root.extra?.["metadata"] as Record<string, unknown>)["typeflux_project"]).toBe("p");
    expect(root.outputs).toEqual({ ok: true });
  });

  it("separates different workflow runs into different roots", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client, "wf");

    for (const runId of ["0199aaaa-bbbb-cccc-dddd-eeeeffff0003", "0199aaaa-bbbb-cccc-dddd-eeeeffff0004"]) {
      await transport.submitTrace({
        name: "act",
        input: {},
        metadata: {},
        tenant: {},
        observations: [],
        workflowId: "wf-1",
        runId,
      });
    }

    expect(roots).toHaveLength(2);
    expect(new Set(roots.map((root) => root.run.config["id"]))).toEqual(
      new Set(["0199aaaa-bbbb-cccc-dddd-eeeeffff0003", "0199aaaa-bbbb-cccc-dddd-eeeeffff0004"]),
    );
  });

  it("falls back to an activity-rooted run without a run id (standalone execute)", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client, "wf");

    await transport.submitTrace({
      name: "act",
      input: { value: "in" },
      metadata: {},
      tenant: {},
      observations: [],
    });

    expect(roots).toHaveLength(1);
    expect(roots[0]!.run.config).toMatchObject({ name: "act", run_type: "chain" });
    expect(roots[0]!.run.config["id"]).toBeUndefined();
  });
});

describe("LangsmithSdkTransport", () => {
  it("maps a trace to a posted+patched run tree with llm and hook children", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client);

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

    const [root] = roots;
    expect(root!.run.config).toMatchObject({
      name: "act",
      run_type: "chain",
      project_name: "proj",
      inputs: { value: "in" },
    });
    expect((root!.run.config["extra"] as Record<string, unknown>)["metadata"]).toMatchObject({
      "typeflux.prompt": "p",
      tenant: { org: "acme" },
    });
    expect(root!.run.posted && root!.run.patched).toBe(true);
    expect(root!.run.outputs).toEqual({ value: "out" });
    expect(root!.run.error).toBe("boom");

    const [generation, hook] = root!.run.children;
    expect(generation!.config).toMatchObject({ name: "act:generation", run_type: "llm" });
    // llm-run inputs keyed `messages` so LangSmith renders the chat view.
    expect(generation!.config["inputs"]).toEqual({
      messages: [{ role: "user", content: "hi" }],
    });
    expect((generation!.config["extra"] as Record<string, unknown>)["metadata"]).toMatchObject({
      model: "gpt-x",
      usage: { cache: true },
      validation_attempt: 0,
    });
    expect(generation!.posted && generation!.patched).toBe(true);

    expect(hook!.config).toMatchObject({ name: "act:hook", run_type: "chain" });
    // The pre-hook model output is auditable from the hook run.
    expect((hook!.config["extra"] as Record<string, unknown>)["metadata"]).toMatchObject({
      llm_output: { value: "pre-hook" },
    });
    expect(hook!.error).toBe("hook failed");

    // Prompt egress: the SDK batch is awaited once per submitted trace.
    expect(client.batchesAwaited).toBe(1);
  });

  it("wraps non-object inputs/outputs into KV maps (LangSmith requires objects)", async () => {
    const client = new FakeClient();
    const { transport, roots } = transportWith(client);

    await transport.submitTrace({
      name: "act",
      input: "plain string",
      output: 42,
      metadata: {},
      tenant: {},
      observations: [],
    });

    expect(roots[0]!.run.config["inputs"]).toEqual({ input: "plain string" });
    expect(roots[0]!.run.outputs).toEqual({ output: 42 });
  });
});

describe("langsmithObserverFromSpec", () => {
  const KEYS = { LANGSMITH_API_KEY: "ls-test" };

  it("returns undefined for none/unset and for non-langsmith types", async () => {
    await expect(langsmithObserverFromSpec(specWithObservability("none"), KEYS)).resolves.toBeUndefined();
    await expect(langsmithObserverFromSpec(specWithObservability(undefined), KEYS)).resolves.toBeUndefined();
    // langfuse has its own module; custom keeps the injected-transport seam.
    await expect(langsmithObserverFromSpec(specWithObservability("langfuse"), KEYS)).resolves.toBeUndefined();
  });

  it("degrades to untraced with one warning when the api key is absent", async () => {
    const warnings = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const observer = await langsmithObserverFromSpec(specWithObservability("langsmith"), {});
      expect(observer).toBeUndefined();
      expect(warnings).toHaveBeenCalledOnce();
      expect(String(warnings.mock.calls[0])).toContain("LANGSMITH_API_KEY");
    } finally {
      warnings.mockRestore();
    }
  });

  it("falls back to the default project on an EMPTY env value (Python `or` parity)", async () => {
    const observer = await langsmithObserverFromSpec(specWithObservability("langsmith"), {
      ...KEYS,
      LANGSMITH_ENDPOINT: "http://localhost:9",
      LANGSMITH_PROJECT: "",
    });
    expect(observer).toBeInstanceOf(StreamingTraceWriter);
  });

  it("builds a streaming writer over the official SDK when the spec + env opt in", async () => {
    // Construct-only: nothing flushes, so no network leaves the test.
    const observer = await langsmithObserverFromSpec(specWithObservability("langsmith"), {
      ...KEYS,
      LANGSMITH_ENDPOINT: "http://localhost:9",
      LANGSMITH_PROJECT: "unit-test",
    });
    expect(observer).toBeInstanceOf(StreamingTraceWriter);
  });
});
