/**
 * Temporal activity context mapping (parity Epic 3, #450). Maps a Temporal
 * activity `Info` into the core's `ActivityContextOverrides` so a context-aware
 * activity hook sees the durable invocation context (workflowId/runId/attempt/…),
 * parity with Python's `invocation_context`. Also provides the worker-side activity
 * heartbeater (#484), keeping `@temporalio/activity` out of `buildTemporalActivity`.
 *
 * The pure parts (`temporalInfoToContext`, `heartbeatIntervalMs`) are unit-testable with plain
 * values; `currentActivityContext` / `currentActivityHeartbeater` read the ambient Temporal
 * context and return `undefined` when not running inside a worker activity (so an activity built
 * with them still runs standalone — e.g. in a direct `executeActivity` call or a test).
 */

import { activityInfo, Context, heartbeat } from "@temporalio/activity";
import type { ActivityContextOverrides } from "@typeflux/temporal";

/** The subset of a Temporal activity `Info` that maps into `ActivityContext`. */
export interface TemporalActivityInfo {
  readonly activityId: string;
  readonly attempt: number;
  readonly taskQueue: string;
  readonly namespace?: string;
  /** Absent when the activity was not scheduled by a workflow. */
  readonly workflowExecution?: { readonly workflowId: string; readonly runId: string };
  /** The activity's heartbeat timeout (ms), when one was set on the call. */
  readonly heartbeatTimeoutMs?: number;
}

/**
 * Derive a background heartbeat cadence (ms) from an activity's heartbeat timeout — parity with
 * Python's `heartbeat_interval_for`: beat at roughly a third of the timeout (so two consecutive
 * misses are needed before Temporal fails the activity), with a 1s floor to avoid hammering the
 * server on a short timeout. Returns `undefined` when no (or a non-positive) timeout is set —
 * Temporal requires that the activity then NOT heartbeat.
 *
 * For any timeout under 2s the result is instead capped at half the timeout, so the first beat
 * lands strictly before the deadline (the bare floor would schedule it at/after a ≤1s deadline and
 * let the activity time out anyway). A 50ms absolute floor keeps a pathological sub-100ms timeout
 * from spinning the event loop at kHz — such an activity is doomed to heartbeat-timeout regardless.
 * Identical to Python's `heartbeat_interval_for` (#492) across the whole range.
 */
export function heartbeatIntervalMs(heartbeatTimeoutMs: number | undefined): number | undefined {
  if (heartbeatTimeoutMs === undefined || heartbeatTimeoutMs <= 0) {
    return undefined;
  }
  return Math.min(Math.max(1000, heartbeatTimeoutMs / 3), Math.max(50, heartbeatTimeoutMs / 2));
}

/** Map a Temporal activity `Info` to the core context fields (pure). */
export function temporalInfoToContext(info: TemporalActivityInfo): ActivityContextOverrides {
  const fields: ActivityContextOverrides = {
    activityId: info.activityId,
    attempt: info.attempt,
    taskQueue: info.taskQueue,
  };
  if (info.namespace !== undefined) {
    fields.namespace = info.namespace;
  }
  if (info.workflowExecution !== undefined) {
    fields.workflowId = info.workflowExecution.workflowId;
    fields.runId = info.workflowExecution.runId;
  }
  return fields;
}

/**
 * The current Temporal activity context, or `undefined` when not inside a worker
 * activity. Pass this as `buildTemporalActivity`'s context provider so worker-run
 * activities enrich their hook context while standalone calls stay context-free.
 */
export function currentActivityContext(): ActivityContextOverrides | undefined {
  let info: TemporalActivityInfo;
  try {
    info = activityInfo();
  } catch {
    return undefined; // activityInfo() throws outside an activity execution
  }
  return temporalInfoToContext(info);
}

/**
 * The current Temporal activity's cancellation `AbortSignal`, or `undefined` when not inside a
 * worker activity. Pass this as `buildTemporalActivity`'s `cancellationSignal` injection so a
 * worker-run activity aborts cooperatively on workflow cancel (#487) while standalone calls run
 * uncancelled. NOTE: Temporal only DELIVERS cancellation to an activity that heartbeats — pair
 * with a heartbeat timeout (#484/#488) for prompt cancellation.
 */
export function currentActivityCancellationSignal(): AbortSignal | undefined {
  try {
    return Context.current().cancellationSignal;
  } catch {
    return undefined; // Context.current() throws outside an activity execution
  }
}

/**
 * A warning sink bound to the current Temporal activity's logger (#478): fail-soft
 * session-cache degradation notices land in the worker's structured logs instead of
 * bare `console.warn`. `undefined` outside a worker activity (the core default applies).
 */
export function currentActivityWarner(): ((message: string) => void) | undefined {
  try {
    const log = Context.current().log;
    return (message: string) => log.warn(message);
  } catch {
    return undefined; // Context.current() throws outside an activity execution
  }
}

/**
 * Start a background heartbeat loop for the current Temporal activity and return a stop function,
 * or `undefined` when there is nothing to do — not inside a worker activity, or the activity has no
 * heartbeat timeout (parity with Python's `heartbeating`: the loop runs only when a timeout is set,
 * at {@link heartbeatIntervalMs} cadence). A heartbeat throwing is swallowed — a heartbeat failure
 * must never kill the activity. The timer is `unref`'d so it can't keep the worker process alive.
 *
 * This is the beat half of Python's activity lifecycle; the cancel half is
 * {@link currentActivityCancellationSignal} (#487). The two pair up: heartbeats are what let
 * Temporal DELIVER a cancellation to the running activity.
 *
 * Pass this as `buildTemporalActivity`'s heartbeater so a long-running worker activity emits
 * heartbeats while standalone calls (no Temporal context) stay heartbeat-free.
 */
export function currentActivityHeartbeater(): (() => void) | undefined {
  let info: TemporalActivityInfo;
  try {
    info = activityInfo();
  } catch {
    return undefined; // not inside an activity execution
  }
  const interval = heartbeatIntervalMs(info.heartbeatTimeoutMs);
  if (interval === undefined) {
    return undefined; // no heartbeat timeout -> Temporal requires no heartbeat
  }
  const timer = setInterval(() => {
    try {
      heartbeat();
    } catch {
      // A heartbeat failure (e.g. details not serializable, or a transient state) must not
      // propagate out of the timer and crash the activity.
    }
  }, interval);
  timer.unref?.();
  return () => clearInterval(timer);
}
