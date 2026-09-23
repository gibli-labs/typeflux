/**
 * Project BUNDLE validation (governance parity, #454; Python
 * `project/validation.py` `validate_project_bundle` + `_validate_resolved_workflow`).
 * Aggregates a {@link ProjectValidationReport} for a project: structural reference
 * checks, and — when an environment (and optionally specific workflows/policies) is
 * selected — a per-workflow resolved bundle that resolves the effective spec, enforces
 * its policies, and validates that its workflow graph is CONSTRUCTIBLE.
 *
 * INJECTION-BASED — the caller supplies the loaded environments + policy specs and the
 * workflow YAML TEXT as {@link ProjectBundleSources}; no filesystem access.
 *
 * TWO Python checks have no honest TS analogue and are emitted as SKIPPED with an
 * explanatory message: `provider_import_policy` (custom extension-class importlib paths)
 * and `activity_imports` (module discovery). The TS SDK INJECTS providers/registries/
 * observers/activities, and the spec stub-rejects the `runtime.imports` / `*.class`
 * fields (#496), so there is no module loading to police.
 */

import { resolveOptionalSecretText } from "./secret-references.js";
import { resolveEnvironmentWorkflow, stringifyEnvValue, validateEnvironmentWorkflowReferences } from "./environment-overlay.js";
import type { ProjectEnvironmentSpec } from "./environment-spec.js";
import { projectSubworkflowResolver, workflowPlanFromSpec, workflowSchemaChainError } from "./build-workflow.js";
import { workflowPlanDigest } from "./frozen-version.js";
import type { PolicyValidationCheck } from "./policy-enforcement.js";
import type { TypefluxProjectPolicySpec } from "./policy.js";
import { PROFILE_KINDS, type ProfileKind, type ProjectProfileSpec, composeProfileOverrides } from "./profile.js";
import { validateWorkflowPolicyCompliance } from "./project-enforcement.js";
import { type ProjectValidationIssue, validateProjectPolicyReferences } from "./project-resolve.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import { loadYamlSpec } from "./loader.js";
import type { TypefluxYamlSpec, WorkflowStepSpec } from "./spec.js";
import { flattenPlanSteps, type WorkflowPlan } from "./workflow-plan.js";

/** The per-workflow resolved-bundle result (Python `ProjectResolvedWorkflowValidation`). */
export interface ProjectResolvedWorkflowValidation {
  workflowId: string;
  environmentId: string;
  ok: boolean;
  /** From the resolved spec (present once resolution succeeded). */
  yamlProject?: string;
  yamlName?: string;
  workflowName?: string;
  taskQueue?: string;
  /** Manifest references, set on successful resolution (Python fills the resolved absolute
   * paths; the injection SDK carries the references — #565). */
  workflowPath?: string;
  environmentProfilePath?: string;
  checks: PolicyValidationCheck[];
}

/** A declared workflow's loaded identity (Python `report.workflows`; #565). */
export interface ProjectWorkflowSummaryValidation {
  workflowId: string;
  /** The manifest reference (Python reports the resolved absolute path). */
  path: string;
  yamlProject: string;
  yamlName: string;
  workflowName: string;
  taskQueue: string;
}

/** The full bundle report (Python `ProjectValidationReport`, injection subset — no manifest/file paths). */
export interface ProjectValidationReport {
  projectName: string;
  ok: boolean;
  issues: ProjectValidationIssue[];
  /** Every declared workflow that loaded, in declaration order (Python `report.workflows`). */
  workflows: ProjectWorkflowSummaryValidation[];
  resolvedWorkflows: ProjectResolvedWorkflowValidation[];
}

/**
 * The loaded specs + workflow text the validator injects instead of reading files:
 * `policies` keyed by project policy id, `environments` keyed by environment id, and
 * `workflows` mapping a workflow id to its `typeflux.yaml` TEXT.
 */
export interface ProjectBundleSources {
  policies: Readonly<Record<string, TypefluxProjectPolicySpec>>;
  environments: Readonly<Record<string, ProjectEnvironmentSpec>>;
  workflows: Readonly<Record<string, string>>;
  /** Loaded component profiles, kind-nested like the manifest's `profiles` section (#570). */
  profiles: Readonly<Record<ProfileKind, Readonly<Record<string, ProjectProfileSpec>>>>;
}

