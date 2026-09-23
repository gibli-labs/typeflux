/**
 * A deterministic, offline provider for the composition example (repo convention:
 * a scripted provider, no API key). TS providers receive the provider-safe JSON
 * schema, so the output types are told apart by shape.
 */

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

export class ClaimsReviewProvider implements ModelProvider {
  readonly providerName = "fake";

  structuredCall(params: StructuredCallParams): unknown {
    const properties = (params.outputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    if ("risk" in properties) return { claim_id: "demo", risk: "medium" };
    if ("note" in properties) return { note: "acknowledged" };
    if ("escalate" in properties) return { summary: "two claims triaged; escalation advised", escalate: true };
    if ("decision" in properties) return { decision: "approved" };
    throw new Error(`ClaimsReviewProvider: unexpected output schema: ${Object.keys(properties).join(", ")}`);
  }
}
