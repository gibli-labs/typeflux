/**
 * Policy coverage matrix (#612, #577 §2): policies × workflows × environments in one grid —
 * which policy set governs each workflow in each environment, which cells run ungoverned,
 * and which rows have environments disagreeing. The #604 policy-split insight is the "you
 * should look" ping; this is the "see everything" view on the Governance page.
 *
 * Honesty conventions (#604/#605/#611): a fetch error is `unresolvable`, never a coverage
 * verdict; a first-load pending cell gates the matrix; a background refetch keeps cached
 * verdicts (surfaced via `refreshing`). Split detection groups by the LOSSLESS JSON of the
 * sorted id set — display strings never collapse distinct selections.
 */

import type { BundleCell } from "./queries";

export type PolicyCellState =
  | "governed"
  | "ungoverned"
  | "composition-failed"
  | "unresolvable"
  | "pending";

export interface PolicyCell {
  env: string;
  state: PolicyCellState;
  /** Sorted selected policy ids; empty unless governed. */
  ids: string[];
}

export interface PolicyMatrixRow {
  workflowId: string;
  cells: PolicyCell[];
  /** True when at least two RESOLVED environments select different policy sets. */
  split: boolean;
  /** True when any resolved environment runs with no policy at all. */
  ungoverned: boolean;
}

export interface PolicyMatrix {
  rows: PolicyMatrixRow[];
  settled: boolean;
  refreshing: boolean;
}

const isResolved = (cell: BundleCell): boolean =>
  cell.bundle !== undefined && cell.error === undefined && !cell.pending;

export function derivePolicyMatrix(cells: BundleCell[]): PolicyMatrix {
  const settled = cells.every((cell) => !cell.pending);
  const refreshing = cells.some((cell) => cell.fetching && !cell.pending);
  const byWorkflow = new Map<string, BundleCell[]>();
  for (const cell of cells) {
    byWorkflow.set(cell.workflowId, [...(byWorkflow.get(cell.workflowId) ?? []), cell]);
  }

  const rows: PolicyMatrixRow[] = [];
  for (const [workflowId, workflowCells] of byWorkflow) {
    const rowCells = workflowCells.map((cell): PolicyCell => {
      if (cell.pending) return { env: cell.env, state: "pending", ids: [] };
      if (!isResolved(cell)) return { env: cell.env, state: "unresolvable", ids: [] };
      const ids = [...(cell.bundle?.policy?.selected_policy_ids ?? [])].sort();
      // Governed means ENFORCED: a selection whose composition failed applies nothing —
      // the rest of the governance page treats that as unenforced, so must this grid
      // (codex). The attempted ids stay visible on the cell.
      const applied = cell.bundle?.policy?.applied_policy_ids ?? [];
      if (ids.length === 0) return { env: cell.env, state: "ungoverned", ids };
      if (applied.length === 0) return { env: cell.env, state: "composition-failed", ids };
      return { env: cell.env, state: "governed", ids };
    });
    const resolvedSets = rowCells
      .filter(
        (cell) =>
          cell.state === "governed" || cell.state === "ungoverned" || cell.state === "composition-failed",
      )
      .map((cell) => JSON.stringify(cell.ids));
    rows.push({
      workflowId,
      cells: rowCells,
      split: new Set(resolvedSets).size > 1,
      ungoverned: rowCells.some(
        (cell) => cell.state === "ungoverned" || cell.state === "composition-failed",
      ),
    });
  }

  rows.sort((a, b) => {
    // Ungoverned-anywhere first, then split rows, then uniform — triage order, stable by id.
    const rank = (row: PolicyMatrixRow): number => (row.ungoverned ? 0 : row.split ? 1 : 2);
    return rank(a) - rank(b) || a.workflowId.localeCompare(b.workflowId);
  });

  return { rows, settled, refreshing };
}
