// Control-plane operations core (#563, slice 1): pure read/validate projections over a loaded bundle.
export {
  API_VERSION,
  BUNDLE_VERSION,
  CATALOG_VERSION,
  openCapabilities,
  ProjectControlPlane,
} from "./project-control-plane.js";
export type {
  ApiCapabilities,
  ApiEnvironmentList,
  ApiEnvironmentSummary,
  ApiMeta,
  ApiResolvedPlan,
  ApiWorkflowList,
  ApiWorkflowSummary,
  ProjectControlPlaneOptions,
  ProjectRuntime,
} from "./project-control-plane.js";
// The environment definition projection (#620): full overlay detail + used_by reverse index.
export { buildEnvironmentDefinition } from "./environment-definitions.js";
export type { ApiEnvironmentDefinition } from "./environment-definitions.js";
// The `/validate` response DTO (#563 slice 2): snake_case translation of the SDK's report.
export { toApiValidationReport } from "./validation-dto.js";
export type {
  ApiProjectValidationReport,
  ApiResolvedWorkflowValidation,
  ApiValidationCheck,
  ApiValidationIssue,
  ApiValidationWorkflowSummary,
} from "./validation-dto.js";
// The resolved-workflow topology projection (#563 slice 2b): nodes+edges DAG.
export { buildBundleTopology } from "./bundle-topology.js";
export type { ApiBundleTopology, ApiBundleTopologyEdge, ApiBundleTopologyNode } from "./bundle-topology.js";
// The resolved-workflow activity catalog projection (#563 slice 2b): schema identities + type-compat.
// (CATALOG_VERSION is already exported above for `meta`; the catalog reuses that same "1" pin.)
export { buildActivityCatalog } from "./activity-catalog.js";
export type { ApiActivityCatalog, ApiCatalogActivity, ApiCatalogSchema } from "./activity-catalog.js";
// The #575 bundle-field projections: effective per-step knobs, external links, per-knob source tags.
export { buildBundleSteps } from "./bundle-steps.js";
export type { ApiBundleMapShape, ApiBundleRetryPolicy, ApiBundleStep } from "./bundle-steps.js";
export { buildBundleLinks } from "./bundle-links.js";
export type { ApiBundleLinks } from "./bundle-links.js";
export { buildBundleRuntimeEffective } from "./bundle-runtime-effective.js";
export type { ApiBundleRuntimeEffective } from "./bundle-runtime-effective.js";
// The policy definition projections (#563 slice 2b): list summaries + full detail (rules + hash + used_by).
export { buildPolicyDefinition, buildPolicySummaries } from "./policy-definitions.js";
export type { ApiPolicyDefinition, ApiPolicySummary } from "./policy-definitions.js";
// The component-profile projections (#570): list summaries + full detail (runtime + content_hash + used_by).
export { buildProfileDefinition, buildProfileSummaries } from "./profile-definitions.js";
export type { ApiProfileDefinition, ApiProfileSummary } from "./profile-definitions.js";
// The in-repo insight-acknowledgement annotations projection (#733): parsed `.typeflux/annotations.yaml`, fail-closed.
export { annotationsPath, loadProjectAnnotations, readProjectAnnotations } from "./annotations.js";
export type { ApiInsightAnnotation, ApiProjectAnnotations, AnnotationsReadResult } from "./annotations.js";
// The resolved-workflow BUNDLE core (#563): identity + runtime summary + policy + topology + lifecycle + validation.
export {
  bundleErasure, BUNDLE_VERSION as RESOLVED_BUNDLE_VERSION, buildResolvedWorkflowBundle } from "./resolved-bundle.js";
