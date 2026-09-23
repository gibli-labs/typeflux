import { describe, expect, it } from "vitest";
import { z } from "zod";

import { ClaimInput, runClaimReview } from "../examples/insurance-claim-review.js";
import { ScriptedProvider } from "./helpers/scripted-provider.js";

type ClaimInputT = z.infer<typeof ClaimInput>;

function claim(): ClaimInputT {
  const evidence = [
    {
      claimId: "CLM-1",
      evidenceId: "EV-1",
      kind: "photo" as const,
      source: "uploader",
      receivedAt: "2026-01-02T00:00:00Z",
      content: "damage photo",
    },
    {
      claimId: "CLM-1",
      evidenceId: "EV-2",
      kind: "invoice" as const,
      source: "shop",
      receivedAt: "2026-01-03T00:00:00Z",
      content: "repair invoice",
    },
  ];
  return {
    claimId: "CLM-1",
    policyId: "POL-1",
    claimantName: "Jordan Lee",
    lossDescription: "Collision damage to front bumper.",
    lossDate: "2026-01-01T00:00:00Z",
    evidence,
  };
}

// Output-schema provider-safety for these activities is covered in
// scenario-schemas.test.ts.
describe("insurance claim review (TS counterpart)", () => {
  it("reviews each evidence item then consolidates with aggregated signals", async () => {
    const provider = new ScriptedProvider([
      // Two raw evidence reviews (the hook cleans the signal lists).
      {
        decision: "support",
        relevanceScore: 0.9,
        riskSignals: ["  PII ", ""],
        missingContext: ["Date of loss "],
        followUpQuestions: ["Confirm date? "],
        summary: "Photo supports the claim.",
      },
      {
        decision: "needs_follow_up",
        relevanceScore: 0.5,
        riskSignals: ["Fraud"],
        missingContext: [],
        followUpQuestions: ["Verify invoice total?"],
        summary: "Invoice needs verification.",
      },
      // Consolidate (the hook stamps claimId, aggregates, derives approval).
      {
        recommendation: "investigate",
        confidence: 0.7,
        summary: "Mixed evidence; investigate.",
        riskSignals: [],
        requiredFollowUp: [],
        evidenceCount: 0,
        approvalRequired: false,
      },
    ]);

    const packet = await runClaimReview(claim(), { provider });

    expect(packet.claimId).toBe("CLM-1");
    expect(packet.evidenceCount).toBe(2);
    expect(packet.recommendation).toBe("investigate");
    // Per-item signals were cleaned (trim/lowercase/drop-empty) then aggregated.
    expect(packet.riskSignals).toEqual(["fraud", "pii"]);
    expect(packet.requiredFollowUp).toContain("Confirm date?");
    expect(packet.requiredFollowUp).toContain("Verify invoice total?");
    expect(packet.requiredFollowUp).toContain("date of loss"); // missingContext was lowercased
    // Risk signals present -> human approval required.
    expect(packet.approvalRequired).toBe(true);
  });

  it("rejects an evidence batch with mixed claim ids (parity with Python)", () => {
    const mixed = claim();
    mixed.evidence[1]!.claimId = "CLM-OTHER"; // belongs to a different claim
    expect(() => ClaimInput.parse(mixed)).toThrow(/same claimId/);
    // The valid claim parses cleanly.
    expect(() => ClaimInput.parse(claim())).not.toThrow();
  });

  it("requires no approval when clean and recommendation is approve", async () => {
    const provider = new ScriptedProvider([
      {
        decision: "support",
        relevanceScore: 1,
        riskSignals: [],
        missingContext: [],
        followUpQuestions: [],
        summary: "Clear support.",
      },
      {
        recommendation: "approve",
        confidence: 0.95,
        summary: "Approve.",
        riskSignals: [],
        requiredFollowUp: [],
        evidenceCount: 0,
        approvalRequired: true,
      },
    ]);

    const single = { ...claim(), evidence: [claim().evidence[0]!] };
    const packet = await runClaimReview(single, { provider });

    expect(packet.evidenceCount).toBe(1);
    expect(packet.recommendation).toBe("approve");
    expect(packet.riskSignals).toEqual([]);
    expect(packet.approvalRequired).toBe(false); // no signals, no follow-up, approve
  });
});
