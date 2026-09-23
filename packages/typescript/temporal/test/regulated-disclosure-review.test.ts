import { describe, expect, it } from "vitest";

import type { DisclosureInput } from "../examples/regulated-disclosure-review.js";
import { runDisclosureReview } from "../examples/regulated-disclosure-review.js";
import { ScriptedProvider } from "./helpers/scripted-provider.js";

function input(overrides?: Partial<DisclosureInput>): DisclosureInput {
  return {
    caseId: "DISC-2026-0042",
    customerName: "Avery Morgan",
    request: "Disclose a material change in account terms ahead of renewal.",
    riskNotes: ["regulated", "manual review"],
    ...overrides,
  };
}

// Raw model outputs (the hooks stamp caseId / approvalRequired / flags afterwards).
const SCRIPT = {
  assess: { riskLevel: "", summary: "Material change flagged.", flags: ["pii"] },
  package: { recommendation: "Escalate to senior reviewer.", summary: "Packaged.", flags: ["legal"] },
  prepare: { recommendation: "Submission prepared.", summary: "Prepared.", flags: [] },
  route: { recommendation: "Route to compliance-A.", summary: "Routed.", flags: ["compliance"] },
  finalize: { decision: "approved", summary: "Approved with conditions." },
};

const scriptedProvider = (finalize: unknown = SCRIPT.finalize) =>
  new ScriptedProvider([SCRIPT.assess, SCRIPT.package, SCRIPT.prepare, SCRIPT.route, finalize]);

// Output-schema provider-safety for these activities is covered in
// scenario-schemas.test.ts.
describe("regulated disclosure review (TS counterpart)", () => {
  it("threads the case id and derives risk/approval through the chain", async () => {
    const review = await runDisclosureReview(input(), { provider: scriptedProvider() });

    // Every step is stamped with the input case id (the model omits it).
    expect(review.assessment.caseId).toBe("DISC-2026-0042");
    expect(review.packet.caseId).toBe("DISC-2026-0042");
    expect(review.decision.caseId).toBe("DISC-2026-0042");

    // riskLevel was empty -> derived "high" because riskNotes are present.
    expect(review.assessment.riskLevel).toBe("high");
    // Flags merge forward, sorted+unique: riskNotes -> assessment -> packet -> routed.
    expect(review.assessment.flags).toEqual(["manual review", "pii", "regulated"]);
    expect(review.packet.flags).toEqual(["compliance", "legal", "manual review", "pii", "regulated"]);
    expect(review.packet.approvalRequired).toBe(true);

    // decision approved (not "rejected").
    expect(review.decision.decision).toBe("approved");
    expect(review.decision.approved).toBe(true);
  });

  it("derives approved=false when the disclosure is rejected", async () => {
    const review = await runDisclosureReview(input(), {
      provider: scriptedProvider({ decision: "rejected", summary: "Insufficient basis." }),
    });
    expect(review.decision.decision).toBe("rejected");
    expect(review.decision.approved).toBe(false);
  });

  it("defaults risk to medium when there are no risk notes", async () => {
    const review = await runDisclosureReview(input({ riskNotes: [] }), {
      provider: scriptedProvider(),
    });
    // assess script returns riskLevel "" -> hook derives "medium" (no risk notes).
    expect(review.assessment.riskLevel).toBe("medium");
  });
});