export interface ValidateProjectBundleOptions {
  /** The environment to resolve under — REQUIRED for any resolved (per-workflow) validation. */
  environmentId?: string;
  /** The workflows to validate (defaults to every declared project workflow). */
  workflowIds?: readonly string[];
  /** An explicit policy-id override applied to every selected workflow (else target-derived). */
  policyIds?: readonly string[];
  /** `.env` values the caller read, threaded into each workflow's interpolation context. */
  envFileValues?: Record<string, string>;
  /** The base interpolation environment (defaults to `process.env`). */
  baseEnv?: Record<string, string | undefined>;
  /**
   * The manifest reference reported as `path` on manifest-level issues (profile-selection codes;
   * Python attaches `project.manifest_path` there, #643). Injection callers may omit it.
   */
  manifestPath?: string;
}

const errorMessage = (error: unknown): string => (error instanceof Error ? error.message : String(error));
/** Whether any step (nested branch steps included) references a sub-workflow (#55 §3.4). */
/** #796: the api-key-requires-TLS invariant at ONE stage regardless of authoring shape.
 * A literal key with `tls: false` fails at YAML load; a `value_from` reference used to be
 * checked only at client connect. Emitted ONLY for that reference+tls-disabled shape:
 * a resolving credential FAILS exactly like the literal would have; an unresolvable
 * source is an explicit deferred notice, never silence. Byte-identical in Python. */
function temporalTlsInvariantCheck(spec: TypefluxYamlSpec): PolicyValidationCheck | undefined {
  const temporal = spec.runtime.temporal;
  const apiKey = temporal.api_key;
  // Python `tls: bool | block = False` — materialize the default (the zod-optional trap).
  const tlsDisabled = (temporal.tls ?? false) === false;
  if (typeof apiKey !== "object" || apiKey === null || !tlsDisabled) return undefined;
  let resolvedKey: string | undefined;
  try {
    resolvedKey = resolveOptionalSecretText(apiKey, "runtime.temporal.api_key");
  } catch {
    resolvedKey = undefined; // Required source absent here — may still resolve at runtime.
  }
  if (resolvedKey) {
    return failed(
      "temporal_tls_invariant",
      "runtime.temporal.api_key resolves to a credential but runtime.temporal.tls is " +
        "disabled — the api-key-requires-TLS invariant fails (the literal form fails at " +
        "YAML load; the reference form is enforced here and at client connect)",
    );
  }
  return skipped(
    "temporal_tls_invariant",
    "TLS invariant deferred: runtime.temporal.api_key is a value_from reference whose " +
      "source is not resolvable at validate time; enforced at client connect",
  );
}

/** #797: wherever a deferred codec presence check is in play, say what `required: false`
 * does NOT defer. Always `passed` — a semantics notice, not a finding. Byte-identical in
 * Python. */
function payloadCodecPresenceCheck(spec: TypefluxYamlSpec): PolicyValidationCheck | undefined {
  const codec = spec.runtime.temporal.payload_codec;
  if (codec === undefined) return undefined;
  const deferred = codec.keys.filter((key) => key.value_from.required === false).map((key) => key.id);
  if (deferred.length === 0) return undefined;
  return passed("payload_codec_presence", {
    deferred_keys: deferred,
    runtime_behavior:
      "required: false defers only the offline presence check — the codec always " +
      "fail-closes at runtime on an unset or invalid key",
  });
}

export function specReferencesSubworkflows(steps: readonly WorkflowStepSpec[]): boolean {
  return steps.some(
    (step) =>
      step.workflow !== undefined ||
      step.map?.workflow !== undefined ||
      (step.parallel !== undefined && step.parallel.branches.some((branch) => specReferencesSubworkflows(branch.steps))),
  );
}

/**
 * Every sibling workflow id a spec references (`workflow:` steps + `map.workflow`
 * fan-outs, recursively through parallel branches), de-duplicated in first-seen order
 * (Python `collect_subworkflow_references`). The transitive-closure admission walk
 * (#55 §9) resolves these against the project manifest and re-validates each child.
 */
export function collectSubworkflowReferences(steps: readonly WorkflowStepSpec[]): string[] {
  const seen: string[] = [];
  const add = (ref: string): void => {
    if (!seen.includes(ref)) seen.push(ref);
  };
  const walk = (nested: readonly WorkflowStepSpec[]): void => {
    for (const step of nested) {
      if (step.workflow !== undefined) add(step.workflow);
      else if (step.map?.workflow !== undefined) add(step.map.workflow);
      else if (step.parallel !== undefined) {
        for (const branch of step.parallel.branches) walk(branch.steps);
      }
    }
  };
  walk(steps);
  return seen;
}

