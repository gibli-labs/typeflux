/**
 * Activity observability contract (parity Epic 4, #451) — the write-side port of the
 * Python `AIActivityObserver` (`execution/observer.py`). `executeActivity` drives an
 * observer through the execution: one `observeActivity` span per call, an
 * `observeGeneration` handle around each provider call attempt, and `updateOutput` /
 * `updateError` to record the outcome.
 *
 * Python uses nested context managers; the TS port uses handles the executor updates
 * explicitly (and `end()` in a `finally`). `NoOpObserver` is the default;
 * `CollectingObserver` records observations in memory — the base a trace writer
 * (Langfuse/LangSmith, a later PR) extends, and what tests assert on.
 */

import type { ChatMessage } from "./manifest-hashing.js";

/** A single observation span — record its output/error/metadata as the work completes. */
export interface ObservationHandle {
  updateOutput(output: unknown): void;
  updateError(error: unknown): void;
  updateMetadata(metadata: Record<string, unknown>): void;
}

/** Parameters of a single provider (LLM) call attempt. */
export interface GenerationParams {
  messages: ChatMessage[];
  model: string | null;
  /** 0-based output-validation attempt (a validation-repair retry increments it). */
  attempt: number;
}

/** The per-activity observation: open a generation/hook handle, and record the activity outcome. */
export interface ActivityObservation extends ObservationHandle {
  /** Open a handle around one provider call attempt. */
  observeGeneration(params: GenerationParams): ObservationHandle;
  /** Open a handle around the context hook (post-validation transform). */
  observeHook(params: { input: unknown; output: unknown }): ObservationHandle;
  /** Close the activity span (called in a `finally`, after success or error). */
  end(): void;
}

/** Parameters of an activity execution span. */
export interface ObserveActivityParams {
  activityName: string;
  input: unknown;
  messages: ChatMessage[];
  model: string | null;
  tenant: Record<string, string>;
  /** Temporal workflow identity when run under a worker (parity with Python's
   * invocation_context) — transports GROUP activity traces per run with it. */
  workflowId?: string;
  runId?: string;
}

/** Observes activity executions. Supply one via `executeActivity`'s `observer` option. */
export interface ActivityObserver {
  observeActivity(params: ObserveActivityParams): ActivityObservation;
  /** Flush any buffered observations to the backend (a writer awaits I/O here). */
  flush(): void | Promise<void>;
}

const NO_OP_HANDLE: ObservationHandle = {
  updateOutput() {},
  updateError() {},
  updateMetadata() {},
};

class NoOpActivityObservation implements ActivityObservation {
  updateOutput(): void {}
  updateError(): void {}
  updateMetadata(): void {}
  observeGeneration(): ObservationHandle {
    return NO_OP_HANDLE;
  }
  observeHook(): ObservationHandle {
    return NO_OP_HANDLE;
  }
  end(): void {}
}

/** The default observer — records nothing. */
export class NoOpObserver implements ActivityObserver {
  observeActivity(): ActivityObservation {
    return new NoOpActivityObservation();
  }
  flush(): void {}
}

/** A shared no-op observer instance (the `executeActivity` default). */
export const NO_OP_OBSERVER: ActivityObserver = new NoOpObserver();

/** A recorded generation (provider-call attempt). */
export interface RecordedGeneration {
  messages: ChatMessage[];
  model: string | null;
  attempt: number;
  output?: unknown;
  error?: unknown;
  metadata: Record<string, unknown>;
}

/** A recorded hook observation. */
export interface RecordedHook {
  /** The activity input. */
  input: unknown;
  /** The pre-hook model output the hook transforms (the hook's actual input). */
  modelOutput: unknown;
  /** The hook's result — set only on success, so a failed hook has no stale output. */
  output?: unknown;
  error?: unknown;
  metadata: Record<string, unknown>;
}

/** A recorded activity execution. */
export interface RecordedActivity {
  activityName: string;
  input: unknown;
  messages: ChatMessage[];
  model: string | null;
  tenant: Record<string, string>;
  workflowId?: string;
  runId?: string;
  generations: RecordedGeneration[];
  hooks: RecordedHook[];
  output?: unknown;
  error?: unknown;
  metadata: Record<string, unknown>;
  ended: boolean;
}

class CollectingObservation implements ActivityObservation {
  constructor(private readonly record: RecordedActivity) {}
  updateOutput(output: unknown): void {
    this.record.output = output;
  }
  updateError(error: unknown): void {
    this.record.error = error;
  }
  updateMetadata(metadata: Record<string, unknown>): void {
    Object.assign(this.record.metadata, metadata);
  }
  observeGeneration(params: GenerationParams): ObservationHandle {
    const generation: RecordedGeneration = { ...params, metadata: {} };
    this.record.generations.push(generation);
    return {
      updateOutput: (output) => {
        generation.output = output;
      },
      updateError: (error) => {
        generation.error = error;
      },
      updateMetadata: (metadata) => Object.assign(generation.metadata, metadata),
    };
  }
  observeHook(params: { input: unknown; output: unknown }): ObservationHandle {
    // `output` (the hook RESULT) is left unset until `updateOutput`, so a hook that throws
    // does not emit the pre-hook model output as if the hook had produced it.
    const hook: RecordedHook = { input: params.input, modelOutput: params.output, metadata: {} };
    this.record.hooks.push(hook);
    return {
      updateOutput: (output) => {
        hook.output = output;
      },
      updateError: (error) => {
        hook.error = error;
      },
      updateMetadata: (metadata) => Object.assign(hook.metadata, metadata),
    };
  }
  end(): void {
    this.record.ended = true;
  }
}

/**
 * Records every observation in memory — the base for a trace writer (a later PR
 * flushes `activities` to Langfuse/LangSmith) and what tests assert on.
 */
export class CollectingObserver implements ActivityObserver {
  readonly activities: RecordedActivity[] = [];

  observeActivity(params: ObserveActivityParams): ActivityObservation {
    const record: RecordedActivity = {
      ...params,
      generations: [],
      hooks: [],
      metadata: {},
      ended: false,
    };
    this.activities.push(record);
    return new CollectingObservation(record);
  }

  flush(): void {}
}
