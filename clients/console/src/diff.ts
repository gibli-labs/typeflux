/**
 * Manifest diff engine: compare the same workflow resolved against two
 * environments and classify every differing path by how much it matters.
 * The point is "what is *meaningfully* different between local and prod",
 * not a wall of JSON — version/governance drift is critical, provider and
 * component divergence is worth a look, address/env-file divergence is
 * expected.
 */

import type { Bundle } from "./api";
import type { Severity } from "./insights";

export interface DiffEntry {
  path: string;
  section: string;
  severity: Severity;
  kind: "changed" | "added" | "removed";
  left: unknown;
  right: unknown;
}

interface Rule {
  pattern: RegExp;
  severity: Severity;
}

/** First match wins; default is info ("expected to differ"). */
const SEVERITY_RULES: Rule[] = [
  // Version drift: the two environments run different workflow code shapes.
  { pattern: /^workflow\.(spec_digest|workflow_type|version_label)/, severity: "critical" },
  // Governance drift: different effective policy.
  { pattern: /^policy\.policy_hash/, severity: "critical" },
  { pattern: /^policy\.(applied_policy_ids|selected_policy_ids|policy_names)/, severity: "critical" },
  // A secret configured on one side but not the other.
  { pattern: /^secret_references\[\d+\]\.configured/, severity: "critical" },
  // Different model/provider behavior between environments.
  { pattern: /^runtime\.provider\./, severity: "warning" },
  { pattern: /^runtime\.provider_retry\./, severity: "warning" },
  { pattern: /^runtime\.registry\./, severity: "warning" },
  // Different component profile content.
  { pattern: /^components\[\d+\]\.(content_hash|id|name)/, severity: "warning" },
  { pattern: /^activities\[\d+\]\./, severity: "warning" },
  { pattern: /^steps\[\d+\]\./, severity: "warning" },
  { pattern: /^topology\./, severity: "warning" },
  { pattern: /^lifecycle\./, severity: "warning" },
  // Expected divergence: addresses, namespaces, env files, paths.
  { pattern: /^runtime\.temporal\./, severity: "info" },
  { pattern: /^environment\./, severity: "info" },
  { pattern: /^deployment_preview/, severity: "info" },
];

/** Paths that differ by construction and carry no signal. */
const IGNORED_PATHS: RegExp[] = [/^project\.manifest_path$/, /^workflow\.path$/];

export function classifyPath(path: string): Severity {
  for (const rule of SEVERITY_RULES) {
    if (rule.pattern.test(path)) {
      return rule.severity;
    }
  }
  return "info";
}

function sectionOf(path: string): string {
  const head = path.split(/[.[]/, 1)[0];
  return head || "bundle";
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function flatten(value: unknown, prefix: string, out: Map<string, unknown>): void {
  if (isPlainObject(value)) {
    const keys = Object.keys(value);
    if (keys.length === 0) {
      out.set(prefix, value);
      return;
    }
    for (const key of keys) {
      flatten(value[key], prefix ? `${prefix}.${key}` : key, out);
    }
    return;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      out.set(prefix, value);
      return;
    }
    value.forEach((item, index) => flatten(item, `${prefix}[${index}]`, out));
    return;
  }
  out.set(prefix, value);
}

export function diffBundles(left: Bundle, right: Bundle): DiffEntry[] {
  const leftPaths = new Map<string, unknown>();
  const rightPaths = new Map<string, unknown>();
  flatten(left, "", leftPaths);
  flatten(right, "", rightPaths);

  const allPaths = new Set([...leftPaths.keys(), ...rightPaths.keys()]);
  const entries: DiffEntry[] = [];
  for (const path of [...allPaths].sort()) {
    if (IGNORED_PATHS.some((pattern) => pattern.test(path))) {
      continue;
    }
    const inLeft = leftPaths.has(path);
    const inRight = rightPaths.has(path);
    const leftValue = leftPaths.get(path);
    const rightValue = rightPaths.get(path);
    if (inLeft && inRight && Object.is(leftValue, rightValue)) {
      continue;
    }
    if (inLeft && inRight && JSON.stringify(leftValue) === JSON.stringify(rightValue)) {
      continue;
    }
    entries.push({
      path,
      section: sectionOf(path),
      severity: classifyPath(path),
      kind: !inLeft ? "added" : !inRight ? "removed" : "changed",
      left: inLeft ? leftValue : undefined,
      right: inRight ? rightValue : undefined,
    });
  }
  return entries;
}

export interface DiffSummary {
  critical: number;
  warning: number;
  info: number;
}

export function summarizeDiff(entries: DiffEntry[]): DiffSummary {
  const summary: DiffSummary = { critical: 0, warning: 0, info: 0 };
  for (const entry of entries) {
    if (entry.severity === "critical") summary.critical += 1;
    else if (entry.severity === "warning") summary.warning += 1;
    else summary.info += 1;
  }
  return summary;
}
