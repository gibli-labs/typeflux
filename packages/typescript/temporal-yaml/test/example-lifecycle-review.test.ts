/**
 * Executes the shipped lifecycle-review example on every CI run: the REAL
 * typeflux.yaml + schemas + hooks, assembled via `assembleYamlRuntime`. The
 * review-gate ORCHESTRATION (query/signal/routing) needs a Temporal server and
 * is covered by `main.ts` (run against a dev server), so this locks the derived
 * lifecycle plan shape + the activities + their injected hooks against rot.
 */

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { LifecycleDemoProvider } from "../examples/lifecycle-review/fakes.js";
import { hooks } from "../examples/lifecycle-review/hooks.js";
import { sampleCase } from "../examples/lifecycle-review/sample-case.js";
import { FinalDecision, ReviewPacket, RiskAssessment, schemas } from "../examples/lifecycle-review/schemas.js";
import { assembleYamlRuntime, loadYamlSpec, workflowPlanFromSpec } from "../src/index.js";

const EXAMPLE_YAML = new URL("../examples/lifecycle-review/typeflux.yaml", import.meta.url);

describe("lifecycle-review example (#455)", () => {
  const spec = () =>
    loadYamlSpec(readFileSync(EXAMPLE_YAML, "utf-8"), {
      sourceLabel: "examples/lifecycle-review/typeflux.yaml",
    });

  it("derives the review gate with its forward-only routes", () => {
    const plan = workflowPlanFromSpec(spec());
    expect(plan.lifecycle?.review).toMatchObject({
      afterStep: "package_for_review",
      invalidUserDecision: "warn",
      userDecisions: {
        prepare_submission: "prepare_submission",
        route_department: "route_to_department",
        send_email: "send_email",
      },
    });
    expect(plan.steps.map((step) => step.id)).toEqual([
      "assess_case",
      "package_for_review",
      "prepare_submission",
      "route_to_department",
      "send_email",
    ]);
  });

  it("runs the activities with their injected normalization hooks", async () => {
    const { activities } = assembleYamlRuntime(spec(), {
      provider: new LifecycleDemoProvider(),
      schemas,
      hooks,
    });

    const assessment = RiskAssessment.parse(await activities["assess_case"]!(sampleCase()));
    // The hook copied the case id and folded the input risk notes into flags.
    expect(assessment.case_id).toBe("CASE-2026-0101");
    expect(assessment.flags).toContain("prior chargeback on file");
    expect(assessment.flags).toContain("manual review");
    expect(assessment.flags).toEqual([...assessment.flags].sort());

    const packet = ReviewPacket.parse(await activities["package_for_review"]!(assessment));
    expect(packet.approval_required).toBe(true);

    // The send_email route step produces the FinalDecision (the workflow output).
    const decision = FinalDecision.parse(await activities["send_email"]!(packet));
    expect(decision.approved).toBe(true);
    expect(decision.decision).toBe("approved");
  });
});
