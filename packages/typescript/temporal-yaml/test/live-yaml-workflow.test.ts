import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

import { buildRuntime, loadYamlSpec, YAML_WORKFLOW_TYPE } from "../src/index.js";

// Skipped in CI. Runs a YAML-defined workflow end to end through buildRuntime against a
// local dev server:
//   temporal server start-dev
//   pnpm -r build   (so the workflow bundle resolves @typeflux/temporal/composition from dist)
//   TYPEFLUX_LIVE_TEMPORAL=1 pnpm --filter @typeflux/temporal-yaml test
const LIVE = process.env["TYPEFLUX_LIVE_TEMPORAL"] === "1";

// A spec has ONE provider for all activities; route on the rendered prompt so each
// activity gets a schema-valid response.
class RoutingProvider implements ModelProvider {
  structuredCall(params: StructuredCallParams): unknown {
    const text = params.messages.map((m) => (typeof m.content === "string" ? m.content : "")).join(" ");
    return text.includes("consolidate") ? { count: 2 } : { ok: true };
  }
}

const SPEC = `
project: p
name: claim_review
task_queue: typeflux-live-yaml
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/review: review the item
      p/consolidate: consolidate the reviews
  provider: { type: openai }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
    - name: consolidate_claim_review
      input: schemas:Batch
      output: schemas:Packet
      prompt: p/consolidate
workflow:
  name: ClaimReviewWorkflow
  input: schemas:Claim
  output: schemas:Packet
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
        concurrency: 2
        collect: { output: schemas:Batch, field: reviews }
    - id: consolidate
      activity: consolidate_claim_review
`;

const schemas = {
  "schemas:EvidenceItem": z.object({ id: z.string() }),
  "schemas:EvidenceReview": z.object({ ok: z.boolean() }),
  "schemas:Batch": z.object({ reviews: z.array(z.object({ ok: z.boolean() })) }),
  "schemas:Packet": z.object({ count: z.number() }),
};

// An activity body (the provider call) that takes longer than its heartbeat timeout, so it
// only survives if the worker emits background heartbeats (#484).
class SlowProvider implements ModelProvider {
  constructor(private readonly delayMs: number) {}
  async structuredCall(_params: StructuredCallParams): Promise<unknown> {
    await new Promise((resolve) => setTimeout(resolve, this.delayMs));
    return { ok: true };
  }
}

const HEARTBEAT_SPEC = `
project: p
name: hb
task_queue: typeflux-live-hb
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/slow: do slow work
  provider: { type: openai }
activities:
  definitions:
    - name: slow_activity
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/slow
      start_to_close_timeout_seconds: 30
      heartbeat_timeout_seconds: 2
workflow:
  name: SlowWorkflow
  input: schemas:EvidenceItem
  output: schemas:EvidenceReview
  steps:
    - id: slow
      activity: slow_activity
`;

// A spec whose retry uses the unlimited sentinel `maximum_attempts: 0`. Temporal TS's
// compileRetryPolicy REJECTS maximumAttempts <= 0, so this only schedules if the proxy options
// omit maximumAttempts for the 0 sentinel (#486).
const UNLIMITED_RETRY_SPEC = `
project: p
name: ur
task_queue: typeflux-live-ur
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/go: do the thing
  provider: { type: openai }
  activity_retry:
    maximum_attempts: 0
activities:
  definitions:
    - name: do_thing
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/go
workflow:
  name: UnlimitedRetryWorkflow
  input: schemas:EvidenceItem
  output: schemas:EvidenceReview
  steps:
    - id: go
      activity: do_thing
`;

// An abort-aware slow provider — the shape of a real fetch-based transport: the forwarded
// cancellation signal drops the in-flight call instead of running it to completion (#487).
class AbortAwareSlowProvider implements ModelProvider {
  /** Set when the forwarded signal aborts — lets the test pin WHEN delivery happened. */
  abortedAt: number | undefined;
  constructor(private readonly delayMs: number) {}
  async structuredCall(params: StructuredCallParams): Promise<unknown> {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, this.delayMs);
      params.signal?.addEventListener("abort", () => {
        this.abortedAt = Date.now();
        clearTimeout(timer);
        reject(params.signal?.reason ?? new Error("aborted"));
      });
    });
    return { ok: true };
  }
}

