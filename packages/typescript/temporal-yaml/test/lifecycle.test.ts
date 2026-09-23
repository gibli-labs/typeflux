import { describe, expect, it } from "vitest";

import { disabledLifecycleStatus, LifecycleRuntime, loadYamlSpec, workflowPlanFromSpec } from "../src/index.js";

const fixedNow = (): string => "2026-01-01T00:00:00.000Z";
const plan = (over: Partial<{ progress: boolean; cancellation: boolean; statusEventLimit: number }> = {}) => ({
  progress: true,
  cancellation: true,
  statusEventLimit: 50,
  ...over,
});

describe("LifecycleRuntime (#482 PR-A)", () => {
  it("records the Python event/status shapes with a monotonic sequence", () => {
    const runtime = new LifecycleRuntime(plan(), fixedNow);
    runtime.addUnits(2);
    runtime.started();
    runtime.stepStarted("review");
    runtime.unitCompleted();
    const status = runtime.status();
    expect(status.state).toBe("running");
    expect(status.current_step).toBe("review");
    expect(status.completed_units).toBe(1);
    expect(status.total_units).toBe(2);
    expect(status.events.map((e) => e.event)).toEqual(["workflow_started", "step_started", "progress"]);
    expect(status.events.map((e) => e.sequence)).toEqual([1, 2, 3]);
    // Every event carries the full snapshot keys (cross-SDK wire shape).
    expect(Object.keys(status.events[0]!).sort()).toEqual(
      [
        "sequence",
        "state",
        "event",
        "timestamp",
        "step_id",
        "completed_units",
        "total_units",
        "cancellation_requested",
        "cancellation_reason",
        "waiting_checkpoint",
        "review_user_decision",
        "review_route_target",
        "terminal_status",
      ].sort(),
    );
    expect(status.events[2]?.timestamp).toBe(fixedNow());
  });

  it("ring-buffers events at status_event_limit with truncation counters (0 retains nothing)", () => {
    const runtime = new LifecycleRuntime(plan({ statusEventLimit: 2 }), fixedNow);
    runtime.started();
    runtime.stepStarted("a");
    runtime.stepStarted("b");
    const status = runtime.status();
    expect(status.events).toHaveLength(2);
    expect(status.event_count).toBe(3);
    expect(status.events_truncated).toBe(true);
    expect(status.oldest_event_sequence).toBe(2);
    expect(status.latest_event_sequence).toBe(3);

    // Python deque(maxlen=0): a zero limit keeps NO events while the wire counters advance.
    const none = new LifecycleRuntime(plan({ statusEventLimit: 0 }), fixedNow);
    none.started();
    none.stepStarted("s");
    const noneStatus = none.status();
    expect(noneStatus.events).toHaveLength(0);
    expect(noneStatus.event_count).toBe(2);
    expect(noneStatus.events_truncated).toBe(true);
    expect(noneStatus.oldest_event_sequence).toBeNull();
    expect(noneStatus.latest_event_sequence).toBe(2); // sequence-based, survives truncation
  });

  it("honors the cancellation toggle and records the cancel lifecycle", () => {
    const disabled = new LifecycleRuntime(plan({ cancellation: false }), fixedNow);
    disabled.requestCancel("nope");
    expect(disabled.cancellationRequested).toBe(false);
    expect(disabled.status().events).toHaveLength(0);

    const runtime = new LifecycleRuntime(plan(), fixedNow);
    runtime.started();
    runtime.requestCancel("operator says stop");
    expect(runtime.cancellationRequested).toBe(true);
    expect(runtime.status().state).toBe("cancelling");
    expect(runtime.status().cancellation_reason).toBe("operator says stop");
    runtime.cancelled();
    const status = runtime.status();
    expect(status.state).toBe("cancelled");
    expect(status.terminal_status).toBe("cancelled");
    expect(status.events.map((e) => e.event)).toEqual([
      "workflow_started",
      "cancellation_requested",
      "workflow_cancelled",
    ]);
  });

  it("honors the progress toggle (counters gated; the progress EVENT records unconditionally)", () => {
    const runtime = new LifecycleRuntime(plan({ progress: false }), fixedNow);
    runtime.addUnits(5);
    runtime.unitCompleted();
    const status = runtime.status();
    expect(status.total_units).toBe(0);
    expect(status.completed_units).toBe(0);
    // Python records the event either way, keeping event streams sequence-identical across SDKs.
    expect(status.events.filter((e) => e.event === "progress")).toHaveLength(1);
  });

  it("records review_invalid_user_decision for any review command (no routes until PR-B)", () => {
    const runtime = new LifecycleRuntime(plan(), fixedNow);
    runtime.submitReview({ user_decision: "approve" });
    runtime.submitReview("not even an object");
    const events = runtime.status().events;
    expect(events.map((e) => e.event)).toEqual(["review_invalid_user_decision", "review_invalid_user_decision"]);
    // Python records the invalid event WITHOUT the decision string (wire parity).
    expect(events[0]?.review_user_decision).toBeNull();
    expect(events[1]?.review_user_decision).toBeNull();
  });

  it("terminal events: completed clears current_step; failed records the literal 'failed'", () => {
    const done = new LifecycleRuntime(plan(), fixedNow);
    done.started();
    done.stepStarted("last");
    done.completed();
    expect(done.status()).toMatchObject({ state: "completed", terminal_status: "completed", current_step: null });

    const failed = new LifecycleRuntime(plan(), fixedNow);
    failed.started();
    failed.failed();
    // Python's wire value is the literal "failed" — the exception type never enters the shape.
    expect(failed.status()).toMatchObject({ state: "failed", terminal_status: "failed" });
    failed.failed(); // re-entry guard: terminal state is never overwritten or re-recorded
    expect(failed.status().events.filter((e) => e.event === "workflow_failed")).toHaveLength(1);
  });

  it("skipUnits never drops the total below completed (Python clamp)", () => {
    const runtime = new LifecycleRuntime(plan(), fixedNow);
    runtime.addUnits(8);
    for (let i = 0; i < 5; i += 1) {
      runtime.unitCompleted();
    }
    runtime.skipUnits(6);
    expect(runtime.status()).toMatchObject({ completed_units: 5, total_units: 5 });
  });

  it("disabledLifecycleStatus matches Python's disabled response", () => {
    expect(disabledLifecycleStatus()).toMatchObject({ state: "disabled", events: [], event_count: 0 });
  });
});

