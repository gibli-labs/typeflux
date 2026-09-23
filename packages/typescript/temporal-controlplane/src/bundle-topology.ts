/**
 * The resolved-workflow TOPOLOGY projection (governance parity, #563 slice 2b; Python
 * `project/bundle.py` `_bundle_topology` + `BundleTopology`). Projects a resolved workflow spec
 * into a nodes+edges DAG for display/monitoring: one node per step (activity/map/parallel),
 * sequential edges in declared step order, the #55 composition shapes — `branch` edges from a
 * parallel node to each branch's first step (condition: the branch's rendered `when`), `collect`
 * edges from each branch's last step back to the parallel node (where the collected value
 * materializes, matching `context[block.id]`), `conditional` sequential edges into `when`-gated
 * steps (condition: the rendered predicate) — and, when the review gate is enabled, review edges
 * from the review checkpoint to each routed decision target (and the timeout route).
 *
 * A pure function over a {@link TypefluxYamlSpec}; the control plane resolves the workflow under an
 * environment first. Snake_case DTO fields match the Python contract; `condition` is omitted on
 * sequential/collect edges and `activity` on parallel nodes (Python `exclude_none`). Emission
 * order is normative for cross-edition fixture parity (Python mirrors it in #55 slice 2):
 * depth-first — each step's node, then (for a parallel node, per branch in declared order) the
 * branch edge, the branch's nodes and internal edges, and its collect edge; consecutive-step
 * edges interleave in walk order; review edges last. A V1 linear spec therefore projects
 * byte-identically to the pre-composition code.
 */

import {
  renderPlanWhen,
  workflowPlanFromSpec,
  type TypefluxYamlSpec,
  type WorkflowPlan,
  type WorkflowPlanStep,
} from "@typeflux/temporal-yaml";

/** A workflow-graph node — one per step (Python `BundleTopologyNode`). */
export interface ApiBundleTopologyNode {
  id: string;
  kind: "activity" | "map" | "parallel" | "workflow";
  /** Present exactly when the node calls an activity; a parallel/workflow block has none (#55). */
  activity?: string;
  /** Present exactly on a `workflow` node — the sub-workflow's manifest id (#55 §3.4). */
  workflow?: string;
}

/**
 * A workflow-graph edge (Python `BundleTopologyEdge`). `condition` is present on review edges
 * (the user decision, or `"timeout"`) and on branch/conditional edges (the rendered `when`
 * predicate) — omitted on sequential and collect edges (Python `condition: None`).
 */
export interface ApiBundleTopologyEdge {
  source: string;
  target: string;
  kind: "sequential" | "review" | "branch" | "collect" | "conditional";
  condition?: string;
}

/** The workflow structure as a nodes+edges DAG (Python `BundleTopology`). */
export interface ApiBundleTopology {
  nodes: ApiBundleTopologyNode[];
  edges: ApiBundleTopologyEdge[];
}

/** Ascending sort of record entries by key (Python `sorted(review.user_decisions.items())`). */
const byKey = <T>(record: Readonly<Record<string, T>>): [string, T][] =>
  Object.entries(record).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));

/**
 * Project a resolved workflow spec into its topology (Python `_bundle_topology`). Nodes come from the
 * workflow PLAN (declared step order; nested branch steps are ordinary nodes); review edges are
 * added only when `lifecycle.enabled` and a review gate is configured.
 */
export function buildBundleTopology(spec: TypefluxYamlSpec, sharedPlan?: WorkflowPlan): ApiBundleTopology {
  // Accepts the caller's already-built plan so a bundle request derives it once (#575 review).
  const plan = sharedPlan ?? workflowPlanFromSpec(spec);
  const nodes: ApiBundleTopologyNode[] = [];
  const edges: ApiBundleTopologyEdge[] = [];

  /** Walk one sequence: emit its nodes + internal edges; return the first/last step ids. */
  const projectSequence = (
    steps: readonly WorkflowPlanStep[],
  ): { first: string | undefined; last: string | undefined } => {
    let previous: string | undefined;
    let first: string | undefined;
    for (const step of steps) {
      if (previous !== undefined) {
        // A sequential edge INTO a when-gated step is `conditional`, carrying the rendered
        // predicate (#55 §7). A gated FIRST step of a sequence has no incoming edge to carry
        // its condition — the gate still shows in lifecycle provenance (step_skipped).
        edges.push(
          step.when !== undefined
            ? { source: previous, target: step.id, kind: "conditional", condition: renderPlanWhen(step.when) }
            : { source: previous, target: step.id, kind: "sequential" },
        );
      }
      if (step.kind === "parallel") {
        nodes.push({ id: step.id, kind: "parallel" });
        for (const branch of step.branches) {
          // Branch edge first, then the branch's own nodes/edges, then its collect edge
          // (the normative emission order — see the module doc).
          const firstId = branch.steps[0]?.id;
          if (firstId !== undefined) {
            edges.push({
              source: step.id,
              target: firstId,
              kind: "branch",
              ...(branch.when !== undefined ? { condition: renderPlanWhen(branch.when) } : {}),
            });
          }
          const walked = projectSequence(branch.steps);
          if (walked.last !== undefined) {
            // The collected value materializes AT the parallel node (`context[block.id]`).
            edges.push({ source: walked.last, target: step.id, kind: "collect" });
          }
        }
      } else if (step.kind === "subworkflow" || step.kind === "subworkflowMap") {
        // Both a plain sub-workflow step and a map-over-sub-workflow project to one
        // `workflow` node carrying the child's manifest id (#55 §3.4/§7); they take part
        // in sequential/conditional/branch/collect edges exactly like activity/map steps.
        nodes.push({ id: step.id, kind: "workflow", workflow: step.workflowId });
      } else {
        nodes.push({ id: step.id, kind: step.kind, activity: step.activity });
      }
      first ??= step.id;
      previous = step.id;
    }
    return { first, last: previous };
  };

  projectSequence(plan.steps);

  const lifecycle = spec.workflow.lifecycle;
  // Review/route edges per GATE (#55 slice 4): the single `review` and the named `gates`
  // both project every gate's decision + timeout routes (single-review output unchanged).
  const gates =
    lifecycle?.enabled === true
      ? lifecycle.review !== undefined
        ? [lifecycle.review]
        : (lifecycle.gates ?? [])
      : [];
  for (const gate of gates) {
    for (const [decision, route] of byKey(gate.user_decisions)) {
      edges.push({ source: gate.after_step, target: route.route, kind: "review", condition: decision });
    }
    // The timeout's route action is a real edge too, so a timeout-only-reachable step isn't orphaned.
    const timeout = gate.timeout;
    if (timeout?.on_timeout === "route" && timeout.route !== undefined) {
      edges.push({ source: gate.after_step, target: timeout.route, kind: "review", condition: "timeout" });
    }
  }

  return { nodes, edges };
}
