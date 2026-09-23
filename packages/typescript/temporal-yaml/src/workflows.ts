/**
 * The generic YAML workflow interpreter (parity Epic 5, #452; Python
 * `yaml/workflow.py` `create_workflow` `run` loop). Adapted to Temporal TS's
 * static-bundle model: instead of a dynamically-built workflow class, this is ONE
 * generic workflow that takes the {@link WorkflowPlan} as an argument and executes it.
 *
 * With `plan.lifecycle` set (#482) it also serves the lifecycle surface — the
 * `typeflux_lifecycle_status` query, the `typeflux_request_cancel` /
 * `typeflux_submit_review` signals, progress units, and the bounded status-event
 * history. Handlers are ALWAYS registered (a disabled lifecycle answers
 * `state: "disabled"` and ignores signals — Python parity: signals stay callable,
 * they just have no effect).
 *
 * This module is the worker's `workflowsPath` entry — it runs in the Temporal
 * sandbox, so it imports only sandbox-safe code: `@temporalio/workflow` and the
 * core's pure `@typeflux/temporal/composition` subpath (NOT the barrel, which
 * pulls node:crypto). It is NOT re-exported from the package index.
 */

import {
  defineSearchAttributeKey,
  SearchAttributeType,
  WorkflowIdReusePolicy,
  type SearchAttributePair,
} from "@temporalio/common";
import {
  ApplicationFailure,
  CancellationScope,
  condition,
  defineQuery,
  defineSignal,
  executeChild,
  isCancellation,
  ParentClosePolicy,
  proxyActivities,
  setHandler,
  workflowInfo,
} from "@temporalio/workflow";

import { fanOut } from "@typeflux/temporal/composition";

import { disabledLifecycleStatus, LifecycleRuntime, type GateState, type WorkflowLifecycleStatus } from "./lifecycle.js";
import { SUBJECT_IDS_SEARCH_ATTRIBUTE } from "./subjects.js";
import {
  activityProxyOptions,
  CACHE_PREP_ACTIVITY_SUFFIX,
  CACHE_RELEASE_ACTIVITY_SUFFIX,
  cacheActivityProxyOptions,
  DEFAULT_MAP_CONCURRENCY,
  evaluatePlanWhen,
  planUnsupportedReason,
  renderPlanWhen,
  resolveContextPath,
  utf8ByteLength,
  type ActivityPlanStep,
  type ActivityProxyOptions,
  type ActivityRetryProxyOptions,
  childIdentityMemo,
  type CompensatePlan,
  type CompensationStatus,
  type MapPlanStep,
  type ParallelPlanStep,
  type RetryPolicyPlan,
  type SubworkflowChildIdentity,
  type SubworkflowMapPlanStep,
  type SubworkflowPlanStep,
  type WorkflowPlan,
  type WorkflowPlanStep,
} from "./workflow-plan.js";

/** One recorded compensation to run on unwind (#299 D299-2): the LIFO entry. */
interface CompensationEntry {
  /** The ORIGINAL compensated step id — carried on every compensation lifecycle event. */
  stepId: string;
  /** The compensating activity name (an ordinary declared activity). */
  activity: string;
  /** The compensation input, resolved at PUSH time (replay-stable — the value exists then). */
  input: unknown;
  /** Per-compensation retry override (ms form); absent = the default bounded retry. */
  retry?: RetryPolicyPlan;
}


/**
 * The generic workflow type registered by the worker (`runtime.ts` `YAML_WORKFLOW_TYPE`),
 * duplicated here as a literal because this module runs in the Temporal sandbox and cannot
 * import `runtime.ts` (it pulls node:crypto). `executeChild` starts children under the SAME
 * generic type — a child is just another plan-as-argument execution (#55 §6).
 */
const YAML_CHILD_WORKFLOW_TYPE = "typefluxYamlWorkflow";

/** The lifecycle surface — exact Python names (cross-SDK signal/query contract, #482). */
export const lifecycleStatusQuery = defineQuery<WorkflowLifecycleStatus>("typeflux_lifecycle_status");
export const requestCancelSignal = defineSignal<[string | null | undefined]>("typeflux_request_cancel");
export const submitReviewSignal = defineSignal<[unknown]>("typeflux_submit_review");

// The optional second argument is the map fan-out's cached-session handle (#478) — a plain
// wire object the worker boundary schema-parses. The HANDLE is Python-wire-compatible;
// the envelope is a logged fork: Python wraps it in a MapActivityContext
// (map_step_id/index/size/concurrency + cached_session + subject_ids), TS sends the bare
// handle — a cross-SDK task-queue mix fails loudly on either side's strict parse. The
// optional THIRD argument is the run's subject id(s) (#715 slice 1), passed only when the
// run has subjects so subject-free histories stay byte-identical.
type ActivityFn = (input: unknown, cachedSession?: unknown, subjectIds?: readonly string[]) => Promise<unknown>;

/** The wire fields of a cached-session handle the INTERPRETER reads (a plan-safe subset). */
interface CachedSessionWire {
  cache_id?: string | null;
}

