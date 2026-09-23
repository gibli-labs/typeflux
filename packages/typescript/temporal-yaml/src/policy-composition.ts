/**
 * Project policy composition (governance parity, #454; Python `project/policy.py`
 * `compose_project_policies` + merge helpers). Layers policies (org → tenant →
 * environment) into one effective policy with a **monotonic most-restrictive
 * merge**: composition can only TIGHTEN a guardrail, never loosen it. Allow-lists
 * INTERSECT, required-flags OR, allow-flags AND, and bounded numerics take the
 * stricter side; a genuine conflict (two disjoint allow-lists, or contradictory
 * scalars with no restrictive direction) fails loudly.
 *
 * This is the pure MERGE over an already-resolved policy set — the `extends`
 * closure resolution (which needs the project's policy registry) is the project
 * layer. Parity is BEHAVIORAL: the same layered policies yield the same effective
 * constraints and the same enforcement decisions. `policyHash` is
 * internally-deterministic (drift detection); a future cross-language pin is a
 * localized change to that one function (match Python's serialization + a golden).
 */

import { createHash } from "node:crypto";

import { canonicalJson } from "@typeflux/temporal";

import type { TypefluxProjectPolicySpec } from "./policy.js";
import { RISK_TIER_ORDER } from "./spec.js";

export class ProjectPolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProjectPolicyError";
  }
}

/** List fields treated as allow-lists (INTERSECTED on merge), not additive sets. */
const ALLOWLIST_FIELD_NAMES = new Set([
  "allowed_addresses",
  "allowed_backends",
  "allowed_hosts",
  "allowed_media_types",
  "allowed_module_roots",
  "allowed_namespaces",
  "allowed_regions",
  "allowed_sources",
  "base_urls",
  "models",
]);

/**
 * Ordered-enum scalars whose conflicting values merge to the STRICTEST (highest-ranked)
 * member instead of hard-failing — the sibling of the numeric most-restrictive table for
 * closed, totally-ordered vocabularies (#300; Python `_MOST_RESTRICTIVE_ORDERED_ENUMS`).
 * For `min_tier` a higher risk tier is the stricter floor, so composing takes the max by
 * `RISK_TIER_ORDER` rank.
 */
const MOST_RESTRICTIVE_ORDERED_ENUM: Record<string, readonly string[]> = {
  min_tier: RISK_TIER_ORDER,
};

/** Numeric scalars with a monotonic "stricter" direction (Python parity). */
const MOST_RESTRICTIVE_NUMERIC: Record<string, "min" | "max"> = {
  max_bytes: "min",
  max_concurrent: "min",
  min_interval_seconds: "max",
  // A lower moderation threshold blocks more output, so it is the stricter one.
  score_threshold: "min",
  // Composition ceilings (#298): a lower ceiling is always the stricter one → min.
  // allow_map_over_workflow is a boolean and merges by `allow_` AND polarity (mergeBool).
  max_steps: "min",
  max_total_steps: "min",
  max_parallel_width: "min",
  max_parallel_nesting: "min",
  max_subworkflow_depth: "min",
};

type Json = unknown;
const isRecord = (v: Json): v is Record<string, Json> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const isEmptyRecord = (v: Json): boolean => isRecord(v) && Object.keys(v).length === 0;
const isEmptyArray = (v: Json): boolean => Array.isArray(v) && v.length === 0;
const lastSegment = (path: string): string => path.slice(path.lastIndexOf(".") + 1);
// bool is not a number here (policy booleans merge by polarity, not magnitude).
const isRealNumber = (v: Json): v is number => typeof v === "number" && Number.isFinite(v);

// ── drop-empty (Python `_drop_empty` + preserve rules) ──────────────────────

