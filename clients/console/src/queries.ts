/**
 * Query layer: every read the console performs, as cached TanStack Query
 * hooks over the typed fetchers in `api.ts` (#578). Query keys follow
 * `[resource, ...params]`; caching replaces the hand-rolled stale-response
 * guards the pages used to carry (RunDiff's key-tagged correlation hook,
 * RunPage's out-of-order correlation ref). Mutations stay plain calls in
 * `api.ts` — operations are deliberate, not cached.
 */

import { QueryClient, useInfiniteQuery, useQueries, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import type {
  Bundle,
  DrainStatus,
  EnvironmentSummary,
  ExecutionList,
  PolicySummary,
  RuntimePinInfo,
  WorkflowSummary,
} from "./api";
import {
  fetchBundle,
  fetchCatalog,
  fetchConnections,
  fetchCorrelation,
  fetchDeployments,
  fetchAnnotations,
  fetchEnforcementEvents,
  fetchEnvironmentDefinition,
  fetchEnvironments,
  fetchExecutions,
  fetchGithubProvenance,
  fetchMeta,
  fetchPolicies,
  fetchPolicyDefinition,
  fetchProfileDefinition,
  fetchProfiles,
  fetchProjects,
  fetchPromptStatus,
  fetchStatus,
  fetchValidate,
  fetchVersions,
  fetchWorkflows,
} from "./api";
import {
  enforcementPageParams,
  enforcementQueryKey,
  windowSince,
  type EnforcementFilters,
} from "./enforcementFeed";
import { pinProbeTarget } from "./runsFeed";

/**
 * The read tier re-reads YAML per request, so a short staleTime keeps the
 * console honest without hammering the server on every navigation. Focus
 * refetch stays off: an operator console should refresh deliberately, not
 * because a window was alt-tabbed.
 */
export function createConsoleQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: 1,
        refetchOnWindowFocus: false,
      },
    },
  });
}

export function errorMessage(error: unknown): string | undefined {
  if (error == null) return undefined;
  return error instanceof Error ? error.message : String(error);
}

export interface ShellData {
  meta: Awaited<ReturnType<typeof fetchMeta>>;
  workflows: Awaited<ReturnType<typeof fetchWorkflows>>;
  environments: Awaited<ReturnType<typeof fetchEnvironments>>;
  policies: Awaited<ReturnType<typeof fetchPolicies>>;
  profiles: Awaited<ReturnType<typeof fetchProfiles>>;
  projects: Awaited<ReturnType<typeof fetchProjects>>;
}

/**
 * Everything the shell (and most route components) need, as one query so
 * there is a single loading gate — the same shape `App.tsx` assembled with
 * Promise.all. Policies/profiles/projects degrade to empty lists so an older
 * or partial control plane still renders.
 */
export function useShellData() {
  return useQuery<ShellData>({
    queryKey: ["shell"],
    queryFn: async () => {
      const [meta, workflows, environments, policies, profiles, projects] = await Promise.all([
        fetchMeta(),
        fetchWorkflows(),
        fetchEnvironments(),
        fetchPolicies().catch(() => []),
        fetchProfiles().catch(() => []),
        fetchProjects().catch(() => []),
      ]);
      return { meta, workflows, environments, policies, profiles, projects };
    },
  });
}

export function useValidation(env: string) {
  return useQuery({
    queryKey: ["validate", env],
    queryFn: () => fetchValidate(env),
  });
}

const bundleKey = (workflowId: string, env: string) => ["bundle", workflowId, env] as const;

export function useBundle(workflowId: string, env: string) {
  return useQuery({
    queryKey: bundleKey(workflowId, env),
    queryFn: () => fetchBundle(workflowId, env),
  });
}

export interface WorkflowBundleEntry {
  workflow: WorkflowSummary;
  bundle?: Bundle;
  error?: string;
}

/**
 * One bundle per workflow (the Overview fan-out). Per-workflow failures stay
 * per-row (`error`), never fail the whole page, and each bundle shares the
 * cache with the workflow detail page.
 */
