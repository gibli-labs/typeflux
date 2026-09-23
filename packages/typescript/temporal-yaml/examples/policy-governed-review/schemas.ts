/**
 * Schema resolver for the policy-governed review example. The activity classifies
 * one content item; the scripted provider tells the activity apart by the presence
 * of the `category` property on the output schema.
 */

import { z } from "zod";

export const schemas = {
  "schemas:ContentItem": z.object({
    id: z.string(),
    body: z.string(),
  }),
  "schemas:Assessment": z.object({
    id: z.string(),
    category: z.enum(["safe", "review", "block"]),
    risk_score: z.number(),
    notes: z.string(),
  }),
};
