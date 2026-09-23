/**
 * Run-to-trace correlation for the TS control plane (#686; Python `project/runs.py`
 * `workflow_run_correlation`, #251). Observer-driven for the TRACE half: the resolved spec's OWN
 * configured observability backend is what gets queried — `none` (or any non-langfuse backend) is
 * reported as exactly that, reachable with no trace, and a langfuse backend goes through the
 * INJECTED reader transport (#573 seam; the CP core holds no vendor client).
 *
 * The CHILDREN half (#55 §9): sub-workflow executions stamp `typeflux_parent_workflow_id` into
 * their memo (decision D2), so the correlation card lists an execution's DIRECT children from a
 * bounded, memo-filtered visibility scan of the generic workflow type. Best-effort like the trace
 * half: an unreachable Temporal tier omits `children` and appends a warning — never a 500/503.
 *
 * DTO parity: Python's `WorkflowRunCorrelation` under the route's `exclude_none` — `trace`,
 * `warning`, and `children` OMIT when absent, so the observer-none shape is exactly
 * `{execution_id, observer, reachable, children}` (children `[]` when reachable and none) on both
 * editions, and null-valued child fields omit inside entries.
 */

import { specReferencesSubworkflows, type TypefluxYamlSpec } from "@typeflux/temporal-yaml";

import {
  defaultVisibilityClient,
  EXECUTIONS_SCAN_LIMIT,
  temporalConnectionOptions,
  type VisibilityClientFactory,
} from "./executions.js";
import { type LangfuseControlPlaneTransport, sanitizeErrorMessage } from "./langfuse-transport.js";

/** One DIRECT child execution of the correlated run (#55 §9; Python twin, exclude_none). */
export interface ApiWorkflowRunCorrelationChild {
  workflow_id: string;
  /** The child's OWN logical name from its identity memo (`typeflux_workflow`); omits when absent. */
  workflow_name?: string;
  status?: string;
  start_time?: string;
}

/** How many children the listing returns at most (the scan itself stays EXECUTIONS_SCAN_LIMIT-bounded). */
export const CORRELATION_CHILDREN_LIMIT = 100;

/** Where an execution was migrated FROM (#204; Python `WorkflowMigrationProvenance`), from its memo. */
export interface ApiWorkflowMigrationProvenance {
  run_id: string;
  version_key?: string;
}

/** Python `WorkflowRunCorrelation` (route exclude_none: absent `trace`/`warning`/`children` omit). */
export interface ApiWorkflowRunCorrelation {
  execution_id: string;
  observer: string;
  reachable: boolean;
  /** The observer's trace summary when one matched the execution id (Python `TraceSummaryView.to_public_dict()`). */
  trace?: Record<string, unknown>;
  warning?: string;
  /**
   * DIRECT children (#55 §9): present (possibly empty) when the listing succeeded; OMITTED when
   * the Temporal tier was unreachable under a composed spec (see `warning`). `[]` without any
   * children SCAN when the resolved spec references no sub-workflows — correlation still makes
   * one bounded read for `migrated_from`.
   */
  children?: ApiWorkflowRunCorrelationChild[];
  /**
   * Migration provenance (#204): set when this run was started by a `migrate` operation (memo
   * `typeflux_migrated_from`). DECOUPLED from the children scan (#204 review): a non-composed
   * spec makes ONE bounded visibility read of the parent execution for it (the documented
   * contract — children stays `[]` with no scan); a composed spec reads it from the same scan.
   * Omitted when the run was not migrated or the read degraded (see `warning`); the
   * authoritative provenance lives in the memo + migrate result.
   */
  migrated_from?: ApiWorkflowMigrationProvenance;
}

export interface WorkflowRunCorrelationOptions {
  executionId: string;
  /** The injected langfuse reader transport (#573); absent ⇒ a langfuse observer degrades honestly. */
  langfuse?: LangfuseControlPlaneTransport | undefined;
  /** Injectable Temporal seam for the children listing; defaults to a real `@temporalio/client` connection. */
  clientFactory?: VisibilityClientFactory | undefined;
}

/** The generic workflow type this profile registers (`@typeflux/temporal-yaml` YAML_WORKFLOW_TYPE). */
const YAML_WORKFLOW_TYPE = "typefluxYamlWorkflow";

/** Parse the migrate provenance memo pair (#204; Python `_read_migration_provenance`). */
function readMigrationProvenance(
  memo: Record<string, unknown>,
): ApiWorkflowMigrationProvenance | undefined {
  const from = memo["typeflux_migrated_from"];
  if (typeof from !== "string" || from.length === 0) return undefined;
  const version = memo["typeflux_migrated_from_version"];
  return {
    run_id: from,
    ...(typeof version === "string" && version.length > 0 ? { version_key: version } : {}),
  };
}

/**
 * The NON-composed provenance read (#204 review): one bounded visibility read of the parent
 * execution by workflow id — the injected seam exposes `list`, so the "describe" is a
 * first-row-only query (newest run first, the visibility default). Throws on an unreachable
 * tier — the caller degrades (provenance omitted + warning).
 */
