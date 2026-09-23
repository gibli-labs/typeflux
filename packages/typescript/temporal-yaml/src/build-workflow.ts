/**
 * Derive a {@link WorkflowPlan} from a validated spec (parity Epic 5, #452; Python
 * `yaml/workflow.py` `_build_call_spec`). Each spec step becomes an `activity` step
 * or a `map` step; the plan is then passed as an argument to the generic
 * `typefluxYamlWorkflow` interpreter.
 *
 * Lifecycle-review steps are deferred — a step that is neither `activity` nor `map`
 * throws here rather than being silently dropped.
 */

import { workflowPlanDigest } from "./frozen-version.js";
import {
  WHEN_OPERATORS,
  type ActivityRetrySpec,
  type TypefluxYamlSpec,
  type WhenLeafSpec,
  type WhenSpec,
  type WorkflowCompensateSpec,
  type WorkflowLifecycleReviewSpec,
  type WorkflowStepSpec,
} from "./spec.js";
import type {
  ActivityTimeoutOptions,
  CompensatePlan,
  MapPlanStep,
  GatePlan,
  ParallelPlanStep,
  PlanWhen,
  PlanWhenLeaf,
  RetryPolicyPlan,
  ReviewPlan,
  SubworkflowChildIdentity,
  SubworkflowMapPlanStep,
  SubworkflowPlanStep,
  WorkflowPlan,
  WorkflowPlanStep,
} from "./workflow-plan.js";

/** `context` is seeded with the workflow input under this key, so no step may use it. */
const RESERVED_STEP_ID = "input";

/**
 * How `workflow:` step references resolve (#55 §3.4): through the PROJECT MANIFEST —
 * a `workflow:` value is a sibling workflow id declared in `typeflux.project.yaml`,
 * and `specFor` returns that sibling's RESOLVED spec (same environment/profile
 * composition as the parent). Cross-runtime composition is structurally excluded:
 * the resolver hands out specs of one project tree, and a project resolves under
 * exactly one runtime.
 */
export interface SubworkflowSpecResolver {
  /** The manifest workflow id of the spec being planned — seeds reference-cycle detection. */
  selfId?: string;
  /**
   * The resolved spec for a sibling workflow id. Returns `undefined` ONLY when the manifest
   * does not declare the id (an undeclared reference then surfaces as the precise
   * plan-derivation error). A DECLARED sibling that fails to resolve (broken profile, missing
   * env var, invalid YAML) must THROW its real resolution error — collapsing it to `undefined`
   * would misreport a config failure as "not declared in the project manifest".
   * `resolveSubworkflowIdentity` wraps such throws with the referencing step's context.
   */
  specFor(workflowId: string): TypefluxYamlSpec | undefined;
}

/** Options threaded through plan derivation (#55 slice 3). */
export interface WorkflowPlanBuildOptions {
  /** Required to resolve `workflow:` / `map.workflow` steps; absent = standalone spec (they reject). */
  subworkflows?: SubworkflowSpecResolver;
}

/**
 * Build a {@link SubworkflowSpecResolver} from a project's per-id sibling lookup (#55 §3.4).
 * `resolve(workflowId)` returns the sibling's RESOLVED spec (composed under the SAME environment
 * as the parent), `undefined` when the manifest doesn't declare it, and THROWS the sibling's own
 * resolution error for a declared-but-broken sibling (see {@link SubworkflowSpecResolver.specFor}).
 * The `selfId` (the parent's manifest id) seeds reference-cycle detection. Results are memoized
 * per id — one resolver instance resolves each sibling at most once, so diamond-shaped reference
 * graphs (A→B, A→C, B→D, C→D) stay linear instead of re-resolving per path.
 */
export function projectSubworkflowResolver(
  selfId: string,
  resolve: (workflowId: string) => TypefluxYamlSpec | undefined,
): SubworkflowSpecResolver {
  const cache = new Map<string, TypefluxYamlSpec | undefined>();
  return {
    selfId,
    specFor: (workflowId) => {
      if (cache.has(workflowId)) {
        return cache.get(workflowId);
      }
      const resolved = resolve(workflowId);
      cache.set(workflowId, resolved);
      return resolved;
    },
  };
}

/** The standalone-spec rejection for a workflow reference (#55 §1: project-manifest resolution only). */
function standaloneSubworkflowError(stepId: string, ref: string): Error {
  return new Error(
    `workflow step ${JSON.stringify(stepId)} references sibling workflow ${JSON.stringify(ref)}, but this ` +
      "spec was loaded standalone — sub-workflow references resolve through a project manifest " +
      "(the `workflow:` value is a workflow id declared in typeflux.project.yaml), so load this " +
      "workflow via its project (#55)",
  );
}

/**
 * The parallel nesting ceiling (#55 decision D3): parallel-in-branch-of-parallel covers
 * every reviewed real shape; deeper nesting is a strong signal the inner block should be
 * a sub-workflow — which also restores operability (its own id, status, drain row).
 * Exported as the CANONICAL constant (Python `yaml/spec.py` MAX_PARALLEL_NESTING_DEPTH):
 * the composition-policy schema (#298) may only tighten this value, never restate it.
 */
export const MAX_PARALLEL_NESTING_DEPTH = 3;

/**
 * The LINEAR schema chain the runtime pipes (Python `_validate_workflow_graph`): each activity step
 * consumes the previous step's output, so its input ref must equal the running ref (starting at
 * `workflow.input`). A map step reads its `over` path and produces a `collect.output` object, so its
 * array/item boundary is not ref-checked here (conservative — left to runtime parse); the chain
 * resumes at its collect output. Returns an error message when the chain is incompatible, else
 * `undefined`. (Review-route tails are checked separately in {@link workflowPlanFromSpec}.)
 */
