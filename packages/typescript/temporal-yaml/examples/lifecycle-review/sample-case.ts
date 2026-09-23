/** The Python example's sample case (`main.py`), adapted for the TS port. */

import type { CaseInput } from "./schemas.js";

export function sampleCase(): CaseInput {
  return {
    case_id: "CASE-2026-0101",
    customer_name: "Priya Natarajan",
    request: "Requesting an expedited policy change and a refund of the prorated premium.",
    risk_notes: ["prior chargeback on file", "expedited-handling request"],
  };
}
