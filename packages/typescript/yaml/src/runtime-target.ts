/** Shared file/project target resolution for foreground and worker entrypoints. */
import { readFileSync } from "node:fs";
import { loadYamlSpec } from "./loader.js";
import { loadProjectBundle } from "./project-fs-loader.js";
import { resolveProjectWorkflow } from "./project-run.js";
import { applyEnvironmentVariablesToProcessEnv } from "./environment-overlay.js";
import { projectSubworkflowResolver, type SubworkflowSpecResolver } from "./build-workflow.js";
import { composeProjectPolicyIds } from "./project-resolve.js";
import { assertRiskTierEnforced, selectProjectPolicyIdsForWorkflow } from "./project-enforcement.js";
import type { ComposedProjectPolicy } from "./policy-composition.js";
import type { TypefluxYamlSpec } from "./spec.js";

export interface RuntimeTargetArgs {
  manifest?: string;
  yamlPath?: string;
  workflow?: string;
  environment?: string;
  policies: string[];
  expectPolicyHash?: string;
}

/** Resolve the target spec (+ optional composed policy and sub-workflow resolver) for either mode. */
export function resolveRuntimeTarget(args: RuntimeTargetArgs): {
  spec: TypefluxYamlSpec;
  policy?: ComposedProjectPolicy;
  subworkflows?: SubworkflowSpecResolver;
} {
  if (args.manifest === undefined) {
    const specPath = args.yamlPath;
    if (specPath === undefined || specPath === "") {
      throw new Error("no project manifest argument and no TYPEFLUX_YAML_PATH set; nothing to run");
    }
    const spec = loadYamlSpec(readFileSync(specPath, "utf-8"), { sourceLabel: specPath });
    return { spec };
  }
  if (args.workflow === undefined || args.environment === undefined) {
    throw new Error("project mode requires --workflow and --environment");
  }
  const { project, sources } = loadProjectBundle(args.manifest);
  // The target environment's variable overlay applies to the WHOLE worker lifetime
  // (Bugbot #706): deploy admission and Python's project `run` evaluate policy —
  // including secret/env-backed checks — under this overlay, and the rendered pod
  // gets the same values via its ConfigMap. A dedicated worker process adopts them
  // persistently (overlay wins, matching withEnvironmentContext precedence) so the
  // policy hash it verifies and the runtime it assembles agree with the plan it
  // was deployed with.
  const environmentSpec = Object.hasOwn(sources.environments, args.environment)
    ? sources.environments[args.environment]
    : undefined;
  if (environmentSpec !== undefined) {
    // Shared applier (the restore function is deliberately dropped: the worker
    // adopts the overlay for its whole lifetime, per the comment above).
    applyEnvironmentVariablesToProcessEnv(environmentSpec.variables);
  }
  const resolved = resolveProjectWorkflow(project, sources, args.workflow, args.environment);
  if (resolved === undefined) {
    throw new Error(`cannot resolve workflow '${args.workflow}' under environment '${args.environment}'`);
  }
  const selected = selectProjectPolicyIdsForWorkflow(project, {
    environmentId: args.environment,
    workflowId: args.workflow,
    explicitPolicyIds: args.policies,
  });
  const policy = selected.length > 0 ? composeProjectPolicyIds(project, sources, selected) : undefined;
  if (args.expectPolicyHash !== undefined) {
    if (policy === undefined) {
      throw new Error("expected project policy hash was provided, but no project policy was selected");
    }
    if (policy.policyHash !== args.expectPolicyHash) {
      throw new Error(
        `selected project policy hash does not match expected deployment policy hash ` +
          `(expected=${args.expectPolicyHash}, actual=${policy.policyHash})`,
      );
    }
  }
  const subworkflows = projectSubworkflowResolver(
    args.workflow,
    (siblingId) => resolveProjectWorkflow(project, sources, siblingId, args.environment!)?.spec,
  );
  // #788 (review M3): the worker's project mode composes its own policy object and
  // previously bypassed the risk-tier binding check entirely — an elevated declared
  // tier (parent or closure) must fail closed here exactly like the Python worker.
  assertRiskTierEnforced(
    resolved.spec,
    selected,
    policy?.payload,
    (siblingId) => resolveProjectWorkflow(project, sources, siblingId, args.environment!)?.spec,
    args.workflow,
  );
  return policy !== undefined ? { spec: resolved.spec, policy, subworkflows } : { spec: resolved.spec, subworkflows };
}

