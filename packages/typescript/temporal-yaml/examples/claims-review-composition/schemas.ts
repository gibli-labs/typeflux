/**
 * Zod schemas for the claims-review composition example (#55) — the TypeScript
 * counterpart of `packages/python/examples/claims_review_composition/schemas.py`.
 * The types chain the parent graph: the `screen` parallel block collects into
 * `IntakeFanout` (fields ARE the branch ids, `.optional()` where the branch is gated),
 * which `consolidate` folds into `Consolidated`; the `escalation` sub-workflow keeps
 * that type, and `finalize` yields the terminal `ReviewPacket`.
 */

import { z } from "zod";

export const schemas = {
  "schemas:Claim": z.object({ claim_id: z.string(), text: z.string() }),
  "schemas:ClaimBatch": z.object({
    priority: z.string(),
    claims: z.array(z.object({ claim_id: z.string(), text: z.string() })),
  }),
  "schemas:Triage": z.object({ claim_id: z.string(), risk: z.string() }),
  "schemas:TriageBatch": z.object({
    triaged: z.array(z.object({ claim_id: z.string(), risk: z.string() })),
  }),
  "schemas:Acknowledgement": z.object({ note: z.string() }),
  // The parallel collect object: fields are the branch ids, Optional because both
  // branches are `when`-gated (decision D4).
  "schemas:IntakeFanout": z.object({
    fast_track: z.object({ note: z.string() }).nullish(),
    full_review: z.object({ triaged: z.array(z.object({ claim_id: z.string(), risk: z.string() })) }).nullish(),
  }),
  "schemas:Consolidated": z.object({ summary: z.string(), escalate: z.boolean() }),
  "schemas:ReviewPacket": z.object({ decision: z.string() }),
};
