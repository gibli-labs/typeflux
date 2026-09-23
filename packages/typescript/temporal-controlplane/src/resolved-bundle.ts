/**
 * The RESOLVED WORKFLOW BUNDLE — the control plane's immutable, secret-safe view of one resolved
 * workflow (governance parity, #563; Python `project/bundle.py` `resolve_workflow_bundle` +
 * `ResolvedWorkflowBundle`). Composes the existing resolution/validation/policy/identity/topology
 * projections; it performs no resolution of its own.
 *
 * SCOPE: the identity + runtime + governance + structure core — `project`, `environment`,
 * `workflow` (identity), `runtime` (secret-safe summary), `policy`, `topology`, `lifecycle`,
 * `validation` — plus `steps` (effective per-step Temporal knobs), `secret_references` (credential
 * provenance), `runtime_effective` (per-knob source tags), `links` (external UI deep links; the
 * live-Langfuse derivation stays deferred, #573), and the #568 slice: `activities` (secret-free
 * activity metadata) + `components` (applied component-profile provenance, from real composition —
 * the #570 loader + #568 resolver) + `environment.profile_variable_names`. The deployment tier is
 * real now (#687): `deployment_preview` (the secret-free worker plan, when a `deployment_image` is
 * supplied) + `deployment_preview_reference` (the `typeflux-project deploy` command otherwise). Still
 * deferred: `code` (git shim) — optional in the contract.
 */

import { activitySlotJsonSchema, schemaHash } from "@typeflux/temporal";
import {
  type AppliedComponentProfile,
  composeProjectPolicyIds,
  effectiveModelFor,
  evaluateWorkflowRiskTier,
  PLAN_INTERPRETER_VERSION,
  ProjectPolicyError,
  type ProjectBundleSources,
  type ProjectEnvironmentSpec,
  providerParamsRecord,
  type SecretReferenceRecord,
  secretReferenceRecords,
  secretValueConfigured,
  selectProjectPolicyIdsForWorkflow,
  type SubworkflowSpecResolver,
  type TypefluxProjectSpec,
  type TypefluxYamlSpec,
  validateProjectBundle,
  withEnvironmentContext,
  type WorkflowPlan,
  workflowPlanDigest,
  workflowPlanFromSpec,
  YAML_WORKFLOW_TYPE,
} from "@typeflux/temporal-yaml";
import type { z } from "zod";

import { assertDeclaredActivityGraph, schemaLogicalName } from "./activity-catalog.js";
import { type ApiBundleActivity, buildBundleActivities } from "./bundle-activities.js";
import { type ApiBundleLinks, buildBundleLinks } from "./bundle-links.js";
import { type ApiBundleRuntimeEffective, buildBundleRuntimeEffective } from "./bundle-runtime-effective.js";
import { type ApiBundleStep, buildBundleSteps } from "./bundle-steps.js";
import { type ApiBundleTopology, buildBundleTopology } from "./bundle-topology.js";
import { sanitizeHost } from "./connections.js";
import {
  type ApiValidationCheck,
  type ApiValidationIssue,
  mapCheck,
  mapIssue,
} from "./validation-dto.js";

export const BUNDLE_VERSION = "1" as const;
/** The spec-digest algorithm the TS runtime stamps (see `workflowPlanDigest`); Python `SPEC_DIGEST_ALGORITHM`. */
const SPEC_DIGEST_ALGORITHM = "typeflux-yaml-plan-v1" as const;

/** Project identity (Python `BundleProject`). */
export interface ApiBundleProject {
  name: string;
  manifest_path: string;
}

/** The selected environment (Python `BundleEnvironment`). */
export interface ApiBundleEnvironment {
  id: string;
  name: string;
  profile_path: string;
  env_files: Record<string, unknown>[];
  profile_variable_names: string[];
}

/** A schema slot with its JSON Schema + content hash (Python `_workflow_io_schema`). */
export interface ApiBundleSchema {
  name: string;
  hash: string;
  json_schema: Record<string, unknown>;
}

/** The resolved workflow's frozen identity (Python `BundleWorkflowIdentity`). */
export interface ApiBundleWorkflowIdentity {
  id: string;
  path: string;
  yaml_project: string;
  yaml_name: string;
  workflow_name: string;
  workflow_type: string;
  version_label?: string;
  spec_digest: string;
  spec_digest_algorithm: string;
  generator_version: string;
  task_queue: string;
  observability_trace_name: string;
  input_schema: ApiBundleSchema;
  output_schema: ApiBundleSchema;
}

