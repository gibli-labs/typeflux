/**
 * Run-to-run manifest diff (#289): compare two executions of the same logical
 * workflow by their recorded manifest identity, classifying each difference by
 * severity. Contract/manifest-focused — never output-quality analytics.
 *
 * Input is the loose trace summary each `GET /correlation` returns (the
 * `TraceSummaryView` public dict: manifest_hash, workflow_contract_hash,
 * git_sha, policy hash, prompt_refs, provider_models, …). Pure; the page
 * renders the output.
 */

import type { Severity } from "./insights";

export type TraceSummary = Record<string, unknown>;

export interface RunDiffRow {
  field: string;
  label: string;
  severity: Severity;
  left: string;
  right: string;
}

interface FieldSpec {
  /** Dotted path into the trace summary. */
  path: string;
  label: string;
  severity: Severity;
}

// The manifest identity fields worth comparing, ordered by severity. Anything
// not listed (output, timing noise) is deliberately out of scope.
const FIELDS: FieldSpec[] = [
  // Version / governance drift — the run executed different pinned code.
  { path: "workflow_contract_hash", label: "Workflow contract hash", severity: "critical" },
  { path: "manifest_hash", label: "Manifest hash", severity: "critical" },
  { path: "git_sha", label: "Code sha", severity: "critical" },
  { path: "policy.policy_hash", label: "Policy hash", severity: "critical" },
  // Behavior may differ — same contract, different resolved inputs.
  { path: "provider_models", label: "Provider models", severity: "warning" },
  { path: "prompt_refs", label: "Prompt refs", severity: "warning" },
  { path: "activities", label: "Activities", severity: "warning" },
  { path: "environment", label: "Environment", severity: "warning" },
  { path: "git_ref", label: "Code ref", severity: "warning" },
  // Expected per-run divergence — shown for completeness, lowest severity.
  { path: "task_queue", label: "Task queue", severity: "info" },
  { path: "temporal_run_id", label: "Temporal run id", severity: "info" },
];

function lookup(summary: TraceSummary, path: string): unknown {
  return path.split(".").reduce<unknown>((value, key) => {
    if (value && typeof value === "object") return (value as Record<string, unknown>)[key];
    return undefined;
  }, summary);
}

/** A stable string for a field value; arrays compare as sorted sets. */
function normalize(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).sort((a, b) => a.localeCompare(b)).join(", ") || "—";
  }
  return String(value);
}

/**
 * The manifest fields that differ between two runs, classified by severity and
 * ordered critical → warning → info. Identical runs yield an empty list.
 */
export function diffTraceSummaries(
  left: TraceSummary | null | undefined,
  right: TraceSummary | null | undefined,
): RunDiffRow[] {
  if (!left || !right) return [];
  const rows: RunDiffRow[] = [];
  for (const spec of FIELDS) {
    const leftValue = normalize(lookup(left, spec.path));
    const rightValue = normalize(lookup(right, spec.path));
    if (leftValue !== rightValue) {
      rows.push({
        field: spec.path,
        label: spec.label,
        severity: spec.severity,
        left: leftValue,
        right: rightValue,
      });
    }
  }
  return rows;
}
