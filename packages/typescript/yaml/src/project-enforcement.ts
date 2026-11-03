/**
 * Project-level policy ENFORCEMENT binding (governance parity, #454; Python
 * `project/policy_enforcement.py` `select_project_policy_ids_for_workflow` +
 * `build_project_policy_runtime_guard`, and the policy slice of
 * `project/validation.py` `_validate_resolved_workflow`).
 *
 * The glue between the bundle layer and the enforcement core: a project's
 * `validation.targets` BIND policies to (workflow, environment) pairs. This module
 * selects the applicable policy ids, composes them (via {@link composeProjectPolicyIds}),
 * and either reports the per-dimension compliance checks or builds the per-call
 * {@link RuntimePolicyGuard}. INJECTION-BASED: the caller supplies the resolved workflow
 * `spec` (from {@link resolveEnvironmentWorkflow}) and the loaded policy `sources`; no
 * filesystem access.
 */

import type { ComposedProjectPolicy } from "./policy-composition.js";
import {
  collectCompositionMetrics,
  evaluateRiskTier,
  type Payload,
  type PolicyValidationCheck,
  ProjectPolicyEnforcementError,
  type RiskTierCascade,
  riskTierCascade,
  type RiskTierEvaluation,
  riskTierFailures,
  RuntimePolicyGuard,
  tierRank,
  validatePolicyCompliance,
} from "./policy-enforcement.js";
import { collectSubworkflowReferences } from "./project-validation.js";
import { composeProjectPolicyIds, type ProjectPolicySources } from "./project-resolve.js";
import type { TypefluxProjectSpec } from "./project-spec.js";
import { type TypefluxYamlSpec } from "./spec.js";

/**
 * Resolve a sibling workflow's spec under the SAME environment as the parent — the
 * closure walk uses it to pull in each referenced child (Python resolves via
 * `resolve_project_workflow`; the TS enforcement core is injection-based, so the caller
 * — `runWorkflowValidation` / the CP — supplies the resolver). `undefined` for an
 * unknown/unresolvable id (already a `workflow_graph` failure).
 */
export type SubworkflowClosureSpecResolver = (workflowId: string) => TypefluxYamlSpec | undefined;

/**
 * Transitive-closure admission (#55 §9, governance closure): validate every
 * transitively-referenced sub-workflow's resolved spec against the PARENT's composed
 * policy. A parent pins a specific child plan + digest, so admitting the parent must
 * guarantee the WHOLE composed program is compliant — not rely on each child being
 * separately admitted (possibly under a laxer policy, or none). Returns `undefined`
 * ONLY when the spec references no sub-workflows (the V1 path — validate output stays
 * byte-identical); otherwise a passed/failed `policy_subworkflow_closure` check
 * aggregating child violations. STRUCTURALLY FAIL-CLOSED: a composed spec with no
 * resolver supplied is a FAILED check naming the missing resolver — never silence —
 * mirroring Python's guarantee (its guard always resolves children itself). The graph
 * layer already rejects reference cycles; the `visited` set guards this walk regardless.
 */
/**
 * BFS over the transitive sub-workflow closure (Python `_iter_subworkflow_closure`),
 * yielding one `{ref, child}` per unique member (the parent itself is not yielded);
 * an unresolvable reference yields `child: undefined` and is not expanded further.
 * The ONE walk — visit order, dedup, and error semantics — shared by the closure
 * admission check, the bundle risk-tier projection, and the deployment tier's
 * observability-consistency gate (#757 review), so the consumers can never drift
 * (#300 slice 2 review). A resolver THROW propagates to the caller (the admission
 * path's declared-but-broken-sibling behavior); a caller that wants to tolerate it
 * wraps its resolver. Cycles cannot loop it: `visited` is the belt-and-braces guard.
 */
export function* walkSubworkflowClosure(
  spec: TypefluxYamlSpec,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver,
  parentWorkflowId: string,
): Generator<{ ref: string; child: TypefluxYamlSpec | undefined }> {
  // Seed with the parent's MANIFEST workflow id (Python `resolved.workflow_id`) — sub-workflow
  // references are manifest ids, so seeding with `spec.name` (the workflow's name field) could
  // skip a legitimate child whose id coincides with the parent's name, or miss a self-reference.
  const visited = new Set<string>([parentWorkflowId]);
  const frontier = [...collectSubworkflowReferences(spec.workflow.steps)];
  while (frontier.length > 0) {
    const ref = frontier.shift() as string;
    if (visited.has(ref)) continue;
    visited.add(ref);
    const child = resolveSubworkflowSpec(ref);
    if (child === undefined) {
      yield { ref, child: undefined };
      continue;
    }
    frontier.push(...collectSubworkflowReferences(child.workflow.steps));
    yield { ref, child };
  }
}

