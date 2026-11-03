/**
 * Out-of-the-box LangSmith tracing (Python parity, mirroring
 * `langfuse-observer.ts`): when the spec declares
 * `runtime.observability.type: langsmith`, `buildRuntime` wires an observer
 * over the OFFICIAL `langsmith` SDK — no transport code in the caller, like
 * Python's `LangSmithObservabilityBackend.from_env`. The SDK is an OPTIONAL
 * peer dependency loaded lazily (the TS mirror of Python's `[langsmith]`
 * extra).
 *
 * Credentials come from the same environment Python reads —
 * `LANGSMITH_API_KEY`, plus `LANGSMITH_PROJECT` (default "default") and
 * `LANGSMITH_ENDPOINT` for self-hosted. An absent key degrades to an untraced
 * run with one warning.
 *
 * Custom backends keep the injected-transport seam: pass your own `observer`
 * (`observerFromSpec(spec, transport)`) and this module never runs.
 */

import { resolveOptionalSecretText } from "./secret-references.js";
import type { EmittedTrace, TraceTransport } from "@typeflux/temporal";

import { redactionFromSpec } from "./observer-from-spec.js";
import type { TypefluxYamlSpec } from "./spec.js";
import {
  StreamingTraceWriter,
  type WorkflowRunRecord,
  type WorkflowRunRecorder,
} from "./streaming-trace-writer.js";

/** The slice of the SDK's `RunTree` this module drives (structural — tests fake it). */
export interface LangsmithRunTree {
  /** The SDK links children onto the parent; the transport detaches them
   * after posting so a retained root cannot accumulate a run's whole tree. */
  child_runs?: unknown[];
  /** Assigned directly per activity: `end()` pins the FIRST end time
   * (`this.end_time ?? endTime`), so extending the root needs assignment. */
  end_time?: number;
  /** Assignable run fields — `patchRun()` sends the current state. */
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  error?: string;
  extra?: Record<string, unknown>;
  createChild(config: {
    name: string;
    run_type: string;
    inputs: Record<string, unknown>;
    extra?: Record<string, unknown>;
  }): LangsmithRunTree;
  postRun(): Promise<unknown>;
  patchRun(): Promise<unknown>;
  end(outputs?: Record<string, unknown>, error?: string): void | Promise<void>;
}

/** What the lazy `import("langsmith")` must provide (structural). */
export interface LangsmithSdkModule {
  Client: new (options: Record<string, unknown>) => LangsmithSdkClient;
  RunTree: new (config: {
    id?: string;
    name: string;
    run_type: string;
    inputs: Record<string, unknown>;
    project_name?: string;
    client?: unknown;
    extra?: Record<string, unknown>;
  }) => LangsmithRunTree;
}

export interface LangsmithSdkClient {
  /** Await the SDK's pending batched uploads (prompt egress per trace). */
  awaitPendingTraceBatches(): Promise<unknown>;
}

/** LangSmith run inputs/outputs are KV maps; wrap non-object payloads. */
function asKV(value: unknown, key: string): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : { [key]: value };
}

/**
 * Adapt one `EmittedTrace` to a LangSmith run tree. Activities of the SAME
 * workflow run GROUP under one root `chain` run (Python parity): the Temporal
 * run id is the root run's id, named `TypefluxWorkflow:<workflow>` — the
 * shape both editions' read paths reconstruct from. Each activity is a child
 * `chain` run; each provider attempt an `llm` grandchild, the context hook a
 * `chain` grandchild (pre-hook output as `llm_output` metadata). Without a
 * run id (standalone `executeActivity`), the activity is its own root.
 *
 * LangSmith permits exactly ONE update per run (a second patch 409s,
 * swallowed by the SDK's batch path — verified live), so the root's
 * lifecycle is: ONE create (seeded with the workflow input/metadata when the
 * start record arrives first) + ONE final patch at completion carrying the
 * outputs/error/end time. Activities never patch the root.
 */
export class LangsmithSdkTransport implements TraceTransport, WorkflowRunRecorder {
  /** Root state per Temporal run id (bounded; see eviction below). A seed
   * arriving AFTER the root was created (a fast first activity beat the
   * start record, e.g. a delayed start ack) stashes here and rides the
   * run's single allowed update at completion (finder). */
  private readonly roots = new Map<
    string,
    {
      root: LangsmithRunTree;
      finalized: boolean;
      pendingSeed?: { inputs?: Record<string, unknown>; metadata?: Record<string, unknown> };
    }
  >();