/**
 * Resolve each step's activity to a proxy honoring its plan timeout + retry options (parity with
 * Python applying each definition's `start_to_close_timeout` / `heartbeat_timeout` / retry policy).
 * The pure `activityProxyOptions` computes the options (default 2-minute start-to-close; heartbeat
 * only when configured; a bounded retry policy resolving the definition's own > the workflow-wide
 * `runtime.activity_retry` > the default, replacing Temporal's unlimited default); proxies are
 * cached per name so each is built once per workflow run (deterministic across replay).
 */
function makeActivityResolver(plan: WorkflowPlan): (name: string) => ActivityFn | undefined {
  const cache = new Map<string, ActivityFn>();
  return (name: string): ActivityFn | undefined => {
    const cached = cache.get(name);
    if (cached !== undefined) {
      return cached;
    }
    const proxy = proxyActivities<Record<string, ActivityFn>>(activityProxyOptions(plan, name));
    // `proxy[name]` is `ActivityFn | undefined` only under `noUncheckedIndexedAccess`; the proxy
    // yields a callable for any name at runtime. The caller's `=== undefined` check stays as the
    // belt-and-suspenders guard, so propagate the type rather than asserting.
    const fn = proxy[name];
    if (fn !== undefined) {
      cache.set(name, fn);
    }
    return fn;
  };
}

/**
 * Resolve the cache-prep/release activities of a map step (#478): the map activity's
 * options WITHOUT its heartbeat (`cacheActivityProxyOptions`) — prep/release emit no
 * heartbeats, so an inherited heartbeat timeout would have Temporal kill them, a
 * failure the in-activity fail-soft cannot catch (Python `_cache_activity_kwargs`, #368).
 */
function makeCacheActivityResolver(
  plan: WorkflowPlan,
): (mapActivity: string, cacheActivity: string) => ActivityFn | undefined {
  const cache = new Map<string, ActivityFn>();
  return (mapActivity: string, cacheActivity: string): ActivityFn | undefined => {
    const cached = cache.get(cacheActivity);
    if (cached !== undefined) {
      return cached;
    }
    const proxy = proxyActivities<Record<string, ActivityFn>>(cacheActivityProxyOptions(plan, mapActivity));
    const fn = proxy[cacheActivity];
    if (fn !== undefined) {
      cache.set(cacheActivity, fn);
    }
    return fn;
  };
}

/**
 * The collect payload guard shared by map and parallel steps (#495/#55): an actionable
 * failure BEFORE Temporal's opaque ~2MB per-payload limit. Computed from workflow data,
 * so the check replays deterministically; a limit <= 0 (or absent) means no limit. The
 * failure `type` stays per-step-kind (cross-edition wire contract).
 */
function enforceCollectPayloadLimit(
  kind: "map" | "parallel",
  stepId: string,
  collected: unknown,
  limit: number | undefined,
  reduceHint: string,
): void {
  if (limit === undefined || limit <= 0) {
    return;
  }
  const size = utf8ByteLength(JSON.stringify(collected));
  if (size > limit) {
    throw ApplicationFailure.create({
      message:
        `${kind} step ${JSON.stringify(stepId)} collected payload is ${size} bytes, exceeding ` +
        `collect.max_bytes=${limit}; ${reduceHint} or raise collect.max_bytes`,
      type: kind === "map" ? "TypefluxMapCollectPayloadTooLarge" : "TypefluxParallelCollectPayloadTooLarge",
      nonRetryable: true,
    });
  }
}

/**
 * Convert a plan {@link RetryPolicyPlan} (ms) to Temporal proxy retry options, dropping
 * `maximumAttempts` for the `0` unlimited sentinel exactly as {@link activityProxyOptions}
 * does (Temporal TS rejects `maximumAttempts <= 0` and treats an absent field as unlimited).
 * Used to apply a per-compensation `retry:` override (#299).
 */
function retryProxyOptionsFrom(retry: RetryPolicyPlan): ActivityRetryProxyOptions {
  const options: ActivityRetryProxyOptions = {
    initialInterval: retry.initialIntervalMs,
    backoffCoefficient: retry.backoffCoefficient,
  };
  if (retry.maximumAttempts > 0) {
    options.maximumAttempts = retry.maximumAttempts;
  }
  if (retry.maximumIntervalMs !== undefined) {
    options.maximumInterval = retry.maximumIntervalMs;
  }
  return options;
}

/**
 * Raise the graceful cancellation (Python `_raise_if_cancelled`). The terminal
 * `workflow_cancelled` event is NO LONGER recorded here (#299 D299-2a): cancellation ALSO
 * unwinds the compensation LIFO, and the terminal event must carry the resulting
 * `compensation_status`, so the single outer catch records it AFTER the unwind runs. The
 * `lifecycle` parameter is retained for call-site symmetry with the pre-#299 signature.
 */
function raiseCancelled(_lifecycle: LifecycleRuntime): never {
  throw ApplicationFailure.create({
    message: "workflow cancellation requested",
    type: "TypefluxWorkflowCancelled",
    nonRetryable: true,
  });
}

/**
 * The review gate (Python `_maybe_wait_for_review`): after a gate's `afterStep` completes, wait for
 * a `typeflux_submit_review` decision for THAT gate on a durable `condition`, with an ABSOLUTE
 * deadline computed once (signals never extend it). Multiple gates (#55 slice 4) sit after distinct
 * top-level steps (DS4-1), so each fires when its checkpoint is reached; a V1 `review` normalizes to
 * one gate named `"review"`. Returns the route target to jump to, or undefined to continue (including
 * the timeout `cancel` action, whose flag the caller's next check raises on).
 */