const passed = (code: string, details?: Record<string, unknown>): PolicyValidationCheck => ({
  code,
  status: "passed",
  ...(details !== undefined ? { details } : {}),
});
const failed = (code: string, message: string): PolicyValidationCheck => ({ code, status: "failed", message });
const skipped = (code: string, message: string): PolicyValidationCheck => ({ code, status: "skipped", message });

const safeGet = <T>(record: Readonly<Record<string, T>>, key: string): T | undefined =>
  Object.hasOwn(record, key) ? record[key] : undefined;

/**
 * Run `fn` with the selected environment's values overlaid onto `process.env`, then restore it —
 * the injection analogue of Python's `project_environment_context`. The policy checks read
 * `process.env` DIRECTLY (Temporal region, `require_api_key` env references), so an
 * environment-scoped policy must be evaluated under the SAME values that resolution used, not the
 * bare host env. Safe because the wrapped policy validation is synchronous (no await can interleave
 * a concurrent reader); a `finally` restores even if it throws. Precedence matches resolution:
 * `.env` values then inline `variables`. NOTE: `baseEnv` (an interpolation-only concern) is not
 * applied here — the policy checks read the host `process.env` plus this environment overlay.
 */
export function withEnvironmentContext<T>(
  environment: ProjectEnvironmentSpec,
  envFileValues: Record<string, string> | undefined,
  fn: () => T,
): T {
  const overlay = new Map<string, string>();
  for (const [key, value] of Object.entries(envFileValues ?? {})) overlay.set(key, value);
  for (const [key, value] of Object.entries(environment.variables)) overlay.set(key, stringifyEnvValue(value));
  const saved = new Map<string, string | undefined>();
  for (const [key, value] of overlay) {
    // `Object.hasOwn` (not a bare `process.env[key]`): a key like `__proto__` / `toString` reads
    // its inherited Object.prototype member, and restoring THAT would leave a corrupt own property
    // (e.g. `__proto__ = "[object Object]"`). An unset key captures `undefined` → deleted on restore.
    saved.set(key, Object.hasOwn(process.env, key) ? process.env[key] : undefined);
    process.env[key] = value;
  }
  try {
    return fn();
  } finally {
    for (const [key, prior] of saved) {
      if (prior === undefined) delete process.env[key];
      else process.env[key] = prior;
    }
  }
}

const buildReport = (
  projectName: string,
  issues: ProjectValidationIssue[],
  workflows: ProjectWorkflowSummaryValidation[],
  resolvedWorkflows: ProjectResolvedWorkflowValidation[],
): ProjectValidationReport => ({ projectName, ok: issues.length === 0, issues, workflows, resolvedWorkflows });

const UNKNOWN_VALIDATION_CODES = new Set([
  "unknown_validation_environment",
  "unknown_validation_workflow",
  "unknown_validation_policy",
]);

/**
 * Validate a project bundle (Python `validate_project_bundle`). With no environment/
 * workflow/policy selection, returns just the structural reference report (policy/target
 * + environment-workflow references). With an `environmentId`, additionally resolves each
 * selected workflow and runs its per-workflow checks; every failed check becomes a
 * `resolved_<code>_failed` issue. Resolution is skipped (and the report returned early) if
 * any reference or unknown-id issue is present, matching Python.
 */
