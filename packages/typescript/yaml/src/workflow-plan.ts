/**
 * The workflow step plan (parity Epic 5, #452) — a pure, sandbox-safe description
 * of a YAML workflow that the generic `typefluxYamlWorkflow` interpreter executes.
 * This module imports nothing (no `node:*`, no Zod), so the workflow bundle can
 * import `resolveContextPath` without pulling non-deterministic code.
 */

/** Default fan-out concurrency for a map step that omits `concurrency`. */
export const DEFAULT_MAP_CONCURRENCY = 5;

/**
 * Version of the generic interpreter's EXECUTION SEMANTICS (Python
 * `GENERATOR_VERSION` parity — its history: #60, #363, #368): bump when
 * `workflows.ts` changes what the SAME plan shape does. Folded into the frozen
 * `workflow.version` digest (#530), so a frozen label never silently spans two
 * different programs, and an upgrade that breaks every label does so with a
 * digest the operator can attribute to the interpreter, not their YAML.
 */
export const PLAN_INTERPRETER_VERSION = 2;

/**
 * Default activity `startToCloseTimeout` (milliseconds) when a definition omits one —
 * parity with Python's `DEFAULT_START_TO_CLOSE_TIMEOUT = timedelta(minutes=2)`.
 */
export const DEFAULT_START_TO_CLOSE_TIMEOUT_MS = 120_000;

/**
 * A bounded activity retry policy in milliseconds (the plan-carried form of Python's
 * `ActivityRetrySpec`). `maximumAttempts: 0` is Temporal's unlimited sentinel; `maximumIntervalMs`
 * absent means no backoff cap.
 */
export interface RetryPolicyPlan {
  maximumAttempts: number;
  initialIntervalMs: number;
  maximumIntervalMs?: number;
  backoffCoefficient: number;
}

/**
 * The default bounded retry policy applied when neither a definition's `retry` nor
 * `runtime.activity_retry` is set (parity with Python's `_default_activity_retry_policy`). Replaces
 * Temporal's own default of UNLIMITED attempts, which would let a deterministic provider failure
 * retry — and spend — without bound.
 */
export const DEFAULT_ACTIVITY_RETRY: RetryPolicyPlan = {
  maximumAttempts: 5,
  initialIntervalMs: 1000,
  maximumIntervalMs: 60_000,
  backoffCoefficient: 2,
};

/**
 * Per-activity Temporal options derived from a definition: `start_to_close_timeout_seconds` /
 * `heartbeat_timeout_seconds` (ms) and an optional `retry` override. All optional: an activity with
 * no override uses {@link DEFAULT_START_TO_CLOSE_TIMEOUT_MS}, no heartbeat, and the workflow-wide
 * retry (its own > `runtime.activity_retry` > {@link DEFAULT_ACTIVITY_RETRY}). The worker emits
 * background heartbeats while a `heartbeatTimeoutMs` activity runs (#484), so the timeout is safe.
 */
export interface ActivityTimeoutOptions {
  startToCloseTimeoutMs?: number;
  heartbeatTimeoutMs?: number;
  retry?: RetryPolicyPlan;
}

/**
 * The compensation unwind outcome (#299 D299-2), carried on the terminal lifecycle status/event:
 * `none` = nothing to unwind, `complete` = every compensation succeeded, `partial` = at least one
 * compensation activity failed. Defined here (the pure, sandbox-safe module) so both the
 * interpreter and the lifecycle runtime share it without a circular import.
 */
export type CompensationStatus = "none" | "complete" | "partial";

/** Comparison operators of the `when:` predicate DSL (#55 §3.2). */
export type PlanWhenOp = "eq" | "neq" | "lt" | "lte" | "gt" | "gte" | "in" | "exists";

/** All known operators, shared by the plan-shape guard and the evaluator. */
export const PLAN_WHEN_OPS: readonly PlanWhenOp[] = ["eq", "neq", "lt", "lte", "gt", "gte", "in", "exists"];

/** One leaf predicate: compare the context value at `path` against `value` with `op`. */
export interface PlanWhenLeaf {
  path: string;
  op: PlanWhenOp;
  value: unknown;
}

/**
 * A normalized `when:` gate carried by the plan (#55 §3.2): one leaf, or exactly one
 * `all`/`any` composition level over leaves (decision D1 — never nested). Pure JSON, so
 * it folds into the canonical plan digest; V1 plans never carry the key.
 */
export interface PlanWhen {
  mode: "leaf" | "all" | "any";
  predicates: PlanWhenLeaf[];
}

/**
 * Evaluate one leaf against recorded context. Comparisons are strict and loud: an
 * unresolvable path under a non-`exists` operator throws (`resolveContextPath`'s
 * error), and an ordering comparison over mismatched or unorderable types throws
 * rather than silently gating false — predicates are load-validated data, so a type
 * surprise here is a real spec/runtime divergence the operator must see.
 */
function evaluateWhenLeaf(leaf: PlanWhenLeaf, context: Record<string, unknown>): boolean {
  if (leaf.op === "exists") {
    let exists = true;
    try {
      resolveContextPath(context, leaf.path);
    } catch {
      exists = false;
    }
    return exists === (leaf.value === true);
  }
  const resolved = resolveContextPath(context, leaf.path);
  switch (leaf.op) {
    case "eq":
      return resolved === leaf.value;
    case "neq":
      return resolved !== leaf.value;
    case "in":
      return Array.isArray(leaf.value) && leaf.value.some((candidate) => candidate === resolved);
    default: {
      if (
        typeof resolved !== typeof leaf.value ||
        (typeof resolved !== "number" && typeof resolved !== "string")
      ) {
        throw new Error(
          `when predicate ${JSON.stringify(leaf.path)} ${leaf.op} ${JSON.stringify(leaf.value)} cannot order ` +
            `a ${typeof resolved} against a ${typeof leaf.value}`,
        );
      }
      const value = leaf.value as number | string;
      switch (leaf.op) {
        case "lt":
          return resolved < value;
        case "lte":
          return resolved <= value;
        case "gt":
          return resolved > value;
        default:
          return resolved >= value;
      }
    }
  }
}