export type {
  ApiBundleErasure,
  ApiBundleErasureCache,
  ApiBundleEnvironment,
  ApiBundleLifecycle,
  ApiBundleLifecycleGate,
  ApiBundleLifecycleReview,
  ApiBundleLifecycleReviewTimeout,
  ApiBundlePolicy,
  ApiBundleProject,
  ApiBundleSchema,
  ApiBundleValidation,
  ApiBundleWorkflowIdentity,
  ApiResolvedWorkflowBundle,
  ResolvedBundleContext,
} from "./resolved-bundle.js";
// The resolved-workflow connection status projection (#563 slice 2b): registry + observability, injected probe.
export { buildWorkflowConnections, defaultConnectionProbe } from "./connections.js";
export type {
  ApiConnectionStatus,
  ApiObserverStatus,
  ApiWorkflowConnections,
  ConnectionProbe,
  ConnectionProbeInput,
  ConnectionProbeResult,
} from "./connections.js";
// The injected langfuse reader seam (#573): powers the connections probe + prompt-status drift.
export { fetchLangfuseTransport, langfuseConnectionProbe } from "./langfuse-transport.js";
export type { FetchLangfuseTransportOptions, LangfuseControlPlaneTransport } from "./langfuse-transport.js";
// The enforcement-events feed (#723): pure normalization/filter/cursor helpers + the transport-read result.
export {
  ADMISSION_ENFORCEMENT_CODES,
  ADMISSION_VERDICT_CODES,
  admissionEventsFromReport,
  buildEnforcementFeed,
  decodeCursor,
  DEFAULT_LIMIT as ENFORCEMENT_DEFAULT_LIMIT,
  DEFAULT_WINDOW_MS as ENFORCEMENT_DEFAULT_WINDOW_MS,
  encodeCursor,
  EnforcementCursorError,
  filterEvents,
  filterFingerprint,
  isAdmissionEnforcementCode,
  MAX_LIMIT as ENFORCEMENT_MAX_LIMIT,
  observerFromReport,
  paginate,
  parseWindowBound,
  POLICY_VERDICT_CODES,
  resolveWindow,
  runtimeEventsFromTraces,
  sortEvents,
} from "./enforcement.js";
export type {
  EnforcementEvent,
  EnforcementEventList,
  EnforcementEvidence,
  EnforcementPartial,
  EnforcementReadResult,
  EnforcementSource,
  EnforcementTraceObservation,
  EnforcementTraceRecord,
  EnforcementVerdict,
  LangfuseEnforcementStatus,
} from "./enforcement.js";
// The injected GitHub reader seam (#727): powers the github-provenance surface's HEAD/PR reads. The
// public surface is the Langfuse precedent — the transport interface + the fetch reference impl + the
// types the HTTP-adapter tests consume (`GithubReadResult`). The pure host-gate/assembly helpers
// (`parseGithubRepo`, `servedProvenance`, `buildGithubProvenance`, …) are internal to the CP: the
// HTTP handler imports them directly, so they never need to leave the package.
export { fetchGithubTransport } from "./github-transport.js";
export type { FetchGithubTransportOptions, GithubProvenanceTransport } from "./github-transport.js";
export type { GithubReadResult } from "./github-provenance.js";
// Caller-facing error carrying an HTTP status (mapped by the HTTP server slice).
export { defaultErrorName, ProjectControlPlaneError } from "./errors.js";
// The HTTP adapter (#620): framework-agnostic route table, registry loader, node:http server.
export {
  Actor,
  ALL_PERMISSIONS,
  buildAuthorizer,
  OpenAuthorizer,
  OPERATE_PERMISSIONS,
  parsePermissions,
  parseTokenSpec,
  PERMISSIONS,
  ProxyHeaderAuthorizer,
  requirePermission,
  TokenAuthorizer,
} from "./http/auth.js";
export type { Authorizer, Permission, RequestHeaders, TokenGrant } from "./http/auth.js";
export {
  boundedTemporalTier,
  EXECUTIONS_SCAN_LIMIT,
  listWorkflowExecutions,
  memoIdentityVisibilityQuery,
  temporalConnectionOptions,
} from "./executions.js";
export type {
  ApiWorkflowExecutionList,
  ApiWorkflowExecutionRecord,
  ExecutionsVisibilityClient,
  TemporalConnectionOptions,
  VisibilityClientFactory,
  VisibilityExecutionInfo,
} from "./executions.js";
// The remaining #671 visibility decomposition (#686): drain view, task-queue workers, correlation.
export { UNIDENTIFIED_VERSION_SUFFIX,
  versionIdentityKey, workflowDrainStatus } from "./drain.js";
export type { ApiWorkflowDrainStatus, WorkflowDrainStatusOptions } from "./drain.js";
export { defaultTaskQueueClient, workflowTaskQueueWorkers } from "./workers.js";
export type {
  ApiWorkflowTaskQueueWorkers,
  TaskQueueClient,
  TaskQueueClientFactory,
  WorkflowTaskQueueWorkersOptions,
} from "./workers.js";
export { workflowRunCorrelation } from "./correlation.js";
export type { ApiWorkflowMigrationProvenance, ApiWorkflowRunCorrelation, ApiWorkflowRunCorrelationChild, WorkflowRunCorrelationOptions } from "./correlation.js";
export { buildWorkflowPromptStatus } from "./prompt-status.js";
export type { ApiPromptStatus, ApiWorkflowPromptStatus, PromptDriftStatus } from "./prompt-status.js";
// The operate tier (#563): ts-plan-argument start/status/review/cancel over @temporalio/client.
export {
  LIFECYCLE_STATUS_QUERY,
  MIGRATE_TERMINATION_REASON_PREFIX,
  migrateTerminationReason,
  operateTargetFromSpec,
  RECOMMENDED_STATUS_POLL_INTERVAL_SECONDS,
  REQUEST_CANCEL_SIGNAL,
  SUBMIT_REVIEW_SIGNAL,
  WorkflowOperations,
} from "./operations.js";
export type {
  ApiRuntimePinInfo,
  ApiWorkflowMigrateResult,
  ApiWorkflowOperationStatus,
  ApiWorkflowStartReceipt,
  OperateTarget,
  OperationsClient,
  OperationsClientFactory,
  OperationsHandle,
  StartHandleOptions,
  WorkflowDescription,
  WorkflowOperationsOptions,
} from "./operations.js";
export { buildRoutes } from "./http/handlers.js";
export type { RegistryContext, Route, RouteHandler, RouteRequest, RouteResponse } from "./http/handlers.js";
export { loadProjectRegistry, ProjectRegistry } from "./http/registry.js";
export type { ProjectRefreshResult, ProjectRegistryEntry, ProjectSummary } from "./http/registry.js";
export { createServer, serve } from "./http/server.js";
export type { ControlPlaneServer, ServeOptions } from "./http/server.js";
export { InProcessTypescriptResolver, type InProcessTypescriptResolverOptions, type TypefluxResolver } from "./resolver.js";
