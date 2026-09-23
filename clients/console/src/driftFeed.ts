/**
 * Drift feed engine (#583): pure derivation of the project-level Drift page
 * rows from payloads the read tier already serves. Same contract as
 * `insights.ts` — severity-ranked `Insight` rows, every row deep-linked to
 * the surface that owns the detail and carrying its remediation; the page
 * renders the output. Five drift classes: plan-vs-resolution, cross-environment,
 * prompt-registry, and the two Temporal-tier classes (version drain and
 * runtime-pin skew, #577 §1). The Temporal-tier derivations take settled query
 * cells and surface unavailable reads as an EXPLICIT outage set the page renders
 * loudly — never folded into an all-clear.
 */

import type { DeploymentEntry, WorkflowPromptStatus } from "./api";
import { latestPlanCoverage, workflowsWithoutPlan } from "./deploymentCoverage";
import { diffBundles, type DiffEntry } from "./diff";
import { middleTruncate } from "./format";
import type { Insight, Severity } from "./insights";
import { drainStale, sortInsights } from "./insights";
import { definitionSourceLinks, type RepoProvenance } from "./links";
import { derivePinSkew } from "./runsFeed";
import type { BundleCell, RuntimePinCell, VersionsCell } from "./queries";

function short(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return middleTruncate(typeof value === "string" ? value : JSON.stringify(value));
}

/**
 * Plan verification emits exactly four mismatch paths (`identity.spec_digest`,
 * `identity.workflow_type`, `policy.policy_hash`, `preflight.ok` — see
 * `verify_deployment_plan`). Identity/governance drift and a failing
 * structural preflight are all critical: the project either runs different
 * code/policy than approved or no longer validates at all. Unknown future
 * paths degrade to warning rather than being dropped.
 */
export function planMismatchSeverity(path: string): Severity {
  return path.startsWith("identity.") || path.startsWith("policy.") || path.startsWith("preflight.")
    ? "critical"
    : "warning";
}

/**
 * Plan drift: approved deployment plans whose live verification no longer
 * matches the current resolution (promotion fails closed until re-approved),
 * plus workflows with no approved plan at all.
 */
export function derivePlanDrift(
  entries: DeploymentEntry[],
  workflowIds: string[],
  /** Project git provenance (#718 §A): when present, drift rows link the plan FILE at the
   * resolved sha (blob + history), not just the Deployments section. */
  project?: RepoProvenance,
): Insight[] {
  const insights: Insight[] = [];
  const pathByPlanId = new Map(entries.map((entry) => [entry.plan_id, entry.path]));

  for (const row of latestPlanCoverage(entries)) {
    if (row.state !== "drifted") continue;
    const critical = row.mismatches.some((m) => planMismatchSeverity(m.path) === "critical");
    const fields = row.mismatches
      .slice(0, 3)
      .map((m) => `${m.path}: ${short(m.plan_value)} → ${short(m.current_value)}`)
      .join(" · ");
    const planPath = pathByPlanId.get(row.planId);
    // When we know the plan FILE, always attach its source links — even when the project has no
    // git provenance (the pair is then `{}`), so the row degrades LOUDLY to the "no repo
    // provenance" note rather than silently dropping the affordance (#718 loud-degradation rule).
    const sourceLinks = planPath !== undefined ? definitionSourceLinks(project, planPath) : undefined;
    insights.push({
      id: `plan:${row.planId}`,
      severity: critical ? "critical" : "warning",
      title: `${row.workflowId} (${row.environmentId}): approved plan no longer matches the resolution`,
      detail:
        `${fields}${row.mismatches.length > 3 ? ` · +${row.mismatches.length - 3} more` : ""}. ` +
        "Promotion fails closed until a fresh plan is generated and re-approved via PR.",
      // The Deployments page is cross-environment; no env scoping to carry.
      link: "#/deployments",
      ...(sourceLinks ? { sourceLinks } : {}),
    });
  }

  for (const workflowId of workflowsWithoutPlan(entries, workflowIds)) {
    insights.push({
      id: `plan:none:${workflowId}`,
      severity: "warning",
      title: `${workflowId}: no approved deployment plan`,
      detail:
        "Nothing pins this workflow's identity, policy, and image for promotion. " +
        "Generate a plan on the Deployments page and approve it by merging its PR.",
      link: "#/deployments",
    });
  }

  return sortInsights(insights);
}

/**
 * Environment drift: critical bundle differences (spec digest, policy hash,
 * secret-configured state — version and governance drift) between the
 * selected environment and another. Warning/info divergence stays on the
 * pairwise diff page these rows link to.
 */
