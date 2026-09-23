/**
 * The control-plane OPERATIONS core (governance parity, #563; Python
 * `controlplane/api.py` read handlers + `project/operations.py`). A pure, in-memory
 * projection of a loaded project bundle into the control-plane API's read DTOs —
 * meta, the workflow/environment listings, and validation. No Temporal, no HTTP: the
 * HTTP server slice adapts this; the policy/profile/topology/catalog projections and the
 * Temporal-backed operations (start/status/drain) layer on top in later slices.
 *
 * INJECTION-BASED: constructed from a {@link LoadedProjectBundle} (typically from
 * `loadProjectBundle`, or hand-built for tests). The DTO field names are snake_case to
 * match the Python API's secret-free contract JSON exactly, so the same generated client
 * / console works against either server.
 */

import {
  type AppliedComponentProfile,
  buildProjectDeploymentPlan,
  buildProjectPolicyRuntimeGuard,
  composeProfileOverrides,
  type DeploymentPlan,
  type DeploymentPlanResolver,
  type DeploymentResolvedWorkflow,
  deploymentPlanId,
  readDeploymentPlanDir,
  PLAN_DIR_NAME,
  type PlanVerification,
  verifyDeploymentPlan,
  PROFILE_KINDS,
  type ProfileKind,
  type ProfileSelection,
  type ProfileSourceIndex,
  ProjectDeploymentError,
  ProjectPolicyEnforcementError,
  ProjectPolicyError,
  ProjectProfileError,
  type LoadedProjectBundle,
  type ProjectBundleSources,
  type TypefluxProjectSpec,
  type TypefluxYamlSpec,
  type SubworkflowSpecResolver,
  type ValidateProjectBundleOptions,
  type WorkflowPlan,
  projectSubworkflowResolver,
  resolveEnvironmentWorkflow,
  specReferencesSubworkflows,
  validateProjectBundle,
  workflowPlanDigest,
  workflowPlanFromSpec,
} from "@typeflux/temporal-yaml";

import { basename, dirname, join } from "node:path";

import type { z } from "zod";

import { type ApiActivityCatalog, buildActivityCatalog, schemaLogicalName } from "./activity-catalog.js";
import { type ApiBundleTopology, buildBundleTopology } from "./bundle-topology.js";
import { type ApiEnvironmentDefinition, buildEnvironmentDefinition } from "./environment-definitions.js";
import {
  type ApiWorkflowExecutionList,
  listWorkflowExecutions,
  type VisibilityClientFactory,
} from "./executions.js";
import { type ApiWorkflowDrainStatus, workflowDrainStatus } from "./drain.js";
import {
  type ApiWorkflowTaskQueueWorkers,
  type TaskQueueClientFactory,
  workflowTaskQueueWorkers,
} from "./workers.js";
import { type ApiWorkflowRunCorrelation, workflowRunCorrelation } from "./correlation.js";
import {
  type ApiWorkflowMigrateResult,
  type ApiWorkflowOperationStatus,
  type ApiWorkflowStartReceipt,
  type OperationsClientFactory,
  WorkflowOperations,
} from "./operations.js";
import type { ReviewCommand } from "@typeflux/temporal-yaml";
import { type ApiWorkflowPromptStatus, buildWorkflowPromptStatus } from "./prompt-status.js";
import {
  type ApiWorkflowConnections,
  buildWorkflowConnections,
  type ConnectionProbe,
  defaultConnectionProbe,
} from "./connections.js";
import { type LangfuseControlPlaneTransport, langfuseConnectionProbe } from "./langfuse-transport.js";
import {
  buildEnforcementFeed,
  type EnforcementEventList,
  type EnforcementReadResult,
  observerFromReport,
} from "./enforcement.js";
import { ProjectControlPlaneError } from "./errors.js";
import {
  type ApiPolicyDefinition,
  type ApiPolicySummary,
  buildPolicyDefinition,
  buildPolicySummaries,
} from "./policy-definitions.js";
import {
  buildProfileDefinition,
  buildProfileSummaries,
  type ApiProfileDefinition,
  type ApiProfileSummary,
} from "./profile-definitions.js";
import { type ApiProjectAnnotations, loadProjectAnnotations, readProjectAnnotations } from "./annotations.js";
import { type ApiResolvedWorkflowBundle, buildResolvedWorkflowBundle } from "./resolved-bundle.js";
import {
  type ApiProjectValidationReport,
  toApiValidationReport,
} from "./validation-dto.js";

const errorMessage = (error: unknown): string => (error instanceof Error ? error.message : String(error));

/** POSIX shell-quote one argument (Python `shlex.quote`): safe strings pass through; anything
 * else is single-quoted with embedded quotes escaped. Used for copyable command surfaces. */
