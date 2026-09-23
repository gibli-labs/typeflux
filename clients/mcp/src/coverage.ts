/**
 * The coverage ledger (#326; design §10 "every CP operation wrapped-or-consciously-excluded").
 * Every control-plane operation in the contract is listed here EXACTLY once as either a wrapped
 * surface (a tool and/or a resource template) or a conscious exclusion with a reason and the phase
 * that will pick it up. Phase 0 wrapped the read tier; Phase 1 wraps the operate tier's six §6.3
 * verbs (start/status[status is a resource]/review/cancel/repin/refresh) — only `migrate` stays
 * excluded (outside the design's operate surface). The conformance test (test/coverage.test.ts) parses
 * contracts/controlplane/openapi.v1.json and asserts this ledger and the contract agree — so the
 * MCP surface can never silently fall behind the contract, and no operate-tier write can sneak in.
 *
 * Paths are the UNPREFIXED (default-project) forms. The contract dual-mounts every read under
 * `/api/v1/projects/{project}/...` too; the test collapses that prefix so each logical operation
 * is checked once. Project scoping in the MCP surface is a client concern (see client.ts), not a
 * separate operation.
 */

/** A control-plane operation keyed by method + unprefixed path. */
export interface OperationKey {
  method: "GET" | "POST";
  /** The unprefixed contract path, e.g. `/api/v1/workflows/{workflow_id}/bundle`. */
  path: string;
}

/** A Phase-0 read operation and the surfaces that wrap it. */
export interface WrappedOperation extends OperationKey {
  kind: "wrapped";
  /** The read tool name that wraps it, if any (design §6.1/6.2). */
  tool?: string;
  /** The resource-template URI that wraps it, if any. */
  resource?: string;
  note?: string;
}

/** An operation consciously left out of Phase 0. */
export interface ExcludedOperation extends OperationKey {
  kind: "excluded";
  /** The phase that will pick it up (1 = operate tier, 2 = recipes/trace, 3 = authoring/HTTP). */
  phase: 1 | 2 | 3;
  reason: string;
}

export type LedgerEntry = WrappedOperation | ExcludedOperation;

/**
 * PROJECT is the resource-template variable for the project id (default project when the caller
 * omits it — the client leaves the path unscoped). WF/ENV/... mirror the contract path params.
 */
