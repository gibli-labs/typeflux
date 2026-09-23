/**
 * Lifecycle review - the TypeScript counterpart to the Python
 * `examples/lifecycle_review`. The same sequential review chain as
 * `regulated-disclosure-review` (assess -> package -> prepare -> route -> notify),
 * each step a typed `defineActivity` with a normalization `hook`.
 *
 * NOTE: in Python, `lifecycle_review` is distinguished from `regulated_disclosure_review`
 * by its YAML *lifecycle* config - human-in-the-loop review signals
 * (`wait_for_lifecycle_state` / `submit_lifecycle_review`) driven by Temporal
 * workflow state. That durable, signal-driven layer is a Temporal-runtime capability
 * the consolidated `@typeflux/temporal` package does not include; what ports is the
 * code-defined activity chain itself.
 */

import { z } from "zod";

import type { ChatMessage, ModelProvider } from "../src/index.js";
import { defineActivity, executeActivity } from "../src/index.js";

export const CaseInput = z.object({
  caseId: z.string(),
  customerName: z.string(),
  request: z.string(),
  riskNotes: z.array(z.string()),
});
export type CaseInput = z.infer<typeof CaseInput>;

export const RiskAssessment = z.object({
  caseId: z.string().default(""),
  riskLevel: z.string(),
  summary: z.string(),
  flags: z.array(z.string()).default([]),
});
export type RiskAssessment = z.infer<typeof RiskAssessment>;

export const ReviewPacket = z.object({
  caseId: z.string().default(""),
  recommendation: z.string(),
  summary: z.string(),
  approvalRequired: z.boolean().default(false),
  flags: z.array(z.string()).default([]),
});
export type ReviewPacket = z.infer<typeof ReviewPacket>;

export const FinalDecision = z.object({
  caseId: z.string().default(""),
  decision: z.string(),
  summary: z.string(),
  approved: z.boolean().default(false),
});
export type FinalDecision = z.infer<typeof FinalDecision>;

function mergedFlags(...groups: string[][]): string[] {
  return [...new Set(groups.flat())].sort();
}

const normalizeReviewRoute = (input: ReviewPacket, output: ReviewPacket): ReviewPacket => ({
  ...output,
  caseId: input.caseId,
  approvalRequired: input.approvalRequired,
  flags: mergedFlags(input.flags, output.flags),
});

export const assessCase = defineActivity({
  name: "assess_case",
  prompt: { name: "lifecycle-review-assess", label: "production" },
  input: CaseInput,
  output: RiskAssessment,
  hook: (input, output) => ({
    ...output,
    caseId: input.caseId,
    riskLevel: output.riskLevel || (input.riskNotes.length > 0 ? "high" : "medium"),
    flags: mergedFlags(input.riskNotes, output.flags),
  }),
});

export const packageForReview = defineActivity({
  name: "package_for_review",
  prompt: { name: "lifecycle-review-package", label: "production" },
  input: RiskAssessment,
  output: ReviewPacket,
  hook: (input, output) => ({
    ...output,
    caseId: input.caseId,
    approvalRequired: true,
    flags: mergedFlags(input.flags, output.flags),
  }),
});

export const prepareSubmission = defineActivity({
  name: "prepare_submission",
  prompt: { name: "lifecycle-review-prepare-submission", label: "production" },
  input: ReviewPacket,
  output: ReviewPacket,
  hook: normalizeReviewRoute,
});

export const routeToDepartment = defineActivity({
  name: "route_to_department",
  prompt: { name: "lifecycle-review-route-department", label: "production" },
  input: ReviewPacket,
  output: ReviewPacket,
  hook: normalizeReviewRoute,
});

export const sendEmail = defineActivity({
  name: "send_email",
  prompt: { name: "lifecycle-review-send-email", label: "production" },
  input: ReviewPacket,
  output: FinalDecision,
  hook: (input, output) => ({
    ...output,
    caseId: input.caseId,
    decision: output.decision || "approved",
    approved: output.decision !== "rejected",
  }),
});

function messages(instruction: string, body: string): ChatMessage[] {
  return [
    { role: "system", content: `You are a case-lifecycle reviewer. ${instruction}` },
    { role: "user", content: body },
  ];
}

export interface LifecycleReview {
  assessment: RiskAssessment;
  packet: ReviewPacket;
  decision: FinalDecision;
}

/** Run the sequential lifecycle review, threading each step into the next. */
export async function runLifecycleReview(
  input: CaseInput,
  options: { provider: ModelProvider },
): Promise<LifecycleReview> {
  const { provider } = options;

  const assessment = await executeActivity(assessCase, input, {
    provider,
    messages: messages("Assess the case risk.", `${input.caseId}: ${input.request}`),
  });
  const packet = await executeActivity(packageForReview, assessment, {
    provider,
    messages: messages("Package the assessment for review.", assessment.summary),
  });
  const prepared = await executeActivity(prepareSubmission, packet, {
    provider,
    messages: messages("Prepare the submission.", packet.recommendation),
  });
  const routed = await executeActivity(routeToDepartment, prepared, {
    provider,
    messages: messages("Route to the right department.", prepared.recommendation),
  });
  const decision = await executeActivity(sendEmail, routed, {
    provider,
    messages: messages("Notify the outcome.", routed.recommendation),
  });

  return { assessment, packet: routed, decision };
}