const shellQuote = (value: string): string =>
  value !== "" && /^[A-Za-z0-9_@%+=:,./-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;

/** Contract version pins carried in `meta` (Python `API_VERSION` / `BUNDLE_VERSION` / `CATALOG_VERSION`). */
export const API_VERSION = "1" as const;
export const BUNDLE_VERSION = "1" as const;
export const CATALOG_VERSION = "1" as const;

/**
 * What the caller can actually do against the routed project (Python `ApiCapabilities`, #619):
 * the actor's grants intersected with the server's abilities. `can_start`/`can_resolve` need
 * RESOLUTION (the plan is built from project code); `can_review`/`can_cancel` need only a binding
 * driver (an unresolvable-but-operable project reviews/cancels honestly). The open default (no
 * auth) grants every permission, so the effective flags reduce to `resolvable`/`operable`.
 */
export interface ApiCapabilities {
  can_start: boolean;
  can_review: boolean;
  can_cancel: boolean;
  can_refresh_project: boolean;
  can_resolve: boolean;
  /**
   * Whether the enforcement-events feed (#723) works for this project. The TS edition implements the
   * endpoint now (slice 2, over the Langfuse transport seam), but it is RESOLUTION-BOUND — admission
   * verdicts are derived from the resolved validation report and the route 501s behind
   * `requireResolvable` for an unresolvable project. So the flag follows `resolvable`, exactly like
   * `can_start`/`can_resolve`, and never advertises a route that would 501 (Python parity).
   */
  enforcement_events: boolean;
  /**
   * Whether the github-provenance surface (#727) has something to serve for this project. The TS
   * control plane implements the endpoint now (slice 2, over the GitHub transport seam), and it is
   * RESOLUTION-BOUND (the route 501s behind `requireResolvable` like `enforcement_events`) AND needs
   * a recorded GitHub repo source to compare — so the flag is `resolvable AND githubRepoPresent`,
   * exactly Python's `for_actor`. On this edition `githubRepoPresent` is structurally false: the TS
   * registry serves LOCAL checkouts only (no Git `repo:` source), so the flag evaluates false
   * everywhere — capability-honest (nothing to serve), not "unimplemented". The server-side TOKEN is
   * runtime config, reflected in `partial.github` (`not_configured`), never the capability.
   */
  github_provenance: boolean;
}

/** One approved deployment plan + its live verification (Python `_DeploymentEntry`, #687). */
export interface ApiDeploymentEntry {
  plan_id: string;
  plan: DeploymentPlan;
  verification: PlanVerification;
  /** The plan file's manifest-relative path (the console builds source links from it). */
  path: string;
  /** The exact CLI command an operator runs to promote this plan. */
  promote_command: string;
}

/**
 * The open authorizer's capabilities for a project (Python `ApiCapabilities.for_actor` with an
 * all-permissions actor): `can_refresh_project` is always granted; the resolution-bound flags
 * follow `resolvable`, the operation-bound flags follow `operable`.
 */
export const openCapabilities = (resolvable: boolean, operable: boolean): ApiCapabilities => ({
  can_start: resolvable,
  can_review: operable,
  can_cancel: operable,
  can_refresh_project: true,
  can_resolve: resolvable,
  // Resolution-bound like the read tier (#723 slice 2): the feed derives admission verdicts from the
  // resolved report and 501s for an unresolvable project, so the flag follows `resolvable`.
  enforcement_events: resolvable,
  // `resolvable AND githubRepoPresent` (Python `for_actor`), but the TS registry records no Git
  // source (local checkouts only, #727 slice 2), so `githubRepoPresent` is structurally false and
  // the flag is false everywhere. The handlers meta route computes the same expression from the
  // registry, so a future Git-source slice (or a test injecting a repo source) lights it up.
  github_provenance: false,
});

/** The default single-project capabilities: a resolvable, operable project under the open authorizer. */
const OPEN_CAPABILITIES: ApiCapabilities = openCapabilities(true, true);

/** A project's declared language runtime (Python `ApiMeta.runtime` / registry entry `runtime`, #619). */
export type ProjectRuntime = "python" | "typescript";

/**
 * The resolver contract's `resolve_plan` response (#642, resolver.v1.json): the raw plan a
 * ts-plan-argument start dispatches as its first argument, plus the start identity. snake_case —
 * this crosses the subprocess wire to a foreign-edition control plane, which treats `plan` as
 * opaque and dispatches it verbatim.
 */
export interface ApiResolvedPlan {
  plan: WorkflowPlan;
  task_queue: string;
  spec_digest: string;
  workflow_name: string;
  version_label: string | null;
  search_attribute: string | null;
}

/** Project + contract metadata (Python `ApiMeta`). */
export interface ApiMeta {
  api_version: typeof API_VERSION;
  bundle_version: typeof BUNDLE_VERSION;
  catalog_version: typeof CATALOG_VERSION;
  project: string;
  manifest_path: string;
  /** The routed project's declared language runtime (#619). */
  runtime: ProjectRuntime;
  /**
   * The authenticated caller's principal (Python `caller_identity`, #577), so the console can
   * attribute reviews to the proxy-auth identity instead of free text. Sourced ONLY from the
   * trusted proxy-auth path (`--trust-proxy-auth`); `null` in token / open modes — a token grant
   * NAME is config, not an identity, and an untrusted actor header is spoofable. The base `meta()`
   * returns `null`; the meta route (which has the auth context) fills it in under a proxy authorizer.
   */
  caller_identity: string | null;
  capabilities: ApiCapabilities;
}

/**
 * A declared workflow's manifest entry (Python `ApiWorkflowSummary`). `path` and `directory`
 * are BOTH always present — `null` for the unused one — because the Python model serializes
 * both (the `/workflows` handler has no `exclude_none`), so a contract client sees a stable shape.
 */
export interface ApiWorkflowSummary {
  id: string;
  path: string | null;
  directory: string | null;
  profiles: Record<string, string>;
}
export interface ApiWorkflowList {
  workflows: ApiWorkflowSummary[];
}

/** A declared environment's manifest entry (Python `ApiEnvironmentSummary`). */
export interface ApiEnvironmentSummary {
  id: string;
  path: string;
}
export interface ApiEnvironmentList {
  environments: ApiEnvironmentSummary[];
}

export interface ProjectControlPlaneOptions {
  /** The manifest path reported by `meta` (Python `project.manifest_path`). */
  manifestPath?: string;
  /** The routed project's declared runtime, reported by `meta` (Python registry entry, #619). Defaults to `typescript` — this server resolves TS. */
  runtime?: ProjectRuntime;
  /** The actor's capabilities reported by `meta` (defaults to the open, all-allowed set). */
  capabilities?: ApiCapabilities;
  /**
   * The activity input/output Zod schemas keyed by their spec ref (e.g. `"schemas:In"`) — the same
   * map the caller injects into `defineActivitiesFromSpec`. The activity CATALOG uses them to project
   * each schema's JSON Schema + content hash; a referenced ref that is NOT supplied fails the catalog
   * with a 422 (the `CatalogSchema` contract requires `hash`+`json_schema`, so there is no partial slot).
   */
  schemas?: Readonly<Record<string, z.ZodType>>;
  /**
   * Probes a backend's reachability for `connections()` (Python `_probe`). Neither edition
   * bounds this route — an injected probe that can hang MUST carry its own timeout. Defaults to a
   * NETWORK-FREE probe: non-langfuse backends read as reachable, a langfuse backend reads as
   * un-probed until a real probe is injected. Inject one to actually reach a langfuse host (#573).
   * When {@link ProjectControlPlaneOptions.langfuse} is set and this is not, the probe is derived
   * from that transport.
   */
  connectionProbe?: ConnectionProbe;
  /**
   * The injected langfuse reader transport (#573): powers the connections reachability probe AND the
   * prompt-status registry-drift tier (live label version vs last-run version). Zero vendor deps in
   * the core — a deployment injects a real transport (e.g. `fetchLangfuseTransport`). Absent ⇒ both
   * tiers degrade honestly (langfuse un-probed / label drift `unknown`).
   */
  langfuse?: LangfuseControlPlaneTransport;
}

/** Case-insensitive-stable ascending sort of `[id, value]` record entries by id (Python `sorted(...)`). */
const byId = <T>(record: Readonly<Record<string, T>>): [string, T][] =>
  Object.entries(record).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));

/**
 * Read-only control-plane projections over one loaded project bundle. Each method mirrors a
 * Python `controlplane/api.py` GET handler and returns its exact response DTO.
 */
export class ProjectControlPlane {
  private readonly project: TypefluxProjectSpec;
  private readonly sources: ProjectBundleSources;
  private readonly manifestPath: string;
  private readonly runtime: ProjectRuntime;
  private readonly capabilities: ApiCapabilities;
  private readonly schemas: Readonly<Record<string, z.ZodType>> | undefined;
  private readonly connectionProbe: ConnectionProbe;
  private readonly langfuse: LangfuseControlPlaneTransport | undefined;

  constructor(bundle: LoadedProjectBundle, options: ProjectControlPlaneOptions = {}) {
    this.project = bundle.project;
    this.sources = bundle.sources;
    this.manifestPath = options.manifestPath ?? "typeflux.project.yaml";
    this.runtime = options.runtime ?? "typescript";
    this.capabilities = options.capabilities ?? OPEN_CAPABILITIES;
    this.schemas = options.schemas;
    this.langfuse = options.langfuse;
    // An explicit `connectionProbe` wins; else derive one from the injected langfuse transport; else
    // the network-free default. So injecting `langfuse` alone lights up BOTH live tiers at once.
    this.connectionProbe =
      options.connectionProbe ??
      (options.langfuse !== undefined ? langfuseConnectionProbe(options.langfuse) : defaultConnectionProbe);
  }

  /** `GET /meta` — project + contract metadata (Python `meta`). */
  meta(): ApiMeta {
    return {
      api_version: API_VERSION,
      bundle_version: BUNDLE_VERSION,
      catalog_version: CATALOG_VERSION,
      project: this.project.name,
      manifest_path: this.manifestPath,
      runtime: this.runtime,
      // Default: no trusted identity (Python `caller_identity` default None). The meta route
      // overrides this under a proxy authorizer; token / open modes keep it null (#577).
      caller_identity: null,
      // Fresh copy per response (Python DTOs are frozen): never hand back the shared
      // OPEN_CAPABILITIES / the caller's stored object, so a mutation of the returned DTO
      // can't leak into later responses (Bugbot).
      capabilities: { ...this.capabilities },
    };
  }

  /** Whether `workflowId` is a declared workflow (Python `_require_workflow` — a route 404s when false). */
  hasWorkflow(workflowId: string): boolean {
    return this.project.workflows.some((workflow) => workflow.id === workflowId);
  }

  /** Whether `environmentId` is a declared environment (Python `_require_environment` — a route 404s when false). */
  hasEnvironment(environmentId: string): boolean {
    return Object.hasOwn(this.project.environments, environmentId);
  }

  /** `GET /workflows` — every declared workflow's manifest entry, in declaration order (Python `workflows`). */
  workflows(): ApiWorkflowList {
    return {
      workflows: this.project.workflows.map((workflow) => ({
        id: workflow.id,
        path: workflow.path ?? null,
        directory: workflow.directory ?? null,
        // Copy (Python `dict(workflow.profiles)`): the DTO must not alias the loaded bundle's
        // manifest data, so mutating a response can't rewrite the project state (Bugbot).
        profiles: { ...workflow.profiles },
      })),
    };
  }

  /** `GET /environments` — every declared environment, sorted by id (Python `environments`). */
  environments(): ApiEnvironmentList {
    return {
      environments: byId(this.project.environments).map(([id, path]) => ({ id, path })),
    };
  }

