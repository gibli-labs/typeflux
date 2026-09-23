/**
 * Executes the shipped claims-review composition example (#55) on every CI run: the
 * REAL parent + pure-YAML children + code-injected `finalize`, assembled via
 * `workflowPlanFromSpec` / `assembleYamlRuntime`. The gate ORCHESTRATION (query/signal)
 * needs a Temporal server and is covered by `main.ts` against a dev server; this locks
 * the derived composition shape (parallel + when + sub-workflows + two gates) and the
 * activities (pure-YAML + injected) against rot.
 */

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { extraActivities } from "../examples/claims-review-composition/activities.js";
import { ClaimsReviewProvider } from "../examples/claims-review-composition/fakes.js";
import { schemas } from "../examples/claims-review-composition/schemas.js";
import {
  assembleYamlRuntime,
  LifecycleRuntime,
  loadYamlSpec,
  projectSubworkflowResolver,
  workflowPlanFromSpec,
} from "../src/index.js";

const read = (name: string): string =>
  readFileSync(new URL(`../examples/claims-review-composition/${name}`, import.meta.url), "utf-8");

const CHILDREN: Record<string, string> = {
  claim_triage: read("claim-triage.yaml"),
  escalation_review: read("escalation-review.yaml"),
};
const subworkflows = () =>
  projectSubworkflowResolver("claims_review", (id) => (CHILDREN[id] !== undefined ? loadYamlSpec(CHILDREN[id]) : undefined));