export const COVERAGE_LEDGER: readonly LedgerEntry[] = [
  // --- WRAPPED: pure-YAML / registry reads (up even on an unresolvable runtime) ---
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/meta",
    resource: "typeflux://{project}/meta",
    note: "capabilities gate; drives which resolution-dependent tools are usable",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows",
    tool: "list_workflows",
    resource: "typeflux://{project}/workflows",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/environments",
    tool: "list_environments",
    resource: "typeflux://{project}/environments",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/environments/{environment_id}",
    resource: "typeflux://{project}/environments/{environment_id}",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/policies",
    tool: "list_policies",
    resource: "typeflux://{project}/policies",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/policies/{policy_id}",
    resource: "typeflux://{project}/policies/{policy_id}",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/profiles",
    resource: "typeflux://{project}/profiles",
  },
  {
    // A pure-YAML, project-level read (the in-repo insight-ack ledger, #733) — static and
    // cacheable like workflows/policies, so it is BOTH a tool and a resource. Always servable
    // (never resolution-bound), so it sits with the other up-even-on-an-unresolvable-runtime reads.
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/annotations",
    tool: "list_annotations",
    resource: "typeflux://{project}/annotations",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/profiles/{kind}/{profile_id}",
    resource: "typeflux://{project}/profiles/{kind}/{profile_id}",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/projects",
    tool: "list_projects",
    resource: "typeflux://projects",
  },
  // --- WRAPPED: resolution-dependent reads (501 UnsupportedRuntime on an unresolvable runtime) ---
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/validate",
    tool: "validate_project",
    resource: "typeflux://{project}/validate",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/bundle",
    tool: "get_bundle",
    resource: "typeflux://{project}/workflows/{workflow_id}/bundle",
    note: "topology is projected from bundle.topology as its own resource",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/catalog",
    tool: "get_catalog",
    resource: "typeflux://{project}/workflows/{workflow_id}/catalog",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/connections",
    tool: "get_connections",
    resource: "typeflux://{project}/workflows/{workflow_id}/connections",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/correlation",
    tool: "get_correlation",
    resource: "typeflux://{project}/workflows/{workflow_id}/correlation",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/executions",
    tool: "list_executions",
    resource: "typeflux://{project}/workflows/{workflow_id}/executions",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/prompt-status",
    tool: "get_prompt_status",
    resource: "typeflux://{project}/workflows/{workflow_id}/prompt-status",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/status",
    tool: "get_status",
    resource: "typeflux://{project}/workflows/{workflow_id}/status",
    note: "Phase 1 adds the get_status tool (an inspect read, trace=false); the resource is subscribable",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/versions",
    tool: "get_versions",
    resource: "typeflux://{project}/workflows/{workflow_id}/versions",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/workflows/{workflow_id}/workers",
    tool: "get_workers",
    resource: "typeflux://{project}/workflows/{workflow_id}/workers",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/deployments",
    tool: "list_deployments",
    resource: "typeflux://{project}/deployments",
  },
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/deployments/{plan_id}",
    tool: "get_deployment",
    resource: "typeflux://{project}/deployments/{plan_id}",
  },
  // Tool-only (no resource): the feed is parameterized (env scope REQUIRED + filters/cursor),
  // so a static resource template can't represent it — same treatment as other query-shaped reads.
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/enforcement-events",
    tool: "list_enforcement_events",
  },
  // Tool-only: the github-provenance surface is a read-at-request snapshot (HEAD drift + plan→PR)
  // whose freshness depends on a live GitHub read, so it is a tool rather than a cacheable resource.
  {
    kind: "wrapped",
    method: "GET",
    path: "/api/v1/github-provenance",
    tool: "get_github_provenance",
  },
  // --- WRAPPED: operate tier (mutating) — Phase 1, capability-gated + confirmation-hinted (§6.3/§9) ---
  {
    kind: "wrapped",
    method: "POST",
    path: "/api/v1/workflows/{workflow_id}/start",
    tool: "start_workflow",
    note: "catalog-schema validation + elicitation + expected_policy_hash; gated on can_start",
  },
  {
    kind: "wrapped",
    method: "POST",
    path: "/api/v1/workflows/{workflow_id}/review",
    tool: "submit_review",
    note: "only version-valid decisions; gated on can_review",
  },
  {
    kind: "wrapped",
    method: "POST",
    path: "/api/v1/workflows/{workflow_id}/cancel",
    tool: "cancel_workflow",
    note: "destructiveHint; gated on can_cancel",
  },
  {
    kind: "wrapped",
    method: "POST",
    path: "/api/v1/workflows/{workflow_id}/repin",
    tool: "repin_operations",
    note: "idempotentHint; requires project.refresh permission → gated on can_refresh_project",
  },
  {
    kind: "wrapped",
    method: "POST",
    path: "/api/v1/projects/{project}/refresh",
    tool: "refresh_project",
    note: "idempotentHint; gated on can_refresh_project",
  },
  // --- EXCLUDED: still out of scope ---
  {
    kind: "excluded",
    method: "POST",
    path: "/api/v1/workflows/{workflow_id}/migrate",
    phase: 3,
    reason:
      "the #204 long-drain terminate-and-resubmit primitive (requires START+CANCEL). Deliberately " +
      "NOT in the design's §6.3 operate list (nor §4's tools row) — Phase 1 wrapped exactly the six " +
      "verbs the design enumerates; Phase 2 (recipes/completions) and Phase 3 (local authoring aids + " +
      "Streamable-HTTP transport) add NO CP write. The Phase-3 scaffold_*/doctor tools are LOCAL " +
      "(workspace + a read-only CP ping), not contract operations, so they are not ledger entries. " +
      "migrate stays deferred pending a design decision to surface it as its own confirmation-hinted " +
      "tool; wrapping it silently would exceed the documented operate surface.",
  },
];

/**
 * The design §6.2 observability tools (`trace_search`, `trace_list`, `trace_inspect`, `trace_diff`,
 * `trace_export`) are BLOCKED on a contract surface and remain DEFERRED after Phase 2.
 *
 * WHY (re-verified in Phase 2, per the Phase-1 handoff): the control-plane contract
 * (contracts/controlplane/openapi.v1.json) exposes NO trace/observability route — there is no
 * trace/search/inspect/diff/export path anywhere in it. The MCP server's ONLY backing is that
 * contract (via `@typeflux/control-plane-client`, attach or managed-local), so it has no reachable
 * trace surface to wrap:
 *   - `GET .../correlation` (wrapped) returns a run's observability IDs (e.g. the Langfuse trace id),
 *     but NOT trace content, and there is no CP endpoint to search/inspect/diff/export traces.
 *   - The design §6.2's "observability trace CLI (or its programmatic equivalent)" lives in the
 *     PYTHON package (packages/python/src/typeflux/observability: backend/inspect/diff/…),
 *     a local CLI over a trace backend (Langfuse/LangSmith/OTel). It is not exposed over the CP HTTP
 *     API and the MCP server is pure-Node — it cannot invoke it. The TS `trace-writer`/`trace` code
 *     is worker-side WRITE path, not a queryable store.
 * So trace_* CANNOT ship against the CP client in Phase 2 without fabricating a surface. They are
 * intentionally absent from the ledger above (which mirrors the contract 1:1). Unblock requires a CP
 * observability/trace contract surface (recommended follow-up); the `/typeflux:diagnose-run` recipe
 * therefore composes the trace-less signals (status/connections/workers/prompt-status) and notes the
 * prior-run `trace_diff` as pending that surface.
 */
export const DEFERRED_NON_CONTRACT_TOOLS = [
  "trace_search",
  "trace_list",
  "trace_inspect",
  "trace_diff",
  "trace_export",
] as const;

/** Every tool the ledger records as wrapping a contract operation (read tier + operate tier). */
export function wrappedToolNames(): string[] {
  return COVERAGE_LEDGER.filter(
    (entry): entry is WrappedOperation => entry.kind === "wrapped" && entry.tool !== undefined,
  ).map((entry) => entry.tool!);
}
