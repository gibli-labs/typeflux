/**
 * Environment OVERLAY — apply a loaded project environment onto a workflow spec
 * (governance parity, #454; Python `project/environment.py`
 * `resolve_project_workflow` / `_merged_overrides` / `build_project_environment_application`).
 *
 * This is the INJECTION-BASED, pure slice of Python's resolver: the caller has
 * already loaded the environment (via {@link loadEnvironmentSpec}) and read the
 * workflow YAML text (and any `.env` file values); this module merges the
 * environment's bounded overrides onto that workflow and builds the interpolation
 * context from its inline variables. It does NOT touch the filesystem.
 *
 * The component-PROFILE layer (Python `_resolved_profile_overrides` → `profiles.py`) slots
 * between the workflow YAML and the environment overrides: the caller composes it (via
 * {@link composeProfileOverrides}) and passes the resulting override fragment as
 * `profileOverrides`; this module deep-merges it UNDER the environment overrides so the
 * precedence is `workflow YAML < profiles < environment overrides`, exactly matching Python's
 * `_deep_merge(profile_overrides, environment_overrides)`. DEFERRED still: manifest/`.env`
 * filesystem discovery and the override PROVENANCE records (the caller derives provenance from
 * the same composition it passes in).
 */

import { loadEnvironmentSpec, type ProjectEnvironmentSpec } from "./environment-spec.js";
import { type LoadYamlSpecOptions, loadYamlSpec } from "./loader.js";
import { deepMerge } from "./overrides.js";
import type { ProjectValidationIssue } from "./project-resolve.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import type { TypefluxYamlSpec } from "./spec.js";

/**
 * The environment's effective overrides for one workflow (Python `_merged_overrides`):
 * the environment-wide `overrides` deep-merged with the per-workflow `overrides`
 * (the workflow-level block wins per key). Both layers are already allow-list-bounded
 * by {@link projectEnvironmentSpec}; the result stays read-only (aliased subtrees,
 * like Python — see {@link deepMerge}).
 */
export function mergedEnvironmentOverrides(
  environment: ProjectEnvironmentSpec,
  workflowId: string,
): Record<string, unknown> {
  let overrides = deepMerge({}, environment.overrides);
  // `Object.hasOwn` (not `workflows[id]`) so a workflow keyed "constructor" /
  // "__proto__" resolves to its OWN entry, never an inherited prototype member.
  const workflow = Object.hasOwn(environment.workflows, workflowId) ? environment.workflows[workflowId] : undefined;
  if (workflow !== undefined) {
    overrides = deepMerge(overrides, workflow.overrides);
  }
  return overrides;
}

/** Stringify an env value at application time (Python `_stringify_env_value`). */
export function stringifyEnvValue(value: string | number | boolean): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/**
 * Apply an environment's `variables` onto `process.env` and return a RESTORE
 * function (#715 slice-5 fix round, items 2+7 — the ONE application helper the
 * `typeflux-yaml-worker` entrypoint and the erase CLI share, so the two can never
 * diverge on stringification or write discipline).
 *
 * Prior values are captured with `Object.hasOwn` (a key named `constructor` /
 * `__proto__` must read its OWN entry, never an inherited prototype member — the
 * same discipline {@link mergedEnvironmentOverrides} applies) and the restore
 * function puts them back exactly: keys that were unset are deleted, overwritten
 * ones are reset. A persistent adopter (the worker entrypoint, whose overlay
 * applies for the whole process lifetime) simply never calls the restore; a
 * scoped adopter (the erase CLI) calls it in `finally` so a later in-process run
 * against a DIFFERENT environment never inherits leftover values (Python's
 * `project_environment_context` parity).
 */
export function applyEnvironmentVariablesToProcessEnv(
  variables: Record<string, string | number | boolean>,
): () => void {
  const saved = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(variables)) {
    saved.set(key, Object.hasOwn(process.env, key) ? process.env[key] : undefined);
    process.env[key] = stringifyEnvValue(value);
  }
  return () => {
    for (const [key, prior] of saved) {
      if (prior === undefined) delete process.env[key];
      else process.env[key] = prior;
    }
  };
}

