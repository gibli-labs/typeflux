/**
 * Environment-page row engine (#605): the pure derivations behind the environment-centric
 * slice of the Overview — every workflow's state IN ONE environment (admission, policy,
 * digest, insight rollup), structured profile-selection rows with differs-from-default
 * markers, and a flattened override table. The page renders the output; the honesty
 * conventions match the cross-env feed (#604): pending/fetching cells are never verdicts,
 * fetch errors are "unresolvable", not failures.
 */

import type { WorkflowSummary } from "./api";
import { deriveBundleInsights } from "./insights";
import type { Severity } from "./insights";
import type { BundleCell } from "./queries";

export interface EnvironmentWorkflowRow {
  workflowId: string;
  state: "ok" | "failing" | "unresolvable" | "pending";
  /** Present for resolved rows. */
  policies: string[];
  policyHash?: string;
  specDigest?: string;
  /** Worst insight severity + count for the resolved bundle (drives the row badge). */
  worstSeverity?: Severity;
  insightCount: number;
  /** The unresolvable reason or first failing validation codes. */
  detail?: string;
}

export function deriveEnvironmentRows(env: string, cells: BundleCell[]): EnvironmentWorkflowRow[] {
  return cells
    .filter((cell) => cell.env === env)
    .map((cell): EnvironmentWorkflowRow => {
      if (cell.pending) {
        return { workflowId: cell.workflowId, state: "pending", policies: [], insightCount: 0 };
      }
      if (cell.bundle === undefined || cell.error !== undefined) {
        return {
          workflowId: cell.workflowId,
          state: "unresolvable",
          policies: [],
          insightCount: 0,
          ...(cell.error !== undefined ? { detail: cell.error } : {}),
        };
      }
      const bundle = cell.bundle;
      const insights = deriveBundleInsights(cell.workflowId, env, bundle);
      const worst = insights[0]?.severity;
      const failingCodes = bundle.validation.ok
        ? []
        : [...new Set(bundle.validation.issues.map((issue) => issue.code))].slice(0, 3);
      return {
        workflowId: cell.workflowId,
        state: bundle.validation.ok ? "ok" : "failing",
        policies: [...(bundle.policy?.selected_policy_ids ?? [])],
        ...(bundle.policy?.policy_hash !== undefined ? { policyHash: bundle.policy.policy_hash } : {}),
        specDigest: bundle.workflow.spec_digest,
        ...(worst !== undefined ? { worstSeverity: worst } : {}),
        insightCount: insights.length,
        ...(failingCodes.length > 0 ? { detail: failingCodes.join(", ") } : {}),
      };
    })
    .sort((a, b) => {
      // Failing first, then unresolvable, pending, ok — triage order, stable by id.
      const rank = (row: EnvironmentWorkflowRow): number =>
        row.state === "failing" ? 0 : row.state === "unresolvable" ? 1 : row.state === "pending" ? 2 : 3;
      return rank(a) - rank(b) || a.workflowId.localeCompare(b.workflowId);
    });
}

export interface ProfileSelectionRow {
  workflowId: string;
  kind: string;
  selected: string;
  /** The workflow's own manifest-level selection for this kind, when it differs. */
  workflowDefault?: string;
  differsFromDefault: boolean;
}

/** Structured `workflow_profiles` rows with differs-from-workflow-default markers (#577 §8). */
export function deriveProfileSelectionRows(
  workflowProfiles: Record<string, Record<string, string>> | undefined,
  workflows: WorkflowSummary[],
): ProfileSelectionRow[] {
  const defaults = new Map(workflows.map((workflow) => [workflow.id, workflow.profiles ?? {}]));
  const rows: ProfileSelectionRow[] = [];
  for (const [workflowId, selection] of Object.entries(workflowProfiles ?? {})) {
    for (const [kind, selected] of Object.entries(selection ?? {})) {
      const workflowDefault = defaults.get(workflowId)?.[kind];
      const differs = workflowDefault !== undefined && workflowDefault !== selected;
      rows.push({
        workflowId,
        kind,
        selected,
        ...(differs ? { workflowDefault } : {}),
        differsFromDefault: differs,
      });
    }
  }
  return rows.sort(
    (a, b) => a.workflowId.localeCompare(b.workflowId) || a.kind.localeCompare(b.kind),
  );
}

/** Flatten an override tree into sorted dotted-path rows — every leaf is a deliberate
 * deviation from the workflow YAML, so each knob gets its own scannable line. */
export function flattenOverrides(overrides: unknown, prefix = ""): [string, string][] {
  if (typeof overrides !== "object" || overrides === null || Array.isArray(overrides)) {
    return prefix === "" ? [] : [[prefix, JSON.stringify(overrides)]];
  }
  const entries = Object.entries(overrides as Record<string, unknown>);
  if (entries.length === 0) return prefix === "" ? [] : [[prefix, "{}"]];
  return entries
    .flatMap(([key, value]) => flattenOverrides(value, prefix === "" ? key : `${prefix}.${key}`))
    .sort((a, b) => a[0].localeCompare(b[0]));
}

/** True when any row is still pending — the table must render as loading, not a verdict. */
export function rowsSettled(rows: EnvironmentWorkflowRow[]): boolean {
  return rows.every((row) => row.state !== "pending");
}

/** True while any cell background-refetches cached data — resolved rows KEEP their content
 * (the #604 convention: a refetch is a "states may change" hint, never a blanked table). */
export function rowsRefreshing(cells: BundleCell[]): boolean {
  return cells.some((cell) => cell.fetching && !cell.pending);
}