describe("LifecycleRuntime review gate (#482 PR-B)", () => {
  const reviewPlan = (over: Record<string, unknown> = {}) =>
    plan({
      review: {
        afterStep: "gate",
        userDecisions: { approve: "publish", reject: "revise" },
        invalidUserDecision: "warn",
        ...over,
      },
    } as never);

  it("stores a valid decision's route and records review_submitted with the wire fields", () => {
    const runtime = new LifecycleRuntime(reviewPlan(), fixedNow);
    runtime.submitReview({ user_decision: "approve", reviewer: "sam" });
    expect(runtime.reviewRouteTarget).toBe("publish");
    expect(runtime.reviewSignalSequence).toBe(1);
    const event = runtime.status().events.at(-1);
    expect(event).toMatchObject({
      event: "review_submitted",
      review_user_decision: "approve",
      review_route_target: "publish",
    });
  });

  it("records invalid decisions (unknown + malformed) without failing under warn, then a valid one resolves", () => {
    const runtime = new LifecycleRuntime(reviewPlan(), fixedNow);
    runtime.submitReview({ user_decision: "maybe" });
    runtime.submitReview("garbage");
    // Full ReviewCommand validation (Python model_validate): a non-string reviewer/notes is
    // an INVALID command, not a routable decision — same wire payload, same outcome cross-SDK.
    runtime.submitReview({ user_decision: "approve", reviewer: 42 });
    runtime.submitReview({ user_decision: "approve", notes: 7 });
    expect(runtime.reviewRouteTarget).toBeNull();
    expect(runtime.reviewSignalSequence).toBe(4);
    expect(runtime.pendingGateAfter("gate")!.invalidFailed).toBe(false);
    expect(runtime.reviewRouteTarget).toBeNull();
    runtime.submitReview({ user_decision: "reject", reviewer: "sam", notes: null });
    expect(runtime.reviewRouteTarget).toBe("revise");
  });

  it("sets the per-gate fail flag under the fail policy", () => {
    const runtime = new LifecycleRuntime(reviewPlan({ invalidUserDecision: "fail" }), fixedNow);
    runtime.submitReview({ user_decision: "maybe" });
    expect(runtime.pendingGateAfter("gate")!.invalidFailed).toBe(true);
    runtime.invalidReviewFailedTerminal();
    expect(runtime.status()).toMatchObject({ state: "failed", terminal_status: "failed" });
    expect(runtime.status().events.at(-1)?.event).toBe("review_invalid_user_decision_failed");
  });

  it("waitingForGate and gateRouted drive state/checkpoint and record the route", () => {
    const runtime = new LifecycleRuntime(reviewPlan(), fixedNow);
    runtime.started();
    const gate = runtime.pendingGateAfter("gate")!;
    runtime.waitingForGate(gate);
    expect(runtime.status()).toMatchObject({ state: "waiting_for_review", waiting_checkpoint: "gate" });
    // The single `review` gate reports through waiting_gates named "review"; events carry no gate_id.
    expect(runtime.status().waiting_gates).toEqual([
      { gate_id: "review", after_step: "gate", valid_user_decisions: { approve: "publish", reject: "revise" } },
    ]);
    expect(runtime.status().events.at(-1)?.gate_id).toBeUndefined();
    runtime.submitReview({ user_decision: "approve" });
    const target = runtime.gateRouted(gate);
    expect(target).toBe("publish");
    expect(runtime.status()).toMatchObject({ state: "running", waiting_checkpoint: null, review_route_target: "publish" });
    expect(runtime.status().waiting_gates).toEqual([]); // always present, empty once resolved
    expect(runtime.status().events.at(-1)).toMatchObject({ event: "review_routed", review_route_target: "publish" });
  });

  it("gateTimedOut applies route / cancel / fail (Python review_timed_out)", () => {
    const routed = new LifecycleRuntime(reviewPlan({ onTimeout: "route", timeoutRoute: "revise", timeoutSeconds: 5 }), fixedNow);
    const routedGate = routed.pendingGateAfter("gate")!;
    routed.waitingForGate(routedGate);
    expect(routed.gateTimedOut(routedGate)).toEqual({ action: "route", route: "revise" });
    expect(routed.status()).toMatchObject({ state: "running", review_route_target: "revise" });
    expect(routed.status().events.map((e) => e.event).slice(-2)).toEqual(["review_timed_out", "review_routed"]);
    // Byte-parity: the review_timed_out event snapshots state BEFORE the action flips it (V1).
    const timedOutEvent = routed.status().events.find((e) => e.event === "review_timed_out")!;
    expect(timedOutEvent).toMatchObject({ state: "waiting_for_review", waiting_checkpoint: null });

    const cancelled = new LifecycleRuntime(reviewPlan({ onTimeout: "cancel", timeoutSeconds: 5 }), fixedNow);
    const cancelledGate = cancelled.pendingGateAfter("gate")!;
    cancelled.waitingForGate(cancelledGate);
    expect(cancelled.gateTimedOut(cancelledGate)).toEqual({ action: "cancel" });
    expect(cancelled.status()).toMatchObject({ state: "cancelling", cancellation_reason: "review timed out" });

    const failed = new LifecycleRuntime(reviewPlan({ onTimeout: "fail", timeoutSeconds: 5 }), fixedNow);
    const failedGate = failed.pendingGateAfter("gate")!;
    failed.waitingForGate(failedGate);
    expect(failed.gateTimedOut(failedGate)).toEqual({ action: "fail" });
    expect(failed.status()).toMatchObject({ state: "failed", terminal_status: "failed" });
  });
});

