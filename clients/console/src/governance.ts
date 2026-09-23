/**
 * Governance engine (#587): pure derivations for the Governance page —
 * policy coverage across (workflow, environment) from resolved bundles,
 * governance-gap feed rows (the `insights.ts` contract), and section-level
 * composition provenance across a policy's `extends` chain.
 *
 * Deliberately NOT here: leaf-level effective rule values. The backend
 * composes rules and `policy_hash` is the composed identity; re-deriving the
 * merge client-side would risk silently diverging from enforcement.
 */

import type { PolicyDefinition } from "./api";
import type { BundleCell } from "./queries";
import type { Insight } from "./insights";
import { sortInsights } from "./insights";

export type CoverageState = "covered" | "none" | "composition_failed" | "unresolvable";

export interface CoverageCell {
  state: CoverageState;
  appliedPolicyIds: string[];
  policyHash: string | null;
}

export interface CoverageRow {
  workflowId: string;
  byEnv: Record<string, CoverageCell>;
}

export function deriveGovernanceCoverage(cells: BundleCell[]): CoverageRow[] {
  const rows = new Map<string, CoverageRow>();
  for (const cell of cells) {
    const row = rows.get(cell.workflowId) ?? { workflowId: cell.workflowId, byEnv: {} };
    const policy = cell.bundle?.policy ?? null;
    let coverage: CoverageCell;
    if (!cell.bundle) {
      coverage = { state: "unresolvable", appliedPolicyIds: [], policyHash: null };
    } else if (!policy) {
      coverage = { state: "none", appliedPolicyIds: [], policyHash: null };
    } else if (policy.applied_policy_ids.length === 0) {
      coverage = {
        state: policy.selected_policy_ids.length > 0 ? "composition_failed" : "none",
        appliedPolicyIds: [],
        policyHash: null,
      };
    } else {
      coverage = {
        state: "covered",
        appliedPolicyIds: [...policy.applied_policy_ids],
        policyHash: policy.policy_hash || null,
      };
    }
    row.byEnv[cell.env] = coverage;
    rows.set(cell.workflowId, row);
  }
  return [...rows.values()].sort((a, b) => a.workflowId.localeCompare(b.workflowId));
}

/** Governance gaps as feed rows: unprotected and composition-failed cells. */
export function deriveGovernanceGaps(rows: CoverageRow[]): Insight[] {
  const insights: Insight[] = [];
  for (const row of rows) {
    for (const [env, cell] of Object.entries(row.byEnv)) {
      if (cell.state === "none") {
        insights.push({
          id: `governance:none:${row.workflowId}:${env}`,
          severity: "warning",
          title: `${row.workflowId} (${env}): no policy applied`,
          detail:
            "Provider, endpoint, observability, and worker constraints are unenforced in this " +
            "environment. Select a policy for the workflow (project manifest or validation target).",
          link: `#/workflows/${row.workflowId}?env=${env}&section=policy`,
        });
      } else if (cell.state === "composition_failed") {
        insights.push({
          id: `governance:composition:${row.workflowId}:${env}`,
          severity: "critical",
          title: `${row.workflowId} (${env}): policy composition failed`,
          detail:
            "The selected policies could not be composed, so nothing is enforced — the " +
            "conflict is in the validation checks.",
          link: `#/workflows/${row.workflowId}?env=${env}&section=validation`,
        });
      }
    }
  }
  return sortInsights(insights);
}

/** True when at least one cell resolved — i.e. coverage was actually assessed. */
export function hasAssessableCoverage(rows: CoverageRow[]): boolean {
  return rows.some((row) =>
    Object.values(row.byEnv).some((cell) => cell.state !== "unresolvable"),
  );
}

/** The rule sections the backend policy spec declares today (presentation order). */
export const KNOWN_RULE_SECTIONS = [
  "providers",
  "observability",
  "runtime",
  "artifacts",
  "review",
  "semantics",
  "imports",
  "secrets",
] as const;

export type RuleSectionKey = (typeof KNOWN_RULE_SECTIONS)[number];

export interface RuleSection {
  key: string;
  known: boolean;
  value: unknown;
}

/**
 * Split a policy's rules into known sections (presentation order) and an
 * unknown tail — a new backend rule kind degrades to JSON, never disappears.
 *
 * The definition payload is a plain model dump, so `null` means *unset*
 * (schema default) while an explicit `{}`/`[]` is a deliberate value — for
 * allowlists, deny-all, the single most restrictive rule a policy can carry
 * (the backend's composition preserves exactly these). Only unset values and
 * pure all-unset skeletons are dropped; explicit empties are always shown.
 */
export function policySections(rules: Record<string, unknown> | null | undefined): RuleSection[] {
  if (!rules) return [];
  const sections: RuleSection[] = [];
  const seen = new Set<string>();
  for (const key of KNOWN_RULE_SECTIONS) {
    seen.add(key);
    const value = rules[key];
    if (isUnsetRuleValue(value)) continue;
    sections.push({ key, known: true, value });
  }
  for (const [key, value] of Object.entries(rules)) {
    if (seen.has(key) || isUnsetRuleValue(value)) continue;
    sections.push({ key, known: false, value });
  }
  return sections;
}

/** Unset (null) or an all-unset default skeleton — never an explicit empty. */
export function isUnsetRuleValue(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (Array.isArray(value)) return false; // [] is an explicit deny-all
  if (typeof value === "object") {
    const values = Object.values(value as Record<string, unknown>);
    // {} is explicit; an object with keys is unset only if every leaf is.
    return values.length > 0 && values.every(isUnsetRuleValue);
  }
  return false;
}

/**
 * The composition chain for one policy: transitive `extends` ancestors first
 * (depth-first in declaration order), the policy itself last — the order in
 * which layers apply, so "later overrides earlier" reads left to right.
 * Cycles and unknown references are skipped (the backend rejects them at
 * validation; the console renders what it can).
 */
export function compositionChain(
  policies: Map<string, PolicyDefinition>,
  rootId: string,
): PolicyDefinition[] {
  const chain: PolicyDefinition[] = [];
  const visited = new Set<string>();
  const visit = (id: string) => {
    if (visited.has(id)) return;
    visited.add(id);
    const policy = policies.get(id);
    if (!policy) return;
    for (const parent of policy.extends ?? []) visit(parent);
    chain.push(policy);
  };
  visit(rootId);
  return chain;
}

export interface SectionProvenance {
  section: string;
  /** Chain members (composition order) that define this section. */
  definedBy: string[];
  /** More than one chain member defines it — later layers override earlier. */
  overridden: boolean;
}

/**
 * Section-level provenance across an `extends` chain (composition order:
 * ancestors first). Presence-based only — which layers speak to a section —
 * never the merged leaf values, which are the backend's job.
 */
export function extendsProvenance(chain: PolicyDefinition[]): SectionProvenance[] {
  const bySection = new Map<string, string[]>();
  for (const policy of chain) {
    for (const section of policySections(
      (policy.rules ?? null) as Record<string, unknown> | null,
    )) {
      const owners = bySection.get(section.key) ?? [];
      owners.push(policy.name);
      bySection.set(section.key, owners);
    }
  }
  const order = [...KNOWN_RULE_SECTIONS] as string[];
  return [...bySection.entries()]
    .sort(([a], [b]) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      return (ia === -1 ? order.length : ia) - (ib === -1 ? order.length : ib) || a.localeCompare(b);
    })
    .map(([section, definedBy]) => ({
      section,
      definedBy,
      overridden: definedBy.length > 1,
    }));
}