export function validateSubworkflowClosurePolicy(
  spec: TypefluxYamlSpec,
  policy: ComposedProjectPolicy,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  parentWorkflowId: string,
): PolicyValidationCheck | undefined {
  const direct = collectSubworkflowReferences(spec.workflow.steps);
  if (direct.length === 0) return undefined;
  if (resolveSubworkflowSpec === undefined) {
    // Refs exist but no resolver: indistinguishable from "children never checked", so it
    // must fail closed — an omitted resolver would otherwise silently skip closure admission.
    const message =
      `workflow references sub-workflows (${direct.map((ref) => `'${ref}'`).join(", ")}) but no ` +
      "resolveSubworkflowSpec resolver was supplied — transitive-closure policy admission (#55 §9) " +
      "cannot run; supply the project's sibling resolver";
    return {
      code: "policy_subworkflow_closure",
      status: "failed",
      message,
      details: { referenced_workflows: [], unresolved_references: direct, policy_hash: policy.policyHash },
    };
  }

  const order: string[] = [];
  const unresolved: string[] = [];
  const failures: string[] = [];
  // Direct references of each resolved (or unresolvable) workflow id, for the
  // longest-path depth measurement below. The root seeds it with its own direct refs.
  const references = new Map<string, string[]>([[parentWorkflowId, [...direct]]]);
  // Per-member flattened step counts for the TREE-WIDE max_total_steps ceiling (#298):
  // unique members only (a diamond's shared child counts once — the composed program
  // contains one copy of its plan).
  const memberStepCounts = new Map<string, number>([
    [parentWorkflowId, collectCompositionMetrics(spec.workflow.steps).flattenedStepCount],
  ]);
  // Per-member EFFECTIVE risk tier for the closure cascade (#300 D300-3): each member's
  // declared tier lifted by the same policy floor. The parent's effective tier maxes over
  // every member's — a parent embedding a higher-tier child inherits at least that tier,
  // because the parent's run executes the child's effects.
  const memberRiskTiers = new Map<string, string>();
  const parentRisk = evaluateRiskTier(spec, policy.payload);
  if (parentRisk !== undefined) memberRiskTiers.set(parentWorkflowId, parentRisk.effective);
  // ONE closure walk (shared with the bundle risk-tier projection via
  // `walkSubworkflowClosure`): an unresolvable ref fails the closure below.
  for (const { ref, child } of walkSubworkflowClosure(spec, resolveSubworkflowSpec, parentWorkflowId)) {
    if (child === undefined) {
      unresolved.push(ref);
      references.set(ref, []);
      continue;
    }
    order.push(ref);
    for (const check of validatePolicyCompliance(child, policy)) {
      if (check.status === "failed") {
        // `||` (not `??`) mirrors Python's `check.message or …`, which also falls back
        // on an empty-string message — the truthiness-parity trap.
        failures.push(`sub-workflow '${ref}': ${check.code}: ${check.message || `policy check failed: ${check.code}`}`);
      }
    }
    references.set(ref, collectSubworkflowReferences(child.workflow.steps));
    memberStepCounts.set(ref, collectCompositionMetrics(child.workflow.steps).flattenedStepCount);
    const childRisk = evaluateRiskTier(child, policy.payload);
    if (childRisk !== undefined) memberRiskTiers.set(ref, childRisk.effective);
  }

  const details: Record<string, unknown> = { referenced_workflows: order, policy_hash: policy.policyHash };
  if (unresolved.length > 0) details["unresolved_references"] = unresolved;
  // A child that cannot be resolved is a FAIL, not a silent pass: at the runtime-guard admission
  // path this check is the only view of the sub-workflow tree (no workflow_graph check runs there),
  // so a dangling/malformed reference must fail closed rather than admit an un-evaluated child.
  const closureFailures = [
    ...failures,
    ...unresolved.map((ref) => `sub-workflow '${ref}' could not be resolved for policy closure`),
  ];

  const compositionPayload = policy.payload["composition"];
  const composition =
    typeof compositionPayload === "object" && compositionPayload !== null
      ? (compositionPayload as Record<string, unknown>)
      : {};

  // composition.max_total_steps ceiling (#298): TREE-WIDE flattened sum over the whole
  // closure (parent + every transitively referenced child, unique members once), so a
  // program split into many small sub-workflows cannot evade the per-member max_steps
  // bound by decomposition.
  const maxTotalSteps = composition["max_total_steps"];
  if (typeof maxTotalSteps === "number") {
    const totalSteps = [...memberStepCounts.values()].reduce((sum, count) => sum + count, 0);
    details["closure_total_steps"] = totalSteps;
    if (totalSteps > maxTotalSteps) {
      const breakdown = [...memberStepCounts.entries()]
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([member, count]) => `${member}: ${count}`)
        .join(", ");
      closureFailures.push(
        `sub-workflow closure total step count ${totalSteps} exceeds composition ceiling max_total_steps ${maxTotalSteps} (${breakdown})`,
      );
    }
  }

  // composition.max_subworkflow_depth ceiling (#298): the parent is depth 0, so the tree
  // depth is the longest chain length minus one (a diamond is bounded by its deepest arm).
  const maxSubDepth = composition["max_subworkflow_depth"];
  if (typeof maxSubDepth === "number") {
    const deepestChain = deepestReferenceChain(parentWorkflowId, references);
    const observedDepth = deepestChain.length - 1;
    details["subworkflow_depth"] = observedDepth;
    if (observedDepth > maxSubDepth) {
      closureFailures.push(
        `sub-workflow reference depth ${observedDepth} exceeds composition ceiling ${maxSubDepth} (deepest chain: ${deepestChain.join(" -> ")})`,
      );
    }
  }

  // Risk-tier cascade (#300 D300-3): the parent's effective tier maxes over every closure
  // member's effective tier. When a member LIFTS the parent above its own declared+floor
  // effective, the parent re-evaluates its macro requirements at the lifted tier — a safe
  // parent embedding a human_gated child must itself satisfy the human_gated controls (or
  // deny). Only the LIFT is reported here; the parent's own tier is covered by its check.
  // Shares `riskTierCascade` with the bundle projection so the two never drift.
  if (parentRisk !== undefined) {
    const cascade = riskTierCascade(parentWorkflowId, parentRisk, memberRiskTiers, spec, policy.payload);
    if (cascade !== undefined) {
      details["risk_tier_cascade"] = {
        parent_effective: cascade.parentEffective,
        cascade_effective: cascade.evaluation.effective,
        lifted_by: cascade.liftedBy,
        requirements: cascade.evaluation.requirements.map((req) => ({ name: req.name, satisfied: req.satisfied })),
      };
      for (const message of riskTierFailures(cascade.evaluation, false)) {
        closureFailures.push(`risk tier cascade from sub-workflow '${cascade.liftedBy}': ${message}`);
      }
    }
  }

  return closureFailures.length > 0
    ? { code: "policy_subworkflow_closure", status: "failed", message: closureFailures.join("; "), details }
    : { code: "policy_subworkflow_closure", status: "passed", details };
}