describe("claims-review composition example (#55)", () => {
  const spec = () => loadYamlSpec(read("typeflux.yaml"), { sourceLabel: "examples/claims-review-composition/typeflux.yaml" });

  it("derives the full composition shape: parallel if/else, sub-workflows, and two gates", () => {
    const plan = workflowPlanFromSpec(spec(), { subworkflows: subworkflows() });

    // Two named gates on distinct steps, each with routed decisions.
    expect(plan.lifecycle?.gates?.map((gate) => gate.id)).toEqual(["intake_gate", "compliance_gate"]);
    const intake = plan.lifecycle?.gates?.find((gate) => gate.id === "intake_gate");
    expect(intake?.afterStep).toBe("consolidate");
    expect(intake?.userDecisions).toMatchObject({ escalate: "escalation", expedite: "finalize" });
    expect(intake?.onTimeout).toBe("route");
    expect(intake?.timeoutRoute).toBe("finalize");

    // A parallel `screen` block with two `when`-gated branches (the if/else pair):
    // fast_track (a plain activity) and full_review (a map.workflow fan-out).
    const screen = plan.steps.find((step) => step.id === "screen");
    expect(screen?.kind).toBe("parallel");
    const branchIds = screen?.kind === "parallel" ? screen.branches.map((branch) => branch.id) : [];
    expect(branchIds).toEqual(["fast_track", "full_review"]);

    // A `workflow:` sub-workflow step for escalation.
    const escalation = plan.steps.find((step) => step.id === "escalation");
    expect(escalation?.kind).toBe("subworkflow");
  });

  it("assembles both authoring modes: pure-YAML activities + the injected finalize", async () => {
    const { activities } = assembleYamlRuntime(spec(), {
      provider: new ClaimsReviewProvider(),
      schemas,
      extraActivities,
      subworkflows: subworkflows(),
    });

    // acknowledge / consolidate are pure-YAML; finalize is code-injected via extraActivities.
    for (const name of ["acknowledge", "consolidate", "finalize"]) {
      expect(activities[name]).toBeDefined();
    }
    const consolidated = (await activities["consolidate"]!({ fast_track: null, full_review: { triaged: [] } })) as {
      escalate: boolean;
    };
    expect(consolidated.escalate).toBe(true);
    const packet = (await activities["finalize"]!({ summary: "s", escalate: true })) as { decision: string };
    expect(packet.decision).toBe("approved");
  });

  it("the pure-YAML twin stands alone: same composition shape, zero injected code", async () => {
    // Scope addition A: the FULL surface must also work with every activity declared
    // inline — no `extraActivities`. Same step/gate ids as the yaml+code parent.
    const pureSpec = loadYamlSpec(read("typeflux-pure.yaml"), {
      sourceLabel: "examples/claims-review-composition/typeflux-pure.yaml",
    });
    const pureSubworkflows = projectSubworkflowResolver("claims_review_pure", (id) =>
      CHILDREN[id] !== undefined ? loadYamlSpec(CHILDREN[id]) : undefined,
    );
    const purePlan = workflowPlanFromSpec(pureSpec, { subworkflows: pureSubworkflows });
    const codedPlan = workflowPlanFromSpec(spec(), { subworkflows: subworkflows() });
    expect(purePlan.steps.map((step) => step.id)).toEqual(codedPlan.steps.map((step) => step.id));
    expect(purePlan.lifecycle?.gates?.map((gate) => gate.id)).toEqual(["intake_gate", "compliance_gate"]);

    // Assembles with NO extraActivities — finalize comes from the spec definition.
    const { activities } = assembleYamlRuntime(pureSpec, {
      provider: new ClaimsReviewProvider(),
      schemas,
      subworkflows: pureSubworkflows,
    });
    for (const name of ["acknowledge", "consolidate", "finalize"]) {
      expect(activities[name]).toBeDefined();
    }
    const packet = (await activities["finalize"]!({ summary: "s", escalate: true })) as { decision: string };
    expect(packet.decision).toBe("approved");
  });

  it("registry composition (#748): child prompts resolve from the MERGE, not a parent copy", async () => {
    // The parent no longer duplicates the sub-workflows' prompts (`triage-claim`,
    // `escalate-review`) — they are pulled in from the child specs' own registries. The
    // child activities co-register on the composed worker, so invoking them exercises the
    // MERGED registry: a successful resolve proves the platform guarantee (#748) that
    // superseded the consumer's hand-rolled prompt-parity test.
    const pureSpec = loadYamlSpec(read("typeflux-pure.yaml"), {
      sourceLabel: "examples/claims-review-composition/typeflux-pure.yaml",
    });
    // The parent's own registry deliberately OMITS the children's prompts now.
    expect(Object.keys(pureSpec.runtime.registry.prompts ?? {})).not.toContain("triage-claim");
    expect(Object.keys(pureSpec.runtime.registry.prompts ?? {})).not.toContain("escalate-review");

    const { activities } = assembleYamlRuntime(pureSpec, {
      provider: new ClaimsReviewProvider(),
      schemas,
      subworkflows: projectSubworkflowResolver("claims_review_pure", (id) =>
        CHILDREN[id] !== undefined ? loadYamlSpec(CHILDREN[id]) : undefined,
      ),
    });
    // `triage_claim` (its prompt `triage-claim` lives only in claim-triage.yaml) resolves and runs.
    const triage = (await activities["triage_claim"]!({ claim_id: "CLM-1", text: "a" })) as { risk: string };
    expect(triage.risk).toBe("medium");
    // `escalate_review` (`escalate-review` lives only in escalation-review.yaml) resolves and runs.
    const escalated = (await activities["escalate_review"]!({ summary: "s", escalate: true })) as {
      escalate: boolean;
    };
    expect(escalated.escalate).toBe(true);
  });

  it("intake_gate timeout ROUTES to finalize at the gate runtime (the expedite default)", () => {
    // The unit twin of the Python runtime test: the example's own two-gate lifecycle
    // shape, gate 1's `timeout: { on_timeout: route, route: finalize }` fired through
    // the gate runtime — no reviewer decision, the timer routes to `finalize`.
    const plan = workflowPlanFromSpec(spec(), { subworkflows: subworkflows() });
    const runtime = new LifecycleRuntime(plan.lifecycle!, () => "2026-01-01T00:00:00.000Z");
    runtime.started();
    const gate = runtime.pendingGateAfter("consolidate")!;
    expect(gate.id).toBe("intake_gate");
    runtime.waitingForGate(gate);
    expect(runtime.gateTimedOut(gate)).toEqual({ action: "route", route: "finalize" });
    expect(runtime.status()).toMatchObject({ state: "running", review_route_target: "finalize" });
    expect(runtime.status().events.map((event) => event.event).slice(-2)).toEqual(["review_timed_out", "review_routed"]);
  });
});