/** Evaluate a normalized `when:` gate against recorded context (deterministic: data only). */
export function evaluatePlanWhen(when: PlanWhen, context: Record<string, unknown>): boolean {
  if (when.mode === "any") {
    return when.predicates.some((leaf) => evaluateWhenLeaf(leaf, context));
  }
  // "leaf" carries exactly one predicate, so `every` covers both modes.
  return when.predicates.every((leaf) => evaluateWhenLeaf(leaf, context));
}

/** Render one leaf as the canonical condition text (`classify.risk < 0.3`). */
function renderWhenLeaf(leaf: PlanWhenLeaf): string {
  switch (leaf.op) {
    case "eq":
      return `${leaf.path} == ${JSON.stringify(leaf.value)}`;
    case "neq":
      return `${leaf.path} != ${JSON.stringify(leaf.value)}`;
    case "lt":
      return `${leaf.path} < ${JSON.stringify(leaf.value)}`;
    case "lte":
      return `${leaf.path} <= ${JSON.stringify(leaf.value)}`;
    case "gt":
      return `${leaf.path} > ${JSON.stringify(leaf.value)}`;
    case "gte":
      return `${leaf.path} >= ${JSON.stringify(leaf.value)}`;
    case "in":
      return `${leaf.path} in ${JSON.stringify(leaf.value)}`;
    default:
      return leaf.value === true ? `${leaf.path} exists` : `${leaf.path} not exists`;
  }
}

/**
 * The canonical text of a `when:` gate — shared by `step_skipped` lifecycle events and
 * the topology projection's `condition` edges (#55 §3.3/§7), so "what could run" and
 * "what ran and why" join on identical strings. Cross-edition wire data: Python renders
 * the same text in slice 2.
 */
export function renderPlanWhen(when: PlanWhen): string {
  const parts = when.predicates.map(renderWhenLeaf);
  if (when.mode === "leaf") {
    return parts[0] as string;
  }
  return `${when.mode}(${parts.join(", ")})`;
}

/**
 * Compensation carried by a completed step (#299 D299-1): on failure OR cancellation the
 * interpreter walks the workflow-local LIFO stack in reverse and runs each entry's
 * `activity` to undo the step's side effect. `inputFrom` is a context dot-path resolved at
 * PUSH time (the compensated step's own output when absent — for a map step, each completed
 * item's own result); `retry` overrides the default bounded activity retry for this
 * compensation only. Pure JSON, so it folds into the canonical plan digest; steps with no
 * `compensate:` never carry the key (V1 plan digests hold).
 */
export interface CompensatePlan {
  activity: string;
  /** Context dot-path for the compensation input; absent = the compensated step's own output. */
  inputFrom?: string;
  /**
   * Per-compensation retry override (ms form); absent = {@link DEFAULT_ACTIVITY_RETRY}.
   *
   * RETRY DIGEST DIVERGENCE (#299 review — decided, not a bug): this field rides the plan, so
   * `workflowPlanDigest` (which hashes the whole plan) includes it in the TS spec digest —
   * consistent with how THIS edition already treats ordinary activity retry
   * (`activityOptions[name].retry` is likewise in the plan). Python EXCLUDES both from its
   * command-graph digest. The two profiles hash different artifacts by design (retry has
   * diverged this way since before #299); #299 keeps `compensate.retry` consistent with each
   * edition's existing retry treatment. See `identity.py` `_compensate_payload`.
   */
  retry?: RetryPolicyPlan;
}

/** Run a single activity over the current value. */
export interface ActivityPlanStep {
  kind: "activity";
  id: string;
  activity: string;
  /** Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3). */
  when?: PlanWhen;
  /** Compensation for this step (#299 D299-1); pushed onto the LIFO when the step completes. */
  compensate?: CompensatePlan;
}

/**
 * Cache-prep/release activity-name suffixes. Pure duplicates of the canonical
 * `@typeflux/temporal` constants (that module imports `node:crypto`, which the
 * workflow sandbox cannot load); a test pins the strings equal.
 */
export const CACHE_PREP_ACTIVITY_SUFFIX = ".__prepare_cache__";
export const CACHE_RELEASE_ACTIVITY_SUFFIX = ".__release_cache__";

/** The session-cache config carried by a map step (#478; from the activity definition). */
export interface SessionCachePlan {
  enabled: boolean;
  ttlSeconds?: number;
}

/** Fan an activity over the items at `over`, then (optionally) collect into `{ [collectField]: results }`. */
export interface MapPlanStep {
  kind: "map";
  id: string;
  activity: string;
  over: string;
  concurrency?: number;
  collectField?: string;
  /**
   * An actionable size bound on the COLLECTED payload (#495; Python `collect.max_bytes`),
   * checked before Temporal's opaque ~2MB per-payload limit. Absent or <= 0 = no limit.
   * Participates in the plan (a workflow argument), so a bound change is replay-visible.
   */
  collectMaxBytes?: number;
  /**
   * Present when the activity definition opts into the provider session cache (#478):
   * the interpreter runs `<activity>.__prepare_cache__` once before the fan-out,
   * threads the handle to every item, and releases a reference-style cache after.
   * Participates in the plan (a workflow argument), so cache-config changes are
   * replay-visible.
   */
  sessionCache?: SessionCachePlan;
  /** Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3). */
  when?: PlanWhen;
  /**
   * Compensation for this map step (#299 D299-1): pushed PER COMPLETED ITEM at map completion
   * in item order, so the flat LIFO unwinds them in reverse item order. Each item's default
   * compensation input is its OWN result (an explicit `inputFrom` resolves against context,
   * constant across items).
   */
  compensate?: CompensatePlan;
}