/** The composed policy applied to this workflow (Python `BundlePolicy`). */
export interface ApiBundlePolicy {
  selected_policy_ids: string[];
  applied_policy_ids: string[];
  policy_names: string[];
  policy_hash: string;
}

/** One expanded macro requirement of a risk tier (Python `BundleRiskTierRequirement`). */
export interface ApiBundleRiskTierRequirement {
  name: string;
  satisfied: boolean;
}

/** The sub-workflow closure LIFT of this workflow's effective tier (Python `BundleRiskTierCascade`). */
export interface ApiBundleRiskTierCascade {
  lifted_by: string;
  effective: string;
  requirements: ApiBundleRiskTierRequirement[];
}

/** The workflow's risk-tier posture under the composed policy (Python `BundleRiskTier`).
 * `effective` is ALWAYS the tier admission enforces (cascade-lifted when a higher-tier
 * child raises it; `floor_source` then reads `cascade:<member>`), and `requirements` is
 * the ENFORCED tier's macro expansion — plus a `require_declared` entry when the policy
 * demands an explicit declaration. */
export interface ApiBundleRiskTier {
  declared: string;
  effective: string;
  floor: string;
  floor_source: string;
  requirements: ApiBundleRiskTierRequirement[];
  /** Present only when a higher-tier sub-workflow lifts the effective tier (exclude_none). */
  cascade?: ApiBundleRiskTierCascade;
}

/** The review gate's bounded-wait config (Python `BundleLifecycleReviewTimeout`). */
export interface ApiBundleLifecycleReviewTimeout {
  seconds: number;
  on_timeout: string;
  route?: string;
}

/** The human-in-the-loop review gate (Python `BundleLifecycleReview`). */
export interface ApiBundleLifecycleReview {
  after_step: string;
  invalid_user_decision: string;
  user_decisions: Record<string, string>;
  timeout?: ApiBundleLifecycleReviewTimeout;
}

/** The workflow lifecycle config (Python `BundleLifecycle`). */
/** One named review gate in the bundle projection (#55 slice 4; Python `BundleLifecycleGate`). */
export interface ApiBundleLifecycleGate {
  id: string;
  after_step: string;
  invalid_user_decision: string;
  user_decisions: Record<string, string>;
  timeout?: ApiBundleLifecycleReviewTimeout;
}

export interface ApiBundleLifecycle {
  enabled: boolean;
  progress: boolean;
  cancellation: boolean;
  status_event_limit: number;
  review?: ApiBundleLifecycleReview;
  /** The named gates (#55 slice 4); absent for single-`review`/gateless specs (exclude_none). */
  gates?: ApiBundleLifecycleGate[];
}

/** The bundle's validation section (Python `BundleValidation`). */
export interface ApiBundleValidation {
  ok: boolean;
  issues: ApiValidationIssue[];
  checks: ApiValidationCheck[];
}

/** The resolved workflow bundle (Python `ResolvedWorkflowBundle`, core subset — see the module SCOPE note). */
/** The cache-erasure contract this deployment gets (#795) — Python `BundleErasureCache`. */
export interface ApiBundleErasureCache {
  declared: "targeted" | "any";
  behavior: string;
}

/** Erasure posture (#795) — Python `BundleErasure`. */
export interface ApiBundleErasure {
  subject_selectors: number;
  cache: ApiBundleErasureCache;
}