  /**
   * `GET /environments/{id}` — one environment's full definition: name, variable names, override
   * map, per-workflow profile selections, and the `used_by` reverse index (Python
   * `environment_definition`). An UNKNOWN (undeclared) id is 404; a declared-but-unsourced
   * environment is a 422 config error (raised by the builder).
   */
  environmentDetail(environmentId: string): ApiEnvironmentDefinition {
    if (!Object.hasOwn(this.project.environments, environmentId)) {
      throw new ProjectControlPlaneError(`unknown project environment: ${environmentId}`, 404);
    }
    return buildEnvironmentDefinition(this.project, this.sources, environmentId);
  }

  /**
   * `GET /validate` — the project validation report as the snake_case API DTO (Python `validate` →
   * `validate_project_bundle`, serialized through `ProjectValidationReport`). Reference-only with no
   * selection; a resolved per-workflow bundle when an environment is given.
   *
   * A declared workflow whose source is present but fails to LOAD (bad YAML / a missing `${VAR}`) is
   * a reference-level failure (Python `invalid_workflow_yaml`) — the SDK's `validateProjectBundle`
   * only checks source PRESENCE, so the load happens here. Like Python, such a failure bails the
   * resolved validation (so the report doesn't ALSO carry a resolution failure for the same graph)
   * and forces `ok: false`.
   */
  validate(options: ValidateProjectBundleOptions = {}): ApiProjectValidationReport {
    // The workflow-parse checks (invalid_workflow_yaml / duplicate_workflow_name) and the
    // per-workflow summary live in the SDK validator itself now (#565) — no compensation here.
    // manifestPath threads through so manifest-level issues carry Python's `path` field (#643).
    // Spread LAST: the CP's configured path is authoritative — an options-bag override would
    // decouple issue-level `path` from the report's own manifest identity.
    const report = toApiValidationReport(
      validateProjectBundle(this.project, this.sources, { ...options, manifestPath: this.manifestPath }),
      this.manifestPath,
    );
    // The insight-annotations file (#733) is parsed HERE, not in the SDK validator: that validator
    // works off pre-loaded `sources` and has no filesystem access, whereas the annotations file is
    // read from disk relative to the manifest. A malformed file is an AUTHORING issue on the
    // validation surface (never an enforcement verdict) while the served projection degrades to empty
    // (fail-closed) — Python `_validate_annotations`. Absent is the common case and no issue.
    const annotations = readProjectAnnotations(this.manifestPath);
    if (annotations.error !== undefined) {
      const name = basename(annotations.path);
      report.issues = [
        ...report.issues,
        {
          code: "invalid_annotations_file",
          message: `annotations '${name}' failed to load: ${annotations.error}`,
          reference: name,
          path: annotations.path,
        },
      ];
      report.ok = false;
    }
    return report;
  }

  /**
   * `GET /workflows/{id}/executions` — this workflow's executions, newest first, every status
   * (Python `executions` → `workflow_executions`, #620). The Temporal tier is BOUNDED: a
   * connect failure or timeout answers 503 `TemporalUnavailable`. Guard order matches Python:
   * workflow 404, then environment 404, then the Temporal call.
   */
  async executions(
    workflowId: string,
    environmentId: string,
    options: { limit?: number; clientFactory?: VisibilityClientFactory } = {},
  ): Promise<ApiWorkflowExecutionList> {
    // `resolveWorkflow` now COMPOSES component profiles into the overlay (#568), so a selected
    // runtime profile's `runtime.temporal` is HONORED — the executions listing connects to the
    // profiled cluster. An invalid selection is still the 422 `ProjectProfileError` (from
    // `composeProfiles`); the old runtime-kind 501 is gone (composition is real).
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    return listWorkflowExecutions(spec, {
      workflowId,
      environmentId,
      ...(options.limit !== undefined ? { limit: options.limit } : {}),
      ...(options.clientFactory !== undefined ? { clientFactory: options.clientFactory } : {}),
      subworkflows: this.subworkflowResolverFor(workflowId, environmentId),
    });
  }

  /**
   * `GET /workflows/{id}/versions` — the cross-version drain view (Python `versions` →
   * `workflow_drain_status`, #686). The ts profile's version identity lives in the MEMO, not the
   * type name — see `drain.ts` for the honest translation. Bounded Temporal tier: unreachable →
   * 503 `TemporalUnavailable`. Guard order matches Python: workflow 404, environment 404, then
   * the Temporal call.
   */
  async versions(
    workflowId: string,
    environmentId: string,
    options: { clientFactory?: VisibilityClientFactory } = {},
  ): Promise<ApiWorkflowDrainStatus> {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    return workflowDrainStatus(spec, {
      workflowId,
      environmentId,
      ...(options.clientFactory !== undefined ? { clientFactory: options.clientFactory } : {}),
      subworkflows: this.subworkflowResolverFor(workflowId, environmentId),
    });
  }

  /**
   * `GET /workflows/{id}/workers` — live task-queue poller presence (Python `workers` →
   * `workflow_task_queue_workers`, #686). Degrades IN-BAND on an unreachable cluster
   * (`reachable: false`, Python parity) — only a hang answers the bounded 503.
   */
  async workers(
    workflowId: string,
    environmentId: string,
    options: { taskQueue?: string | undefined; clientFactory?: TaskQueueClientFactory } = {},
  ): Promise<ApiWorkflowTaskQueueWorkers> {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    return workflowTaskQueueWorkers(spec, {
      workflowId,
      environmentId,
      ...(options.taskQueue !== undefined ? { taskQueue: options.taskQueue } : {}),
      ...(options.clientFactory !== undefined ? { clientFactory: options.clientFactory } : {}),
    });
  }

  /**
   * `GET /workflows/{id}/correlation` — observer-aware run-to-trace correlation (Python
   * `correlation` → `workflow_run_correlation`, #686). The trace half is observer-driven (a
   * non-langfuse observer answers the trivial reachable shape; langfuse goes through the
   * injected transport, #573); the `children` half (#55 §9) lists the execution's DIRECT
   * sub-workflow children from the parent-link memo via a bounded visibility scan, degrading
   * to an omitted field + warning when Temporal is unreachable.
   */
  async correlation(
    workflowId: string,
    environmentId: string,
    executionId: string,
    options: { clientFactory?: VisibilityClientFactory } = {},
  ): Promise<ApiWorkflowRunCorrelation> {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    return workflowRunCorrelation(spec, {
      executionId,
      langfuse: this.langfuse,
      ...(options.clientFactory !== undefined ? { clientFactory: options.clientFactory } : {}),
    });
  }

  /**
   * `GET /workflows/{id}/prompt-status` — prompt-registry drift for one workflow under one
   * environment (Python `prompt_status` → `workflow_prompt_status`, #639). Guard order matches
   * Python: unknown workflow 404, then unknown environment 404 (both via `resolveWorkflow`).
   */
  async promptStatus(workflowId: string, environmentId: string): Promise<ApiWorkflowPromptStatus> {
    // Profile composition is real now (#568): `resolveWorkflow` merges a selected registry
    // profile's `runtime.registry` into the overlay, so this projection reads the PROFILED
    // registry (its type, its inline templates) — a selected registry profile HONESTLY changes
    // `registry_type` and the drift rows. An invalid selection is still the 422
    // `ProjectProfileError` raised inside `resolveWorkflow`.
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    return buildWorkflowPromptStatus(spec, workflowId, environmentId, this.langfuse);
  }

  /**
   * `GET /enforcement-events` — the normalized enforcement feed (Python `enforcement_events`, #723):
   * admission verdicts from the read tier's own validation report merged with bounded runtime
   * moderation-block verdicts read from Langfuse through the injected transport seam (#573). The
   * request-surface validation (required scope, verdict allow-set, cursor fingerprint, window) lives
   * in the HTTP adapter; this method takes the already-validated inputs and composes the two sources.
   *
   * `policyIds` here is the caller's FILTER over each event's RECORDED applied policies — it is NOT
   * forwarded into `validate` (which would re-scope how admission verdicts are COMPUTED and fabricate
   * a different outcome than the project actually has); validation always runs under the real policy
   * selection. The runtime source DEGRADES LOUDLY: a non-langfuse observer / absent transport reports
   * `not_configured`, a transport failure reports `unreachable` — the admission events always serve.
   */
  async enforcementEvents(options: {
    environmentId: string;
    workflowIds: readonly string[];
    policyIds: readonly string[];
    verdicts: readonly string[];
    since: Date;
    until: Date;
    limit: number;
    offset: number;
    cursorFingerprint: string;
    readerLimit: number;
  }): Promise<EnforcementEventList> {
    const report = this.validate({
      environmentId: options.environmentId,
      ...(options.workflowIds.length > 0 ? { workflowIds: [...options.workflowIds] } : {}),
    });
    const observer = observerFromReport(report);
    const readResult = await this.readEnforcementRuntime(observer, {
      environmentId: options.environmentId,
      since: options.since,
      until: options.until,
      limit: options.readerLimit,
    });
    return buildEnforcementFeed({
      report,
      readResult,
      environmentId: options.environmentId,
      policyIds: options.policyIds,
      workflowIds: options.workflowIds,
      verdicts: options.verdicts,
      since: options.since,
      until: options.until,
      limit: options.limit,
      offset: options.offset,
      cursorFingerprint: options.cursorFingerprint,
    });
  }