export function useWorkflowBundles(
  workflows: WorkflowSummary[],
  env: string,
): { entries: WorkflowBundleEntry[]; loading: boolean } {
  const results = useQueries({
    queries: workflows.map((workflow) => ({
      queryKey: bundleKey(workflow.id, env),
      queryFn: () => fetchBundle(workflow.id, env),
    })),
  });
  return {
    loading: results.some((result) => result.isPending),
    entries: workflows.map((workflow, index) => ({
      workflow,
      bundle: results[index].data,
      error: errorMessage(results[index].error),
    })),
  };
}

export interface BundleCell {
  workflowId: string;
  env: string;
  bundle?: Bundle;
  error?: string;
  pending: boolean;
  /** In-flight background refetch of already-cached data (isFetching, not isPending) —
   * consumers deriving cross-cell conclusions should surface "still comparing" (#604). */
  fetching: boolean;
  /** Last successful fetch time — changes on every refetch, for memo keys. */
  updatedAt: number;
}

/**
 * Every (workflow, environment) bundle — the Drift page's cross-environment
 * comparison input (#583). Each cell shares its cache entry with the
 * Overview fan-out and the workflow/diff pages via `bundleKey`; failures stay
 * per-cell (an environment that doesn't resolve locally is a note, not a
 * page failure).
 */
export function useBundleMatrix(workflowIds: string[], envIds: string[]): BundleCell[] {
  const pairs = workflowIds.flatMap((workflowId) =>
    envIds.map((env) => ({ workflowId, env })),
  );
  const results = useQueries({
    queries: pairs.map(({ workflowId, env }) => ({
      queryKey: bundleKey(workflowId, env),
      queryFn: () => fetchBundle(workflowId, env),
    })),
  });
  return pairs.map((pair, index) => ({
    ...pair,
    bundle: results[index].data,
    error: errorMessage(results[index].error),
    pending: results[index].isPending,
    fetching: results[index].isFetching,
    updatedAt: results[index].dataUpdatedAt,
  }));
}

/** The common shape every per-workflow query cell exposes for fingerprinting: the bundle matrix
 * carries `env`/`fetching`/`error`, the versions matrix `error`, the runtime-pin matrix
 * `unavailable` — optional fields simply contribute nothing for cells that lack them. */
export interface FingerprintableCell {
  workflowId: string;
  env?: string;
  pending: boolean;
  updatedAt: number;
  fetching?: boolean;
  error?: string;
  unavailable?: boolean;
}

/**
 * A stable fingerprint for memoizing an O(workflows × environments) derivation over a cell
 * matrix — the convention the Drift / Governance / Overview / Environment / persona surfaces
 * share, extracted (#721 F8) so every site keys its `useMemo` off ONE template (including the
 * Drift page's Temporal-tier versions/runtime-pin matrices, #577 §1). It reruns when any cell
 * settles (`pending` → `updatedAt`), refetches in the background (`fetching`), or changes
 * error/unavailable state, and never on an unrelated re-render.
 */
export function cellFingerprint(cells: FingerprintableCell[]): string {
  return cells
    .map(
      (cell) =>
        `${cell.workflowId}|${cell.env ?? ""}|${cell.pending ? "p" : cell.updatedAt}|${cell.fetching ? "f" : ""}|${cell.error ?? (cell.unavailable ? "u" : "")}`,
    )
    .join(";");
}

/**
 * The bundle matrix plus the two derivations every cross-cell surface pairs it with (#721 F10):
 * the settled gate (first load done across all cells) and the memo fingerprint. Takes the shell
 * summaries and maps them to ids, so the persona views can drop three lines of boilerplate each.
 */
export function useBundleCoverage(
  workflows: WorkflowSummary[],
  environments: EnvironmentSummary[],
): { cells: BundleCell[]; settled: boolean; fingerprint: string } {
  const cells = useBundleMatrix(
    workflows.map((workflow) => workflow.id),
    environments.map((environment) => environment.id),
  );
  return { cells, settled: cells.every((cell) => !cell.pending), fingerprint: cellFingerprint(cells) };
}

export function useCatalog(workflowId: string, env: string) {
  return useQuery({
    queryKey: ["catalog", workflowId, env],
    queryFn: () => fetchCatalog(workflowId, env),
  });
}

