/**
 * The workflow lifecycle runtime (parity #482; Python `yaml/workflow.py` `_LifecycleRuntime` +
 * `core/contracts.py` status models). Pure and sandbox-safe: no imports beyond the plan types, no
 * clocks of its own (the interpreter injects `now`), so it is unit-testable outside Temporal and
 * deterministic inside it.
 *
 * The status/event shapes are CROSS-SDK WIRE DATA — the `typeflux_lifecycle_status` query response
 * must match Python's Pydantic serialization key-for-key (snake_case, exact event names).
 */

import type { CompensationStatus, GatePlan, LifecyclePlan, ReviewPlan } from "./workflow-plan.js";

/** The `typeflux_submit_review` signal payload (Python `ReviewCommand`). */
export interface ReviewCommand {
  user_decision: string;
  reviewer?: string | null;
  notes?: string | null;
  /**
   * Which gate to decide (#55 slice 4). Absent + exactly one gate waiting ⇒ that gate (V1
   * clients work verbatim); absent with a single-gate workflow ⇒ that gate; absent + several
   * waiting ⇒ recorded `review_invalid_user_decision` (ambiguous, never guessed).
   */
  gate?: string | null;
}

/**
 * One currently-waiting gate reported in the status wire (#55 §8): the execution's OWN view of
 * its open gates and their valid decisions. The CP prefers this over the resolved-spec set
 * (§6 drift caveat) — a running child pins the plan it started with, which an edited spec no
 * longer reflects.
 */
export interface WaitingGate {
  gate_id: string;
  after_step: string;
  valid_user_decisions: Record<string, string>;
}

/** One status event (Python `WorkflowLifecycleEvent`) — every field snapshots record-time state. */
export interface WorkflowLifecycleEvent {
  sequence: number;
  state: string;
  event: string;
  timestamp: string | null;
  step_id: string | null;
  completed_units: number;
  total_units: number;
  cancellation_requested: boolean;
  cancellation_reason: string | null;
  waiting_checkpoint: string | null;
  review_user_decision: string | null;
  review_route_target: string | null;
  terminal_status: string | null;
  /**
   * The rendered `when:` predicate of a `step_skipped` event (#55 §3.3) — present ONLY
   * on that event so every pre-composition event serializes byte-identically to Python's
   * model (whose optional `condition` stays None — and excluded — everywhere else).
   * Declared additively in binding.v1.json `event_fields` and the status wire contract
   * (openapi WorkflowLifecycleEvent); Python emits `step_skipped` itself in #55 slice 2.
   */
  condition?: string;
  /**
   * The gate a review event belongs to (#55 slice 4). Present ONLY on multi-gate (`lifecycle.gates`)
   * review events; omitted for V1 single-`review` specs and every non-review event, so
   * pre-slice-4 event streams serialize byte-identically. Additive in binding.v1.json `event_fields`.
   */
  gate_id?: string;
  /**
   * The compensation outcome (#299 D299-2): `none` / `complete` / `partial`. Present ONLY once the
   * compensation unwind has run — on the terminal `workflow_failed` / `workflow_cancelled` events
   * (and omitted everywhere else, so non-compensating event streams serialize byte-identically to
   * Python's exclude-None model). Additive in binding.v1.json `event_fields`.
   */
  compensation_status?: string;
}