function preserveEmptyMapping(path: string[]): boolean {
  // An explicit empty `constrain_providers` (or an empty per-provider allowance under
  // it) is an intentional deny-all, exactly like `providers.allowed` (#300 D300-6) —
  // it must survive drop-empty or the constraint silently vanishes.
  return (
    (path.length === 2 && path[0] === "providers" && path[1] === "allowed") ||
    (path.length === 3 && path[0] === "providers" && path[1] === "allowed") ||
    path[path.length - 1] === "constrain_providers" ||
    (path.length >= 2 && path[path.length - 2] === "constrain_providers")
  );
}

function preserveEmptyAllowlist(path: string[]): boolean {
  return path.length > 0 && ALLOWLIST_FIELD_NAMES.has(path[path.length - 1]!);
}

function preserveEmptyValue(path: string[], value: Json): boolean {
  if (isEmptyRecord(value)) return preserveEmptyMapping(path);
  if (isEmptyArray(value)) return preserveEmptyAllowlist(path);
  return false;
}

/**
 * Recursively drop null / empty-object / empty-array entries so only
 * EXPLICITLY-SET constraints participate in the merge — EXCEPT an empty
 * `providers.allowed` (or an empty provider allowance) and an empty allow-list,
 * which are intentional deny-all signals and must survive.
 */
function dropEmpty(value: Json, path: string[] = []): Json {
  if (isRecord(value)) {
    const out: Record<string, Json> = {};
    for (const [key, item] of Object.entries(value)) {
      const cleaned = dropEmpty(item, [...path, key]);
      const empty = cleaned === null || cleaned === undefined || isEmptyRecord(cleaned) || isEmptyArray(cleaned);
      if (!empty || preserveEmptyValue([...path, key], cleaned)) {
        out[key] = cleaned;
      }
    }
    return out;
  }
  if (Array.isArray(value)) {
    return value.filter((item) => item !== null && item !== undefined).map((item) => dropEmpty(item, path));
  }
  return value;
}

// ── allow-list intersection (Python `_intersect_*`) ─────────────────────────

function dedupe(values: Json[]): Json[] {
  const out: Json[] = [];
  for (const v of values) {
    if (!out.some((existing) => existing === v || canonicalJson(existing) === canonicalJson(v))) {
      out.push(v);
    }
  }
  return out;
}

function sortedAllowlist(values: Json[]): Json[] {
  // Strings sort before non-strings; non-strings by canonical JSON (Python parity).
  return dedupe(values).sort((a, b) => {
    const ka: [number, string] = typeof a === "string" ? [0, a] : [1, canonicalJson(a)];
    const kb: [number, string] = typeof b === "string" ? [0, b] : [1, canonicalJson(b)];
    return ka[0] - kb[0] || (ka[1] < kb[1] ? -1 : ka[1] > kb[1] ? 1 : 0);
  });
}

function intersectAllowlist(left: Json[], right: Json[], path: string): Json[] {
  if (left.length === 0 || right.length === 0) return [];
  const effective = right.filter((v) => left.some((l) => l === v || canonicalJson(l) === canonicalJson(v)));
  if (effective.length === 0) {
    // Two non-empty allow-lists intersecting to empty are contradictory inputs.
    throw new ProjectPolicyError(`conflicting project policy allow-list at ${path}: no overlapping values`);
  }
  return sortedAllowlist(effective);
}

function intersectMappingAllowlist(
  left: Record<string, Json>,
  right: Record<string, Json>,
  path: string,
): Record<string, Json> {
  if (Object.keys(left).length === 0 || Object.keys(right).length === 0) return {};
  // Own keys only — `k in left` would match inherited Object.prototype members, so
  // a provider/model literally named "toString"/"constructor" would falsely
  // intersect (/code-review; same class as the enforcement fix).
  const shared = Object.keys(right).filter((k) => Object.hasOwn(left, k));
  if (shared.length === 0) {
    throw new ProjectPolicyError(`conflicting project policy allow-list at ${path}: no overlapping keys`);
  }
  const out: Record<string, Json> = {};
  for (const key of shared) {
    out[key] = mergePolicyValue(left[key], right[key], `${path}.${key}`);
  }
  return out;
}