/** A workflow's full risk-tier posture for surfacing (#300 slice 2; Python `RiskTierPosture`). */
export interface RiskTierPosture {
  base: RiskTierEvaluation;
  cascade: RiskTierCascade | undefined;
}

/**
 * The effective risk tier of every sub-workflow closure member (#300 D300-3), keyed by
 * workflow id — consumes the SAME {@link walkSubworkflowClosure} walk as the closure
 * admission check, so the bundle projection and admission agree on the cascade. A
 * dangling / broken reference is skipped here (it fails the closure check on its own
 * path, not this read projection): `specFor` returns `undefined` for an undeclared id
 * and THROWS for a declared-but-broken sibling — the catching wrapper tolerates both
 * as "not a tier contributor" without changing the shared walk's semantics.
 */
function closureMemberEffectiveTiers(
  spec: TypefluxYamlSpec,
  policy: ComposedProjectPolicy,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  parentWorkflowId: string,
): Map<string, string> {
  const memberRiskTiers = new Map<string, string>();
  const parent = evaluateRiskTier(spec, policy.payload);
  if (parent !== undefined) memberRiskTiers.set(parentWorkflowId, parent.effective);
  if (resolveSubworkflowSpec === undefined) return memberRiskTiers;
  const tolerant: SubworkflowClosureSpecResolver = (workflowId) => {
    try {
      return resolveSubworkflowSpec(workflowId);
    } catch {
      return undefined;
    }
  };
  for (const { ref, child } of walkSubworkflowClosure(spec, tolerant, parentWorkflowId)) {
    if (child === undefined) continue;
    const childRisk = evaluateRiskTier(child, policy.payload);
    if (childRisk !== undefined) memberRiskTiers.set(ref, childRisk.effective);
  }
  return memberRiskTiers;
}