export function useVersions(workflowId: string, env: string) {
  return useQuery({
    queryKey: ["versions", workflowId, env],
    queryFn: () => fetchVersions(workflowId, env),
  });
}

export function useExecutions(workflowId: string, env: string) {
  return useQuery({
    queryKey: executionsKey(workflowId, env),
    queryFn: () => fetchExecutions(workflowId, env),
  });
}

export interface ExecutionsCell {
  workflowId: string;
  executions: ExecutionList["executions"];
  error?: string;
  pending: boolean;
}

/**
 * Recent executions across every workflow (the Runs surface's input, #589) —
 * one cached query per workflow over the shared executions key, so the
 * per-workflow Runs pages and the cross-workflow surface never double-fetch.
 * Per-workflow failures stay per-row; #586 bounds the Temporal tier
 * server-side, so a dead cluster answers fast instead of wedging the fan-out.
 */
export function useExecutionsMatrix(workflowIds: string[], env: string): ExecutionsCell[] {
  const results = useQueries({
    queries: workflowIds.map((workflowId) => ({
      queryKey: executionsKey(workflowId, env),
      queryFn: () => fetchExecutions(workflowId, env),
      // Temporal-tier calls are already bounded and retried server-side where
      // sensible; a client retry doubles the serial env-lock cost of a 9-way
      // fan-out against a dead cluster for no new information.
      retry: false,
    })),
  });
  return workflowIds.map((workflowId, index) => ({
    workflowId,
    // An errored cell exposes no rows even when a previous fetch succeeded:
    // stale executions beside an "unavailable" note for the same workflow
    // read as a contradiction, and nothing would mark which rows are stale.
    executions: results[index].isError ? [] : (results[index].data?.executions ?? []),
    error: errorMessage(results[index].error),
    pending: results[index].isPending,
  }));
}

export const executionsKey = (workflowId: string, env: string) =>
  ["executions", workflowId, env] as const;

export interface VersionsCell {
  workflowId: string;
  drain?: DrainStatus;
  error?: string;
  pending: boolean;
  /** Last successful fetch time — changes on every refetch, for memo keys. */
  updatedAt: number;
}

/**
 * Every workflow's cross-version drain status (the Drift page's version-drain input, #577 §1) —
 * one cached query per workflow over the shared `["versions", …]` key, so the per-workflow
 * Versions page and this fan-out never double-fetch. Temporal-tier: per-workflow failures stay
 * per-cell (`error`), and #586 bounds the read server-side so a dead cluster answers fast instead
 * of wedging the fan-out. An errored cell exposes NO drain data — a stale "drained" beside an
 * "unavailable" note would read as a false all-clear.
 *
 * `requested` gates the whole fan-out (nothing fetches until the operator asks): this is an
 * on-demand probe like {@link usePromptStatus}, because a control plane whose Temporal address is
 * routable-but-dead answers each read only at its bound (#581), and firing a fan-out of those on
 * every Drift-page load would occupy the browser's connection budget and starve the resolution-tier
 * reads the other sections need. Once requested the result is cached, so re-opening the page within
 * `staleTime` shows the last probe instantly.
 */
export function useVersionsMatrix(
  workflowIds: string[],
  env: string,
  requested: boolean,
): VersionsCell[] {
  const results = useQueries({
    queries: workflowIds.map((workflowId) => ({
      queryKey: ["versions", workflowId, env],
      queryFn: () => fetchVersions(workflowId, env),
      enabled: requested,
      // Temporal-tier, bounded + retried server-side where sensible (#586); a client retry only
      // doubles the serial env-lock cost of the fan-out against a dead cluster — same call as
      // useExecutionsMatrix.
      retry: false,
    })),
  });
  return workflowIds.map((workflowId, index) => ({
    workflowId,
    drain: results[index].isError ? undefined : results[index].data,
    error: errorMessage(results[index].error),
    // A disabled query reports `isPending` too; only a REQUESTED, in-flight fetch is pending here.
    pending: requested && results[index].isPending,
    updatedAt: results[index].dataUpdatedAt,
  }));
}

