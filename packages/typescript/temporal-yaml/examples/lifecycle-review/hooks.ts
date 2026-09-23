/**
 * The deterministic normalization hooks ported from the Python example's
 * `activities.py` (`normalize_*`). Injected by activity name via
 * `assembleYamlRuntime`'s `hooks` option — the TS equivalent of the Python
 * `AIActivity(hook=...)` binding. Each runs AFTER output validation and returns
 * the corrected output, so ids/flags stay consistent regardless of the model.
 */

import type { ActivityHook } from "@typeflux/temporal";

import type { CaseInput, FinalDecision, ReviewPacket, RiskAssessment } from "./schemas.js";

const sortedUnique = (...groups: string[][]): string[] => [...new Set(groups.flat())].sort();

const normalizeAssessment: ActivityHook<CaseInput, RiskAssessment> = (input, output) => ({
  ...output,
  case_id: input.case_id,
  risk_level: output.risk_level || (input.risk_notes.length > 0 ? "high" : "medium"),
  flags: sortedUnique(input.risk_notes, output.flags),
});

const normalizeReviewPacket: ActivityHook<RiskAssessment, ReviewPacket> = (input, output) => ({
  ...output,
  case_id: input.case_id,
  approval_required: true,
  flags: sortedUnique(input.flags, output.flags),
});

const normalizeReviewRoute: ActivityHook<ReviewPacket, ReviewPacket> = (input, output) => ({
  ...output,
  case_id: input.case_id,
  approval_required: input.approval_required,
  flags: sortedUnique(input.flags, output.flags),
});

const normalizeFinalDecision: ActivityHook<ReviewPacket, FinalDecision> = (input, output) => ({
  ...output,
  case_id: input.case_id,
  decision: output.decision || "approved",
  approved: output.decision !== "rejected",
});

/** Keyed by activity `name` — the shape `assembleYamlRuntime({ hooks })` expects. */
export const hooks = {
  assess_case: normalizeAssessment,
  package_for_review: normalizeReviewPacket,
  prepare_submission: normalizeReviewRoute,
  route_to_department: normalizeReviewRoute,
  send_email: normalizeFinalDecision,
} as Record<string, ActivityHook<unknown, unknown>>;