describe("LifecycleRuntime multiple gates (#55 slice 4)", () => {
  const gatesPlan = () =>
    plan({
      gates: [
        { id: "first", afterStep: "a", userDecisions: { go: "b" }, invalidUserDecision: "warn" },
        { id: "second", afterStep: "b", userDecisions: { yes: "c", no: "c" }, invalidUserDecision: "fail" },
      ],
    } as never);

  it("stamps gate_id on multi-gate review events and resolves the addressed gate", () => {
    const runtime = new LifecycleRuntime(gatesPlan(), fixedNow);
    runtime.started();
    const first = runtime.pendingGateAfter("a")!;
    runtime.waitingForGate(first);
    // waiting_gates reports the open gate with its own decisions; the event carries gate_id.
    expect(runtime.status().waiting_gates).toEqual([
      { gate_id: "first", after_step: "a", valid_user_decisions: { go: "b" } },
    ]);
    expect(runtime.status().events.at(-1)).toMatchObject({ event: "waiting_for_review", gate_id: "first" });
    // A decision addressed to `first` by id resolves only that gate.
    runtime.submitReview({ user_decision: "go", gate: "first" });
    expect(first.routeTarget).toBe("b");
    expect(runtime.gateRouted(first)).toBe("b");
    expect(runtime.status()).toMatchObject({ state: "running", waiting_checkpoint: null, review_route_target: "b" });
    expect(runtime.status().events.at(-1)).toMatchObject({ event: "review_routed", gate_id: "first" });
  });

  it("pre-stages a decision for a not-yet-open gate by id, applied when it opens", () => {
    const runtime = new LifecycleRuntime(gatesPlan(), fixedNow);
    runtime.started();
    // Decide `second` before it opens — stored on that gate, not on `first`.
    runtime.submitReview({ user_decision: "yes", gate: "second" });
    expect(runtime.pendingGateAfter("a")!.routeTarget).toBeNull();
    const second = runtime.pendingGateAfter("b")!;
    expect(second.routeTarget).toBe("c");
  });

  it("rejects an ambiguous decision (several waiting, no gate) but honors an explicit gate", () => {
    const runtime = new LifecycleRuntime(gatesPlan(), fixedNow);
    runtime.started();
    const first = runtime.pendingGateAfter("a")!;
    const second = runtime.pendingGateAfter("b")!;
    runtime.waitingForGate(first);
    runtime.waitingForGate(second);
    // Two gates waiting, no `gate` field ⇒ ambiguous ⇒ recorded invalid, nothing resolved.
    runtime.submitReview({ user_decision: "go" });
    expect(runtime.status().events.at(-1)?.event).toBe("review_invalid_user_decision");
    expect(first.routeTarget).toBeNull();
    expect(second.routeTarget).toBeNull();
    // waiting_gates lists both, ordered by open time (first opened first).
    expect(runtime.status().waiting_gates?.map((g) => g.gate_id)).toEqual(["first", "second"]);
    // waiting_checkpoint tracks the EARLIEST-opened still-waiting gate.
    expect(runtime.status().waiting_checkpoint).toBe("a");
    // An explicit gate id resolves exactly that gate.
    runtime.submitReview({ user_decision: "yes", gate: "second" });
    expect(second.routeTarget).toBe("c");
    expect(first.routeTarget).toBeNull();
  });

  it("an unknown gate id and an unknown decision are recorded invalid; fail policy trips per-gate", () => {
    const runtime = new LifecycleRuntime(gatesPlan(), fixedNow);
    runtime.started();
    runtime.submitReview({ user_decision: "go", gate: "nope" });
    // The ATTEMPTED gate id joins the audit event (mistyped-gate vs unknown-decision, item 10).
    expect(runtime.status().events.at(-1)).toMatchObject({
      event: "review_invalid_user_decision",
      gate_id: "nope",
      review_user_decision: null,
      review_route_target: null,
    });
    expect(runtime.pendingGateAfter("a")!.invalidFailed).toBe(false);
    expect(runtime.pendingGateAfter("b")!.invalidFailed).toBe(false);
    // An unknown decision on the `fail`-policy `second` gate trips ONLY that gate's flag.
    runtime.submitReview({ user_decision: "maybe", gate: "second" });
    expect(runtime.pendingGateAfter("b")!.invalidFailed).toBe(true);
    expect(runtime.pendingGateAfter("a")!.invalidFailed).toBe(false);
    expect(runtime.status().events.at(-1)).toMatchObject({ event: "review_invalid_user_decision", gate_id: "second" });
  });

  it("a closed (routed) gate is not explicitly targetable and its fail policy cannot leak (item 3)", () => {
    const runtime = new LifecycleRuntime(gatesPlan(), fixedNow);
    runtime.started();
    const first = runtime.pendingGateAfter("a")!;
    runtime.waitingForGate(first);
    runtime.submitReview({ user_decision: "go", gate: "first" });
    runtime.gateRouted(first);
    // Explicit targeting of the CLOSED `first` gate records invalid (never-guess), even with a
    // decision that was valid while it was open.
    runtime.submitReview({ user_decision: "go", gate: "first" });
    expect(runtime.status().events.at(-1)).toMatchObject({
      event: "review_invalid_user_decision",
      gate_id: "first",
    });
    // An invalid decision aimed at the closed fail-policy `second`... first close it by routing:
    const second = runtime.pendingGateAfter("b")!;
    runtime.waitingForGate(second);
    runtime.submitReview({ user_decision: "yes", gate: "second" });
    runtime.gateRouted(second);
    runtime.submitReview({ user_decision: "bogus", gate: "second" });
    // The closed gate is un-targetable, so its `fail` policy CANNOT trip any flag.
    expect(second.invalidFailed).toBe(false);
    expect(first.invalidFailed).toBe(false);
  });

  it("gate B's timeout events never carry gate A's resolved decision (item 1 cross-edition parity)", () => {
    const runtime = new LifecycleRuntime(
      plan({
        gates: [
          { id: "first", afterStep: "a", userDecisions: { go: "b" }, invalidUserDecision: "warn" },
          {
            id: "second",
            afterStep: "b",
            userDecisions: { yes: "c" },
            invalidUserDecision: "warn",
            onTimeout: "route",
            timeoutRoute: "c",
            timeoutSeconds: 5,
          },
        ],
      } as never),
      fixedNow,
    );
    runtime.started();
    // Gate A decided + routed: the singleton wire fields now hold A's decision.
    const first = runtime.pendingGateAfter("a")!;
    runtime.waitingForGate(first);
    runtime.submitReview({ user_decision: "go", gate: "first" });
    runtime.gateRouted(first);
    expect(runtime.status().review_user_decision).toBe("go");
    // Gate B opens, then times out: NONE of B's events may carry A's decision (Python None/None).
    const second = runtime.pendingGateAfter("b")!;
    runtime.waitingForGate(second);
    expect(runtime.gateTimedOut(second)).toEqual({ action: "route", route: "c" });
    const bEvents = runtime.status().events.filter((e) => e.gate_id === "second");
    expect(bEvents.map((e) => e.event)).toEqual(["waiting_for_review", "review_timed_out", "review_routed"]);
    for (const event of bEvents) {
      expect(event.review_user_decision).toBeNull();
    }
    expect(bEvents.at(-1)?.review_route_target).toBe("c"); // the timeout route, not A's route
    expect(bEvents[0]?.review_route_target).toBeNull();
    expect(bEvents[1]?.review_route_target).toBeNull();
    // STATUS must not pair A's decision with B's timeout route: the singleton
    // decision clears when a timeout route is set.
    expect(runtime.status().review_user_decision).toBeNull();
    expect(runtime.status().review_route_target).toBe("c");
  });

  it("waiting_checkpoint advances to the next still-waiting gate when the earliest resolves", () => {
    const runtime = new LifecycleRuntime(gatesPlan(), fixedNow);
    runtime.started();
    const first = runtime.pendingGateAfter("a")!;
    const second = runtime.pendingGateAfter("b")!;
    runtime.waitingForGate(first);
    runtime.waitingForGate(second);
    runtime.submitReview({ user_decision: "go", gate: "first" });
    runtime.gateRouted(first);
    // `first` resolved; the checkpoint moves to the still-waiting `second`.
    expect(runtime.status()).toMatchObject({ state: "waiting_for_review", waiting_checkpoint: "b" });
    expect(runtime.status().waiting_gates?.map((g) => g.gate_id)).toEqual(["second"]);
  });

  it("a single-gate `gates` list accepts an implicit (gateless) decision like the V1 review", () => {
    const runtime = new LifecycleRuntime(
      plan({ gates: [{ id: "only", afterStep: "a", userDecisions: { ok: "b" }, invalidUserDecision: "warn" }] } as never),
      fixedNow,
    );
    runtime.started();
    // No `gate` field, single gate ⇒ unambiguous (V1 pre-submission parity), even before it opens.
    runtime.submitReview({ user_decision: "ok" });
    expect(runtime.pendingGateAfter("a")!.routeTarget).toBe("b");
  });
});

