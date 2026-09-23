/**
 * The canonical conformance fixture project's activity IO schemas as injected Zod objects (#620).
 * The control-plane server holds no activity CODE, so the EMBEDDING server supplies the schemas its
 * catalog/bundle projections need (the #620 injected-schemas seam). These mirror
 * `contracts/controlplane/conformance/project/python/conformance_project/schemas.py` shape-for-shape
 * so the TS catalog/bundle project the same JSON Schemas + hashes for the conformance suite.
 *
 * Keyed by the spec ref the workflow YAML uses (`schemas:<Name>`), matching the map a real host would
 * inject into `defineActivitiesFromSpec`. Serve's `--conformance-schemas` flag wires this in.
 */

import { z } from "zod";

const claimItem = z.object({ value: z.string() });
const claimInput = z.object({ claims: z.array(claimItem) });
const itemAssessment = z.object({ value: z.string() });
const assessmentBatch = z.object({ reviews: z.array(itemAssessment) });
const decision = z.object({ value: z.string() });
const classification = z.object({ value: z.string(), deep_review: z.boolean() });
// The composition fixture's parallel collect object (#55): fields ARE the branch ids
// (decision D4); the gated `summary` branch's field is nullable (None when skipped).
const reviewFanout = z.object({ assessments: assessmentBatch, summary: assessmentBatch.nullable() });

/** The conformance schemas keyed by spec ref (`schemas:ClaimItem`, …) for the injected-schemas seam. */
export const CONFORMANCE_SCHEMAS: Readonly<Record<string, z.ZodType>> = {
  "schemas:ClaimItem": claimItem,
  "schemas:ClaimInput": claimInput,
  "schemas:ItemAssessment": itemAssessment,
  "schemas:AssessmentBatch": assessmentBatch,
  "schemas:Decision": decision,
  "schemas:Classification": classification,
  "schemas:ReviewFanout": reviewFanout,
};
