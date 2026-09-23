/**
 * Project-level policy RESOLUTION (governance parity, #454; Python
 * `project/policy.py` `compose_project_policies` + `_resolve_policy_closure`, and
 * `project/loader.py` `_validate_targets` + `_validate_policy_compositions`). Ties
 * the project manifest's policy references to the pure composition core: resolves
 * each policy's `extends` closure and composes the selected set into a
 * `ComposedProjectPolicy` ready for `enforcePolicyCompliance`.
 *
 * INJECTION-BASED — the TS SDK reads no files. The caller loads the referenced
 * policy specs (via `loadPolicySpec` on text it read) and provides them as
 * `sources`; this module resolves + composes + validates in-memory. A filesystem
 * loader that walks a manifest tree and builds `sources` is a thin, separate Node
 * helper (a later slice), so the core stays pure and testable.
 */

import { composeProjectPolicies, ProjectPolicyError } from "./policy-composition.js";
import type { AppliedPolicy, ComposedProjectPolicy } from "./policy-composition.js";
import type { TypefluxProjectPolicySpec } from "./policy.js";
import type { TypefluxProjectSpec } from "./project-spec.js";

/** A structural/semantic problem found while resolving a project (Python `ProjectValidationIssue`). */
export interface ProjectValidationIssue {
  code: string;
  message: string;
  reference?: string;
  /** The workflow's manifest reference, on `resolved_*_failed` issues (Python sets the
   * resolved absolute path; the injection SDK carries the reference — #565). */
  path?: string;
}

/** The in-memory policy specs the resolver needs, keyed by the project policy id. */
export interface ProjectPolicySources {
  /** Loaded policy specs keyed by their project policy id (as declared in `project.policies`). */
  policies: Readonly<Record<string, TypefluxProjectPolicySpec>>;
}

/** A local policy id is a reference, not a path/URL (Python `_validate_policy_id`). */
export function assertPolicyId(id: string): void {
  if (id.length === 0 || id.trim() !== id) {
    throw new ProjectPolicyError("policy id must be non-empty and trimmed");
  }
  if (/[/\\:]/.test(id)) {
    throw new ProjectPolicyError(`policy ids must be local project references, not paths or URLs: ${id}`);
  }
}

/**
 * Resolve the transitive `extends` closure of the selected policy ids (Python
 * `_resolve_policy_closure`): a post-order DFS so an extended (parent) policy is
 * applied BEFORE the policy that extends it, deduped, with cycle detection. The
 * ordered result feeds {@link composeProjectPolicies} (base constraints first, then
 * the overriding layers). Throws {@link ProjectPolicyError} on an unknown id, a
 * name/id mismatch, or an `extends` cycle.
 */
export function resolveProjectPolicyClosure(
  project: TypefluxProjectSpec,
  sources: ProjectPolicySources,
  policyIds: readonly string[],
): AppliedPolicy[] {
  const applied: AppliedPolicy[] = [];
  const appliedIds = new Set<string>();
  const visiting: string[] = [];

  const visit = (policyId: string): void => {
    assertPolicyId(policyId);
    if (appliedIds.has(policyId)) return;
    if (visiting.includes(policyId)) {
      throw new ProjectPolicyError(`project policy extends cycle detected: ${[...visiting, policyId].join(" -> ")}`);
    }
    // The manifest is the authority on which policies exist: an id (or an `extends`
    // parent) not DECLARED in `project.policies` is unknown, even if the caller
    // over-provided it in `sources` (Python `load_project_policy` KeyErrors on an
    // undeclared id; Bugbot). Own-key lookups guard a prototype-named id.
    if (!Object.hasOwn(project.policies, policyId)) {
      throw new ProjectPolicyError(`unknown project policy: ${policyId}`);
    }
    const spec = Object.hasOwn(sources.policies, policyId) ? sources.policies[policyId] : undefined;
    if (spec === undefined) {
      throw new ProjectPolicyError(`policy source not provided for declared policy: ${policyId}`);
    }
    // The loaded spec's own name must match the id it is declared/keyed under
    // (Python `load_project_policy`), so a mis-filed source can't silently apply.
    if (spec.name !== policyId) {
      throw new ProjectPolicyError(`policy '${policyId}' name must match the project policy id: '${spec.name}'`);
    }
    visiting.push(policyId);
    for (const parentId of spec.extends) {
      visit(parentId);
    }
    visiting.pop();
    if (!appliedIds.has(policyId)) {
      applied.push({ id: policyId, spec });
      appliedIds.add(policyId);
    }
  };

  for (const policyId of policyIds) {
    visit(policyId);
  }
  return applied;
}