  constructor(
    private readonly client: LangsmithSdkClient,
    private readonly runTreeClass: LangsmithSdkModule["RunTree"],
    private readonly project: string,
    /** The spec's workflow name — the grouped root run's identity. */
    private readonly workflowName?: string,
  ) {}

  private async rootFor(
    runId: string | undefined,
    seed?: { inputs?: Record<string, unknown>; metadata?: Record<string, unknown> },
  ): Promise<
    | {
        root: LangsmithRunTree;
        finalized: boolean;
        pendingSeed?: { inputs?: Record<string, unknown>; metadata?: Record<string, unknown> };
      }
    | undefined
  > {
    if (runId === undefined || this.workflowName === undefined) {
      return undefined;
    }
    const existing = this.roots.get(runId);
    if (existing !== undefined) {
      return existing;
    }
    const root = new this.runTreeClass({
      id: runId,
      name: `TypefluxWorkflow:${this.workflowName}`,
      run_type: "chain",
      inputs: seed?.inputs ?? {},
      project_name: this.project,
      client: this.client,
      extra: { metadata: { "typeflux.workflow": this.workflowName, ...seed?.metadata } },
    });
    await root.postRun();
    const state: {
      root: LangsmithRunTree;
      finalized: boolean;
      pendingSeed?: { inputs?: Record<string, unknown>; metadata?: Record<string, unknown> };
    } = { root, finalized: false };
    this.roots.set(runId, state);
    // Bounded memory on a long-lived worker: evict the oldest tracked runs.
    // An evicted run that traces again re-posts its root; LangSmith treats the
    // duplicate create as a per-event conflict and the children still attach
    // to the existing run id.
    if (this.roots.size > 1000) {
      const oldest = this.roots.keys().next().value;
      if (oldest !== undefined) {
        this.roots.delete(oldest);
      }
    }
    return state;
  }

  /**
   * Enrich the grouped root (Python parity: the workflow span carries the
   * run's input/output + identity metadata). The START record seeds the
   * root's CREATE (input/metadata); the COMPLETION record spends the run's
   * single allowed update (outputs/error/end time).
   */
  async recordWorkflowRun(record: WorkflowRunRecord): Promise<void> {
    const seed = {
      ...(record.input !== undefined ? { inputs: asKV(record.input, "input") } : {}),
      ...(record.metadata !== undefined ? { metadata: record.metadata } : {}),
    };
    const existed = record.runId !== undefined && this.roots.has(record.runId);
    const state = await this.rootFor(record.runId, seed);
    if (state === undefined) {
      return;
    }
    if (existed && (seed.inputs !== undefined || seed.metadata !== undefined)) {
      // The root pre-dates this seed (an activity flushed first): the create
      // already shipped without it, so it must ride the single final patch.
      state.pendingSeed = { ...state.pendingSeed, ...seed };
    }
    if (record.output !== undefined || record.error !== undefined) {
      if (state.finalized) {
        return; // the single update is spent — a second completion is a no-op
      }
      state.finalized = true;
      if (state.pendingSeed?.inputs !== undefined) {
        state.root.inputs = state.pendingSeed.inputs;
      }
      if (state.pendingSeed?.metadata !== undefined) {
        const existing = (state.root.extra?.["metadata"] as Record<string, unknown>) ?? {};
        state.root.extra = {
          ...state.root.extra,
          metadata: { ...existing, ...state.pendingSeed.metadata },
        };
      }
      if (record.output !== undefined) {
        state.root.outputs = asKV(record.output, "output");
      }
      if (record.error !== undefined) {
        state.root.error = record.error;
      }
      state.root.end_time = Date.now();
      await state.root.patchRun();
    }
    await this.client.awaitPendingTraceBatches();
  }