// ── scalar merges (Python `_merge_most_restrictive_numeric` / `_merge_bool`) ─

function mergeNumeric(left: Json, right: Json, path: string): number | null {
  const field = lastSegment(path);
  // Own-key only — a field named "constructor" would otherwise resolve the
  // inherited member and defeat the `=== undefined` guard (unreachable under the
  // strict schema today, but consistent with the other prototype-key fixes).
  const direction = Object.hasOwn(MOST_RESTRICTIVE_NUMERIC, field) ? MOST_RESTRICTIVE_NUMERIC[field] : undefined;
  if (direction === undefined || !isRealNumber(left) || !isRealNumber(right)) return null;
  return direction === "min" ? Math.min(left, right) : Math.max(left, right);
}

function mergeBool(left: boolean, right: boolean, path: string): boolean | null {
  const field = lastSegment(path);
  if (field === "required" || field.startsWith("require_") || field.endsWith("_required")) {
    return left || right; // a requirement, once imposed, stays imposed
  }
  if (field.startsWith("allow_") || field === "retry_rate_limits" || field === "retry_transient_errors") {
    return left && right; // an allowance survives only if BOTH layers permit it
  }
  return null;
}

// A closed ordered-enum scalar (min_tier) merges to the STRICTEST (highest-ranked) member.
function mergeOrderedEnum(left: Json, right: Json, path: string): string | null {
  const order = MOST_RESTRICTIVE_ORDERED_ENUM[lastSegment(path)];
  if (order === undefined || typeof left !== "string" || typeof right !== "string") return null;
  const li = order.indexOf(left);
  const ri = order.indexOf(right);
  if (li < 0 || ri < 0) return null;
  return li >= ri ? left : right;
}

// ── recursive merge (Python `_merge_policy_value` / `_merge_policy_payloads`) ─

function mergePolicyValue(left: Json, right: Json, path: string): Json {
  if (isRecord(left) && isRecord(right)) {
    // `constrain_providers` (#300 D300-6) shares `providers.allowed`'s mapping shape,
    // so it composes by the same mapping-allowlist INTERSECTION (shared provider keys
    // survive; their nested `models` lists intersect via the recursion).
    return path.endsWith("providers.allowed") || lastSegment(path) === "constrain_providers"
      ? intersectMappingAllowlist(left, right, path)
      : mergePolicyPayloads(left, right, path);
  }
  if (Array.isArray(left) && Array.isArray(right)) {
    return ALLOWLIST_FIELD_NAMES.has(lastSegment(path))
      ? intersectAllowlist(left, right, path)
      : sortedAllowlistOrDedupe(left, right);
  }
  if (typeof left === "boolean" && typeof right === "boolean") {
    const merged = mergeBool(left, right, path);
    if (merged !== null) return merged;
  }
  const numeric = mergeNumeric(left, right, path);
  if (numeric !== null) return numeric;
  const enumMerged = mergeOrderedEnum(left, right, path);
  if (enumMerged !== null) return enumMerged;
  if (left === right) return left;
  throw new ProjectPolicyError(
    `conflicting project policy values at ${path}: ${JSON.stringify(left)} != ${JSON.stringify(right)}`,
  );
}

// A non-allowlist list merges as a de-duplicated concatenation (Python `_dedupe_sequence`).
function sortedAllowlistOrDedupe(left: Json[], right: Json[]): Json[] {
  return dedupe([...left, ...right]);
}

function mergePolicyPayloads(
  left: Record<string, Json>,
  right: Record<string, Json>,
  path: string,
): Record<string, Json> {
  const merged: Record<string, Json> = { ...left };
  for (const [key, value] of Object.entries(right)) {
    const itemPath = `${path}.${key}`;
    // Object.hasOwn, not `in`: a policy key named like a prototype member
    // ("toString"/"constructor") must not falsely count as already-present.
    merged[key] = Object.hasOwn(merged, key) ? mergePolicyValue(merged[key], value, itemPath) : value;
  }
  return merged;
}