export interface ApiResolvedWorkflowBundle {
  bundle_version: typeof BUNDLE_VERSION;
  project: ApiBundleProject;
  environment: ApiBundleEnvironment;
  workflow: ApiBundleWorkflowIdentity;
  runtime: Record<string, unknown>;
  policy?: ApiBundlePolicy;
  /** Risk-tier posture under the composed policy (#300 slice 2); absent when the policy
   * declares no `risk_tiers` dimension (Python `BundleRiskTier | None` under exclude_none). */
  risk_tier?: ApiBundleRiskTier;
  /** The planned activity set with secret-free AI/source metadata (#568). Always present (Python
   * `()` default under exclude_none — clients see an empty array, never `undefined`). */
  activities: ApiBundleActivity[];
  steps: ApiBundleStep[];
  topology: ApiBundleTopology;
  lifecycle?: ApiBundleLifecycle;
  secret_references: SecretReferenceRecord[];
  /** Erasure posture (#795); absent when the workflow declares neither subject selectors nor
   * a cache-erasure requirement (Python `BundleErasure | None` under exclude_none). */
  erasure?: ApiBundleErasure;
  validation: ApiBundleValidation;
  runtime_effective: ApiBundleRuntimeEffective[];
  /** Applied component-profile provenance (#568; Python `AppliedComponentProfile.to_dict()`) — one
   * entry per selected profile, or `[]` when the workflow/environment selects none. */
  components: AppliedComponentProfile[];
  /** Omitted (not null) when nothing resolves — Python `BundleLinks | None` under exclude_none. */
  links?: ApiBundleLinks;
  /** The secret-free deployment worker plan (Python `_deployment_preview`) — present only when a
   * `deployment_image` was supplied; carries `{error}` when the plan cannot be generated. */
  deployment_preview?: Record<string, unknown>;
  /** The copyable `typeflux-project deploy` command (Python `deployment_preview_reference`) — present
   * when NO `deployment_image` was supplied (mutually exclusive with `deployment_preview`). */
  deployment_preview_reference?: string;
}

/** Everything the bundle builder needs — assembled by the control plane from the loaded project. */
export interface ResolvedBundleContext {
  project: TypefluxProjectSpec;
  sources: ProjectBundleSources;
  spec: TypefluxYamlSpec;
  /** The selected environment spec — its `variables` overlay `process.env` for secret resolution. */
  environment: ProjectEnvironmentSpec;
  /** The applied component-profile provenance from resolution (Python `resolved.components`). */
  components: AppliedComponentProfile[];
  workflowId: string;
  environmentId: string;
  environmentName: string;
  workflowPath: string;
  profilePath: string;
  manifestPath: string;
  policyIds: readonly string[];
  schemas: Readonly<Record<string, z.ZodType>> | undefined;
  /** Resolves `workflow:` sub-workflow references so the derived plan embeds child plans (#55 §3.4). */
  subworkflows?: SubworkflowSpecResolver;
  /** The deployment image the caller asked a preview for (#687). Present ⇒ `deployment_preview`
   * is emitted (and the reference omitted); absent ⇒ `deployment_preview_reference` is emitted. */
  deploymentImage?: string;
  /** The pre-built secret-free deployment preview for `deploymentImage` (the control plane builds it
   * — it owns the sibling resolver; a generation failure arrives as `{error, notices?}`). */
  deploymentPreview?: Record<string, unknown>;
}

/** A schema slot from an injected Zod schema (Python `_workflow_io_schema` — name + hash + JSON Schema).
 * Unlike the activity catalog, the workflow io is NOT provider-gated (it's not a provider response). */
function bundleSchemaSlot(
  ref: string,
  io: "input" | "output",
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
): ApiBundleSchema {
  const schema = schemas !== undefined && Object.hasOwn(schemas, ref) ? schemas[ref] : undefined;
  if (schema === undefined) {
    throw new Error(`resolved bundle requires an injected schema for workflow ref '${ref}' (none was supplied)`);
  }
  const jsonSchema = activitySlotJsonSchema(schema, io) as Record<string, unknown>;
  return { name: schemaLogicalName(ref), hash: schemaHash(jsonSchema), json_schema: jsonSchema };
}

/** The secret-safe runtime summary (Python `resolved.summary()` + `_provider_runtime`): types, hosts,
 * models, and configured-flags — never api keys, prompt text, or `value_from` values. */
