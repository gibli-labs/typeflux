// First-class YAML compensation (#299 SLICE 1): spec parsing, plan building + load-time
// rejections, the old-worker plan-shape guard, the lifecycle compensation events + status wire,
// and digest present-only. The end-to-end LIFO unwind is covered by live-compensation.test.ts.

import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

import { assembleYamlRuntime } from "../src/runtime.js";
import { loadYamlSpec } from "../src/loader.js";
import { workflowCompensationError, workflowPlanFromSpec } from "../src/build-workflow.js";
import { workflowPlanDigest } from "../src/frozen-version.js";
import { LifecycleRuntime } from "../src/lifecycle.js";
import { planUnsupportedReason, type LifecyclePlan, type WorkflowPlan } from "../src/workflow-plan.js";

class StaticProvider implements ModelProvider {
  structuredCall(_params: StructuredCallParams): unknown {
    return { v: "ok" };
  }
}

const SPEC = (steps: string, defs = ""): string => `
project: p
name: saga
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: x } }
  provider: { type: openai }
activities:
  definitions:
    - { name: book, input: schemas:A, output: schemas:B, prompt: p/x }
    - { name: undo_book, input: schemas:B, output: schemas:B, prompt: p/x }
    - { name: charge, input: schemas:B, output: schemas:C, prompt: p/x }
    - { name: undo_wrong, input: schemas:C, output: schemas:C, prompt: p/x }${defs}
workflow:
  name: SagaWorkflow
  input: schemas:A
  output: schemas:C
  steps:
${steps}
`;