/** The `typeflux_lifecycle_status` query response (Python `WorkflowLifecycleStatus`). */
export interface WorkflowLifecycleStatus {
  state: string;
  current_step: string | null;
  completed_units: number;
  total_units: number;
  cancellation_requested: boolean;
  cancellation_reason: string | null;
  waiting_checkpoint: string | null;
  review_user_decision: string | null;
  review_route_target: string | null;
  terminal_status: string | null;
  event_count: number;
  events_truncated: boolean;
  oldest_event_sequence: number | null;
  latest_event_sequence: number | null;
  events: WorkflowLifecycleEvent[];
  /**
   * The execution's currently-open gates (#55 §8), ordered by open time — ALWAYS present,
   * `[]` when none (the status-field convention, like `events`). Carries per-gate
   * `valid_user_decisions` so the CP can prefer this execution-reported set over the resolved
   * spec (§6). Additive in binding.v1.json `workflow_lifecycle_status.fields`.
   */
  waiting_gates: WaitingGate[];
  /**
   * The terminal compensation outcome (#299 D299-2): `none` / `complete` / `partial`, or null
   * until the compensation LIFO has unwound (i.e. null for every non-failed/non-cancelled run,
   * and for a run with no `compensate:`-bearing completed steps). Additive in binding.v1.json
   * `workflow_lifecycle_status.fields` and the openapi WorkflowLifecycleStatus (client 1.9.0).
   */
  compensation_status: string | null;
}

/** The disabled-lifecycle query response (Python `WorkflowLifecycleStatus(state="disabled")`). */
export function disabledLifecycleStatus(): WorkflowLifecycleStatus {
  return {
    state: "disabled",
    current_step: null,
    completed_units: 0,
    total_units: 0,
    cancellation_requested: false,
    cancellation_reason: null,
    waiting_checkpoint: null,
    review_user_decision: null,
    review_route_target: null,
    terminal_status: null,
    event_count: 0,
    events_truncated: false,
    oldest_event_sequence: null,
    latest_event_sequence: null,
    events: [],
    waiting_gates: [],
    compensation_status: null,
  };
}

/**
 * One gate's mutable runtime state (#55 slice 4). A V1 `lifecycle.review` normalizes to a single
 * gate named `"review"`; `lifecycle.gates` yields one per declared gate. `open` orders concurrent
 * waits (v1 sequences wait one at a time, but the wire semantics generalize).
 */
export interface GateState {
  readonly id: string;
  readonly afterStep: string;
  readonly userDecisions: Record<string, string>;
  readonly invalidUserDecision: "warn" | "fail";
  readonly timeoutSeconds?: number;
  readonly onTimeout?: "fail" | "cancel" | "route";
  readonly timeoutRoute?: string;
  waiting: boolean;
  /**
   * Sequence at which the gate entered `waiting_for_review`; earliest wins `waiting_checkpoint`.
   * NOTE: under DS4-1 (distinct after_step per gate) at most ONE gate waits at a time in a v1
   * sequence, so the plural-open-gate machinery here (openSeq ordering, `waitingGates` sorting,
   * the decision union) is unreachable beyond one element today. It is kept deliberately as the
   * v2 substrate — gates inside parallel branches would wait concurrently — do not simplify away.
   */
  openSeq: number | null;
  /** The staged decision + its route (may be set BEFORE the gate opens — pre-submission). */
  decision: string | null;
  routeTarget: string | null;
  /** True once the gate has been routed/timed-out; a fired gate never re-opens (forward-only). */
  fired: boolean;
  /**
   * Set when THIS gate's `fail` invalid-decision policy trips; its wait loop raises on the next
   * poll. Per-gate — never runtime-wide — so an invalid decision on one gate can never fail a
   * LATER gate's wait (#55 slice 4 review round, item 3).
   */
  invalidFailed: boolean;
}

/** Build the internal gate list from a plan — the single `review` sugar, or the `gates` list. */
function gatesFromPlan(plan: LifecyclePlan): GateState[] {
  const build = (source: ReviewPlan, id: string): GateState => ({
    id,
    afterStep: source.afterStep,
    userDecisions: source.userDecisions,
    invalidUserDecision: source.invalidUserDecision,
    ...(source.timeoutSeconds !== undefined ? { timeoutSeconds: source.timeoutSeconds } : {}),
    ...(source.onTimeout !== undefined ? { onTimeout: source.onTimeout } : {}),
    ...(source.timeoutRoute !== undefined ? { timeoutRoute: source.timeoutRoute } : {}),
    waiting: false,
    openSeq: null,
    decision: null,
    routeTarget: null,
    fired: false,
    invalidFailed: false,
  });
  if (plan.gates !== undefined) {
    return plan.gates.map((gate: GatePlan) => build(gate, gate.id));
  }
  if (plan.review !== undefined) {
    return [build(plan.review, "review")];
  }
  return [];
}

