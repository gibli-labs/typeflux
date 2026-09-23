/**
 * Runs feed engine (#589): pure derivations for the cross-workflow Runs
 * surface — failure-first triage ordering over execution records, and the
 * runtime-pin-skew insight for the run inspector. The page renders the
 * output; per-run detail stays with the inspector and its deep links.
 */

import type { ExecutionList, RuntimePinInfo } from "./api";
import { middleTruncate } from "./format";
import type { Insight } from "./insights";

export type ExecutionRecord = ExecutionList["executions"][number];

export interface TriageRow {
  workflowId: string;
  record: ExecutionRecord;
}

/** Temporal reports statuses in varying case/spelling across surfaces; one normalization. */
function normalizedStatus(status: string): string {
  return status.trim().toUpperCase().replace(/[\s-]+/g, "_");
}

/**
 * Failure-first status classes. Temporal reports statuses in varying case
 * across surfaces; unknown statuses rank between running and completed so a
 * new status is visible near the top, never buried.
 */
export function statusRank(status: string): number {
  const normalized = normalizedStatus(status);
  if (["FAILED", "TERMINATED", "TIMED_OUT", "CANCELED", "CANCELLED"].includes(normalized)) {
    return 0;
  }
  if (["RUNNING", "WAITING", "WAITING_REVIEW", "CONTINUED_AS_NEW"].includes(normalized)) {
    return 1;
  }
  if (normalized === "COMPLETED") return 3;
  return 2; // unknown: visible, between running and completed
}

/**
 * The execution whose status the Drift page's pin probe reads (#577 §1). The executions list is
 * newest-first across ALL statuses, so blindly probing `executions[0]` lets a newer CLOSED run
 * shadow an older RUNNING one — and a closed run's lifecycle query may not answer, marking the
 * workflow unavailable while real skew on the running execution goes unreported. Prefer the newest
 * RUNNING execution (the one queryable-by-construction class); fall back to the newest execution of
 * any status only when nothing is running — if that probe fails, the caller keeps the workflow as
 * UNAVAILABLE, never an all-clear.
 */
export function pinProbeTarget(executions: ExecutionRecord[]): ExecutionRecord | undefined {
  return (
    executions.find((record) => normalizedStatus(record.status) === "RUNNING") ?? executions[0]
  );
}

/** The Badge kind for a status — aligned with the insights Severity vocabulary. */
export function statusSeverity(status: string): "critical" | "ok" | "info" {
  const rank = statusRank(status);
  if (rank === 0) return "critical";
  if (rank === 3) return "ok";
  return "info";
}

/** Failure-first, then newest-first, stable across workflows. */
export function triageOrder(rows: TriageRow[]): TriageRow[] {
  return [...rows].sort((a, b) => {
    const rank = statusRank(a.record.status) - statusRank(b.record.status);
    if (rank !== 0) return rank;
    const timeA = a.record.start_time ?? "";
    const timeB = b.record.start_time ?? "";
    // ISO-8601 strings compare lexicographically; newest first, absent last.
    if (timeA !== timeB) return timeA > timeB ? -1 : 1;
    return (
      a.workflowId.localeCompare(b.workflowId) ||
      a.record.execution_id.localeCompare(b.record.execution_id)
    );
  });
}

/**
 * Runtime-pin skew (#577 §1): mutating operations are bound to the pinned
 * resolution; a differing spec digest proves the workflow graph moved since
 * the pin. The converse does NOT hold — the digest covers the graph only,
 * so runtime-config/policy changes don't move it (the backend documents
 * "identity, not a freshness verdict"); the inspector therefore offers
 * repin unconditionally and this insight only adds the provable case.
 */
export function derivePinSkew(
  workflowId: string,
  env: string,
  pin: RuntimePinInfo | null | undefined,
  bundleSpecDigest: string | undefined,
): Insight | null {
  const pinned = pin?.spec_digest;
  if (!pinned || !bundleSpecDigest || pinned === bundleSpecDigest) return null;
  return {
    id: `pin-skew:${workflowId}:${env}`,
    severity: "warning",
    title: `Operations are pinned to an older workflow graph (${middleTruncate(pinned)} vs current ${middleTruncate(bundleSpecDigest)})`,
    detail:
      "Start/review/cancel are bound to the runtime pinned at first use " +
      `(${pin?.pinned_at ?? "unknown time"}); the workflow graph has resolved differently since. ` +
      "Repin to drop the pinned runtime so the next mutating call re-resolves.",
    link: `#/workflows/${workflowId}/runs?env=${env}`,
  };
}