/**
 * The workflow's full risk-tier posture for surfacing (#300 slice 2; Python
 * `evaluate_workflow_risk_tier`): its own `base` evaluation plus the sub-workflow closure
 * `cascade` lift when a higher-tier child raises it. Returns `undefined` when the composed
 * policy declares no `risk_tiers` dimension. Shares {@link evaluateRiskTier} and
 * {@link riskTierCascade} with admission, so the bundle surfaces the SAME posture the
 * `policy_risk_tier` / `policy_subworkflow_closure` checks compute — never a second path.
 */
export function evaluateWorkflowRiskTier(
  spec: TypefluxYamlSpec,
  policy: ComposedProjectPolicy,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  parentWorkflowId: string,
): RiskTierPosture | undefined {
  const base = evaluateRiskTier(spec, policy.payload);
  if (base === undefined) return undefined;
  const memberRiskTiers = closureMemberEffectiveTiers(spec, policy, resolveSubworkflowSpec, parentWorkflowId);
  const cascade = riskTierCascade(parentWorkflowId, base, memberRiskTiers, spec, policy.payload);
  return { base, cascade };
}

/**
 * The longest root-to-leaf chain of workflow ids over the reference DAG (#298; Python
 * `_deepest_reference_chain`). Memoized per node (longest chain below a node is
 * path-independent in a DAG); the `visiting` set is a belt-and-braces cycle guard.
 */
function deepestReferenceChain(root: string, references: Map<string, string[]>): string[] {
  const cache = new Map<string, string[]>();
  const deepest = (node: string, visiting: ReadonlySet<string>): string[] => {
    const cached = cache.get(node);
    if (cached !== undefined) return cached;
    let best: string[] = [];
    for (const child of references.get(node) ?? []) {
      if (visiting.has(child)) continue;
      const sub = deepest(child, new Set([...visiting, node]));
      if (sub.length > best.length) best = sub;
    }
    const result = [node, ...best];
    cache.set(node, result);
    return result;
  };
  return deepest(root, new Set());
}

/** Order-preserving dedup (Python `_dedupe`) — first occurrence wins. */
const dedupe = (values: readonly string[]): string[] => [...new Set(values)];

export interface SelectProjectPolicyIdsOptions {
  environmentId: string;
  workflowId: string;
  /** An explicit override — when non-empty, replaces the target-derived selection (deduped). */
  explicitPolicyIds?: readonly string[];
}

/**
 * The project policy ids that apply to a workflow in an environment (Python
 * `select_project_policy_ids_for_workflow`). Explicit ids win outright (deduped);
 * otherwise every `validation.target` whose `environment` is unset OR matches, AND that
 * lists the workflow, contributes its policies — deduped in first-seen order.
 */
export function selectProjectPolicyIdsForWorkflow(
  project: TypefluxProjectSpec,
  options: SelectProjectPolicyIdsOptions,
): string[] {
  const explicit = options.explicitPolicyIds ?? [];
  if (explicit.length > 0) return dedupe(explicit);
  const selected: string[] = [];
  // Target order follows JS object-key order: insertion order for normal names (so it
  // matches Python's insertion-ordered dict), ascending for integer-like names — an
  // SDK-wide zod-record trait. It affects only the recorded id-list order, never the
  // enforced constraints: the policy merge is commutative (intersect / AND / min) and
  // the drift hash already sorts the id lists (#554).
  for (const target of Object.values(project.validation.targets)) {
    // An unset target environment applies to every environment; a set one must match.
    if (target.environment !== undefined && target.environment !== options.environmentId) continue;
    if (!target.workflows.includes(options.workflowId)) continue;
    selected.push(...target.policies);
  }
  return dedupe(selected);
}

