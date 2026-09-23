/**
 * Financial-claims marketing review - the TypeScript counterpart to the Python
 * `examples/financial_claims_marketing_review`. Each promotional claim is reviewed
 * for compliance by a typed activity, then the reviews are consolidated into one
 * submission-level packet: claims are binned by decision and an overall decision
 * is derived (rejected > legal_review > revise > approved). Two activities, each
 * with a normalization `hook`.
 *
 * Code-defined port of `pipeline.run_offline_pipeline` (the Python example runs
 * via the YAML runtime).
 */

import { z } from "zod";

import type { ChatMessage, ModelProvider } from "../src/index.js";
import { defineActivity, executeActivity } from "../src/index.js";

const MarketingChannel = z.enum(["web", "email", "social", "advisor_script", "brochure"]);
const ProductCategory = z.enum(["retirement", "investment", "cash_management", "insurance"]);
const ReviewDecision = z.enum(["approved", "revise", "legal_review", "rejected"]);
const RiskLevel = z.enum(["low", "medium", "high", "critical"]);
const PacketDecision = z.enum([
  "approved",
  "revise_before_publish",
  "legal_review_required",
  "rejected",
]);

export const MarketingClaim = z.object({
  submissionId: z.string(),
  campaignId: z.string(),
  claimId: z.string(),
  productCategory: ProductCategory,
  channel: MarketingChannel,
  jurisdiction: z.string(),
  audience: z.string(),
  claimText: z.string(),
  evidence: z.string(),
  contact: z.string(),
});
export type MarketingClaim = z.infer<typeof MarketingClaim>;

export const MarketingSubmission = z.object({
  submissionId: z.string(),
  brand: z.string(),
  reviewerEmail: z.string(),
  claims: z.array(MarketingClaim).min(1),
});
export type MarketingSubmission = z.infer<typeof MarketingSubmission>;

export const ClaimComplianceReview = z.object({
  submissionId: z.string().default(""), // hook stamps these from the input claim
  campaignId: z.string().default(""),
  claimId: z.string().default(""),
  decision: ReviewDecision,
  riskLevel: RiskLevel,
  riskSignals: z.array(z.string()),
  missingEvidence: z.array(z.string()),
  requiredDisclosures: z.array(z.string()),
  suggestedRevision: z.string(),
  rationale: z.string(),
});
export type ClaimComplianceReview = z.infer<typeof ClaimComplianceReview>;

export const ClaimComplianceReviewBatch = z.object({
  reviews: z.array(ClaimComplianceReview).min(1),
});
export type ClaimComplianceReviewBatch = z.infer<typeof ClaimComplianceReviewBatch>;

export const MarketingReviewPacket = z.object({
  submissionId: z.string().default(""),
  finalDecision: PacketDecision.default("approved"), // hook derives it
  summary: z.string(),
  approvedClaimIds: z.array(z.string()).default([]),
  revisionClaimIds: z.array(z.string()).default([]),
  legalReviewClaimIds: z.array(z.string()).default([]),
  rejectedClaimIds: z.array(z.string()).default([]),
  missingEvidence: z.array(z.string()).default([]),
  requiredDisclosures: z.array(z.string()).default([]),
  // Model-provided operational guidance (parity with Python `suggested_next_steps`);
  // the hook leaves it untouched, so it flows through the `...output` spread.
  suggestedNextSteps: z.array(z.string()).default([]),
});
export type MarketingReviewPacket = z.infer<typeof MarketingReviewPacket>;

/** Trim, drop empties, sort unique (Python `sorted({str(v).strip() ...})`). */
function cleaned(values: string[]): string[] {
  return [...new Set(values.map((v) => String(v).trim()).filter(Boolean))].sort();
}

export const reviewMarketingClaim = defineActivity({
  name: "review_marketing_claim",
  prompt: { name: "financial-claims-review-claim", label: "production" },
  input: MarketingClaim,
  output: ClaimComplianceReview,
  validationRetries: 2,
  hook: (input, output) => ({
    ...output,
    submissionId: input.submissionId,
    campaignId: input.campaignId,
    claimId: input.claimId,
    riskSignals: cleaned(output.riskSignals),
    missingEvidence: cleaned(output.missingEvidence),
    requiredDisclosures: cleaned(output.requiredDisclosures),
    suggestedRevision: output.suggestedRevision.trim(),
    rationale: output.rationale.trim(),
  }),
});

export const consolidateMarketingReview = defineActivity({
  name: "consolidate_marketing_review",
  prompt: { name: "financial-claims-consolidate", label: "production" },
  input: ClaimComplianceReviewBatch,
  output: MarketingReviewPacket,
  hook: (batch, output) => {
    const idsFor = (decision: string) =>
      batch.reviews.filter((r) => r.decision === decision).map((r) => r.claimId);
    const rejected = idsFor("rejected");
    const legal = idsFor("legal_review");
    const revise = idsFor("revise");
    const missingEvidence = cleaned(batch.reviews.flatMap((r) => r.missingEvidence));
    const requiredDisclosures = cleaned(batch.reviews.flatMap((r) => r.requiredDisclosures));

    let finalDecision: z.infer<typeof PacketDecision>;
    if (rejected.length > 0) {
      finalDecision = "rejected";
    } else if (legal.length > 0) {
      finalDecision = "legal_review_required";
    } else if (revise.length > 0 || missingEvidence.length > 0) {
      finalDecision = "revise_before_publish";
    } else {
      finalDecision = "approved";
    }

    return {
      ...output,
      submissionId: batch.reviews[0]?.submissionId ?? "",
      approvedClaimIds: idsFor("approved"),
      revisionClaimIds: revise,
      legalReviewClaimIds: legal,
      rejectedClaimIds: rejected,
      missingEvidence,
      requiredDisclosures,
      finalDecision,
    };
  },
});

function messages(instruction: string, body: string): ChatMessage[] {
  return [
    { role: "system", content: `You are a financial-marketing compliance reviewer. ${instruction}` },
    { role: "user", content: body },
  ];
}

/** Review each marketing claim, then consolidate into a submission packet. */
export async function runMarketingReview(
  submission: MarketingSubmission,
  options: { provider: ModelProvider },
): Promise<MarketingReviewPacket> {
  const { provider } = options;
  const reviews: ClaimComplianceReview[] = [];
  for (const claim of submission.claims) {
    reviews.push(
      await executeActivity(reviewMarketingClaim, claim, {
        provider,
        messages: messages(`Review this ${claim.channel} claim.`, claim.claimText),
      }),
    );
  }
  return executeActivity(consolidateMarketingReview, { reviews }, {
    provider,
    messages: messages("Consolidate the claim reviews.", submission.brand),
  });
}