/** One branch of a parallel block: a nested step sequence, optionally gated. */
export interface ParallelPlanBranch {
  id: string;
  /** Skip the WHOLE branch when false; its collect field is null (#55 §3.3). */
  when?: PlanWhen;
  steps: WorkflowPlanStep[];
}

/**
 * Run branches concurrently over the current value, then merge their terminal values
 * into `{ [branchId]: value | null }` — the collect object, whose fields ARE the branch
 * ids (#55 §3.1, decision D4). The collect schema ref is a load-time artifact and does
 * not ride the plan; `collectMaxBytes` guards the merged payload exactly like a map's.
 */
export interface ParallelPlanStep {
  kind: "parallel";
  id: string;
  branches: ParallelPlanBranch[];
  collectMaxBytes?: number;
  /** Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3). */
  when?: PlanWhen;
}

/**
 * The resolve-time-embedded identity of a sub-workflow invocation (#55 §6,
 * ts-plan-argument profile). `workflowIdentityMemo` cannot run where `executeChild`
 * runs — it takes a `TypefluxYamlSpec` and the digest needs `node:crypto`, neither of
 * which exists in the workflow sandbox — so the plan node carries everything the
 * interpreter needs to stamp the child's identity memo and start it. Because
 * `workflowPlanDigest` hashes the whole parent plan, the embedded child plan AND its
 * `childDigest` fold into the parent digest by construction: a child-only edit moves
 * the parent digest (the frozen-label cascade, documented in docs/yaml.md).
 */
export interface SubworkflowChildIdentity {
  /** The sibling workflow id as declared in the project manifest (the `workflow:` value). */
  workflowId: string;
  /** The child's logical workflow name (`spec.workflow.name`) — its OWN drain/visibility identity. */
  workflowName: string;
  /** The child's project (`spec.project`); same-project by construction (#55 §3.4). */
  project: string;
  /** `workflowPlanDigest` of the embedded child plan, computed at resolve time. */
  childDigest: string;
  /** The child's `workflow.version` label, when it declares one. */
  versionLabel?: string;
  /** The child's resolved plan — the argument the interpreter passes to `executeChild`. */
  plan: WorkflowPlan;
}

/**
 * Assemble a child's identity memo from plan-node fields alone (#55 §6): the sandbox cannot
 * run `workflowIdentityMemo` (it needs a `TypefluxYamlSpec` and node:crypto), so the
 * resolve-time-embedded identity carries the digest/name/project/label. The parent-link key
 * (decision D2) rides here too — Temporal's ParentWorkflowExecution stays the authoritative
 * link; the memo spares memo-only correlation readers (the CP `children` listing) a
 * describe round-trip.
 *
 * KEY-SET CONTRACT (drift guard): these keys MUST stay exactly `workflowIdentityMemo`'s
 * (frozen-version.ts — typeflux_spec_digest / typeflux_workflow / typeflux_project /
 * typeflux_workflow_version-when-labeled) plus `typeflux_parent_workflow_id`. The drain view,
 * frozen-version scan, and correlation listing all read these literals; a rename here without
 * the twin silently breaks child visibility. Pinned by a unit test comparing both key sets.
 */
export function childIdentityMemo(
  identity: SubworkflowChildIdentity,
  parentWorkflowId: string,
): Record<string, string> {
  return {
    typeflux_spec_digest: identity.childDigest,
    typeflux_workflow: identity.workflowName,
    typeflux_project: identity.project,
    ...(identity.versionLabel !== undefined ? { typeflux_workflow_version: identity.versionLabel } : {}),
    typeflux_parent_workflow_id: parentWorkflowId,
  };
}

/**
 * Run a sibling project workflow as a Temporal CHILD workflow (#55 §3.4/§6): its own
 * workflow id (`{parent_workflow_id}.{step_id}`), history budget, memo identity, drain
 * row, and binding verification. `WorkflowIdReusePolicy: ALLOW_DUPLICATE` +
 * `ParentClosePolicy: TERMINATE` per the binding-contract note; the task queue is
 * inherited from the parent.
 */
export interface SubworkflowPlanStep extends SubworkflowChildIdentity {
  kind: "subworkflow";
  id: string;
  /** Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3). */
  when?: PlanWhen;
  /**
   * The PARENT-SIDE compensation for having invoked the child (#299 D299-1/D299-3): the child
   * unwinds its OWN stack internally on its own failure; this is the parent's inverse of the
   * child's net effect. Default compensation input is the child's result.
   */
  compensate?: CompensatePlan;
}

/**
 * Fan a sub-workflow over the items at `over` with V1 map semantics (#55 §3.4
 * `map.workflow`): bounded `concurrency`, child ids `{parent_workflow_id}.{step_id}-{index}`,
 * optional collect into `{ [collectField]: results }` guarded by `collectMaxBytes`
 * (the map guard semantics and failure type, exactly).
 */
export interface SubworkflowMapPlanStep extends SubworkflowChildIdentity {
  kind: "subworkflowMap";
  id: string;
  over: string;
  concurrency?: number;
  collectField?: string;
  collectMaxBytes?: number;
  /** Skip this step AND the remainder of its enclosing sequence when false (#55 §3.3). */
  when?: PlanWhen;
}

