/**
 * Shared project-workflow resolution for the deployment-tier bins (deployment tier, #687):
 * the `typeflux-project` deploy CLI and the `typeflux-yaml-worker` worker entrypoint both
 * resolve a project workflow's spec under an environment the SAME way — compose the workflow's
 * + environment's component profiles, then apply the environment overlay. Factored here so the
 * two bins never diverge on resolution (Python `resolve_project_workflow`).
 */

import { composeProfileOverrides, type ProfileSourceIndex } from "./profile.js";
import type { DeploymentResolvedWorkflow } from "./deployment.js";
import type { DeploymentPlanResolver } from "./deployment-plans.js";
import { projectSubworkflowResolver } from "./build-workflow.js";
import { resolveEnvironmentWorkflow } from "./environment-overlay.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import type { ProjectBundleSources } from "./project-validation.js";
import type { TypefluxYamlSpec } from "./spec.js";

/**
 * Resolve one workflow's spec + environment metadata under an environment — the SAME resolution
 * the `typeflux-project deploy` bin and the `typeflux-yaml-worker` entrypoint run, so a plan built
 * from this export reproduces CLI-identical `spec_digest`s (Python `resolve_project_workflow`;
 * #757 item 2 makes it a public export of `@typeflux/temporal-yaml`).
 *
 * COMPOSITION ORDER (the step consumers reimplementing from lower-level primitives MISS): this
 * composes the workflow's + environment's component PROFILE selections via
 * {@link composeProfileOverrides} and feeds the result to `resolveEnvironmentWorkflow` as
 * `profileOverrides`, BEFORE the environment overlay. A twin that calls `resolveEnvironmentWorkflow`
 * without that step silently DIVERGES from the CLI the moment the bundle declares any profile
 * (project `profiles`, a workflow's `profiles`, or an environment's per-workflow `profiles`) — it
 * would resolve a different spec, hence a different digest, and its committed plans would fail
 * verification against the CLI. This was the concrete risk `assertDeploymentResolverParity` in
 * an adopter's deployment wrapper guarded against; adopting THIS export removes the need
 * for that reimplementation. Returns `undefined` for an unknown workflow/environment.
 *
 * HERMETIC INTERPOLATION (#760): `env` is the base environment `${VAR}` spec references
 * interpolate against — it defaults to `process.env`, so an omitted `env` is a zero-behavior-change
 * no-op. A consumer emitting COMMITTED, machine-independent artifacts (deployment plans/renders,
 * whose bytes must not carry the operator's shell) passes a fixed map so the operator's
 * `process.env` never leaks into the resolved spec — the same guarantee an adopter's
 * `withHermeticResolutionEnv` global-`process.env` swap gave, without the global mutation or
 * sync-only assumption. When `env` is provided the interpolation is COMPLETELY hermetic: it flows
 * to {@link resolveEnvironmentWorkflow} as `baseEnv`, and a `${VAR}` whose name is absent from the
 * injected map errors exactly as an unset shell variable would (no silent `process.env` fallback).
 * The environment's `variables:` overlay still layers OVER this base (overlay wins), matching the
 * overlay-over-`process.env` precedence the non-hermetic path already has.
 */
export function resolveProjectWorkflow(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  workflowId: string,
  environmentId: string,
  env?: Record<string, string | undefined>,
): DeploymentResolvedWorkflow | undefined {
  const workflow = project.workflows.find((entry) => entry.id === workflowId);
  if (workflow === undefined || !Object.hasOwn(project.environments, environmentId)) return undefined;
  const environment = Object.hasOwn(sources.environments, environmentId) ? sources.environments[environmentId] : undefined;
  const workflowText = Object.hasOwn(sources.workflows, workflowId) ? sources.workflows[workflowId] : undefined;
  if (environment === undefined || workflowText === undefined) return undefined;
  const index: ProfileSourceIndex = {
    specs: sources.profiles,
    paths: project.profiles ?? { provider: {}, registry: {}, runtime: {} },
  };
  const { overrides } = composeProfileOverrides(index, {
    workflowSelection: { ...(workflow.profiles ?? {}) },
    environmentSelection: { ...(environment.workflows[workflowId]?.profiles ?? {}) },
    workflowContext: `workflow '${workflowId}' profile selection`,
    environmentContext: `environment '${environment.name}' profile selection for '${workflowId}'`,
  });
  // `exactOptionalPropertyTypes`: set `baseEnv` by presence, not by writing an explicit
  // `undefined` — an omitted `env` must leave `resolveEnvironmentWorkflow` on its `process.env`
  // default, while an injected `{}` must mean "hermetic against nothing" (every `${VAR}` errors).
  const resolveOptions: Parameters<typeof resolveEnvironmentWorkflow>[1] = {
    environment,
    workflowId,
    runtimeDefaults: project.defaults.runtime,
    profileOverrides: overrides,
    sourceLabel: `${environmentId}:${workflowId}`,
  };
  if (env !== undefined) resolveOptions.baseEnv = env;
  const spec: TypefluxYamlSpec = resolveEnvironmentWorkflow(workflowText, resolveOptions);
  return {
    spec,
    environmentName: environment.name,
    workflowPath: workflow.path ?? `${workflow.directory}/${project.defaults.workflow_filename}`,
    variables: environment.variables,
  };
}

/**
 * A `DeploymentPlanResolver` (spec + sub-workflow resolver) for the plan writer/verifier, wrapping
 * {@link resolveProjectWorkflow} with the sub-workflow closure resolver so a parent's plan digest
 * reflects its transitive closure (#757 item 2; Python `plan_resolver_for`). Use this — rather than
 * reimplementing the resolver from lower-level exports — so a consumer's committed plans verify
 * identically to CLI-written ones, INCLUDING under component-profile composition (see the divergence
 * note on {@link resolveProjectWorkflow}).
 *
 * `env` (#760) is the hermetic interpolation base (see {@link resolveProjectWorkflow}); it is
 * threaded to BOTH the parent and every sub-workflow resolution, so a parent's plan digest — which
 * folds in its transitive closure — is computed against the SAME injected environment throughout.
 * Omitted, both resolve against `process.env` (zero behavior change).
 */
export function planResolverFor(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  env?: Record<string, string | undefined>,
): DeploymentPlanResolver {
  return (workflowId, environmentId) => {
    const resolved = resolveProjectWorkflow(project, sources, workflowId, environmentId, env);
    if (resolved === undefined) return undefined;
    const subworkflows = projectSubworkflowResolver(
      workflowId,
      (siblingId) => resolveProjectWorkflow(project, sources, siblingId, environmentId, env)?.spec,
    );
    return { spec: resolved.spec, subworkflows };
  };
}
