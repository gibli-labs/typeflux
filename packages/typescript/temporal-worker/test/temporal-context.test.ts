import { describe, expect, it } from "vitest";

import { currentActivityContext, currentActivityHeartbeater, temporalInfoToContext } from "../src/index.js";
// An internal cadence detail — deliberately not part of the package surface (#493).
import { heartbeatIntervalMs } from "../src/temporal-context.js";

describe("temporalInfoToContext (#450)", () => {
  it("maps a workflow-scheduled activity Info to the full context fields", () => {
    expect(
      temporalInfoToContext({
        activityId: "act-1",
        attempt: 3,
        taskQueue: "tf-queue",
        namespace: "default",
        workflowExecution: { workflowId: "wf-1", runId: "run-1" },
      }),
    ).toEqual({
      activityId: "act-1",
      attempt: 3,
      taskQueue: "tf-queue",
      namespace: "default",
      workflowId: "wf-1",
      runId: "run-1",
    });
  });

  it("omits workflowId/runId when the activity was not scheduled by a workflow", () => {
    const ctx = temporalInfoToContext({
      activityId: "act-2",
      attempt: 1,
      taskQueue: "tf-queue",
      namespace: "default",
    });
    expect(ctx).toEqual({ activityId: "act-2", attempt: 1, taskQueue: "tf-queue", namespace: "default" });
    expect("workflowId" in ctx).toBe(false);
    expect("runId" in ctx).toBe(false);
  });

  it("omits namespace when absent (no undefined-valued key under exactOptionalPropertyTypes)", () => {
    const ctx = temporalInfoToContext({ activityId: "act-3", attempt: 1, taskQueue: "q" });
    expect(ctx).toEqual({ activityId: "act-3", attempt: 1, taskQueue: "q" });
    expect("namespace" in ctx).toBe(false);
  });
});

describe("currentActivityContext (#450)", () => {
  it("returns undefined when not running inside a Temporal worker activity", () => {
    // No ambient Temporal activity here -> activityInfo() throws -> undefined.
    expect(currentActivityContext()).toBeUndefined();
  });
});

describe("heartbeatIntervalMs (#484)", () => {
  it("returns undefined when no heartbeat timeout is set (Temporal then forbids heartbeating)", () => {
    expect(heartbeatIntervalMs(undefined)).toBeUndefined();
  });

  it("returns undefined for a non-positive timeout", () => {
    expect(heartbeatIntervalMs(0)).toBeUndefined();
    expect(heartbeatIntervalMs(-5)).toBeUndefined();
  });

  it("beats at a third of the timeout (parity with Python's heartbeat_interval_for)", () => {
    expect(heartbeatIntervalMs(30_000)).toBe(10_000);
    expect(heartbeatIntervalMs(6_000)).toBe(2_000);
  });

  it("applies a 1s floor for timeouts >= 2s (parity with Python, no hammering)", () => {
    // 2000/3 ≈ 666ms -> floored to 1000ms; 1000 <= 2000/2 so the cap does not bind.
    expect(heartbeatIntervalMs(2_000)).toBe(1_000);
  });

  it("caps the interval below the timeout so a beat lands before the deadline (timeouts under 2s)", () => {
    // For any timeout under 2s the half-timeout cap beats the floor, keeping the first beat
    // strictly before the deadline (parity with Python's heartbeat_interval_for, #492). 1500 pins
    // the (1s, 2s) region where the floor alone would have returned 1000.
    expect(heartbeatIntervalMs(1_500)).toBe(750);
    expect(heartbeatIntervalMs(1_000)).toBe(500); // floor 1000 > 500 -> capped to 1000/2
    expect(heartbeatIntervalMs(900)).toBe(450);
  });

  it("never spins on a pathological sub-100ms timeout (50ms absolute floor)", () => {
    // A 5ms timeout must not produce a kHz loop cadence; the activity is doomed to
    // heartbeat-timeout either way, so bound the local churn.
    expect(heartbeatIntervalMs(5)).toBe(50);
  });
});

describe("currentActivityHeartbeater (#484)", () => {
  it("returns undefined when not running inside a Temporal worker activity", () => {
    // No ambient Temporal activity here -> activityInfo() throws -> undefined (no loop, nothing to stop).
    expect(currentActivityHeartbeater()).toBeUndefined();
  });
});