export type WorkflowPlanStep =
  | ActivityPlanStep
  | MapPlanStep
  | ParallelPlanStep
  | SubworkflowPlanStep
  | SubworkflowMapPlanStep;

/**
 * Every LEAF (activity/map) step of a plan subtree, depth-first in declared order —
 * parallel nodes contribute their branches' steps, not themselves. The one walk shared
 * by every consumer that reasons per-activity (worker build, validation projections,
 * bundle/catalog), so none of them can miss a nested step. Sub-workflow steps
 * contribute NOTHING: they call no activity of the parent's — their interior belongs
 * to the child execution's own plan, worker map, and projections (#55 §6).
 */
export function flattenPlanSteps(steps: readonly WorkflowPlanStep[]): (ActivityPlanStep | MapPlanStep)[] {
  const leaves: (ActivityPlanStep | MapPlanStep)[] = [];
  for (const step of steps) {
    if (step.kind === "parallel") {
      for (const branch of step.branches) {
        leaves.push(...flattenPlanSteps(branch.steps));
      }
    } else if (step.kind === "activity" || step.kind === "map") {
      leaves.push(step);
    }
  }
  return leaves;
}

/**
 * Every `compensate:`-bearing step's (stepId, compensating activity name), depth-first in
 * declared order, recursing into parallel branches (#299). Compensation nodes are NOT leaf
 * steps, so {@link flattenPlanSteps} misses them — worker assembly walks THIS too, so a
 * typo'd `compensate.activity` fails the registered-set check at build, not mid-unwind.
 */
export function flattenCompensationActivities(
  steps: readonly WorkflowPlanStep[],
): { stepId: string; activity: string }[] {
  const out: { stepId: string; activity: string }[] = [];
  for (const step of steps) {
    if (step.kind === "parallel") {
      for (const branch of step.branches) {
        out.push(...flattenCompensationActivities(branch.steps));
      }
    } else if (
      (step.kind === "activity" || step.kind === "map" || step.kind === "subworkflow") &&
      step.compensate !== undefined
    ) {
      out.push({ stepId: step.id, activity: step.compensate.activity });
    }
  }
  return out;
}

/**
 * Every sub-workflow node of a plan subtree, depth-first in declared order (plain
 * steps and map fan-outs alike) — the walk consumers that reason per-CHILD share
 * (worker assembly's activity merge, projections, tests).
 */
export function flattenSubworkflowSteps(
  steps: readonly WorkflowPlanStep[],
): (SubworkflowPlanStep | SubworkflowMapPlanStep)[] {
  const nodes: (SubworkflowPlanStep | SubworkflowMapPlanStep)[] = [];
  for (const step of steps) {
    if (step.kind === "parallel") {
      for (const branch of step.branches) {
        nodes.push(...flattenSubworkflowSteps(branch.steps));
      }
    } else if (step.kind === "subworkflow" || step.kind === "subworkflowMap") {
      nodes.push(step);
    }
  }
  return nodes;
}

/**
 * Lifecycle config carried by the plan (#482, Python `WorkflowLifecycleSpec` with
 * `enabled: true`). Present only when the spec enables the lifecycle; the interpreter then
 * registers the `typeflux_lifecycle_status` query + `typeflux_request_cancel` /
 * `typeflux_submit_review` signals and records status events.
 */
export interface LifecyclePlan {
  /** Progress-unit tracking (Python `progress`, default true). */
  progress: boolean;
  /** Honor `typeflux_request_cancel` (Python `cancellation`, default true). */
  cancellation: boolean;
  /** Status-event ring buffer size; 0 retains no event history (Python deque(maxlen=0)). */
  statusEventLimit: number;
  /**
   * The single human-in-the-loop review gate (#482 PR-B); absent = no single gate. Present
   * ONLY for V1 `lifecycle.review` specs — its serialization is byte-identical to pre-slice-4,
   * so V1 plan digests hold. A `lifecycle.gates` spec carries {@link gates} instead (#55 §8).
   */
  review?: ReviewPlan;
  /**
   * Multiple named review gates (#55 slice 4); absent for V1 specs. Mutually exclusive with
   * {@link review} at load. Each gate is a {@link ReviewPlan} plus its `id`.
   */
  gates?: GatePlan[];
}

/** The review gate carried by the plan (Python `WorkflowLifecycleReviewSpec`). */
export interface ReviewPlan {
  /** The step id after which the workflow waits for a review decision. */
  afterStep: string;
  /** decision string -> the (forward-only, load-validated) step id to route to. */
  userDecisions: Record<string, string>;
  /** Unknown decision policy: warn keeps waiting; fail terminates (Python default warn). */
  invalidUserDecision: "warn" | "fail";
  /** Absolute review deadline in seconds (never extended by signals); absent = wait forever. */
  timeoutSeconds?: number;
  /** Timeout action (Python default fail). */
  onTimeout?: "fail" | "cancel" | "route";
  /** The timeout route (present exactly when onTimeout is `route`). */
  timeoutRoute?: string;
}

/** One named gate carried by the plan (Python `WorkflowLifecycleGateSpec`): a {@link ReviewPlan} + `id`. */
export interface GatePlan extends ReviewPlan {
  /** The gate id — names it in the status `waiting_gates` wire and the `gate` signal field. */
  id: string;
}

export interface WorkflowPlan {
  steps: WorkflowPlanStep[];
  /** Lifecycle signals/query/status config — absent means disabled (#482). */
  lifecycle?: LifecyclePlan;
  /**
   * Per-activity-name timeout/retry overrides. An activity absent from this map (or the map being
   * undefined) uses {@link DEFAULT_START_TO_CLOSE_TIMEOUT_MS}, no heartbeat, and the workflow-wide
   * retry. The interpreter reads it with `Object.hasOwn` so an activity named like an inherited
   * member is safe.
   */
  activityOptions?: Record<string, ActivityTimeoutOptions>;
  /**
   * The workflow-wide retry policy from `runtime.activity_retry`, applied to any activity without
   * its own `retry`. Absent → {@link DEFAULT_ACTIVITY_RETRY}.
   */
  retryPolicy?: RetryPolicyPlan;
}