export function workflowSchemaChainError(
  spec: TypefluxYamlSpec,
  options: WorkflowPlanBuildOptions = {},
): string | undefined {
  const definitions = spec.activities.definitions ?? [];
  const inputRef = new Map(definitions.map((definition) => [definition.name, definition.input] as const));
  const outputRef = new Map(definitions.map((definition) => [definition.name, definition.output] as const));
  /** A `workflow:` step's input/output refs are the CHILD's `workflow.input`/`output` (#55 §4.2). */
  const childRefs = (
    ref: string,
  ): { input: string | undefined; output: string | undefined } => {
    const childSpec = options.subworkflows?.specFor(ref);
    // Unknown / unresolvable refs are unknowable HERE — plan derivation rejects them
    // with the precise error (standalone / undeclared / cycle); the chain stays
    // conservative rather than duplicating that failure.
    return childSpec === undefined
      ? { input: undefined, output: undefined }
      : { input: childSpec.workflow.input, output: childSpec.workflow.output };
  };

  // The running-value state at one point of a sequence. A map WITHOUT `collect` yields an ARRAY of
  // results, which the single-ref chain can't represent — and which a single-input activity cannot
  // consume. Track that state so a following activity step is rejected (rather than silently
  // skipped) — the map must `collect` first.
  interface Chain {
    ref: string | undefined;
    isArray: boolean;
  }
  // A `when` gate makes the running value at that point a potential TERMINAL of the enclosing
  // sequence (#55 §3.3): the sequence may complete there, so the value must type-match the
  // sequence's declared result. Collected during the walk, checked against the terminal.
  interface GatePoint {
    id: string;
    at: Chain;
  }

  /** Walk one sequence (top level or a branch), recursing into parallel blocks. */
  const walkSequence = (
    steps: readonly WorkflowStepSpec[],
    entry: Chain,
  ): { chain: Chain; gates: GatePoint[] } | { error: string } => {
    let current = entry;
    const gates: GatePoint[] = [];
    for (const step of steps) {
      if (step.when !== undefined) {
        gates.push({ id: step.id, at: current });
      }
      if (step.activity !== undefined) {
        if (current.isArray) {
          return {
            error:
              `workflow step ${JSON.stringify(step.id)} cannot consume the array output of a preceding ` +
              `collect-less map step; add a \`collect\` to that map`,
          };
        }
        const expected = inputRef.get(step.activity);
        if (expected !== undefined && current.ref !== undefined && expected !== current.ref) {
          return {
            error:
              `workflow step ${JSON.stringify(step.id)} expects input ${JSON.stringify(expected)}, ` +
              `but the prior step's output is ${JSON.stringify(current.ref)}`,
          };
        }
        current = { ref: outputRef.get(step.activity), isArray: false };
      } else if (step.workflow !== undefined) {
        // A `workflow:` step consumes the running value as the CHILD's input and
        // yields the child's output (#55 §4.2) — the V1 activity rule with the
        // child's `workflow.input`/`output` refs standing in for a definition's.
        if (current.isArray) {
          return {
            error:
              `workflow step ${JSON.stringify(step.id)} cannot consume the array output of a preceding ` +
              `collect-less map step; add a \`collect\` to that map`,
          };
        }
        const refs = childRefs(step.workflow);
        if (refs.input !== undefined && current.ref !== undefined && refs.input !== current.ref) {
          return {
            error:
              `workflow step ${JSON.stringify(step.id)} runs sibling workflow ${JSON.stringify(step.workflow)} ` +
              `expecting input ${JSON.stringify(refs.input)}, but the prior step's output is ${JSON.stringify(current.ref)}`,
          };
        }
        current = { ref: refs.output, isArray: false };
      } else if (step.map !== undefined) {
        // A map reads its `over` path from context (not the running value), so its own input is not
        // ref-checked; the chain resumes at `collect.output`, or becomes an array when there is no collect.
        // Identically for a `map.workflow` child fan-out (#55 §3.4) — per-item typing is the
        // resolved-type editions' job (Python), conservative here.
        const ref = step.map.collect?.output;
        current = { ref, isArray: ref === undefined };
      } else if (step.parallel !== undefined) {
        // Each branch's first step consumes the block's input under the V1 chaining rule; the
        // block's output is its collect object (#55 §3.1). The collect-field <-> branch-id merge
        // typing is Python's resolved-type check — TS's injected-schema model catches a mismatch
        // at the strict activity boundary instead (the documented ref-vs-type asymmetry).
        for (const branch of step.parallel.branches) {
          const walked = walkSequence(branch.steps, current);
          if ("error" in walked) {
            return walked;
          }
          // A mid-branch gate exits the branch with the running value as its contribution, so the
          // value must match the branch's terminal type (= its collect field's type). Checked
          // where both refs are knowable (the existing conservatism).
          for (const gate of walked.gates) {
            if (
              gate.at.ref !== undefined &&
              walked.chain.ref !== undefined &&
              !gate.at.isArray &&
              !walked.chain.isArray &&
              gate.at.ref !== walked.chain.ref
            ) {
              return {
                error:
                  `when gate on step ${JSON.stringify(gate.id)} makes ${JSON.stringify(gate.at.ref)} a potential ` +
                  `result of branch ${JSON.stringify(branch.id)}, which terminates with ` +
                  `${JSON.stringify(walked.chain.ref)} — a gated sequence must exit with its declared result type`,
              };
            }
          }
        }
        current = { ref: step.parallel.collect.output, isArray: false };
      }
    }
    return { chain: current, gates };
  };

  const walked = walkSequence(spec.workflow.steps, { ref: spec.workflow.input, isArray: false });
  if ("error" in walked) {
    return walked.error;
  }
  // An explicit `workflow.output` must equal the TERMINAL step's output: the runtime returns the
  // terminal value, so a divergent declared output is unrunnable (checked here so `/validate` and the
  // bundle/catalog projections agree). A collect-less-map terminal is an array with no single ref — skip.
  const declaredOutput = spec.workflow.output;
  if (declaredOutput !== undefined && !walked.chain.isArray && walked.chain.ref !== undefined && declaredOutput !== walked.chain.ref) {
    return `workflow.output ${JSON.stringify(declaredOutput)} does not match the terminal step's output ${JSON.stringify(walked.chain.ref)}`;
  }
  // A top-level gate is early exit: the workflow completes with the running value at the gate, so
  // that value must match `workflow.output` (a gated FIRST step therefore requires
  // workflow.input == workflow.output). Checked only when the declared output is known — the
  // spec's `output` is optional in TS (existing divergence, unchanged).
  if (declaredOutput !== undefined) {
    for (const gate of walked.gates) {
      if (!gate.at.isArray && gate.at.ref !== undefined && gate.at.ref !== declaredOutput) {
        return (
          `when gate on step ${JSON.stringify(gate.id)} would complete the workflow with ` +
          `${JSON.stringify(gate.at.ref)}, but workflow.output is ${JSON.stringify(declaredOutput)} — ` +
          `a gated sequence must exit with its declared result type`
        );
      }
    }
  }
  return undefined;
}

/**
 * Load-validate every step's `compensate:` (#299 D299-1): the `input_from` path must be
 * resolvable at the compensated step's position (the workflow input, or a step completed
 * before it — the compensated step's own output is addressable by its id, since it is in
 * context by the time compensation runs), and — when both refs are statically known — the
 * compensating activity's declared input type must equal the referenced value's type (the
 * conservative single-ref chain, exactly like {@link workflowSchemaChainError}; injected
 * activities with no definition, and dotted field paths, skip the type check). The
 * declared-activity check is the worker-build registered-set job (compensations join
 * {@link referencedActivities}). Returns an error message, else `undefined`.
 */
