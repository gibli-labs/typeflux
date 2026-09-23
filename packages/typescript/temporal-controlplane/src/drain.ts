/**
 * The cross-version drain view for the ts-plan-argument binding (#686; Python `project/drain.py`
 * `workflow_drain_status`). Python's drain identity lives in VERSIONED TYPE NAMES
 * (`{logical}.{version|digest12}` — every execution registers its own type, so counting running
 * executions per type IS counting per version). This profile registers ONE generic type
 * (`typefluxYamlWorkflow`) and freezes identity in the MEMO (contracts/temporal-binding:
 * `typeflux_workflow` / `typeflux_project` / `typeflux_spec_digest` / optional
 * `typeflux_workflow_version`), so the drain view is a bounded RUNNING-only visibility scan over
 * the generic type, memo-filtered to this workflow's identity and grouped by the memo's version
 * identity.
 *
 * DTO parity: the response is Python's exact `WorkflowDrainStatus` shape. The `running` map's
 * keys (and `current_workflow_type`) carry the SAME `{logical}.{label|digest12}` rendering
 * Python's `registered_workflow_type` produces, derived from the memo instead of the type name —
 * so the console's "key == current_workflow_type ⇒ current, else draining" logic works unchanged
 * across bindings, and both control-plane editions render identical keys for the same cluster
 * state (the Python CP's ts driver mirrors this derivation in `binding_ts.py`).
 *
 * Same-label hazard, shared with Python: a version LABEL keys the group, so two digests reusing
 * one label would collapse — exactly the reuse `enforceFrozenWorkflowVersion` (#662) refuses at
 * start, on both editions. An execution missing the digest memo renders `{logical}.unknown`
 * (fail-safe: it can never equal the current key, so it reads as draining, never as drained).
 */

import {
  type SubworkflowSpecResolver,
  type TypefluxYamlSpec,
  workflowPlanDigest,
  workflowPlanFromSpec,
  YAML_WORKFLOW_TYPE,
} from "@typeflux/temporal-yaml";

import {
  boundedTemporalTier,
  defaultVisibilityClient,
  EXECUTIONS_SCAN_LIMIT,
  temporalConnectionOptions,
  type VisibilityClientFactory,
} from "./executions.js";
import { ProjectControlPlaneError } from "./errors.js";

/** Python `WorkflowDrainStatus` — running-execution counts per version identity. */
export interface ApiWorkflowDrainStatus {
  logical_workflow: string;
  /** The currently-resolved version's identity key (`{logical}.{label|digest12}`). */
  current_workflow_type: string;
  /** The visibility query the counts came from (running executions of the generic type). */
  query: string;
  /** Running-execution count per version identity key, keys sorted ascending (Python `sorted`). */
  running: Record<string, number>;
  total_running: number;
  /** True when nothing but the current version is running — old versions are safe to decommission. */
  drained: boolean;
}

/** Python `registered_workflow_type`'s suffix truncation (`identity._TYPE_DIGEST_LENGTH`). */
const VERSION_DIGEST_LENGTH = 12;

/**
 * Render one version identity key from a memo's label/digest pair — Python
 * `registered_workflow_type` verbatim (`{logical}.{label}` when a label exists, else
 * `{logical}.{digest[:12]}`), so drain keys read identically across both bindings' drain views.
 */
/** The fail-safe suffix for a memo with neither label nor digest. Contains a
 * space, which no workflow.version label or hex digest can carry — a project
 * literally versioned "unknown" can never collide with it (finder). */
export const UNIDENTIFIED_VERSION_SUFFIX = "(unidentified memo)";

export function versionIdentityKey(
  logical: string,
  label: string | undefined,
  digest: string | undefined,
): string {
  // Truthy checks, not presence — Python's `label or …` / `elif digest:`
  // (an empty-string memo value must not render an empty suffix; finder).
  const suffix = label || (digest ? digest.slice(0, VERSION_DIGEST_LENGTH) : UNIDENTIFIED_VERSION_SUFFIX);
  return `${logical}.${suffix}`;
}