function safeRuntimeSummary(spec: TypefluxYamlSpec): Record<string, unknown> {
  const { temporal, registry, provider, observability } = spec.runtime;

  const tls = temporal.tls;
  const tlsIsBool = typeof tls === "boolean";
  const tlsEnabled = tlsIsBool ? tls : tls !== undefined;
  const tlsMode = tls === undefined ? "disabled" : tlsIsBool ? (tls ? "boolean" : "disabled") : "custom";

  // Python `_provider_runtime` ALWAYS emits `params` — `provider.provider_params().to_dict()` carries
  // at least the (materialized) model, so a plain `provider: { model: … }` still yields `params: {model}`.
  // Mirror that: merge the effective model into the declared params, and include the block when non-empty.
  const effectiveModel = effectiveModelFor(provider);
  const providerParams = {
    ...(provider.params !== undefined ? providerParamsRecord(provider.params) : {}),
    ...(effectiveModel !== undefined ? { model: effectiveModel } : {}),
  };

  return {
    temporal: {
      // Materialize the same defaults the runtime/policy layer uses (policy-enforcement.ts), so the
      // bundle reports the Temporal target executions actually hit, not a bare null.
      address: temporal.address ?? "localhost:7233",
      namespace: temporal.namespace ?? "default",
      tls_enabled: tlsEnabled,
      tls_mode: tlsMode,
      api_key_configured: secretValueConfigured(temporal.api_key),
    },
    registry: {
      type: registry.type,
      label: registry.label ?? null,
      // Strip any credentials a URL-shaped host may carry (shared with the connections projection).
      // DIVERGENCE: this is stricter than Python (which returns the host raw) and may canonicalize
      // (e.g. a trailing `/`) — a deliberate security choice for the secret-free contract over byte-parity.
      host: registry.host !== undefined ? sanitizeHost(registry.host) : null,
    },
    provider: {
      type: provider.type,
      // The EFFECTIVE model the runtime + policy layer use (`effectiveModelFor`): the top-level
      // `model`, else `params.model`, else the per-provider default — so preflight matches execution (codex).
      model: effectiveModel ?? null,
      // `base_url`/`vertex` are unsupported in the TS spec (clients are injected transports, #499),
      // so they can never be set — permanent divergences, always false.
      base_url_configured: false,
      api_key_configured: secretValueConfigured(provider.api_key),
      vertex: false,
      allow_prompt_model_override: provider.allow_prompt_model_override,
      ...(Object.keys(providerParams).length > 0 ? { params: providerParams } : {}),
    },
    observability: {
      type: observability?.type ?? null,
      execution_manifest: observability?.execution_manifest ?? true,
      // Carried so /connections decomposes from resolve_bundle alone for
      // foreign-edition projects (#671; the resolver contract's coverage_note).
      redaction_enabled: observability?.redaction?.enabled ?? true,
    },
  };
}

/** The workflow's frozen identity (Python `_bundle_workflow_identity`). */
function bundleWorkflowIdentity(context: ResolvedBundleContext, plan: WorkflowPlan): ApiBundleWorkflowIdentity {
  const { spec, schemas } = context;
  const digest = workflowPlanDigest(plan);

  // The workflow OUTPUT schema ref is what the runtime returns — the TERMINAL step's result: a final
  // ACTIVITY step yields its activity output, a final MAP step yields its `collect.output` object
  // (Python parity — NOT the mapped activity output). Any `workflow.output`/terminal MISMATCH was
  // already rejected by `assertDeclaredActivityGraph` (the shared `workflowSchemaChainError`), so here
  // the terminal ref and the declared output agree; a final map WITHOUT a collect has no single ref,
  // so the explicit `workflow.output` is used.
  const definitions = spec.activities.definitions ?? [];
  const steps = spec.workflow.steps;
  const lastStep = steps[steps.length - 1];
  const terminalRef =
    lastStep === undefined
      ? undefined
      : lastStep.activity !== undefined
        ? definitions.find((definition) => definition.name === lastStep.activity)?.output
        : // A terminal parallel block yields its collect object (#55); a terminal map its collect.
          (lastStep.map?.collect?.output ?? lastStep.parallel?.collect.output);
  const outputRef = terminalRef ?? spec.workflow.output;
  if (outputRef === undefined) {
    throw new Error("resolved bundle cannot determine the workflow output schema ref; set `workflow.output`");
  }

  return {
    id: context.workflowId,
    path: context.workflowPath,
    yaml_project: spec.project,
    yaml_name: spec.name,
    workflow_name: spec.workflow.name,
    workflow_type: YAML_WORKFLOW_TYPE,
    ...(spec.workflow.version !== undefined ? { version_label: spec.workflow.version } : {}),
    spec_digest: digest,
    spec_digest_algorithm: SPEC_DIGEST_ALGORITHM,
    generator_version: String(PLAN_INTERPRETER_VERSION),
    task_queue: spec.task_queue,
    observability_trace_name: `TypefluxWorkflow:${spec.workflow.name}`,
    input_schema: bundleSchemaSlot(spec.workflow.input, "input", schemas),
    output_schema: bundleSchemaSlot(outputRef, "output", schemas),
  };
}

