/**
 * A composed workflow that references pure-code activities (#746) injected via
 * `extraActivities`: a `parallel` block with a real (AI) branch and a zero-cost CODE
 * passthrough branch that echoes sibling context (`blob_index`) into the collect, and a
 * terminal CODE "refiner" step that consumes the merged collect. This is the on-graph
 * shape the issue's adopter Refiner needs — a deterministic derivation as a wired
 * workflow step, not an off-graph caller step. Asserts the plan shape, that the runtime
 * assembles both authoring modes, and that the code activities run WITHOUT any provider call.
 */

import { describe, expect, it } from "vitest";
import { z } from "zod";

import {
  defineActivity,
  defineCodeActivity,
  type ModelProvider,
  type StructuredCallParams,
} from "@typeflux/temporal";

import {
  assembleYamlRuntime,
  composeProjectPolicies,
  loadPolicySpec,
  loadYamlSpec,
  workflowPlanFromSpec,
} from "../src/index.js";

const Blob = z.object({ blob_index: z.number(), text: z.string() });
const Assessment = z.object({ verdict: z.string() });
const IndexEcho = z.object({ blob_index: z.number() });
const Gathered = z.object({
  assess_branch: Assessment.optional(),
  passthrough_branch: IndexEcho.optional(),
});
const ReviewSurface = z.object({ blob_index: z.number(), verdict: z.string() });

const schemas = {
  "schemas:Blob": Blob,
  "schemas:Assessment": Assessment,
  "schemas:IndexEcho": IndexEcho,
  "schemas:Gathered": Gathered,
  "schemas:ReviewSurface": ReviewSurface,
};

/** Echoes the blob index (sibling context) — a zero-cost code passthrough branch. */
const echoIndex = defineCodeActivity({
  name: "echo_index",
  input: Blob,
  output: IndexEcho,
  handler: (input) => ({ blob_index: input.blob_index }),
});

/** The terminal Refiner: assembles the review surface from the merged collect, deterministically. */
const refineSurface = defineCodeActivity({
  name: "refine_surface",
  input: Gathered,
  output: ReviewSurface,
  handler: (input) => ({
    blob_index: input.passthrough_branch?.blob_index ?? -1,
    verdict: input.assess_branch?.verdict ?? "unknown",
  }),
});

const extraActivities = { echo_index: echoIndex, refine_surface: refineSurface };

const YAML = `
project: refiner_demo
name: review_blob
task_queue: refiner-demo-ts
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      assess-prompt: "Assess {{text}}"
  provider: { type: openai }
activities:
  definitions:
    - name: assess
      input: schemas:Blob
      output: schemas:Assessment
      prompt: assess-prompt
workflow:
  name: ReviewBlob
  input: schemas:Blob
  output: schemas:ReviewSurface
  steps:
    - id: gather
      parallel:
        branches:
          - id: assess_branch
            steps:
              - id: assess
                activity: assess
          - id: passthrough_branch
            steps:
              - id: echo_index
                activity: echo_index
        collect:
          output: schemas:Gathered
    - id: refine_surface
      activity: refine_surface
`;

/** Fails if the provider is ever called — the code branches must not dispatch. */
class ThrowingProvider implements ModelProvider {
  calls = 0;
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    throw new Error("a code activity must never call the provider");
  }
}

const spec = () => loadYamlSpec(YAML, { sourceLabel: "test/code-activity-composition.yaml" });

