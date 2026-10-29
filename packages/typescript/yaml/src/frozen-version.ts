/**
 * Frozen `workflow.version` enforcement (#530; Python `_enforce_frozen_version_label`
 * parity, adapted): a version label is a frozen pointer to ONE graph. Every start
 * stamps the derived plan's digest into the execution memo; starting under a label
 * whose most recent recorded digest differs from the loaded graph fails loud.
 *
 * The TS adaptation: Python registers a VERSIONED workflow type per spec and
 * enforces at worker start with a type-scoped visibility query. TS runs one
 * generic `typefluxYamlWorkflow` type whose plan rides as the workflow ARGUMENT —
 * in-flight executions replay their recorded plan and are structurally immune to
 * graph edits — so the frozen guarantee is purely about NEW starts, and start
 * time is the natural enforcement point. The label lives in the memo
 * (`typeflux_workflow_version`) instead of the type name, and the query scans a
 * bounded page of recent generic-type executions for the matching identity.
 *
 * One caveat the immunity claim does NOT cover: replay determinism, not activity
 * AVAILABILITY. Deploying a graph edit that removes/renames an activity to
 * workers serving in-flight executions still fails those executions with
 * "activity not registered" once retries exhaust — Python's per-spec type would
 * have left them stalled workerless instead. That gap is architectural
 * (plan-as-argument predates this check) and start-time enforcement cannot close
 * it; keep removed activities registered until their executions drain.
 */

import { createHash } from "node:crypto";

import { canonicalJson } from "@typeflux/temporal";

import type { TypefluxYamlSpec } from "./spec.js";
import { PLAN_INTERPRETER_VERSION, type WorkflowPlan } from "./workflow-plan.js";

/**
 * The spec digest: sha256 over the canonical JSON of the derived plan, wrapped in
 * an algorithm + interpreter-version envelope (Python's payload embeds
 * `algorithm`/`generator_version` the same way — its history #60/#363/#368 shows
 * why: identical YAML through a different interpreter is a different
 * deterministic program, and a frozen label must not silently span two). The
 * plan IS the TS replay identity (plan-as-argument), so digesting it pins
 * exactly what a frozen version must freeze and nothing the runtime doesn't honor.
 */
export function workflowPlanDigest(plan: WorkflowPlan): string {
  const payload = {
    algorithm: "typeflux-yaml-plan-v1",
    interpreter_version: PLAN_INTERPRETER_VERSION,
    plan,
  };
  return createHash("sha256").update(canonicalJson(payload), "utf-8").digest("hex");
}

/**
 * The identity memo stamped on every start (Python `_workflow_identity_memo`):
 * digest, logical workflow name, and project — plus, when the spec carries a
 * `workflow.version`, the label itself (TS-specific: Python encodes the label in
 * the versioned type name, which TS's generic type cannot).
 *
 * KEY-SET CONTRACT (drift guard): `childIdentityMemo` (workflow-plan.ts) stamps child
 * starts with EXACTLY these keys plus `typeflux_parent_workflow_id` — the sandbox cannot
 * call this function, so the twin re-declares the literals. A key rename here must land
 * in both; a unit test compares the two key sets.
 */
export function workflowIdentityMemo(spec: TypefluxYamlSpec, planDigest: string): Record<string, string> {
  return {
    typeflux_spec_digest: planDigest,
    typeflux_workflow: spec.workflow.name,
    typeflux_project: spec.project,
    ...(spec.workflow.version !== undefined ? { typeflux_workflow_version: spec.workflow.version } : {}),
  };
}

/** The visibility slice of a client this module needs (`@temporalio/client` shape). */
export interface WorkflowListClient {
  workflow: {
    list(options: { query: string }): AsyncIterable<{ memo?: Record<string, unknown> | undefined }>;
  };
}

/** How many recent executions the best-effort scan reads before giving up. */
const FROZEN_VERSION_SCAN_LIMIT = 1000;

export interface EnforceFrozenVersionParams {
  /** The generic workflow type the runtime registers (`typefluxYamlWorkflow`). */
  workflowType: string;
  workflowName: string;
  /** Scopes labels per-project: two projects may reuse a name + label freely. */
  project: string;
  versionLabel: string;
  planDigest: string;
  /**
   * The configured `workflow_search_attribute` (name + this workflow's logical
   * name), added as a query conjunct so the scan reads only THIS workflow's
   * starts instead of every YAML spec's — without it, a busy namespace can push
   * the label's last start past the scan bound (best-effort degrades sooner).
   */
  searchAttribute?: { name: string; value: string };
  /** Degradation notices (visibility unsupported / query failed). Default: console.warn. */
  warn?: (message: string) => void;
}

/**
 * Best-effort frozen-label check (Python `_enforce_frozen_version_label`): find
 * the most recent execution whose identity memo matches (workflow name, version
 * label) and compare digests. Skips with a warning when the client has no
 * visibility support, the query fails, or no prior execution matches within the
 * scan bound — exactly Python's best-effort stance. Throws only on a REAL
 * mismatch: a changed graph reusing a frozen label.
 */
export async function enforceFrozenWorkflowVersion(
  client: WorkflowListClient,
  params: EnforceFrozenVersionParams,
): Promise<void> {
  const warn = params.warn ?? ((message: string) => console.warn(message));
  const list = client.workflow?.list;
  if (typeof list !== "function") {
    warn(
      `skipping frozen workflow.version check for ${params.workflowName}@${params.versionLabel}: ` +
        "the client has no visibility support (workflow.list)",
    );
    return;
  }
  let recorded: string | undefined;
  try {
    let scanned = 0;
    // The attribute NAME is spec-validated to [A-Za-z][A-Za-z0-9_]* and the VALUE
    // is quote-escaped, so the interpolation is injection-safe.
    const attributeClause =
      params.searchAttribute !== undefined
        ? ` AND ${params.searchAttribute.name} = '${params.searchAttribute.value.replace(/'/g, "''")}'`
        : "";
    // Most-recent-first (Temporal visibility default ordering is StartTime DESC),
    // so the first identity match is the digest the label is frozen to.
    for await (const execution of list.call(client.workflow, {
      query: `WorkflowType = '${params.workflowType}'${attributeClause}`,
    })) {
      scanned += 1;
      const memo = execution.memo ?? {};
      if (
        memo["typeflux_workflow"] === params.workflowName &&
        memo["typeflux_project"] === params.project &&
        memo["typeflux_workflow_version"] === params.versionLabel
      ) {
        const digest = memo["typeflux_spec_digest"];
        recorded = typeof digest === "string" ? digest : undefined;
        break;
      }
      if (scanned >= FROZEN_VERSION_SCAN_LIMIT) {
        // Bounded best-effort: an older match beyond the page is not scanned —
        // say so instead of silently treating the label as fresh.
        warn(
          `frozen workflow.version check for ${params.workflowName}@${params.versionLabel} scanned ` +
            `${FROZEN_VERSION_SCAN_LIMIT} executions without an identity match; treating the label as fresh`,
        );
        break;
      }
    }
  } catch (error) {
    warn(
      `skipping frozen workflow.version check for ${params.workflowName}@${params.versionLabel}: ` +
        `visibility query failed (${error instanceof Error ? error.name : typeof error})`,
    );
    return;
  }
  if (recorded !== undefined && recorded !== params.planDigest) {
    throw new Error(
      `workflow.version '${params.versionLabel}' is frozen to spec digest '${recorded}', but the ` +
        `loaded YAML graph has digest '${params.planDigest}'; assign a new workflow.version ` +
        "for graph changes instead of reusing a version label",
    );
  }
}
