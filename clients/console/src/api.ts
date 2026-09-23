/**
 * API layer: one typed client instance plus the contract type aliases the
 * console uses. All types come from the generated client — never declare
 * contract shapes by hand.
 */

import { createControlPlaneClient } from "@gibli-labs/control-plane-client";
import type { components } from "@gibli-labs/control-plane-client";

export type ApiMeta = components["schemas"]["ApiMeta"];
export type Capabilities = components["schemas"]["ApiCapabilities"];
export type WorkflowSummary = components["schemas"]["ApiWorkflowSummary"];
export type EnvironmentSummary = components["schemas"]["ApiEnvironmentSummary"];
export type Bundle = components["schemas"]["ResolvedWorkflowBundle"];
export type BundleTopology = components["schemas"]["BundleTopology"];
export type Catalog = components["schemas"]["ActivityCatalog"];
export type ValidationReport = components["schemas"]["ProjectValidationReport"];
export type ValidationIssue = components["schemas"]["ProjectValidationIssue"];
export type DrainStatus = components["schemas"]["WorkflowDrainStatus"];
export type OperationStatus = components["schemas"]["WorkflowOperationStatus"];
export type ExecutionList = components["schemas"]["WorkflowExecutionList"];
export type RunCorrelation = components["schemas"]["WorkflowRunCorrelation"];

/**
 * Same-origin by default: dev uses the Vite proxy, a co-hosted deployment
 * needs nothing. A separately-hosted console can pin the API origin in
 * localStorage (and the server must opt in via --cors-origin).
 */
export const apiBase: string =
  (typeof localStorage !== "undefined" && localStorage.getItem("typeflux.apiBase")) || "";

export const client = createControlPlaneClient({ baseUrl: apiBase });

/**
 * Active project (#256). When set, every request is rewritten to the
 * project-scoped routes (`/api/v1/projects/{id}/...`); when unset, the
 * unprefixed routes serve the server's default project. Persisted so a
 * reload keeps the selection.
 */
const PROJECT_KEY = "typeflux.project";

export function currentProject(): string | null {
  return (typeof localStorage !== "undefined" && localStorage.getItem(PROJECT_KEY)) || null;
}

/** The ProjectSummary the console is scoped to: the ?project= selection, else the default. */
export function activeProject(projects: ProjectSummary[]): ProjectSummary | undefined {
  const id = currentProject();
  return id !== null
    ? projects.find((project) => project.id === id)
    : (projects.find((project) => project.default) ?? projects[0]);
}

export function setCurrentProject(projectId: string | null): void {
  if (typeof localStorage === "undefined") return;
  if (projectId) localStorage.setItem(PROJECT_KEY, projectId);
  else localStorage.removeItem(PROJECT_KEY);
}

/**
 * Bearer token for a token-protected control plane (#323). Persisted like
 * `apiBase`/project so a reload keeps it, and applied via the request
 * middleware below — letting an "internal operator console" deployment
 * authenticate without an external header-injecting proxy. The value lives only
 * in localStorage and on the outgoing `Authorization` header; never logged.
 */
const API_TOKEN_KEY = "typeflux.apiToken";

export function currentApiToken(): string | null {
  return (typeof localStorage !== "undefined" && localStorage.getItem(API_TOKEN_KEY)) || null;
}

export function setApiToken(token: string | null): void {
  if (typeof localStorage === "undefined") return;
  if (token) localStorage.setItem(API_TOKEN_KEY, token);
  else localStorage.removeItem(API_TOKEN_KEY);
}