// ── public API ──────────────────────────────────────────────────────────────

export interface ComposedProjectPolicy {
  /** The policy ids originally requested for composition, in order. */
  selectedPolicyIds: string[];
  /** The resolved closure actually applied (deduped, parents first). */
  appliedPolicyIds: string[];
  /** The `name` of each applied policy, in applied order. */
  policyNames: string[];
  /** Internally-deterministic content hash of the effective payload (drift id). */
  policyHash: string;
  /** The effective (merged, drop-empty'd) policy constraints. */
  payload: Record<string, Json>;
}

/** One resolved policy in the closure: its registered id + validated spec. */
export interface AppliedPolicy {
  id: string;
  spec: TypefluxProjectPolicySpec;
}

/** The constraint payload of a single policy — only its explicitly-set governance. */
function policyConstraints(spec: TypefluxProjectPolicySpec): Record<string, Json> {
  const { version: _v, name: _n, description: _d, extends: _e, ...rest } = spec as Record<string, Json>;
  void _v;
  void _n;
  void _d;
  void _e;
  return dropEmpty(rest) as Record<string, Json>;
}

/**
 * Sort AND dedup the id-lists so the hash is the identity of the EFFECTIVE policy:
 * order-insensitive, and stable under a caller passing a duplicate selected id
 * (`[a, a]` and `[a]` compose to the same policy, so they must hash the same —
 * /code-review). applied_policy_ids is already deduped by the extends closure.
 */
function canonicalHashPayload(payload: Record<string, Json>): Record<string, Json> {
  const out = { ...payload };
  for (const key of ["selected_policy_ids", "applied_policy_ids", "policy_names"]) {
    if (Array.isArray(out[key])) out[key] = [...new Set(out[key] as string[])].sort();
  }
  return out;
}

/** The single hashing seam (behavioral/internally-deterministic; see module doc). */
function policyHash(payload: Record<string, Json>): string {
  return createHash("sha256").update(canonicalJson(canonicalHashPayload(payload)), "utf-8").digest("hex");
}

/**
 * Compose an already-resolved policy closure into one effective policy
 * (Python `compose_project_policies`, the merge half). `applied` is the closure
 * in apply order (parents before children); `selectedPolicyIds` is what was
 * originally requested. Throws `ProjectPolicyError` on a genuine conflict.
 */
export function composeProjectPolicies(
  applied: AppliedPolicy[],
  selectedPolicyIds: string[],
): ComposedProjectPolicy {
  if (selectedPolicyIds.length === 0) {
    throw new ProjectPolicyError("at least one project policy id is required for composition");
  }
  // The resolved closure must be non-empty for a non-empty request — an empty
  // `applied` would silently compose to a constraint-free (ungoverned) payload,
  // which for governance is fail-OPEN. Refuse it (finder review).
  if (applied.length === 0) {
    throw new ProjectPolicyError("no policies resolved for composition (empty applied closure)");
  }
  let merged: Record<string, Json> = {};
  for (const { id, spec } of applied) {
    merged = mergePolicyPayloads(merged, policyConstraints(spec), id);
  }
  const payload = dropEmpty({
    version: "1",
    selected_policy_ids: [...selectedPolicyIds],
    applied_policy_ids: applied.map((a) => a.id),
    policy_names: applied.map((a) => a.spec.name),
    ...merged,
  }) as Record<string, Json>;
  return {
    selectedPolicyIds: [...selectedPolicyIds],
    appliedPolicyIds: applied.map((a) => a.id),
    policyNames: applied.map((a) => a.spec.name),
    policyHash: policyHash(payload),
    payload,
  };
}