export function workflowCompensationError(
  spec: TypefluxYamlSpec,
  options: WorkflowPlanBuildOptions = {},
): string | undefined {
  // Early-out for the common (compensation-free) spec: skip the whole walk so a spec with no
  // `compensate:` never triggers a single sub-workflow resolution here (keeps the diamond-graph
  // resolution count — and every compensation-free digest — unchanged).
  const hasCompensate = (steps: readonly WorkflowStepSpec[]): boolean =>
    steps.some(
      (step) =>
        step.compensate !== undefined ||
        (step.parallel !== undefined && step.parallel.branches.some((branch) => hasCompensate(branch.steps))),
    );
  if (!hasCompensate(spec.workflow.steps)) {
    return undefined;
  }
  const definitions = spec.activities.definitions ?? [];
  const inputRef = new Map(definitions.map((d) => [d.name, d.input] as const));
  const outputRef = new Map(definitions.map((d) => [d.name, d.output] as const));
  const childOutputRef = (ref: string): string | undefined => options.subworkflows?.specFor(ref)?.workflow.output;
  // The compensated step's OWN output ref (the default compensation input): an activity's or
  // an activity-map's per-item result, or a sub-workflow's child output. A parallel/map.workflow
  // step never reaches here (rejected at plan build), so undefined is the safe fallback.
  const ownOutputRef = (step: WorkflowStepSpec): string | undefined => {
    if (step.activity !== undefined) {
      return outputRef.get(step.activity);
    }
    if (step.map?.activity !== undefined) {
      return outputRef.get(step.map.activity);
    }
    if (step.workflow !== undefined) {
      return childOutputRef(step.workflow);
    }
    return undefined;
  };

  // The ref a LATER step sees when it references this step by id (context[step.id]): an activity's
  // output, a MAP's COLLECT object (NOT its per-item output), a sub-workflow's child output, or a
  // parallel block's collect object. Distinct from ownOutputRef, which is the map's per-ITEM ref.
  const contextRefOf = (step: WorkflowStepSpec): string | undefined => {
    if (step.activity !== undefined) {
      return outputRef.get(step.activity);
    }
    if (step.map !== undefined) {
      return step.map.collect?.output;
    }
    if (step.workflow !== undefined) {
      return childOutputRef(step.workflow);
    }
    if (step.parallel !== undefined) {
      return step.parallel.collect.output;
    }
    return undefined;
  };
  // Spec-level twin of workflow-plan.ts `guaranteedContextIdsOf`: the ids guaranteed present on
  // every path once `step` completed (its own id, plus ungated branches' ungated prefixes,
  // recursively). Used to propagate parallel-interior roots to LATER steps.
  const guaranteedSpecIds = (step: WorkflowStepSpec): string[] => {
    const ids = [step.id];
    if (step.parallel !== undefined) {
      for (const branch of step.parallel.branches) {
        if (branch.when !== undefined) {
          continue; // the whole branch may be gated out
        }
        for (const nested of branch.steps) {
          if (nested.when !== undefined) {
            break; // a false gate skips this step and the branch remainder
          }
          ids.push(...guaranteedSpecIds(nested));
        }
      }
    }
    return ids;
  };

  let error: string | undefined;
  const checkCompensate = (
    step: WorkflowStepSpec,
    available: Set<string>,
    refById: Map<string, string | undefined>,
  ): void => {
    const c = step.compensate as NonNullable<WorkflowStepSpec["compensate"]>;
    let referencedRef: string | undefined;
    if (c.input_from === undefined) {
      referencedRef = ownOutputRef(step);
    } else {
      const root = c.input_from.split(".")[0] ?? "";
      // A MAP records its compensations PER ITEM from inside its executor, BEFORE context[mapId]
      // exists — so a self-rooted map input_from can never resolve at per-item record time.
      if (step.map !== undefined && root === step.id) {
        error =
          `step ${JSON.stringify(step.id)} compensate.input_from reads ${JSON.stringify(c.input_from)} ` +
          "(its own collected output), which is not available when its items compensate — omit " +
          "input_from to compensate each item on its own result";
        return;
      }
      if (root !== "input" && !available.has(root)) {
        error =
          `step ${JSON.stringify(step.id)} compensate.input_from reads ${JSON.stringify(c.input_from)}, but ` +
          `${JSON.stringify(root)} is not available there (available: the workflow input and steps completed on ` +
          "every path to this step, including the step itself)";
        return;
      }
      // A bare step id resolves to what a later step sees (contextRefOf, recorded in refById);
      // `input` to the workflow input; a dotted field path is conservative (a sub-field is untyped).
      referencedRef = c.input_from.includes(".")
        ? undefined
        : root === "input"
          ? spec.workflow.input
          : refById.get(root);
    }
    const compInputRef = inputRef.get(c.activity);
    if (referencedRef !== undefined && compInputRef !== undefined && referencedRef !== compInputRef) {
      error =
        `step ${JSON.stringify(step.id)} compensate runs activity ${JSON.stringify(c.activity)} expecting input ` +
        `${JSON.stringify(compInputRef)}, but the referenced value ` +
        `${c.input_from === undefined ? "(the step's own output)" : JSON.stringify(c.input_from)} is ` +
        `${JSON.stringify(referencedRef)}`;
    }
  };

  const walk = (steps: readonly WorkflowStepSpec[], roots: Set<string>, refById: Map<string, string | undefined>): void => {
    for (const step of steps) {
      if (error !== undefined) {
        return;
      }
      if (step.compensate !== undefined) {
        // Available roots for THIS compensation: the current roots PLUS the step's own id for an
        // ATOMIC step (its output is recorded before its compensation pushes). A MAP records per
        // item BEFORE context[mapId] exists (self-ref rejected in checkCompensate); a PARALLEL
        // carries no compensate. The step's own id is NOT added to `roots` yet — a branch of a
        // parallel must not see the block's collect (mirrors validateWhenPaths).
        const available = new Set(roots);
        if (step.activity !== undefined || step.workflow !== undefined) {
          available.add(step.id);
        }
        checkCompensate(step, available, refById);
        if (error !== undefined) {
          return;
        }
      }
      if (step.parallel !== undefined) {
        for (const branch of step.parallel.branches) {
          // Each branch sees the block-entry roots (NOT the parallel's own id) plus its OWN earlier
          // steps (copies keep sibling branches invisible to each other).
          walk(branch.steps, new Set(roots), new Map(refById));
        }
      }
      // AFTER the step: its guaranteed context ids join `roots`, so a LATER compensation can read a
      // guaranteed post-parallel branch step; its context ref joins refById for later type checks.
      for (const id of guaranteedSpecIds(step)) {
        roots.add(id);
      }
      refById.set(step.id, contextRefOf(step));
    }
  };
  walk(spec.workflow.steps, new Set(["input"]), new Map());
  return error;
}

/**
 * Normalize one spec `when:` leaf into the plan's `{path, op, value}` shape. Zod
 * guarantees exactly one operator is present; `exists` values arrive as booleans
 * (yamlBoolean coercion), and an `eq: null` literal is a real null comparison.
 */
function planWhenLeaf(leaf: WhenLeafSpec): PlanWhenLeaf {
  for (const op of WHEN_OPERATORS) {
    if (leaf[op] !== undefined) {
      return { path: leaf.path, op, value: leaf[op] };
    }
  }
  // Unreachable for a zod-validated leaf; loud for a hand-built one.
  throw new Error(`when predicate on ${JSON.stringify(leaf.path)} has no operator`);
}

/** Normalize a spec `when:` into the plan's `{mode, predicates}` shape (#55 §3.2). */
function planWhenFromSpec(when: WhenSpec): PlanWhen {
  if ("predicate" in when) {
    // The whenSpec union's forward-compat stub never parses; defend against a hand-built spec.
    throw new Error("when.predicate (named injected predicates) is not supported (#55)");
  }
  if ("all" in when) {
    return { mode: "all", predicates: when.all.map(planWhenLeaf) };
  }
  if ("any" in when) {
    return { mode: "any", predicates: when.any.map(planWhenLeaf) };
  }
  return { mode: "leaf", predicates: [planWhenLeaf(when)] };
}

