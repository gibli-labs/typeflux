/**
 * Task-queue worker presence for the TS control plane (#686; Python `project/workers.py`
 * `workflow_task_queue_workers`, #278). A workflow started against a task queue no worker polls
 * sits pending forever; this reports the LIVE poller count for the workflow's resolved task queue
 * so the console can warn before the operator is left wondering.
 *
 * Runtime-neutral by construction: a task-queue describe knows nothing about workflow types or
 * memos, so the projection is identical across bindings — only the connection mapping is the
 * ts profile's (`temporalConnectionOptions`).
 *
 * DEGRADATION POSTURE (Python parity): a connect/describe failure degrades IN-BAND
 * (`reachable: false` + detail) — never a 500, and not the 503 either; the panel renders the
 * warning. Only a HANG hits the Temporal-tier bound (#581) and answers 503, exactly like
 * Python's route (`workflow_task_queue_workers` catches its own exceptions; `_temporal_bounded`
 * catches only the timeout).
 */

import type { TypefluxYamlSpec } from "@typeflux/temporal-yaml";

import {
  boundedTemporalTier,
  type TemporalConnectionOptions,
  temporalConnectionOptions,
  temporalTierTimeoutSeconds,
} from "./executions.js";

/** Python `WorkflowTaskQueueWorkers` (route serializes with exclude_none — `detail` omits when absent). */
export interface ApiWorkflowTaskQueueWorkers {
  task_queue: string;
  reachable: boolean;
  workers_polling: number;
  detail?: string;
}

/** The Temporal-tier seam: a workflow-task-queue poller count plus a close handle. */
export interface TaskQueueClient {
  /** The number of WORKFLOW-task pollers on the queue (Python `_poller_count`). */
  pollerCount(taskQueue: string): Promise<number>;
  close(): Promise<void>;
}

export type TaskQueueClientFactory = (options: TemporalConnectionOptions) => Promise<TaskQueueClient>;

/**
 * The default client: a real `@temporalio/client` connection driving the raw
 * `workflowService.describeTaskQueue` RPC (lazily imported — test seams never load it).
 */
export const defaultTaskQueueClient: TaskQueueClientFactory = async (options) => {
  const { Connection } = await import("@temporalio/client");
  const connection = await Connection.connect({
    address: options.address,
    tls: options.tls,
    // Python's temporalio fast-fails a refused connect, which is what puts the
    // failure on the IN-BAND degrade path; grpc-js instead retries a refused
    // endpoint until its connect deadline, which would otherwise exhaust the
    // route's Temporal-tier bound and turn every unreachable cluster into the
    // 503 hang answer. Half the tier bound keeps the connect failure INSIDE
    // the degrade net (reachable: false), reserving the 503 for a real hang.
    connectTimeout: Math.max(1, temporalTierTimeoutSeconds() / 2) * 1000,
    ...(options.apiKey !== undefined ? { apiKey: options.apiKey } : {}),
  });
  return {
    pollerCount: async (taskQueue: string) => {
      const response = await connection.workflowService.describeTaskQueue({
        namespace: options.namespace,
        taskQueue: { name: taskQueue },
        // TASK_QUEUE_TYPE_WORKFLOW (temporal.api.enums.v1.TaskQueueType) — the workflow pollers,
        // matching Python's `TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW`.
        taskQueueType: 1,
      });
      return response.pollers?.length ?? 0;
    },
    close: () => connection.close(),
  };
};

export interface WorkflowTaskQueueWorkersOptions {
  workflowId: string;
  environmentId: string;
  /** An explicit override (the start panel's task-queue field); empty string falls back (Python `or`). */
  taskQueue?: string | undefined;
  /** Injectable Temporal seam; defaults to a real `@temporalio/client` connection. */
  clientFactory?: TaskQueueClientFactory;
}

/** Report the live poller count for the workflow's task queue (Python `workflow_task_queue_workers`). */
export async function workflowTaskQueueWorkers(
  spec: TypefluxYamlSpec,
  options: WorkflowTaskQueueWorkersOptions,
): Promise<ApiWorkflowTaskQueueWorkers> {
  // Python `task_queue or resolved.spec.task_queue` — truthiness on purpose: an empty-string
  // override falls back to the resolved queue (the pinned "" parity rule).
  const queue = options.taskQueue || spec.task_queue;
  const factory = options.clientFactory ?? defaultTaskQueueClient;

  // A config error in the connection mapping (bad TLS block, required-but-unset api_key) is a 422
  // `TsBindingConfigError` — OUTSIDE the degrade net, like Python's resolution step: only the live
  // Temporal tier (connect/describe) degrades in-band.
  const connection = temporalConnectionOptions(spec);

  const run = async (): Promise<ApiWorkflowTaskQueueWorkers> => {
    let client: TaskQueueClient;
    try {
      client = await factory(connection);
    } catch (error) {
      // Degrade, never 500 the panel (Python's `except Exception` around connect+describe).
      return degraded(queue, error);
    }
    try {
      const count = await client.pollerCount(queue);
      return { task_queue: queue, reachable: true, workers_polling: count };
    } catch (error) {
      return degraded(queue, error);
    } finally {
      await client.close().catch(() => undefined);
    }
  };

  // The bound catches only a HANG (503); connect/describe failures degraded in-band above.
  return boundedTemporalTier(run, "describing task-queue workers");
}

const degraded = (queue: string, error: unknown): ApiWorkflowTaskQueueWorkers => ({
  task_queue: queue,
  reachable: false,
  workers_polling: 0,
  // Python puts the raw `{exc}` here — cluster reachability text (address class), not a secret
  // slot; the message prefix is the cross-edition parity surface.
  detail: `could not reach Temporal: ${error instanceof Error ? error.message : String(error)}`,
});
