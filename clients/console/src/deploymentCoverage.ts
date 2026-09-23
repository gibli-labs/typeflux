/**
 * Deployment plan coverage (#294): reproject the approved plans from
 * `GET /deployments` into a workflow-centric view — for each
 * (workflow, environment), is the *latest* approved plan still current, and
 * which workflows have no approved plan at all?
 *
 * Pure derivation over the deployment entries (each already carries live
 * verification against the current resolution); the page renders the output.
 */

import type { components } from "@typeflux/control-plane-client";

type DeploymentEntry = components["schemas"]["_DeploymentEntry"];
type PlanMismatch = components["schemas"]["PlanMismatch"];

export interface CoverageRow {
  workflowId: string;
  environmentId: string;
  planId: string;
  workflowType: string;
  generatedAt: string;
  state: "ready" | "drifted";
  mismatches: PlanMismatch[];
}

/**
 * The latest approved plan per (workflow, environment), classified ready vs
 * drifted. "Latest" is the greatest `generated_at` (ISO-8601, lexicographically
 * comparable). Sorted by workflow then environment for a stable table.
 */
export function latestPlanCoverage(entries: DeploymentEntry[]): CoverageRow[] {
  const latest = new Map<string, DeploymentEntry>();
  for (const entry of entries) {
    const { workflow_id, environment_id } = entry.plan.identity;
    // NUL delimiter: ids cannot contain it, so groups never collide.
    const key = `${workflow_id}\u0000${environment_id}`;
    const current = latest.get(key);
    if (!current || entry.plan.generated_at > current.plan.generated_at) {
      latest.set(key, entry);
    }
  }

  return [...latest.values()]
    .map((entry) => ({
      workflowId: entry.plan.identity.workflow_id,
      environmentId: entry.plan.identity.environment_id,
      planId: entry.plan_id,
      workflowType: entry.plan.identity.workflow_type,
      generatedAt: entry.plan.generated_at,
      state: (entry.verification.ok ? "ready" : "drifted") as CoverageRow["state"],
      mismatches: entry.verification.mismatches ?? [],
    }))
    .sort(
      (a, b) =>
        a.workflowId.localeCompare(b.workflowId) ||
        a.environmentId.localeCompare(b.environmentId),
    );
}

/**
 * Project workflows that have no approved plan in any environment — the
 * coverage gaps. Sorted; input order of `projectWorkflowIds` is not assumed.
 */
export function workflowsWithoutPlan(
  entries: DeploymentEntry[],
  projectWorkflowIds: string[],
): string[] {
  const covered = new Set(entries.map((entry) => entry.plan.identity.workflow_id));
  return projectWorkflowIds.filter((id) => !covered.has(id)).sort((a, b) => a.localeCompare(b));
}
