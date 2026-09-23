/**
 * A scripted provider for the two activities (offline — no API key). Told apart
 * by output-schema shape: a `category` output is the classifier; a `verdict`
 * output is the substantiator. The substantiator marks a claim mentioning
 * "unverified" as unsupported, so a fan-out shows mixed verdicts.
 */

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";

export class ScriptedReviewProvider implements ModelProvider {
  readonly providerName = "fake";

  structuredCall(params: StructuredCallParams): unknown {
    const properties = (params.outputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    if ("category" in properties) {
      return { category: "financial-disclosure" };
    }
    if ("verdict" in properties) {
      // The rendered prompt carries the claim text; flag the unverifiable one.
      const rendered = params.messages
        .map((m) => (typeof m.content === "string" ? m.content : ""))
        .join(" ");
      const supported = !rendered.includes("unverified");
      return {
        verdict: supported ? "substantiated" : "insufficient_evidence",
        supported,
      };
    }
    throw new Error(`ScriptedReviewProvider: unexpected output schema: ${Object.keys(properties).join(", ")}`);
  }
}
