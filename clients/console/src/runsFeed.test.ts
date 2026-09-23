import { describe, expect, it } from "vitest";

import type { RuntimePinInfo } from "./api";
import {
  derivePinSkew,
  pinProbeTarget,
  statusRank,
  triageOrder,
  type ExecutionRecord,
  type TriageRow,
} from "./runsFeed";

function row(workflowId: string, executionId: string, status: string, startTime?: string): TriageRow {
  return {
    workflowId,
    record: {
      execution_id: executionId,
      run_id: null,
      workflow_type: `${workflowId}.abc`,
      status,
      start_time: startTime ?? null,
      current_version: true,
    } as unknown as TriageRow["record"],
  };
}

describe("statusRank", () => {
  it("ranks failure classes first, unknown before completed, case-insensitively", () => {
    expect(statusRank("FAILED")).toBe(0);
    expect(statusRank("Timed Out")).toBe(0);
    expect(statusRank("timed-out")).toBe(0);
    expect(statusRank("CANCELLED")).toBe(0);
    expect(statusRank("Running")).toBe(1);
    expect(statusRank("SOME_FUTURE_STATUS")).toBe(2);
    expect(statusRank("COMPLETED")).toBe(3);
  });
});

describe("triageOrder", () => {
  it("orders failure-first, then newest-first, stable on ties", () => {
    const ordered = triageOrder([
      row("wf_a", "ok-new", "COMPLETED", "2026-07-07T10:00:00Z"),
      row("wf_b", "running", "RUNNING", "2026-07-07T09:00:00Z"),
      row("wf_a", "failed-old", "FAILED", "2026-07-01T00:00:00Z"),
      row("wf_c", "failed-new", "FAILED", "2026-07-06T00:00:00Z"),
      row("wf_b", "no-time", "COMPLETED"),
    ]);
    expect(ordered.map((entry) => entry.record.execution_id)).toEqual([
      "failed-new",
      "failed-old",
      "running",
      "ok-new",
      "no-time",
    ]);
  });

  it("breaks full ties deterministically by workflow then execution id", () => {
    const ordered = triageOrder([
      row("wf_b", "x", "RUNNING", "2026-07-07T09:00:00Z"),
      row("wf_a", "y", "RUNNING", "2026-07-07T09:00:00Z"),
    ]);
    expect(ordered.map((entry) => entry.workflowId)).toEqual(["wf_a", "wf_b"]);
  });
});

describe("pinProbeTarget", () => {
  const record = (executionId: string, status: string): ExecutionRecord =>
    ({
      execution_id: executionId,
      run_id: null,
      workflow_type: "wf.abc",
      status,
      start_time: null,
      current_version: true,
    }) as unknown as ExecutionRecord;

  it("prefers the newest RUNNING execution over a newer closed one", () => {
    // The list is newest-first across ALL statuses: a closed run at [0] must not shadow the
    // running one (its lifecycle query may not answer, and live skew shows on the running run).
    const target = pinProbeTarget([
      record("closed-new", "COMPLETED"),
      record("running-old", "Running"),
      record("running-older", "RUNNING"),
    ]);
    expect(target?.execution_id).toBe("running-old");
  });

  it("falls back to the newest execution of any status when nothing is running", () => {
    const target = pinProbeTarget([record("closed-new", "FAILED"), record("closed-old", "COMPLETED")]);
    expect(target?.execution_id).toBe("closed-new");
  });

  it("is undefined for an empty list (no runs — nothing to probe)", () => {
    expect(pinProbeTarget([])).toBeUndefined();
  });
});

describe("derivePinSkew", () => {
  const pin = (digest: string): RuntimePinInfo =>
    ({ spec_digest: digest, pinned_at: "2026-07-07T00:00:00Z" }) as unknown as RuntimePinInfo;

  it("is null when matching, absent, or the bundle digest is unknown", () => {
    expect(derivePinSkew("wf", "local", pin("d1"), "d1")).toBeNull();
    expect(derivePinSkew("wf", "local", null, "d1")).toBeNull();
    expect(derivePinSkew("wf", "local", pin("d1"), undefined)).toBeNull();
  });

  it("emits a warning with both digests and the repin remediation on skew", () => {
    const insight = derivePinSkew("wf", "local", pin("a".repeat(30)), "b".repeat(30));
    expect(insight).not.toBeNull();
    expect(insight?.severity).toBe("warning");
    expect(insight?.title).toContain("aaaaaaaaaa…");
    expect(insight?.title).toContain("bbbbbbbbbb…");
    expect(insight?.detail).toContain("Repin");
  });
});