  /**
   * Read the runtime (Langfuse) enforcement source through the injected transport (#573), the ONLY
   * impurity in the feed. Mirrors Python `default_langfuse_enforcement_reader`: runtime events exist
   * only where the observer is Langfuse AND a transport is injected; a query failure is `unreachable`
   * (never a silent empty `ok`), an unconfigured source is `not_configured`. `host` is null so the
   * transport applies its own process-env fallback (Python reads credentials from the serving process
   * env, not the project's env file).
   */
  private async readEnforcementRuntime(
    observer: string | undefined,
    options: { environmentId: string; since: Date; until: Date; limit: number },
  ): Promise<EnforcementReadResult> {
    if (this.langfuse === undefined || observer !== "langfuse") {
      return { status: "not_configured", traces: [] };
    }
    // Python `_langfuse_env_configured`: an observer that IS langfuse but whose credentials are absent
    // answers `not_configured` WITHOUT touching the network — otherwise an empty-credentialed fetch
    // adapter would 401 into a misleading `unreachable`, so the two editions would disagree on the
    // status for the identical misconfiguration. A fixture transport omits the flag (undefined) and
    // is treated as configured, so its stubbed read still drives the `ok`/`unreachable` paths.
    if (this.langfuse.enforcementCredentialsConfigured === false) {
      return { status: "not_configured", traces: [] };
    }
    try {
      const traces = await this.langfuse.searchEnforcementTraces({
        environmentId: options.environmentId,
        since: options.since,
        until: options.until,
        limit: options.limit,
        host: null,
      });
      return { status: "ok", traces };
    } catch {
      return { status: "unreachable", traces: [] };
    }
  }

  /**
   * The resolver contract's `resolve_plan` (#642): the raw `WorkflowPlan` plus the start identity a
   * plan-as-argument dispatch needs (task queue, plan digest for `typeflux_spec_digest`, logical
   * name, version label, opt-in search attribute). Resolution + profile composition run exactly
   * like every other projection (`resolveWorkflow`); the plan derivation is the SAME
   * `workflowPlanFromSpec` the operate tier dispatches, so a control plane starting via this DTO
   * starts the identical plan a TS-edition start would.
   */
  resolvedPlan(workflowId: string, environmentId: string): ApiResolvedPlan {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    let plan;
    try {
      plan = this.planFor(workflowId, environmentId, spec);
    } catch (error) {
      // A resolved spec whose graph fails plan derivation is a CONFIG error (422), like bundle.
      throw new ProjectControlPlaneError(
        `failed to derive the workflow plan for ${environmentId}:${workflowId}: ${errorMessage(error)}`,
        422,
      );
    }
    return {
      plan,
      task_queue: spec.task_queue,
      spec_digest: workflowPlanDigest(plan),
      workflow_name: spec.workflow.name,
      version_label: spec.workflow.version ?? null,
      search_attribute: spec.runtime.temporal.workflow_search_attribute ?? null,
    };
  }

  /**
   * The workflow-level and environment-level profile selections, kept SEPARATE so composition can
   * validate each kind-set in Python's order (workflow first, then environment) before merging
   * per-kind (Python `_resolved_profile_overrides`).
   */
  private profileSelections(
    workflowId: string,
    environmentId: string,
  ): { workflowSelection: ProfileSelection; environmentSelection: ProfileSelection } {
    const workflow = this.project.workflows.find((entry) => entry.id === workflowId);
    const environment = Object.hasOwn(this.sources.environments, environmentId)
      ? this.sources.environments[environmentId]
      : undefined;
    return {
      workflowSelection: { ...(workflow?.profiles ?? {}) },
      environmentSelection: { ...(environment?.workflows[workflowId]?.profiles ?? {}) },
    };
  }

  /**
   * The injected profile source index (Python `project.profiles` + `sources.profiles`): the loaded
   * profile specs keyed kind → id, plus the manifest's per-id reference path for `source_path`
   * provenance. Empty kinds when the manifest declares no profiles.
   */
  private profileIndex(): ProfileSourceIndex {
    const paths = this.project.profiles ?? { provider: {}, registry: {}, runtime: {} };
    return { specs: this.sources.profiles, paths };
  }

  /**
   * Compose the effective component-profile overrides + provenance for a workflow/environment
   * (Python `_resolved_profile_overrides`). The overrides slot between the workflow YAML and the
   * environment overrides in {@link resolveWorkflow}; the provenance feeds the bundle's
   * `components`. Wraps `ProjectProfileError` (unknown kind / undeclared id) as a 422 with the
   * matching discriminant so a broken selection fails identically on both editions.
   */
  private composeProfiles(
    workflowId: string,
    environmentId: string,
  ): { overrides: Record<string, unknown>; provenance: AppliedComponentProfile[] } {
    const { workflowSelection, environmentSelection } = this.profileSelections(workflowId, environmentId);
    const environmentName = this.sources.environments[environmentId]?.name ?? environmentId;
    try {
      return composeProfileOverrides(this.profileIndex(), {
        workflowSelection,
        environmentSelection,
        workflowContext: `workflow '${workflowId}' profile selection`,
        environmentContext: `environment '${environmentName}' profile selection for '${workflowId}'`,
      });
    } catch (error) {
      if (error instanceof ProjectProfileError) {
        throw new ProjectControlPlaneError(error.message, 422, "ProjectProfileError");
      }
      throw error;
    }
  }

  /**
   * `GET /workflows/{id}/bundle` (topology projection) — the resolved workflow's structure as a
   * nodes+edges DAG (Python `_bundle_topology`). The full `ResolvedWorkflowBundle` (identity, policy,
   * activities, runtime, links, …) layers on top in later slices.
   */
  bundleTopology(workflowId: string, environmentId: string): ApiBundleTopology {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    try {
      // `workflowPlanFromSpec` (inside the builder) rejects a bad graph AFTER resolution — a
      // reserved/duplicate step id or an invalid review route. Wrap it as a caller-facing 400 so
      // the HTTP adapter maps a bad project config to a client error, not a 500 (codex).
      return buildBundleTopology(spec, this.planFor(workflowId, environmentId, spec));
    } catch (error) {
      // A bad graph after a clean resolution is a CONFIG/validation failure — 422, matching the
      // Python contract (a `TypefluxError`/`ValueError` maps to 422), not a 400/500 (codex).
      throw new ProjectControlPlaneError(
        `failed to build topology for ${environmentId}:${workflowId}: ${errorMessage(error)}`,
        422,
      );
    }
  }

  /**
   * `GET /workflows/{id}/catalog` — the resolved workflow's activity catalog (Python
   * `resolve_activity_catalog`): schema identities + JSON Schemas (from the injected `schemas`),
   * kinds, `used_by_steps`, and `compatible_next`.
   */
  activityCatalog(workflowId: string, environmentId: string): ApiActivityCatalog {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    try {
      return buildActivityCatalog(
        spec,
        this.project.name,
        workflowId,
        environmentId,
        this.schemas,
        this.subworkflowResolverFor(workflowId, environmentId),
      );
    } catch (error) {
      throw new ProjectControlPlaneError(
        `failed to build catalog for ${environmentId}:${workflowId}: ${errorMessage(error)}`,
        422,
      );
    }
  }