describe("code activity in a composed workflow (#746)", () => {
  it("derives the plan: a code step behaves identically to an AI step in workflowPlanFromSpec", () => {
    const plan = workflowPlanFromSpec(spec());

    const gather = plan.steps.find((step) => step.id === "gather");
    expect(gather?.kind).toBe("parallel");
    const branchIds = gather?.kind === "parallel" ? gather.branches.map((branch) => branch.id) : [];
    expect(branchIds).toEqual(["assess_branch", "passthrough_branch"]);

    // The code passthrough branch step is an ordinary activity step referencing the code activity.
    const passthrough =
      gather?.kind === "parallel"
        ? gather.branches.find((branch) => branch.id === "passthrough_branch")?.steps[0]
        : undefined;
    expect(passthrough).toMatchObject({ kind: "activity", activity: "echo_index" });

    // The terminal Refiner is a plain activity step referencing the injected code activity.
    const refine = plan.steps.find((step) => step.id === "refine_surface");
    expect(refine).toMatchObject({ kind: "activity", activity: "refine_surface" });
  });

  it("assembles both authoring modes and runs the code activities with no provider call", async () => {
    const provider = new ThrowingProvider();
    const { activities } = assembleYamlRuntime(spec(), { provider, schemas, extraActivities });

    // assess is pure-YAML (AI); echo_index / refine_surface are code-injected.
    for (const name of ["assess", "echo_index", "refine_surface"]) {
      expect(activities[name]).toBeDefined();
    }

    // The passthrough echoes sibling context; the Refiner merges the collect — both without the provider.
    const echoed = (await activities["echo_index"]!({ blob_index: 7, text: "hi" })) as { blob_index: number };
    expect(echoed).toEqual({ blob_index: 7 });

    const surface = (await activities["refine_surface"]!({
      assess_branch: { verdict: "approve" },
      passthrough_branch: { blob_index: 7 },
    })) as { blob_index: number; verdict: string };
    expect(surface).toEqual({ blob_index: 7, verdict: "approve" });

    expect(provider.calls).toBe(0);
  });

  it("a code activity name colliding with a spec definition throws at assembly", () => {
    const collide = {
      assess: defineCodeActivity({
        name: "assess",
        input: Blob,
        output: Assessment,
        handler: () => ({ verdict: "x" }),
      }),
    };
    expect(() =>
      assembleYamlRuntime(spec(), { provider: new ThrowingProvider(), schemas, extraActivities: collide }),
    ).toThrowError(/duplicate activity name/);
  });
});

describe("code activities under a required-moderation policy (#746 governance)", () => {
  // A spec with NO AI definitions: the only step references an injected activity, so the
  // moderation posture rides entirely on the extraActivities pre-flight in assembleYamlRuntime.
  const CODE_ONLY_YAML = `
project: refiner_demo
name: review_code_only
task_queue: refiner-demo-ts
runtime:
  temporal: {}
  registry:
    type: inline
    prompts: {}
  provider: { type: openai }
activities:
  definitions: []
workflow:
  name: ReviewCodeOnly
  input: schemas:Gathered
  output: schemas:ReviewSurface
  steps:
    - id: refine_surface
      activity: refine_surface
`;
  const codeOnlySpec = () => loadYamlSpec(CODE_ONLY_YAML, { sourceLabel: "test/code-only.yaml" });
  const requireModeration = () =>
    composeProjectPolicies([{ id: "org", spec: loadPolicySpec("name: org\nsemantics: { required: true }") }], ["org"]);

  it("a required-moderation policy admits a code activity (no model output to moderate)", () => {
    // Structurally, a code descriptor CANNOT declare moderation (defineCodeActivity rejects the
    // option) — the requirement governs LLM semantics, so the pre-flight exempts kind "code".
    const { activities } = assembleYamlRuntime(codeOnlySpec(), {
      provider: new ThrowingProvider(),
      schemas,
      extraActivities: { refine_surface: refineSurface },
      policy: requireModeration(),
    });
    expect(activities["refine_surface"]).toBeDefined();
  });

  it("the same policy still rejects an injected AI descriptor WITHOUT moderation (behavior pinned)", () => {
    const ai = defineActivity({
      name: "refine_surface",
      prompt: { name: "assess-prompt" },
      input: Gathered,
      output: ReviewSurface,
    });
    expect(() =>
      assembleYamlRuntime(codeOnlySpec(), {
        provider: new ThrowingProvider(),
        schemas,
        extraActivities: { refine_surface: ai },
        policy: requireModeration(),
      }),
    ).toThrowError(/must declare moderation/);
  });
});
