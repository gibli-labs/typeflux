/**
 * Environment DEFINITION projection for the control-plane explorer (#620; Python
 * `project/definitions.py` `environment_definition` + `EnvironmentDefinition`). Renders one
 * declared environment's overlay read-only: its variable names, override map, per-workflow
 * profile selections, and the reverse index of workflows that USE it (its own per-workflow
 * overrides plus the validation targets that select it).
 *
 * The overrides are config, not secrets (secrets appear as `value_from` references resolved at
 * runtime), so `overrides` renders in full — matching Python, which dumps the raw override map.
 */

import type {
  ProjectBundleSources,
  ProjectEnvironmentSpec,
  TypefluxProjectSpec,
} from "@typeflux/temporal-yaml";

import { ProjectControlPlaneError } from "./errors.js";

const asc = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * One environment's full definition (Python `EnvironmentDefinition`). `profile_path` is the
 * manifest-relative source path (masked as a machine-specific value in the conformance suite);
 * `used_by` is sorted. `env_files` mirrors Python's `entry.model_dump(exclude={"resolved_path"})`.
 */
export interface ApiEnvironmentDefinition {
  id: string;
  name: string;
  profile_path: string;
  env_files: Record<string, unknown>[];
  variable_names: string[];
  overrides: Record<string, unknown>;
  workflow_profiles: Record<string, Record<string, string>>;
  used_by: string[];
}

/**
 * Project one declared environment into its definition (Python `environment_definition`).
 * The caller has already 404'd an undeclared id; a declared-but-unsourced environment is a
 * 422 config error (the source failed to load), mirroring Python's load-then-project path.
 *
 * `used_by` = the environment's OWN per-workflow overrides ∪ every validation target that
 * selects this environment — the same union Python computes. `variable_names` and
 * `workflow_profiles` are sorted for a stable, diff-friendly response.
 */
export function buildEnvironmentDefinition(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  environmentId: string,
): ApiEnvironmentDefinition {
  const environment: ProjectEnvironmentSpec | undefined = Object.hasOwn(sources.environments, environmentId)
    ? sources.environments[environmentId]
    : undefined;
  if (environment === undefined) {
    throw new ProjectControlPlaneError(`no source provided for environment '${environmentId}'`, 422);
  }

  const used = new Set<string>(Object.keys(environment.workflows));
  for (const target of Object.values(project.validation.targets)) {
    if (target.environment === environmentId) {
      for (const workflowId of target.workflows) used.add(workflowId);
    }
  }

  return {
    id: environmentId,
    name: environment.name,
    // The manifest-relative source path (Python `environment.profile_path`); "" when absent
    // matches Python's default profile path never being null on a loaded environment.
    profile_path: project.environments[environmentId] ?? "",
    // Python dumps each env-file entry minus its resolved absolute path; the TS spec carries no
    // resolved path, so `{path, required}` IS the JSON shape (round-tripped via the spec fields).
    env_files: environment.env_files.map((entry) => ({ path: entry.path, required: entry.required })),
    variable_names: Object.keys(environment.variables).sort(asc),
    overrides: environment.overrides,
    workflow_profiles: Object.fromEntries(
      Object.entries(environment.workflows)
        .sort(([a], [b]) => asc(a, b))
        .map(([workflowId, spec]) => [workflowId, { ...spec.profiles }]),
    ),
    used_by: [...used].sort(asc),
  };
}