/**
 * Tracks state, progress units, cancellation, and the bounded event history for one workflow run.
 * `now` is injected (`new Date().toISOString()` in the Temporal sandbox is deterministic across
 * replay; tests pass a fixed clock).
 */
export class LifecycleRuntime {
  readonly plan: LifecyclePlan;
  private readonly now: () => string | null;
  private state = "pending";
  private currentStep: string | null = null;
  private completedUnits = 0;
  private totalUnits = 0;
  cancellationRequested = false;
  private cancellationReason: string | null = null;
  private waitingCheckpoint: string | null = null;
  private reviewUserDecision: string | null = null;
  /**
   * The last-resolved gate's route target (#55 §8 singleton). Null until a valid decision or
   * timeout route lands; persists after routing. The interpreter reads the per-GATE
   * {@link GateState.routeTarget} (via {@link pendingGateAfter}) to route — never this singleton.
   */
  reviewRouteTarget: string | null = null;
  /** Bumped by every submit_review signal — the gates' wake predicate compares against it. */
  reviewSignalSequence = 0;
  private terminalStatus: string | null = null;
  /**
   * The terminal compensation outcome (#299 D299-2), set once the unwind has run; null otherwise.
   * Surfaced in status() and, once set, on every subsequently-recorded event (the terminal one).
   */
  private compensationStatus: string | null = null;
  private sequence = 0;
  private eventCount = 0;
  private readonly events: WorkflowLifecycleEvent[] = [];
  /** The normalized gate list (one for `review`, N for `gates`); empty when no gate. */
  private readonly gates: GateState[];
  /** Gate lookups keyed once at construction (no per-signal/per-step linear scans). */
  private readonly gatesById: Map<string, GateState>;
  private readonly gatesByAfterStep: Map<string, GateState>;
  /** True for `lifecycle.gates` specs — controls whether events carry a `gate_id` (byte-parity). */
  private readonly multiGate: boolean;

  constructor(plan: LifecyclePlan, now: () => string | null) {
    this.plan = plan;
    this.now = now;
    this.gates = gatesFromPlan(plan);
    // Unique ids and distinct after_step are load-validated (spec.ts), so both maps are total.
    this.gatesById = new Map(this.gates.map((gate) => [gate.id, gate]));
    this.gatesByAfterStep = new Map(this.gates.map((gate) => [gate.afterStep, gate]));
    this.multiGate = plan.gates !== undefined;
  }

  /** The `gate_id` an event carries: the gate's id in multi-gate mode, omitted otherwise. */
  private gateIdField(gate: GateState): { gate_id?: string } {
    return this.multiGate ? { gate_id: gate.id } : {};
  }

  /**
   * Base overrides for gate events that carry NO decision (waiting/invalid): in multi-gate mode,
   * force the decision/route fields to null so a previously-resolved gate's singleton values
   * never leak into another gate's events (Python `_record` passes None; item 1 of the slice-4
   * review round). Single-`review` mode keeps this edition's legacy snapshot fallback so V1
   * event streams stay byte-identical.
   */
  private gateEventBase(gate?: GateState): Partial<WorkflowLifecycleEvent> {
    if (!this.multiGate) {
      return {};
    }
    return {
      review_user_decision: null,
      review_route_target: null,
      ...(gate !== undefined ? { gate_id: gate.id } : {}),
    };
  }

  /** The earliest-opened still-waiting gate (drives `waiting_checkpoint`), or undefined. */
  private earliestWaitingGate(): GateState | undefined {
    let earliest: GateState | undefined;
    for (const gate of this.gates) {
      if (gate.waiting && (earliest === undefined || (gate.openSeq ?? 0) < (earliest.openSeq ?? 0))) {
        earliest = gate;
      }
    }
    return earliest;
  }