async function maybeWaitForReview(lifecycle: LifecycleRuntime | undefined, stepId: string): Promise<string | undefined> {
  const gate = lifecycle?.pendingGateAfter(stepId);
  if (lifecycle === undefined || gate === undefined) {
    return undefined;
  }
  lifecycle.waitingForGate(gate);
  const deadline = gate.timeoutSeconds !== undefined ? Date.now() + gate.timeoutSeconds * 1000 : undefined;
  let observed = lifecycle.reviewSignalSequence;
  for (;;) {
    if (lifecycle.cancellationRequested) {
      raiseCancelled(lifecycle);
    }
    // THIS gate's fail flag — per-gate, so an invalid decision on another (already-closed)
    // fail-policy gate can never fail this gate's wait (#55 slice 4 review round).
    if (gate.invalidFailed) {
      lifecycle.invalidReviewFailedTerminal();
      throw ApplicationFailure.create({
        message: `workflow review received invalid user_decision at ${stepId}`,
        type: "TypefluxInvalidReviewDecision",
        nonRetryable: true,
      });
    }
    // The gate's OWN route target — not the singleton (which is last-resolved across gates).
    if (gate.routeTarget !== null) {
      return routeOrThrow(lifecycle, gate);
    }
    let timedOut = false;
    if (deadline !== undefined) {
      const remaining = deadline - Date.now();
      timedOut =
        remaining <= 0 ||
        !(await condition(
          () => lifecycle.reviewSignalSequence > observed || lifecycle.cancellationRequested,
          remaining,
        ));
    } else {
      await condition(() => lifecycle.reviewSignalSequence > observed || lifecycle.cancellationRequested);
    }
    if (timedOut) {
      const outcome = lifecycle.gateTimedOut(gate);
      if (outcome.action === "route") {
        if (outcome.route === undefined) {
          throw ApplicationFailure.create({
            message: "workflow review timeout route missing",
            type: "TypefluxReviewRouteMissing",
            nonRetryable: true,
          });
        }
        return outcome.route;
      }
      if (outcome.action === "cancel") {
        // Python raises INSIDE _apply_review_timeout (right after review_timed_out) — raising
        // here rather than deferring to the caller's next check keeps a gate on the LAST step
        // from completing successfully.
        raiseCancelled(lifecycle);
      }
      throw ApplicationFailure.create({
        message: `workflow review timed out at ${stepId}`,
        type: "TypefluxReviewTimeout",
        nonRetryable: true,
      });
    }
    observed = lifecycle.reviewSignalSequence;
  }
}

/** Resolve a gate via `review_routed`, raising the defensive missing-route failure. */
function routeOrThrow(lifecycle: LifecycleRuntime, gate: GateState): string {
  const target = lifecycle.gateRouted(gate);
  if (target === undefined) {
    throw ApplicationFailure.create({
      message: "workflow review route missing",
      type: "TypefluxReviewRouteMissing",
      nonRetryable: true,
    });
  }
  return target;
}

/**
 * Python `_lifecycle_total_units` over a step slice, recursive: 1 per activity,
 * resolvable map lengths, and — per #55 §8 — a parallel block's total is the SUM of its
 * branches' (applied recursively), so branch subsequences count exactly like inline steps.
 */
function totalUnitsOf(steps: readonly WorkflowPlanStep[], context: Record<string, unknown>): number {
  let total = 0;
  for (const step of steps) {
    if (step.kind === "activity" || step.kind === "subworkflow") {
      // A sub-workflow node counts ONE parent unit (#55 §8): interior progress
      // belongs to the child's own lifecycle.
      total += 1;
    } else if (step.kind === "parallel") {
      for (const branch of step.branches) {
        total += totalUnitsOf(branch.steps, context);
      }
    } else {
      try {
        const items = resolveContextPath(context, step.over);
        if (Array.isArray(items)) {
          total += items.length;
        }
      } catch {
        // Unresolvable at this point -> contributes nothing (Python's _try_resolve_context_path).
      }
    }
  }
  return total;
}

/**
 * Fan an activity over map items while racing the lifecycle cancel flag: when
 * `typeflux_request_cancel` lands mid-map, the in-flight activities are CANCELLED (a
 * `CancellationScope`, the TS analogue of Python cancelling the activity handles) instead of
 * running the fan-out to completion.
 */
async function fanOutCancellable(
  lifecycle: LifecycleRuntime | undefined,
  items: unknown[],
  run: (item: unknown) => Promise<unknown>,
  concurrency: number,
): Promise<unknown[]> {
  if (lifecycle === undefined || !lifecycle.plan.cancellation) {
    return fanOut(items, run, { concurrency });
  }
  const scope = new CancellationScope();
  const work = scope.run(() => fanOut(items, run, { concurrency }));
  // Suppress the cancellation-path rejection until it is awaited on the cancel branch — a raced
  // promise must not surface as an unhandled rejection while the cancel branch wins.
  work.catch(() => undefined);
  const winner = await Promise.race([
    work.then(() => "done" as const),
    condition(() => lifecycle.cancellationRequested).then(() => "cancelled" as const),
  ]);
  if (winner === "cancelled") {
    scope.cancel();
    try {
      await work;
    } catch {
      // The scope cancellation surfaces here; the typed cancellation below is the real signal.
    }
    raiseCancelled(lifecycle);
  }
  return work;
}

