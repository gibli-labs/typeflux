/**
 * Worker bootstrap (parity Epic 3, #450) — a thin wrapper over `@temporalio/worker`
 * `Worker.create` that registers a Typeflux activity map on a task queue. The
 * option assembly is factored into the pure `workerCreateOptions` so it is
 * unit-testable without a live Temporal server; `createTypefluxWorker` just feeds
 * its result to `Worker.create` (which connects to a server).
 */

import { type NativeConnection, Worker, type WorkerOptions } from "@temporalio/worker";

/** A registered activity map — e.g. the result of `buildTemporalActivities`. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type TemporalActivities = Record<string, (...args: any[]) => Promise<unknown>>;

export interface TypefluxWorkerOptions {
  /** The task queue this worker polls (required). */
  taskQueue: string;
  /** The activity functions to register, keyed by name. */
  activities: TemporalActivities;
  /** Defaults to the connection's namespace when omitted. */
  namespace?: string;
  /** A `NativeConnection` to the Temporal server (defaults to localhost). */
  connection?: NativeConnection;
  /** Path to the workflows module (omit for an activity-only worker). */
  workflowsPath?: string;
  /** A pre-built workflow bundle (alternative to `workflowsPath`). */
  workflowBundle?: WorkerOptions["workflowBundle"];
  /**
   * A `DataConverter` (e.g. one carrying an AES-256-GCM payload codec, #188). Passed to
   * `Worker.create` so activity/workflow payloads are encrypted on the wire. Omit for the
   * default (plaintext) converter.
   */
  dataConverter?: WorkerOptions["dataConverter"];
  /** Passthrough of any other `@temporalio` `WorkerOptions`. */
  workerOptions?: Partial<WorkerOptions>;
}

/**
 * Assemble the `@temporalio` `WorkerOptions` (pure — no server connection). Throws
 * if `taskQueue` is missing. Only defined fields are set, so it composes cleanly
 * under `exactOptionalPropertyTypes`.
 */
export function workerCreateOptions(options: TypefluxWorkerOptions): WorkerOptions {
  if (!options.taskQueue) {
    throw new Error("createTypefluxWorker: taskQueue is required");
  }
  const merged: WorkerOptions = {
    ...options.workerOptions,
    taskQueue: options.taskQueue,
    activities: options.activities,
  };
  if (options.namespace !== undefined) {
    merged.namespace = options.namespace;
  }
  if (options.connection !== undefined) {
    merged.connection = options.connection;
  }
  if (options.workflowsPath !== undefined) {
    merged.workflowsPath = options.workflowsPath;
  }
  if (options.workflowBundle !== undefined) {
    merged.workflowBundle = options.workflowBundle;
  }
  if (options.dataConverter !== undefined) {
    merged.dataConverter = options.dataConverter;
  }
  return merged;
}

/** Create a Temporal worker that serves the given Typeflux activities. */
export function createTypefluxWorker(options: TypefluxWorkerOptions): Promise<Worker> {
  return Worker.create(workerCreateOptions(options));
}