  /**
   * `GET /workflows/{id}/connections` — the resolved workflow's registry + observability connection
   * status (Python `workflow_connections`): backend types + hosts (never credentials) and reachability
   * from the injected `connectionProbe` (the default is network-free — see the option's doc). Async: a
   * real probe is a live network call, so an unknown id (404) / bad config (422) surfaces as a rejection.
   */
  async connections(workflowId: string, environmentId: string): Promise<ApiWorkflowConnections> {
    // Composition is real (#568): a selected registry / runtime profile's `runtime.registry` /
    // `runtime.observability`/`runtime.temporal` is HONORED in the overlay, so this projection
    // reports the PROFILED backends' status. An invalid selection stays the 422
    // `ProjectProfileError` raised inside `resolveWorkflow`.
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    return buildWorkflowConnections(spec, workflowId, environmentId, this.connectionProbe);
  }

  /**
   * `GET /policies` — every declared policy as a summary, sorted by id (Python `policy_definitions`).
   * The `/policies` route has no `exclude_none`, so each entry's `description` is always present.
   */
  policies(): ApiPolicySummary[] {
    return buildPolicySummaries(this.project, this.sources);
  }

  /**
   * `GET /annotations` — the in-repo insight-acknowledgement projection (#733; Python
   * `load_project_annotations`): the parsed `.typeflux/annotations.yaml` beside the manifest. A
   * pure-YAML, project-level read — NOT resolution-bound and NO capability flag (always servable,
   * like /workflows and /policies). Parsed fail-closed: absent → empty (the common case), and a
   * malformed file degrades to an EMPTY projection here while surfacing as a validation issue on
   * /validate (never a partial parse).
   */
  annotations(): ApiProjectAnnotations {
    return loadProjectAnnotations(this.manifestPath);
  }

  /**
   * `GET /policies/{id}` — a policy's full definition: rules, `extends`, composed `policy_hash`, and
   * the workflows it governs (Python `policy_definition`). An UNKNOWN (undeclared) id is 404; a
   * declared-but-unsourced policy is a 422 config error (raised by the builder).
   */
  policyDetail(policyId: string): ApiPolicyDefinition {
    if (!Object.hasOwn(this.project.policies, policyId)) {
      throw new ProjectControlPlaneError(`unknown project policy: ${policyId}`, 404);
    }
    return buildPolicyDefinition(this.project, this.sources, policyId);
  }

  /**
   * `GET /profiles` — every declared component profile as a summary, kinds in fixed
   * provider → registry → runtime order, ids sorted within each kind (Python
   * `profile_definitions`). Bare array; a declared-but-unsourced profile is a 422 config error.
   */
  profiles(): ApiProfileSummary[] {
    return buildProfileSummaries(this.project, this.sources);
  }

  /**
   * `GET /profiles/{kind}/{profile_id}` — a profile's loaded content (`runtime` fragment +
   * cross-SDK `content_hash`) and the workflows/environments selecting it (Python
   * `profile_definition`). Python's single combined check makes an unknown kind OR an
   * undeclared id the same 404; unsourced/kind-mismatched content is 422 (from the builder).
   */
  profileDetail(kind: string, profileId: string): ApiProfileDefinition {
    const declared =
      this.project.profiles !== undefined && (PROFILE_KINDS as readonly string[]).includes(kind)
        ? this.project.profiles[kind as ProfileKind]
        : undefined;
    if (declared === undefined || !Object.hasOwn(declared, profileId)) {
      throw new ProjectControlPlaneError(`unknown project profile: ${kind}/${profileId}`, 404);
    }
    return buildProfileDefinition(this.project, this.sources, kind as ProfileKind, profileId);
  }

  /**
   * `GET /workflows/{id}/bundle` — the resolved workflow BUNDLE core (Python `resolve_workflow_bundle`):
   * project + environment + frozen identity + secret-safe runtime summary + composed policy + topology
   * + lifecycle + validation. An UNKNOWN id → 404; a bad config (missing schema / bad graph) → 422.
   * `activities`/`steps` and the absent-subsystem fields are deferred (see the module SCOPE note).
   */
  bundle(
    workflowId: string,
    environmentId: string,
    policyIds: readonly string[] = [],
    deploymentImage?: string,
  ): ApiResolvedWorkflowBundle {
    // `resolveWorkflow` composes the selected component profiles (#568) — an undeclared selection
    // is the 422 `ProjectProfileError` (Python parity) raised here, and the resolved spec + the
    // applied-profile provenance (the bundle's `components`) come back together.
    const { spec, components } = this.resolveWorkflow(workflowId, environmentId);
    // resolveWorkflow succeeded → the workflow is declared and the environment is sourced.
    const workflow = this.project.workflows.find((entry) => entry.id === workflowId);
    const environment = Object.hasOwn(this.sources.environments, environmentId)
      ? this.sources.environments[environmentId]
      : undefined;
    if (workflow === undefined || environment === undefined) {
      throw new ProjectControlPlaneError(`unresolved bundle context for ${environmentId}:${workflowId}`, 422);
    }
    // Deployment preview (#687): a supplied image builds the secret-free worker preview here — the
    // control plane owns the sibling resolver the closure admission needs. A generation/admission
    // failure is advisory (`{error}`), but a policy COMPOSITION error (unknown/broken policy id) is
    // NOT swallowed — Python's `_deployment_preview` lets `ProjectPolicyError` propagate to a 422
    // for the whole bundle (only `ProjectDeploymentError` is caught there).
    let deploymentPreview: Record<string, unknown> | undefined;
    if (deploymentImage !== undefined) {
      try {
        deploymentPreview = this.buildDeploymentPreview(workflowId, environmentId, policyIds, spec, deploymentImage);
      } catch (error) {
        if (error instanceof ProjectPolicyError) {
          throw new ProjectControlPlaneError(error.message, 422, "ProjectPolicyError");
        }
        throw error;
      }
    }
    try {
      return buildResolvedWorkflowBundle({
        project: this.project,
        sources: this.sources,
        spec,
        environment,
        components,
        workflowId,
        environmentId,
        environmentName: environment.name,
        workflowPath: workflow.path ?? `${workflow.directory}/${this.project.defaults.workflow_filename}`,
        profilePath: this.project.environments[environmentId] ?? "",
        manifestPath: this.manifestPath,
        policyIds,
        schemas: this.schemas,
        subworkflows: this.subworkflowResolverFor(workflowId, environmentId),
        ...(deploymentImage !== undefined ? { deploymentImage } : {}),
        ...(deploymentPreview !== undefined ? { deploymentPreview } : {}),
      });
    } catch (error) {
      if (error instanceof ProjectControlPlaneError) throw error;
      throw new ProjectControlPlaneError(
        `failed to build bundle for ${environmentId}:${workflowId}: ${errorMessage(error)}`,
        422,
      );
    }
  }

  /**
   * Build the secret-free deployment preview for one workflow (Python `_deployment_preview`). Uses
   * the SAME worker-plan builder + admission the CLI runs, with `allowMutableImage` (a preview is
   * advisory), and projects the worker descriptors (config KEYS only — never values). A generation
   * failure (bad image, failed admission, unclassified env) is swallowed into `{error}` so the
   * bundle never 422s on the preview; advisory notices ride along either way.
   */
  private buildDeploymentPreview(
    workflowId: string,
    environmentId: string,
    policyIds: readonly string[],
    spec: TypefluxYamlSpec,
    deploymentImage: string,
  ): Record<string, unknown> {
    const notices = this.deploymentPreviewNotices(spec);
    const noticesField = notices.length > 0 ? { notices } : {};
    const resolveWorkflow = (id: string): DeploymentResolvedWorkflow | undefined => {
      if (!this.project.workflows.some((entry) => entry.id === id)) return undefined;
      let resolvedSpec: TypefluxYamlSpec;
      try {
        resolvedSpec = this.resolveWorkflow(id, environmentId).spec;
      } catch {
        return undefined;
      }
      const workflow = this.project.workflows.find((entry) => entry.id === id)!;
      const environment = this.sources.environments[environmentId];
      if (environment === undefined) return undefined;
      return {
        spec: resolvedSpec,
        environmentName: environment.name,
        workflowPath: workflow.path ?? `${workflow.directory}/${this.project.defaults.workflow_filename}`,
        variables: environment.variables,
      };
    };
    let plan;
    try {
      plan = buildProjectDeploymentPlan(
        this.project,
        this.sources,
        { environmentId, workflowIds: [workflowId], policyIds, image: deploymentImage, allowMutableImage: true },
        resolveWorkflow,
      );
    } catch (error) {
      // ADMISSION failures (a failed compliance/closure check → ProjectPolicyEnforcementError) and
      // GENERATION failures (bad image, unclassified env → ProjectDeploymentError) are advisory: the
      // validation section already carries admission detail, so the preview records why it could not
      // be generated. A COMPOSITION error (unknown/broken policy id → the base ProjectPolicyError)
      // is NOT swallowed — it propagates so the whole bundle 422s (Python `_deployment_preview`
      // catches only ProjectDeploymentError). ProjectPolicyEnforcementError is checked first because
      // it subclasses ProjectPolicyError.
      if (error instanceof ProjectDeploymentError || error instanceof ProjectPolicyEnforcementError) {
        return { error: error.message, ...noticesField };
      }
      throw error;
    }
    return {
      target: plan.target,
      environment_id: plan.environment_id,
      image: plan.image,
      image_digest_pinned: plan.image_digest_pinned,
      workers: plan.workers.map((worker) => ({
        name: worker.name,
        workflow_id: worker.workflow_id,
        workflow_name: worker.workflow_name,
        task_queue: worker.task_queue,
        config_map_name: worker.config_map_name,
        // Config VALUES stay out of the bundle; the deploy command is the authoritative generator.
        config_map_keys: Object.keys(worker.config_map).sort(),
        secret_name: worker.secret_name,
        secret_env: worker.secret_env,
        secret_files: worker.secret_files,
        policy: worker.policy,
      })),
      ...noticesField,
    };
  }

