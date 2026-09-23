/**
 * A scripted, offline provider (no API key, no billing) for the governed-review
 * example. It reports `providerName = "openai"` so the run exercises the SAME
 * provider identity the org/tenant policy governs and the spec's
 * `provider_limits.providers.openai` tier targets.
 */

import type { ModelProvider, ModerationResult, StructuredCallParams } from "@typeflux/temporal";

export class ScriptedReviewProvider implements ModelProvider {
  readonly providerName = "openai";
  readonly calls: StructuredCallParams[] = [];

  structuredCall(params: StructuredCallParams): unknown {
    this.calls.push(params);
    const properties = (params.outputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    if (!("category" in properties)) {
      throw new Error(`ScriptedReviewProvider: unexpected output schema: ${Object.keys(properties).join(", ")}`);
    }
    // Deterministic assessment — the sample message asks for a password reset but
    // leaks an SSN, so it routes to human review (and motivates the redaction the
    // policy requires before any of this reaches a trace backend).
    return {
      id: "msg-1001",
      category: "review",
      risk_score: 0.42,
      notes: "Password-reset request containing an SSN — redact PII and route to a human reviewer.",
    };
  }
}

/**
 * A lenient content-safety moderator (injected for the `assess_content` activity —
 * `moderators` is keyed by activity name): it does NOT flag (`flagged: false`), but
 * reports a category the org policy forbids. With the activity's own
 * `on_violation: flag` it would pass — but the moderation POLICY escalates the
 * reported category to a block at the runtime checkpoint (#454).
 */
export const contentSafetyModerator = (): ModerationResult => ({
  flagged: false,
  categories: ["self_harm"],
  detail: "reported a sensitive category the moderator itself chose not to block",
});
