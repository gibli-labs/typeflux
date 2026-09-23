import { describe, expect, it } from "vitest";

import type { MarketingClaim, MarketingSubmission } from "../examples/financial-claims-marketing-review.js";
import { runMarketingReview } from "../examples/financial-claims-marketing-review.js";
import { ScriptedProvider } from "./helpers/scripted-provider.js";

function mkClaim(claimId: string): MarketingClaim {
  return {
    submissionId: "SUB-1",
    campaignId: "CMP-1",
    claimId,
    productCategory: "investment",
    channel: "web",
    jurisdiction: "US",
    audience: "retail",
    claimText: `Claim ${claimId}`,
    evidence: "prospectus",
    contact: "owner@example.com",
  };
}

function submission(ids: string[]): MarketingSubmission {
  return {
    submissionId: "SUB-1",
    brand: "Acme Invest",
    reviewerEmail: "reviewer@example.com",
    claims: ids.map(mkClaim),
  };
}

const review = (decision: string, extra: Record<string, unknown> = {}) => ({
  decision,
  riskLevel: "low",
  riskSignals: [],
  missingEvidence: [],
  requiredDisclosures: [],
  suggestedRevision: "",
  rationale: "ok",
  ...extra,
});

const packet = { summary: "Consolidated." };

// Output-schema provider-safety for these activities is covered in
// scenario-schemas.test.ts.
describe("financial-claims marketing review (TS counterpart)", () => {
  it("bins claims by decision and escalates to legal_review", async () => {
    const provider = new ScriptedProvider([
      review("approved", { requiredDisclosures: ["APR disclosure"] }),
      review("revise", { riskSignals: ["Overstated"], missingEvidence: ["Source"] }),
      review("legal_review"),
      packet,
    ]);

    const result = await runMarketingReview(submission(["CLM-1", "CLM-2", "CLM-3"]), { provider });

    expect(result.submissionId).toBe("SUB-1");
    expect(result.approvedClaimIds).toEqual(["CLM-1"]);
    expect(result.revisionClaimIds).toEqual(["CLM-2"]);
    expect(result.legalReviewClaimIds).toEqual(["CLM-3"]);
    expect(result.rejectedClaimIds).toEqual([]);
    expect(result.missingEvidence).toEqual(["Source"]);
    expect(result.requiredDisclosures).toEqual(["APR disclosure"]);
    // rejected? no. legal? yes -> legal_review_required.
    expect(result.finalDecision).toBe("legal_review_required");
  });

  it("a single rejected claim makes the whole submission rejected", async () => {
    const provider = new ScriptedProvider([
      review("approved"),
      review("rejected"),
      review("legal_review"),
      packet,
    ]);
    const result = await runMarketingReview(submission(["A", "B", "C"]), { provider });
    expect(result.rejectedClaimIds).toEqual(["B"]);
    expect(result.finalDecision).toBe("rejected"); // rejected outranks legal_review
  });

  it("approves when every claim is approved with no missing evidence", async () => {
    const provider = new ScriptedProvider([review("approved"), review("approved"), packet]);
    const result = await runMarketingReview(submission(["A", "B"]), { provider });
    expect(result.approvedClaimIds).toEqual(["A", "B"]);
    expect(result.finalDecision).toBe("approved");
  });

  it("preserves model-provided suggestedNextSteps through consolidation", async () => {
    const provider = new ScriptedProvider([
      review("approved"),
      { summary: "Done.", suggestedNextSteps: ["Add APR disclosure", "Re-review in Q3"] },
    ]);
    const result = await runMarketingReview(submission(["A"]), { provider });
    expect(result.suggestedNextSteps).toEqual(["Add APR disclosure", "Re-review in Q3"]);
  });
});