  /** Advisory deployment-preview notices (Python `_deployment_preview_notices`): the sub-workflow
   * visibility caveat when a frozen `workflow.version` lacks a configured search attribute. */
  private deploymentPreviewNotices(spec: TypefluxYamlSpec): string[] {
    if (
      specReferencesSubworkflows(spec.workflow.steps) &&
      spec.workflow.version !== undefined &&
      spec.runtime.temporal.workflow_search_attribute === undefined
    ) {
      return [
        "workflow.version is declared but runtime.temporal.workflow_search_attribute is not " +
          "configured; wide sub-workflow fan-outs degrade the frozen-version scan — configure " +
          "the search attribute (#55)",
      ];
    }
    return [];
  }

  /**
   * `GET /deployments` — every file under the project's `deployments/` dir, each valid plan
   * live-verified against the current resolution (Python `deployments_list` +
   * `_deployment_entry`). PER-PLAN DEGRADE, never a listing-wide failure: a plan whose workflow
   * no longer resolves records a failed verification (`resolution` mismatch), and a MALFORMED /
   * TAMPERED file surfaces as an error entry (`parse` mismatch naming the file + load error,
   * placeholder plan) rather than silently vanishing — an operator must see a corrupt plan file,
   * not a shorter listing. `promote_command` is the exact `typeflux-project deploy` invocation
   * an operator runs (the plan is authoritative; components are shell-quoted).
   */
  deployments(): ApiDeploymentEntry[] {
    return readDeploymentPlanDir(this.planDir()).map((entry) =>
      entry.plan !== undefined
        ? this.deploymentEntry(entry.plan)
        : this.errorDeploymentEntry(entry.file, entry.error ?? "unreadable deployment plan file"),
    );
  }

  /** `GET /deployments/{plan_id}` — one plan's entry (error entries match by filename stem), or a
   * 404 for an unknown id (Python `deployments_detail`). */
  deployment(planId: string): ApiDeploymentEntry {
    for (const entry of readDeploymentPlanDir(this.planDir())) {
      if (entry.plan !== undefined) {
        if (deploymentPlanId(entry.plan) === planId) return this.deploymentEntry(entry.plan);
      } else if (entry.file.replace(/\.yaml$/, "") === planId) {
        return this.errorDeploymentEntry(entry.file, entry.error ?? "unreadable deployment plan file");
      }
    }
    throw new ProjectControlPlaneError(`unknown deployment plan: ${planId}`, 404);
  }

  /**
   * The approved deployment plans as github-provenance refs (#727; Python `github_provenance`'s plan
   * loop), newest-first by `generated_at` — the (plan id, manifest-relative path) of every VALID
   * plan. Malformed/error entries carry no plan and are SKIPPED here (they still surface in
   * `/deployments`, not the provenance feed). The caller resolves each plan file's commit sha via the
   * registry; on this edition that is always null (no clone / git subprocess), so plans list with
   * `sha`/`pr` null.
   */
  githubPlanFiles(): Array<{ planId: string; path: string }> {
    return readDeploymentPlanDir(this.planDir())
      .flatMap((entry) => (entry.plan !== undefined ? [{ file: entry.file, plan: entry.plan }] : []))
      .sort((a, b) =>
        a.plan.generated_at < b.plan.generated_at ? 1 : a.plan.generated_at > b.plan.generated_at ? -1 : 0,
      )
      .map(({ file, plan }) => ({
        planId: deploymentPlanId(plan),
        // The entry's ACTUAL on-disk filename — Python passes `deployments/{file}` into
        // plan_file_sha, so a plan stored under a non-{planId}.yaml basename must resolve
        // the same commit on both editions (Bugbot).
        path: `${PLAN_DIR_NAME}/${file}`,
      }));
  }

  /** The project's `deployments/` directory (manifest-relative; Python `_plan_dir`). */
  private planDir(): string {
    return join(dirname(this.manifestPath), PLAN_DIR_NAME);
  }

  /** A `DeploymentPlanResolver` over the CP's own resolution (reuses `resolveWorkflow` + the sibling resolver). */
  private deploymentPlanResolver(): DeploymentPlanResolver {
    return (workflowId, environmentId) => {
      if (!this.hasWorkflow(workflowId) || !this.hasEnvironment(environmentId)) return undefined;
      let spec: TypefluxYamlSpec;
      try {
        spec = this.resolveWorkflow(workflowId, environmentId).spec;
      } catch {
        return undefined;
      }
      return { spec, subworkflows: this.subworkflowResolverFor(workflowId, environmentId) };
    };
  }

  private deploymentEntry(plan: DeploymentPlan): ApiDeploymentEntry {
    const planId = deploymentPlanId(plan);
    let verification: PlanVerification;
    try {
      verification = verifyDeploymentPlan(this.project, this.sources, this.deploymentPlanResolver(), plan);
    } catch (error) {
      // A plan whose target no longer resolves (deleted workflow/environment) is drift, not a
      // server fault — record it as a failed verification so the listing stays available.
      verification = {
        ok: false,
        mismatches: [{ path: "resolution", plan_value: planId, current_value: errorMessage(error) }],
      };
    }
    return {
      plan_id: planId,
      plan,
      verification,
      path: `${PLAN_DIR_NAME}/${planId}.yaml`,
      // The plan is authoritative — env, workflow, and image come from the file — so promotion
      // needs only the manifest and the plan path (the TS bin is `typeflux-project`, D687-2).
      promote_command: this.promoteCommand(`${PLAN_DIR_NAME}/${planId}.yaml`),
    };
  }

  /** An entry for a malformed/tampered plan file: the filename + load error, a zeroed placeholder
   * plan (the contract requires one), and a failed verification — never a silent omission. */
  private errorDeploymentEntry(file: string, error: string): ApiDeploymentEntry {
    return {
      plan_id: file.replace(/\.yaml$/, ""),
      plan: {
        plan_version: "1",
        plan_hash: "",
        generated_at: "",
        identity: {
          workflow_id: "",
          workflow_name: "",
          workflow_type: "",
          spec_digest: "",
          spec_digest_algorithm: "",
          environment_id: "",
          code: null,
        },
        policy: { selected_policy_ids: [], applied_policy_ids: [], policy_hash: "" },
        deployment: { image: "", image_digest_pinned: false, preflight: { ok: false, issue_codes: [] } },
      },
      verification: { ok: false, mismatches: [{ path: "parse", plan_value: file, current_value: error }] },
      path: `${PLAN_DIR_NAME}/${file}`,
      promote_command: this.promoteCommand(`${PLAN_DIR_NAME}/${file}`),
    };
  }

  /** The copyable promote invocation, SHELL-QUOTED (Python `shlex.quote`) — defense in depth on
   * top of the loader's id-charset validation, so a tampered plan/filename can never turn the
   * console's copy-paste command into an injection vector. */
  private promoteCommand(planPath: string): string {
    return `typeflux-project deploy ${shellQuote(this.manifestPath)} --apply ${shellQuote(planPath)}`;
  }

