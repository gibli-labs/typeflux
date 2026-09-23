/**
 * Typed wrapper over `@typeflux/control-plane-client` for the MCP read tier (#326 Phase 0).
 *
 * Directly modeled on clients/console/src/api.ts: one openapi-fetch client, an `unwrap` that turns
 * a non-2xx into a structured {@link ApiRequestError}, and one thin read method per contract GET
 * operation. The client speaks paths, not named methods: `client.GET("/api/v1/...", {params})`.
 * Every response is the secret-free JSON the control plane already emits (the CP redacts at its
 * boundary; this layer adds nothing and strips nothing).
 *
 * Project scoping mirrors the console: when a project id is set, an onRequest middleware rewrites
 * `/api/v1/<rest>` to `/api/v1/projects/<id>/<rest>` (never double-scoping, never scoping the
 * project registry listing). A bearer token, when present, is attached as `Authorization: Bearer`
 * and is never logged.
 */

import { createControlPlaneClient } from "@typeflux/control-plane-client";
import type { components } from "@typeflux/control-plane-client";

import { ApiRequestError, describeError } from "./errors.js";

export type ApiMeta = components["schemas"]["ApiMeta"];
export type Capabilities = components["schemas"]["ApiCapabilities"];
export type WorkflowSummary = components["schemas"]["ApiWorkflowSummary"];
export type EnvironmentSummary = components["schemas"]["ApiEnvironmentSummary"];
export type EnvironmentDefinition = components["schemas"]["EnvironmentDefinition"];
export type Bundle = components["schemas"]["ResolvedWorkflowBundle"];
export type BundleTopology = components["schemas"]["BundleTopology"];
export type Catalog = components["schemas"]["ActivityCatalog"];
export type ValidationReport = components["schemas"]["ProjectValidationReport"];
export type DrainStatus = components["schemas"]["WorkflowDrainStatus"];
export type OperationStatus = components["schemas"]["WorkflowOperationStatus"];
export type ExecutionList = components["schemas"]["WorkflowExecutionList"];
export type RunCorrelation = components["schemas"]["WorkflowRunCorrelation"];
export type PolicySummary = components["schemas"]["PolicySummary"];
export type PolicyDefinition = components["schemas"]["PolicyDefinition"];
export type ProfileSummary = components["schemas"]["ProfileSummary"];
export type ProfileDefinition = components["schemas"]["ProfileDefinition"];
export type WorkflowConnections = components["schemas"]["WorkflowConnections"];
export type WorkflowPromptStatus = components["schemas"]["WorkflowPromptStatus"];
export type TaskQueueWorkers = components["schemas"]["WorkflowTaskQueueWorkers"];
export type EnforcementEventList = components["schemas"]["EnforcementEventList"];
export type GithubProvenance = components["schemas"]["GithubProvenance"];
export type InsightAnnotation = components["schemas"]["InsightAnnotation"];
export type DeploymentEntry = components["schemas"]["_DeploymentEntry"];
export type DeploymentPlan = components["schemas"]["DeploymentPlan"];
export type ProjectSummary = components["schemas"]["ProjectSummary"];

// --- Operate tier (#326 Phase 1; design §6.3) — the mutating verbs, reused verbatim from the
// contract like the console's api.ts. The MCP operate tools carry the matching permission and are
// capability-gated + confirmation-hinted at the server (see operate.ts / server.ts). ---
export type StartRequest = components["schemas"]["ApiStartRequest"];
export type StartReceipt = components["schemas"]["WorkflowStartReceipt"];
export type ReviewRequest = components["schemas"]["ApiReviewRequest"];
export type ReviewCommand = components["schemas"]["ReviewCommand"];
export type CancelRequest = components["schemas"]["ApiCancelRequest"];
export type RepinResult = components["schemas"]["ApiRepinResult"];
export type RefreshResult = components["schemas"]["ProjectRefreshResult"];

export interface ControlPlaneClientOptions {
  baseUrl: string;
  /** Optional bearer token for a token-protected control plane (#323); attached, never logged. */
  token?: string | undefined;
  /** Optional project id; when set, every request routes to the project-scoped mount. */
  project?: string | undefined;
}