export interface WorkflowPolicyEnforcementContext {
  /** The resolved workflow spec (from {@link resolveEnvironmentWorkflow}). */
  spec: TypefluxYamlSpec;
  /** The workflow id being enforced (selects the applicable policies). */
  workflowId: string;
  /** The environment id the workflow resolved under. */
  environmentId: string;
  /** An explicit policy-id override (see {@link selectProjectPolicyIdsForWorkflow}). */
  explicitPolicyIds?: readonly string[];
  /**
   * Resolves a referenced sub-workflow's spec under the same environment (#55 §9
   * transitive-closure admission). When supplied and the workflow references
   * sub-workflows, each child is re-validated against the parent's composed policy.
   * Omit for a non-composed workflow or when child resolution is unavailable.
   */
  resolveSubworkflowSpec?: SubworkflowClosureSpecResolver;
}

/**
 * The policy-compliance checks for one resolved workflow (Python
 * `_validate_resolved_workflow`, the policy slice). Selects the applicable policies;
 * with none selected, a single skipped check (parity with Python); otherwise composes
 * them — a composition failure surfaces as a `policy_composition` FAILED check rather
 * than throwing — and returns {@link validatePolicyCompliance}'s per-dimension checks.
 * Never throws; use {@link buildProjectPolicyRuntimeGuard} to fail closed.
 */
/** Tiers whose declaration is a governance INTENTION that must be enforced (#788).
 * `safe`/undeclared is a declaration that requires nothing, so it never fails. */
const ELEVATED_RISK_TIERS: ReadonlySet<string> = new Set(["policy_gated", "human_gated", "prohibited"]);

/** Fail closed when a declared elevated risk tier would run unenforced (#788, audit B1;
 * Python `_risk_tier_binding_check`). Two silent-inert cases: no policy selected for the
 * (workflow, environment), or a composed policy with no `risk_tiers` dimension — in both,
 * `evaluateRiskTier` has nothing in play and the declaration does nothing. */
/** The highest risk tier declared by the workflow OR its sub-workflow closure (#788):
 * a safe parent embedding an elevated child still executes the child's effects, so the
 * binding requirement follows the closure exactly like the #300 cascade. Without a
 * resolver the closure is invisible here — the same limitation the closure-admission
 * check has (Python resolves internally). */
function maxDeclaredClosureRiskTier(
  spec: TypefluxYamlSpec,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  parentWorkflowId: string,
): string | undefined {
  let best = spec.workflow.risk_tier as string | undefined;
  if (resolveSubworkflowSpec !== undefined) {
    const tolerant: SubworkflowClosureSpecResolver = (workflowId) => {
      try {
        return resolveSubworkflowSpec(workflowId);
      } catch {
        return undefined;
      }
    };
    for (const { child } of walkSubworkflowClosure(spec, tolerant, parentWorkflowId)) {
      if (child === undefined) continue;
      const declared = child.workflow.risk_tier as string | undefined;
      if (declared !== undefined && (best === undefined || tierRank(declared) > tierRank(best))) {
        best = declared;
      }
    }
  }
  return best;
}

function riskTierBindingCheck(
  spec: TypefluxYamlSpec,
  selected: readonly string[],
  policyPayload: unknown,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  parentWorkflowId: string,
): PolicyValidationCheck {
  const declared = maxDeclaredClosureRiskTier(spec, resolveSubworkflowSpec, parentWorkflowId);
  if (declared === undefined || !ELEVATED_RISK_TIERS.has(declared)) {
    // Bugbot (#788): with NO resolver, an elevated CHILD is invisible to the closure
    // walk — an unbound parent that references sub-workflows cannot be proven safe,
    // so it fails closed instead of silently passing (Python walks the project graph
    // internally and has no such blind spot; bound cases are backstopped by closure
    // admission, which always runs with a resolver on the validate/admission paths).
    if (
      selected.length === 0 &&
      resolveSubworkflowSpec === undefined &&
      collectSubworkflowReferences(spec.workflow.steps).length > 0
    ) {
      return {
        code: "risk_tier_binding",
        status: "failed",
        message:
          "workflow references sub-workflows but no sub-workflow resolver was provided and no " +
          "project policy is selected — the closure's declared risk tiers cannot be verified. " +
          "Provide a resolver (or bind a policy) so an elevated child cannot run unenforced.",
      };
    }
    return { code: "risk_tier_binding", status: "passed", message: "" };
  }
  if (selected.length === 0) {
    return {
      code: "risk_tier_binding",
      status: "failed",
      message:
        `workflow declares risk_tier '${declared}' (directly or via its sub-workflow closure) but no project policy is selected for this ` +
        "workflow/environment — the declared tier would run UNENFORCED. Bind the workflow in " +
        "project validation.targets (or pass explicit policy ids) to a policy whose risk_tiers " +
        "dimension defines the tier's requirements.",
    };
  }
  // Enforced means the evaluation at the closure-lifted tier yields a denial or at
  // least one requirement — a merely PRESENT `risk_tiers` key whose effective-tier
  // block demands nothing is still a gap (the hollow-dimension bypass, review M4).
  const evaluation = evaluateRiskTier(spec, policyPayload as Payload, declared);
  if (evaluation === undefined) {
    return {
      code: "risk_tier_binding",
      status: "failed",
      message:
        `workflow declares risk_tier '${declared}' (directly or via its sub-workflow closure) but the composed policy (${selected.join(", ")}) ` +
        "declares no risk_tiers dimension — the declared tier would run UNENFORCED " +
        "(evaluateRiskTier has nothing in play). Add a risk_tiers block to the policy.",
    };
  }
  if (!evaluation.denied && evaluation.requirements.length === 0) {
    return {
      code: "risk_tier_binding",
      status: "failed",
      message:
        `workflow declares risk_tier '${declared}' (directly or via its sub-workflow closure) but the composed policy (${selected.join(", ")}) ` +
        `defines no requirements for the effective tier '${evaluation.effective}' — the declared tier would run UNENFORCED ` +
        "(a hollow risk_tiers dimension). Add requirements (or a denial) for the tier.",
    };
  }
  return { code: "risk_tier_binding", status: "passed", message: "" };
}