  async submitTrace(trace: EmittedTrace): Promise<void> {
    const state = await this.rootFor(trace.runId);
    const activityConfig = {
      name: trace.name,
      run_type: "chain",
      inputs: asKV(trace.input, "input"),
      extra: { metadata: { ...trace.metadata, tenant: trace.tenant } },
    };
    const activity =
      state !== undefined
        ? state.root.createChild(activityConfig)
        : new this.runTreeClass({
            ...activityConfig,
            project_name: this.project,
            client: this.client,
          });
    await activity.postRun();
    for (const observation of trace.observations) {
      const generation = observation.type === "generation";
      const child = activity.createChild({
        name: generation ? `${trace.name}:generation` : `${trace.name}:hook`,
        run_type: generation ? "llm" : "chain",
        // LangSmith's UI renders `inputs.messages` of an llm run as a chat
        // transcript (the SDK wrappers' convention) — a generation's input IS
        // the rendered message list, so key it accordingly (finder).
        inputs: asKV(observation.input, generation ? "messages" : "input"),
        extra: {
          metadata: {
            ...observation.metadata,
            ...(generation && observation.model != null ? { model: observation.model } : {}),
            // Repair retries stay distinguishable (Python parity).
            ...(generation ? { validation_attempt: observation.attempt } : {}),
            ...(!generation && observation.modelOutput !== undefined
              ? { llm_output: observation.modelOutput }
              : {}),
          },
        },
      });
      await child.postRun();
      await child.end(
        observation.output !== undefined ? asKV(observation.output, "output") : undefined,
        observation.error,
      );
      await child.patchRun();
    }
    await activity.end(
      trace.output !== undefined ? asKV(trace.output, "output") : undefined,
      trace.error,
    );
    await activity.patchRun();
    if (state !== undefined && Array.isArray(state.root.child_runs)) {
      // DETACH the posted subtree from the retained root: the SDK's
      // createChild pushes every child onto the parent, so a busy/long-lived
      // run would otherwise accumulate its whole tree in the transport's
      // heap (finder). The child keeps its own parent/trace references —
      // detaching only drops the root's back-pointers. The root itself is
      // NEVER patched per activity: LangSmith allows one update per run, and
      // that update is the completion record's.
      state.root.child_runs.length = 0;
    }
    // Prompt egress per trace (short-lived runs need no shutdown ceremony).
    // DELIVERY is SDK-owned: langsmith batches and retries internally; like
    // the langfuse SDK, a hard outage surfaces in its logs, not here.
    await this.client.awaitPendingTraceBatches();
  }
}

/**
 * Build the out-of-the-box observer for a `type: langsmith` spec. Returns
 * undefined for every other type, and undefined with one warning when
 * `LANGSMITH_API_KEY` is absent. Throws with an install hint when the spec
 * opts in but the optional `langsmith` peer is not installed.
 */
export async function langsmithObserverFromSpec(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): Promise<StreamingTraceWriter | undefined> {
  if (spec.runtime.observability?.type !== "langsmith") {
    return undefined;
  }
  // Spec-declared credentials (#793) win field-by-field; env vars stay the fallback.
  const declared = spec.runtime.observability?.langsmith;
  const apiKey =
    resolveOptionalSecretText(declared?.api_key, "runtime.observability.langsmith.api_key", environment) ??
    environment["LANGSMITH_API_KEY"];
  if (!apiKey) {
    console.warn(
      "runtime.observability.type is langsmith but no credentials resolve — running untraced " +
        "(declare runtime.observability.langsmith.api_key or export LANGSMITH_API_KEY, plus " +
        "LANGSMITH_ENDPOINT for self-hosted and LANGSMITH_PROJECT to pick the project)",
    );
    return undefined;
  }
  let sdk: LangsmithSdkModule;
  try {
    sdk = (await import("langsmith")) as unknown as LangsmithSdkModule;
  } catch (error) {
    throw new Error(
      "runtime.observability.type is langsmith but the langsmith SDK is not installed — " +
        "add it to your app (npm/pnpm add langsmith). It is an optional peer dependency, " +
        "loaded only when a spec opts into langsmith tracing (Python's [langsmith] extra).",
      { cause: error },
    );
  }
  const endpoint =
    resolveOptionalSecretText(declared?.endpoint, "runtime.observability.langsmith.endpoint", environment) ??
    environment["LANGSMITH_ENDPOINT"];
  const client = new sdk.Client({
    apiKey,
    ...(endpoint !== undefined ? { apiUrl: endpoint } : {}),
  });
  // Python parity: `os.getenv("LANGSMITH_PROJECT") or "default"` — truthy
  // `||`, not `??`, so an empty-string env value falls back too (the ported
  // truthiness trap).
  const project =
    resolveOptionalSecretText(declared?.project, "runtime.observability.langsmith.project", environment) ||
    environment["LANGSMITH_PROJECT"] ||
    "default";
  return new StreamingTraceWriter(
    new LangsmithSdkTransport(client, sdk.RunTree, project, spec.workflow.name),
    // Redaction defaults ON for a tracing backend (observerFromSpec parity).
    redactionFromSpec(spec) ?? {},
  );
}