describe("workflowPlanFromSpec review validation (#482 PR-B)", () => {
  const REVIEW_SPEC = (review: string, extraSteps = "") => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
activities:
  definitions:
    - name: draft
      input: schemas:In
      output: schemas:Mid
      prompt: p/x
    - name: publish
      input: schemas:Mid
      output: schemas:Out
      prompt: p/x
    - name: mismatch
      input: schemas:Other
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  input: schemas:In
  output: schemas:Out
  lifecycle:
    enabled: true
    review:
${review}
  steps:
    - id: gate
      activity: draft
    - id: publish_step
      activity: publish
${extraSteps}
`;

  it("maps a valid review block (decisions, policy, timeout) into the plan", () => {
    const plan_ = workflowPlanFromSpec(
      loadYamlSpec(
        REVIEW_SPEC(
          "      after_step: gate\n      user_decisions:\n        approve: { route: publish_step }\n      invalid_user_decision: fail\n      timeout: { seconds: 30, on_timeout: route, route: publish_step }",
        ),
      ),
    );
    expect(plan_.lifecycle?.review).toEqual({
      afterStep: "gate",
      userDecisions: { approve: "publish_step" },
      invalidUserDecision: "fail",
      timeoutSeconds: 30,
      onTimeout: "route",
      timeoutRoute: "publish_step",
    });
  });

  it("validates the review block even when the lifecycle is DISABLED (Python validates unconditionally)", () => {
    const disabled = REVIEW_SPEC(
      "      after_step: nope\n      user_decisions:\n        approve: { route: publish_step }",
    ).replace("    enabled: true", "    enabled: false");
    expect(() => workflowPlanFromSpec(loadYamlSpec(disabled))).toThrow(/after_step references unknown step/);
  });

  it("rejects unknown after_step and unknown routes", () => {
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(REVIEW_SPEC("      after_step: nope\n      user_decisions:\n        approve: { route: publish_step }")),
      ),
    ).toThrow(/after_step references unknown step/);
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(REVIEW_SPEC("      after_step: gate\n      user_decisions:\n        approve: { route: nowhere }")),
      ),
    ).toThrow(/routes to unknown step/);
  });

  it("rejects backward routes (forward-only, Python load-time constraint)", () => {
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(
          REVIEW_SPEC("      after_step: publish_step\n      user_decisions:\n        redo: { route: gate }"),
        ),
      ),
    ).toThrow(/forward-only/);
  });

  it("rejects a route whose tail map reads a skipped step's output (Python context_types walk)", () => {
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(
          REVIEW_SPEC(
            "      after_step: gate\n      user_decisions:\n        approve: { route: map_step }",
            "    - id: skipped\n      activity: publish\n    - id: map_step\n      map: { activity: publish, over: skipped.items }",
          ).replace("    - id: publish_step\n      activity: publish\n", ""),
        ),
      ),
    ).toThrow(/reads "skipped.items" — the route would skip the step that produces it/);
  });

  it("rejects a route whose tail refs do not chain (the TS analogue of Python's type walk)", () => {
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(
          REVIEW_SPEC(
            "      after_step: gate\n      user_decisions:\n        approve: { route: bad_step }",
            "    - id: bad_step\n      activity: mismatch",
          ),
        ),
      ),
    ).toThrow(/expects "schemas:Other", but the review checkpoint output is "schemas:Mid"/);
  });
});

describe("workflowPlanFromSpec multiple gates (#55 slice 4)", () => {
  // draft: In→Mid, refine: Mid→Mid, publish: Mid→Out — two gates after distinct steps.
  const GATES_SPEC = (gates: string) => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
activities:
  definitions:
    - name: draft
      input: schemas:In
      output: schemas:Mid
      prompt: p/x
    - name: refine
      input: schemas:Mid
      output: schemas:Mid
      prompt: p/x
    - name: publish
      input: schemas:Mid
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  input: schemas:In
  output: schemas:Out
  lifecycle:
    enabled: true
    gates:
${gates}
  steps:
    - id: gate
      activity: draft
    - id: refine_step
      activity: refine
    - id: publish_step
      activity: publish
`;
  const TWO_GATES =
    "      - id: g1\n        after_step: gate\n        user_decisions:\n          go: { route: refine_step }\n" +
    "      - id: g2\n        after_step: refine_step\n        user_decisions:\n          ok: { route: publish_step }\n";

  it("maps two valid gates into plan.lifecycle.gates (and no single `review`)", () => {
    const plan_ = workflowPlanFromSpec(loadYamlSpec(GATES_SPEC(TWO_GATES)));
    expect(plan_.lifecycle?.review).toBeUndefined();
    expect(plan_.lifecycle?.gates).toEqual([
      { id: "g1", afterStep: "gate", userDecisions: { go: "refine_step" }, invalidUserDecision: "warn" },
      { id: "g2", afterStep: "refine_step", userDecisions: { ok: "publish_step" }, invalidUserDecision: "warn" },
    ]);
  });

  it("rejects declaring both `review` and `gates`", () => {
    const both = GATES_SPEC(TWO_GATES).replace(
      "    gates:\n",
      "    review: { after_step: gate, user_decisions: { go: { route: refine_step } } }\n    gates:\n",
    );
    expect(() => loadYamlSpec(both)).toThrow(/use either `review`.*or `gates`.*not both/s);
  });

  it("rejects duplicate gate ids", () => {
    const dup = TWO_GATES.replace("id: g2", "id: g1");
    expect(() => loadYamlSpec(GATES_SPEC(dup))).toThrow(/duplicate gate id "g1"/);
  });

  it("rejects two gates sharing an after_step (distinct-checkpoint rule, DS4-1)", () => {
    const shared = TWO_GATES.replace("after_step: refine_step", "after_step: gate");
    expect(() => loadYamlSpec(GATES_SPEC(shared))).toThrow(/shares after_step "gate" with an earlier gate/);
  });

  it("rejects a decision name reused across gates with DIVERGENT routes; identical reuse is allowed (DS4-6)", () => {
    // `ok` on g1 routes to refine_step, but `ok` on g2 routes to publish_step — the CP's flat
    // fallback union would silently advertise the later gate's route; reject at load.
    const divergent =
      "      - id: g1\n        after_step: gate\n        user_decisions:\n          ok: { route: refine_step }\n" +
      "      - id: g2\n        after_step: refine_step\n        user_decisions:\n          ok: { route: publish_step }\n";
    expect(() => loadYamlSpec(GATES_SPEC(divergent))).toThrow(
      /decision "ok" routes to "refine_step" on gate "g1" but to "publish_step" on gate "g2"/,
    );
    // Identical route semantics across gates stay allowed (the union is well-defined).
    const identical =
      "      - id: g1\n        after_step: gate\n        user_decisions:\n          skip: { route: publish_step }\n" +
      "      - id: g2\n        after_step: refine_step\n        user_decisions:\n          skip: { route: publish_step }\n";
    expect(() => workflowPlanFromSpec(loadYamlSpec(GATES_SPEC(identical)))).not.toThrow();
  });

  it("validates each gate's routes forward-only, scoped by gate id", () => {
    const backward = TWO_GATES.replace(
      "      - id: g2\n        after_step: refine_step\n        user_decisions:\n          ok: { route: publish_step }\n",
      "      - id: g2\n        after_step: refine_step\n        user_decisions:\n          back: { route: gate }\n",
    );
    expect(() => workflowPlanFromSpec(loadYamlSpec(GATES_SPEC(backward)))).toThrow(/gate "g2".*forward-only/s);
  });
});