export function deriveEnvironmentDrift(
  workflowId: string,
  baseEnv: string,
  otherEnv: string,
  entries: DiffEntry[],
): Insight[] {
  const critical = entries.filter((entry) => entry.severity === "critical");
  if (critical.length === 0) return [];
  const paths = critical
    .slice(0, 3)
    .map((entry) => entry.path)
    .join(" · ");
  return [
    {
      id: `env:${workflowId}:${baseEnv}:${otherEnv}`,
      severity: "critical",
      title: `${workflowId}: version/governance drift between ${baseEnv} and ${otherEnv}`,
      detail:
        `${paths}${critical.length > 3 ? ` · +${critical.length - 3} more` : ""}. ` +
        "The two environments resolve different workflow code or effective policy — " +
        "align the YAML (or ship the pending version) before treating them as equivalent.",
      link: `#/workflows/${workflowId}/diff?left=${baseEnv}&right=${otherEnv}`,
    },
  ];
}

export interface EnvironmentDriftFeed {
  rows: Insight[];
  /** Deduped "workflow does not resolve in env" notes (outage honesty), rendered as a hint. */
  notes: string[];
}

/**
 * Every workflow's critical bundle drift between `baseEnv` and each of `otherEnvs`, over the
 * resolved bundle matrix — the exact derivation the Drift page's "Environment drift" section
 * renders, extracted (#721 F5) so the executive persona rollup can COUNT the same criticals
 * without forking the loop. Callers gate on the matrix being settled; a (workflow, environment)
 * pair that does not resolve is a deduped note, never an insight. Pure: keyed by the caller on
 * the same cell fingerprint the section already memoizes on.
 */
export function deriveEnvironmentDriftFeed(
  baseEnv: string,
  otherEnvs: string[],
  workflowIds: string[],
  cells: BundleCell[],
): EnvironmentDriftFeed {
  const cellFor = (workflowId: string, cellEnv: string) =>
    cells.find((cell) => cell.workflowId === workflowId && cell.env === cellEnv);
  const rows: Insight[] = [];
  const notes: string[] = [];
  for (const workflowId of workflowIds) {
    const base = cellFor(workflowId, baseEnv);
    if (!base?.bundle) {
      notes.push(`${workflowId} does not resolve in ${baseEnv}`);
      continue;
    }
    for (const otherEnv of otherEnvs) {
      const other = cellFor(workflowId, otherEnv);
      if (!other?.bundle) {
        notes.push(`${workflowId} does not resolve in ${otherEnv}`);
        continue;
      }
      rows.push(
        ...deriveEnvironmentDrift(
          workflowId,
          baseEnv,
          otherEnv,
          diffBundles(base.bundle, other.bundle),
        ),
      );
    }
  }
  return { rows: sortInsights(rows), notes: [...new Set(notes)] };
}

/**
 * Prompt drift: registry versions that moved past what the last run used.
 * One row per drifting prompt, linking to the workflow's prompt-registry
 * panel (which carries the per-label detail and the Langfuse links).
 */
export function derivePromptDrift(
  workflowId: string,
  env: string,
  status: WorkflowPromptStatus,
): Insight[] {
  const insights: Insight[] = [];
  for (const prompt of status.prompts ?? []) {
    if (prompt.status !== "drift") continue;
    insights.push({
      id: `prompt:${workflowId}:${prompt.name}`,
      severity: "warning",
      title: `${workflowId}: prompt ${prompt.name} drifted (registry ${prompt.registry_version ?? "?"} vs last run ${prompt.last_run_version ?? "—"})`,
      detail:
        "The registry moved past what the last execution used — the next run behaves " +
        "differently. Review the registry change, then run (or pin) deliberately.",
      link: `#/workflows/${workflowId}?env=${env}&section=prompt-registry`,
    });
  }
  return sortInsights(insights);
}

/** A Temporal-tier drift class: its ranked rows plus the workflows whose backing read was
 * unavailable (each entry a display label — the workflow id, optionally annotated with WHICH stage
 * failed) — the page renders `unavailable` as an EXPLICIT outage state ("… unknown"), never an
 * empty all-clear (#577 §1 loud-degradation rule). */
export interface TemporalDriftFeed {
  rows: Insight[];
  unavailable: string[];
}

/**
 * The outage verdict for a Temporal-tier drift class over `total` workflows (#577 §1) — the ONE
 * place the page's rendering decides between the three honest empty-feed states, pure and
 * unit-tested beside the derivations:
 * - `none`   — every read answered; an empty feed is a genuine all-clear.
 * - `partial`— some workflows unreadable: an empty feed is INCOMPLETE (neutral "drift unknown for
 *              N workflows" naming them), never the green all-clear.
 * - `total`  — nothing was readable: the class is UNKNOWN outright.
 */
export type TemporalDriftOutage =
  | { kind: "none" }
  | { kind: "partial"; unavailable: string[] }
  | { kind: "total" };

export function temporalDriftOutage(feed: TemporalDriftFeed, total: number): TemporalDriftOutage {
  if (feed.unavailable.length === 0) return { kind: "none" };
  if (total > 0 && feed.unavailable.length >= total) return { kind: "total" };
  return { kind: "partial", unavailable: feed.unavailable };
}