// Attach the operator's bearer token (when set) to every request. Read live per
// request, so setting/clearing the token takes effect without a reload.
client.use({
  onRequest({ request }) {
    const token = currentApiToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
});

const API_PREFIX = "/api/v1/";

// Inject the active project segment into every request path. The typed call
// sites stay unchanged (`/api/v1/workflows`); only the effective URL changes.
client.use({
  onRequest({ request }) {
    const projectId = currentProject();
    if (!projectId) return undefined;
    const url = new URL(request.url);
    const at = url.pathname.indexOf(API_PREFIX);
    if (at === -1) return undefined;
    const rest = url.pathname.slice(at + API_PREFIX.length);
    // The registry listing is project-agnostic; never scope it, and never
    // double-scope an already-scoped path.
    if (rest === "projects" || rest.startsWith("projects/")) return undefined;
    url.pathname = `${url.pathname.slice(0, at)}${API_PREFIX}projects/${encodeURIComponent(
      projectId,
    )}/${rest}`;
    return new Request(url, request);
  },
});

export interface ApiFailure {
  status: number;
  message: string;
  /** The contract's `ApiError.error` discriminant (#617) — e.g. `UnsupportedRuntime`. */
  code?: string;
}

export function describeError(status: number, body: unknown): ApiFailure {
  if (body && typeof body === "object") {
    const candidate = body as { error?: string; message?: string; detail?: string };
    const message = candidate.message ?? candidate.detail;
    if (message) {
      return {
        status,
        message: candidate.error ? `${candidate.error}: ${message}` : message,
        ...(candidate.error ? { code: candidate.error } : {}),
      };
    }
  }
  return { status, message: `request failed with status ${status}` };
}

/**
 * A failed API call with the wire taxonomy preserved (#621 slice 3): `code` is the
 * contract's `ApiError.error` discriminant, `status` the HTTP status. Surfaces that
 * must distinguish a by-construction limitation (501 `UnsupportedRuntime`) from a
 * genuine failure branch on these instead of parsing the flattened message.
 */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  constructor(failure: ApiFailure) {
    super(failure.message);
    this.name = "ApiRequestError";
    this.status = failure.status;
    this.code = failure.code;
  }
}

/**
 * True when the routed project's runtime has no resolver on the serving control
 * plane (#619) — resolution surfaces render an honest unavailable state for this,
 * never a raw error panel: the server is behaving exactly as the contract says.
 */
export function isUnsupportedRuntime(error: unknown): boolean {
  return error instanceof ApiRequestError && (error.code === "UnsupportedRuntime" || error.status === 501);
}

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await promise;
  if (data === undefined) {
    throw new ApiRequestError(describeError(response.status, error));
  }
  return data;
}

export function fetchMeta(): Promise<ApiMeta> {
  return unwrap(client.GET("/api/v1/meta"));
}

export async function fetchWorkflows(): Promise<WorkflowSummary[]> {
  const payload = await unwrap(client.GET("/api/v1/workflows"));
  return payload.workflows ?? [];
}

export async function fetchEnvironments(): Promise<EnvironmentSummary[]> {
  const payload = await unwrap(client.GET("/api/v1/environments"));
  return payload.environments ?? [];
}

export function fetchValidate(environmentId?: string): Promise<ValidationReport> {
  return unwrap(
    client.GET("/api/v1/validate", {
      params: { query: environmentId ? { environment_id: environmentId } : {} },
    }),
  );
}

export function fetchBundle(workflowId: string, environmentId: string): Promise<Bundle> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/bundle", {
      params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
    }),
  );
}

export function fetchCatalog(workflowId: string, environmentId: string): Promise<Catalog> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/catalog", {
      params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
    }),
  );
}

export function fetchVersions(workflowId: string, environmentId: string): Promise<DrainStatus> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/versions", {
      params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
    }),
  );
}

export function fetchStatus(
  workflowId: string,
  environmentId: string,
  executionId: string,
): Promise<OperationStatus> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/status", {
      params: {
        path: { workflow_id: workflowId },
        query: { environment_id: environmentId, execution_id: executionId },
      },
    }),
  );
}

export type StartRequest = components["schemas"]["ApiStartRequest"];
export type StartReceipt = components["schemas"]["WorkflowStartReceipt"];
export type ReviewRequest = components["schemas"]["ApiReviewRequest"];
export type CancelRequest = components["schemas"]["ApiCancelRequest"];
export type ReviewCommand = components["schemas"]["ReviewCommand"];

export function startWorkflow(workflowId: string, body: StartRequest): Promise<StartReceipt> {
  return unwrap(
    client.POST("/api/v1/workflows/{workflow_id}/start", {
      params: { path: { workflow_id: workflowId } },
      body,
    }),
  );
}

