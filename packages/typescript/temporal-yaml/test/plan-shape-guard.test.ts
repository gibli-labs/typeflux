// The interpreter plan-shape guard (#55 §5.2): an old worker handed a new-shape plan
// must fail loud with a nonRetryable TypefluxPlanUnsupported, never misexecute it.
// The two concrete pre-guard failure modes this pins:
//   - `steps: []` ran the while loop zero times and completed "successfully" with the
//     input as the result (a silent no-op);
//   - `steps` absent TypeError'd mid-loop (`plan.steps.length` on undefined) — a
//     workflow-TASK failure, i.e. an indefinite retry hang, not a workflow failure.

import { describe, expect, it } from "vitest";

import { ApplicationFailure } from "@temporalio/workflow";

import { planUnsupportedReason, type WorkflowPlan } from "../src/workflow-plan.js";
import { typefluxYamlWorkflow } from "../src/workflows.js";

const v1Plan = (): WorkflowPlan => ({
  steps: [
    { kind: "activity", id: "a", activity: "act_a" },
    {
      kind: "map",
      id: "m",
      activity: "act_m",
      over: "input.items",
      concurrency: 2,
      collectField: "results",
      collectMaxBytes: 1_500_000,
      sessionCache: { enabled: true, ttlSeconds: 60 },
    },
  ],
  lifecycle: {
    progress: true,
    cancellation: true,
    statusEventLimit: 50,
    review: {
      afterStep: "a",
      userDecisions: { approve: "m" },
      invalidUserDecision: "warn",
      timeoutSeconds: 60,
      onTimeout: "route",
      timeoutRoute: "m",
    },
  },
  activityOptions: { act_a: { startToCloseTimeoutMs: 1000 } },
  retryPolicy: { maximumAttempts: 3, initialIntervalMs: 100, backoffCoefficient: 2 },
});