export interface RuntimePinCell {
  workflowId: string;
  pin?: RuntimePinInfo | null;
  /** The Temporal-tier read backing this workflow's pin was unavailable at one of its two stages
   * (executions fan-out or the representative status read): the pin is UNKNOWN, never assume "no
   * skew". */
  unavailable: boolean;
  pending: boolean;
  /** Last successful status fetch time (0 until a representative execution resolves), for memo keys. */
  updatedAt: number;
}

/**
 * Every workflow's runtime pin (the Drift page's pin-skew input, #577 §1). The runtime pin is a
 * workflow+env property surfaced on every execution's lifecycle status (#592), so this is a
 * two-stage Temporal-tier read: fan out the recent executions (shared cache with the Runs surface
 * via `executionsKey`), pick a representative (most recent) execution per workflow, then read its
 * status for the pin. Both stages are outage-honest — an error at either surfaces as `unavailable`
 * so the caller renders the class as UNKNOWN, never a false all-clear. A workflow with no runs
 * simply has no pin (nothing to compare), which is not an outage.
 *
 * `requested` gates BOTH stages (on-demand, like {@link useVersionsMatrix}): the status read for a
 * dead-but-routable cluster answers only at its client bound, so this never fires on page load.
 * The status read uses its OWN `["runtime-pin", …]` key (never the run inspector's `["status", …]`
 * key): the pin is stable, so a modest reuse window is honest here, whereas the inspector
 * deliberately keeps lifecycle snapshots un-cached. The read is untraced (`fetchStatus` server
 * default `trace=false`), so it records nothing.
 */
export function useRuntimePinMatrix(
  workflowIds: string[],
  env: string,
  requested: boolean,
): RuntimePinCell[] {
  const executionResults = useQueries({
    queries: workflowIds.map((workflowId) => ({
      queryKey: executionsKey(workflowId, env),
      queryFn: () => fetchExecutions(workflowId, env),
      enabled: requested,
      retry: false,
    })),
  });
  const reps = workflowIds.map((workflowId, index) => {
    const result = executionResults[index];
    return {
      workflowId,
      // Newest RUNNING execution preferred (a newer closed run must not shadow it — its lifecycle
      // query may not answer, and the running one is where live skew shows); see pinProbeTarget.
      executionId: result.isError
        ? undefined
        : pinProbeTarget(result.data?.executions ?? [])?.execution_id,
      execError: result.isError,
      execPending: requested && result.isPending,
    };
  });
  const statusResults = useQueries({
    // A stable-length array (one entry per workflow, aligned by index): reps without a
    // representative execution are DISABLED, not omitted, so the mapping below stays 1:1.
    queries: reps.map((rep) => ({
      queryKey: ["runtime-pin", rep.workflowId, env, rep.executionId ?? "none"],
      queryFn: () => fetchStatus(rep.workflowId, env, rep.executionId!),
      enabled: requested && rep.executionId !== undefined,
      retry: false,
      staleTime: 60_000,
    })),
  });
  return reps.map((rep, index) => {
    if (rep.execError) {
      return { workflowId: rep.workflowId, unavailable: true, pending: false, updatedAt: 0 };
    }
    if (rep.execPending) {
      return { workflowId: rep.workflowId, unavailable: false, pending: true, updatedAt: 0 };
    }
    if (rep.executionId === undefined) {
      // Readable, but no runs → no pin to compare. Not an outage.
      return { workflowId: rep.workflowId, unavailable: false, pending: false, updatedAt: 0 };
    }
    const status = statusResults[index];
    return {
      workflowId: rep.workflowId,
      pin: status.data?.runtime_pin,
      unavailable: status.isError,
      pending: requested && status.isPending,
      updatedAt: status.dataUpdatedAt,
    };
  });
}

/**
 * Run↔trace correlation for one execution; disabled until an execution is
 * selected. The query key carries the full fetch identity, which is exactly
 * the stale-response guard the old hand-rolled hooks implemented by hand.
 */
export function useCorrelation(workflowId: string, env: string, executionId: string | null) {
  return useQuery({
    queryKey: ["correlation", workflowId, env, executionId],
    queryFn: () => fetchCorrelation(workflowId, env, executionId!),
    enabled: executionId != null,
  });
}