async function expectNoContent(
  promise: Promise<{ error?: unknown; response: Response }>,
): Promise<void> {
  const { error, response } = await promise;
  if (!response.ok) {
    throw new ApiRequestError(describeError(response.status, error));
  }
}

export function submitReview(workflowId: string, body: ReviewRequest): Promise<void> {
  return expectNoContent(
    client.POST("/api/v1/workflows/{workflow_id}/review", {
      params: { path: { workflow_id: workflowId } },
      body,
    }),
  );
}

export function requestCancel(workflowId: string, body: CancelRequest): Promise<void> {
  return expectNoContent(
    client.POST("/api/v1/workflows/{workflow_id}/cancel", {
      params: { path: { workflow_id: workflowId } },
      body,
    }),
  );
}

export function fetchExecutions(
  workflowId: string,
  environmentId: string,
): Promise<ExecutionList> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/executions", {
      params: {
        path: { workflow_id: workflowId },
        query: { environment_id: environmentId },
      },
    }),
  );
}

export function fetchCorrelation(
  workflowId: string,
  environmentId: string,
  executionId: string,
): Promise<RunCorrelation> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/correlation", {
      params: {
        path: { workflow_id: workflowId },
        query: { environment_id: environmentId, execution_id: executionId },
      },
    }),
  );
}

export type EnvironmentDefinition = components["schemas"]["EnvironmentDefinition"];
export type PolicySummary = components["schemas"]["PolicySummary"];
export type PolicyDefinition = components["schemas"]["PolicyDefinition"];
export type ProfileSummary = components["schemas"]["ProfileSummary"];
export type ProfileDefinition = components["schemas"]["ProfileDefinition"];

export function fetchEnvironmentDefinition(id: string): Promise<EnvironmentDefinition> {
  return unwrap(
    client.GET("/api/v1/environments/{environment_id}", {
      params: { path: { environment_id: id } },
    }),
  );
}

export async function fetchPolicies(): Promise<PolicySummary[]> {
  return unwrap(client.GET("/api/v1/policies"));
}

export function fetchPolicyDefinition(id: string): Promise<PolicyDefinition> {
  return unwrap(
    client.GET("/api/v1/policies/{policy_id}", { params: { path: { policy_id: id } } }),
  );
}

export async function fetchProfiles(): Promise<ProfileSummary[]> {
  return unwrap(client.GET("/api/v1/profiles"));
}

export function fetchProfileDefinition(kind: string, id: string): Promise<ProfileDefinition> {
  return unwrap(
    client.GET("/api/v1/profiles/{kind}/{profile_id}", {
      params: { path: { kind, profile_id: id } },
    }),
  );
}

export type RuntimePinInfo = components["schemas"]["RuntimePinInfo"];
export type ApiRepinResult = components["schemas"]["ApiRepinResult"];

export function postRepin(workflowId: string, environmentId: string): Promise<ApiRepinResult> {
  return unwrap(
    client.POST("/api/v1/workflows/{workflow_id}/repin", {
      params: { path: { workflow_id: workflowId } },
      body: { environment_id: environmentId },
    }),
  );
}

export type WorkflowConnections = components["schemas"]["WorkflowConnections"];

export function fetchConnections(
  workflowId: string,
  environmentId: string,
): Promise<WorkflowConnections> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/connections", {
      params: {
        path: { workflow_id: workflowId },
        query: { environment_id: environmentId },
      },
    }),
  );
}

export type WorkflowPromptStatus = components["schemas"]["WorkflowPromptStatus"];

export function fetchPromptStatus(
  workflowId: string,
  environmentId: string,
): Promise<WorkflowPromptStatus> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/prompt-status", {
      params: {
        path: { workflow_id: workflowId },
        query: { environment_id: environmentId },
      },
    }),
  );
}

export type TaskQueueWorkers = components["schemas"]["WorkflowTaskQueueWorkers"];

export function fetchWorkers(
  workflowId: string,
  environmentId: string,
  taskQueue?: string,
): Promise<TaskQueueWorkers> {
  return unwrap(
    client.GET("/api/v1/workflows/{workflow_id}/workers", {
      params: {
        path: { workflow_id: workflowId },
        query: { environment_id: environmentId, ...(taskQueue ? { task_queue: taskQueue } : {}) },
      },
    }),
  );
}