/** The composed policy for this workflow (Python `_bundle_policy`), or undefined when none is selected. */
function bundlePolicy(context: ResolvedBundleContext): ApiBundlePolicy | undefined {
  const selected = selectProjectPolicyIdsForWorkflow(context.project, {
    environmentId: context.environmentId,
    workflowId: context.workflowId,
    explicitPolicyIds: [...context.policyIds],
  });
  if (selected.length === 0) return undefined;
  try {
    const composed = composeProjectPolicyIds(context.project, context.sources, selected);
    return {
      selected_policy_ids: composed.selectedPolicyIds,
      applied_policy_ids: composed.appliedPolicyIds,
      policy_names: composed.policyNames,
      policy_hash: composed.policyHash,
    };
  } catch (error) {
    if (error instanceof ProjectPolicyError) {
      // A composition conflict surfaces as a failed validation check; keep the bundle inspectable
      // with the selection recorded (Python parity).
      return { selected_policy_ids: selected, applied_policy_ids: [], policy_names: [], policy_hash: "" };
    }
    throw error;
  }
}

/** The workflow's risk-tier posture (Python `_bundle_risk_tier`), or undefined when the
 * composed policy declares no `risk_tiers` dimension. Same selection as {@link bundlePolicy}
 * so the surface tracks the policy the workflow is actually governed by; derived from the
 * shared `evaluateWorkflowRiskTier`, so it shows the SAME posture admission computes. */
function bundleRiskTier(context: ResolvedBundleContext): ApiBundleRiskTier | undefined {
  const selected = selectProjectPolicyIdsForWorkflow(context.project, {
    environmentId: context.environmentId,
    workflowId: context.workflowId,
    explicitPolicyIds: [...context.policyIds],
  });
  if (selected.length === 0) return undefined;
  let composed;
  try {
    composed = composeProjectPolicyIds(context.project, context.sources, selected);
  } catch (error) {
    // A composition conflict surfaces as a failed validation check (like `bundlePolicy`);
    // there is no composed policy to derive a tier posture from.
    if (error instanceof ProjectPolicyError) return undefined;
    throw error;
  }
  // Adapt the plan-derivation resolver (`.specFor`) to the closure resolver shape; a
  // standalone bundle without one still yields the base posture (no cascade).
  const resolver =
    context.subworkflows !== undefined
      ? (workflowId: string) => context.subworkflows?.specFor(workflowId)
      : undefined;
  const posture = evaluateWorkflowRiskTier(context.spec, composed, resolver, context.workflowId);
  if (posture === undefined) return undefined;
  const requirements = (requirementSource: { requirements: { name: string; satisfied: boolean }[] }) =>
    requirementSource.requirements.map((req) => ({ name: req.name, satisfied: req.satisfied }));
  // `effective` is ALWAYS what admission enforces: a cascade lift promotes the top-level
  // posture (effective / floor_source / requirements) to the LIFTED evaluation; the
  // `cascade` block explains it. `require_declared` is a real admission requirement too,
  // so it surfaces as a requirement entry (first — it is checked before the macro
  // expansion) instead of silently reading satisfied. Python `_bundle_risk_tier` parity.
  const enforced = posture.cascade !== undefined ? posture.cascade.evaluation : posture.base;
  const enforcedRequirements: ApiBundleRiskTierRequirement[] = [
    ...(enforced.requireDeclared ? [{ name: "require_declared", satisfied: !enforced.undeclared }] : []),
    ...requirements(enforced),
  ];
  return {
    declared: posture.base.declared,
    effective: enforced.effective,
    floor: posture.base.floor,
    floor_source:
      posture.cascade !== undefined ? `cascade:${posture.cascade.liftedBy}` : posture.base.floorSource,
    requirements: enforcedRequirements,
    ...(posture.cascade !== undefined
      ? {
          cascade: {
            lifted_by: posture.cascade.liftedBy,
            effective: posture.cascade.evaluation.effective,
            requirements: requirements(posture.cascade.evaluation),
          },
        }
      : {}),
  };
}

