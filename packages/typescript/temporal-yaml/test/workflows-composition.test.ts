// Interpreter semantics of parallel blocks + when gating (#55 slice 1), run outside
// the Temporal sandbox by mocking @temporalio/workflow's context-bound APIs: activities
// resolve from a test registry, signal/query handlers are captured, and the pure
// lifecycle runtime records events exactly as it would in the sandbox. Live dev-server
// coverage (replay, CancellationScope) rides live-yaml-workflow.test.ts.

import { describe, expect, it, vi } from "vitest";

import { loadYamlSpec } from "../src/loader.js";
import { workflowPlanFromSpec } from "../src/build-workflow.js";
import type { WorkflowLifecycleStatus } from "../src/lifecycle.js";

const harness = vi.hoisted(() => ({
  activities: {} as Record<string, (input: unknown, cachedSession?: unknown) => Promise<unknown>>,
  handlers: new Map<string, (...args: unknown[]) => unknown>(),
}));

vi.mock("@temporalio/workflow", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@temporalio/workflow")>();
  return {
    ...actual,
    proxyActivities: () =>
      new Proxy(
        {},
        {
          get: (_target, name: string) =>
            harness.activities[name] ??
            (() => {
              throw new Error(`unregistered test activity ${String(name)}`);
            }),
        },
      ),
    setHandler: (definition: { name: string }, handler: (...args: unknown[]) => unknown) => {
      harness.handlers.set(definition.name, handler);
    },
    // The tests keep cancellation/review off, so `condition` is never awaited; fail loud
    // if a code path unexpectedly reaches it.
    condition: () => {
      throw new Error("condition() is not expected outside the sandbox in these tests");
    },
  };
});

import { typefluxYamlWorkflow } from "../src/workflows.js";
import type { WorkflowPlan } from "../src/workflow-plan.js";

/** The composition spec under test (the design's §3.1 shape + an early-exit tail). */
const yamlSpec = (options?: { lifecycle?: boolean; maxBytes?: number; earlyExit?: boolean; midBranchGate?: boolean }) => `
project: p
name: disclosure_review
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - { name: classify_disclosure, input: "schemas:Disclosure", output: "schemas:Classification", prompt: p/c }
    - { name: legal_screen, input: "schemas:Classification", output: "schemas:LegalScreen", prompt: p/l1 }
    - { name: legal_assess, input: "schemas:LegalScreen", output: "schemas:LegalScreen", prompt: p/l2 }
    - { name: medical_review, input: "schemas:Classification", output: "schemas:MedicalReview", prompt: p/m }
    - { name: consolidate_reviews, input: "schemas:ReviewBundle", output: "schemas:ReviewOutcome", prompt: p/x }
    - { name: deep_analysis, input: "schemas:ReviewOutcome", output: "schemas:ReviewOutcome", prompt: p/d }
workflow:
  name: DisclosureReviewWorkflow
  input: schemas:Disclosure
  output: schemas:ReviewOutcome
${options?.lifecycle === true ? "  lifecycle: { enabled: true, cancellation: false }\n" : ""}
  steps:
    - id: classify
      activity: classify_disclosure
    - id: reviews
      parallel:
        branches:
          - id: legal
            when: { path: classify.needs_legal, eq: true }
            steps:
              - id: legal_screen_step
                activity: legal_screen
              - id: legal_assess_step
${options?.midBranchGate === true ? "                when: { path: legal_screen_step.escalate, eq: true }\n" : ""}
                activity: legal_assess
          - id: medical
            steps:
              - id: medical_review_step
                activity: medical_review
        collect:
          output: schemas:ReviewBundle
          max_bytes: ${options?.maxBytes ?? 1_500_000}
    - id: consolidate
      activity: consolidate_reviews
${
  options?.earlyExit === true
    ? "    - id: deep_analysis_step\n      when: { path: classify.severity, gte: 3 }\n      activity: deep_analysis\n"
    : ""
}
`;

const planOf = (yaml: string): WorkflowPlan => workflowPlanFromSpec(loadYamlSpec(yaml));

/** Register the standard fake activities; each records its call input. */
function registerActivities(overrides: Partial<typeof harness.activities> = {}): Record<string, unknown[]> {
  const calls: Record<string, unknown[]> = {};
  const fake =
    (name: string, result: (input: unknown) => unknown) =>
    async (input: unknown): Promise<unknown> => {
      (calls[name] ??= []).push(input);
      return result(input);
    };
  harness.activities = {
    classify_disclosure: fake("classify_disclosure", () => ({ needs_legal: true, severity: 1, route: "x" })),
    legal_screen: fake("legal_screen", () => ({ escalate: false, screen: "legal-screened" })),
    legal_assess: fake("legal_assess", () => ({ assessment: "legal-assessed" })),
    medical_review: fake("medical_review", () => ({ review: "medical-reviewed" })),
    consolidate_reviews: fake("consolidate_reviews", (input) => ({ outcome: "done", from: input })),
    deep_analysis: fake("deep_analysis", () => ({ outcome: "deep" })),
    ...overrides,
  } as typeof harness.activities;
  return calls;
}