  /**
   * The operate tier's shared prelude (Python `api.py` `start`/`status`/`review`/`cancel`): resolve
   * the workflow under the environment (404s an unknown workflow/environment, 422s a broken spec),
   * COMPOSING component profiles into the overlay (#568) — a selected runtime profile's
   * `runtime.temporal`/`runtime.observability` is now HONORED, so the operate tier talks to the
   * PROFILED cluster. An invalid selection stays the 422 `ProjectProfileError` raised in
   * `resolveWorkflow`. The POLICY gate (#663) then composes and admits the selected policies —
   * see `enforcePolicySelection` — so a policy failure (422) always precedes the Temporal tier.
   */
  private operateSpec(
    workflowId: string,
    environmentId: string,
    policyIds: readonly string[] | undefined,
    expectedPolicyHash: string | null | undefined,
  ): TypefluxYamlSpec {
    const { spec } = this.resolveWorkflow(workflowId, environmentId);
    // The policy gate lives IN the shared prelude (not per method) so a future operate method
    // cannot silently skip it — every operate op resolves through operateSpec.
    this.enforcePolicySelection(spec, workflowId, environmentId, policyIds, expectedPolicyHash);
    return spec;
  }

  /**
   * `POST /workflows/{id}/start` — start one plan-as-argument execution (Python `start`). Guard
   * order matches the route: workflow 404 / environment 404 via `resolveWorkflow`, the runtime-kind
   * profile gate, then the bounded Temporal start (→ 503 on unreachable). Every call builds a fresh
   * `WorkflowOperations` (per-request freshness — the TS architecture pins no runtime), so
   * `runtime_pin` is always null in status, honestly (there is nothing pinned to report).
   *
   * POLICY (#663): the shared prelude composes the caller's `policyIds` (or the project's
   * target-derived selection), runs admission compliance against the resolved spec, and verifies
   * `expectedPolicyHash` — all BEFORE the Temporal tier, failing closed with 422
   * `ProjectPolicyEnforcementError`. Per-call guard hooks are the worker's half
   * (`assembleYamlRuntime`), exactly Python's CP/worker split.
   */
  async start(
    workflowId: string,
    environmentId: string,
    executionId: string,
    input: Record<string, unknown>,
    options: {
      taskQueue?: string | null;
      policyIds?: readonly string[];
      expectedPolicyHash?: string | null;
      clientFactory?: OperationsClientFactory;
    } = {},
  ): Promise<ApiWorkflowStartReceipt> {
    return this.withOperations(workflowId, environmentId, options, (operations, spec) => {
      // Python validates the payload against the workflow's input model BEFORE any Temporal
      // call (`_coerce_input` → 422 "invalid workflow input for <Model>: …") and dispatches the
      // COERCED value, so Zod defaults/strips apply exactly like pydantic's (codex). The input
      // schema comes from the injected-schemas seam; a missing schema fails closed like bundle.
      const ref = spec.workflow.input;
      const schema = this.schemas !== undefined && Object.hasOwn(this.schemas, ref) ? this.schemas[ref] : undefined;
      if (schema === undefined) {
        throw new ProjectControlPlaneError(
          `starting requires an injected schema for the workflow input ref '${ref}' (none was supplied)`,
          422,
        );
      }
      const parsed = schema.safeParse(input);
      if (!parsed.success) {
        throw new ProjectControlPlaneError(
          `invalid workflow input for ${schemaLogicalName(ref)}: ${parsed.error.message}`,
          422,
        );
      }
      return operations.start(parsed.data as Record<string, unknown>, {
        workflowId: executionId,
        ...(options.taskQueue !== undefined ? { taskQueue: options.taskQueue } : {}),
      });
    });
  }

  /**
   * `GET /workflows/{id}/status` — memo-verified lifecycle status (Python `status`). Unknown
   * workflow/environment 404, runtime-kind profile gate, memo mismatch 409, unreachable 503.
   * `runtime_pin` is null (the TS architecture pins no runtime — honest, never fabricated).
   */
  async status(
    workflowId: string,
    environmentId: string,
    executionId: string,
    options: {
      runId?: string | null;
      policyIds?: readonly string[];
      expectedPolicyHash?: string | null;
      clientFactory?: OperationsClientFactory;
    } = {},
  ): Promise<ApiWorkflowOperationStatus> {
    return this.withOperations(workflowId, environmentId, options, (operations) =>
      operations.status(executionId, {
        ...(options.runId !== undefined ? { runId: options.runId } : {}),
      }),
    );
  }

  /**
   * `POST /workflows/{id}/review` — route a waiting review checkpoint (Python `review`). Same guard
   * order + memo verification as status; the handler answers 204 on success.
   */
  async submitReview(
    workflowId: string,
    environmentId: string,
    executionId: string,
    command: ReviewCommand,
    options: {
      runId?: string | null;
      policyIds?: readonly string[];
      expectedPolicyHash?: string | null;
      clientFactory?: OperationsClientFactory;
    } = {},
  ): Promise<void> {
    await this.withOperations(workflowId, environmentId, options, (operations) =>
      operations.submitReview(executionId, command, {
        ...(options.runId !== undefined ? { runId: options.runId } : {}),
      }),
    );
  }

  /**
   * `POST /workflows/{id}/cancel` — request graceful cancellation (Python `cancel`). Same guard
   * order + memo verification as status; the handler answers 204 on success.
   */
  async requestCancel(
    workflowId: string,
    environmentId: string,
    executionId: string,
    reason: string | null | undefined,
    options: {
      runId?: string | null;
      policyIds?: readonly string[];
      expectedPolicyHash?: string | null;
      clientFactory?: OperationsClientFactory;
    } = {},
  ): Promise<void> {
    await this.withOperations(workflowId, environmentId, options, (operations) =>
      operations.requestCancel(executionId, reason, {
        ...(options.runId !== undefined ? { runId: options.runId } : {}),
      }),
    );
  }

  /**
   * `POST /workflows/{id}/migrate` — terminate-and-resubmit across graph versions (Python
   * `migrate`, #204), addressed exactly like cancel/review (execution id + optional run id;
   * omitted run id targets the current run). Same guard order + policy gate as the other operate
   * ops; the old execution is verified for project + logical workflow and its version REQUIRED to
   * differ. Refuses a same-version migrate, no serving workers, and (unless `abandonGates`) an
   * open gate — all preflighted BEFORE the terminate, including the carried-input validation
   * against the CURRENT version's injected input schema (fail-closed when the schema is missing,
   * the start path's exact posture).
   */
  async migrate(
    workflowId: string,
    environmentId: string,
    executionId: string,
    options: {
      runId?: string | null;
      abandonGates?: boolean;
      reason?: string | null;
      dryRun?: boolean;
      policyIds?: readonly string[];
      expectedPolicyHash?: string | null;
      clientFactory?: OperationsClientFactory;
    } = {},
  ): Promise<ApiWorkflowMigrateResult> {
    return this.withOperations(workflowId, environmentId, options, (operations, spec) => {
      // The carried-input preflight (#204 review): validated against the CURRENT version's input
      // schema BEFORE the terminate — a missing schema fails closed here, never mid-migrate, and
      // the coerced value (zod defaults/strips) is what gets dispatched, like start.
      const ref = spec.workflow.input;
      const schema =
        this.schemas !== undefined && Object.hasOwn(this.schemas, ref) ? this.schemas[ref] : undefined;
      if (schema === undefined) {
        throw new ProjectControlPlaneError(
          `migrating requires an injected schema for the workflow input ref '${ref}' to validate ` +
            "the carried-over input against the current version (none was supplied)",
          422,
        );
      }
      return operations.migrate(executionId, {
        ...(options.runId !== undefined ? { runId: options.runId } : {}),
        ...(options.abandonGates !== undefined ? { abandonGates: options.abandonGates } : {}),
        ...(options.reason !== undefined ? { reason: options.reason } : {}),
        ...(options.dryRun !== undefined ? { dryRun: options.dryRun } : {}),
        validateCarriedInput: (input) => {
          const parsed = schema.safeParse(input);
          if (!parsed.success) {
            throw new ProjectControlPlaneError(
              "migrate refused: the carried-over input is not valid for the current version's " +
                `input schema ${schemaLogicalName(ref)}: ${parsed.error.message}`,
              422,
            );
          }
          return parsed.data;
        },
      });
    });
  }