/**
 * Lifecycle status for one execution. Untraced (`trace=false` server default),
 * so the query cache never serves a stale snapshot as if it were fresh:
 * staleTime 0 + no retention. When `polling`, the refetch interval honors the
 * server's recommended cadence from the last snapshot — the same contract the
 * old hand-rolled setTimeout chain implemented.
 */
export function useOperationStatus(
  workflowId: string,
  env: string,
  executionId: string | null,
  polling: boolean,
) {
  return useQuery({
    queryKey: ["status", workflowId, env, executionId],
    queryFn: () => fetchStatus(workflowId, env, executionId!),
    enabled: executionId != null,
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchInterval: polling
      ? (query) =>
          Math.max(query.state.data?.recommended_poll_interval_seconds ?? 1, 1) * 1000
      : false,
  });
}

/**
 * On-demand probes (#580): nothing fetches until the operator asks
 * (`requested`), but the result is a cached query — navigating away and back
 * within staleTime shows the last probe instantly instead of resetting to an
 * unpressed button. Probes reach external systems (registry, observer,
 * prompt registry), so a minute of reuse is the point, and `dataUpdatedAt`
 * lets callers show when the probe ran.
 */
export function useConnections(workflowId: string, env: string, requested: boolean) {
  return useQuery({
    queryKey: ["connections", workflowId, env],
    queryFn: () => fetchConnections(workflowId, env),
    enabled: requested,
    staleTime: 60_000,
  });
}

export function usePromptStatus(workflowId: string, env: string, requested: boolean) {
  return useQuery({
    queryKey: ["prompt-status", workflowId, env],
    queryFn: () => fetchPromptStatus(workflowId, env),
    enabled: requested,
    staleTime: 60_000,
  });
}

export interface PolicyDefinitionCell {
  policyId: string;
  definition?: import("./api").PolicyDefinition;
  pending: boolean;
}

/**
 * Every declared policy's definition (the Governance page's chain input) —
 * one cached query per policy, sharing entries with the sidebar policy pages
 * via the `useDefinition` key shape.
 */
export function usePolicyDefinitionMap(policyIds: string[]): PolicyDefinitionCell[] {
  const results = useQueries({
    queries: policyIds.map((policyId) => ({
      queryKey: ["definition", "policy", null, policyId],
      queryFn: async () => ({
        type: "policy" as const,
        data: await fetchPolicyDefinition(policyId),
      }),
    })),
  });
  return policyIds.map((policyId, index) => ({
    policyId,
    definition: results[index].data?.data,
    pending: results[index].isPending,
  }));
}

/**
 * The policy definitions plus the two derivations the persona governance / security views pair
 * them with (#721 F10): the settled gate and the manifest-relative `path` lookup (for the
 * source-of-truth links). Sharing one hook keeps the two views from re-deriving the same map.
 */
export function usePolicyDefinitions(policies: PolicySummary[]): {
  definitions: PolicyDefinitionCell[];
  settled: boolean;
  pathById: Map<string, string>;
} {
  const definitions = usePolicyDefinitionMap(policies.map((policy) => policy.id));
  return {
    definitions,
    settled: definitions.every((cell) => !cell.pending),
    pathById: new Map(policies.map((policy) => [policy.id, policy.path] as const)),
  };
}

/**
 * The enforcement-events feed for one environment (#723 slice 3) — the
 * console's ONE cursor-paginated read, as an infinite query: each "load more"
 * appends the next page under the same cached entry, keyed on the full filter
 * identity (env + workflow + verdict + frozen window bound). This is the
 * documented load-more pattern for future cursor feeds.
 *
 * The `since` bound is FROZEN per filter set, not recomputed per fetch: the
 * server binds each page cursor to a fingerprint of the raw filters it was
 * minted under, so a load-more request that shifted `since` would be rejected
 * (422) rather than silently skipping/duplicating rows. Changing any filter
 * (including re-picking the window) mints a fresh bound and a fresh query.
 * `until` is deliberately never sent — the server resolves it to now() per
 * request, and the cursor fingerprints the raw (absent) value, so pages stay
 * valid while the feed stays current.
 *
 * `enabled` gates on the `enforcement_events` capability: when the control
 * plane doesn't advertise the feed, nothing is fetched (the panel renders the
 * explicit not-supported state instead of an error).
 *
 * The key/page-param/frozen-since logic is the pure `enforcement*` layer in
 * `enforcementFeed.ts` (unit-tested there): this hook is the thin React seam that
 * freezes `since` per filter set and threads those helpers into TanStack. A
 * generic `useCursorFeed` extraction is DECLINED while this is the only cursor
 * feed — extract it when a second one exists; the pure-layer tests are the
 * guardrail until then.
 */