/**
 * A Temporal `RetryPolicy` shape (ms durations) — a structural subset, built without importing
 * `@temporalio`. `maximumAttempts` is OMITTED for unlimited (Temporal TS rejects `<= 0` and treats
 * an absent field as unlimited; `@default Infinity`), so the plan's `0` sentinel never reaches it.
 */
export interface ActivityRetryProxyOptions {
  maximumAttempts?: number;
  initialInterval: number;
  maximumInterval?: number;
  backoffCoefficient: number;
}

/** Temporal proxy options (ms) — a subset of `@temporalio` `ActivityOptions`. */
export interface ActivityProxyOptions {
  startToCloseTimeout: number;
  heartbeatTimeout?: number;
  retry: ActivityRetryProxyOptions;
}

/**
 * The resolved (plan-sentinel-shaped) retry policy for an activity: the definition's own >
 * the workflow-wide `plan.retryPolicy` > {@link DEFAULT_ACTIVITY_RETRY} — the single
 * precedence rule shared by the interpreter's proxy options and the control-plane bundle's
 * `effective_retry`, so the two can never drift. `maximumAttempts: 0` stays the unlimited
 * sentinel here (Python `BundleRetryPolicy` shape); only the Temporal-facing
 * {@link activityProxyOptions} drops it.
 */
export function resolvedActivityRetryPlan(plan: WorkflowPlan, name: string): RetryPolicyPlan {
  const opts =
    plan.activityOptions !== undefined && Object.hasOwn(plan.activityOptions, name)
      ? plan.activityOptions[name]
      : undefined;
  return opts?.retry ?? plan.retryPolicy ?? DEFAULT_ACTIVITY_RETRY;
}

/**
 * Resolve the Temporal proxy options for an activity: its plan override (looked up with
 * `Object.hasOwn`, so an activity named like an inherited member is safe) or, when absent,
 * {@link DEFAULT_START_TO_CLOSE_TIMEOUT_MS} with no heartbeat. `startToCloseTimeout` always falls
 * back to the default even when only a heartbeat is configured (parity with Python's
 * `start_to_close_timeout or DEFAULT_START_TO_CLOSE_TIMEOUT`); `heartbeatTimeout` is set only when
 * configured. The retry policy resolves the definition's own > the workflow-wide
 * `plan.retryPolicy` > {@link DEFAULT_ACTIVITY_RETRY} (parity with Python's `_resolve_retry_policy`),
 * so every activity carries a bounded policy instead of Temporal's unlimited default. Pure (no
 * Temporal import) so the resolution is unit-testable; the sandbox interpreter passes the result
 * straight to `proxyActivities`.
 */
export function activityProxyOptions(plan: WorkflowPlan, name: string): ActivityProxyOptions {
  const opts =
    plan.activityOptions !== undefined && Object.hasOwn(plan.activityOptions, name)
      ? plan.activityOptions[name]
      : undefined;
  const retryPlan = resolvedActivityRetryPlan(plan, name);
  const retry: ActivityRetryProxyOptions = {
    initialInterval: retryPlan.initialIntervalMs,
    backoffCoefficient: retryPlan.backoffCoefficient,
  };
  // `0` is the spec's unlimited sentinel; Temporal TS rejects `maximumAttempts <= 0` and treats an
  // omitted field as unlimited, so drop it for 0 rather than passing it through.
  if (retryPlan.maximumAttempts > 0) {
    retry.maximumAttempts = retryPlan.maximumAttempts;
  }
  if (retryPlan.maximumIntervalMs !== undefined) {
    retry.maximumInterval = retryPlan.maximumIntervalMs;
  }
  const result: ActivityProxyOptions = {
    startToCloseTimeout: opts?.startToCloseTimeoutMs ?? DEFAULT_START_TO_CLOSE_TIMEOUT_MS,
    retry,
  };
  if (opts?.heartbeatTimeoutMs !== undefined) {
    result.heartbeatTimeout = opts.heartbeatTimeoutMs;
  }
  return result;
}

/**
 * Proxy options for the cache-prep/release activities of a map step (#478): the map
 * activity's own timeout/retry, but NEVER its `heartbeatTimeout` — prep/release emit
 * no heartbeats, so inheriting one would have Temporal kill them (a failure the
 * in-activity fail-soft cannot catch, defeating "caching never fails the workflow",
 * Python `_cache_activity_kwargs` / #368).
 */
export function cacheActivityProxyOptions(plan: WorkflowPlan, name: string): ActivityProxyOptions {
  const options = activityProxyOptions(plan, name);
  const cacheOptions: ActivityProxyOptions = {
    startToCloseTimeout: options.startToCloseTimeout,
    retry: options.retry,
  };
  return cacheOptions;
}

/**
 * UTF-8 byte length of a string in pure JS — no `Buffer`/`TextEncoder`, so the
 * computation is unquestionably sandbox-safe and deterministic across replay
 * (#495 collect.max_bytes; Python measures `model_dump_json().encode("utf-8")`).
 */
export function utf8ByteLength(text: string): number {
  let bytes = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.codePointAt(index) as number;
    if (code > 0xffff) {
      index += 1; // the code point consumed a surrogate pair
    }
    bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
  }
  return bytes;
}