describe("workflowPlanFromSpec lifecycle (#482 PR-A)", () => {
  const SPEC = (lifecycle: string) => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: do it } }
  provider: { type: openai }
activities:
  definitions:
    - name: a
      input: schemas:In
      output: schemas:Out
      prompt: p/x
workflow:
  name: W
  input: schemas:In
${lifecycle}
  steps:
    - id: s
      activity: a
`;

  it("maps an enabled lifecycle block (defaults applied) into the plan", () => {
    const plan_ = workflowPlanFromSpec(loadYamlSpec(SPEC("  lifecycle: { enabled: true }")));
    expect(plan_.lifecycle).toEqual({ progress: true, cancellation: true, statusEventLimit: 50 });
  });

  it("maps explicit toggles and history limits", () => {
    const plan_ = workflowPlanFromSpec(
      loadYamlSpec(
        SPEC("  lifecycle: { enabled: true, progress: false, cancellation: false, history: { status_event_limit: 7 } }"),
      ),
    );
    expect(plan_.lifecycle).toEqual({ progress: false, cancellation: false, statusEventLimit: 7 });
  });

  it("omits the plan lifecycle when disabled or absent (the interpreter answers state=disabled)", () => {
    expect(workflowPlanFromSpec(loadYamlSpec(SPEC("  lifecycle: { enabled: false }"))).lifecycle).toBeUndefined();
    expect(workflowPlanFromSpec(loadYamlSpec(SPEC(""))).lifecycle).toBeUndefined();
  });
});