export function useEnforcementEvents(env: string, filters: EnforcementFilters, enabled: boolean) {
  const since = useMemo(
    () => windowSince(filters.window),
    // Re-anchor the window whenever ANY part of the filter identity changes —
    // each filter set gets a fresh "now", and load-more within it stays fixed.
    [filters.window, filters.workflowId, filters.verdict, env],
  );
  return useInfiniteQuery({
    queryKey: enforcementQueryKey(env, filters, since),
    queryFn: ({ pageParam }) =>
      fetchEnforcementEvents(env, enforcementPageParams(filters, since, pageParam)),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled,
  });
}

export function useDeployments() {
  return useQuery({
    queryKey: ["deployments"],
    queryFn: () => fetchDeployments(),
  });
}

/**
 * The project-level github-provenance read (#727): HEAD-vs-served drift + each plan's approving PR,
 * as ONE cached query the Drift section, the Overview insight feed, and the Deployments plan cards
 * all share (no per-plan fan-out).
 *
 * `enabled` gates on the `github_provenance` capability: absent (older contract) and false both
 * suppress the fetch — the surfaces render the explicit not-supported panel, never an error.
 *
 * The staleTime is DELIBERATELY generous (5 minutes, vs the 30s default): this read reaches the
 * GitHub REST API server-side, which is rate-limited (5,000 requests/hr, shared) and bounded per
 * request. A long reuse window keeps navigation between the three surfaces — and repeated visits —
 * from re-spending that shared budget; the data (a branch HEAD, merged PRs) moves on the order of
 * commits/merges, not seconds, so staleness of a few minutes is honest. An operator who needs a
 * fresh read refreshes the project (which re-clones server-side and re-answers).
 */
export function useGithubProvenance(enabled: boolean) {
  return useQuery({
    queryKey: ["github-provenance"],
    queryFn: () => fetchGithubProvenance(),
    enabled,
    staleTime: 5 * 60_000,
  });
}

/**
 * The project-level insight-acknowledgement annotations (#733): the parsed `.typeflux/annotations.yaml`
 * as ONE cached query every insight feed shares (InsightList mounts across Overview, Drift, and the
 * persona views). Unlike github_provenance / enforcement_events this read has NO capability gate — it
 * is pure-YAML and project-level, ALWAYS servable (the CP serves it even for a project it cannot
 * resolve; ApiCapabilities carries no `annotations` flag). So the hook is unconditionally enabled; a
 * server on an older contract without the route simply errors and the feeds fall back to no
 * acknowledgements (never a crash — {@link annotateInsights} degrades to all-active).
 *
 * The staleTime is generous (5 minutes): the annotations file is a committed repo artifact that
 * changes on a commit + refresh, not on the order of seconds, so a long reuse window keeps every
 * feed sharing one read. A project refresh invalidates this key (OverviewPage) so a re-clone's edits
 * show without a reload.
 */
export function useAnnotations() {
  return useQuery({
    queryKey: ["annotations"],
    queryFn: () => fetchAnnotations(),
    staleTime: 5 * 60_000,
  });
}

export function useDefinition(
  kind: "environment" | "policy" | "profile",
  id: string,
  profileKind?: string,
) {
  return useQuery({
    queryKey: ["definition", kind, profileKind ?? null, id],
    queryFn: async () => {
      if (kind === "environment") {
        return { type: "environment" as const, data: await fetchEnvironmentDefinition(id) };
      }
      if (kind === "policy") {
        return { type: "policy" as const, data: await fetchPolicyDefinition(id) };
      }
      return {
        type: "profile" as const,
        data: await fetchProfileDefinition(profileKind ?? "provider", id),
      };
    },
  });
}