  /** Recompute `waiting_checkpoint` WITHOUT touching `state` (used mid-event where state must hold). */
  private recomputeCheckpoint(): void {
    this.waitingCheckpoint = this.earliestWaitingGate()?.afterStep ?? null;
  }

  /** Recompute the earliest-opened still-waiting gate's checkpoint + the waiting/running state. */
  private refreshWaiting(): void {
    const earliest = this.earliestWaitingGate();
    if (earliest !== undefined) {
      this.state = "waiting_for_review";
      this.waitingCheckpoint = earliest.afterStep;
    } else {
      // Back to running only when no gate waits and the run is not terminal/cancelling.
      if (this.state === "waiting_for_review") {
        this.state = "running";
      }
      this.waitingCheckpoint = null;
    }
  }

  /**
   * The not-yet-fired gate to open after `stepId` completes, or undefined (distinct after_step,
   * DS4-1). A gate PRE-DECIDED before its checkpoint (routeTarget set, not fired) is still
   * returned, so the interpreter opens it and routes immediately — V1 pre-submission parity.
   */
  pendingGateAfter(stepId: string): GateState | undefined {
    const gate = this.gatesByAfterStep.get(stepId);
    return gate !== undefined && !gate.fired ? gate : undefined;
  }

  /** Record one event: a monotonic sequence + a full state snapshot, ring-buffered. */
  private record(event: string, overrides: Partial<WorkflowLifecycleEvent> = {}): void {
    this.sequence += 1;
    this.eventCount += 1;
    this.events.push({
      sequence: this.sequence,
      state: this.state,
      event,
      timestamp: this.now(),
      step_id: this.currentStep,
      completed_units: this.completedUnits,
      total_units: this.totalUnits,
      cancellation_requested: this.cancellationRequested,
      cancellation_reason: this.cancellationReason,
      waiting_checkpoint: this.waitingCheckpoint,
      review_user_decision: this.reviewUserDecision,
      review_route_target: this.reviewRouteTarget,
      terminal_status: this.terminalStatus,
      // Present-only (#299): omitted while null so non-compensating event streams are byte-identical
      // to Python's exclude-None model; carried on the terminal event once the unwind has set it.
      ...(this.compensationStatus !== null ? { compensation_status: this.compensationStatus } : {}),
      ...overrides,
    });
    // Ring buffer: Python builds deque(maxlen=status_event_limit), so 0 retains NO events
    // (every event still counts toward event_count/sequence — the wire counters advance).
    while (this.events.length > this.plan.statusEventLimit) {
      this.events.shift();
    }
  }

  started(): void {
    this.state = "running";
    this.record("workflow_started");
  }

  stepStarted(stepId: string): void {
    this.currentStep = stepId;
    this.record("step_started");
  }

  /**
   * A `when:` gate skipped a step or branch (#55 §3.3): provenance for "what did not
   * run and why". `step_id` is the SKIPPED id (current_step is untouched — the step
   * never runs) and `condition` carries the rendered predicate. Skipped work releases
   * its progress units via the caller's `skipUnits` (the existing clamp).
   */
  stepSkipped(stepId: string, condition: string): void {
    this.record("step_skipped", { step_id: stepId, condition });
  }

  /** Add units to the total (per-activity at plan time; per map item when `over` resolves). */
  addUnits(count: number): void {
    if (this.plan.progress) {
      this.totalUnits += count;
    }
  }

  unitCompleted(): void {
    // Python gates only the COUNTER on the progress toggle; the event records unconditionally
    // (workflow.py unit_completed), so event streams stay sequence-identical across SDKs.
    if (this.plan.progress) {
      this.completedUnits += 1;
    }
    this.record("progress");
  }

  /** Routing skipped steps: their units come off the total (Python `skip_units`). */
  skipUnits(count: number): void {
    if (this.plan.progress && count > 0) {
      // Python clamps to completed_units, never below: the progress ratio cannot exceed 100%.
      this.totalUnits = Math.max(this.completedUnits, this.totalUnits - count);
    }
  }