/**
 * The step ids GUARANTEED to be in the context on every path once `step` has completed
 * (#55 §4.5: gates may only read "results of steps already completed on every path").
 * The step's own id always qualifies relative to LATER steps of its own sequence — a
 * false gate on it skips the remainder, so any later step that runs saw it run. Inside
 * a parallel block, only unconditional work qualifies for the world AFTER the block:
 * a `when`-gated branch may be skipped entirely (collect field null), and a mid-branch
 * gate skips its step and the branch remainder while the block still completes — so
 * those ids are conditionally absent and must not become gate roots downstream. Branch
 * ids are never roots: a branch's terminal value is a FIELD of the collect object
 * (`<blockId>.<branchId>`).
 */
function guaranteedContextIdsOf(step: WorkflowPlanStep): string[] {
  const ids = [step.id];
  if (step.kind === "parallel") {
    for (const branch of step.branches) {
      if (branch.when !== undefined) {
        continue; // the whole branch may be gated out
      }
      for (const nested of branch.steps) {
        if (nested.when !== undefined) {
          break; // a false gate skips this step and the branch remainder
        }
        ids.push(...guaranteedContextIdsOf(nested));
      }
    }
  }
  return ids;
}

/**
 * Load-check every `when` path against the context roots available at the gated
 * step/branch (#55 §4.5, the review walk's `availableRoots` discipline): workflow input
 * plus the results of steps already completed on every path to it — for a branch step,
 * the enclosing scopes' prior steps and earlier steps of its OWN branch. Sibling-branch
 * results are not addressable (they may not exist yet). Mutates `roots` progressively.
 */
function validateWhenPaths(steps: readonly WorkflowPlanStep[], roots: Set<string>): void {
  const check = (when: PlanWhen, label: string): void => {
    for (const leaf of when.predicates) {
      const root = leaf.path.split(".")[0] ?? "";
      if (!roots.has(root)) {
        throw new Error(
          `when predicate on ${label} reads ${JSON.stringify(leaf.path)}, but ${JSON.stringify(root)} is not ` +
            "available there (available: the workflow input and steps completed on every path to the gate; " +
            "sibling-branch results are not addressable)",
        );
      }
    }
  };
  for (const step of steps) {
    if (step.when !== undefined) {
      check(step.when, `step ${JSON.stringify(step.id)}`);
    }
    if (step.kind === "parallel") {
      for (const branch of step.branches) {
        if (branch.when !== undefined) {
          check(branch.when, `branch ${JSON.stringify(branch.id)}`);
        }
      }
      for (const branch of step.branches) {
        // Each branch sees the block-entry roots plus its OWN earlier steps (the copy
        // keeps sibling branches invisible to each other).
        validateWhenPaths(branch.steps, new Set(roots));
      }
    }
    for (const id of guaranteedContextIdsOf(step)) {
      roots.add(id);
    }
  }
}

/** Register a step/branch id into the workflow's single flat id namespace. */
function registerStepId(id: string, seen: Set<string>): void {
  // A step writes its result to `context[step.id]`, which later `over`/`when` paths read.
  // Reject the reserved `input` id and duplicates so a step can't silently shadow the
  // workflow input or an earlier step's result (parity with the Python validation).
  // Branch ids join the same namespace: they become the collect object's field names.
  if (id === RESERVED_STEP_ID) {
    throw new Error(`workflow step id ${JSON.stringify(id)} is reserved (it holds the workflow input)`);
  }
  if (seen.has(id)) {
    throw new Error(`duplicate workflow step id ${JSON.stringify(id)}`);
  }
  seen.add(id);
}

/**
 * Sub-workflow refs recorded per step id during plan derivation — the child's
 * `workflow.input`/`output` ref strings, consumed by the review-route type walk
 * (#55 §4.2). Keyed by the PLAN step id (plain `workflow:` steps only; a
 * `map.workflow` step's chain identity is its collect ref, like any map).
 */
type SubworkflowRefTable = Map<string, { inputRef: string; outputRef: string | undefined }>;

/** Mutable state threaded through one plan derivation (#55 slice 3). */
interface PlanBuildContext {
  options: WorkflowPlanBuildOptions;
  /** The manifest-id reference chain from the root spec to here (cycle detection §4.6). */
  visiting: readonly string[];
  subworkflowRefs: SubworkflowRefTable;
  /**
   * Resolved child identities memoized per manifest id, shared across the WHOLE derivation
   * (nested child builds included): a diamond reference graph (A→B, A→C, B→D, C→D) resolves D
   * once instead of once per path — per-path re-resolution is exponential in depth. Safe to
   * share across paths: a child's resolution is path-independent (same resolver, same
   * environment), and cycle detection stays per-path via `visiting` (a memoized child already
   * proved its subtree acyclic).
   */
  identityCache: Map<string, SubworkflowChildIdentity>;
}

/**
 * Resolve one `workflow:` reference into the plan node's embedded child identity
 * (#55 §6, ts-plan-argument): the child's RESOLVED plan (recursively, so grandchildren
 * embed too), its `workflowPlanDigest` (computed here — the sandbox cannot), and the
 * memo identity fields. The embedded plan + digest fold into the PARENT digest by
 * construction (`workflowPlanDigest` hashes the whole parent plan): a child-only edit
 * moves every ancestor digest — the frozen-label cascade, documented in docs/yaml.md.
 */