/** The workflow lifecycle projection (Python `_bundle_lifecycle`), or undefined when disabled. */
function bundleLifecycle(spec: TypefluxYamlSpec): ApiBundleLifecycle | undefined {
  const lifecycle = spec.workflow.lifecycle;
  if (lifecycle === undefined || lifecycle.enabled !== true) return undefined;
  const review = lifecycle.review;
  // Flatten each decision's route object to its route string (Python parity), sorted by decision.
  const flattenDecisions = (userDecisions: Record<string, { route: string }>): Record<string, string> =>
    Object.fromEntries(
      Object.entries(userDecisions)
        .map(([decision, route]) => [decision, route.route] as const)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
    );
  const timeoutOf = (gate: {
    timeout?: { seconds: number; on_timeout: string; route?: string | undefined } | undefined;
  }): { timeout?: ApiBundleLifecycleReviewTimeout } =>
    gate.timeout !== undefined
      ? {
          timeout: {
            seconds: gate.timeout.seconds,
            on_timeout: gate.timeout.on_timeout,
            ...(gate.timeout.route !== undefined ? { route: gate.timeout.route } : {}),
          },
        }
      : {};
  return {
    enabled: lifecycle.enabled,
    progress: lifecycle.progress,
    cancellation: lifecycle.cancellation,
    status_event_limit: lifecycle.history.status_event_limit,
    ...(review !== undefined
      ? {
          review: {
            after_step: review.after_step,
            invalid_user_decision: review.invalid_user_decision,
            user_decisions: flattenDecisions(review.user_decisions),
            ...timeoutOf(review),
          },
        }
      : {}),
    // Additive: the named gates (#55 slice 4). Absent for single-review specs, so their
    // bundle output stays byte-identical (Python exclude_none parity).
    ...(lifecycle.gates !== undefined
      ? {
          gates: lifecycle.gates.map((gate) => ({
            id: gate.id,
            after_step: gate.after_step,
            invalid_user_decision: gate.invalid_user_decision,
            user_decisions: flattenDecisions(gate.user_decisions),
            ...timeoutOf(gate),
          })),
        }
      : {}),
  };
}

/** The bundle's validation section (Python `resolve_workflow_bundle` validation): all report issues +
 * this workflow's resolved checks under the selected environment. */
function bundleValidation(context: ResolvedBundleContext): ApiBundleValidation {
  const report = validateProjectBundle(context.project, context.sources, {
    environmentId: context.environmentId,
    workflowIds: [context.workflowId],
    policyIds: [...context.policyIds],
    // Manifest-level issues in the bundle's embedded report carry `path` too (#643).
    manifestPath: context.manifestPath,
  });
  const resolved = report.resolvedWorkflows.find(
    (workflow) => workflow.workflowId === context.workflowId && workflow.environmentId === context.environmentId,
  );
  return {
    ok: report.ok,
    issues: report.issues.map(mapIssue),
    checks: (resolved?.checks ?? []).map(mapCheck),
  };
}

/**
 * Build the resolved workflow bundle core (Python `resolve_workflow_bundle`). The caller (the control
 * plane) has already resolved the workflow under the environment; this composes the projections.
 */