describe("planUnsupportedReason", () => {
  it("accepts a maximal V1 plan (every implemented key present)", () => {
    expect(planUnsupportedReason(v1Plan())).toBeUndefined();
  });

  it("accepts the minimal V1 plan", () => {
    expect(planUnsupportedReason({ steps: [{ kind: "activity", id: "a", activity: "x" }] })).toBeUndefined();
  });

  it("rejects a non-object plan", () => {
    expect(planUnsupportedReason(null)).toMatch(/not an object/);
    expect(planUnsupportedReason([])).toMatch(/not an object/);
    expect(planUnsupportedReason("plan")).toMatch(/not an object/);
  });

  it("rejects empty and absent steps (the silent no-op / TypeError hazards)", () => {
    expect(planUnsupportedReason({ steps: [] })).toMatch(/has no steps/);
    expect(planUnsupportedReason({})).toMatch(/has no steps/);
    expect(planUnsupportedReason({ steps: "nope" })).toMatch(/has no steps/);
  });

  it("rejects a step of an unknown kind, naming the step and the kind", () => {
    // A future node shape an older worker cannot interpret — exactly the version-skew case.
    const reason = planUnsupportedReason({
      steps: [{ kind: "loop", id: "p", body: {} }],
    });
    expect(reason).toMatch(/step "p" has kind "loop", which this worker does not implement/);
  });

  it("rejects a subworkflow step missing its embedded child-identity keys (#55 §6)", () => {
    // `subworkflow` IS a known kind now (slice 3) — but the interpreter needs the resolve-time
    // embedded identity to start the child, so a drifted node missing it must fail loud.
    const reason = planUnsupportedReason({
      steps: [{ kind: "subworkflow", id: "p", workflowName: "child", plan: { steps: [] } }],
    });
    expect(reason).toMatch(/kind "subworkflow"\) is missing required key\(s\)/);
  });

  it("rejects a kind-less step", () => {
    expect(planUnsupportedReason({ steps: [{ id: "s", activity: "x" }] })).toMatch(/kind undefined/);
  });

  it("rejects a step missing a required field", () => {
    const reason = planUnsupportedReason({ steps: [{ kind: "map", id: "m", activity: "x" }] });
    expect(reason).toMatch(/step "m" \(kind "map"\) is missing required key\(s\): over/);
  });

  it("rejects an unknown STEP key — new gating semantics must not be silently ignored", () => {
    const reason = planUnsupportedReason({
      steps: [{ kind: "activity", id: "a", activity: "x", onError: { route: "b" } }],
    });
    expect(reason).toMatch(/step "a" carries key\(s\) this worker does not implement: onError/);
  });

  it("accepts a composition plan: parallel branches with when gates (#55 slice 1)", () => {
    expect(
      planUnsupportedReason({
        steps: [
          { kind: "activity", id: "classify", activity: "classify" },
          {
            kind: "parallel",
            id: "reviews",
            when: { mode: "leaf", predicates: [{ path: "classify.route", op: "eq", value: "review" }] },
            collectMaxBytes: 1_500_000,
            branches: [
              {
                id: "legal",
                when: { mode: "all", predicates: [{ path: "classify.needs_legal", op: "eq", value: true }] },
                steps: [{ kind: "activity", id: "legal_screen", activity: "legal_screen" }],
              },
              { id: "medical", steps: [{ kind: "activity", id: "medical_review", activity: "medical_review" }] },
            ],
          },
        ],
      }),
    ).toBeUndefined();
  });

  it("rejects unknown when modes/operators and unknown branch keys — forward skew stays loud", () => {
    const leaf = (op: string) => ({ mode: "leaf", predicates: [{ path: "input.x", op, value: 1 }] });
    const planWith = (when: unknown, branchExtra: Record<string, unknown> = {}) => ({
      steps: [
        {
          kind: "parallel",
          id: "p",
          branches: [{ id: "b", steps: [{ kind: "activity", id: "s", activity: "x" }], ...branchExtra }, ...[]],
          ...(when !== undefined ? { when } : {}),
        },
      ],
    });
    expect(planUnsupportedReason(planWith(leaf("matches")))).toMatch(/operator "matches", which this worker does not implement/);
    expect(planUnsupportedReason(planWith({ mode: "not", predicates: [] }))).toMatch(/mode "not", which this worker does not implement/);
    expect(planUnsupportedReason(planWith({ mode: "leaf", predicates: [] }))).toMatch(/has no predicates/);
    // A multi-predicate "leaf" would evaluate criteria the rendered condition hides.
    expect(
      planUnsupportedReason(
        planWith({
          mode: "leaf",
          predicates: [
            { path: "input.x", op: "eq", value: 1 },
            { path: "input.y", op: "eq", value: 2 },
          ],
        }),
      ),
    ).toMatch(/mode "leaf" with 2 predicates/);
    expect(planUnsupportedReason(planWith(undefined, { concurrency: 2 }))).toMatch(
      /branch of plan step "p" carries key\(s\) this worker does not implement: concurrency/,
    );
    expect(planUnsupportedReason({ steps: [{ kind: "parallel", id: "p", branches: [] }] })).toMatch(/has no branches/);
  });

  it("rejects branches without an id and duplicate branch ids (collect-field integrity)", () => {
    const branch = (id?: string) => ({
      ...(id !== undefined ? { id } : {}),
      steps: [{ kind: "activity", id: `s_${id ?? "x"}_${Math.random().toString(36).slice(2)}`, activity: "a" }],
    });
    expect(
      planUnsupportedReason({ steps: [{ kind: "parallel", id: "p", branches: [branch(undefined)] }] }),
    ).toMatch(/branch of plan step "p" has no id/);
    expect(
      planUnsupportedReason({ steps: [{ kind: "parallel", id: "p", branches: [branch("b"), branch("b")] }] }),
    ).toMatch(/duplicate branch id "b"/);
  });

  it("rejects unknown top-level plan keys", () => {
    const plan = { ...v1Plan(), edges: [] };
    expect(planUnsupportedReason(plan)).toMatch(/plan carries key\(s\) this worker does not implement: edges/);
  });

  it("accepts a valid multi-gate lifecycle and rejects malformed gates (#55 slice 4)", () => {
    const gateNode = { id: "g", afterStep: "a", userDecisions: {}, invalidUserDecision: "warn" as const };
    const lifecycle = { progress: true, cancellation: true, statusEventLimit: 0 };
    // A well-formed `gates` array is now IMPLEMENTED semantics — accepted.
    expect(
      planUnsupportedReason({ steps: v1Plan().steps, lifecycle: { ...lifecycle, gates: [gateNode] } }),
    ).toBeUndefined();
    // An empty array, a non-object entry, a missing id, and unknown gate keys are all rejected.
    expect(planUnsupportedReason({ steps: v1Plan().steps, lifecycle: { ...lifecycle, gates: [] } })).toMatch(
      /lifecycle gates is not a non-empty array/,
    );
    expect(
      planUnsupportedReason({ steps: v1Plan().steps, lifecycle: { ...lifecycle, gates: [{ ...gateNode, id: 7 }] } }),
    ).toMatch(/gate is missing its string id/);
    expect(
      planUnsupportedReason({ steps: v1Plan().steps, lifecycle: { ...lifecycle, gates: [{ ...gateNode, futureKnob: 1 }] } }),
    ).toMatch(/gate carries key\(s\) this worker does not implement: futureKnob/);
  });

  it("rejects gates missing REQUIRED interpreted fields (silent-skip / TypeError hazards)", () => {
    const lifecycle = { progress: true, cancellation: true, statusEventLimit: 0 };
    // Missing afterStep: the gate would silently never fire.
    expect(
      planUnsupportedReason({
        steps: v1Plan().steps,
        lifecycle: { ...lifecycle, gates: [{ id: "g", userDecisions: {}, invalidUserDecision: "warn" }] },
      }),
    ).toMatch(/gate is missing its string afterStep/);
    // Missing userDecisions: submitReview would TypeError into an undiagnostic retry hang.
    expect(
      planUnsupportedReason({
        steps: v1Plan().steps,
        lifecycle: { ...lifecycle, gates: [{ id: "g", afterStep: "a", invalidUserDecision: "warn" }] },
      }),
    ).toMatch(/gate is missing its userDecisions object/);
    // An invalidUserDecision this interpreter doesn't implement is new semantics.
    expect(
      planUnsupportedReason({
        steps: v1Plan().steps,
        lifecycle: { ...lifecycle, gates: [{ id: "g", afterStep: "a", userDecisions: {}, invalidUserDecision: "explode" }] },
      }),
    ).toMatch(/invalidUserDecision this worker does not implement: "explode"/);
    // The same required checks now protect the single review node (shared helper).
    expect(
      planUnsupportedReason({
        steps: v1Plan().steps,
        lifecycle: { ...lifecycle, review: { userDecisions: {}, invalidUserDecision: "warn" } },
      }),
    ).toMatch(/review gate is missing its string afterStep/);
  });

  it("rejects unknown review-gate keys — a future single-gate field is new semantics", () => {
    const review = {
      steps: v1Plan().steps,
      lifecycle: {
        progress: true,
        cancellation: true,
        statusEventLimit: 0,
        review: { afterStep: "a", userDecisions: {}, invalidUserDecision: "warn", surprise: "x" },
      },
    };
    expect(planUnsupportedReason(review)).toMatch(/review gate carries key\(s\) this worker does not implement: surprise/);
  });
});

