/**
 * A scripted provider mirroring the Python `LifecycleDemoProvider`
 * (`packages/python/examples/lifecycle_review/fakes.py`): deterministic, offline.
 * TS providers receive the provider-safe JSON SCHEMA, so the three output types
 * are told apart by shape (`risk_level` / `approved` / else the review packet).
 */

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

export class LifecycleDemoProvider implements ModelProvider {
  readonly providerName = "fake";

  structuredCall(params: StructuredCallParams): unknown {
    const properties = (params.outputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    if ("risk_level" in properties) {
      return {
        case_id: "CASE-2026-0101",
        risk_level: "high",
        summary: "Request is eligible but needs approval because risk notes are present.",
        flags: ["manual review"],
      };
    }
    if ("approved" in properties) {
      return {
        case_id: "CASE-2026-0101",
        decision: "approved",
        summary: "Human approval was received and the case can proceed.",
        approved: true,
      };
    }
    if ("recommendation" in properties) {
      return {
        case_id: "CASE-2026-0101",
        recommendation: "approve_after_review",
        summary: "Prepared for human approval before finalization.",
        approval_required: true,
        flags: ["manual review"],
      };
    }
    throw new Error(`LifecycleDemoProvider: unexpected output schema: ${Object.keys(properties).join(", ")}`);
  }
}