export interface EnvironmentInterpolationEnvOptions {
  /**
   * The base environment the variables/`.env` layer over (defaults to `process.env`,
   * matching Python's `os.environ` base and {@link loadYamlSpec}'s own default). Pass
   * `{}` for hermetic resolution against only the environment's declared values.
   */
  base?: Record<string, string | undefined>;
  /**
   * Values the caller read from the environment's `.env` files (Python
   * `_read_env_file`). Injected here rather than read, since this module is text-only.
   */
  envFileValues?: Record<string, string>;
}

/**
 * Build the `${VAR}` interpolation context from a project environment (Python
 * `build_project_environment_application` + `project_environment_context`). Precedence,
 * low → high: base env (`process.env`) < `.env` file values < inline `variables` —
 * so a declared variable wins over a stray process/`.env` value of the same name.
 *
 * With nothing to overlay the base is returned BY REFERENCE (like {@link loadYamlSpec}'s
 * own `env ?? process.env` default — no wasted copy of the whole environment). When
 * there is an overlay the base is shallow-copied first so it is never mutated, and each
 * overlay entry is written with `Object.defineProperty`: a variable / `.env` key is only
 * shape-checked against a POSIX-ish identifier (which admits `__proto__`), and a plain
 * `env[key] =` would route that through the prototype setter (dropping the value / risking
 * pollution) — a defined own property is safe and reads back for the interpolator.
 * Inherited-name safety (`${toString}` etc. resolving to a prototype member) is handled at
 * the correct depth by {@link interpolateEnv}'s own-property lookup, not this prototype.
 */
export function environmentInterpolationEnv(
  environment: ProjectEnvironmentSpec,
  options: EnvironmentInterpolationEnvOptions = {},
): Record<string, string | undefined> {
  const base = options.base ?? process.env;
  const { envFileValues } = options;
  const hasEnvFiles = envFileValues !== undefined && Object.keys(envFileValues).length > 0;
  const hasVariables = Object.keys(environment.variables).length > 0;
  if (!hasEnvFiles && !hasVariables) return base;
  const env: Record<string, string | undefined> = { ...base };
  const put = (key: string, value: string | undefined): void => {
    Object.defineProperty(env, key, { value, enumerable: true, writable: true, configurable: true });
  };
  if (envFileValues !== undefined) {
    for (const [key, value] of Object.entries(envFileValues)) put(key, value);
  }
  for (const [key, value] of Object.entries(environment.variables)) put(key, stringifyEnvValue(value));
  return env;
}

export interface ResolveEnvironmentWorkflowOptions {
  /** The loaded environment whose overrides + variables overlay the workflow. */
  environment: ProjectEnvironmentSpec;
  /** The workflow id being resolved (selects the per-workflow override block). */
  workflowId: string;
  /**
   * Project-wide runtime defaults beneath the workflow YAML (Python
   * `project.defaults.runtime`). The caller supplies it from the loaded project spec;
   * an empty map no-ops.
   */
  runtimeDefaults?: Record<string, unknown>;
  /** Values read from the environment's `.env` files (see {@link environmentInterpolationEnv}). */
  envFileValues?: Record<string, string>;
  /** The base interpolation environment (defaults to `process.env`). */
  baseEnv?: Record<string, string | undefined>;
  /**
   * The composed component-PROFILE override fragment (Python `_resolved_profile_overrides`'s
   * merged overrides — the `{ runtime: {...} }` from {@link composeProfileOverrides}). Slots
   * BENEATH the environment overrides: `profileOverrides < environment overrides`, mirroring
   * Python `_deep_merge(profile_overrides, environment_overrides)`. Omitted / empty no-ops.
   */
  profileOverrides?: Record<string, unknown>;
  /** A label for error messages (e.g. the workflow file path). */
  sourceLabel?: string;
}