function resolveSubworkflowIdentity(
  ref: string,
  stepId: string,
  build: PlanBuildContext,
): SubworkflowChildIdentity {
  const resolver = build.options.subworkflows;
  if (resolver === undefined) {
    throw standaloneSubworkflowError(stepId, ref);
  }
  if (build.visiting.includes(ref)) {
    // The project-level reference relation must be acyclic (#55 §4.6): a workflow
    // transitively invoking itself would recurse forever at resolve time and could
    // never terminate at run time.
    throw new Error(
      `sub-workflow reference cycle detected: ${[...build.visiting, ref].join(" -> ")} — ` +
        "a workflow must not transitively invoke itself (#55)",
    );
  }
  const cached = build.identityCache.get(ref);
  if (cached !== undefined) {
    return cached;
  }
  let childSpec: TypefluxYamlSpec | undefined;
  try {
    childSpec = resolver.specFor(ref);
  } catch (error) {
    // A DECLARED sibling that failed to resolve (broken profile, missing env var, invalid
    // YAML) — surface ITS error with the referencing step's context, never the misleading
    // "not declared in the project manifest".
    throw new Error(
      `workflow step ${JSON.stringify(stepId)} references sibling workflow ${JSON.stringify(ref)}, ` +
        `which failed to resolve: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (childSpec === undefined) {
    throw new Error(
      `workflow step ${JSON.stringify(stepId)} references sibling workflow ${JSON.stringify(ref)}, ` +
        "which is not declared in the project manifest — sub-workflow references resolve through " +
        "typeflux.project.yaml workflow ids (#55)",
    );
  }
  const childPlan = buildWorkflowPlan(
    childSpec,
    { subworkflows: { selfId: ref, specFor: (id) => resolver.specFor(id) } },
    [...build.visiting, ref],
    build.identityCache,
  );
  const identity: SubworkflowChildIdentity = {
    workflowId: ref,
    workflowName: childSpec.workflow.name,
    project: childSpec.project,
    childDigest: workflowPlanDigest(childPlan),
    ...(childSpec.workflow.version !== undefined ? { versionLabel: childSpec.workflow.version } : {}),
    plan: childPlan,
  };
  build.identityCache.set(ref, identity);
  return identity;
}

/**
 * Build one sequence of plan steps (the top level, or a parallel branch), recursing
 * into parallel blocks. `parallelDepth` counts enclosing parallel blocks for the
 * D3 nesting ceiling.
 */
function planStepsFromSpec(
  specSteps: readonly WorkflowStepSpec[],
  spec: TypefluxYamlSpec,
  seen: Set<string>,
  parallelDepth: number,
  build: PlanBuildContext,
): WorkflowPlanStep[] {
  return specSteps.map((step): WorkflowPlanStep => {
    registerStepId(step.id, seen);

    const kinds = [step.activity, step.map, step.parallel, step.workflow].filter((kind) => kind !== undefined).length;
    if (kinds !== 1) {
      throw new Error(
        `workflow step ${JSON.stringify(step.id)} must have exactly one of \`activity\`, \`map\`, \`parallel\`, or \`workflow\``,
      );
    }
    const when = step.when !== undefined ? planWhenFromSpec(step.when) : undefined;
    // Compensation is available on activity / map / subworkflow steps only (#299 D299-1):
    // a `parallel` step's branches carry their OWN steps' compensation, and a `map.workflow`
    // (subworkflowMap) child fan-out is deferred to a later slice. Reject loud so a
    // misplaced `compensate:` never loads silently as a no-op.
    const compensate = step.compensate !== undefined ? compensatePlanFromSpec(step.compensate, step.id) : undefined;
    if (compensate !== undefined && step.parallel !== undefined) {
      throw new Error(
        `workflow step ${JSON.stringify(step.id)} is a parallel block and cannot carry \`compensate:\` — ` +
          "attach compensation to the individual steps inside its branches instead (#299)",
      );
    }
    if (compensate !== undefined && step.map?.workflow !== undefined) {
      throw new Error(
        `workflow step ${JSON.stringify(step.id)} fans a sub-workflow (\`map.workflow\`) and cannot carry ` +
          "`compensate:` in this release — compensation covers activity / map / sub-workflow steps (#299)",
      );
    }
    if (step.workflow !== undefined) {
      const identity = resolveSubworkflowIdentity(step.workflow, step.id, build);
      const childSpec = build.options.subworkflows?.specFor(step.workflow);
      if (childSpec !== undefined) {
        build.subworkflowRefs.set(step.id, {
          inputRef: childSpec.workflow.input,
          outputRef: childSpec.workflow.output,
        });
      }
      const subworkflowStep: SubworkflowPlanStep = {
        kind: "subworkflow",
        id: step.id,
        ...identity,
      };
      if (when !== undefined) {
        subworkflowStep.when = when;
      }
      if (compensate !== undefined) {
        subworkflowStep.compensate = compensate;
      }
      return subworkflowStep;
    }
    if (step.map?.workflow !== undefined) {
      // Child-workflow fan-out (#55 §3.4): V1 map semantics — bounded concurrency,
      // collect, max_bytes — with a child execution per item instead of an activity.
      const identity = resolveSubworkflowIdentity(step.map.workflow, step.id, build);
      const mapStep: SubworkflowMapPlanStep = {
        kind: "subworkflowMap",
        id: step.id,
        ...identity,
        over: step.map.over,
      };
      if (step.map.concurrency !== undefined) {
        mapStep.concurrency = step.map.concurrency;
      }
      if (step.map.collect?.field !== undefined) {
        mapStep.collectField = step.map.collect.field;
      }
      if ((step.map.collect?.max_bytes ?? 0) > 0) {
        // Same default-on/0-disables semantics as an activity map's collect (#495).
        mapStep.collectMaxBytes = step.map.collect?.max_bytes as number;
      }
      if (when !== undefined) {
        mapStep.when = when;
      }
      return mapStep;
    }
    if (step.map !== undefined) {
      const mapStep: MapPlanStep = {
        kind: "map",
        id: step.id,
        // The spec validated exactly one of activity/workflow; the workflow arm returned above.
        activity: step.map.activity as string,
        over: step.map.over,
      };
      if (step.map.concurrency !== undefined) {
        mapStep.concurrency = step.map.concurrency;
      }
      if (step.map.collect?.field !== undefined) {
        mapStep.collectField = step.map.collect.field;
      }
      if ((step.map.collect?.max_bytes ?? 0) > 0) {
        // The spec defaults the bound ON (Python DEFAULT_MAP_COLLECT_MAX_BYTES);
        // an explicit 0 disables it and is omitted from the plan (limit <= 0 is a
        // no-op in the interpreter anyway — keep the plan minimal).
        mapStep.collectMaxBytes = step.map.collect?.max_bytes as number;
      }
      // The activity definition's session cache rides the plan (#478), so the
      // interpreter brackets the fan-out with prep/release and the config change
      // is replay-visible (the plan is a workflow argument).
      const definition = (spec.activities.definitions ?? []).find((def) => def.name === step.map?.activity);
      // An explicit `cache: {enabled: false}` is behaviorally ABSENT (the interpreter
      // gates on enabled === true), so it stays out of the plan — Python's digest
      // collapses disabled→None for the same reason, and a graph-inert toggle must
      // not move the frozen workflow.version digest (#530).
      if (definition?.cache?.enabled === true) {
        mapStep.sessionCache = {
          enabled: true,
          ...(definition.cache.ttl_seconds !== undefined ? { ttlSeconds: definition.cache.ttl_seconds } : {}),
        };
      }
      if (when !== undefined) {
        mapStep.when = when;
      }
      if (compensate !== undefined) {
        mapStep.compensate = compensate;
      }
      return mapStep;
    }
    if (step.parallel !== undefined) {
      const depth = parallelDepth + 1;
      if (depth > MAX_PARALLEL_NESTING_DEPTH) {
        throw new Error(
          `workflow step ${JSON.stringify(step.id)} nests parallel blocks deeper than ` +
            `${MAX_PARALLEL_NESTING_DEPTH} — extract the inner block into a sub-workflow instead ` +
            "(#55 slice 3): a sub-workflow restores per-block operability (its own id, status, and drain row) " +
            "that deep nesting loses",
        );
      }
      const parallelStep: ParallelPlanStep = {
        kind: "parallel",
        id: step.id,
        branches: step.parallel.branches.map((branch) => {
          registerStepId(branch.id, seen);
          const branchWhen = branch.when !== undefined ? planWhenFromSpec(branch.when) : undefined;
          return {
            id: branch.id,
            ...(branchWhen !== undefined ? { when: branchWhen } : {}),
            steps: planStepsFromSpec(branch.steps, spec, seen, depth, build),
          };
        }),
      };
      if (step.parallel.collect.max_bytes > 0) {
        // Same default-on/0-disables semantics as a map's collect (#495).
        parallelStep.collectMaxBytes = step.parallel.collect.max_bytes;
      }
      if (when !== undefined) {
        parallelStep.when = when;
      }
      return parallelStep;
    }
    return {
      kind: "activity",
      id: step.id,
      activity: step.activity as string,
      ...(when !== undefined ? { when } : {}),
      ...(compensate !== undefined ? { compensate } : {}),
    };
  });
}