/**
 * Non-identity start context (#55 §6): configuration the interpreter needs to start
 * CHILD workflows but that must NOT ride the plan — anything in the plan folds into
 * the frozen `workflow.version` digest, and the visibility attribute is deployment
 * config, not graph identity. Passed as the OPTIONAL third workflow argument
 * (binding contract `ts-plan-argument.start_args`); absent for spec shapes with no
 * new needs, so pre-existing starts are byte-identical.
 */
export interface YamlWorkflowStartContext {
  /**
   * The configured `runtime.temporal.workflow_search_attribute` name. Children stamp
   * their OWN logical name into it (they are their own workflow) — which keeps wide
   * child fan-outs OUT of the parent's frozen-version scan (#55 §6, the
   * scan-degradation mitigation). Propagated to grandchildren verbatim.
   */
  searchAttribute?: string;
}

const KNOWN_START_CONTEXT_KEYS = new Set(["searchAttribute"]);

/**
 * Execute a YAML workflow plan: run each step in order, threading the result
 * forward and recording it in the context by step id. An `activity` step calls its
 * activity on the current value; a `map` step fans the activity over the items at
 * `over` (bounded by `concurrency`) and, if `collectField` is set, wraps the results
 * into `{ [collectField]: results }` (parity with the spec's `collect`); a `parallel`
 * step (#55) runs its gated-in branches concurrently and merges their terminal values
 * into a collect object keyed by branch id; a `subworkflow` step runs a sibling
 * project workflow as a Temporal CHILD workflow, and `subworkflowMap` fans a child
 * per item (#55 §3.4). `when:` gates skip a step and the remainder of its enclosing
 * sequence (top level: early exit) — execution is a recursive
 * run-sequence/run-parallel walk, not a scheduler; the V1 sequential loop is the
 * degenerate case.
 */
