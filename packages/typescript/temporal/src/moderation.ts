/**
 * Moderation checkpoint (parity Epic 6, #453; Python #158). A user-supplied
 * moderator inspects an activity's validated (and hook-transformed) output and
 * returns a verdict; `onViolation` decides whether a flagged verdict blocks
 * (throws) or just flags (passes through). The moderator is a plain function —
 * no vendor dependency.
 */

/** The verdict a moderator returns. Safe to record as audit evidence (no raw output). */
export interface ModerationResult {
  flagged: boolean;
  categories?: string[];
  maxScore?: number;
  detail?: string;
}

/** Inspects a validated output and returns a verdict (sync or async). */
export type Moderator<Output> = (output: Output) => ModerationResult | Promise<ModerationResult>;

export interface ModerationConfig<Output> {
  moderator: Moderator<Output>;
  /** Action on a flagged verdict: `block` throws, `flag` passes through. Default `block`. */
  onViolation?: "block" | "flag";
}

/** Raised when a moderation checkpoint blocks an activity's output. Terminal — the
 * same output reproduces it, so a worker should treat it as non-retryable. */
export class ModerationBlockedError extends Error {
  readonly activityName: string;
  readonly categories: string[];

  /** `reason` overrides the default message — used by a policy verdict escalation.
   * `||` (not `??`) so an empty reason falls back to the descriptive default. */
  constructor(activityName: string, result: ModerationResult, reason?: string) {
    const categories = result.categories ?? [];
    super(
      reason ||
        `moderation blocked output for activity ${JSON.stringify(activityName)}: ` +
          (categories.join(", ") || "unspecified"),
    );
    this.name = "ModerationBlockedError";
    this.activityName = activityName;
    this.categories = categories;
  }
}

/**
 * A policy verdict-escalation hook (#454; Python `RuntimePolicyGuard.
 * moderation_policy_block`): applies the org's bar to the moderator's REPORTED
 * categories/score independent of the moderator's own `flagged` decision, and
 * returns a block reason when the policy forces a block (a disallowed category, or
 * a score at/over the policy threshold), else `undefined`. Can only tighten.
 */
export type ModerationPolicyBlock = (verdict: {
  activityName: string;
  categories: string[];
  maxScore: number | undefined;
}) => string | undefined;

/**
 * The moderation verdict recorded as redaction-exempt audit evidence on the
 * activity trace (#158/#454; Python `_record_moderation_verdict`) — the
 * CLASSIFICATION only (never the raw output). Snake_case mirrors Python's emitted
 * `typeflux_moderation` metadata for cross-SDK trace consistency (the
 * `typeflux_moderation.*` path is preserved by redaction's DEFAULT_EXCLUDED_PATHS).
 */
export interface ModerationVerdict {
  decision: "allow" | "flag" | "block";
  categories: string[];
  /** `null` (not undefined) when the moderator reports no score — JSON-stable and
   * present in the trace, matching Python's `max_score: None → null`. */
  max_score: number | null;
  moderator: string;
}

/**
 * Run the moderator on `output`, apply an optional policy escalation, then
 * `onViolation`. Returns the output unchanged when it passes (not flagged, or
 * flagged with `flag`, and the policy did not escalate); throws
 * `ModerationBlockedError` when the policy blocks or a flagged verdict is `block`.
 * `recordVerdict` (if given) receives the classification on EVERY path — recorded
 * before any throw, so a blocked output still lands on the trace.
 */
export async function applyModeration<Output>(
  activityName: string,
  moderation: ModerationConfig<Output> | undefined,
  output: Output,
  moderationPolicyBlock?: ModerationPolicyBlock,
  recordVerdict?: (verdict: ModerationVerdict) => void,
): Promise<Output> {
  if (moderation === undefined) {
    return output;
  }
  const result = await moderation.moderator(output);
  // Policy escalation FIRST (#454): a disallowed category / at-threshold score
  // blocks even a `flag` activity, or output a lenient moderator cleared
  // (flagged=false) — the policy can only tighten (Python `moderation_policy_block`).
  const reason = moderationPolicyBlock?.({
    activityName,
    categories: result.categories ?? [],
    maxScore: result.maxScore,
  });
  // Fail closed: a flagged verdict blocks unless the action is EXPLICITLY "flag"
  // (matches Python `_apply_moderation_verdict`). An unknown/typo'd action from
  // untyped config (e.g. "blok") therefore blocks rather than silently passing.
  const flaggedBlock = result.flagged && moderation.onViolation !== "flag";
  // Record BEFORE enforcing (Python `_record_moderation_verdict` runs before the
  // raise), so a blocked verdict is still on the trace. The moderator function's
  // name is audit context; an anonymous moderator falls back to "moderator".
  recordVerdict?.({
    decision: reason !== undefined || flaggedBlock ? "block" : result.flagged ? "flag" : "allow",
    categories: result.categories ?? [],
    // `?? null` (not left undefined): JSON serialization drops undefined, so the
    // trace would lose the field — a 0 score is preserved, absent becomes null.
    max_score: result.maxScore ?? null,
    moderator: moderation.moderator.name || "moderator",
  });
  if (reason !== undefined) {
    throw new ModerationBlockedError(activityName, result, reason);
  }
  if (flaggedBlock) {
    throw new ModerationBlockedError(activityName, result);
  }
  return output;
}