export function validateProjectBundle(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  options: ValidateProjectBundleOptions = {},
): ProjectValidationReport {
  // Structural reference validation (Python `validate_project`, injection subset): policy
  // and validation-target references, plus each environment's per-workflow overrides.
  const issues: ProjectValidationIssue[] = [...validateProjectPolicyReferences(project, { policies: sources.policies })];
  // Python attaches `path=project.manifest_path` to every manifest-level issue (profile-selection
  // codes and the profile kind/load failures, #643) — spread into those sites when provided.
  const manifestRef = options.manifestPath !== undefined ? { path: options.manifestPath } : {};
  // Only the project's DECLARED environments are validated — an over-provided / reused
  // `sources.environments` must not let an unrelated environment invalidate the report (codex).
  // A declared environment with NO provided source is the injection analogue of Python's
  // `missing_environment_file` (symmetric with `missing_policy_source`).
  for (const environmentId of Object.keys(project.environments)) {
    const environment = safeGet(sources.environments, environmentId);
    if (environment === undefined) {
      issues.push({
        code: "missing_environment_source",
        message: `environment '${environmentId}' is declared but no source was provided`,
        reference: environmentId,
      });
      continue;
    }
    issues.push(...validateEnvironmentWorkflowReferences(project, environment));
  }
  // Every declared workflow must have a provided source (Python `missing_workflow_file`;
  // symmetric with missing_policy_source / missing_environment_source) — reported even in
  // reference-only mode, unlike the resolution failure a selected-but-unsourced workflow yields.
  for (const workflow of project.workflows) {
    if (!Object.hasOwn(sources.workflows, workflow.id)) {
      issues.push({
        code: "missing_workflow_source",
        message: `workflow '${workflow.id}' is declared but no source was provided`,
        reference: workflow.id,
      });
    }
  }
  // Parse every SOURCED workflow (Python `validate_project` loads each one, #565): a
  // present-but-malformed source is invalid_workflow_yaml; loaded `workflow.name` collisions
  // are ONE aggregated duplicate_workflow_name (Python's single-raise shape). Reference-level:
  // both bail resolution below.
  const workflowSummaries: ProjectWorkflowSummaryValidation[] = [];
  const idsByWorkflowName = new Map<string, string[]>();
  for (const workflow of project.workflows) {
    const text = safeGet(sources.workflows, workflow.id);
    if (text === undefined) continue; // missing_workflow_source above
    const path = workflow.path ?? `${workflow.directory}/${project.defaults.workflow_filename}`;
    try {
      // A BARE load, no defaults layer — Python's reference-level `load_yaml_spec(workflow_path)`
      // (loader.py) parses the raw file, so a workflow relying on `defaults.runtime` for a
      // REQUIRED field fails reference validation on both sides alike.
      const spec = loadYamlSpec(text, { sourceLabel: workflow.id });
      workflowSummaries.push({
        workflowId: workflow.id,
        path,
        yamlProject: spec.project,
        yamlName: spec.name,
        workflowName: spec.workflow.name,
        taskQueue: spec.task_queue,
      });
      idsByWorkflowName.set(spec.workflow.name, [...(idsByWorkflowName.get(spec.workflow.name) ?? []), workflow.id]);
    } catch (error) {
      issues.push({
        code: "invalid_workflow_yaml",
        message: `workflow '${workflow.id}' failed to load: ${error instanceof Error ? error.message : String(error)}`,
        reference: workflow.id,
        path,
      });
    }
  }
  const duplicateWorkflowNames = [...idsByWorkflowName.entries()]
    .filter(([, ids]) => ids.length > 1)
    .map(([name]) => name)
    .sort();
  if (duplicateWorkflowNames.length > 0) {
    issues.push({
      code: "duplicate_workflow_name",
      message: `duplicate YAML workflow name(s): ${duplicateWorkflowNames.join(", ")}`,
    });
  }
  // Declared component profiles (#570; Python `_validate_profiles`): every declared profile
  // must have a loaded source (the injection analogue of `invalid_component_profile` for a
  // missing/unloadable file), and every workflow-level selection must reference a declared
  // kind + id (Python `invalid_profile_selection` / `unknown_profile_reference`).
  if (project.profiles !== undefined) {
    for (const kind of PROFILE_KINDS) {
      for (const profileId of Object.keys(project.profiles[kind])) {
        const source = Object.hasOwn(sources.profiles[kind], profileId)
          ? sources.profiles[kind][profileId]
          : undefined;
        if (source === undefined) {
          issues.push({
            code: "missing_profile_source",
            message: `${kind} profile '${profileId}' is declared but no source was provided`,
            reference: profileId,
          });
        } else if (source.kind !== kind) {
          // An injected source can bypass the fs loader's declaredKind check — mirror it here
          // so /validate agrees with the profile APIs (Python's load-time kind rejection).
          issues.push({
            code: "invalid_component_profile",
            message: `profile '${profileId}' is referenced under profiles.${kind} but declares kind: ${source.kind}`,
            reference: profileId,
            // Python's analogue (a load-time kind rejection) carries the manifest path too.
            ...manifestRef,
          });
        }
      }
    }
  }
  const checkProfileSelection = (selection: Record<string, string>, context: string, reference: string): void => {
    // Python `validate_profile_selection` raises on the FIRST unknown kind — one issue for the
    // whole selection, and the per-id checks never run for it (parity: same issue set/count).
    for (const kind of Object.keys(selection)) {
      if (!(PROFILE_KINDS as readonly string[]).includes(kind)) {
        issues.push({
          code: "invalid_profile_selection",
          message: `${context} selects unknown profile kind '${kind}'; valid kinds: ${PROFILE_KINDS.join(", ")}`,
          reference,
          ...manifestRef,
        });
        return;
      }
    }
    for (const [kind, profileId] of Object.entries(selection)) {
      const declared = project.profiles?.[kind as ProfileKind] ?? {};
      if (!Object.hasOwn(declared, profileId)) {
        issues.push({
          code: "unknown_profile_reference",
          message: `${context} selects unknown ${kind} profile: ${profileId}`,
          reference,
          ...manifestRef,
        });
      }
    }
  };
  for (const workflow of project.workflows) {
    checkProfileSelection(workflow.profiles, `workflow '${workflow.id}' profile selection`, workflow.id);
  }
  // Environment-level `workflow_profiles` selections (Python checks every LOADABLE environment:
  // `load_project_environment` raises on an undeclared workflow id, so Python emits NO profile
  // issues for that environment — the unknown_environment_workflow issue already covers it).
  for (const environmentId of Object.keys(project.environments)) {
    const environment = safeGet(sources.environments, environmentId);
    if (environment === undefined) continue; // missing_environment_source already reported above
    const declaredIds = new Set(project.workflows.map((workflow) => workflow.id));
    if (Object.keys(environment.workflows).some((workflowId) => !declaredIds.has(workflowId))) {
      continue;
    }
    for (const [workflowId, envWorkflow] of Object.entries(environment.workflows)) {
      checkProfileSelection(
        envWorkflow.profiles,
        `environment '${environmentId}' profile selection for workflow '${workflowId}'`,
        environmentId,
      );
    }
  }
  const referenceIssueCount = issues.length;

  const { environmentId } = options;
  const explicitWorkflowIds = options.workflowIds ?? [];
  const policyIds = options.policyIds ?? [];
  const resolvedWorkflows: ProjectResolvedWorkflowValidation[] = [];

  // Reference-only mode: nothing selected → return the structural report as-is.
  if (environmentId === undefined && explicitWorkflowIds.length === 0 && policyIds.length === 0) {
    return buildReport(project.name, issues, workflowSummaries, resolvedWorkflows);
  }

  // A resolved validation is anchored to an environment; requesting workflows/policies
  // without one is an error (Python `validation_environment_required`).
  if (environmentId === undefined) {
    const needs = [
      explicitWorkflowIds.length > 0 ? "workflowIds" : undefined,
      policyIds.length > 0 ? "policyIds" : undefined,
    ].filter((flag): flag is string => flag !== undefined);
    const prefix = needs.length > 0 ? needs.join(" and ") : "resolved project validation";
    issues.push({
      code: "validation_environment_required",
      message: `${prefix} requires an environmentId for resolved project validation`,
    });
    return buildReport(project.name, issues, workflowSummaries, resolvedWorkflows);
  }

  // Unknown-id checks against the manifest's declared sets.
  const declaredWorkflowIds = new Set(project.workflows.map((workflow) => workflow.id));
  const declaredPolicyIds = new Set(Object.keys(project.policies));
  if (!Object.hasOwn(project.environments, environmentId)) {
    issues.push({
      code: "unknown_validation_environment",
      message: `unknown project environment: ${environmentId}`,
      reference: environmentId,
    });
  }
  const selectedWorkflowIds = explicitWorkflowIds.length > 0 ? [...explicitWorkflowIds] : [...declaredWorkflowIds];
  for (const workflowId of selectedWorkflowIds) {
    if (!declaredWorkflowIds.has(workflowId)) {
      issues.push({ code: "unknown_validation_workflow", message: `unknown project workflow: ${workflowId}`, reference: workflowId });
    }
  }
  for (const policyId of policyIds) {
    if (!declaredPolicyIds.has(policyId)) {
      issues.push({ code: "unknown_validation_policy", message: `unknown project policy: ${policyId}`, reference: policyId });
    }
  }

  // Don't resolve against a manifest that is itself invalid (Python bails on any reference
  // issue OR any unknown-id issue) — resolution would only produce confusing secondary errors.
  if (referenceIssueCount > 0 || issues.some((issue) => UNKNOWN_VALIDATION_CODES.has(issue.code))) {
    return buildReport(project.name, issues, workflowSummaries, resolvedWorkflows);
  }

  for (const workflowId of selectedWorkflowIds) {
    const validation = validateResolvedWorkflow(project, sources, {
      environmentId,
      workflowId,
      policyIds,
      ...(options.envFileValues !== undefined ? { envFileValues: options.envFileValues } : {}),
      ...(options.baseEnv !== undefined ? { baseEnv: options.baseEnv } : {}),
    });
    resolvedWorkflows.push(validation);
    for (const check of validation.checks) {
      if (check.status === "failed") {
        issues.push({
          code: `resolved_${check.code}_failed`,
          message: check.message || `resolved check failed: ${check.code}`,
          reference: `${environmentId}:${workflowId}`,
          // Python sets `path` from the validation's own workflow_path, which stays unset when
          // resolution itself failed — never from the manifest map.
          ...(validation.workflowPath !== undefined ? { path: validation.workflowPath } : {}),
        });
      }
    }
  }

  return buildReport(project.name, issues, workflowSummaries, resolvedWorkflows);
}