/** Every activity name referenced by a step subtree (leaf activity/map steps + compensations). */
function referencedActivities(steps: readonly WorkflowPlanStep[]): Set<string> {
  const names = new Set<string>();
  const walk = (list: readonly WorkflowPlanStep[]): void => {
    for (const step of list) {
      if (step.kind === "parallel") {
        for (const branch of step.branches) {
          walk(branch.steps);
        }
      } else if (step.kind === "activity" || step.kind === "map") {
        // Sub-workflow steps call no activity of the PARENT's — the child's
        // activities belong to its own plan/options (#55 §6).
        names.add(step.activity);
      }
      // A compensating activity is ordinary (#299): its timeout/retry options must ride the
      // plan and it must pass the worker-build registered-set check, so it counts as referenced
      // (activity / map / subworkflow steps can all carry one).
      if ((step.kind === "activity" || step.kind === "map" || step.kind === "subworkflow") && step.compensate !== undefined) {
        names.add(step.compensate.activity);
      }
    }
  };
  walk(steps);
  return names;
}

export function workflowPlanFromSpec(
  spec: TypefluxYamlSpec,
  options: WorkflowPlanBuildOptions = {},
): WorkflowPlan {
  return buildWorkflowPlan(
    spec,
    options,
    options.subworkflows?.selfId !== undefined ? [options.subworkflows.selfId] : [],
  );
}

/**
 * The recursive core of {@link workflowPlanFromSpec}: `visiting` carries the manifest-id chain;
 * `identityCache` is the derivation-wide child-identity memo (fresh at the root, shared down).
 */
function buildWorkflowPlan(
  spec: TypefluxYamlSpec,
  options: WorkflowPlanBuildOptions,
  visiting: readonly string[],
  identityCache: Map<string, SubworkflowChildIdentity> = new Map(),
): WorkflowPlan {
  const seen = new Set<string>();
  const build: PlanBuildContext = { options, visiting, subworkflowRefs: new Map(), identityCache };
  const steps = planStepsFromSpec(spec.workflow.steps, spec, seen, 0, build);
  // `when` paths are data the interpreter resolves at runtime — check their roots at
  // load so a gate can never read a context value that cannot exist at its position.
  validateWhenPaths(steps, new Set([RESERVED_STEP_ID]));
  // Compensation load-validation (#299 D299-1): resolvable input_from + a matching schema chain
  // for the compensating activity. Throws here (like the when-path check) so /validate and the
  // bundle/catalog projections agree — a broken compensation can never load silently.
  const compensationError = workflowCompensationError(spec, options);
  if (compensationError !== undefined) {
    throw new Error(compensationError);
  }
  const plan: WorkflowPlan = { steps };
  const activityOptions = activityOptionsFromSpec(spec, referencedActivities(steps));
  if (activityOptions !== undefined) {
    plan.activityOptions = activityOptions;
  }
  // Python validates a review/gates block UNCONDITIONALLY (its model_validator runs regardless
  // of `enabled`), so a disabled lifecycle must not let broken routes load silently.
  const reviewPlan =
    spec.workflow.lifecycle?.review !== undefined
      ? reviewPlanFromSpec(spec, spec.workflow.lifecycle.review, steps, build.subworkflowRefs)
      : undefined;
  // Multiple named gates (#55 slice 4): each gate is validated with the same forward-only
  // routed-tail discipline, scoped by its id in error prose. `review`/`gates` are mutually
  // exclusive at load (spec.ts), so at most one of these is populated.
  const gatePlans =
    spec.workflow.lifecycle?.gates !== undefined
      ? spec.workflow.lifecycle.gates.map((gate): GatePlan => ({
          id: gate.id,
          ...reviewPlanFromSpec(spec, gate, steps, build.subworkflowRefs, `gate ${JSON.stringify(gate.id)}`),
        }))
      : undefined;
  if (spec.workflow.lifecycle?.enabled === true) {
    plan.lifecycle = {
      progress: spec.workflow.lifecycle.progress,
      cancellation: spec.workflow.lifecycle.cancellation,
      statusEventLimit: spec.workflow.lifecycle.history.status_event_limit,
    };
    if (reviewPlan !== undefined) {
      plan.lifecycle.review = reviewPlan;
    }
    if (gatePlans !== undefined) {
      plan.lifecycle.gates = gatePlans;
    }
  }
  if (spec.runtime.activity_retry !== undefined) {
    // The workflow-wide override; each activity without its own `retry` resolves to this in
    // `activityProxyOptions`, else the bounded default.
    plan.retryPolicy = retryPolicyPlanFrom(spec.runtime.activity_retry, "runtime.activity_retry");
  }
  return plan;
}

/**
 * Map each activity definition's `start_to_close_timeout_seconds` / `heartbeat_timeout_seconds` (ms
 * timeouts) and `retry` (override) to per-activity options (parity with Python's `_build_call_spec`:
 * `start_to_close_timeout` falls back to the 2-minute default, `heartbeat_timeout` is omitted when
 * unset, and `retry` resolves later against `runtime.activity_retry`/the default). The worker
 * heartbeats while a `heartbeatTimeout` activity runs (#484), so the timeout is safe. Returns
 * undefined when no definition sets any option, so the plan stays minimal. Built with a null-prototype
 * object so an activity named like an inherited member (`toString`, `__proto__`) is a plain own key.
 */
function activityOptionsFromSpec(
  spec: TypefluxYamlSpec,
  referencedActivities: ReadonlySet<string>,
): Record<string, ActivityTimeoutOptions> | undefined {
  const options: Record<string, ActivityTimeoutOptions> = Object.create(null) as Record<string, ActivityTimeoutOptions>;
  for (const def of spec.activities.definitions ?? []) {
    // Options for a definition no step calls are never consulted by the
    // interpreter — keeping them out of the plan also keeps a tuned-but-unused
    // definition from moving the frozen workflow.version digest (#530).
    if (!referencedActivities.has(def.name)) {
      continue;
    }
    const entry: ActivityTimeoutOptions = {};
    if (def.start_to_close_timeout_seconds !== undefined) {
      entry.startToCloseTimeoutMs = secondsToMs(
        def.start_to_close_timeout_seconds,
        `activity ${JSON.stringify(def.name)} start_to_close_timeout_seconds`,
      );
    }
    if (def.heartbeat_timeout_seconds !== undefined) {
      entry.heartbeatTimeoutMs = secondsToMs(
        def.heartbeat_timeout_seconds,
        `activity ${JSON.stringify(def.name)} heartbeat_timeout_seconds`,
      );
    }
    if (def.retry !== undefined) {
      entry.retry = retryPolicyPlanFrom(def.retry, `activity ${JSON.stringify(def.name)} retry`);
    }
    if (entry.startToCloseTimeoutMs !== undefined || entry.heartbeatTimeoutMs !== undefined || entry.retry !== undefined) {
      options[def.name] = entry;
    }
  }
  return Object.keys(options).length > 0 ? options : undefined;
}

