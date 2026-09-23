/**
 * Zod ports of the Python example's pydantic models
 * (`packages/python/examples/lifecycle_review/schemas.py`).
 */

import { z } from "zod";

export const CaseInput = z.object({
  case_id: z.string().describe("Case identifier."),
  customer_name: z.string().describe("Customer name."),
  request: z.string().describe("Customer request to review."),
  risk_notes: z.array(z.string()).default([]).describe("Known risk notes."),
});
export type CaseInput = z.infer<typeof CaseInput>;

export const RiskAssessment = z.object({
  case_id: z.string(),
  risk_level: z.string(),
  summary: z.string(),
  flags: z.array(z.string()).default([]),
});
export type RiskAssessment = z.infer<typeof RiskAssessment>;

export const ReviewPacket = z.object({
  case_id: z.string(),
  recommendation: z.string(),
  summary: z.string(),
  approval_required: z.boolean(),
  flags: z.array(z.string()).default([]),
});
export type ReviewPacket = z.infer<typeof ReviewPacket>;

export const FinalDecision = z.object({
  case_id: z.string(),
  decision: z.string(),
  summary: z.string(),
  approved: z.boolean(),
});
export type FinalDecision = z.infer<typeof FinalDecision>;

export const schemas = {
  "schemas:CaseInput": CaseInput,
  "schemas:RiskAssessment": RiskAssessment,
  "schemas:ReviewPacket": ReviewPacket,
  "schemas:FinalDecision": FinalDecision,
};
