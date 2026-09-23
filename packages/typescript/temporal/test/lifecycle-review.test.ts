import { describe, expect, it } from "vitest";

import { runLifecycleReview } from "../examples/lifecycle-review.js";
import { ScriptedProvider } from "./helpers/scripted-provider.js";

const SCRIPT = {
  assess: { riskLevel: "", summary: "Flagged.", flags: ["pii"] },
  package: { recommendation: "Escalate.", summary: "Packaged.", flags: ["legal"] },
  prepare: { recommendation: "Prepared.", summary: "Prepared.", flags: [] },
  route: { recommendation: "Route to ops.", summary: "Routed.", flags: ["ops"] },
};

const provider = (finalize: unknown) =>
  new ScriptedProvider([SCRIPT.assess, SCRIPT.package, SCRIPT.prepare, SCRIPT.route, finalize]);

const input = (riskNotes: string[] = ["regulated"]) => ({
  caseId: "CASE-1",
  customerName: "Sam Rivera",
  request: "Update beneficiary on the policy.",
  riskNotes,
});

// Output-schema provider-safety for these activities is covered in
// scenario-schemas.test.ts.
describe("lifecycle review (TS counterpart)", () => {
  it("threads the case id and derives risk/flags/approval through the chain", async () => {
    const review = await runLifecycleReview(input(), {
      provider: provider({ decision: "approved", summary: "Done." }),
    });
    expect(review.decision.caseId).toBe("CASE-1");
    expect(review.assessment.riskLevel).toBe("high"); // riskNotes present
    expect(review.assessment.flags).toEqual(["pii", "regulated"]);
    expect(review.packet.flags).toEqual(["legal", "ops", "pii", "regulated"]);
    expect(review.packet.approvalRequired).toBe(true);
    expect(review.decision.approved).toBe(true);
  });

  it("derives approved=false on rejection and medium risk without notes", async () => {
    const review = await runLifecycleReview(input([]), {
      provider: provider({ decision: "rejected", summary: "No." }),
    });
    expect(review.assessment.riskLevel).toBe("medium"); // no risk notes
    expect(review.decision.approved).toBe(false);
  });
});