/**
 * The plan-shape guard (#55 §5.2): every key/kind the interpreter implements, per plan
 * level. The plan is a client-supplied workflow ARGUMENT with zero runtime validation —
 * an OLD worker handed a NEW-shape plan must fail loud and nonRetryable instead of
 * misexecuting it. Without this guard the two concrete failure modes are: a step of an
 * unknown kind falls into the map arm and TypeErrors on the missing `over` (an
 * indefinite workflow-task retry hang), and a plan whose `steps` is empty no-op-completes
 * (the while loop runs zero times and returns the input as a "successful" result).
 * `PLAN_INTERPRETER_VERSION` protects neither case — it is only hashed into the digest
 * envelope, never checked at execution.
 *
 * Strictness is deliberate: the plan is an internal artifact, so ANY unknown key — a new
 * step kind (a `subworkflow` node, #55 slice 3), a new `when` operator, a lifecycle
 * `gates` list — implies semantics this worker does not implement, and silently ignoring
 * it would misexecute the workflow. Key sets, kinds, and operator names are checked here
 * (shape novelty, recursively through branches); value types stay the job of the
 * interpreter arms and the strict activity boundary, as today.
 */
const KNOWN_PLAN_KEYS = new Set(["steps", "lifecycle", "activityOptions", "retryPolicy"]);
const SUBWORKFLOW_IDENTITY_KEYS = ["workflowId", "workflowName", "project", "childDigest", "plan"] as const;
const KNOWN_STEP_KEYS: Record<string, ReadonlySet<string>> = {
  activity: new Set(["kind", "id", "activity", "when", "compensate"]),
  map: new Set(["kind", "id", "activity", "over", "concurrency", "collectField", "collectMaxBytes", "sessionCache", "when", "compensate"]),
  parallel: new Set(["kind", "id", "branches", "collectMaxBytes", "when"]),
  subworkflow: new Set(["kind", "id", ...SUBWORKFLOW_IDENTITY_KEYS, "versionLabel", "when", "compensate"]),
  subworkflowMap: new Set([
    "kind",
    "id",
    ...SUBWORKFLOW_IDENTITY_KEYS,
    "versionLabel",
    "over",
    "concurrency",
    "collectField",
    "collectMaxBytes",
    "when",
  ]),
};
const REQUIRED_STEP_KEYS: Record<string, readonly string[]> = {
  activity: ["id", "activity"],
  map: ["id", "activity", "over"],
  parallel: ["id", "branches"],
  subworkflow: ["id", ...SUBWORKFLOW_IDENTITY_KEYS],
  subworkflowMap: ["id", ...SUBWORKFLOW_IDENTITY_KEYS, "over"],
};
const KNOWN_BRANCH_KEYS = new Set(["id", "when", "steps"]);
const KNOWN_COMPENSATE_KEYS = new Set(["activity", "inputFrom", "retry"]);
const KNOWN_RETRY_KEYS = new Set(["maximumAttempts", "initialIntervalMs", "maximumIntervalMs", "backoffCoefficient"]);
const KNOWN_WHEN_KEYS = new Set(["mode", "predicates"]);
const KNOWN_WHEN_MODES = new Set(["leaf", "all", "any"]);
const KNOWN_WHEN_LEAF_KEYS = new Set(["path", "op", "value"]);
const KNOWN_LIFECYCLE_KEYS = new Set(["progress", "cancellation", "statusEventLimit", "review", "gates"]);
const KNOWN_REVIEW_KEYS = new Set([
  "afterStep",
  "userDecisions",
  "invalidUserDecision",
  "timeoutSeconds",
  "onTimeout",
  "timeoutRoute",
]);
// A gate node is a review node plus its `id` (#55 slice 4).
const KNOWN_GATE_KEYS = new Set(["id", ...KNOWN_REVIEW_KEYS]);

/** The keys of `value` not in `known`, sorted (empty = fine). */
function unknownKeysOf(value: object, known: ReadonlySet<string>): string[] {
  return Object.keys(value)
    .filter((key) => !known.has(key))
    .sort();
}

/** Why a plan-carried `when` gate is not interpretable, or `undefined` (guard depth: shape novelty). */
function whenUnsupportedReason(when: unknown, label: string): string | undefined {
  if (when === null || typeof when !== "object" || Array.isArray(when)) {
    return `the when gate on ${label} is not an object`;
  }
  const unknownKeys = unknownKeysOf(when, KNOWN_WHEN_KEYS);
  if (unknownKeys.length > 0) {
    return `the when gate on ${label} carries key(s) this worker does not implement: ${unknownKeys.join(", ")}`;
  }
  const mode = (when as { mode?: unknown }).mode;
  if (typeof mode !== "string" || !KNOWN_WHEN_MODES.has(mode)) {
    return `the when gate on ${label} has mode ${JSON.stringify(mode)}, which this worker does not implement`;
  }
  const predicates = (when as { predicates?: unknown }).predicates;
  if (!Array.isArray(predicates) || predicates.length === 0) {
    return `the when gate on ${label} has no predicates`;
  }
  if (mode === "leaf" && predicates.length !== 1) {
    // The evaluator ANDs and the renderer takes predicates[0]: a multi-predicate "leaf"
    // would gate on criteria the rendered condition text hides — reject the shape.
    return `the when gate on ${label} has mode "leaf" with ${predicates.length} predicates (exactly 1 expected)`;
  }
  for (const leaf of predicates) {
    if (leaf === null || typeof leaf !== "object" || Array.isArray(leaf)) {
      return `a when predicate on ${label} is not an object`;
    }
    const unknownLeafKeys = unknownKeysOf(leaf, KNOWN_WHEN_LEAF_KEYS);
    if (unknownLeafKeys.length > 0) {
      return `a when predicate on ${label} carries key(s) this worker does not implement: ${unknownLeafKeys.join(", ")}`;
    }
    const op = (leaf as { op?: unknown }).op;
    if (typeof op !== "string" || !(PLAN_WHEN_OPS as readonly string[]).includes(op)) {
      return `a when predicate on ${label} has operator ${JSON.stringify(op)}, which this worker does not implement`;
    }
  }
  return undefined;
}