/** Environments (other than `environmentId`) whose targets bind this workflow (#788;
 * Python `workflow_bound_in_other_environments`): distinguishes "bound elsewhere, just
 * not here" (normal multi-environment shape — validation reports without failing) from
 * "bound nowhere" (fails closed). */
function boundInOtherEnvironments(
  project: TypefluxProjectSpec,
  workflowId: string,
  environmentId: string,
): string[] {
  const environments: string[] = [];
  for (const target of Object.values(project.validation.targets)) {
    if (!target.workflows.includes(workflowId) || target.policies.length === 0) continue;
    if (target.environment !== undefined && target.environment !== environmentId) {
      environments.push(target.environment);
    }
  }
  return [...new Set(environments)];
}

/** The #788 binding verdict for callers outside this module's validation/guard paths
 * (the admission seam): the same check, exported. */
export function riskTierBindingVerdict(
  spec: TypefluxYamlSpec,
  selected: readonly string[],
  policyPayload: unknown,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  workflowId: string,
): PolicyValidationCheck {
  return riskTierBindingCheck(spec, selected, policyPayload, resolveSubworkflowSpec, workflowId);
}

/** Throw the #788 fail-close for callers outside the guard builder (the worker entry's
 * project mode composes its own policy object and must not bypass the binding check). */
export function assertRiskTierEnforced(
  spec: TypefluxYamlSpec,
  selected: readonly string[],
  policyPayload: unknown,
  resolveSubworkflowSpec: SubworkflowClosureSpecResolver | undefined,
  workflowId: string,
): void {
  const check = riskTierBindingCheck(spec, selected, policyPayload, resolveSubworkflowSpec, workflowId);
  if (check.status === "failed") {
    throw new ProjectPolicyEnforcementError(
      `project policy enforcement failed: ${check.code}: ${check.message}`,
      [check],
    );
  }
}

/** The validation face of the unbound branch (#788): a workflow whose elevated tier is
 * bound only in OTHER environments' targets reports `skipped` naming them (the normal
 * multi-environment shape; Python parity) — starting it here still fails closed at
 * guard build, which stays strict. */
function softenedUnboundBindingCheck(
  project: TypefluxProjectSpec,
  context: WorkflowPolicyEnforcementContext,
): PolicyValidationCheck {
  const check = riskTierBindingCheck(context.spec, [], undefined, context.resolveSubworkflowSpec, context.workflowId);
  if (check.status !== "failed") return check;
  const elsewhere = boundInOtherEnvironments(project, context.workflowId, context.environmentId);
  if (elsewhere.length === 0) return check;
  return {
    code: "risk_tier_binding",
    status: "skipped",
    message:
      `elevated risk tier is bound only in other environments (${elsewhere.join(", ")}) — not ` +
      `validated here; starting this workflow in '${context.environmentId}' fails closed at guard build.`,
  };
}