describe("typefluxYamlWorkflow plan-shape guard", () => {
  // The guard is the FIRST statement of the workflow, before any Temporal API call, so
  // these run under plain vitest — exactly what an old worker does with a new plan.
  const expectUnsupported = async (plan: unknown): Promise<ApplicationFailure> => {
    let caught: unknown;
    await typefluxYamlWorkflow(plan as WorkflowPlan, { x: 1 }).then(
      () => {
        throw new Error("expected TypefluxPlanUnsupported");
      },
      (error: unknown) => {
        caught = error;
      },
    );
    expect(caught).toBeInstanceOf(ApplicationFailure);
    const failure = caught as ApplicationFailure;
    expect(failure.type).toBe("TypefluxPlanUnsupported");
    expect(failure.nonRetryable).toBe(true);
    return failure;
  };

  it("fails loud on `steps: []` instead of no-op-completing with the input", async () => {
    const failure = await expectUnsupported({ steps: [] });
    expect(failure.message).toMatch(/has no steps/);
    expect(failure.message).toMatch(/deploy/);
  });

  it("fails loud on absent `steps` instead of TypeErroring into a retry hang", async () => {
    await expectUnsupported({});
  });

  it("fails loud on an unknown step kind, naming the offender", async () => {
    const failure = await expectUnsupported({
      steps: [{ kind: "subworkflow", id: "child", plan: {} }],
    });
    expect(failure.message).toMatch(/"subworkflow"/);
    expect(failure.message).toMatch(/upgrade and deploy/);
  });
});
