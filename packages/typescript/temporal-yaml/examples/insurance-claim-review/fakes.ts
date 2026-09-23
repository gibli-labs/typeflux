/**
 * A scripted provider mirroring the Python example's `FakeProvider`
 * (`packages/python/examples/insurance_claim_review/fakes.py`): deterministic,
 * offline, no API key. TS providers receive the activity's provider-safe JSON
 * SCHEMA (not the zod object), so the two activities are told apart by shape —
 * the consolidate packet is the one with a `recommendation` property.
 */

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

export class ScriptedInsuranceProvider implements ModelProvider {
  /** Provider-limit selection keys off the provider NAME (#529), so the scripted
   * stand-in reports the name the spec's `provider_limits.providers.openai` tier
   * targets — the offline run exercises the same admission policy the real
   * provider would (codex). */
  readonly providerName = "openai";
  readonly calls: StructuredCallParams[] = [];
  private reviewIndex = 0;

  structuredCall(params: StructuredCallParams): unknown {
    this.calls.push(params);
    const properties = (params.outputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    if (!("recommendation" in properties) && !("decision" in properties)) {
      throw new Error(`ScriptedInsuranceProvider: unexpected output schema: ${Object.keys(properties).join(", ")}`);
    }
    if ("recommendation" in properties) {
      return {
        claim_id: "CLM-2026-0042",
        recommendation: "investigate",
        confidence: 0.78,
        summary: "Claim has supporting evidence, but duplicate billing needs adjuster review.",
        risk_signals: ["duplicate invoice"],
        required_follow_up: ["Confirm whether invoice INV-8842 was paid twice."],
        evidence_count: 4,
        approval_required: true,
      };
    }
    this.reviewIndex += 1;
    const index = this.reviewIndex;
    return {
      claim_id: "CLM-2026-0042",
      evidence_id: `pending-${index}`,
      decision: index === 2 ? "needs_follow_up" : "support",
      relevance_score: index === 3 ? 0.62 : 0.9,
      risk_signals: index === 2 ? ["duplicate invoice"] : [],
      missing_context: index === 2 ? ["repair shop estimate"] : [],
      follow_up_questions: index === 2 ? ["Confirm whether invoice INV-8842 was paid twice."] : [],
      summary: `Evidence item ${index} supports the claimed loss.`,
    };
  }
}