export interface WorkflowDrainStatusOptions {
  workflowId: string;
  environmentId: string;
  /** Injectable Temporal seam; defaults to a real `@temporalio/client` connection. */
  clientFactory?: VisibilityClientFactory;
  /** Resolves `workflow:` sub-workflow refs so the current-digest derivation embeds children (#55). */
  subworkflows?: SubworkflowSpecResolver;
}

/**
 * Compute the drain view for one resolved workflow (Python `workflow_drain_status`). The scan is
 * DOUBLY bounded like the executions listing: the whole call races the Temporal-tier timeout
 * (#581 → 503 `TemporalUnavailable`), and the memo-filtered iteration stops after
 * {@link EXECUTIONS_SCAN_LIMIT} rows (bounded best-effort — the same posture as the listing; a
 * truncated scan can at worst UNDER-count, and `drained` stays fail-safe because the query is
 * running-only and newest-first, so old versions' long-running executions surface first).
 */
export async function workflowDrainStatus(
  spec: TypefluxYamlSpec,
  options: WorkflowDrainStatusOptions,
): Promise<ApiWorkflowDrainStatus> {
  const logical = spec.workflow.name;
  // A bad graph after a clean resolution is a CONFIG failure — 422 like the executions listing.
  let currentDigest: string;
  try {
    currentDigest = workflowPlanDigest(
      workflowPlanFromSpec(spec, options.subworkflows !== undefined ? { subworkflows: options.subworkflows } : {}),
    );
  } catch (error) {
    throw new ProjectControlPlaneError(
      `failed to derive the workflow plan: ${error instanceof Error ? error.message : String(error)}`,
      422,
    );
  }
  const currentKey = versionIdentityKey(logical, spec.workflow.version, currentDigest);
  // Python `_drain_query` minus the type-prefix trick: this profile's types are constant, so the
  // generic-type query IS the drain query, narrowed to running executions server-side. UNLIKE the
  // executions listing, the drain view must NOT narrow by the optional search attribute: runs
  // started BEFORE the attribute was configured would be invisible and an undrained old version
  // could read as drained (codex) — this is a SAFETY view, so scan the generic type and filter
  // memos client-side only.
  const query = `WorkflowType = '${YAML_WORKFLOW_TYPE}' AND ExecutionStatus = 'Running'`;
  const factory = options.clientFactory ?? defaultVisibilityClient;

  const run = async (): Promise<ApiWorkflowDrainStatus> => {
    const client = await factory(temporalConnectionOptions(spec));
    try {
      const running = new Map<string, number>();
      let scanned = 0;
      let truncated = false;
      for await (const execution of client.list(query)) {
        scanned += 1;
        const memo = execution.memo ?? {};
        if (memo["typeflux_workflow"] === logical && memo["typeflux_project"] === spec.project) {
          const label = memo["typeflux_workflow_version"];
          const digest = memo["typeflux_spec_digest"];
          const key = versionIdentityKey(
            logical,
            typeof label === "string" ? label : undefined,
            typeof digest === "string" ? digest : undefined,
          );
          running.set(key, (running.get(key) ?? 0) + 1);
        }
        if (scanned >= EXECUTIONS_SCAN_LIMIT) {
          truncated = true;
          break;
        }
      }
      const sortedKeys = [...running.keys()].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
      const runningRecord: Record<string, number> = {};
      let total = 0;
      for (const key of sortedKeys) {
        const count = running.get(key)!;
        runningRecord[key] = count;
        total += count;
      }
      return {
        logical_workflow: logical,
        current_workflow_type: currentKey,
        query,
        running: runningRecord,
        total_running: total,
        // Python: `all(type == current for type in running)` — vacuously true with none
        // running. A TRUNCATED scan fails CLOSED: unseen rows could hide an old
        // version's long-running execution, so a partial view must never claim
        // decommissioning is safe (codex).
        drained: !truncated && sortedKeys.every((key) => key === currentKey),
      };
    } finally {
      await client.close().catch(() => undefined);
    }
  };

  return boundedTemporalTier(run, "computing the drain view");
}
