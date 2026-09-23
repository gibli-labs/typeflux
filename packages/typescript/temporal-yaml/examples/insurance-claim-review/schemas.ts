/**
 * Zod ports of the Python example's pydantic models
 * (`packages/python/examples/insurance_claim_review/schemas.py`). Datetimes ride
 * as ISO-8601 strings (the wire shape either SDK sees across Temporal payloads).
 */

import { z } from "zod";

export const EvidenceKind = z.enum([
  "photo",
  "invoice",
  "email",
  "repair_estimate",
  "police_report",
  "adjuster_note",
  "other",
]);

export const EvidenceDecision = z.enum(["support", "contradict", "needs_follow_up", "irrelevant"]);

export const ClaimRecommendation = z.enum(["approve", "investigate", "deny", "escalate"]);

export const EvidenceItem = z.object({
  claim_id: z.string().describe("Claim identifier this evidence belongs to."),
  evidence_id: z.string().describe("Stable evidence identifier."),
  kind: EvidenceKind.describe("Evidence type."),
  source: z.string().describe("Source system, sender, or uploader."),
  received_at: z.string().describe("When the evidence entered the claim file (ISO-8601)."),
  content: z.string().describe("Text extracted from the evidence item."),
});
export type EvidenceItem = z.infer<typeof EvidenceItem>;

export const ClaimInput = z
  .object({
    claim_id: z.string().describe("Claim identifier."),
    policy_id: z.string().describe("Policy identifier."),
    claimant_name: z.string().describe("Claimant display name."),
    loss_description: z.string().describe("Claimant's description of the loss."),
    loss_date: z.string().describe("Reported date of loss (ISO-8601)."),
    evidence: z.array(EvidenceItem).min(1, "claim must include at least one evidence item"),
  })
  .refine((claim) => new Set(claim.evidence.map((item) => item.claim_id)).size === 1, {
    message: "all evidence items must use the same claim_id",
    path: ["evidence"],
  });
export type ClaimInput = z.infer<typeof ClaimInput>;

export const EvidenceReview = z.object({
  claim_id: z.string().describe("Claim identifier copied from the evidence item."),
  evidence_id: z.string().describe("Reviewed evidence identifier."),
  decision: EvidenceDecision.describe("How this evidence affects the claim."),
  relevance_score: z
    .number()
    .min(0)
    .max(1)
    .describe("0 means irrelevant, 1 means directly claim-dispositive."),
  risk_signals: z.array(z.string()).describe("Potential fraud, compliance, or ambiguity signals."),
  missing_context: z.array(z.string()).describe("Information needed before final disposition."),
  follow_up_questions: z.array(z.string()).describe("Specific questions for adjuster follow-up."),
  summary: z.string().describe("Short evidence-level review summary."),
});
export type EvidenceReview = z.infer<typeof EvidenceReview>;

export const EvidenceReviewBatch = z.object({
  reviews: z.array(EvidenceReview).min(1, "review batch must contain at least one review"),
});
export type EvidenceReviewBatch = z.infer<typeof EvidenceReviewBatch>;

export const ClaimReviewPacket = z.object({
  claim_id: z.string().describe("Claim identifier."),
  recommendation: ClaimRecommendation.describe("Suggested claim disposition."),
  confidence: z.number().min(0).max(1).describe("Confidence in the recommendation."),
  summary: z.string().describe("Claim-level review summary."),
  risk_signals: z.array(z.string()).describe("Claim-level risk signals."),
  required_follow_up: z.array(z.string()).describe("Follow-up needed before disposition."),
  evidence_count: z.number().int().min(1).describe("Number of reviewed evidence items."),
  approval_required: z.boolean().describe("True when a human adjuster must approve."),
});
export type ClaimReviewPacket = z.infer<typeof ClaimReviewPacket>;

/** The `schemas:` resolver map the YAML runtime needs. */
export const schemas = {
  "schemas:EvidenceItem": EvidenceItem,
  "schemas:ClaimInput": ClaimInput,
  "schemas:EvidenceReview": EvidenceReview,
  "schemas:EvidenceReviewBatch": EvidenceReviewBatch,
  "schemas:ClaimReviewPacket": ClaimReviewPacket,
};