export function validateWorkflowPolicyCompliance(
  project: TypefluxProjectSpec,
  sources: ProjectPolicySources,
  context: WorkflowPolicyEnforcementContext,
): PolicyValidationCheck[] {
  const selected = selectProjectPolicyIdsForWorkflow(project, {
    environmentId: context.environmentId,
    workflowId: context.workflowId,
    explicitPolicyIds: context.explicitPolicyIds ?? [],
  });
  if (selected.length === 0) {
    return [
      {
        code: "policy_enforcement",
        status: "skipped",
        message: "no project policies selected for this workflow/environment",
      },
      softenedUnboundBindingCheck(project, context),
    ];
  }
  let policy;
  try {
    policy = composeProjectPolicyIds(project, sources, selected);
  } catch (error) {
    return [
      {
        code: "policy_composition",
        status: "failed",
        message: error instanceof Error ? error.message : String(error),
      },
      // Python parity (#788): the binding verdict is skipped, not silently absent,
      // when composition itself failed.
      { code: "risk_tier_binding", status: "skipped", message: "skipped because policy composition failed" },
    ];
  }
  const checks = validatePolicyCompliance(context.spec, policy);
  checks.push(riskTierBindingCheck(context.spec, selected, policy.payload, context.resolveSubworkflowSpec, context.workflowId));
  // Transitive-closure admission (#55 §9): re-validate referenced sub-workflows against
  // the parent's composed policy. Emitted only for a composed workflow with a resolver
  // (V1 validate output stays byte-identical for everything else).
  const closure = validateSubworkflowClosurePolicy(context.spec, policy, context.resolveSubworkflowSpec, context.workflowId);
  if (closure !== undefined) checks.push(closure);
  return checks;
}

/**
 * Build the per-call {@link RuntimePolicyGuard} for a resolved workflow (Python
 * `build_project_policy_runtime_guard`): select the applicable policies (none →
 * `undefined`, no guard), compose them, run admission compliance, and FAIL CLOSED —
 * throwing a {@link ProjectPolicyEnforcementError} (carrying the checks) if any check
 * failed — before returning a guard bound to the spec's provider identity. Wire the
 * returned guard's hooks into the executor for per-call enforcement.
 */
export function buildProjectPolicyRuntimeGuard(
  project: TypefluxProjectSpec,
  sources: ProjectPolicySources,
  context: WorkflowPolicyEnforcementContext,
): RuntimePolicyGuard | undefined {
  const selected = selectProjectPolicyIdsForWorkflow(project, {
    environmentId: context.environmentId,
    workflowId: context.workflowId,
    explicitPolicyIds: context.explicitPolicyIds ?? [],
  });
  if (selected.length === 0) {
    const unbound = riskTierBindingCheck(context.spec, selected, undefined, context.resolveSubworkflowSpec, context.workflowId);
    if (unbound.status === "failed") {
      // Fail closed (#788, audit B1): an elevated declared tier with nothing to enforce
      // it refuses instead of silently running unenforced (Python parity).
      throw new ProjectPolicyEnforcementError(
        `project policy enforcement failed: ${unbound.code}: ${unbound.message}`,
        [unbound],
      );
    }
    return undefined;
  }
  const policy = composeProjectPolicyIds(project, sources, selected);
  const checks = validatePolicyCompliance(context.spec, policy);
  checks.push(riskTierBindingCheck(context.spec, selected, policy.payload, context.resolveSubworkflowSpec, context.workflowId));
  // Transitive-closure admission (#55 §9): a non-compliant referenced sub-workflow fails
  // the parent's admission fail-closed (never emitted for a non-composed workflow).
  const closure = validateSubworkflowClosurePolicy(context.spec, policy, context.resolveSubworkflowSpec, context.workflowId);
  if (closure !== undefined) checks.push(closure);
  const failures = checks.filter((check) => check.status === "failed");
  if (failures.length > 0) {
    // Python `_policy_failure_message`: "project policy enforcement failed: <code>: <msg>; …".
    // `||` (not `??`) mirrors Python's `check.message or …`, which also falls back on an
    // empty-string message — the truthiness-parity trap.
    const parts = failures.map((check) => `${check.code}: ${check.message || `policy check failed: ${check.code}`}`);
    throw new ProjectPolicyEnforcementError(`project policy enforcement failed: ${parts.join("; ")}`, checks);
  }
  return new RuntimePolicyGuard(policy, context.spec.runtime.provider.type);
}
