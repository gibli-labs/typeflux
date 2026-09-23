/** The last-write-wins guard for manual fetches (#786). */
import { describe, expect, it } from "vitest";

import { createStaleGuard } from "./staleGuard";

describe("createStaleGuard (#786)", () => {
  it("the latest request is current until superseded", () => {
    const guard = createStaleGuard();
    const first = guard.next();
    expect(first()).toBe(true);
    const second = guard.next();
    expect(first()).toBe(false); // a late first response must not land
    expect(second()).toBe(true);
  });

  it("invalidate() marks the in-flight request stale without starting a new one", () => {
    const guard = createStaleGuard();
    const inFlight = guard.next();
    guard.invalidate(); // the selection changed while the response was in flight
    expect(inFlight()).toBe(false);
    const next = guard.next();
    expect(next()).toBe(true);
  });

  it("observe() stays true until any later next() or invalidate() (deferred follow-ups)", () => {
    const guard = createStaleGuard();
    const unchanged = guard.observe();
    expect(unchanged()).toBe(true);
    guard.next(); // a newer check ran while the follow-up waited
    expect(unchanged()).toBe(false);
    const afterCheck = guard.observe();
    guard.invalidate(); // the identity changed while the follow-up waited
    expect(afterCheck()).toBe(false);
  });

  it("guards are independent per instance", () => {
    const a = createStaleGuard();
    const b = createStaleGuard();
    const aReq = a.next();
    b.invalidate();
    expect(aReq()).toBe(true);
  });
});