/**
 * Version drain (#577 §1): executions still running on old versioned workflow types, per workflow,
 * over the settled cross-version drain matrix. REUSES {@link drainStale} — the same stale-type
 * detection the per-workflow Versions-page insight ({@link deriveDrainInsights}) reads — so the
 * project view and the workflow view can never disagree on what counts as un-drained. Each row
 * names the workflow, the old versioned types still carrying runs, and the remediation ("drain
 * before decommission"), linking to the workflow's Versions section.
 *
 * Outage honesty: a cell whose Temporal-tier read errored has NO drain data and joins `unavailable`
 * — the class is unknown for it, never a silent "drained". Callers gate on the matrix being settled.
 */
export function deriveDrainDrift(env: string, cells: VersionsCell[]): TemporalDriftFeed {
  const rows: Insight[] = [];
  const unavailable: string[] = [];
  for (const cell of cells) {
    if (cell.error !== undefined) {
      unavailable.push(cell.workflowId);
      continue;
    }
    if (cell.drain === undefined) continue; // not yet settled — the caller gates on this
    const { staleTypes, staleRuns } = drainStale(cell.drain);
    if (staleTypes.length === 0) {
      // Fail-closed verdict with nothing enumerated: the backend can report `drained: false`
      // while `running` carries no non-current type (e.g. the TS drain scan fails closed after a
      // truncated visibility read). Dropping the workflow here would render an all-clear that
      // CONTRADICTS the backend's unsafe verdict — surface it as its own warning row instead.
      if (!cell.drain.drained) {
        rows.push({
          id: `drain:${cell.workflowId}:unsafe`,
          severity: "warning",
          title: `${cell.workflowId}: drain verdict is unsafe — scan incomplete`,
          detail:
            "The backend reports not-drained, but no old-version runs were enumerated — the " +
            "drain scan failed closed (e.g. truncated visibility results). Do not decommission " +
            "old versions until a complete drain read confirms they are drained.",
          link: `#/workflows/${cell.workflowId}/versions?env=${env}`,
        });
      }
      continue;
    }
    rows.push({
      id: `drain:${cell.workflowId}`,
      severity: "warning",
      title: `${cell.workflowId}: ${staleRuns} execution${staleRuns === 1 ? "" : "s"} still on ${staleTypes.length} old version${staleTypes.length === 1 ? "" : "s"}`,
      detail:
        `Old versioned type${staleTypes.length === 1 ? "" : "s"} still carrying runs: ` +
        `${staleTypes.map((workflowType) => middleTruncate(workflowType)).join(" · ")}. ` +
        "Drain before decommissioning — an old version is not safe to remove while executions run on it.",
      link: `#/workflows/${cell.workflowId}/versions?env=${env}`,
    });
  }
  return { rows: sortInsights(rows), unavailable };
}

/** The resolution-tier side of the pin-skew comparison for one workflow: the current bundle's
 * spec digest, or the error that kept the bundle read from answering. */
export interface PinSkewResolution {
  specDigest?: string;
  error?: string;
}

/**
 * Runtime-pin skew (#577 §1): per-workflow, the provable graph-changed case — mutating operations
 * are bound to a runtime pin whose spec digest differs from the current resolution. REUSES
 * {@link derivePinSkew} (the run inspector's #592 derivation) verbatim, so the semantics — and the
 * documented caveat that a MATCHING digest is not a freshness verdict (config/policy edits don't
 * move it) — are identical; the row links to the workflow's run inspector where the pin and repin
 * action live. `resolutionByWorkflow` carries the current bundle spec digest per workflow, OR the
 * bundle read's error.
 *
 * Outage honesty, BOTH sides of the comparison: a workflow whose Temporal-tier pin read was
 * unavailable joins `unavailable`; so does a workflow with a pinned digest whose RESOLUTION read
 * errored (no current digest to compare against — its skew is unknown, not absent). A workflow
 * with no pin at all yields nothing regardless (there is no pinned runtime to be skewed). Never a
 * silent "no skew".
 */
export function derivePinSkewDrift(
  env: string,
  pins: RuntimePinCell[],
  resolutionByWorkflow: Map<string, PinSkewResolution>,
): TemporalDriftFeed {
  const rows: Insight[] = [];
  const unavailable: string[] = [];
  for (const cell of pins) {
    if (cell.unavailable) {
      unavailable.push(cell.workflowId);
      continue;
    }
    const resolution = resolutionByWorkflow.get(cell.workflowId);
    if (cell.pin?.spec_digest && resolution?.error !== undefined) {
      unavailable.push(`${cell.workflowId} (resolution read failed — skew unknown)`);
      continue;
    }
    const skew = derivePinSkew(cell.workflowId, env, cell.pin, resolution?.specDigest);
    if (skew) rows.push(skew);
  }
  return { rows: sortInsights(rows), unavailable };
}