async function fetchMigrationProvenance(
  spec: TypefluxYamlSpec,
  executionId: string,
  factory: VisibilityClientFactory,
): Promise<ApiWorkflowMigrationProvenance | undefined> {
  const client = await factory(temporalConnectionOptions(spec));
  try {
    const escaped = executionId.replace(/'/g, "''");
    for await (const execution of client.list(`WorkflowId = '${escaped}'`)) {
      return readMigrationProvenance(execution.memo ?? {});
    }
    return undefined;
  } finally {
    await client.close().catch(() => undefined);
  }
}

/**
 * List the execution's DIRECT children via the parent-link memo (#55 §9): a bounded newest-first
 * scan of the generic type, filtered client-side on `typeflux_parent_workflow_id` (memo fields
 * cannot appear in visibility queries). Throws on an unreachable tier — the caller degrades.
 */
async function listDirectChildren(
  spec: TypefluxYamlSpec,
  executionId: string,
  factory: VisibilityClientFactory,
): Promise<ApiWorkflowRunCorrelationChild[]> {
  const client = await factory(temporalConnectionOptions(spec));
  try {
    const children: ApiWorkflowRunCorrelationChild[] = [];
    let scanned = 0;
    for await (const execution of client.list(`WorkflowType = '${YAML_WORKFLOW_TYPE}'`)) {
      scanned += 1;
      const memo = execution.memo ?? {};
      if (memo["typeflux_parent_workflow_id"] === executionId) {
        const name = memo["typeflux_workflow"];
        children.push({
          workflow_id: execution.workflowId,
          ...(typeof name === "string" ? { workflow_name: name } : {}),
          ...(execution.status?.name !== undefined ? { status: execution.status.name } : {}),
          ...(execution.startTime !== undefined ? { start_time: execution.startTime.toISOString() } : {}),
        });
        if (children.length >= CORRELATION_CHILDREN_LIMIT) {
          break;
        }
      }
      if (scanned >= EXECUTIONS_SCAN_LIMIT) {
        break; // Bounded best-effort, like the drain/executions scans.
      }
    }
    return children;
  } finally {
    await client.close().catch(() => undefined);
  }
}

/** Correlate one execution with its observer's trace + its direct children (Python `workflow_run_correlation`). */
export async function workflowRunCorrelation(
  spec: TypefluxYamlSpec,
  options: WorkflowRunCorrelationOptions,
): Promise<ApiWorkflowRunCorrelation> {
  // The CHILDREN half first (observer-independent). No sub-workflow references in the resolved
  // spec ⇒ `[]` without a children SCAN (a V1 workflow cannot have YAML-started children) — but
  // migrate provenance is DECOUPLED (#204 review): the non-composed case still makes ONE bounded
  // read of the parent execution for its `typeflux_migrated_from` memo.
  let children: ApiWorkflowRunCorrelationChild[] | undefined;
  let migratedFrom: ApiWorkflowMigrationProvenance | undefined;
  let childrenWarning: string | undefined;
  const factory = options.clientFactory ?? defaultVisibilityClient;
  if (!specReferencesSubworkflows(spec.workflow.steps)) {
    children = [];
    try {
      migratedFrom = await fetchMigrationProvenance(spec, options.executionId, factory);
    } catch (error) {
      // Degrade: provenance omitted + warning, never an error (children never needed the tier).
      childrenWarning = `temporal unreachable for the migration provenance: ${sanitizeErrorMessage(error)}`;
    }
  } else {
    // Provenance stays a DEDICATED first-row read even when composed (Bugbot): under a busy
    // namespace the parent may never appear inside the children scan's bounded window, and
    // Python's composed path describes the parent first for exactly this reason.
    try {
      let provenanceWarning: string | undefined;
      try {
        migratedFrom = await fetchMigrationProvenance(spec, options.executionId, factory);
      } catch (error) {
        // Inner degrade, matching Python: a failed provenance read warns but the scan proceeds.
        provenanceWarning = `temporal unreachable for the migration provenance: ${sanitizeErrorMessage(error)}`;
      }
      children = await listDirectChildren(spec, options.executionId, factory);
      childrenWarning = provenanceWarning;
    } catch (error) {
      // Whole-scan failure: children unknown, provenance discarded with it — the single
      // children warning, exactly Python's one-connection degrade contract.
      children = undefined;
      migratedFrom = undefined;
      childrenWarning = `temporal unreachable for the children listing: ${sanitizeErrorMessage(error)}`;
    }
  }
  const withChildren = (base: ApiWorkflowRunCorrelation): ApiWorkflowRunCorrelation => ({
    ...base,
    ...(children !== undefined ? { children } : {}),
    ...(migratedFrom !== undefined ? { migrated_from: migratedFrom } : {}),
    ...(childrenWarning !== undefined
      ? { warning: base.warning !== undefined ? `${base.warning}; ${childrenWarning}` : childrenWarning }
      : {}),
  });

  // Python `getattr(observability, "type", "none") or "none"` — a falsy/empty type reads "none".
  const observer = spec.runtime.observability?.type || "none";
  if (observer !== "langfuse") {
    return withChildren({ execution_id: options.executionId, observer, reachable: true });
  }
  if (options.langfuse === undefined) {
    // No injected transport: the backend is configured but this server cannot reach it — the
    // honest analogue of Python's unreachable-backend degrade (its backend construction reads
    // credentials from the environment; this edition's credentials live in the injected adapter).
    return withChildren({
      execution_id: options.executionId,
      observer,
      reachable: false,
      warning: "observability backend unreachable: no langfuse transport configured",
    });
  }
  let trace: Record<string, unknown> | null;
  try {
    trace = await options.langfuse.traceSummary({ workflowId: options.executionId, host: null });
  } catch (error) {
    // Python's degrade path, with the TS sanitization rule: the warning carries the error's CLASS
    // name only (a raw message can embed a credentialed URL/key), behind Python's exact prefix.
    return withChildren({
      execution_id: options.executionId,
      observer,
      reachable: false,
      warning: `observability backend unreachable: ${sanitizeErrorMessage(error)}`,
    });
  }
  return withChildren({
    execution_id: options.executionId,
    observer,
    reachable: true,
    // `trace: null` (no match) omits, matching Python's exclude_none serialization.
    ...(trace !== null ? { trace } : {}),
  });
}
