/**
 * Regulated disclosure review - the TypeScript counterpart to the Python
 * `examples/regulated_disclosure_review`. A SEQUENTIAL five-step review (the
 * complement to the MLR example's fan-out): a disclosure request is assessed,
 * packaged, prepared, routed to compliance, and finalized, each step a typed
 * `defineActivity` whose normalization `hook` threads the case id and merges
 * flags forward so the chain stays internally consistent.
 *
 * The Python example runs via the YAML runtime; this is the code-defined form
 * (`defineActivity` + `executeActivity`), the SDK's first-class path.
 */

import { z } from "zod";

import type { ChatMessage, ModelProvider } from "../src/index.js";
import { defineActivity, executeActivity } from "../src/index.js";

export const DisclosureInput = z.object({
  caseId: z.string(),
  customerName: z.string(),
  request: z.string(),
  riskNotes: z.array(z.string()),
});
export type DisclosureInput = z.infer<typeof DisclosureInput>;

export const RiskAssessment = z.object({
  caseId: z.string().default(""), // hook stamps it from the input
  riskLevel: z.string(),
  summary: z.string(),
  flags: z.array(z.string()).default([]),
});
export type RiskAssessment = z.infer<typeof RiskAssessment>;

export const ReviewPacket = z.object({
  caseId: z.string().default(""),
  recommendation: z.string(),
  summary: z.string(),
  approvalRequired: z.boolean().default(false), // hook sets it
  flags: z.array(z.string()).default([]),
});
export type ReviewPacket = z.infer<typeof ReviewPacket>;

export const FinalDecision = z.object({
  caseId: z.string().default(""),
  decision: z.string(),
  summary: z.string(),
  approved: z.boolean().default(false), // hook derives it from `decision`
});
export type FinalDecision = z.infer<typeof FinalDecision>;

/** Sorted unique merge - the TS form of Python `sorted(set(...))`. */
function mergedFlags(...groups: string[][]): string[] {
  return [...new Set(groups.flat())].sort();
}

/** prepare + route share this normalization (carry the case + approval forward). */
const normalizeReviewRoute = (input: ReviewPacket, output: ReviewPacket): ReviewPacket => ({
  ...output,
  caseId: input.caseId,
  approvalRequired: input.approvalRequired,
  flags: mergedFlags(input.flags, output.flags),
});

export const assessDisclosure = defineActivity({
  name: "assess_disclosure",
  prompt: { name: "regulated-disclosure-assess", label: "production" },
  input: DisclosureInput,
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
  prompt: { name: "regulated-disclosure-package", label: "production" },
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
  prompt: { name: "regulated-disclosure-prepare", label: "production" },
  input: ReviewPacket,
  output: ReviewPacket,
  hook: normalizeReviewRoute,
});

export const routeToCompliance = defineActivity({
  name: "route_to_compliance",
  prompt: { name: "regulated-disclosure-route", label: "production" },
  input: ReviewPacket,
  output: ReviewPacket,
  hook: normalizeReviewRoute,
});

export const finalizeDisclosure = defineActivity({
  name: "finalize_disclosure",
  prompt: { name: "regulated-disclosure-finalize", label: "production" },
  input: ReviewPacket,
  output: FinalDecision,
  hook: (input, output) => ({
    ...output,
    caseId: input.caseId,
    decision: output.decision || "approved",
    approved: output.decision !== "rejected",
  }),
});

function messages(role: string, body: string): ChatMessage[] {
  return [
    { role: "system", content: `You are a regulated-disclosure reviewer. ${role}` },
    { role: "user", content: body },
  ];
}

export interface DisclosureReview {
  assessment: RiskAssessment;
  packet: ReviewPacket;
  decision: FinalDecision;
}

/** Run the sequential disclosure review, threading each step into the next. */
export async function runDisclosureReview(
  input: DisclosureInput,
  options: { provider: ModelProvider },
): Promise<DisclosureReview> {
  const { provider } = options;

  const assessment = await executeActivity(assessDisclosure, input, {
    provider,
    messages: messages("Assess the disclosure's risk.", `${input.caseId}: ${input.request}`),
  });
  const packet = await executeActivity(packageForReview, assessment, {
    provider,
    messages: messages("Package the assessment for review.", assessment.summary),
  });
  const prepared = await executeActivity(prepareSubmission, packet, {
    provider,
    messages: messages("Prepare the submission.", packet.recommendation),
  });
  const routed = await executeActivity(routeToCompliance, prepared, {
    provider,
    messages: messages("Route to the right compliance queue.", prepared.recommendation),
  });
  const decision = await executeActivity(finalizeDisclosure, routed, {
    provider,
    messages: messages("Finalize the disclosure decision.", routed.recommendation),
  });

  return { assessment, packet: routed, decision };
}