/**
 * Resolve a workflow spec under a project environment (the injection-based core of
 * Python `resolve_project_workflow`): compute the environment's merged overrides for
 * the workflow, build the interpolation context from its variables/`.env` values, and
 * apply both through {@link loadYamlSpec}.
 *
 * Precedence, low → high: `runtimeDefaults` < workflow YAML < profiles < environment overrides.
 * The composed profile fragment (`profileOverrides`) is deep-merged BENEATH the environment
 * overrides here (Python `_deep_merge(profile_overrides, environment_overrides)`). Overrides are
 * applied before interpolation, so an override value may reference `${VAR}` resolved from the
 * environment's variables.
 */
export function resolveEnvironmentWorkflow(
  workflowText: string,
  options: ResolveEnvironmentWorkflowOptions,
): TypefluxYamlSpec {
  const { environment, workflowId, runtimeDefaults, envFileValues, baseEnv, profileOverrides, sourceLabel } = options;
  // Build each options bag by presence, not by writing explicit `undefined` — the
  // package's `exactOptionalPropertyTypes` distinguishes an omitted key from `undefined`.
  const envOptions: EnvironmentInterpolationEnvOptions = {};
  if (baseEnv !== undefined) envOptions.base = baseEnv;
  if (envFileValues !== undefined) envOptions.envFileValues = envFileValues;
  const environmentOverrides = mergedEnvironmentOverrides(environment, workflowId);
  const loadOptions: LoadYamlSpecOptions = {
    // Profiles < environment overrides: deep-merge the profile fragment first, env on top
    // (Python `_deep_merge(profile_overrides, environment_overrides)`). An empty/omitted
    // fragment leaves the environment overrides untouched.
    overrides:
      profileOverrides !== undefined && Object.keys(profileOverrides).length > 0
        ? deepMerge(profileOverrides, environmentOverrides)
        : environmentOverrides,
    env: environmentInterpolationEnv(environment, envOptions),
  };
  if (runtimeDefaults !== undefined) loadOptions.runtimeDefaults = runtimeDefaults;
  if (sourceLabel !== undefined) loadOptions.sourceLabel = sourceLabel;
  return loadYamlSpec(workflowText, loadOptions);
}

/**
 * The environment's references to workflows the project does not declare (Python
 * `_validate_environment_workflow_ids`). Returns one {@link ProjectValidationIssue} per
 * unknown id rather than throwing — the same typed shape (code + message + reference) as
 * {@link validateProjectPolicyReferences}, so the bundle validator concatenates it with
 * the policy reference checks uniformly. An empty array means every per-workflow override
 * targets a declared project workflow.
 */
export function validateEnvironmentWorkflowReferences(
  project: TypefluxProjectSpec,
  environment: ProjectEnvironmentSpec,
): ProjectValidationIssue[] {
  const declared = new Set(project.workflows.map((workflow) => workflow.id));
  const issues: ProjectValidationIssue[] = [];
  for (const workflowId of Object.keys(environment.workflows)) {
    if (!declared.has(workflowId)) {
      issues.push({
        code: "unknown_environment_workflow",
        message: `environment '${environment.name}' references unknown workflow: ${workflowId}`,
        reference: environment.name,
      });
    }
  }
  return issues;
}

/**
 * Convenience over {@link resolveEnvironmentWorkflow} that also parses the environment
 * from its YAML text (Python `load_project_environment` + `resolve_project_workflow`).
 * Both the workflow and the environment are supplied as text — this remains a text-only
 * injection point (no filesystem access).
 */
export function resolveEnvironmentWorkflowFromText(
  workflowText: string,
  environmentText: string,
  options: Omit<ResolveEnvironmentWorkflowOptions, "environment"> & { environmentSourceLabel?: string },
): TypefluxYamlSpec {
  const { environmentSourceLabel, ...rest } = options;
  const environment = loadEnvironmentSpec(
    environmentText,
    environmentSourceLabel !== undefined ? { sourceLabel: environmentSourceLabel } : {},
  );
  return resolveEnvironmentWorkflow(workflowText, { ...rest, environment });
}