/** Why a plan-carried `compensate` node is not interpretable, or `undefined` (guard depth: shape novelty). */
function compensateUnsupportedReason(compensate: unknown, label: string): string | undefined {
  if (compensate === null || typeof compensate !== "object" || Array.isArray(compensate)) {
    return `the compensate config on ${label} is not an object`;
  }
  const unknownKeys = unknownKeysOf(compensate, KNOWN_COMPENSATE_KEYS);
  if (unknownKeys.length > 0) {
    return `the compensate config on ${label} carries key(s) this worker does not implement: ${unknownKeys.join(", ")}`;
  }
  const activity = (compensate as { activity?: unknown }).activity;
  if (typeof activity !== "string" || activity.length === 0) {
    return `the compensate config on ${label} is missing its string activity`;
  }
  const inputFrom = (compensate as { inputFrom?: unknown }).inputFrom;
  if (inputFrom !== undefined && typeof inputFrom !== "string") {
    return `the compensate config on ${label} has a non-string inputFrom`;
  }
  const retry = (compensate as { retry?: unknown }).retry;
  if (retry !== undefined) {
    if (retry === null || typeof retry !== "object" || Array.isArray(retry)) {
      return `the compensate config on ${label} has a non-object retry`;
    }
    const unknownRetryKeys = unknownKeysOf(retry, KNOWN_RETRY_KEYS);
    if (unknownRetryKeys.length > 0) {
      return `the compensate retry on ${label} carries key(s) this worker does not implement: ${unknownRetryKeys.join(", ")}`;
    }
  }
  return undefined;
}

/** Why a step list (top level or a branch sequence) is not interpretable, or `undefined`. */
function stepsUnsupportedReason(steps: unknown): string | undefined {
  if (!Array.isArray(steps) || steps.length === 0) {
    // steps: [] would otherwise no-op-complete; steps absent would TypeError mid-loop.
    return "the plan has no steps (a plan this worker cannot interpret must fail, not silently complete)";
  }
  for (const step of steps) {
    if (step === null || typeof step !== "object" || Array.isArray(step)) {
      return "a plan step is not an object";
    }
    const kind = (step as { kind?: unknown }).kind;
    const id = (step as { id?: unknown }).id;
    const label = typeof id === "string" ? JSON.stringify(id) : "<missing id>";
    if (typeof kind !== "string" || !Object.hasOwn(KNOWN_STEP_KEYS, kind)) {
      return `plan step ${label} has kind ${JSON.stringify(kind)}, which this worker does not implement`;
    }
    const missing = (REQUIRED_STEP_KEYS[kind] as readonly string[]).filter((key) => !Object.hasOwn(step, key));
    if (missing.length > 0) {
      return `plan step ${label} (kind ${JSON.stringify(kind)}) is missing required key(s): ${missing.join(", ")}`;
    }
    const unknownStepKeys = unknownKeysOf(step, KNOWN_STEP_KEYS[kind] as ReadonlySet<string>);
    if (unknownStepKeys.length > 0) {
      return `plan step ${label} carries key(s) this worker does not implement: ${unknownStepKeys.join(", ")}`;
    }
    const when = (step as { when?: unknown }).when;
    if (when !== undefined) {
      const whenReason = whenUnsupportedReason(when, `step ${label}`);
      if (whenReason !== undefined) {
        return whenReason;
      }
    }
    const compensate = (step as { compensate?: unknown }).compensate;
    if (compensate !== undefined) {
      const compensateReason = compensateUnsupportedReason(compensate, `step ${label}`);
      if (compensateReason !== undefined) {
        return compensateReason;
      }
    }
    if (kind === "subworkflow" || kind === "subworkflowMap") {
      // The embedded child plan is executed by THIS worker's interpreter via
      // executeChild, so a child shape this worker cannot interpret must fail at
      // the PARENT's entry — before any work runs — not one child start later.
      const childReason = planUnsupportedReason((step as { plan?: unknown }).plan);
      if (childReason !== undefined) {
        return `the sub-workflow plan embedded in step ${label} is unsupported: ${childReason}`;
      }
    }
    if (kind === "parallel") {
      const branches = (step as { branches?: unknown }).branches;
      if (!Array.isArray(branches) || branches.length === 0) {
        return `plan step ${label} has no branches`;
      }
      const seenBranchIds = new Set<string>();
      for (const branch of branches) {
        if (branch === null || typeof branch !== "object" || Array.isArray(branch)) {
          return `a branch of plan step ${label} is not an object`;
        }
        const unknownBranchKeys = unknownKeysOf(branch, KNOWN_BRANCH_KEYS);
        if (unknownBranchKeys.length > 0) {
          return `a branch of plan step ${label} carries key(s) this worker does not implement: ${unknownBranchKeys.join(", ")}`;
        }
        const branchId = (branch as { id?: unknown }).id;
        // Branch ids are the collect object's field names: a missing id would collect
        // under "undefined", and a duplicate would silently overwrite a sibling's result.
        if (typeof branchId !== "string") {
          return `a branch of plan step ${label} has no id`;
        }
        if (seenBranchIds.has(branchId)) {
          return `plan step ${label} has duplicate branch id ${JSON.stringify(branchId)}`;
        }
        seenBranchIds.add(branchId);
        const branchLabel = JSON.stringify(branchId);
        const branchWhen = (branch as { when?: unknown }).when;
        if (branchWhen !== undefined) {
          const whenReason = whenUnsupportedReason(branchWhen, `branch ${branchLabel}`);
          if (whenReason !== undefined) {
            return whenReason;
          }
        }
        const nested = stepsUnsupportedReason((branch as { steps?: unknown }).steps);
        if (nested !== undefined) {
          return nested;
        }
      }
    }
  }
  return undefined;
}