describe("compensation spec + plan (#299)", () => {
  it("carries the compensate node on an activity step (activity, input_from, retry)", () => {
    const plan = workflowPlanFromSpec(
      loadYamlSpec(
        SPEC(
          `    - id: book
      activity: book
      compensate: { activity: undo_book, input_from: book, retry: { maximum_attempts: 2, initial_interval_seconds: 3, maximum_interval_seconds: 9, backoff_coefficient: 2 } }
    - id: charge
      activity: charge`,
        ),
      ),
    );
    const book = plan.steps[0] as { compensate?: unknown };
    expect(book.compensate).toEqual({
      activity: "undo_book",
      inputFrom: "book",
      retry: { maximumAttempts: 2, initialIntervalMs: 3000, maximumIntervalMs: 9000, backoffCoefficient: 2 },
    });
  });

  it("defaults compensate.inputFrom to the step's own output (omitted from the node)", () => {
    const plan = workflowPlanFromSpec(
      loadYamlSpec(
        SPEC(`    - id: book
      activity: book
      compensate: { activity: undo_book }
    - id: charge
      activity: charge`),
      ),
    );
    expect(plan.steps[0]).toMatchObject({ kind: "activity", compensate: { activity: "undo_book" } });
    expect((plan.steps[0] as { compensate: { inputFrom?: string } }).compensate.inputFrom).toBeUndefined();
  });

  it("rejects compensate on a parallel step and on a map.workflow step", () => {
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(
          SPEC(`    - id: par
      compensate: { activity: undo_book }
      parallel:
        branches:
          - id: b1
            steps:
              - { id: s1, activity: book }
        collect: { output: schemas:C }`),
        ),
      ),
    ).toThrow(/parallel block and cannot carry .compensate/);
  });

  it("load-rejects an input_from whose root is not available at the step", () => {
    expect(() =>
      workflowPlanFromSpec(
        loadYamlSpec(`
project: p
name: s
task_queue: q
runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: x } }, provider: { type: openai } }
activities:
  definitions:
    - { name: book, input: schemas:A, output: schemas:B, prompt: p/x }
    - { name: undo_book, input: schemas:B, output: schemas:B, prompt: p/x }
workflow:
  name: W
  input: schemas:A
  output: schemas:B
  steps:
    - id: book
      activity: book
      compensate: { activity: undo_book, input_from: nope }
`),
      ),
    ).toThrow(/is not available there/);
  });

  it("load-rejects a compensating activity whose input type does not accept the referenced value", () => {
    // undo_wrong expects schemas:C, but book's own output is schemas:B.
    const err = workflowCompensationError(
      loadYamlSpec(
        SPEC(`    - id: book
      activity: book
      compensate: { activity: undo_wrong }
    - id: charge
      activity: charge`),
      ),
    );
    expect(err).toMatch(/compensate runs activity "undo_wrong" expecting input "schemas:C"/);
  });

  it("rejects a review route that skips the step a compensate.input_from needs (#299 MUST-FIX 3)", () => {
    // Route A -> C skips B, so C's compensate.input_from: B cannot resolve on the routed path.
    const yaml = `
project: p
name: route
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: x } }
  provider: { type: openai }
activities:
  definitions:
    - { name: a_step, input: schemas:A, output: schemas:A, prompt: p/x }
    - { name: b_step, input: schemas:A, output: schemas:A, prompt: p/x }
    - { name: c_step, input: schemas:A, output: schemas:A, prompt: p/x }
    - { name: cancel_c, input: schemas:A, output: schemas:A, prompt: p/x }
workflow:
  name: RouteWorkflow
  input: schemas:A
  output: schemas:A
  lifecycle:
    enabled: true
    review:
      after_step: A
      user_decisions:
        approve: { route: C }
  steps:
    - { id: A, activity: a_step }
    - { id: B, activity: b_step }
    - id: C
      activity: c_step
      compensate: { activity: cancel_c, input_from: B }
`;
    expect(() => workflowPlanFromSpec(loadYamlSpec(yaml))).toThrow(
      /routes past step "B", but step "C" compensate\.input_from reads "B"/,
    );
  });

  it("rejects a typo'd compensate.activity at assembly, not mid-unwind (#299 MUST-FIX 4)", () => {
    const spec = loadYamlSpec(
      SPEC(`    - id: book
      activity: book
      compensate: { activity: nonexistent_undo }
    - id: charge
      activity: charge`),
    );
    // Plan build alone does NOT reject (TS defers declared-activity to worker assembly), but
    // assembling the runtime must reject the unknown compensating activity loud.
    expect(() =>
      assembleYamlRuntime(spec, {
        provider: new StaticProvider(),
        schemas: {
          "schemas:A": z.object({ v: z.string() }),
          "schemas:B": z.object({ v: z.string() }),
          "schemas:C": z.object({ v: z.string() }),
        },
      }),
    ).toThrow(/compensate references activity "nonexistent_undo" which is not among/);
  });

  it("rejects a map whose compensate.input_from is its OWN id (#299 verify edge 2)", () => {
    const yaml = `
project: p
name: mapself
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: x } }
  provider: { type: openai }
activities:
  definitions:
    - { name: review_item, input: schemas:Item, output: schemas:R, prompt: p/x }
    - { name: cancel_item, input: schemas:R, output: schemas:R, prompt: p/x }
workflow:
  name: MapSelf
  input: schemas:Batch
  output: schemas:RBatch
  steps:
    - id: review
      map: { activity: review_item, over: input.items, collect: { output: schemas:RBatch, field: results } }
      compensate: { activity: cancel_item, input_from: review }
`;
    expect(() => workflowPlanFromSpec(loadYamlSpec(yaml))).toThrow(/its own collected output/);
  });

  it("rejects a branch compensate.input_from that reads the enclosing parallel collect (#299 verify edge 3)", () => {
    const yaml = `
project: p
name: brc
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: x } }
  provider: { type: openai }
activities:
  definitions:
    - { name: a_step, input: schemas:A, output: schemas:A, prompt: p/x }
    - { name: cancel_a, input: schemas:A, output: schemas:A, prompt: p/x }
workflow:
  name: BranchReadsCollect
  input: schemas:A
  output: schemas:Fan
  steps:
    - id: fan
      parallel:
        branches:
          - id: a
            steps:
              - { id: sa, activity: a_step, compensate: { activity: cancel_a, input_from: fan } }
        collect: { output: schemas:Fan }
`;
    expect(() => workflowPlanFromSpec(loadYamlSpec(yaml))).toThrow(/"fan" is not available/);
  });

  it("accepts a top-level compensate.input_from that reads a guaranteed post-parallel branch step (#299 verify edge 3)", () => {
    const yaml = `
project: p
name: tlg
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/x: x } }
  provider: { type: openai }
activities:
  definitions:
    - { name: a_step, input: schemas:A, output: schemas:A, prompt: p/x }
    - { name: after_step, input: schemas:Fan, output: schemas:A, prompt: p/x }
    - { name: cancel_after, input: schemas:A, output: schemas:A, prompt: p/x }
workflow:
  name: TopLevelGuaranteed
  input: schemas:A
  output: schemas:A
  steps:
    - id: fan
      parallel:
        branches:
          - id: a
            steps:
              - { id: sa, activity: a_step }
        collect: { output: schemas:Fan }
    - id: after
      activity: after_step
      compensate: { activity: cancel_after, input_from: sa }
`;
    // `sa` is an ungated branch step (guaranteed after the block), so referencing it is valid.
    expect(() => workflowPlanFromSpec(loadYamlSpec(yaml))).not.toThrow();
  });
});