const API_PREFIX = "/api/v1/";

/**
 * A read-only view of one Typeflux control plane. Each method wraps one contract GET operation and
 * returns the parsed body, throwing {@link ApiRequestError} (status + code preserved) on failure.
 */
export class TypefluxControlPlane {
  private readonly client: ReturnType<typeof createControlPlaneClient>;
  readonly baseUrl: string;
  readonly project: string | undefined;

  constructor(options: ControlPlaneClientOptions) {
    this.baseUrl = options.baseUrl;
    this.project = options.project;
    this.client = createControlPlaneClient({ baseUrl: options.baseUrl });

    const token = options.token;
    if (token) {
      this.client.use({
        onRequest({ request }) {
          request.headers.set("Authorization", `Bearer ${token}`);
          return request;
        },
      });
    }

    const project = options.project;
    if (project) {
      this.client.use({
        onRequest({ request }) {
          const url = new URL(request.url);
          const at = url.pathname.indexOf(API_PREFIX);
          if (at === -1) return undefined;
          const rest = url.pathname.slice(at + API_PREFIX.length);
          // The registry listing is project-agnostic; never scope it, never double-scope.
          if (rest === "projects" || rest.startsWith("projects/")) return undefined;
          url.pathname = `${url.pathname.slice(0, at)}${API_PREFIX}projects/${encodeURIComponent(
            project,
          )}/${rest}`;
          return new Request(url, request);
        },
      });
    }
  }

  private async unwrap<T>(
    promise: Promise<{ data?: T; error?: unknown; response: Response }>,
  ): Promise<T> {
    const { data, error, response } = await promise;
    if (data === undefined) {
      throw new ApiRequestError(describeError(response.status, error));
    }
    return data;
  }

  meta(): Promise<ApiMeta> {
    return this.unwrap(this.client.GET("/api/v1/meta"));
  }

  async workflows(): Promise<WorkflowSummary[]> {
    const payload = await this.unwrap(this.client.GET("/api/v1/workflows"));
    return payload.workflows ?? [];
  }

  async environments(): Promise<EnvironmentSummary[]> {
    const payload = await this.unwrap(this.client.GET("/api/v1/environments"));
    return payload.environments ?? [];
  }

  environment(environmentId: string): Promise<EnvironmentDefinition> {
    return this.unwrap(
      this.client.GET("/api/v1/environments/{environment_id}", {
        params: { path: { environment_id: environmentId } },
      }),
    );
  }

  policies(): Promise<PolicySummary[]> {
    return this.unwrap(this.client.GET("/api/v1/policies"));
  }

  policy(policyId: string): Promise<PolicyDefinition> {
    return this.unwrap(
      this.client.GET("/api/v1/policies/{policy_id}", { params: { path: { policy_id: policyId } } }),
    );
  }

  profiles(): Promise<ProfileSummary[]> {
    return this.unwrap(this.client.GET("/api/v1/profiles"));
  }

  /** The #733 insight-acknowledgement annotations projection: the in-repo `.typeflux/annotations.yaml`
   * entries the console renders as acknowledged/suppressed insights. A pure-YAML, project-level read —
   * always servable (never resolution-bound), empty when the file is absent or malformed (a malformed
   * file surfaces as a validation issue on `validate` instead). */
  async annotations(): Promise<InsightAnnotation[]> {
    const payload = await this.unwrap(this.client.GET("/api/v1/annotations"));
    return payload.annotations ?? [];
  }

  profile(kind: string, profileId: string): Promise<ProfileDefinition> {
    return this.unwrap(
      this.client.GET("/api/v1/profiles/{kind}/{profile_id}", {
        params: { path: { kind, profile_id: profileId } },
      }),
    );
  }

  /** Whole-project validation. Optionally scope to an environment and/or candidate workflows/policies. */
  validate(
    environmentId?: string,
    options: { workflowIds?: string[]; policyIds?: string[] } = {},
  ): Promise<ValidationReport> {
    const query: Record<string, unknown> = {};
    if (environmentId) query.environment_id = environmentId;
    if (options.workflowIds && options.workflowIds.length > 0) query.workflow_id = options.workflowIds;
    if (options.policyIds && options.policyIds.length > 0) query.policy_id = options.policyIds;
    return this.unwrap(
      this.client.GET("/api/v1/validate", { params: { query: query as never } }),
    );
  }

