/**
 * Schema resolver for the privacy-governance example (#188). The activity assesses
 * one disclosure case; the payload codec encrypts these values on Temporal history
 * and the custom redaction rules mask PII on the observability egress path.
 */

import { z } from "zod";

export const schemas = {
  "schemas:DisclosureRequest": z.object({
    case_id: z.string(),
    body: z.string(),
  }),
  "schemas:DisclosureAssessment": z.object({
    case_id: z.string(),
    risk_level: z.string(),
    summary: z.string(),
  }),
};
