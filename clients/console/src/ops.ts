/**
 * Pure helpers for the operations surface. Input validation is two-stage:
 * the console parses shape (must be a JSON object) for instant feedback,
 * and the server validates against the workflow's input model (a 422
 * envelope on mismatch) — the contract stays server-side.
 */

export type ParsedInput =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; error: string };

export function parseWorkflowInput(text: string): ParsedInput {
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: false, error: "workflow input is required (a JSON object)" };
  }
  let value: unknown;
  try {
    value = JSON.parse(trimmed);
  } catch (caught) {
    return {
      ok: false,
      error: `not valid JSON: ${caught instanceof Error ? caught.message : String(caught)}`,
    };
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { ok: false, error: "workflow input must be a JSON object" };
  }
  return { ok: true, value: value as Record<string, unknown> };
}

/** Caller-chosen Temporal workflow id: readable prefix + compact timestamp. */
export function generateExecutionId(workflowId: string, now: number): string {
  return `${workflowId}-${now.toString(36)}`;
}

/** Copyable TraceListQuery snippet from a start receipt's hint. */
export function traceQuerySnippet(hint: Record<string, unknown>): string {
  const args = Object.entries(hint)
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ");
  return `TraceListQuery(${args})`;
}

/** Review decisions are only submittable while the run waits at the gate. */
export function canSubmitReview(state: string | null | undefined): boolean {
  return state === "waiting_for_review";
}

/** Terminal lifecycle states: no further operations apply. */
export function isTerminal(state: string | null | undefined): boolean {
  return state === "completed" || state === "failed" || state === "cancelled";
}

/** Single-sourced phrasing for a capability-gated control the actor can't use. */
export function accessDeniedHint(action: string): string {
  return `Your access does not permit ${action}.`;
}

/** How the review form attributes the reviewer (#577 §5): when the control plane vouches for a
 * caller identity (`/meta.caller_identity`, non-null only under trusted proxy auth), that principal
 * IS the reviewer — the free-text field is replaced by a read-only attribution, so review records
 * carry the authenticated identity, not whatever was typed. Without one, free text stays (the
 * pre-identity behavior, and the only option for token/open auth). */
export interface ReviewerAttribution {
  /** The value to submit as the review's `reviewer`. */
  value: string | null;
  /** True when `value` is the trusted proxy principal (render read-only, not an input). */
  fromIdentity: boolean;
}

export function reviewerAttribution(
  callerIdentity: string | null | undefined,
  freeText: string,
): ReviewerAttribution {
  // The server already trims and nulls empty identities; the guard here is defensive only.
  if (callerIdentity != null && callerIdentity.trim().length > 0) {
    return { value: callerIdentity, fromIdentity: true };
  }
  return { value: freeText.trim() || null, fromIdentity: false };
}