const CANCEL_SPEC = `
project: p
name: cx
task_queue: typeflux-live-cancel
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/slow: do slow work
  provider: { type: openai }
activities:
  definitions:
    - name: slow_cancellable
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/slow
      start_to_close_timeout_seconds: 60
      heartbeat_timeout_seconds: 2
workflow:
  name: CancelWorkflow
  input: schemas:EvidenceItem
  output: schemas:EvidenceReview
  steps:
    - id: slow
      activity: slow_cancellable
`;

const ARTIFACT_SPEC = (root: string): string => `
project: p
name: af
task_queue: typeflux-live-artifacts
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/review: review the evidence
  provider: { type: openai }
  artifacts:
    local_roots: ["${root}"]
activities:
  definitions:
    - name: review_files
      input: schemas:FilesInput
      output: schemas:EvidenceReview
      prompt: p/review
      artifacts:
        - name: evidence_files
          from: input.files
          attach: { role: user, text: "Attached evidence:" }
workflow:
  name: ArtifactWorkflow
  input: schemas:FilesInput
  output: schemas:EvidenceReview
  steps:
    - id: review
      activity: review_files
`;

const LIFECYCLE_SPEC = `
project: p
name: lc
task_queue: typeflux-live-lifecycle
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/slow: do slow work
  provider: { type: openai }
activities:
  definitions:
    - name: slow_step
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/slow
    - name: slow_step_two
      input: schemas:EvidenceReview
      output: schemas:EvidenceReview
      prompt: p/slow
workflow:
  name: LifecycleWorkflow
  input: schemas:EvidenceItem
  output: schemas:EvidenceReview
  lifecycle: { enabled: true }
  steps:
    - id: first
      activity: slow_step
    - id: second
      activity: slow_step_two
`;

const REVIEW_SPEC = (review: string) => `
project: p
name: rv
task_queue: typeflux-live-review
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/step: do the step
  provider: { type: openai }
activities:
  definitions:
    - name: draft
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/step
    - name: revise
      input: schemas:EvidenceReview
      output: schemas:EvidenceReview
      prompt: p/step
    - name: publish
      input: schemas:EvidenceReview
      output: schemas:EvidenceReview
      prompt: p/step
workflow:
  name: ReviewWorkflow
  input: schemas:EvidenceItem
  output: schemas:EvidenceReview
  lifecycle:
    enabled: true
    review:
${review}
  steps:
    - id: gate
      activity: draft
    - id: revise_step
      activity: revise
    - id: publish_step
      activity: publish
`;

// Two named review gates after distinct steps (#55 slice 4): first_gate after `gate`,
// second_gate after `revise_step`. Both route forward to the natural next step (EvidenceReview
// throughout), so the chain type-checks and every step runs.
const GATES_SPEC = `
project: p
name: rvg
task_queue: typeflux-live-gates
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/step: do the step
  provider: { type: openai }
activities:
  definitions:
    - name: draft
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/step
    - name: revise
      input: schemas:EvidenceReview
      output: schemas:EvidenceReview
      prompt: p/step
    - name: publish
      input: schemas:EvidenceReview
      output: schemas:EvidenceReview
      prompt: p/step
workflow:
  name: TwoGateWorkflow
  input: schemas:EvidenceItem
  output: schemas:EvidenceReview
  lifecycle:
    enabled: true
    gates:
      - id: first_gate
        after_step: gate
        user_decisions:
          proceed: { route: revise_step }
      - id: second_gate
        after_step: revise_step
        user_decisions:
          approve: { route: publish_step }
          reject: { route: publish_step }
  steps:
    - id: gate
      activity: draft
    - id: revise_step
      activity: revise
    - id: publish_step
      activity: publish
`;