export async function typefluxYamlWorkflow(
  plan: WorkflowPlan,
  input: unknown,
  startContext?: YamlWorkflowStartContext,
): Promise<unknown> {
  // The plan-shape guard (#55 §5.2), BEFORE any other work: the plan is a client-supplied
  // argument, and a shape this worker does not implement must fail loud and nonRetryable —
  // an unknown step kind would otherwise TypeError into an indefinite workflow-task retry
  // hang, and an empty `steps` would no-op-complete "successfully". Version-skew safety
  // depends on this: workers carrying the guard deploy BEFORE any control plane or starter
  // dispatches new-shape plans to their task queue.
  const unsupportedReason = planUnsupportedReason(plan);
  if (unsupportedReason !== undefined) {
    throw ApplicationFailure.create({
      message:
        `unsupported workflow plan: ${unsupportedReason}; upgrade and deploy ` +
        "@typeflux/temporal-yaml workers before dispatching plans that use new plan features (#55)",
      type: "TypefluxPlanUnsupported",
      nonRetryable: true,
    });
  }
  // The start context is a client-supplied argument too (#55 §6): a key this worker does
  // not implement means the starter expects behavior this worker cannot provide (e.g. a new
  // deployment knob), so reject loud rather than silently ignoring it.
  if (startContext !== undefined) {
    const unknownKey = Object.keys(startContext).find((key) => !KNOWN_START_CONTEXT_KEYS.has(key));
    if (unknownKey !== undefined) {
      throw ApplicationFailure.create({
        message:
          `unsupported workflow start context key ${JSON.stringify(unknownKey)}; upgrade and deploy ` +
          "@typeflux/temporal-yaml workers before dispatching starts that use new start-context features (#55)",
        type: "TypefluxPlanUnsupported",
        nonRetryable: true,
      });
    }
  }
  const context: Record<string, unknown> = { input };
  let current: unknown = input;
  const activityFor = makeActivityResolver(plan);
  const cacheActivityFor = makeCacheActivityResolver(plan);

  // The workflow-local compensation LIFO (#299 D299-2): each `compensate:`-bearing step pushes
  // on completion, with its input resolved AT PUSH TIME (the context value exists then and is
  // replay-stable). On failure OR cancellation the outer catch walks this in reverse inside a
  // non-cancellable scope. One flat stack per run — completed sibling branches of a parallel
  // block and per-item map compensations all live here (#299 D299-3).
  const compensationStack: CompensationEntry[] = [];

  /** Append one compensation entry to the LIFO (#299). */
  const pushCompensationEntry = (stepId: string, compensate: CompensatePlan, input: unknown): void => {
    compensationStack.push({
      stepId,
      activity: compensate.activity,
      input,
      ...(compensate.retry !== undefined ? { retry: compensate.retry } : {}),
    });
  };

  /**
   * Record an ATOMIC step's compensation on the LIFO (#299), called AFTER `context[step.id]` is
   * set (so `input_from` — including the step's own output by id — resolves). Activity /
   * sub-workflow steps only: a `map` step self-pushes PER COMPLETED ITEM from inside its executor
   * (so a mid-fan-out failure still compensates the succeeded items — {@link executeMapStep}),
   * and a `parallel` step never carries compensation (its branch steps push as they complete).
   */
  const pushCompensation = (step: WorkflowPlanStep, value: unknown): void => {
    if (step.kind === "map") {
      return; // map compensations are pushed per-item inside executeMapStep
    }
    const compensate = (step as { compensate?: CompensatePlan }).compensate;
    if (compensate === undefined) {
      return;
    }
    pushCompensationEntry(
      step.id,
      compensate,
      compensate.inputFrom !== undefined ? resolveContextPath(context, compensate.inputFrom) : value,
    );
  };

  /**
   * Walk the compensation LIFO in reverse inside a NON-CANCELLABLE scope (#299 D299-2): each
   * entry's compensating activity runs with a bounded per-compensation timeout (its own proxy
   * options) and the per-compensation retry override when set. A compensation failure records
   * `compensation_failed` and the unwind CONTINUES (best-effort, loud); the caller still rethrows
   * the original error. Returns the terminal {@link CompensationStatus}. The non-cancellable scope
   * answers the #368 "scheduling during cancellation is unreliable" concern — compensation is the
   * point of the feature, not optional cleanup.
   */
  const runCompensations = async (): Promise<CompensationStatus> => {
    if (compensationStack.length === 0) {
      return "none";
    }
    let anyFailed = false;
    await CancellationScope.nonCancellable(async () => {
      while (compensationStack.length > 0) {
        const entry = compensationStack.pop() as CompensationEntry;
        lifecycle?.compensationStarted(entry.stepId);
        try {
          const options: ActivityProxyOptions = activityProxyOptions(plan, entry.activity);
          if (entry.retry !== undefined) {
            options.retry = retryProxyOptionsFrom(entry.retry);
          }
          const proxy = proxyActivities<Record<string, ActivityFn>>(options);
          await proxy[entry.activity]!(entry.input);
          lifecycle?.compensationCompleted(entry.stepId);
        } catch {
          // Best-effort per the saga precedent, but LOUD: record and keep unwinding so one broken
          // compensation never strands the rest. The original failure is rethrown by the caller.
          anyFailed = true;
          lifecycle?.compensationFailed(entry.stepId);
        }
      }
    });
    return anyFailed ? "partial" : "complete";
  };

  // `new Date()` is deterministic inside the Temporal sandbox (replay-safe workflow time).
  const lifecycle =
    plan.lifecycle !== undefined ? new LifecycleRuntime(plan.lifecycle, () => new Date().toISOString()) : undefined;
  setHandler(lifecycleStatusQuery, () => lifecycle?.status() ?? disabledLifecycleStatus());
  setHandler(requestCancelSignal, (reason) => lifecycle?.requestCancel(reason));
  setHandler(submitReviewSignal, (command) => lifecycle?.submitReview(command));

  if (lifecycle !== undefined) {
    // Python's up-front total: one unit per activity step PLUS the item count of every map whose
    // `over` resolves from the INPUT root (known before any step runs); maps over earlier step
    // outputs join the total when they execute (_lifecycle_total_units parity).
    lifecycle.addUnits(totalUnitsOf(plan.steps, context));
    lifecycle.started();
  }

  // The run's subject ids (#715 slice 1), read LAZILY off this execution's own
  // `TypefluxSubjectIds` search attribute (stamped at start / inherited from the
  // parent) and cached — lazy so a plan driven outside a workflow execution
  // (unit harnesses) never touches the workflow context; deterministic inside one.
  let runSubjectIdsCache: string[] | undefined;
  const runSubjectIds = (): string[] => {
    if (runSubjectIdsCache === undefined) {
      try {
        runSubjectIdsCache = parentSubjectIds();
      } catch {
        runSubjectIdsCache = [];
      }
    }
    return runSubjectIdsCache;
  };

  /** Run one activity step over the running value. */
  const executeActivityStep = async (step: ActivityPlanStep, value: unknown): Promise<unknown> => {
    // `proxyActivities` returns a Proxy that yields a callable for ANY name, so this
    // never throws at runtime — it satisfies `noUncheckedIndexedAccess`. An unknown
    // activity name surfaces as a Temporal "no registered activity" error; validating
    // step activities against the registered set is the worker-build job (buildRuntime, PR4).
    const activity = activityFor(step.activity);
    if (activity === undefined) {
      throw new Error(`workflow step ${JSON.stringify(step.id)} references unknown activity ${JSON.stringify(step.activity)}`);
    }
    // Pass the subject envelope ONLY when the run has subjects (#715 slice 1), so
    // subject-free histories are byte-identical to pre-#715 starts. Code-defined
    // activities ignore the extra arguments (plain JS call semantics).
    const subjects = runSubjectIds();
    const result = subjects.length > 0 ? await activity(value, undefined, subjects) : await activity(value);
    lifecycle?.unitCompleted();
    return result;
  };

  /** Fan the map activity over its items (bounded), collect, and guard the payload. */
  const executeMapStep = async (step: MapPlanStep): Promise<unknown> => {
    const activity = activityFor(step.activity);
    if (activity === undefined) {
      throw new Error(`workflow step ${JSON.stringify(step.id)} references unknown activity ${JSON.stringify(step.activity)}`);
    }
    const items = resolveContextPath(context, step.over);
    if (!Array.isArray(items)) {
      throw new Error(
        `map step ${JSON.stringify(step.id)} path ${JSON.stringify(step.over)} did not resolve to an array`,
      );
    }
    if (step.over.split(".")[0] !== "input") {
      // Input-rooted map items were counted up front; only step-output maps add here.
      lifecycle?.addUnits(items.length);
    }
    // Prepare a provider-side cached session once for the whole fan-out (#478).
    // Skipped for empty maps (no per-item calls to accelerate); the prep activity
    // fails SOFT to a supported:false handle when caching is unavailable, so every
    // item still runs — only cost/latency differ.
    let cachedSession: unknown;
    if (step.sessionCache?.enabled === true && items.length > 0) {
      const prep = cacheActivityFor(step.activity, `${step.activity}${CACHE_PREP_ACTIVITY_SUFFIX}`);
      if (prep === undefined) {
        throw new Error(`map step ${JSON.stringify(step.id)} could not resolve its cache-prep activity`);
      }
      // A representative item: prep resolves the activity's cache:"reference"
      // artifacts from it (they must be identical across items).
      cachedSession = await prep(items[0]);
    }
    // Per-item completions recorded AS THEY SETTLE (#299), so a mid-fan-out failure still
    // compensates every already-succeeded item. `fanOut` settles all in-flight siblings before
    // it rejects, so no item pushes after the unwind begins. The `finally` below pushes the
    // completed items' compensations in ITEM order (→ reverse item order on unwind).
    const itemOutputs = new Array<unknown>(items.length);
    const completedIndices: number[] = [];
    const indexed = items.map((item, index) => ({ item, index }));
    try {
      const results = await fanOutCancellable(
        lifecycle,
        indexed,
        async (entry) => {
          const { item, index } = entry as { item: unknown; index: number };
          const subjects = runSubjectIds();
          const result =
            subjects.length > 0
              ? await activity(item, cachedSession, subjects)
              : await activity(item, cachedSession);
          itemOutputs[index] = result;
          completedIndices.push(index);
          lifecycle?.unitCompleted();
          return result;
        },
        step.concurrency ?? DEFAULT_MAP_CONCURRENCY,
      );
      // Best-effort release of a reference-style cache (one with a server-side object,
      // i.e. a cache_id) now that the fan-out is done, so it doesn't linger until TTL.
      // SUCCESS PATH ONLY: scheduling an activity during workflow cancellation/failure
      // isn't reliable, and the cache TTL-expires regardless. The release activity
      // already swallows provider errors; suppress activity-level failures too, so
      // cleanup never fails the workflow (#368).
      const cacheId = (cachedSession as CachedSessionWire | undefined)?.cache_id;
      if (cacheId !== undefined && cacheId !== null) {
        const release = cacheActivityFor(step.activity, `${step.activity}${CACHE_RELEASE_ACTIVITY_SUFFIX}`);
        if (release !== undefined) {
          try {
            await release(cachedSession);
          } catch (error) {
            // Suppressed: best-effort cleanup (see above) — EXCEPT cancellation,
            // which must keep propagating (Python's suppress(Exception) lets
            // CancelledError, a BaseException, escape the same way).
            if (isCancellation(error)) {
              throw error;
            }
          }
        }
      }
      const collected = step.collectField !== undefined ? { [step.collectField]: results } : results;
      enforceCollectPayloadLimit("map", step.id, collected, step.collectMaxBytes, "reduce fan-out, shrink item outputs,");
      return collected;
    } finally {
      const compensate = step.compensate;
      if (compensate !== undefined && completedIndices.length > 0) {
        for (const index of [...completedIndices].sort((a, b) => a - b)) {
          pushCompensationEntry(
            step.id,
            compensate,
            compensate.inputFrom !== undefined ? resolveContextPath(context, compensate.inputFrom) : itemOutputs[index],
          );
        }
      }
    }
  };

  /**
   * Run one branch's step sequence (#55 §5.1): steps in order, threading the branch's
   * running value and recording every result into the SHARED flat context (unique ids
   * make concurrent sibling writes collision-free). A false `when` gate skips the step
   * and the REMAINDER of the sequence — the branch's contribution is the running value
   * at the gate (§3.3 sequence gating). No review gates here: gates sit between
   * top-level steps only.
   */
  const runBranchSequence = async (steps: readonly WorkflowPlanStep[], entry: unknown): Promise<unknown> => {
    let value = entry;
    for (const [index, step] of steps.entries()) {
      if (lifecycle?.cancellationRequested === true) {
        raiseCancelled(lifecycle);
      }
      if (step.when !== undefined && !evaluatePlanWhen(step.when, context)) {
        lifecycle?.stepSkipped(step.id, renderPlanWhen(step.when));
        lifecycle?.skipUnits(totalUnitsOf(steps.slice(index), context));
        return value;
      }
      lifecycle?.stepStarted(step.id);
      value = await executeStep(step, value);
      context[step.id] = value;
      // A completed step inside a parallel branch pushes onto the SAME flat LIFO (#299 D299-3):
      // a later failure unwinds every completed sibling branch's compensations too.
      pushCompensation(step, value);
    }
    return value;
  };

  /**
   * Execute a parallel block (#55 §5.1): evaluate branch gates in declared order
   * against recorded context, run the gated-in branches concurrently under the map
   * runner's structured-concurrency discipline (`fanOutCancellable` — a failing branch
   * cancels in-flight siblings and propagates), then assemble the collect object keyed
   * by branch id in DECLARED order (a gated-out branch's field is null) and enforce
   * `collect.max_bytes` exactly like a map's.
   */
  const executeParallelStep = async (step: ParallelPlanStep, entry: unknown): Promise<unknown> => {
    const gatedIn: ParallelPlanStep["branches"] = [];
    const resultsById = new Map<string, unknown>();
    for (const branch of step.branches) {
      if (branch.when !== undefined && !evaluatePlanWhen(branch.when, context)) {
        // Whole-branch gating: the collect field is null (its schema field must be
        // Optional — Python-enforced at load; TS's strict activity boundary catches it).
        lifecycle?.stepSkipped(branch.id, renderPlanWhen(branch.when));
        lifecycle?.skipUnits(totalUnitsOf(branch.steps, context));
        resultsById.set(branch.id, null);
      } else {
        gatedIn.push(branch);
      }
    }
    // All gated-in branches run concurrently (no branch-count concurrency knob in v1:
    // branches are static, few, and heterogeneous). Branch tasks start in declared
    // order; progress is driven solely by recorded completions, so replay is stable.
    const results = await fanOutCancellable(
      lifecycle,
      gatedIn,
      (branch) => runBranchSequence((branch as ParallelPlanStep["branches"][number]).steps, entry),
      Math.max(1, gatedIn.length),
    );
    gatedIn.forEach((branch, index) => resultsById.set(branch.id, results[index]));
    const collected: Record<string, unknown> = {};
    for (const branch of step.branches) {
      collected[branch.id] = resultsById.get(branch.id);
    }
    enforceCollectPayloadLimit("parallel", step.id, collected, step.collectMaxBytes, "shrink branch outputs");
    return collected;
  };

  /**
   * The child's OWN logical name stamped into the configured search attribute (#55 §6): a child
   * is its own workflow, so stamping its name keeps wide child fan-outs OUT of the PARENT's
   * frozen-version scan (which is already narrowed to the parent's name). Absent config ⇒ no
   * attribute, exactly like a top-level start with no `workflow_search_attribute`.
   */
  /** Read the parent execution's subject ids off its own search attributes (#715
   * slice 1) — deterministic in the sandbox (reading own info is replay-safe). */
  const parentSubjectIds = (): string[] => {
    const typed = workflowInfo().typedSearchAttributes;
    const key = defineSearchAttributeKey(SUBJECT_IDS_SEARCH_ATTRIBUTE, SearchAttributeType.KEYWORD_LIST);
    const value = typed?.get(key);
    return Array.isArray(value) ? value.map((item) => String(item)) : [];
  };

  const childSearchAttributes = (identity: SubworkflowChildIdentity): SearchAttributePair[] | undefined => {
    const pairs: SearchAttributePair[] = [];
    const name = startContext?.searchAttribute;
    if (name !== undefined) {
      pairs.push({ key: defineSearchAttributeKey(name, SearchAttributeType.KEYWORD), value: identity.workflowName });
    }
    // #715 slice 1: the child inherits the PARENT's subject ids into its own
    // TypefluxSubjectIds (children of a subject's review are that subject's data,
    // so erasure that enumerates the parent must also reach its children). The
    // parent reads its own subjects off its search attributes — deterministic in
    // the sandbox — unlike the own-name attribute stamped from the plan identity.
    const inheritedSubjects = parentSubjectIds();
    if (inheritedSubjects.length > 0) {
      pairs.push({
        key: defineSearchAttributeKey(SUBJECT_IDS_SEARCH_ATTRIBUTE, SearchAttributeType.KEYWORD_LIST),
        value: inheritedSubjects,
      });
    }
    return pairs.length > 0 ? pairs : undefined;
  };

  /**
   * Start one child workflow (#55 §6): deterministic id `{parent}.{suffix}`, `ALLOW_DUPLICATE`
   * reuse (a re-run parent re-starts children under the same ids once prior runs closed; a
   * still-running duplicate fails the start loud), `TERMINATE` parent-close (children are
   * structural — a crashed parent must not leak them; graceful cancel handles the map fan-out
   * via its scope), inherited task queue, child identity memo + own-name search attribute. The
   * embedded child plan is the argument; the start context propagates to grandchildren verbatim.
   * `workflowInfo()` is read HERE (not eagerly), so a plan with no sub-workflow steps never
   * touches the workflow context — the interpreter stays unit-testable outside an execution.
   */
  const startChildWorkflow = (
    identity: SubworkflowChildIdentity,
    idSuffix: string,
    childInput: unknown,
  ): Promise<unknown> => {
    const parentWorkflowId = workflowInfo().workflowId;
    const attributes = childSearchAttributes(identity);
    return executeChild(YAML_CHILD_WORKFLOW_TYPE, {
      workflowId: `${parentWorkflowId}.${idSuffix}`,
      args: startContext !== undefined ? [identity.plan, childInput, startContext] : [identity.plan, childInput],
      workflowIdReusePolicy: WorkflowIdReusePolicy.ALLOW_DUPLICATE,
      parentClosePolicy: ParentClosePolicy.TERMINATE,
      memo: childIdentityMemo(identity, parentWorkflowId),
      ...(attributes !== undefined ? { typedSearchAttributes: attributes } : {}),
    });
  };

  /** Run one sub-workflow step: start the child on the running value, count one parent unit (#55 §8). */
  const executeSubworkflowStep = async (step: SubworkflowPlanStep, value: unknown): Promise<unknown> => {
    const result = await startChildWorkflow(step, step.id, value);
    lifecycle?.unitCompleted();
    return result;
  };

  /**
   * Fan a child per item (#55 §3.4 `map.workflow`): V1 map semantics with a child execution in
   * place of an activity — child ids `{parent}.{step}-{index}`, bounded concurrency, optional
   * collect into `{ [collectField]: results }`, and the map collect-payload guard, exactly.
   */
  const executeSubworkflowMapStep = async (step: SubworkflowMapPlanStep): Promise<unknown> => {
    const items = resolveContextPath(context, step.over);
    if (!Array.isArray(items)) {
      throw new Error(
        `map step ${JSON.stringify(step.id)} path ${JSON.stringify(step.over)} did not resolve to an array`,
      );
    }
    if (step.over.split(".")[0] !== "input") {
      // Input-rooted map items were counted up front; only step-output maps add here.
      lifecycle?.addUnits(items.length);
    }
    // Pair items with their declared index so a child's deterministic id survives the fan-out
    // (the runner yields items, not indexes; `indexOf` would misresolve duplicate items).
    const indexed = items.map((item, index) => ({ item, index }));
    const results = await fanOutCancellable(
      lifecycle,
      indexed,
      async (entry) => {
        const { item, index } = entry as { item: unknown; index: number };
        const result = await startChildWorkflow(step, `${step.id}-${index}`, item);
        lifecycle?.unitCompleted();
        return result;
      },
      step.concurrency ?? DEFAULT_MAP_CONCURRENCY,
    );
    const collected = step.collectField !== undefined ? { [step.collectField]: results } : results;
    enforceCollectPayloadLimit("map", step.id, collected, step.collectMaxBytes, "reduce fan-out, shrink child outputs,");
    return collected;
  };

  /** Execute one step of any kind over the running value (recursive via parallel). */
  const executeStep = async (step: WorkflowPlanStep, value: unknown): Promise<unknown> => {
    if (step.kind === "activity") {
      return executeActivityStep(step, value);
    }
    if (step.kind === "map") {
      return executeMapStep(step);
    }
    if (step.kind === "subworkflow") {
      return executeSubworkflowStep(step, value);
    }
    if (step.kind === "subworkflowMap") {
      return executeSubworkflowMapStep(step);
    }
    return executeParallelStep(step, value);
  };

  const stepIndexes = new Map(plan.steps.map((step, index) => [step.id, index]));
  try {
    let stepIndex = 0;
    while (stepIndex < plan.steps.length) {
      const step = plan.steps[stepIndex] as WorkflowPlanStep;
      if (lifecycle?.cancellationRequested === true) {
        raiseCancelled(lifecycle);
      }

      // Top-level `when` gating is EARLY EXIT (#55 §3.3): a false gate skips this step
      // and the remainder of the sequence, completing the workflow with the running
      // value at the gate (load-validated to match workflow.output).
      if (step.when !== undefined && !evaluatePlanWhen(step.when, context)) {
        lifecycle?.stepSkipped(step.id, renderPlanWhen(step.when));
        lifecycle?.skipUnits(totalUnitsOf(plan.steps.slice(stepIndex), context));
        break;
      }

      lifecycle?.stepStarted(step.id);
      current = await executeStep(step, current);
      context[step.id] = current;
      pushCompensation(step, current);

      const routeTarget = await maybeWaitForReview(lifecycle, step.id);
      if (routeTarget !== undefined) {
        // Load-validated for spec-built plans — but the plan is a client-supplied workflow
        // ARGUMENT, so a hand-built/drifted plan must fail loud (Python's KeyError), not skip
        // every remaining step and complete "successfully".
        const routeIndex = stepIndexes.get(routeTarget);
        if (routeIndex === undefined) {
          throw ApplicationFailure.create({
            message: `workflow review route targets unknown step ${JSON.stringify(routeTarget)}`,
            type: "TypefluxReviewRouteMissing",
            nonRetryable: true,
          });
        }
        lifecycle?.skipUnits(totalUnitsOf(plan.steps.slice(stepIndex + 1, routeIndex), context));
        stepIndex = routeIndex;
        continue;
      }
      stepIndex += 1;
    }
  } catch (error) {
    // Compensation unwind (#299 D299-2): BOTH failure and cancellation walk the LIFO in reverse
    // inside a non-cancellable scope, then the terminal event carries the resulting
    // compensation_status. The ORIGINAL error is always rethrown — a compensation failure never
    // masks it. The terminal `workflow_cancelled` / `workflow_failed` event is recorded HERE (not
    // at the raise point), AFTER the unwind, so it reflects the true compensation outcome.
    const isCancellation = error instanceof ApplicationFailure && error.type === "TypefluxWorkflowCancelled";
    const compensationStatus = await runCompensations();
    if (lifecycle !== undefined) {
      if (isCancellation) {
        lifecycle.cancelled(compensationStatus);
      } else {
        lifecycle.failed(compensationStatus);
      }
    }
    throw error;
  }

  lifecycle?.completed();
  return current;
}