/**
 * Compose the selected project policy ids into one {@link ComposedProjectPolicy},
 * resolving each policy's `extends` closure first, over ONLY the project's DECLARED
 * policies (Python `compose_project_policies`). The result is ready for
 * `enforcePolicyCompliance` — the governance seam — so it must never apply a policy
 * the manifest did not declare, regardless of what `sources` contains (Bugbot).
 */
export function composeProjectPolicyIds(
  project: TypefluxProjectSpec,
  sources: ProjectPolicySources,
  policyIds: readonly string[],
): ComposedProjectPolicy {
  if (policyIds.length === 0) {
    throw new ProjectPolicyError("at least one project policy id is required for composition");
  }
  const applied = resolveProjectPolicyClosure(project, sources, policyIds);
  return composeProjectPolicies(applied, [...policyIds]);
}

/**
 * Structural + composition validation of a project's policy/target references
 * (Python `_validate_targets` + `_validate_policy_compositions`), injection-based:
 * every validation target must reference DECLARED workflows/environments/policies,
 * each declared policy must compose (its `extends` closure resolves), and each
 * target's policy set must compose. Filesystem checks (workflow/env/profile files)
 * and per-workflow enforcement are separate slices. Returns every issue (empty = clean).
 */
export function validateProjectPolicyReferences(
  project: TypefluxProjectSpec,
  sources: ProjectPolicySources,
): ProjectValidationIssue[] {
  const issues: ProjectValidationIssue[] = [];
  const workflowIds = new Set(project.workflows.map((workflow) => workflow.id));
  const environmentIds = new Set(Object.keys(project.environments));
  const policyIds = new Set(Object.keys(project.policies));

  // Structural: every validation target references only declared ids.
  const targetsWithUnknownPolicy = new Set<string>();
  for (const [targetName, target] of Object.entries(project.validation.targets)) {
    for (const workflowId of target.workflows) {
      if (!workflowIds.has(workflowId)) {
        issues.push({
          code: "unknown_target_workflow",
          message: `validation target '${targetName}' references unknown workflow: ${workflowId}`,
          reference: targetName,
        });
      }
    }
    if (target.environment !== undefined && !environmentIds.has(target.environment)) {
      issues.push({
        code: "unknown_target_environment",
        message: `validation target '${targetName}' references unknown environment: ${target.environment}`,
        reference: targetName,
      });
    }
    for (const policyId of target.policies) {
      if (!policyIds.has(policyId)) {
        targetsWithUnknownPolicy.add(targetName);
        issues.push({
          code: "unknown_target_policy",
          message: `validation target '${targetName}' references unknown policy: ${policyId}`,
          reference: targetName,
        });
      }
    }
  }

  // Composition: each declared policy composes on its own (extends closure resolves).
  // `composeProjectPolicyIds` enforces declared-only resolution itself, so an
  // over-provided undeclared `extends` parent surfaces here as an "unknown project
  // policy" composition failure (Bugbot) — no separate source filter needed.
  const invalidPolicyIds = new Set<string>();
  for (const policyId of policyIds) {
    if (!Object.hasOwn(sources.policies, policyId)) {
      // Declared, but no source provided — the injection analogue of Python's
      // filesystem `missing_policy_file` (a distinct signal from a broken composition).
      invalidPolicyIds.add(policyId);
      issues.push({
        code: "missing_policy_source",
        message: `policy '${policyId}' is declared but no source was provided`,
        reference: policyId,
      });
      continue;
    }
    try {
      composeProjectPolicyIds(project, sources, [policyId]);
    } catch (error) {
      invalidPolicyIds.add(policyId);
      issues.push({
        code: "invalid_policy_composition",
        message: `policy '${policyId}' failed to compose: ${errorMessage(error)}`,
        reference: policyId,
      });
    }
  }

  // Composition: each target's policy SET composes (a most-restrictive conflict
  // between two policies only surfaces when they compose together). Skip targets
  // that already have an unknown or individually-invalid member policy.
  for (const [targetName, target] of Object.entries(project.validation.targets)) {
    if (target.policies.length === 0) continue;
    if (targetsWithUnknownPolicy.has(targetName)) continue;
    if (target.policies.some((policyId) => invalidPolicyIds.has(policyId))) continue;
    try {
      composeProjectPolicyIds(project, sources, target.policies);
    } catch (error) {
      issues.push({
        code: "invalid_target_policy_composition",
        message: `validation target '${targetName}' policies failed to compose: ${errorMessage(error)}`,
        reference: targetName,
      });
    }
  }

  return issues;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
