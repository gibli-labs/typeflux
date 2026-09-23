/**
 * Trace writer (parity Epic 4, #451) — the emit side of observability. The Python
 * writers (`observability/langfuse.py` / `langsmith.py`) are OpenTelemetry-span based;
 * to keep the core dependency-light (Decision 0), this is a backend-agnostic writer
 * that captures executions via the observer contract and `flush()`es each as an
 * `EmittedTrace` through an injected `TraceTransport`. The caller's transport adapts
 * `EmittedTrace` to a Langfuse `trace`/`generation`, a LangSmith run tree, an OTel
 * exporter, or a raw HTTP call — so no vendor SDK lands here.
 */

import { CollectingObserver, type RecordedActivity } from "./observer.js";
import { redactMetadata, type RedactionConfig } from "./redaction.js";

/** One child observation of a trace (a provider call attempt, or the context hook). */
export interface EmittedObservation {
  type: "generation" | "hook";
  model?: string | null;
  input: unknown;
  /** For a hook: the pre-hook model output it ran against (parity with Python's `llm_output`). */
  modelOutput?: unknown;
  output?: unknown;
  error?: string;
  /** Present for generations — the 0-based validation attempt. */
  attempt?: number;
  /**
   * Observation-level metadata recorded via `updateMetadata` — e.g. the provider
   * `usage` report (#478), which is how cache hits reach a trace backend.
   * Present only when non-empty.
   */
  metadata?: Record<string, unknown>;
}

/** A backend-agnostic trace for one activity execution. */
export interface EmittedTrace {
  name: string;
  input: unknown;
  /** Temporal workflow identity when available — transports group per run. */
  workflowId?: string;
  runId?: string;
  output?: unknown;
  error?: string;
  metadata: Record<string, unknown>;
  tenant: Record<string, string>;
  observations: EmittedObservation[];
}

/** The injected sink — wire a real Langfuse/LangSmith client (or HTTP) via an adapter. */
export interface TraceTransport {
  submitTrace(trace: EmittedTrace): void | Promise<void>;
}

/** Serialize a thrown value to a string for the emitted payload (errors are `unknown`). */
function serializeError(error: unknown): string {
  if (error instanceof Error) {
    return error.name === "Error" ? error.message : `${error.name}: ${error.message}`;
  }
  return String(error);
}

/** Map a recorded activity to the backend-agnostic emitted trace. */
export function toEmittedTrace(record: RecordedActivity): EmittedTrace {
  const observations: EmittedObservation[] = [
    ...record.generations.map((generation): EmittedObservation => {
      const out: EmittedObservation = {
        type: "generation",
        model: generation.model,
        input: generation.messages,
        attempt: generation.attempt,
      };
      if (generation.output !== undefined) {
        out.output = generation.output;
      }
      if (generation.error !== undefined) {
        out.error = serializeError(generation.error);
      }
      if (Object.keys(generation.metadata).length > 0) {
        out.metadata = generation.metadata;
      }
      return out;
    }),
    ...record.hooks.map((hook): EmittedObservation => {
      const out: EmittedObservation = { type: "hook", input: hook.input, modelOutput: hook.modelOutput };
      if (hook.output !== undefined) {
        out.output = hook.output;
      }
      if (hook.error !== undefined) {
        out.error = serializeError(hook.error);
      }
      if (Object.keys(hook.metadata).length > 0) {
        out.metadata = hook.metadata;
      }
      return out;
    }),
  ];
  const trace: EmittedTrace = {
    name: record.activityName,
    input: record.input,
    metadata: record.metadata,
    tenant: record.tenant,
    observations,
    ...(record.workflowId !== undefined ? { workflowId: record.workflowId } : {}),
    ...(record.runId !== undefined ? { runId: record.runId } : {}),
  };
  if (record.output !== undefined) {
    trace.output = record.output;
  }
  if (record.error !== undefined) {
    trace.error = serializeError(record.error);
  }
  return trace;
}

/**
 * An `ActivityObserver` that buffers executions and `flush()`es each as an
 * `EmittedTrace` through the injected transport. Pass an instance as
 * `executeActivity`'s `observer`, and `await writer.flush()` when the worker drains
 * (Python flushes on worker shutdown).
 */
export class TraceWriter extends CollectingObserver {
  constructor(
    private readonly transport: TraceTransport,
    /** When set, redact PII from each trace's data fields before submit. */
    private readonly redaction?: RedactionConfig,
  ) {
    super();
  }

  /** Emit every ENDED buffered trace, then drop those from the buffer. */
  async flush(): Promise<void> {
    // Drain first so a concurrent execution recorded during the awaits is not lost or
    // double-sent — its trace stays buffered for the next flush. Only ENDED records
    // ship: a concurrent activity still executing (a map step's sibling, an
    // overlapping workflow on the same worker) must not emit half-complete and lose
    // its output — `end()` runs in the executor's finally, so every record ends and
    // ships on a later flush.
    const pending: RecordedActivity[] = [];
    for (let i = this.activities.length - 1; i >= 0; i -= 1) {
      if (this.activities[i]!.ended) {
        pending.unshift(...this.activities.splice(i, 1));
      }
    }
    for (let i = 0; i < pending.length; i += 1) {
      const trace = toEmittedTrace(pending[i]!);
      const emitted = this.redaction !== undefined ? redactEmittedTrace(trace, this.redaction) : trace;
      try {
        await this.transport.submitTrace(emitted);
      } catch (error) {
        // Re-buffer the failed record + the rest (at the front, preserving order) so a
        // transient transport failure doesn't drop traces — the caller can retry flush.
        this.activities.unshift(...pending.slice(i));
        throw error;
      }
    }
  }
}

/**
 * Redact PII from a trace's data-bearing fields. `metadata` is redacted at its own
 * root so the internal-path excludes (`typeflux.*`, …) align; input/output/error are
 * free-form user data with no relevant excludes, so they redact fully.
 */
function redactEmittedTrace(trace: EmittedTrace, config: RedactionConfig): EmittedTrace {
  const out: EmittedTrace = {
    ...trace,
    input: redactMetadata(trace.input, config),
    metadata: redactMetadata(trace.metadata, config),
    observations: trace.observations.map((observation): EmittedObservation => {
      const redacted: EmittedObservation = { ...observation, input: redactMetadata(observation.input, config) };
      if (redacted.modelOutput !== undefined) {
        redacted.modelOutput = redactMetadata(redacted.modelOutput, config);
      }
      if (redacted.output !== undefined) {
        redacted.output = redactMetadata(redacted.output, config);
      }
      if (redacted.error !== undefined) {
        redacted.error = redactMetadata(redacted.error, config);
      }
      if (redacted.metadata !== undefined) {
        // Redacted at its own root, like the trace-level metadata, so the
        // internal-path excludes (`typeflux.*`, …) align.
        redacted.metadata = redactMetadata(redacted.metadata, config) as Record<string, unknown>;
      }
      return redacted;
    }),
  };
  if (trace.output !== undefined) {
    out.output = redactMetadata(trace.output, config);
  }
  if (trace.error !== undefined) {
    out.error = redactMetadata(trace.error, config);
  }
  return out;
}
