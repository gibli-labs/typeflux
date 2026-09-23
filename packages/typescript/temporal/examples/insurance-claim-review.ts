/**
 * Insurance claim evidence review - the TypeScript counterpart to the Python
 * `examples/insurance_claim_review`. Each evidence item is reviewed by a typed
 * activity, then the per-item reviews are consolidated into one claim-level
 * packet. Two activities, each with a normalization `hook`: the first stamps the
 * claim/evidence id and cleans signal lists; the second aggregates signals and
 * derives whether human approval is required.
 *
 * The Python example runs the per-item step as a YAML map step (concurrency 3);
 * this is the code-defined offline pipeline (a sequential per-item loop), the
 * direct port of `pipeline.run_offline_pipeline`.
 */

import { z } from "zod";

import type { ChatMessage, ModelProvider } from "../src/index.js";
import { defineActivity, executeActivity } from "../src/index.js";

const EvidenceKind = z.enum([
  "photo",
  "invoice",
  "email",
  "repair_estimate",
  "police_report",
  "adjuster_note",
  "other",
]);
const EvidenceDecision = z.enum(["support", "contradict", "needs_follow_up", "irrelevant"]);
const ClaimRecommendation = z.enum(["approve", "investigate", "deny", "escalate"]);

export const EvidenceItem = z.object({
  claimId: z.string(),
  evidenceId: z.string(),
  kind: EvidenceKind,
  source: z.string(),
  receivedAt: z.string(), // ISO timestamp
  content: z.string(),
});
export type EvidenceItem = z.infer<typeof EvidenceItem>;

export const ClaimInput = z
  .object({
    claimId: z.string(),
    policyId: z.string(),
    claimantName: z.string(),
    lossDescription: z.string(),
    lossDate: z.string(),
    evidence: z.array(EvidenceItem).min(1),
  })
  // Parity with the Python `ClaimInput` validator: every evidence item must belong
  // to the same claim, or consolidation would stamp one claimId over mixed evidence.
  .refine((claim) => claim.evidence.every((item) => item.claimId === claim.claimId), {
    message: "all evidence items must use the same claimId as the claim",
    path: ["evidence"],
  });
export type ClaimInput = z.infer<typeof ClaimInput>;

export const EvidenceReview = z.object({
  claimId: z.string().default(""), // hook stamps it from the input item
  evidenceId: z.string().default(""),
  decision: EvidenceDecision,
  relevanceScore: z.number().min(0).max(1),
  riskSignals: z.array(z.string()),
  missingContext: z.array(z.string()),
  followUpQuestions: z.array(z.string()),
  summary: z.string(),
});
export type EvidenceReview = z.infer<typeof EvidenceReview>;

export const EvidenceReviewBatch = z.object({
  reviews: z.array(EvidenceReview).min(1),
});
export type EvidenceReviewBatch = z.infer<typeof EvidenceReviewBatch>;

export const ClaimReviewPacket = z.object({
  claimId: z.string().default(""),
  recommendation: ClaimRecommendation,
  confidence: z.number().min(0).max(1),
  summary: z.string(),
  riskSignals: z.array(z.string()).default([]),
  requiredFollowUp: z.array(z.string()).default([]),
  evidenceCount: z.number().int().default(1),
  approvalRequired: z.boolean().default(false),
});
export type ClaimReviewPacket = z.infer<typeof ClaimReviewPacket>;

/** Trim, lowercase, drop empties, sort unique (Python `sorted({s.strip().lower() ...})`). */
function cleanedSet(values: string[]): string[] {
  return [...new Set(values.map((v) => v.trim().toLowerCase()).filter(Boolean))].sort();
}

export const reviewEvidenceItem = defineActivity({
  name: "review_evidence_item",
  prompt: { name: "insurance-claim-review-evidence", label: "production" },
  input: EvidenceItem,
  output: EvidenceReview,
  validationRetries: 2,
  hook: (input, output) => ({
    ...output,
    claimId: input.claimId,
    evidenceId: input.evidenceId,
    riskSignals: cleanedSet(output.riskSignals),
    missingContext: cleanedSet(output.missingContext),
    followUpQuestions: output.followUpQuestions.map((q) => q.trim()).filter(Boolean),
  }),
});

export const consolidateClaimReview = defineActivity({
  name: "consolidate_claim_review",
  prompt: { name: "insurance-claim-consolidate", label: "production" },
  input: EvidenceReviewBatch,
  output: ClaimReviewPacket,
  hook: (batch, output) => {
    const claimId = batch.reviews[0]?.claimId ?? "";
    const riskSignals = [
      ...new Set(batch.reviews.flatMap((r) => r.riskSignals).filter(Boolean)),
    ].sort();
    const requiredFollowUp = [
      ...new Set(
        batch.reviews.flatMap((r) => [...r.followUpQuestions, ...r.missingContext]).filter(Boolean),
      ),
    ].sort();
    return {
      ...output,
      claimId,
      riskSignals,
      requiredFollowUp,
      evidenceCount: batch.reviews.length,
      approvalRequired: Boolean(
        riskSignals.length > 0 || requiredFollowUp.length > 0 || output.recommendation !== "approve",
      ),
    };
  },
});

function messages(instruction: string, body: string): ChatMessage[] {
  return [
    { role: "system", content: `You are an insurance claim reviewer. ${instruction}` },
    { role: "user", content: body },
  ];
}

/** Review each evidence item, then consolidate into one claim-level packet. */
export async function runClaimReview(
  claim: ClaimInput,
  options: { provider: ModelProvider },
): Promise<ClaimReviewPacket> {
  const { provider } = options;
  const reviews: EvidenceReview[] = [];
  for (const item of claim.evidence) {
    reviews.push(
      await executeActivity(reviewEvidenceItem, item, {
        provider,
        messages: messages(`Review this ${item.kind} evidence.`, item.content),
      }),
    );
  }
  return executeActivity(
    consolidateClaimReview,
    { reviews },
    { provider, messages: messages("Consolidate the evidence reviews.", claim.lossDescription) },
  );
}