describe.skipIf(!LIVE)("live YAML runtime e2e (#452)", () => {
  it("runs a YAML spec end to end through buildRuntime", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const spec = loadYamlSpec(SPEC);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider: new RoutingProvider(),
        schemas,
        worker: { connection },
        // Run the workflow from source (vitest does not use dist/).
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();

      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(
          client,
          { evidence: [{ id: "e1" }, { id: "e2" }] },
          { workflowId: `yaml-rt-${Date.now()}` },
        ),
      );

      expect(result).toEqual({ count: 2 });
    } finally {
      await connection.close();
    }
  });


  it("brackets a session-cached map with prep and release, threading the handle (#478)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    // A reference-style scripted provider that records the full cache lifecycle.
    const prepared: string[] = [];
    const released: (string | null)[] = [];
    const perItemHandles: (string | null | undefined)[] = [];
    const cachingProvider: ModelProvider = {
      providerName: "fake-reference",
      supportsSessionCache: true,
      sessionCacheStyle: "reference",
      prepareCachedSession: (params) => {
        prepared.push(params.identityHash);
        return {
          provider: "fake-reference",
          identity_hash: params.identityHash,
          supported: true,
          style: "reference",
          cache_id: "cachedContents/live-1",
          model: null,
          created_at: null,
          ttl_seconds: null,
          reference_cached: false,
          prefix_stable_messages: null,
          per_item_artifact_messages: false,
        };
      },
      releaseCachedSession: (handle) => {
        released.push(handle.cache_id);
      },
      structuredCall: (params: StructuredCallParams) => {
        perItemHandles.push(params.cachedSession?.cache_id);
        return { ok: true };
      },
    };
    const spec = loadYamlSpec(`
project: p
name: cached_review
task_queue: typeflux-live-yaml-cache
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/review:
        messages:
          - { role: system, content: You review evidence carefully. }
          - { role: user, content: review the item }
  provider: { type: openai }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
      cache: { enabled: true }
workflow:
  name: CachedReviewWorkflow
  input: schemas:Claim
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
        concurrency: 2
        collect: { output: schemas:Batch, field: reviews }
`);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider: cachingProvider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(
          client,
          { evidence: [{ id: "e1" }, { id: "e2" }, { id: "e3" }] },
          { workflowId: `yaml-cache-${Date.now()}` },
        ),
      );
      // The collect step wraps the three per-item results.
      expect(result).toEqual({ reviews: [{ ok: true }, { ok: true }, { ok: true }] });
      // Prep ran exactly once (before the fan-out), every item carried the engaged
      // handle, and the reference cache was released with its cache_id afterward.
      expect(prepared).toHaveLength(1);
      expect(perItemHandles).toEqual(["cachedContents/live-1", "cachedContents/live-1", "cachedContents/live-1"]);
      expect(released).toEqual(["cachedContents/live-1"]);
    } finally {
      await connection.close();
    }
  });


  it("fails a map whose collected payload exceeds collect.max_bytes with the typed error (#495)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const spec = loadYamlSpec(`
project: p
name: bounded_review
task_queue: typeflux-live-yaml-maxbytes
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/review: review the item
  provider: { type: openai }
activities:
  definitions:
    - name: review_evidence_item
      input: schemas:EvidenceItem
      output: schemas:EvidenceReview
      prompt: p/review
workflow:
  name: BoundedReviewWorkflow
  input: schemas:Claim
  steps:
    - id: review_evidence
      map:
        activity: review_evidence_item
        over: input.evidence
        collect: { output: schemas:Batch, field: reviews, max_bytes: 10 }
`);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider: new RoutingProvider(),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const failure: unknown = await runtime.worker
        .runUntil(
          runtime.runWorkflow(client, { evidence: [{ id: "e1" }, { id: "e2" }] }, { workflowId: `yaml-maxbytes-${Date.now()}` }),
        )
        .then(() => undefined)
        .catch((error: unknown) => error);
      expect(failure).toBeDefined();
      // The client wraps the failure; the typed ApplicationFailure rides as the cause.
      const cause = (failure as { cause?: { type?: string; message?: string } }).cause;
      expect(cause?.type).toBe("TypefluxMapCollectPayloadTooLarge");
      expect(cause?.message).toMatch(/exceeding collect.max_bytes=10/);
    } finally {
      await connection.close();
    }
  });

  it("emits heartbeats so an activity slower than its heartbeat_timeout survives (#484)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const spec = loadYamlSpec(HEARTBEAT_SPEC);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        // The activity body sleeps 3s while heartbeat_timeout is 2s, so it only completes if the
        // worker emits background heartbeats. (Without the loop it HeartbeatTimeouts at ~2s every
        // attempt and exhausts the bounded retry (#486) -> the workflow fails; it never returns
        // {ok:true}. So reaching the assertion IS the proof the heartbeat loop worked.)
        provider: new SlowProvider(3000),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();

      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { id: "e1" }, { workflowId: `yaml-hb-${Date.now()}` }),
      );

      expect(result).toEqual({ ok: true });
    } finally {
      await connection.close();
    }
  });

  it("schedules an activity with the unlimited-retry sentinel (maximum_attempts: 0) without a compile error (#486)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const spec = loadYamlSpec(UNLIMITED_RETRY_SPEC);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider: new RoutingProvider(), // returns { ok: true } for a non-consolidate prompt
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();

      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { id: "e1" }, { workflowId: `yaml-ur-${Date.now()}` }),
      );

      // 0 -> omitted maximumAttempts -> Temporal accepts it as unlimited; the activity schedules and
      // succeeds rather than throwing "RetryPolicy.maximumAttempts must be a positive integer".
      expect(result).toEqual({ ok: true });
    } finally {
      await connection.close();
    }
  });

  it("resolves a real file artifact through the full Temporal round-trip (#481)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    const { mkdtemp, writeFile } = await import("node:fs/promises");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");

    const root = await mkdtemp(join(tmpdir(), "tf-live-artifacts-"));
    await writeFile(join(root, "evidence.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));

    const spec = loadYamlSpec(ARTIFACT_SPEC(root));
    const seen: StructuredCallParams[] = [];
    const provider: ModelProvider = {
      structuredCall: (params) => {
        seen.push(params);
        return { ok: true };
      },
    };
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas: { ...schemas, "schemas:FilesInput": z.object({ files: z.array(z.string()) }) },
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { files: ["evidence.png"] }, { workflowId: `yaml-af-${Date.now()}` }),
      );
      expect(result).toEqual({ ok: true });
      // The worker-side resolver read + hashed the real file and the group reached the provider
      // with the attach message — the input crossed Temporal serialization, resolution didn't.
      expect(seen[0]?.artifacts?.[0]?.artifacts[0]).toMatchObject({
        kind: "image",
        media_type: "image/png",
        size_bytes: 4,
      });
      expect(seen[0]?.messages.at(-1)).toMatchObject({
        content: [
          { type: "text", text: "Attached evidence:" },
          { type: "artifact_group", group: "evidence_files" },
        ],
      });
    } finally {
      await connection.close();
    }
  });

  it("serves the lifecycle status query and honors typeflux_request_cancel between steps (#482)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const spec = loadYamlSpec(LIFECYCLE_SPEC);
    let calls = 0;
    const provider: ModelProvider = {
      structuredCall: async () => {
        calls += 1;
        await new Promise((resolve) => setTimeout(resolve, 2500));
        return { ok: true };
      },
    };
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      await runtime.worker.runUntil(async () => {
        const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
          taskQueue: runtime.taskQueue,
          workflowId: `yaml-lc-${Date.now()}`,
          args: [runtime.plan, { id: "e1" }],
        });
        // While step `first` runs: the status query answers with the Python wire shape.
        await new Promise((resolve) => setTimeout(resolve, 1200));
        const status = await handle.query("typeflux_lifecycle_status");
        expect(status).toMatchObject({ state: "running", current_step: "first", total_units: 2 });
        expect((status as { events: { event: string }[] }).events.map((e) => e.event)).toEqual([
          "workflow_started",
          "step_started",
        ]);
        // Cancel mid-step-one: Python parity — the flag is honored at the NEXT step start, so
        // step `second` never runs and the workflow fails typed.
        await handle.signal("typeflux_request_cancel", "operator stop");
        const failure = await handle.result().then(
          () => undefined,
          (e: unknown) => e,
        );
        expect(String((failure as { cause?: { type?: string } }).cause?.type ?? failure)).toContain(
          "TypefluxWorkflowCancelled",
        );
        expect(calls).toBe(1); // the second step never spent a provider call
      });
    } finally {
      await connection.close();
    }
  });

  it("review gate: an approve decision routes forward, skipping steps (#482)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    const spec = loadYamlSpec(
      REVIEW_SPEC("      after_step: gate\n      user_decisions:\n        approve: { route: publish_step }\n        reject: { route: revise_step }"),
    );
    const ran: string[] = [];
    const provider: ModelProvider = {
      structuredCall: (params) => {
        ran.push(String(params.messages[0]?.content ?? ""));
        return { ok: true };
      },
    };
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      await runtime.worker.runUntil(async () => {
        const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
          taskQueue: runtime.taskQueue,
          workflowId: `yaml-rv-${Date.now()}`,
          args: [runtime.plan, { id: "e1" }],
        });
        // The gate opens after `gate` completes.
        for (let waited = 0; waited < 8_000; waited += 200) {
          const status = (await handle.query("typeflux_lifecycle_status")) as { state: string };
          if (status.state === "waiting_for_review") break;
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
        const waiting = (await handle.query("typeflux_lifecycle_status")) as { state: string; waiting_checkpoint: string };
        expect(waiting).toMatchObject({ state: "waiting_for_review", waiting_checkpoint: "gate" });
        await handle.signal("typeflux_submit_review", { user_decision: "approve", reviewer: "sam" });
        expect(await handle.result()).toEqual({ ok: true });
        const events = ((await handle.query("typeflux_lifecycle_status").catch(() => undefined)) ?? undefined) as
          | { events: { event: string }[] }
          | undefined;
        // revise_step was skipped: only draft + publish ran (2 provider calls).
        expect(ran).toHaveLength(2);
        if (events !== undefined) {
          expect(events.events.map((e) => e.event)).toContain("review_submitted");
          expect(events.events.map((e) => e.event)).toContain("review_routed");
        }
      });
    } finally {
      await connection.close();
    }
  }, 30_000);

  it("two review gates: each decided by gate id, driven in sequence (#55 slice 4)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    const spec = loadYamlSpec(GATES_SPEC);
    const ran: string[] = [];
    const provider: ModelProvider = {
      structuredCall: (params) => {
        ran.push(String(params.messages[0]?.content ?? ""));
        return { ok: true };
      },
    };
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      await runtime.worker.runUntil(async () => {
        const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
          taskQueue: runtime.taskQueue,
          workflowId: `yaml-gates-${Date.now()}`,
          args: [runtime.plan, { id: "e1" }],
        });
        type Status = {
          state: string;
          waiting_checkpoint: string | null;
          waiting_gates?: { gate_id: string; after_step: string; valid_user_decisions: Record<string, string> }[];
        };
        const waitForGate = async (afterStep: string): Promise<Status> => {
          for (let waited = 0; waited < 10_000; waited += 200) {
            const status = (await handle.query("typeflux_lifecycle_status")) as Status;
            if (status.state === "waiting_for_review" && status.waiting_checkpoint === afterStep) return status;
            await new Promise((resolve) => setTimeout(resolve, 200));
          }
          throw new Error(`gate after ${afterStep} never opened`);
        };
        // First gate opens after `gate`; waiting_gates names it and lists its decision.
        const first = await waitForGate("gate");
        expect(first.waiting_gates).toEqual([
          { gate_id: "first_gate", after_step: "gate", valid_user_decisions: { proceed: "revise_step" } },
        ]);
        await handle.signal("typeflux_submit_review", { user_decision: "proceed", gate: "first_gate", reviewer: "sam" });
        // Second gate opens after `revise_step`; decide it by its own id.
        const second = await waitForGate("revise_step");
        expect(second.waiting_gates).toEqual([
          {
            gate_id: "second_gate",
            after_step: "revise_step",
            valid_user_decisions: { approve: "publish_step", reject: "publish_step" },
          },
        ]);
        await handle.signal("typeflux_submit_review", { user_decision: "approve", gate: "second_gate" });
        expect(await handle.result()).toEqual({ ok: true });
        // All three steps ran (both gates routed to the natural next step).
        expect(ran).toHaveLength(3);
        const final = (await handle.query("typeflux_lifecycle_status").catch(() => undefined)) as
          | { events: { event: string; gate_id?: string }[] }
          | undefined;
        if (final !== undefined) {
          const gatedEvents = final.events.filter((e) => e.event === "review_submitted");
          // Every review event carries its gate_id in multi-gate mode.
          expect(gatedEvents.map((e) => e.gate_id).sort()).toEqual(["first_gate", "second_gate"]);
        }
      });
    } finally {
      await connection.close();
    }
  }, 40_000);

  it("review gate: timeout with on_timeout fail raises TypefluxReviewTimeout (#482)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    const spec = loadYamlSpec(
      REVIEW_SPEC(
        "      after_step: gate\n      user_decisions:\n        approve: { route: publish_step }\n      timeout: { seconds: 2, on_timeout: fail }",
      ),
    );
    const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      await runtime.worker.runUntil(async () => {
        const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
          taskQueue: runtime.taskQueue,
          workflowId: `yaml-rvt-${Date.now()}`,
          args: [runtime.plan, { id: "e1" }],
        });
        const failure = await handle.result().then(
          () => undefined,
          (e: unknown) => e,
        );
        expect(String((failure as { cause?: { type?: string } }).cause?.type ?? failure)).toContain(
          "TypefluxReviewTimeout",
        );
      });
    } finally {
      await connection.close();
    }
  }, 30_000);

  it("review gate: an invalid decision under the fail policy terminates typed (#482)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    const spec = loadYamlSpec(
      REVIEW_SPEC(
        "      after_step: gate\n      user_decisions:\n        approve: { route: publish_step }\n      invalid_user_decision: fail",
      ),
    );
    const provider: ModelProvider = { structuredCall: () => ({ ok: true }) };
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      await runtime.worker.runUntil(async () => {
        const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
          taskQueue: runtime.taskQueue,
          workflowId: `yaml-rvi-${Date.now()}`,
          args: [runtime.plan, { id: "e1" }],
        });
        for (let waited = 0; waited < 8_000; waited += 200) {
          const status = (await handle.query("typeflux_lifecycle_status")) as { state: string };
          if (status.state === "waiting_for_review") break;
          await new Promise((resolve) => setTimeout(resolve, 200));
        }
        await handle.signal("typeflux_submit_review", { user_decision: "maybe" });
        const failure = await handle.result().then(
          () => undefined,
          (e: unknown) => e,
        );
        expect(String((failure as { cause?: { type?: string } }).cause?.type ?? failure)).toContain(
          "TypefluxInvalidReviewDecision",
        );
      });
    } finally {
      await connection.close();
    }
  }, 30_000);

  it("cancels a running activity promptly via the cooperative signal (#487)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const spec = loadYamlSpec(CANCEL_SPEC);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtimeProvider = new AbortAwareSlowProvider(30_000);
      const runtime = await buildRuntime(spec, {
        // The activity body would run 30s; heartbeat_timeout 2s keeps cancellation deliverable.
        provider: runtimeProvider,
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();

      const provider = runtimeProvider;
      await runtime.worker.runUntil(async () => {
        const handle = await client.workflow.start(YAML_WORKFLOW_TYPE, {
          taskQueue: runtime.taskQueue,
          workflowId: `yaml-cancel-${Date.now()}`,
          args: [runtime.plan, { id: "e1" }],
        });
        // Let the activity start and heartbeat, then cancel the workflow.
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const cancelledAt = Date.now();
        await handle.cancel();
        await expect(handle.result()).rejects.toThrow(/cancel/i);
        // Pin heartbeat-DELIVERED cancellation while the worker is still serving: the abort must
        // reach the in-flight provider call within a few heartbeat cycles of the cancel. (Worker
        // shutdown aborts the same signal, so asserting only elapsed time would pass even with
        // server->activity delivery broken — poll BEFORE runUntil ends and shutdown fires.)
        for (let waited = 0; provider.abortedAt === undefined && waited < 8_000; waited += 100) {
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
        expect(provider.abortedAt).toBeDefined();
        expect((provider.abortedAt ?? 0) - cancelledAt).toBeLessThan(8_000);
      });
    } finally {
      await connection.close();
    }
  }, 30_000);

  it("freezes workflow.version to the recorded graph digest through a real server (#530)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    // Per-run labels keep the test hermetic against this dev server's history.
    const label = `v1-${Date.now()}`;
    const frozenSpec = (version: string, stepId = "consolidate") =>
      loadYamlSpec(
        SPEC.replace("task_queue: typeflux-live-yaml", "task_queue: typeflux-live-frozen")
          .replace("name: ClaimReviewWorkflow", `name: FrozenWorkflow\n  version: ${version}`)
          .replace("- id: consolidate", `- id: ${stepId}`),
      );
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const buildOptions = {
        provider: new RoutingProvider(),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      };
      const client = new Client();
      // First start under the label: fresh, records the identity memo.
      const runtime = await buildRuntime(frozenSpec(label), buildOptions);
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { evidence: [{ id: "e1" }, { id: "e2" }] }, { workflowId: `frozen-${label}` }),
      );
      expect(result).toEqual({ count: 2 });

      // Visibility indexing is eventually consistent — give the record a moment.
      await new Promise((resolve) => setTimeout(resolve, 2000));

      // The SAME label with an edited graph (renamed step -> new digest) must be refused
      // BEFORE any start — the memo written above round-trips through real visibility.
      const edited = await buildRuntime(frozenSpec(label, "consolidate_renamed"), buildOptions);
      await expect(
        edited.worker.runUntil(
          edited.runWorkflow(client, { evidence: [{ id: "e1" }] }, { workflowId: `frozen-edit-${label}` }),
        ),
      ).rejects.toThrow(/frozen to spec digest .* assign a new workflow\.version/s);

      // A NEW label frees the edited graph.
      const bumped = await buildRuntime(frozenSpec(`${label}-2`, "consolidate_renamed"), buildOptions);
      const rerun = await bumped.worker.runUntil(
        bumped.runWorkflow(client, { evidence: [{ id: "e1" }] }, { workflowId: `frozen-bump-${label}` }),
      );
      expect(rerun).toEqual({ count: 2 }); // RoutingProvider's fixed consolidate response
    } finally {
      await connection.close();
    }
  }, 90_000);

  it("executes a parallel block with when gating live: gated-out branch null, early exit, per-branch provenance (#55)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");

    const COMPOSITION_SPEC = `
project: p
name: comp
task_queue: typeflux-live-composition
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/classify: classify the disclosure
      p/legal: run the legal screen
      p/medical: run the medical review
      p/consolidate: consolidate the reviews
      p/deep: run deep analysis
  provider: { type: openai }
activities:
  definitions:
    - { name: classify_disclosure, input: "schemas:Disclosure", output: "schemas:Classification", prompt: p/classify }
    - { name: legal_screen, input: "schemas:Classification", output: "schemas:LegalScreen", prompt: p/legal }
    - { name: medical_review, input: "schemas:Classification", output: "schemas:MedicalReview", prompt: p/medical }
    - { name: consolidate_reviews, input: "schemas:ReviewBundle", output: "schemas:ReviewOutcome", prompt: p/consolidate }
    - { name: deep_analysis, input: "schemas:ReviewOutcome", output: "schemas:ReviewOutcome", prompt: p/deep }
workflow:
  name: CompositionWorkflow
  input: schemas:Disclosure
  output: schemas:ReviewOutcome
  lifecycle: { enabled: true }
  steps:
    - id: classify
      activity: classify_disclosure
    - id: reviews
      parallel:
        branches:
          - id: legal
            when: { path: classify.needs_legal, eq: true }
            steps: [{ id: legal_screen_step, activity: legal_screen }]
          - id: medical
            steps: [{ id: medical_review_step, activity: medical_review }]
        collect: { output: schemas:ReviewBundle }
    - id: consolidate
      activity: consolidate_reviews
    - id: deep_analysis_step
      when: { path: classify.severity, gte: 3 }
      activity: deep_analysis
`;
    const compositionSchemas = {
      "schemas:Disclosure": z.object({ id: z.string() }),
      "schemas:Classification": z.object({ needs_legal: z.boolean(), severity: z.number() }),
      "schemas:LegalScreen": z.object({ legal_ok: z.boolean() }),
      "schemas:MedicalReview": z.object({ medical_ok: z.boolean() }),
      // Branch-gated field is Optional (nullable): the gated-out branch collects null (#55 §3.3).
      "schemas:ReviewBundle": z.object({
        legal: z.object({ legal_ok: z.boolean() }).nullable(),
        medical: z.object({ medical_ok: z.boolean() }).nullable(),
      }),
      "schemas:ReviewOutcome": z.object({ count: z.number() }),
    };
    // Routes on the rendered prompt; deep analysis must never run (severity 1 < 3).
    const provider: ModelProvider = {
      structuredCall(params: StructuredCallParams): unknown {
        const text = params.messages.map((m) => (typeof m.content === "string" ? m.content : "")).join(" ");
        if (text.includes("classify")) return { needs_legal: false, severity: 1 };
        if (text.includes("legal")) return { legal_ok: true };
        if (text.includes("medical")) return { medical_ok: true };
        if (text.includes("consolidate")) return { count: 7 };
        throw new Error(`unexpected prompt in composition live test: ${text}`);
      },
    };

    const spec = loadYamlSpec(COMPOSITION_SPEC);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(spec, {
        provider,
        schemas: compositionSchemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      const client = new Client();
      const workflowId = `composition-${Date.now()}`;

      // The status query needs a polling worker even for a closed run, so it runs
      // INSIDE runUntil, after the result lands.
      const { result, status } = await runtime.worker.runUntil(async () => {
        const result = await runtime.runWorkflow(client, { id: "d1" }, { workflowId });
        const status = await client.workflow
          .getHandle(workflowId)
          .query<{ state: string; events: { event: string; step_id: string | null; condition?: string }[] }>(
            "typeflux_lifecycle_status",
          );
        return { result, status };
      });
      // needs_legal=false gates the legal branch out (null field); severity 1 < 3 early-exits
      // BEFORE deep_analysis_step, completing with consolidate's output.
      expect(result).toEqual({ count: 7 });
      expect(status.state).toBe("completed");
      const skipped = status.events.filter((event) => event.event === "step_skipped");
      expect(skipped).toEqual([
        expect.objectContaining({ step_id: "legal", condition: "classify.needs_legal == true" }),
        expect.objectContaining({ step_id: "deep_analysis_step", condition: "classify.severity >= 3" }),
      ]);
      const started = status.events.filter((event) => event.event === "step_started").map((event) => event.step_id);
      expect(started).toEqual(["classify", "reviews", "medical_review_step", "consolidate"]);
    } finally {
      await connection.close();
    }
  }, 60_000);
});

// #188 FIX 2: the worker encrypts activity/result payloads, but a workflow's START payloads
// (plan + input) are serialized by the CALLER's Client. runWorkflow fail-closes (D188-3) when a
// codec is declared but the supplied Client was not built with `runtime.dataConverter` — otherwise
// the start rides to history as PLAINTEXT while the worker encrypts everything else.
const CODEC_SPEC = SPEC.replace(
  "  temporal: {}",
  `  temporal:
    payload_codec:
      type: aes
      current: k1
      keys:
        - { id: k1, value_from: { env: TF_CODEC_KEY } }`,
);

describe.skipIf(!LIVE)("live YAML start-path payload codec (#188 FIX 2)", () => {
  it("runtime.dataConverter is exposed for a codec spec; runWorkflow accepts a Client built with it", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    process.env["TF_CODEC_KEY"] = ("01234567" + "89abcdef").repeat(2); // exactly 32 bytes
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(CODEC_SPEC), {
        provider: new RoutingProvider(),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      // The codec's dataConverter must be exposed so callers wire their START-path Client.
      expect(runtime.dataConverter).toBeDefined();
      expect(runtime.dataConverter?.payloadCodecs?.length).toBe(1);
      // A Client built WITH runtime.dataConverter encrypts start payloads and runs end to end.
      const client = new Client({ dataConverter: runtime.dataConverter! });
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(client, { evidence: [{ id: "e1" }, { id: "e2" }] }, { workflowId: `yaml-codec-ok-${Date.now()}` }),
      );
      expect(result).toEqual({ count: 2 });
    } finally {
      delete process.env["TF_CODEC_KEY"];
      await connection.close();
    }
  }, 60_000);

  it("runWorkflow THROWS fail-closed when a codec is declared but the Client is un-wired", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    process.env["TF_CODEC_KEY"] = ("01234567" + "89abcdef").repeat(2);
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(CODEC_SPEC), {
        provider: new RoutingProvider(),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      // A bare Client carries the SDK's default codec-free converter → the start would plaintext.
      await runtime.worker.runUntil(async () => {
        await expect(
          runtime.runWorkflow(new Client(), { evidence: [{ id: "e1" }] }, { workflowId: `yaml-codec-bad-${Date.now()}` }),
        ).rejects.toThrow(/not built with a payload codec.*dataConverter: runtime\.dataConverter/s);
      });
    } finally {
      delete process.env["TF_CODEC_KEY"];
      await connection.close();
    }
  }, 60_000);

  it("a no-codec spec leaves runtime.dataConverter undefined and runWorkflow runs with a bare Client (unaffected)", async () => {
    const { NativeConnection } = await import("@temporalio/worker");
    const { Client } = await import("@temporalio/client");
    const connection = await NativeConnection.connect({ address: "localhost:7233" });
    try {
      const runtime = await buildRuntime(loadYamlSpec(SPEC), {
        provider: new RoutingProvider(),
        schemas,
        worker: { connection },
        workflowsPath: new URL("../src/workflows.ts", import.meta.url).pathname,
      });
      expect(runtime.dataConverter).toBeUndefined();
      const result = await runtime.worker.runUntil(
        runtime.runWorkflow(new Client(), { evidence: [{ id: "e1" }, { id: "e2" }] }, { workflowId: `yaml-nocodec-${Date.now()}` }),
      );
      expect(result).toEqual({ count: 2 });
    } finally {
      await connection.close();
    }
  }, 60_000);
});