  /** The `typeflux_request_cancel` signal (no effect when cancellation is disabled). */
  requestCancel(reason: string | null | undefined): void {
    if (!this.plan.cancellation) {
      return;
    }
    this.cancellationRequested = true;
    this.cancellationReason = reason ?? null;
    this.state = "cancelling";
    this.record("cancellation_requested");
  }

  /**
   * A compensation began (#299 D299-2): `stepId` is the ORIGINAL compensated step id (always
   * present on compensation events), forced into `step_id` so it never inherits `current_step`.
   */
  compensationStarted(stepId: string): void {
    this.record("compensation_started", { step_id: stepId });
  }

  /** A compensation activity succeeded (#299). */
  compensationCompleted(stepId: string): void {
    this.record("compensation_completed", { step_id: stepId });
  }

  /** A compensation activity failed (#299): recorded, but the unwind continues (best-effort, loud). */
  compensationFailed(stepId: string): void {
    this.record("compensation_failed", { step_id: stepId });
  }

  /**
   * Record the terminal cancellation (the caller then raises TypefluxWorkflowCancelled). `status`
   * is the compensation outcome from the unwind that ran BEFORE this terminal event (#299 D299-2a).
   */
  cancelled(status: CompensationStatus = "none"): void {
    this.compensationStatus = status;
    this.state = "cancelled";
    this.terminalStatus = "cancelled";
    this.record("workflow_cancelled");
  }

  /**
   * Resolve which gate a `submit_review` command targets (#55 §8, decision DS4-2):
   * - explicit `gate` id ⇒ that gate, provided it has NOT already fired — a decision can be
   *   pre-staged on a not-yet-open gate, but a routed/timed-out gate is closed and is not
   *   targetable (never-guess rule; #55 slice 4 review round, item 3a). Unknown ids likewise.
   * - absent + a single-gate workflow ⇒ that one gate (preserves V1 pre-submission verbatim).
   * - absent + several gates ⇒ the sole currently-waiting gate, or none (ambiguous — never guessed).
   */
  private targetGate(gate: string | null | undefined): GateState | undefined {
    if (gate !== undefined && gate !== null) {
      const explicit = this.gatesById.get(gate);
      return explicit !== undefined && !explicit.fired ? explicit : undefined;
    }
    if (this.gates.length === 1) {
      return this.gates[0];
    }
    const waiting = this.gates.filter((g) => g.waiting);
    return waiting.length === 1 ? waiting[0] : undefined;
  }

  /**
   * The `typeflux_submit_review` signal (Python `submit_review`): a malformed command, an
   * un-targetable/ambiguous gate, or an unknown decision records `review_invalid_user_decision`
   * (setting the fail flag on the TARGET gate under its `fail` policy); a valid decision stores
   * the gate's route target and records `review_submitted`. The sequence bump wakes the gates'
   * `condition`.
   */
  submitReview(command: unknown): void {
    this.reviewSignalSequence += 1;
    const parsed = parseReviewCommand(command);
    if (parsed === undefined) {
      // Malformed shape: attribute the fail policy only when there's a single unambiguous gate.
      const soleGate = this.gates.length === 1 ? this.gates[0] : undefined;
      if (soleGate?.invalidUserDecision === "fail") {
        soleGate.invalidFailed = true;
      }
      this.record("review_invalid_user_decision", this.gateEventBase());
      return;
    }
    const gate = this.targetGate(parsed.gate);
    if (gate === undefined) {
      // Unknown/closed gate id, or ambiguous/absent target with several gates — record, never
      // guess. The ATTEMPTED gate id (when the command carried one) joins the event so the audit
      // trail distinguishes a mistyped/closed gate from an unknown decision (item 10).
      this.record("review_invalid_user_decision", {
        ...this.gateEventBase(),
        ...(parsed.gate !== null && parsed.gate !== undefined ? { gate_id: parsed.gate } : {}),
      });
      return;
    }
    const route = Object.hasOwn(gate.userDecisions, parsed.decision) ? gate.userDecisions[parsed.decision] : undefined;
    if (route === undefined) {
      if (gate.invalidUserDecision === "fail") {
        gate.invalidFailed = true;
      }
      this.record("review_invalid_user_decision", this.gateEventBase(gate));
      return;
    }
    gate.decision = parsed.decision;
    gate.routeTarget = route;
    // Singleton wire fields reflect the just-submitted decision, so a status query in the
    // post-submit/pre-route window shows it exactly as the V1 single gate did (byte-parity).
    this.reviewUserDecision = parsed.decision;
    this.reviewRouteTarget = route;
    this.record("review_submitted", {
      review_user_decision: parsed.decision,
      review_route_target: route,
      ...this.gateIdField(gate),
    });
  }