const lifecycleStatus = (): WorkflowLifecycleStatus =>
  (harness.handlers.get("typeflux_lifecycle_status") as () => WorkflowLifecycleStatus)();

describe("parallel execution (#55 §5.1)", () => {
  it("runs gated-in branches concurrently and collects by branch id in declared order", async () => {
    const calls = registerActivities();
    const result = (await typefluxYamlWorkflow(planOf(yamlSpec()), { doc: 1 })) as { from: unknown };
    // consolidate consumed the collect object: fields ARE the branch ids, declared order.
    expect(result).toEqual({
      outcome: "done",
      from: {
        legal: { assessment: "legal-assessed" },
        medical: { review: "medical-reviewed" },
      },
    });
    expect(Object.keys((result as { from: Record<string, unknown> }).from)).toEqual(["legal", "medical"]);
    // Branch first steps consumed the BLOCK input (classify's output).
    expect(calls["legal_screen"]).toEqual([{ needs_legal: true, severity: 1, route: "x" }]);
    expect(calls["medical_review"]).toEqual([{ needs_legal: true, severity: 1, route: "x" }]);
    // Chained branch step consumed its predecessor.
    expect(calls["legal_assess"]).toEqual([{ escalate: false, screen: "legal-screened" }]);
  });

  it("is order-independent: a slow first branch changes nothing in the collect object", async () => {
    registerActivities({
      legal_screen: async (input: unknown) => {
        await new Promise((resolve) => setTimeout(resolve, 20));
        return { escalate: false, screen: "legal-screened" };
      },
    });
    const slow = (await typefluxYamlWorkflow(planOf(yamlSpec()), { doc: 1 })) as { from: Record<string, unknown> };
    registerActivities();
    const fast = (await typefluxYamlWorkflow(planOf(yamlSpec()), { doc: 1 })) as { from: Record<string, unknown> };
    expect(slow).toEqual(fast);
    expect(Object.keys(slow.from)).toEqual(Object.keys(fast.from));
  });

  it("a gated-out branch contributes null and records step_skipped with the rendered condition", async () => {
    registerActivities({
      classify_disclosure: async () => ({ needs_legal: false, severity: 1, route: "x" }),
    });
    const result = (await typefluxYamlWorkflow(planOf(yamlSpec({ lifecycle: true })), { doc: 1 })) as {
      from: Record<string, unknown>;
    };
    expect(result.from).toEqual({ legal: null, medical: { review: "medical-reviewed" } });
    const status = lifecycleStatus();
    const skipped = status.events.filter((event) => event.event === "step_skipped");
    expect(skipped).toHaveLength(1);
    expect(skipped[0]).toMatchObject({
      step_id: "legal",
      condition: "classify.needs_legal == true",
    });
    // The legal branch never ran.
    const started = status.events.filter((event) => event.event === "step_started").map((event) => event.step_id);
    expect(started).not.toContain("legal_screen_step");
  });

  it("if/else: two mutually exclusive gates run exactly one branch (both directions)", async () => {
    const IFELSE = `
project: p
name: routing
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
activities:
  definitions:
    - { name: classify, input: "schemas:In", output: "schemas:Classified", prompt: p/c }
    - { name: auto_approve, input: "schemas:Classified", output: "schemas:Decision", prompt: p/a }
    - { name: manual_review, input: "schemas:Classified", output: "schemas:Decision", prompt: p/m }
workflow:
  name: RoutingWorkflow
  input: schemas:In
  steps:
    - id: classify_step
      activity: classify
    - id: routing
      parallel:
        branches:
          - id: fast_track
            when: { path: classify_step.risk, lt: 0.3 }
            steps: [{ id: auto_approve_step, activity: auto_approve }]
          - id: full_review
            when: { path: classify_step.risk, gte: 0.3 }
            steps: [{ id: manual_review_step, activity: manual_review }]
        collect: { output: schemas:RoutingResult }
`;
    for (const [risk, ran, skipped] of [
      [0.1, "fast_track", "full_review"],
      [0.9, "full_review", "fast_track"],
    ] as const) {
      registerActivities({
        classify: async () => ({ risk }),
        auto_approve: async () => ({ decision: "auto" }),
        manual_review: async () => ({ decision: "manual" }),
      });
      const result = (await typefluxYamlWorkflow(planOf(IFELSE), {})) as Record<string, unknown>;
      expect(result[skipped]).toBeNull();
      expect(result[ran]).toEqual({ decision: risk < 0.3 ? "auto" : "manual" });
    }
  });

  it("early exit: a false top-level gate completes the workflow with the running value", async () => {
    registerActivities();
    const result = await typefluxYamlWorkflow(planOf(yamlSpec({ lifecycle: true, earlyExit: true })), { doc: 1 });
    // severity 1 < 3: deep_analysis is skipped; the workflow returns consolidate's output.
    expect(result).toMatchObject({ outcome: "done" });
    const status = lifecycleStatus();
    expect(status.state).toBe("completed");
    const skipped = status.events.filter((event) => event.event === "step_skipped");
    expect(skipped).toHaveLength(1);
    expect(skipped[0]).toMatchObject({ step_id: "deep_analysis_step", condition: "classify.severity >= 3" });
    // The skipped step's unit was released: progress completes at 100%.
    expect(status.completed_units).toBe(status.total_units);
  });

  it("early exit gate passing runs the tail (the if/else pair's other pattern)", async () => {
    registerActivities({
      classify_disclosure: async () => ({ needs_legal: true, severity: 5, route: "x" }),
    });
    const result = await typefluxYamlWorkflow(planOf(yamlSpec({ earlyExit: true })), { doc: 1 });
    expect(result).toEqual({ outcome: "deep" });
  });

  it("mid-branch gate skips the remainder; the branch contributes the running value", async () => {
    registerActivities(); // legal_screen returns escalate: false -> legal_assess_step gated out
    const result = (await typefluxYamlWorkflow(planOf(yamlSpec({ lifecycle: true, midBranchGate: true })), {
      doc: 1,
    })) as { from: Record<string, unknown> };
    expect(result.from["legal"]).toEqual({ escalate: false, screen: "legal-screened" });
    const skipped = lifecycleStatus().events.filter((event) => event.event === "step_skipped");
    expect(skipped[0]).toMatchObject({
      step_id: "legal_assess_step",
      condition: "legal_screen_step.escalate == true",
    });
  });

  it("enforces collect.max_bytes on the merged payload", async () => {
    registerActivities({
      medical_review: async () => ({ review: "x".repeat(512) }),
    });
    await expect(typefluxYamlWorkflow(planOf(yamlSpec({ maxBytes: 128 })), { doc: 1 })).rejects.toMatchObject({
      type: "TypefluxParallelCollectPayloadTooLarge",
      nonRetryable: true,
    });
  });

  it("a failing branch fails the workflow (map-runner discipline)", async () => {
    registerActivities({
      medical_review: async () => {
        throw new Error("medical backend down");
      },
    });
    await expect(typefluxYamlWorkflow(planOf(yamlSpec()), { doc: 1 })).rejects.toThrow(/medical backend down/);
  });

  it("per-branch lifecycle event subsequences stay ordered (interleaving not pinned)", async () => {
    registerActivities({
      legal_screen: async () => {
        await new Promise((resolve) => setTimeout(resolve, 10));
        return { escalate: false, screen: "legal-screened" };
      },
    });
    await typefluxYamlWorkflow(planOf(yamlSpec({ lifecycle: true })), { doc: 1 });
    const started = lifecycleStatus()
      .events.filter((event) => event.event === "step_started")
      .map((event) => event.step_id);
    // The block itself, then branch steps in each branch's declared order (subsequence),
    // then the post-block step — regardless of cross-branch interleaving.
    expect(started[0]).toBe("classify");
    expect(started[1]).toBe("reviews");
    expect(started.indexOf("legal_screen_step")).toBeLessThan(started.indexOf("legal_assess_step"));
    expect(started.indexOf("legal_assess_step")).toBeLessThan(started.indexOf("consolidate"));
    expect(started.indexOf("medical_review_step")).toBeLessThan(started.indexOf("consolidate"));
    // Progress accounted every activity exactly once.
    const status = lifecycleStatus();
    expect(status.total_units).toBe(5);
    expect(status.completed_units).toBe(5);
  });

  it("all branches gated out yields an all-null collect object", async () => {
    registerActivities({
      classify_disclosure: async () => ({ needs_legal: false, severity: 1, route: "x" }),
    });
    const yaml = yamlSpec().replace(
      "          - id: medical\n            steps:\n              - id: medical_review_step\n                activity: medical_review\n",
      "          - id: medical\n            when: { path: classify.needs_legal, eq: true }\n            steps:\n              - id: medical_review_step\n                activity: medical_review\n",
    );
    const result = (await typefluxYamlWorkflow(planOf(yaml), { doc: 1 })) as { from: Record<string, unknown> };
    expect(result.from).toEqual({ legal: null, medical: null });
  });
});