export function buildResolvedWorkflowBundle(context: ResolvedBundleContext): ApiResolvedWorkflowBundle {
  // ONE plan per bundle request — every projection below derives from the same immutable spec,
  // so each taking `workflowPlanFromSpec` output as a parameter beats four rebuilds (#575 review).
  const buildOptions = context.subworkflows !== undefined ? { subworkflows: context.subworkflows } : {};
  const plan = workflowPlanFromSpec(context.spec, buildOptions);
  // Reject an unrunnable graph (undeclared / duplicate activities) up front — Python's
  // `create_workflow` raises here, so the bundle must not 200 for a graph only `validation` flags (codex).
  assertDeclaredActivityGraph(context.spec, plan, context.subworkflows);
  const policy = bundlePolicy(context);
  const riskTier = bundleRiskTier(context);
  const lifecycle = bundleLifecycle(context.spec);
  // Live now-checks (secret configured-ness, external-link env vars) run under the selected
  // environment's variable overlay — the SAME resolution as validation and the runtime summary.
  // The callback MUST stay synchronous: the overlay mutates the global process.env for its
  // duration, and only single-threaded synchronous execution keeps a concurrent request's
  // overlay (another environment's variables) from interleaving in. A future async slice
  // (#568 metadata, #573 live Langfuse) must resolve its data BEFORE this block.
  const { runtime, secretReferences, links } = withEnvironmentContext(context.environment, undefined, () => ({
    runtime: safeRuntimeSummary(context.spec),
    secretReferences: secretReferenceRecords(context.spec),
    links: buildBundleLinks(context.spec),
  }));
  const erasure = bundleErasure(context.spec);
  return {
    bundle_version: BUNDLE_VERSION,
    project: { name: context.project.name, manifest_path: context.manifestPath },
    environment: {
      id: context.environmentId,
      name: context.environmentName,
      profile_path: context.profilePath,
      // env_files stays empty: the injection CP does not read `.env` files (text-only stance);
      // profile_variable_names is the sorted inline-variable names (Python
      // `build_project_environment_application`: `tuple(sorted(environment.variables))`).
      env_files: [],
      profile_variable_names: Object.keys(context.environment.variables).sort(),
    },
    workflow: bundleWorkflowIdentity(context, plan),
    runtime,
    ...(policy !== undefined ? { policy } : {}),
    ...(riskTier !== undefined ? { risk_tier: riskTier } : {}),
    // Real now (#568): the planned activity metadata, and the applied component-profile provenance
    // from resolution (empty when nothing is selected — Python `()` defaults, present arrays).
    activities: buildBundleActivities(context.spec, context.schemas, plan),
    steps: buildBundleSteps(context.spec, context.schemas, plan),
    topology: buildBundleTopology(context.spec, plan),
    ...(lifecycle !== undefined ? { lifecycle } : {}),
    secret_references: secretReferences,
    ...(erasure !== undefined ? { erasure } : {}),
    validation: bundleValidation(context),
    runtime_effective: buildBundleRuntimeEffective(context.spec, context.project),
    components: context.components,
    ...(links !== undefined ? { links } : {}),
    // Deployment tier (#687): a supplied image yields the secret-free worker preview; otherwise
    // the copyable promote command. Mutually exclusive, exactly like Python's exclude_none pair.
    // When an image is supplied the preview key is ALWAYS emitted (the CP always pairs them; a
    // missing preview from a direct caller degrades to an explicit error, never silently dropping
    // BOTH fields).
    ...(context.deploymentImage !== undefined
      ? {
          deployment_preview:
            context.deploymentPreview ?? { error: "no deployment preview was computed for the supplied deployment_image" },
        }
      : { deployment_preview_reference: deploymentPreviewReference(context) }),
  };
}

/** The copyable `typeflux-project deploy` command surfaced when no image is previewed
 * (Python `deployment_preview_reference`; the TS bin is `typeflux-project`, D687-2). */
function deploymentPreviewReference(context: ResolvedBundleContext): string {
  return (
    `typeflux-project deploy ${context.manifestPath} ` +
    `--environment ${context.environmentId} --workflow ${context.workflowId} --image <digest-pinned-image>`
  );
}

/** Cross-edition EXACT text (Python `_CACHE_ERASURE_BEHAVIOR` — byte-identical strings). */
const CACHE_ERASURE_BEHAVIOR: Readonly<Record<"targeted" | "any", string>> = {
  targeted:
    "targeted per-subject invalidation (declared REQUIRED: wiring a store without " +
    "SubjectErasableCacheStore fails runtime assembly, an erase run on the cache " +
    "surface without a wired store fails loudly, and a deployment that wires no " +
    "cache store satisfies the requirement vacuously — an empty cache has nothing " +
    "to erase)",
  any:
    "wired-store dependent: targeted per-subject invalidation when the injected " +
    "CacheStore implements SubjectErasableCacheStore, else the documented " +
    "full-cache-flush fallback (the erasure receipt records which behavior ran)",
};

/** #795: erasure posture, present only when a spec declaration makes it load-bearing
 * (subject selectors or a cache-erasure requirement) — existing bundles are unchanged. */
export function bundleErasure(spec: TypefluxYamlSpec): ApiBundleErasure | undefined {
  const selectors = spec.workflow.subjects?.length ?? 0;
  const declared = spec.runtime.cache_erasure;
  if (selectors === 0 && declared === undefined) return undefined;
  const effective = declared ?? "any";
  return {
    subject_selectors: selectors,
    cache: { declared: effective, behavior: CACHE_ERASURE_BEHAVIOR[effective] },
  };
}