  /** The gate opened: waiting_for_review at its checkpoint (Python `waiting_for_review`). */
  waitingForGate(gate: GateState): void {
    gate.waiting = true;
    gate.openSeq = this.sequence + 1; // the sequence this event will carry
    this.refreshWaiting();
    this.record("waiting_for_review", { ...this.gateEventBase(gate), step_id: gate.afterStep });
  }

  /**
   * A decision (or timeout route) resolved a gate (Python `review_routed`): the gate stops waiting,
   * the checkpoint recomputes to the next earliest still-waiting gate (or null), the singleton
   * fields become this last-resolved gate's, and the route is recorded. Returns the target
   * (undefined = a defensive missing-route condition the interpreter raises as TypefluxReviewRouteMissing).
   */
  gateRouted(gate: GateState): string | undefined {
    gate.waiting = false;
    gate.openSeq = null;
    gate.fired = true;
    this.reviewUserDecision = gate.decision;
    this.reviewRouteTarget = gate.routeTarget;
    this.refreshWaiting();
    const target = gate.routeTarget ?? undefined;
    this.record("review_routed", {
      step_id: gate.afterStep,
      review_user_decision: gate.decision,
      review_route_target: target ?? null,
      ...this.gateIdField(gate),
    });
    return target;
  }

  /**
   * Apply a gate's timeout (Python `review_timed_out`): `route` resumes at the timeout route
   * (recorded like a decision route); `cancel` marks cancellation for the caller's next check;
   * `fail` moves to the terminal failed state (the interpreter raises TypefluxReviewTimeout).
   */
  gateTimedOut(gate: GateState): { action: "route"; route: string | undefined } | { action: "cancel" } | { action: "fail" } {
    gate.waiting = false;
    gate.openSeq = null;
    gate.fired = true;
    // The gate stops driving the checkpoint, but `state` stays "waiting_for_review" for the
    // review_timed_out event — the action flips state only AFTER (V1 single-gate byte-parity).
    this.recomputeCheckpoint();
    // Explicit nulls in BOTH modes (Python passes None): at a single-gate timeout the singletons
    // are null anyway (a decided gate routes, never times out), and in multi-gate mode this keeps
    // an earlier gate's resolved decision from leaking into this gate's timeout events (item 1).
    this.record("review_timed_out", {
      step_id: gate.afterStep,
      review_user_decision: null,
      review_route_target: null,
      ...this.gateIdField(gate),
    });
    if (gate.onTimeout === "route") {
      const route = gate.timeoutRoute;
      // Status queries read review_route_target, so the timeout route must be SET like a
      // submitted decision route, not only recorded on the event (Python #297 review).
      // The singleton decision clears with it: an earlier gate's submitted decision must
      // not pair with this gate's timeout route in status (no-op in single-gate mode —
      // a timed-out gate was never decided).
      gate.decision = null;
      gate.routeTarget = route ?? null;
      this.reviewUserDecision = null;
      this.reviewRouteTarget = route ?? null;
      // Back to running only when no gate still waits (another gate keeps waiting_for_review).
      this.refreshWaiting();
      this.record("review_routed", {
        step_id: gate.afterStep,
        review_user_decision: null,
        review_route_target: route ?? null,
        ...this.gateIdField(gate),
      });
      return { action: "route", route };
    }
    if (gate.onTimeout === "cancel") {
      this.cancellationRequested = true;
      this.cancellationReason = "review timed out";
      this.state = "cancelling";
      return { action: "cancel" };
    }
    this.state = "failed";
    this.terminalStatus = "failed";
    return { action: "fail" };
  }