/**
 * Why this interpreter cannot execute `plan`, or `undefined` when it can (#55 §5.2).
 * Pure and sandbox-safe so `typefluxYamlWorkflow` can run it on entry and tests can
 * exercise it directly; the interpreter wraps a returned reason in a nonRetryable
 * `TypefluxPlanUnsupported` ApplicationFailure (retrying cannot help — only a worker
 * deploy can).
 */
export function planUnsupportedReason(plan: unknown): string | undefined {
  if (plan === null || typeof plan !== "object" || Array.isArray(plan)) {
    return "the plan is not an object";
  }
  const unknownPlanKeys = unknownKeysOf(plan, KNOWN_PLAN_KEYS);
  if (unknownPlanKeys.length > 0) {
    return `the plan carries key(s) this worker does not implement: ${unknownPlanKeys.join(", ")}`;
  }
  const stepsReason = stepsUnsupportedReason((plan as { steps?: unknown }).steps);
  if (stepsReason !== undefined) {
    return stepsReason;
  }
  const lifecycle = (plan as { lifecycle?: unknown }).lifecycle;
  if (lifecycle !== undefined) {
    if (lifecycle === null || typeof lifecycle !== "object" || Array.isArray(lifecycle)) {
      return "the plan lifecycle is not an object";
    }
    const unknownLifecycleKeys = unknownKeysOf(lifecycle, KNOWN_LIFECYCLE_KEYS);
    if (unknownLifecycleKeys.length > 0) {
      return `the plan lifecycle carries key(s) this worker does not implement: ${unknownLifecycleKeys.join(", ")}`;
    }
    const review = (lifecycle as { review?: unknown }).review;
    if (review !== undefined) {
      const reason = reviewNodeUnsupportedReason(review, "the plan review gate", KNOWN_REVIEW_KEYS);
      if (reason !== undefined) {
        return reason;
      }
    }
    // Multiple named gates (#55 slice 4): the plan carries a `gates` array of review-shaped
    // nodes each with an `id`. An old worker that predates this reaches the empty-steps/unknown-key
    // guards above for the STEP surface; the gate surface gets its own strict rejection so a
    // new-shape gate never falls through to single-gate handling on a worker that lacks it.
    const gates = (lifecycle as { gates?: unknown }).gates;
    if (gates !== undefined) {
      if (!Array.isArray(gates) || gates.length === 0) {
        return "the plan lifecycle gates is not a non-empty array";
      }
      for (const gate of gates) {
        if (gate !== null && typeof gate === "object" && !Array.isArray(gate) && typeof (gate as { id?: unknown }).id !== "string") {
          return "a plan lifecycle gate is missing its string id";
        }
        const reason = reviewNodeUnsupportedReason(gate, "a plan lifecycle gate", KNOWN_GATE_KEYS);
        if (reason !== undefined) {
          return reason;
        }
      }
    }
  }
  return undefined;
}

/**
 * The shared review/gate node shape check (#55 slice 4): unknown keys are new semantics, and the
 * REQUIRED fields must be present with their interpreted types — a gate whose `afterStep` is
 * missing would silently never fire, and a missing `userDecisions` would TypeError mid-signal
 * into an undiagnostic retry hang; both must instead fail loud as TypefluxPlanUnsupported.
 */
function reviewNodeUnsupportedReason(
  node: unknown,
  label: string,
  knownKeys: ReadonlySet<string>,
): string | undefined {
  if (node === null || typeof node !== "object" || Array.isArray(node)) {
    return `${label} is not an object`;
  }
  const unknownKeys = unknownKeysOf(node, knownKeys);
  if (unknownKeys.length > 0) {
    return `${label} carries key(s) this worker does not implement: ${unknownKeys.join(", ")}`;
  }
  const candidate = node as { afterStep?: unknown; userDecisions?: unknown; invalidUserDecision?: unknown };
  if (typeof candidate.afterStep !== "string") {
    return `${label} is missing its string afterStep`;
  }
  if (
    candidate.userDecisions === null ||
    typeof candidate.userDecisions !== "object" ||
    Array.isArray(candidate.userDecisions)
  ) {
    return `${label} is missing its userDecisions object`;
  }
  if (candidate.invalidUserDecision !== "warn" && candidate.invalidUserDecision !== "fail") {
    return `${label} has an invalidUserDecision this worker does not implement: ${JSON.stringify(candidate.invalidUserDecision)}`;
  }
  return undefined;
}

/**
 * Resolve a dot path (`input.evidence`, `<stepId>.field`) against the workflow
 * context — `{ input, [stepId]: result, … }` — mirroring Python `_resolve_context_path`.
 * Throws if an intermediate value is not a traversable object.
 */
export function resolveContextPath(context: Record<string, unknown>, path: string): unknown {
  let current: unknown = context;
  for (const part of path.split(".")) {
    // `Object.hasOwn` (not bare bracket access) so a missing key throws a clear error
    // and an inherited member (`toString`, `constructor`, `__proto__`) never resolves —
    // only own data is addressable (parity with Python's `Mapping[]`/`KeyError`).
    if (current === null || typeof current !== "object" || !Object.hasOwn(current, part)) {
      throw new Error(`cannot resolve workflow path ${JSON.stringify(path)} at ${JSON.stringify(part)}`);
    }
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}