  /**
   * The shared operate-op wrapper: run the runtime-kind + policy gates (`operateSpec`), build a
   * fresh `WorkflowOperations` (per-request freshness — the TS architecture pins no runtime), run
   * `fn`, and shut the client down on every settled path (an abandoned in-flight connect self-closes via the shutdown flag). One place holds the construct/shutdown contract so the
   * four operate methods cannot drift.
   */
  private async withOperations<T>(
    workflowId: string,
    environmentId: string,
    options: {
      policyIds?: readonly string[];
      expectedPolicyHash?: string | null;
      clientFactory?: OperationsClientFactory;
    },
    fn: (operations: WorkflowOperations, spec: TypefluxYamlSpec) => Promise<T>,
  ): Promise<T> {
    const spec = this.operateSpec(workflowId, environmentId, options.policyIds, options.expectedPolicyHash);
    const operations = new WorkflowOperations(spec, {
      ...(options.clientFactory !== undefined ? { clientFactory: options.clientFactory } : {}),
      subworkflows: this.subworkflowResolverFor(workflowId, environmentId),
    });
    try {
      return await fn(operations, spec);
    } finally {
      operations.shutdown();
    }
  }

  /**
   * Compose + admit the operate call's policy selection (#663; Python
   * `PythonVersionedTypeDriver.for_project_workflow` → `build_project_policy_runtime_guard` +
   * `_verify_expected_policy_hash`). Explicit `policyIds` win outright; otherwise the project's
   * `validation.targets` select. Admission compliance runs against the RESOLVED (profiled) spec and
   * fails closed; the expected-hash check then pins the composed closure. Per-call enforcement is
   * the worker's half — `assembleYamlRuntime` wires the same guard's hooks into the executor — so
   * the control plane's share is exactly the admission gate, mirroring Python's split across the
   * CP/worker seam. Every policy failure maps to Python's contract shape: 422
   * `ProjectPolicyEnforcementError` (a `ValueError` under Python's generic `TypefluxError` handler).
   */
  private enforcePolicySelection(
    spec: TypefluxYamlSpec,
    workflowId: string,
    environmentId: string,
    policyIds: readonly string[] | undefined,
    expectedPolicyHash: string | null | undefined,
  ): void {
    let policyHash: string | undefined;
    try {
      const guard = buildProjectPolicyRuntimeGuard(this.project, this.sources, {
        spec,
        workflowId,
        environmentId,
        explicitPolicyIds: policyIds ?? [],
        // Transitive-closure admission (#55 §9): re-validate referenced sub-workflows against the
        // parent's composed policy at the deploy gate too, so the TS CP fails closed on a
        // non-compliant child exactly as Python's `build_project_policy_runtime_guard` does. A
        // declared-but-unresolvable sibling returns undefined here → the closure check records it
        // and fails closed (never an uncaught 500).
        resolveSubworkflowSpec: (siblingId) => {
          if (!this.project.workflows.some((workflow) => workflow.id === siblingId)) return undefined;
          try {
            return this.resolveWorkflow(siblingId, environmentId).spec;
          } catch {
            return undefined;
          }
        },
      });
      policyHash = guard?.policy.policyHash;
    } catch (error) {
      // Composition errors (unknown policy id, broken `extends` closure) and failed admission
      // checks are both 422s in Python (`ValueError`-rooted); keep the source message verbatim.
      if (error instanceof ProjectPolicyEnforcementError) {
        throw new ProjectControlPlaneError(error.message, 422, "ProjectPolicyEnforcementError");
      }
      if (error instanceof ProjectPolicyError) {
        throw new ProjectControlPlaneError(error.message, 422, "ProjectPolicyError");
      }
      throw error;
    }
    // Python `_verify_expected_policy_hash`: absent/null expected hash skips; a provided hash
    // demands a selected policy AND an exact match, message parity included.
    if (expectedPolicyHash === undefined || expectedPolicyHash === null) return;
    if (policyHash === undefined) {
      throw new ProjectControlPlaneError(
        "expected project policy hash was provided, but no project policy was selected",
        422,
        "ProjectPolicyEnforcementError",
      );
    }
    if (policyHash !== expectedPolicyHash) {
      throw new ProjectControlPlaneError(
        "selected project policy hash does not match expected deployment policy hash " +
          `(expected=${expectedPolicyHash}, actual=${policyHash})`,
        422,
        "ProjectPolicyEnforcementError",
      );
    }
  }

  /**
   * Resolve one workflow under an environment (Python `resolve_project_workflow`, the CP path),
   * COMPOSING the selected component profiles into the overlay (Python `_resolved_profile_overrides`
   * → `_deep_merge(profile_overrides, environment_overrides)`). Returns the resolved spec plus the
   * applied-profile provenance (the bundle's `components`). Status mapping follows the Python
   * contract: a genuinely UNKNOWN (undeclared) workflow or environment is 404 (NotFound); a
   * declared-but-unsourced source or a spec that fails to resolve is a CONFIGURATION error → 422;
   * an undeclared profile selection is a 422 `ProjectProfileError` (from `composeProfiles`).
   */
  /**
   * The sub-workflow resolver for one workflow under one environment (#55 §3.4): `specFor(id)`
   * returns a sibling's RESOLVED spec — composed under the SAME environment, so a parent and its
   * children share one deployment target — or `undefined` when the id is not a declared project
   * workflow (an undeclared reference then surfaces as the precise plan-derivation error, not a
   * silent skip). `selfId` seeds reference-cycle detection. Same-project by construction, so
   * cross-runtime composition is structurally excluded (a project resolves under one runtime).
   */
  private subworkflowResolverFor(selfWorkflowId: string, environmentId: string): SubworkflowSpecResolver {
    return projectSubworkflowResolver(selfWorkflowId, (siblingId) => {
      if (!this.project.workflows.some((workflow) => workflow.id === siblingId)) {
        return undefined;
      }
      return this.resolveWorkflow(siblingId, environmentId).spec;
    });
  }

  /** Derive a workflow's plan with its sub-workflow references resolved+embedded (#55 §3.4/§6). */
  private planFor(workflowId: string, environmentId: string, spec: TypefluxYamlSpec): WorkflowPlan {
    return workflowPlanFromSpec(spec, {
      subworkflows: this.subworkflowResolverFor(workflowId, environmentId),
    });
  }

  private resolveWorkflow(
    workflowId: string,
    environmentId: string,
  ): { spec: TypefluxYamlSpec; components: AppliedComponentProfile[] } {
    if (!this.project.workflows.some((workflow) => workflow.id === workflowId)) {
      throw new ProjectControlPlaneError(`unknown project workflow: ${workflowId}`, 404);
    }
    if (!Object.hasOwn(this.project.environments, environmentId)) {
      throw new ProjectControlPlaneError(`unknown project environment: ${environmentId}`, 404);
    }
    const environment = Object.hasOwn(this.sources.environments, environmentId)
      ? this.sources.environments[environmentId]
      : undefined;
    const workflowText = Object.hasOwn(this.sources.workflows, workflowId) ? this.sources.workflows[workflowId] : undefined;
    if (environment === undefined || workflowText === undefined) {
      const missing = environment === undefined ? `environment '${environmentId}'` : `workflow '${workflowId}'`;
      throw new ProjectControlPlaneError(`no source provided for ${missing}`, 422);
    }
    // Compose profiles FIRST so an undeclared selection fails closed (422 ProjectProfileError)
    // before we touch the loader — and so the resolved overlay actually carries the profile
    // fragment (Python composes then merges). This is what makes the profile-honoring reads real.
    const { overrides: profileOverrides, provenance } = this.composeProfiles(workflowId, environmentId);
    try {
      const spec = resolveEnvironmentWorkflow(workflowText, {
        environment,
        workflowId,
        runtimeDefaults: this.project.defaults.runtime,
        profileOverrides,
        sourceLabel: `${environmentId}:${workflowId}`,
      });
      return { spec, components: provenance };
    } catch (error) {
      if (error instanceof ProjectControlPlaneError) throw error;
      throw new ProjectControlPlaneError(`failed to resolve ${environmentId}:${workflowId}: ${errorMessage(error)}`, 422);
    }
  }

}
