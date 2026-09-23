import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Exercise the REAL heartbeat loop (setInterval + heartbeat() + clearInterval) with fake timers and
// a mocked Temporal activity context — the gated live test proves it end to end, this guards it in
// CI. A separate file from temporal-context.test.ts because that suite needs activityInfo() to
// throw (the "outside an activity" path), whereas here we make it return an Info.
const { activityInfoMock, heartbeatMock } = vi.hoisted(() => ({
  activityInfoMock: vi.fn(),
  heartbeatMock: vi.fn(),
}));

vi.mock("@temporalio/activity", () => ({
  activityInfo: () => activityInfoMock(),
  heartbeat: () => heartbeatMock(),
}));

// Imported after vi.mock so the module under test binds the mocked @temporalio/activity.
const { currentActivityHeartbeater } = await import("../src/temporal-context.js");

const infoWith = (heartbeatTimeoutMs?: number) => ({
  activityId: "a",
  attempt: 1,
  taskQueue: "q",
  ...(heartbeatTimeoutMs !== undefined ? { heartbeatTimeoutMs } : {}),
});

describe("currentActivityHeartbeater loop (#484)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    activityInfoMock.mockReset();
    heartbeatMock.mockReset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("beats at the derived interval until stopped", () => {
    activityInfoMock.mockReturnValue(infoWith(3000)); // interval = max(1000, 3000/3) = 1000ms
    const stop = currentActivityHeartbeater();
    expect(stop).toBeDefined();

    vi.advanceTimersByTime(2500);
    expect(heartbeatMock).toHaveBeenCalledTimes(2); // t=1000, 2000 (first beat at t=interval, not 0)

    stop?.();
    vi.advanceTimersByTime(5000);
    expect(heartbeatMock).toHaveBeenCalledTimes(2); // stopped -> no further beats
  });

  it("swallows a heartbeat error so a failed beat never crashes the activity", () => {
    activityInfoMock.mockReturnValue(infoWith(3000));
    heartbeatMock.mockImplementation(() => {
      throw new Error("beat failed");
    });
    const stop = currentActivityHeartbeater();
    expect(() => vi.advanceTimersByTime(2500)).not.toThrow();
    expect(heartbeatMock).toHaveBeenCalled(); // it did attempt to beat
    stop?.();
  });

  it("does not beat when the activity has no heartbeat timeout (Temporal forbids it)", () => {
    activityInfoMock.mockReturnValue(infoWith(undefined));
    expect(currentActivityHeartbeater()).toBeUndefined();
    vi.advanceTimersByTime(5000);
    expect(heartbeatMock).not.toHaveBeenCalled();
  });
});