export type DeploymentPlan = components["schemas"]["DeploymentPlan"];
export type PlanVerification = components["schemas"]["PlanVerification"];
export type DeploymentEntry = components["schemas"]["_DeploymentEntry"];

export async function fetchDeployments(): Promise<DeploymentEntry[]> {
  return unwrap(client.GET("/api/v1/deployments"));
}

export type EnforcementEventList = components["schemas"]["EnforcementEventList"];
export type EnforcementEvent = components["schemas"]["EnforcementEvent"];
export type EnforcementPartial = components["schemas"]["EnforcementPartial"];
export type EnforcementVerdict = EnforcementEvent["verdict"];

/**
 * One page of the enforcement-events feed (#723). The endpoint REQUIRES
 * environment scope (an unscoped call cannot resolve admission verdicts and
 * 422s), so `environmentId` is a positional argument, not a filter. `cursor`
 * pages within one filter set — the server binds the cursor to a fingerprint
 * of the filters it was minted under, so callers must hold `since` (and every
 * other filter) fixed across load-more requests.
 */
export function fetchEnforcementEvents(
  environmentId: string,
  query: {
    workflowId?: string | null;
    verdict?: EnforcementVerdict | null;
    since?: string | null;
    cursor?: string | null;
  } = {},
): Promise<EnforcementEventList> {
  return unwrap(
    client.GET("/api/v1/enforcement-events", {
      params: {
        query: {
          environment_id: environmentId,
          ...(query.workflowId ? { workflow_id: [query.workflowId] } : {}),
          ...(query.verdict ? { verdict: [query.verdict] } : {}),
          ...(query.since ? { since: query.since } : {}),
          ...(query.cursor ? { cursor: query.cursor } : {}),
        },
      },
    }),
  );
}

export type GithubProvenance = components["schemas"]["GithubProvenance"];
export type HeadProvenance = components["schemas"]["HeadProvenance"];
export type PlanProvenance = components["schemas"]["PlanProvenance"];
export type PullRequestRef = components["schemas"]["PullRequestRef"];
export type GithubPartial = components["schemas"]["GithubPartial"];
export type GithubPartialState = GithubPartial["github"];

/**
 * The project-level github-provenance envelope (#727): HEAD-vs-served drift plus every approved
 * plan's commit + approving PR, read server-side at request time. Env-agnostic — plan files and
 * the repo HEAD are project-level, so the endpoint takes no query params (project scope comes from
 * the request-path middleware, like every other read). One fetch serves all plan rows; callers
 * never fan out per plan.
 */
export function fetchGithubProvenance(): Promise<GithubProvenance> {
  return unwrap(client.GET("/api/v1/github-provenance"));
}

export type ProjectSummary = components["schemas"]["ProjectSummary"];

export async function fetchProjects(): Promise<ProjectSummary[]> {
  return unwrap(client.GET("/api/v1/projects"));
}

export type ProjectAnnotations = components["schemas"]["ProjectAnnotations"];
export type InsightAnnotation = components["schemas"]["InsightAnnotation"];

/**
 * The project-level insight-acknowledgement projection (#733): the parsed `.typeflux/annotations.yaml`
 * beside the manifest. A pure-YAML, project-level read — NOT resolution-bound and (unlike
 * github_provenance / enforcement_events) NOT capability-gated: the control plane serves it
 * unconditionally, even for a project it cannot resolve, so there is no `enabled` gate. Env-agnostic
 * (the file is project-level), so no query params — project scope comes from the request-path
 * middleware like every other read. A malformed file is served as an EMPTY projection and reported
 * separately as an `invalid_annotations_file` validation issue (fail-closed), never a partial parse.
 */
export function fetchAnnotations(): Promise<ProjectAnnotations> {
  return unwrap(client.GET("/api/v1/annotations"));
}

export type RefreshResult = components["schemas"]["ProjectRefreshResult"];

export async function refreshProject(projectId: string): Promise<RefreshResult> {
  return unwrap(
    client.POST("/api/v1/projects/{project}/refresh", {
      params: { path: { project: projectId } },
    }),
  );
}