  /** The `fail` invalid-decision policy tripped (Python `invalid_review_failed`). */
  invalidReviewFailedTerminal(): void {
    this.state = "failed";
    this.terminalStatus = "failed";
    this.record("review_invalid_user_decision_failed");
  }

  completed(): void {
    this.state = "completed";
    this.currentStep = null; // Python nulls current_step before recording workflow_completed
    this.terminalStatus = "completed";
    this.record("workflow_completed");
  }

  failed(status: CompensationStatus = "none"): void {
    // The compensation outcome is recorded even when a terminal state was already set (e.g. a
    // review-timeout `fail`): status queries then still surface how the unwind resolved (#299).
    if (this.compensationStatus === null) {
      this.compensationStatus = status;
    }
    if (this.terminalStatus !== null) {
      return; // Python's re-entry guard: a terminal state is never overwritten
    }
    this.state = "failed";
    // Python records the LITERAL "failed" (the exception type never enters the wire shape).
    this.terminalStatus = "failed";
    this.record("workflow_failed");
  }

  /** The execution-reported waiting gates, ordered by open time — `[]` when none. */
  private waitingGates(): WaitingGate[] {
    const open = this.gates.filter((gate) => gate.waiting);
    if (open.length === 0) {
      return [];
    }
    open.sort((a, b) => (a.openSeq ?? 0) - (b.openSeq ?? 0));
    return open.map((gate) => {
      const validUserDecisions: Record<string, string> = {};
      // Sorted, like Python's `sorted(user_decisions.items())` — stable cross-edition wire order.
      for (const decision of Object.keys(gate.userDecisions).sort()) {
        validUserDecisions[decision] = gate.userDecisions[decision]!;
      }
      return { gate_id: gate.id, after_step: gate.afterStep, valid_user_decisions: validUserDecisions };
    });
  }

  status(): WorkflowLifecycleStatus {
    return {
      state: this.state,
      current_step: this.currentStep,
      completed_units: this.completedUnits,
      total_units: this.totalUnits,
      cancellation_requested: this.cancellationRequested,
      cancellation_reason: this.cancellationReason,
      waiting_checkpoint: this.waitingCheckpoint,
      review_user_decision: this.reviewUserDecision,
      review_route_target: this.reviewRouteTarget,
      terminal_status: this.terminalStatus,
      event_count: this.eventCount,
      events_truncated: this.eventCount > this.events.length,
      oldest_event_sequence: this.events[0]?.sequence ?? null,
      // Python reads `self._sequence or None` — the latest sequence survives buffer truncation.
      latest_event_sequence: this.sequence > 0 ? this.sequence : null,
      events: [...this.events],
      // Always present, `[]` when no gate waits (status-field convention, §8).
      waiting_gates: this.waitingGates(),
      // Always present (null until the unwind runs), like terminal_status — #299 D299-2.
      compensation_status: this.compensationStatus,
    };
  }
}

/**
 * Validate a `typeflux_submit_review` payload as Python's `ReviewCommand.model_validate` does:
 * `user_decision` must be a string and `reviewer`/`notes`/`gate` each a string or null when
 * present — a `reviewer: 42` or `gate: 7` is an INVALID command across both SDKs, not a routable
 * decision. Returns the decision and the optional target gate id (#55 slice 4).
 */
function parseReviewCommand(command: unknown): { decision: string; gate?: string | null } | undefined {
  if (typeof command !== "object" || command === null) {
    return undefined;
  }
  const candidate = command as ReviewCommand;
  if (typeof candidate.user_decision !== "string") {
    return undefined;
  }
  for (const field of [candidate.reviewer, candidate.notes, candidate.gate]) {
    if (field !== undefined && field !== null && typeof field !== "string") {
      return undefined;
    }
  }
  return { decision: candidate.user_decision, gate: candidate.gate ?? null };
}