describe("old-worker plan-shape guard for compensate (#299)", () => {
  const base = (compensate: unknown): WorkflowPlan => ({
    steps: [{ kind: "activity", id: "s1", activity: "a", ...(compensate !== undefined ? { compensate } : {}) } as never],
  });

  it("accepts a well-formed compensate node", () => {
    expect(planUnsupportedReason(base({ activity: "undo", inputFrom: "s1" }))).toBeUndefined();
  });

  it("rejects a compensate node with an unknown key", () => {
    expect(planUnsupportedReason(base({ activity: "undo", bogus: 1 }))).toMatch(/compensate config on step "s1" carries key/);
  });

  it("rejects a compensate node missing its activity", () => {
    expect(planUnsupportedReason(base({ inputFrom: "s1" }))).toMatch(/missing its string activity/);
  });

  it("rejects compensate carried on a parallel step (an old-worker unknown key)", () => {
    const plan: WorkflowPlan = {
      steps: [
        {
          kind: "parallel",
          id: "par",
          branches: [{ id: "b1", steps: [{ kind: "activity", id: "s1", activity: "a" }] }],
          // @ts-expect-error compensate is not valid on a parallel node
          compensate: { activity: "undo" },
        },
      ],
    };
    expect(planUnsupportedReason(plan)).toMatch(/carries key\(s\) this worker does not implement: compensate/);
  });
});

describe("lifecycle compensation events + status wire (#299)", () => {
  const plan: LifecyclePlan = { progress: true, cancellation: true, statusEventLimit: 50 };

  it("records ordered compensation events with the ORIGINAL step id and a terminal compensation_status", () => {
    const lc = new LifecycleRuntime(plan, () => null);
    lc.started();
    lc.compensationStarted("book_flight");
    lc.compensationCompleted("book_flight");
    lc.compensationStarted("book_hotel");
    lc.compensationCompleted("book_hotel");
    lc.failed("complete");
    const status = lc.status();
    expect(status.compensation_status).toBe("complete");
    expect(status.events.filter((e) => e.event.startsWith("compensation_")).map((e) => [e.event, e.step_id])).toEqual([
      ["compensation_started", "book_flight"],
      ["compensation_completed", "book_flight"],
      ["compensation_started", "book_hotel"],
      ["compensation_completed", "book_hotel"],
    ]);
    // The terminal event carries compensation_status; pre-terminal events omit it (present-only).
    const terminal = status.events.find((e) => e.event === "workflow_failed");
    expect(terminal?.compensation_status).toBe("complete");
    expect(status.events.find((e) => e.event === "workflow_started")?.compensation_status).toBeUndefined();
  });

  it("cancelled() carries compensation_status too (D299-2a); a run with no unwind stays null", () => {
    const cancelled = new LifecycleRuntime(plan, () => null);
    cancelled.started();
    cancelled.cancelled("partial");
    expect(cancelled.status().compensation_status).toBe("partial");

    const clean = new LifecycleRuntime(plan, () => null);
    clean.started();
    clean.completed();
    expect(clean.status().compensation_status).toBeNull();
  });
});

describe("compensation digest present-only (#299)", () => {
  it("adds the compensate key only when present; a compensation-free plan is unchanged", () => {
    const withoutYaml = SPEC(`    - id: book
      activity: book
    - id: charge
      activity: charge`);
    const withYaml = SPEC(`    - id: book
      activity: book
      compensate: { activity: undo_book }
    - id: charge
      activity: charge`);
    const without = workflowPlanFromSpec(loadYamlSpec(withoutYaml));
    const withComp = workflowPlanFromSpec(loadYamlSpec(withYaml));
    expect(workflowPlanDigest(without)).not.toBe(workflowPlanDigest(withComp));
    // The non-compensated step is byte-identical across the two plans.
    expect(JSON.stringify(without.steps[1])).toBe(JSON.stringify(withComp.steps[1]));
    // The compensate key is present ONLY on the compensated step.
    expect("compensate" in (without.steps[0] as object)).toBe(false);
    expect("compensate" in (withComp.steps[0] as object)).toBe(true);
  });
});