/**
 * Build a {@link CompensatePlan} from a step's `compensate:` (#299 D299-1). The
 * compensating activity is ordinary — its timeout/retry options ride the plan via
 * {@link referencedActivities} — so this node carries only the target name, the
 * optional `input_from` context path, and an optional per-compensation retry override.
 * Type-chain / declared-activity validation lives in {@link workflowCompensationError}.
 */
function compensatePlanFromSpec(compensate: WorkflowCompensateSpec, stepId: string): CompensatePlan {
  const plan: CompensatePlan = { activity: compensate.activity };
  if (compensate.input_from !== undefined) {
    plan.inputFrom = compensate.input_from;
  }
  if (compensate.retry !== undefined) {
    plan.retry = retryPolicyPlanFrom(compensate.retry, `step ${JSON.stringify(stepId)} compensate.retry`);
  }
  return plan;
}

/** Convert an `ActivityRetrySpec` (snake_case, seconds) to a plan `RetryPolicyPlan` (ms). */
function retryPolicyPlanFrom(retry: ActivityRetrySpec, label: string): RetryPolicyPlan {
  const plan: RetryPolicyPlan = {
    maximumAttempts: retry.maximum_attempts,
    initialIntervalMs: secondsToMs(retry.initial_interval_seconds, `${label}.initial_interval_seconds`),
    backoffCoefficient: retry.backoff_coefficient,
  };
  // `null` is the explicit "no backoff cap" value; a number maps to a millisecond cap.
  if (retry.maximum_interval_seconds !== null) {
    plan.maximumIntervalMs = secondsToMs(retry.maximum_interval_seconds, `${label}.maximum_interval_seconds`);
  }
  return plan;
}

/**
 * Convert a duration in (possibly fractional) seconds to whole milliseconds for Temporal. A positive
 * sub-millisecond value rounds to 0, which Temporal silently treats as "no value" (its internal
 * `ms ? … : undefined` guard drops a 0) — so clamp to >=1ms, ensuring a configured bound is never
 * silently removed (parity: Python keeps the microsecond value; both fire near-instantly). Throws on
 * a non-finite result (an absurd seconds value overflowing past *1000) rather than emitting an
 * Infinity that fails opaquely deep in Temporal at schedule time. `label` names the field for errors.
 */
function secondsToMs(seconds: number, label: string): number {
  const ms = Math.round(seconds * 1000);
  if (!Number.isFinite(ms)) {
    throw new Error(`${label} (${seconds} seconds) is too large to express as a millisecond duration`);
  }
  return Math.max(1, ms);
}

/**
 * Map + validate the review gate (#482 PR-B; Python `WorkflowSpec._validate_steps` +
 * `_validate_review_routes`): `after_step` must exist; every decision route and the timeout route
 * must exist and sit STRICTLY AFTER `after_step` (forward-only — no loops); and each route's tail
 * must type-check. Python walks resolved Python types; the TS analogue walks the spec's schema
 * REF strings (activity input/output refs; a map contributes its `collect.output` ref, or an
 * unknowable plain-array shape that ends ref checking for the rest of that tail).
 */