  /**
   * Resolve a workflow bundle. `policyIds` previews resolution under candidate policies;
   * `deploymentImage` previews the bundle for a specific deployment image (design §6.1).
   */
  bundle(
    workflowId: string,
    environmentId: string,
    options: { policyIds?: string[]; deploymentImage?: string } = {},
  ): Promise<Bundle> {
    const query: Record<string, unknown> = { environment_id: environmentId };
    if (options.policyIds && options.policyIds.length > 0) query.policy_id = options.policyIds;
    if (options.deploymentImage) query.deployment_image = options.deploymentImage;
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/bundle", {
        params: { path: { workflow_id: workflowId }, query: query as never },
      }),
    );
  }

  /** Topology has no endpoint — it is `bundle.topology`, projected here as its own read. */
  async topology(
    workflowId: string,
    environmentId: string,
    options: { policyIds?: string[]; deploymentImage?: string } = {},
  ): Promise<Bundle["topology"]> {
    const resolved = await this.bundle(workflowId, environmentId, options);
    return resolved.topology;
  }

  catalog(workflowId: string, environmentId: string): Promise<Catalog> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/catalog", {
        params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
      }),
    );
  }

  versions(workflowId: string, environmentId: string): Promise<DrainStatus> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/versions", {
        params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
      }),
    );
  }

  status(workflowId: string, environmentId: string, executionId: string): Promise<OperationStatus> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/status", {
        params: {
          path: { workflow_id: workflowId },
          query: { environment_id: environmentId, execution_id: executionId },
        },
      }),
    );
  }

  executions(workflowId: string, environmentId: string, limit?: number): Promise<ExecutionList> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/executions", {
        params: {
          path: { workflow_id: workflowId },
          query: { environment_id: environmentId, ...(limit !== undefined ? { limit } : {}) },
        },
      }),
    );
  }

  correlation(
    workflowId: string,
    environmentId: string,
    executionId: string,
  ): Promise<RunCorrelation> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/correlation", {
        params: {
          path: { workflow_id: workflowId },
          query: { environment_id: environmentId, execution_id: executionId },
        },
      }),
    );
  }

  connections(workflowId: string, environmentId: string): Promise<WorkflowConnections> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/connections", {
        params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
      }),
    );
  }

  promptStatus(workflowId: string, environmentId: string): Promise<WorkflowPromptStatus> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/prompt-status", {
        params: { path: { workflow_id: workflowId }, query: { environment_id: environmentId } },
      }),
    );
  }

  workers(
    workflowId: string,
    environmentId: string,
    taskQueue?: string,
  ): Promise<TaskQueueWorkers> {
    return this.unwrap(
      this.client.GET("/api/v1/workflows/{workflow_id}/workers", {
        params: {
          path: { workflow_id: workflowId },
          query: { environment_id: environmentId, ...(taskQueue ? { task_queue: taskQueue } : {}) },
        },
      }),
    );
  }

  deployments(): Promise<DeploymentEntry[]> {
    return this.unwrap(this.client.GET("/api/v1/deployments"));
  }

  /** The #723 enforcement-events feed (policy violations, Langfuse read-at-request). The
   * environment scope is REQUIRED by the endpoint (an unscoped call is a 422); the rest are the
   * contract's optional filters. `partial.langfuse` in the response marks degraded runtime reads. */
  enforcementEvents(
    environmentId: string,
    options: {
      workflowIds?: string[];
      policyIds?: string[];
      verdict?: string;
      since?: string;
      until?: string;
      limit?: number;
      cursor?: string;
    } = {},
  ): Promise<EnforcementEventList> {
    const query: Record<string, unknown> = { environment_id: environmentId };
    if (options.workflowIds && options.workflowIds.length > 0) query.workflow_id = options.workflowIds;
    if (options.policyIds && options.policyIds.length > 0) query.policy_id = options.policyIds;
    if (options.verdict !== undefined) query.verdict = options.verdict;
    if (options.since !== undefined) query.since = options.since;
    if (options.until !== undefined) query.until = options.until;
    if (options.limit !== undefined) query.limit = options.limit;
    if (options.cursor !== undefined) query.cursor = options.cursor;
    return this.unwrap(
      this.client.GET("/api/v1/enforcement-events", { params: { query: query as never } }),
    );
  }

  /** The #727 github-provenance surface: HEAD-vs-served drift + plan→approving-PR links, read at
   * request time server-side. Parameter-free (the served project's recorded git provenance and its
   * approved plans are the inputs); `partial.github` in the response marks degraded/unconfigured
   * GitHub reads (`not_configured` when no server-side token or no github repo provenance). */
  githubProvenance(): Promise<GithubProvenance> {
    return this.unwrap(this.client.GET("/api/v1/github-provenance"));
  }

  /** One deployment plan entry (`{path, plan, ...}` — the contract returns a `_DeploymentEntry`). */
  deployment(planId: string): Promise<DeploymentEntry> {
    return this.unwrap(
      this.client.GET("/api/v1/deployments/{plan_id}", { params: { path: { plan_id: planId } } }),
    );
  }

  /** The project registry listing (project-agnostic — never scoped). */
  projects(): Promise<ProjectSummary[]> {
    return this.unwrap(this.client.GET("/api/v1/projects"));
  }

  // --- Operate tier (#326 Phase 1; design §6.3). Every write reuses the same openapi-fetch client
  // (no separate heavy chunk — these are POST calls on the already-loaded client) and the same
  // status+code error mapping as the reads (`unwrap` / `expectNoContent` → ApiRequestError). ---

  /**
   * Resolve a 204-No-Content POST: openapi-fetch reports `data === undefined` on an empty body, so
   * `unwrap` can't distinguish success from failure — branch on `response.ok` instead (console's
   * `expectNoContent`), preserving the wire status+code on failure.
   */
  private async expectNoContent(
    promise: Promise<{ error?: unknown; response: Response }>,
  ): Promise<void> {
    const { error, response } = await promise;
    if (!response.ok) {
      throw new ApiRequestError(describeError(response.status, error));
    }
  }

  /** Start one execution of a resolved workflow version (`WorkflowStartReceipt`). */
  start(workflowId: string, body: StartRequest): Promise<StartReceipt> {
    return this.unwrap(
      this.client.POST("/api/v1/workflows/{workflow_id}/start", {
        params: { path: { workflow_id: workflowId } },
        body,
      }),
    );
  }

  /** Submit a lifecycle review decision for a running execution (204). */
  submitReview(workflowId: string, body: ReviewRequest): Promise<void> {
    return this.expectNoContent(
      this.client.POST("/api/v1/workflows/{workflow_id}/review", {
        params: { path: { workflow_id: workflowId } },
        body,
      }),
    );
  }

  /** Request cancellation of a running execution (204). */
  cancel(workflowId: string, body: CancelRequest): Promise<void> {
    return this.expectNoContent(
      this.client.POST("/api/v1/workflows/{workflow_id}/cancel", {
        params: { path: { workflow_id: workflowId } },
        body,
      }),
    );
  }

  /** Drop the pinned operations runtime for a workflow so it re-pins on the next mutating call. */
  repin(workflowId: string, environmentId: string): Promise<RepinResult> {
    return this.unwrap(
      this.client.POST("/api/v1/workflows/{workflow_id}/repin", {
        params: { path: { workflow_id: workflowId } },
        body: { environment_id: environmentId },
      }),
    );
  }

  /**
   * Re-fetch a Git-sourced project clone. Targets `/projects/{project}/refresh` by explicit path —
   * this route has no unprefixed form and is skipped by the project-scoping middleware, so a
   * default- or project-scoped client both address the named project without double-scoping.
   */
  refresh(projectId: string): Promise<RefreshResult> {
    return this.unwrap(
      this.client.POST("/api/v1/projects/{project}/refresh", {
        params: { path: { project: projectId } },
      }),
    );
  }
}
