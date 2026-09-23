/**
 * ADAPTED from the Python example's `main.py::sample_claim` (not a verbatim
 * port): datetimes ride as ISO strings, and the deliberate PII the original
 * plants (a card number, a phone number) is trimmed — it exists there to demo
 * the Langfuse redaction config, which this offline port omits (see README).
 */

import type { ClaimInput } from "./schemas.js";

export function sampleClaim(receivedAt = "2026-05-22T09:15:00Z"): ClaimInput {
  const claimId = "CLM-2026-0042";
  const evidence = (id: string, kind: ClaimInput["evidence"][number]["kind"], source: string, content: string) => ({
    claim_id: claimId,
    evidence_id: id,
    kind,
    source,
    received_at: receivedAt,
    content,
  });
  return {
    claim_id: claimId,
    policy_id: "POL-AUTO-7788",
    claimant_name: "Jordan Lee",
    loss_description:
      "Rear-end collision after heavy rain. Claimant reports bumper damage, " +
      "trunk alignment issues, and towing from the scene.",
    loss_date: "2026-05-21T14:30:00Z",
    evidence: [
      evidence(
        "EV-001",
        "photo",
        "mobile upload",
        "Photo note: rear bumper dent and cracked tail light. Uploaded by jordan.lee@example.com.",
      ),
      evidence(
        "EV-002",
        "invoice",
        "repair shop portal",
        "Invoice INV-8842 for bumper replacement, 1180 USD. A second copy of INV-8842 was submitted by email.",
      ),
      evidence(
        "EV-003",
        "email",
        "claimant email",
        "Claimant states the vehicle was towed and asks about rental coverage while repairs are pending.",
      ),
      evidence(
        "EV-004",
        "police_report",
        "county records",
        "Police report 22-04471 documents a two-vehicle rear-end collision at the reported time and location.",
      ),
    ],
  };
}
