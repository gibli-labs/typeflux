import { resolveProjectWorkflow } from "@typeflux/yaml/project-run";
export { resolveProjectWorkflow } from "@typeflux/yaml/project-run";
import type { DeploymentPlanResolver } from "./deployment-plans.js";
import { projectSubworkflowResolver } from "@typeflux/yaml/build-workflow";
import type { TypefluxProjectSpec } from "./project-spec.js";
import type { ProjectBundleSources } from "./project-validation.js";

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