function reviewPlanFromSpec(
  spec: TypefluxYamlSpec,
  review: WorkflowLifecycleReviewSpec,
  steps: readonly WorkflowPlanStep[],
  subworkflowRefs: SubworkflowRefTable = new Map(),
  gateLabel = "review",
): ReviewPlan {
  const stepIndexes = new Map(steps.map((step, index) => [step.id, index]));
  const afterIndex = stepIndexes.get(review.after_step);
  if (afterIndex === undefined) {
    throw new Error(`${gateLabel} after_step references unknown step: ${JSON.stringify(review.after_step)}`);
  }
  const outputRefs = new Map<string, string | undefined>();
  const inputRefs = new Map<string, string | undefined>();
  for (const def of spec.activities.definitions ?? []) {
    outputRefs.set(def.name, def.output);
    inputRefs.set(def.name, def.input);
  }
  const stepOutputRef = (step: WorkflowPlanStep): string | undefined => {
    if (step.kind === "activity") {
      return outputRefs.get(step.activity);
    }
    if (step.kind === "subworkflow") {
      // The child's workflow.output ref, recorded at plan derivation (#55 §4.2).
      return subworkflowRefs.get(step.id)?.outputRef;
    }
    const specStep = spec.workflow.steps.find((s) => s.id === step.id);
    return step.kind === "map" || step.kind === "subworkflowMap"
      ? specStep?.map?.collect?.output
      : specStep?.parallel?.collect.output;
  };
  /** The ref a step consumes from the running value, where knowable (activity / subworkflow). */
  const stepInputRef = (step: WorkflowPlanStep): string | undefined => {
    if (step.kind === "activity") {
      return inputRefs.get(step.activity);
    }
    return step.kind === "subworkflow" ? subworkflowRefs.get(step.id)?.inputRef : undefined;
  };

  const checkRoute = (routeStepId: string, label: string): void => {
    const routeIndex = stepIndexes.get(routeStepId);
    if (routeIndex === undefined) {
      throw new Error(`${label} routes to unknown step: ${JSON.stringify(routeStepId)}`);
    }
    if (routeIndex <= afterIndex) {
      // Forward-only (Python load-time constraint): review routing can never loop back.
      throw new Error(
        `${label} routes to ${JSON.stringify(routeStepId)}, which is not strictly after ` +
          `after_step ${JSON.stringify(review.after_step)} (${gateLabel} routes are forward-only)`,
      );
    }
    // A `when` predicate in a routed tail must not read a context root the jump skips —
    // the gate would throw at runtime evaluation, after the route already committed.
    const checkGateRoots = (when: PlanWhen | undefined, roots: Set<string>, gateLabel: string): void => {
      if (when === undefined) {
        return;
      }
      for (const leaf of when.predicates) {
        const root = leaf.path.split(".")[0] ?? "";
        if (!roots.has(root)) {
          throw new Error(
            `${label} routes past step ${JSON.stringify(root)}, but the when predicate on ${gateLabel} ` +
              `reads ${JSON.stringify(leaf.path)} — the route would skip the step that produces it`,
          );
        }
      }
    };
    // A `compensate.input_from` in a routed tail must not reference a step the jump SKIPS (#299):
    // the compensated step still runs on the routed path, so it would push a compensation whose
    // input_from can't resolve — a valid route turned into a runtime workflow failure. The step's
    // OWN output is always available at push time (root === step.id is fine).
    const checkCompensateRoots = (step: WorkflowPlanStep, roots: Set<string>): void => {
      const compensate = (step as { compensate?: CompensatePlan }).compensate;
      if (compensate?.inputFrom === undefined) {
        return;
      }
      const root = compensate.inputFrom.split(".")[0] ?? "";
      // A MAP's own collected value isn't available when its items compensate, so `root === step.id`
      // is a valid own-reference for an ATOMIC step only (workflowCompensationError already rejects
      // a self-rooted map input_from before this runs; this stays defensive).
      const ownRefOk = root === step.id && step.kind !== "map";
      if (!ownRefOk && !roots.has(root)) {
        throw new Error(
          `${label} routes past step ${JSON.stringify(root)}, but step ${JSON.stringify(step.id)} ` +
            `compensate.input_from reads ${JSON.stringify(compensate.inputFrom)} — the route would skip ` +
            "the step whose output the compensation needs (#299)",
        );
      }
    };
    // A parallel block in a routed tail: only its ENTRY is route-dependent — the branches'
    // first steps consume the checkpoint output instead of the block's natural predecessor,
    // and nested maps/gates must not read a context root the jump skips. Everything strictly
    // internal (mid-branch chaining, gate typing) is route-invariant and already checked by
    // `workflowSchemaChainError`. `entryRef` is threaded to first steps only.
    const checkBranchTail = (
      branchSteps: readonly WorkflowPlanStep[],
      entryRef: string | undefined,
      roots: Set<string>,
      branchId: string,
    ): void => {
      let first = true;
      for (const nested of branchSteps) {
        checkGateRoots(nested.when, roots, `step ${JSON.stringify(nested.id)}`);
        checkCompensateRoots(nested, roots);
        if (nested.kind === "activity" || nested.kind === "subworkflow") {
          // Both consume the running value: an activity by its declared input ref, a
          // sub-workflow by its child's workflow.input ref (#55 §4.2).
          const inputRef = stepInputRef(nested);
          if (first && entryRef !== undefined && inputRef !== undefined && inputRef !== entryRef) {
            throw new Error(
              `${label} routes into a parallel block whose branch ${JSON.stringify(branchId)} starts at ` +
                `step ${JSON.stringify(nested.id)} expecting ${JSON.stringify(inputRef)}, but the ${gateLabel} ` +
                `checkpoint output is ${JSON.stringify(entryRef)}`,
            );
          }
        } else if (nested.kind === "map" || nested.kind === "subworkflowMap") {
          const overRoot = nested.over.split(".")[0] ?? "";
          if (!roots.has(overRoot)) {
            throw new Error(
              `${label} routes past step ${JSON.stringify(overRoot)}, but map step ${JSON.stringify(nested.id)} ` +
                `reads ${JSON.stringify(nested.over)} — the route would skip the step that produces it`,
            );
          }
        } else {
          for (const branch of nested.branches) {
            checkGateRoots(branch.when, roots, `branch ${JSON.stringify(branch.id)}`);
            checkBranchTail(branch.steps, first ? entryRef : undefined, new Set(roots), branch.id);
          }
        }
        for (const id of guaranteedContextIdsOf(nested)) {
          roots.add(id);
        }
        first = false;
      }
    };
    // Ref type-walk (the TS analogue of Python's resolved-type walk). `availableRoots` mirrors
    // Python's context_types: input + every step up to the checkpoint + tail steps as walked —
    // a routed map/gate must not read a context root the jump SKIPS (it would fail at runtime).
    // Steps contribute only their GUARANTEED subtree: a gated branch's inner results are
    // conditionally absent even when the block completed.
    const availableRoots = new Set<string>([
      "input",
      ...steps.slice(0, afterIndex + 1).flatMap((s) => guaranteedContextIdsOf(s)),
    ]);
    const workflowOutputRef = spec.workflow.output;
    let currentRef = stepOutputRef(steps[afterIndex] as WorkflowPlanStep);
    for (const step of steps.slice(routeIndex)) {
      checkGateRoots(step.when, availableRoots, `step ${JSON.stringify(step.id)}`);
      checkCompensateRoots(step, availableRoots);
      // The §3.3 gate-typing rule applies to routed tails too: a false gate on a tail
      // step completes the workflow with the value flowing AT that gate — for the route
      // target that is the CHECKPOINT output, not its natural predecessor's output. A
      // when-gated MAP step consumes no running value, so the tail chain check alone
      // would accept a route whose false gate returns the checkpoint ref despite
      // workflow.output (codex round; Python enforces the same on resolved types).
      if (
        step.when !== undefined &&
        workflowOutputRef !== undefined &&
        currentRef !== undefined &&
        currentRef !== workflowOutputRef
      ) {
        throw new Error(
          `${label} routes into a when-gated tail: a false gate on step ${JSON.stringify(step.id)} ` +
            `would complete the workflow with ${JSON.stringify(currentRef)}, but workflow.output is ` +
            `${JSON.stringify(workflowOutputRef)} — a gated sequence must exit with its declared result type`,
        );
      }
      if (step.kind === "activity" || step.kind === "subworkflow") {
        // Both consume the running value (#55 §4.2): the sub-workflow's input ref is
        // its child's workflow.input, recorded at plan derivation.
        const inputRef = stepInputRef(step);
        if (currentRef !== undefined && inputRef !== undefined && inputRef !== currentRef) {
          throw new Error(
            `${label} routes from ${JSON.stringify(review.after_step)} to step ${JSON.stringify(step.id)}, ` +
              `which expects ${JSON.stringify(inputRef)}, but the ${gateLabel} checkpoint output is ${JSON.stringify(currentRef)}`,
          );
        }
        currentRef = stepOutputRef(step);
      } else if (step.kind === "map" || step.kind === "subworkflowMap") {
        const overRoot = step.over.split(".")[0] ?? "";
        if (!availableRoots.has(overRoot)) {
          throw new Error(
            `${label} routes past step ${JSON.stringify(overRoot)}, but map step ${JSON.stringify(step.id)} ` +
              `reads ${JSON.stringify(step.over)} — the route would skip the step that produces it`,
          );
        }
        // A map's element typing is beyond ref checking; its output is the collect ref when
        // present, else an unknowable plain array.
        currentRef = stepOutputRef(step);
      } else {
        for (const branch of step.branches) {
          checkGateRoots(branch.when, availableRoots, `branch ${JSON.stringify(branch.id)}`);
          checkBranchTail(branch.steps, currentRef, new Set(availableRoots), branch.id);
        }
        // The block's output is its collect object; the chain resumes at its ref.
        currentRef = stepOutputRef(step);
      }
      for (const id of guaranteedContextIdsOf(step)) {
        availableRoots.add(id);
      }
    }
    if (workflowOutputRef !== undefined && currentRef !== undefined && currentRef !== workflowOutputRef) {
      throw new Error(
        `${label} route expects workflow output ${JSON.stringify(workflowOutputRef)}, ` +
          `but the routed tail returns ${JSON.stringify(currentRef)}`,
      );
    }
  };

  const userDecisions: Record<string, string> = Object.create(null) as Record<string, string>;
  for (const [decision, route] of Object.entries(review.user_decisions)) {
    checkRoute(route.route, `${gateLabel} user_decision ${JSON.stringify(decision)}`);
    userDecisions[decision] = route.route;
  }
  if (review.timeout?.on_timeout === "route" && review.timeout.route !== undefined) {
    checkRoute(review.timeout.route, `${gateLabel} timeout`);
  }
  const plan: ReviewPlan = {
    afterStep: review.after_step,
    userDecisions,
    invalidUserDecision: review.invalid_user_decision,
  };
  if (review.timeout !== undefined) {
    plan.timeoutSeconds = review.timeout.seconds;
    plan.onTimeout = review.timeout.on_timeout;
    if (review.timeout.route !== undefined) {
      plan.timeoutRoute = review.timeout.route;
    }
  }
  return plan;
}
