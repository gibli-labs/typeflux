/**
 * N-way environment comparison matrix (#611, #577 §8): every workflow's match state against
 * ONE base environment, so "is prod behind staging everywhere?" is a glance instead of N
 * pairwise diff visits. Cells classify spec-digest and policy-hash drift; the existing
 * pairwise diff page stays the drill-down.
 *
 * Honesty conventions (#604/#605): a fetch error is `unresolvable`, never drift; a
 * first-load pending cell is `pending` and the matrix reports itself unsettled; a background
 * refetch keeps the cached verdict (surfaced via `refreshing`, not by blanking). A base that
 * does not resolve makes the whole row not-comparable — a note, not an alarm.
 */

import type { BundleCell } from "./queries";

export type MatrixCellState = "match" | "drift" | "unresolvable" | "pending";

export interface MatrixDrift {
  spec: boolean;
  policy: boolean;
  /** A secret slot configured on one side but not the other — the pairwise feed treats this
   * as critical, so the matrix must not show green above it (codex). */
  secret: boolean;
}

export interface MatrixCell {
  env: string;
  state: MatrixCellState;
  /** Present exactly when state is "drift". */
  drift?: MatrixDrift;
}

export interface MatrixRow {
  workflowId: string;
  /** False when the BASE environment does not resolve — cells then carry only their own
   * resolvability, never a drift verdict (nothing to compare against). */
  comparable: boolean;
  cells: MatrixCell[];
}

export interface EnvMatrix {
  rows: MatrixRow[];
  /** "workflow does not resolve in base" style notes, deduped. */
  notes: string[];
  /** Any first-load cell still in flight — the matrix is not a verdict yet. */
  settled: boolean;
  /** Any background refetch in flight — verdicts stay, with a still-comparing hint. */
  refreshing: boolean;
}

/** Version identity: the graph digest PLUS workflow_type/version_label — a frozen-version
 * split leaves the digest identical but is critical in the pairwise feed (codex). */
const digestOf = (cell: BundleCell): string | undefined => {
  const workflow = cell.bundle?.workflow;
  if (workflow === undefined) return undefined;
  return `${workflow.spec_digest}#${workflow.workflow_type}#${workflow.version_label ?? ""}`;
};
/** Policy identity: the composed hash PLUS the sorted selected ids — a failed composition
 * empties the hash, so two environments with different invalid selections must not compare
 * equal on hash alone (codex; the pairwise feed flags selected_policy_ids changes). */
const policyOf = (cell: BundleCell): string | null => {
  const policy = cell.bundle?.policy;
  if (policy === undefined || policy === null) return null;
  return `${policy.policy_hash}#${JSON.stringify([...(policy.selected_policy_ids ?? [])].sort())}`;
};
/** The configured-secret fingerprint: sorted SLOT->configured pairs, keyed by runtime_path
 * only — the pairwise feed treats `configured` changes as critical but a source RENAME with
 * both sides configured as info, so the name must not enter the drift verdict (codex). */
const secretsOf = (cell: BundleCell): string =>
  ((cell.bundle?.secret_references ?? []) as { runtime_path?: string; configured?: boolean }[])
    .map((ref) => `${ref.runtime_path ?? ""}=${ref.configured === true}`)
    .sort()
    .join(";");
// A boolean, not a type predicate: negating a predicate on an already-BundleCell narrows to
// never; resolvability here is a state, not a type refinement.
const isResolved = (cell: BundleCell | undefined): boolean =>
  cell !== undefined && cell.bundle !== undefined && cell.error === undefined && !cell.pending;

export function deriveEnvMatrix(baseEnv: string, cells: BundleCell[]): EnvMatrix {
  const settled = cells.every((cell) => !cell.pending);
  const refreshing = cells.some((cell) => cell.fetching && !cell.pending);
  const byWorkflow = new Map<string, BundleCell[]>();
  for (const cell of cells) {
    byWorkflow.set(cell.workflowId, [...(byWorkflow.get(cell.workflowId) ?? []), cell]);
  }

  const rows: MatrixRow[] = [];
  const notes: string[] = [];
  for (const [workflowId, workflowCells] of byWorkflow) {
    const base = workflowCells.find((cell) => cell.env === baseEnv);
    const others = workflowCells.filter((cell) => cell.env !== baseEnv);
    const baseResolved = isResolved(base);
    if (!baseResolved && settled) {
      notes.push(`${workflowId} does not resolve in ${baseEnv} — row not comparable`);
    }
    rows.push({
      workflowId,
      comparable: baseResolved,
      cells: others.map((cell): MatrixCell => {
        if (cell.pending) return { env: cell.env, state: "pending" };
        if (!isResolved(cell)) return { env: cell.env, state: "unresolvable" };
        if (!baseResolved || base === undefined) {
          // Own resolvability only — no drift verdict without a base to compare against.
          return { env: cell.env, state: "unresolvable" };
        }
        const drift: MatrixDrift = {
          spec: digestOf(cell) !== digestOf(base),
          policy: policyOf(cell) !== policyOf(base),
          secret: secretsOf(cell) !== secretsOf(base),
        };
        if (!drift.spec && !drift.policy && !drift.secret) return { env: cell.env, state: "match" };
        return { env: cell.env, state: "drift", drift };
      }),
    });
  }

  rows.sort((a, b) => {
    // Rows with any drift first, then not-comparable, then all-match — triage order.
    const worst = (row: MatrixRow): number =>
      row.cells.some((cell) => cell.state === "drift") ? 0 : row.comparable ? 2 : 1;
    return worst(a) - worst(b) || a.workflowId.localeCompare(b.workflowId);
  });

  return { rows, notes: [...new Set(notes)], settled, refreshing };
}