interface ResolvedWorkflowContext {
  environmentId: string;
  workflowId: string;
  policyIds: readonly string[];
  envFileValues?: Record<string, string>;
  baseEnv?: Record<string, string | undefined>;
}

/**
 * Resolve one workflow under an environment and run its per-workflow checks (Python
 * `_validate_resolved_workflow`): resolution → observability → policy compliance →
 * import checks (N/A in TS, skipped) → workflow-graph constructibility → execution
 * manifest. Graph + manifest are skipped when policy enforcement failed, mirroring
 * Python's dependency ordering (a non-compliant workflow won't run).
 */
function validateResolvedWorkflow(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  context: ResolvedWorkflowContext,
): ProjectResolvedWorkflowValidation {
  const { environmentId, workflowId } = context;
  const checks: PolicyValidationCheck[] = [];

  const environment = safeGet(sources.environments, environmentId);
  const workflowText = safeGet(sources.workflows, workflowId);
  if (environment === undefined || workflowText === undefined) {
    const missing = environment === undefined ? `environment '${environmentId}'` : `workflow '${workflowId}'`;
    checks.push(failed("environment_workflow_resolution", `no source provided for ${missing}`));
    return { workflowId, environmentId, ok: false, checks };
  }

  let spec: TypefluxYamlSpec;
  try {
    // The PROFILED spec is what the bundle/operate tiers run (Python's resolve_project_workflow
    // composes profiles) — validating the unprofiled overlay would pass configs the runtime
    // never uses (codex). A broken selection fails this check like any resolution error.
    const workflow = project.workflows.find((entry) => entry.id === workflowId);
    const { overrides } = composeProfileOverrides(
      { specs: sources.profiles, paths: project.profiles ?? { provider: {}, registry: {}, runtime: {} } },
      {
        workflowSelection: workflow?.profiles ?? {},
        environmentSelection: environment.workflows[workflowId]?.profiles ?? {},
        workflowContext: `workflow '${workflowId}' profile selection`,
        environmentContext: `environment '${environment.name}' profile selection for '${workflowId}'`,
      },
    );
    spec = resolveEnvironmentWorkflow(workflowText, {
      environment,
      workflowId,
      runtimeDefaults: project.defaults.runtime,
      sourceLabel: `${environmentId}:${workflowId}`,
      profileOverrides: overrides,
      ...(context.envFileValues !== undefined ? { envFileValues: context.envFileValues } : {}),
      ...(context.baseEnv !== undefined ? { baseEnv: context.baseEnv } : {}),
    });
  } catch (error) {
    checks.push(failed("environment_workflow_resolution", `failed to resolve environment/workflow bundle: ${errorMessage(error)}`));
    return { workflowId, environmentId, ok: false, checks };
  }
  checks.push(passed("environment_workflow_resolution"));

  // Resolve a SIBLING workflow's spec under the same environment (#55 §3.4): sub-workflow
  // (`workflow:` / `map.workflow`) references resolve through the project manifest, exactly like
  // the parent itself, so the graph check below type-walks the child's declared IO. Returns
  // `undefined` (never throws) for an unknown/unresolvable id — the plan derivation then reports
  // the precise dangling/cycle error.
  const resolveSiblingSpec = (siblingId: string): TypefluxYamlSpec | undefined => {
    const siblingText = safeGet(sources.workflows, siblingId);
    const siblingWorkflow = project.workflows.find((entry) => entry.id === siblingId);
    if (siblingText === undefined || siblingWorkflow === undefined) {
      return undefined;
    }
    try {
      const { overrides: siblingOverrides } = composeProfileOverrides(
        { specs: sources.profiles, paths: project.profiles ?? { provider: {}, registry: {}, runtime: {} } },
        {
          workflowSelection: siblingWorkflow.profiles ?? {},
          environmentSelection: environment.workflows[siblingId]?.profiles ?? {},
          workflowContext: `workflow '${siblingId}' profile selection`,
          environmentContext: `environment '${environment.name}' profile selection for '${siblingId}'`,
        },
      );
      return resolveEnvironmentWorkflow(siblingText, {
        environment,
        workflowId: siblingId,
        runtimeDefaults: project.defaults.runtime,
        sourceLabel: `${environmentId}:${siblingId}`,
        profileOverrides: siblingOverrides,
        ...(context.envFileValues !== undefined ? { envFileValues: context.envFileValues } : {}),
        ...(context.baseEnv !== undefined ? { baseEnv: context.baseEnv } : {}),
      });
    } catch {
      return undefined;
    }
  };
  const subworkflows = projectSubworkflowResolver(workflowId, resolveSiblingSpec);

  const observability = spec.runtime.observability;
  // Materialize Python's `ObservabilitySpec.execution_manifest = True` default: the TS spec
  // makes the field optional (no runtime consumer yet), so an unset value is treated as ENABLED
  // — the manifest check builds by default and is skipped only when explicitly `false` (the
  // zod-optional-vs-Pydantic-default parity trap).
  const executionManifestEnabled = observability?.execution_manifest ?? true;
  checks.push(passed("observability_config", { type: observability?.type, execution_manifest: executionManifestEnabled }));

  // #796/#797 (byte-identical in the Python edition; emitted only when the triggering
  // config exists, so existing validate outputs are unchanged). Evaluated under the
  // selected environment so a value_from env source sees what deployment will.
  withEnvironmentContext(environment, context.envFileValues, () => {
    const tlsCheck = temporalTlsInvariantCheck(spec);
    if (tlsCheck !== undefined) checks.push(tlsCheck);
    const codecCheck = payloadCodecPresenceCheck(spec);
    if (codecCheck !== undefined) checks.push(codecCheck);
    return undefined;
  });

  // Evaluate policy under the SELECTED environment (its variables/.env visible on process.env),
  // so an environment-scoped check (Temporal region, `require_api_key`) sees what deployment will
  // (codex) — mirroring Python running `validate_project_policy` inside `project_environment_context`.
  const policyChecks = withEnvironmentContext(environment, context.envFileValues, () =>
    validateWorkflowPolicyCompliance(
      project,
      { policies: sources.policies },
      { spec, workflowId, environmentId, explicitPolicyIds: context.policyIds, resolveSubworkflowSpec: resolveSiblingSpec },
    ),
  );
  checks.push(...policyChecks);
  const policyFailed = policyChecks.some((check) => check.status === "failed");

  // The Python module/class-import checks have no TS analogue — extensions and activities
  // are injected, and the spec stub-rejects `runtime.imports` / `*.class` (#496).
  checks.push(skipped("provider_import_policy", "the TS SDK injects extensions — no importlib class paths to validate"));
  checks.push(skipped("activity_imports", "the TS SDK injects activities — no module imports to resolve"));

  let plan: WorkflowPlan | undefined;
  if (policyFailed) {
    checks.push(skipped("workflow_graph", "skipped because policy enforcement failed"));
  } else {
    try {
      // A governed bundle must DECLARE every activity it references — that is what makes the
      // activity policy-checkable (moderation/semantics inspect `activities.definitions`).
      // Duplicate definitions can't be assembled (`defineActivitiesFromSpec` rejects them), and a
      // step referencing an UNDECLARED activity is a dangling reference `assembleYamlRuntime`
      // rejects at runtime. Code-first injected `extraActivities` are outside declarative bundle
      // validation — a workflow that relies on them isn't fully governable here (codex).
      const declared = new Set<string>();
      const duplicateNames = new Set<string>();
      for (const definition of spec.activities.definitions ?? []) {
        if (declared.has(definition.name)) duplicateNames.add(definition.name);
        declared.add(definition.name);
      }
      const built = workflowPlanFromSpec(spec, { subworkflows });
      // Leaf steps (nested branch steps included): a parallel node itself calls no activity.
      const undefinedActivities = [...new Set(flattenPlanSteps(built.steps).map((step) => step.activity))].filter(
        (activity) => !declared.has(activity),
      );
      // The linear schema chain must also type-check (Python `_validate_workflow_graph`): a step whose
      // input ref differs from the prior step's output is unrunnable, so `/validate` reports it here —
      // keeping it consistent with the bundle/catalog projections that reject the same graph.
      const chainError =
        duplicateNames.size === 0 && undefinedActivities.length === 0
          ? workflowSchemaChainError(spec, { subworkflows })
          : undefined;
      if (duplicateNames.size > 0) {
        checks.push(failed("workflow_graph", `duplicate activity definition name(s): ${[...duplicateNames].join(", ")}`));
      } else if (undefinedActivities.length > 0) {
        checks.push(
          failed("workflow_graph", `workflow references activities not defined in the spec: ${undefinedActivities.join(", ")}`),
        );
      } else if (chainError !== undefined) {
        checks.push(failed("workflow_graph", chainError));
      } else {
        plan = built;
        checks.push(passed("workflow_graph", { step_count: built.steps.length }));
      }
    } catch (error) {
      checks.push(failed("workflow_graph", errorMessage(error)));
    }
  }

  // Sub-workflow visibility notice (#55 §6 mitigation b), IMMEDIATELY after workflow_graph and
  // only for workflows with a direct sub-workflow reference (V1 outputs stay byte-identical).
  // Always "passed" — this is a notice, never a rejection: the TS frozen-version scan is
  // bounded and fail-open, and wide child fan-outs flood it precisely when a frozen
  // `workflow.version` most needs it; the configured search attribute keeps children (which
  // stamp their OWN name) out of the parent's scan. Byte-identical in the Python edition.
  if (specReferencesSubworkflows(spec.workflow.steps)) {
    const searchAttributeConfigured = spec.runtime.temporal.workflow_search_attribute !== undefined;
    const versionLabel = spec.workflow.version ?? null;
    checks.push(
      passed("subworkflow_visibility", {
        search_attribute_configured: searchAttributeConfigured,
        workflow_version: versionLabel,
        ...(versionLabel !== null && !searchAttributeConfigured
          ? {
              notice:
                "workflow.version is declared but runtime.temporal.workflow_search_attribute is not " +
                "configured; wide sub-workflow fan-outs degrade the frozen-version scan — configure " +
                "the search attribute (#55)",
            }
          : {}),
      }),
    );
  }

  if (policyFailed) {
    checks.push(skipped("execution_manifest", "skipped because policy enforcement failed"));
  } else if (plan === undefined) {
    checks.push(skipped("execution_manifest", "skipped because workflow graph validation failed"));
  } else if (!executionManifestEnabled) {
    checks.push(skipped("execution_manifest", "runtime.observability.execution_manifest is disabled"));
  } else {
    // `activity_count` is the distinct activity DEFINITIONS (Python `len(manifest.activities)`),
    // not the step count — an activity invoked by two steps is one activity; `map_step_count` is
    // per-step (Python `len(manifest.map_steps)`).
    checks.push(
      passed("execution_manifest", {
        plan_digest: workflowPlanDigest(plan),
        activity_count: new Set((spec.activities.definitions ?? []).map((definition) => definition.name)).size,
        map_step_count: flattenPlanSteps(plan.steps).filter((step) => step.kind === "map").length,
      }),
    );
  }

  const workflow = project.workflows.find((entry) => entry.id === workflowId);
  const workflowPath = workflow?.path ?? `${workflow?.directory}/${project.defaults.workflow_filename}`;
  return {
    workflowId,
    environmentId,
    ok: !checks.some((check) => check.status === "failed"),
    yamlProject: spec.project,
    yamlName: spec.name,
    workflowName: spec.workflow.name,
    taskQueue: spec.task_queue,
    workflowPath,
    environmentProfilePath: safeGet(project.environments, environmentId) ?? "",
    checks,
  };
}
