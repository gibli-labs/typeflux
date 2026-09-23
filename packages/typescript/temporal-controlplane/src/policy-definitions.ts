/**
 * Policy DEFINITION projections for the control-plane explorer (governance parity, #563 slice 2b;
 * Python `project/definitions.py` `policy_definitions` / `policy_definition` + `PolicySummary` /
 * `PolicyDefinition`). Policies are config by construction (no secrets), so rules render in full.
 *
 * Injection note: the loaded policy specs come from the bundle `sources.policies` — a declared
 * policy with no supplied source is a bundle defect (422), the same status the resolved-workflow
 * projections use for a declared-but-unsourced reference.
 */

import {
  assertPolicyId,
  composeProjectPolicyIds,
  type ProjectBundleSources,
  typefluxProjectPolicySpec,
  type TypefluxProjectPolicySpec,
  type TypefluxProjectSpec,
} from "@typeflux/temporal-yaml";
import { z } from "zod";

import { ProjectControlPlaneError } from "./errors.js";

const errorMessage = (error: unknown): string => (error instanceof Error ? error.message : String(error));

const asc = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * A policy list entry (Python `PolicySummary`). The `/policies` route has NO `exclude_none`, so
 * `description` is ALWAYS present (`null` when the policy declares none) — not omitted.
 */
export interface ApiPolicySummary {
  id: string;
  name: string;
  description: string | null;
  path: string;
}

/**
 * A policy's full definition (Python `PolicyDefinition`). The `/policies/{id}` route HAS
 * `exclude_none`, so `description` and `policy_hash` are OMITTED when absent (the latter when
 * composition fails — the conflict surfaces via `validate()` instead).
 */
export interface ApiPolicyDefinition {
  id: string;
  name: string;
  description?: string;
  extends: string[];
  policy_hash?: string;
  /** The full rule rendering — policy rules are config, not secrets. */
  rules: Record<string, unknown>;
  used_by: string[];
}

/**
 * The loaded spec for a DECLARED policy, rejecting the same identity defects as the resolver /
 * Python `load_project_policy` (so a mis-filed policy is never listed or inspected as if valid), all 422:
 * an id that isn't a local reference (contains `/`, `\`, `:` — `loadProjectSpec`'s record key only
 * checks non-empty/trimmed, so this is NOT redundant), a missing source, or a spec whose own `name`
 * doesn't match the manifest id it's keyed under.
 */
function requireSourcedPolicy(sources: ProjectBundleSources, policyId: string): TypefluxProjectPolicySpec {
  try {
    // Python `_validate_policy_id` runs first — a path/URL-like id is a config defect, not a 200.
    assertPolicyId(policyId);
  } catch (error) {
    throw new ProjectControlPlaneError(errorMessage(error), 422);
  }
  const spec = Object.hasOwn(sources.policies, policyId) ? sources.policies[policyId] : undefined;
  if (spec === undefined) {
    throw new ProjectControlPlaneError(`policy source not provided for declared policy: ${policyId}`, 422);
  }
  if (spec.name !== policyId) {
    throw new ProjectControlPlaneError(
      `policy '${policyId}' name must match the project policy id: '${spec.name}'`,
      422,
    );
  }
  return spec;
}

/** The workflows a policy governs, via the validation targets that select it (Python `used_by`). */
function policyUsedBy(project: TypefluxProjectSpec, policyId: string): string[] {
  const used = new Set<string>();
  for (const target of Object.values(project.validation.targets)) {
    if (target.policies.includes(policyId)) {
      for (const workflowId of target.workflows) used.add(workflowId);
    }
  }
  return [...used].sort(asc);
}

/** List every declared policy (sorted by id) as a summary (Python `policy_definitions`). */
export function buildPolicySummaries(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
): ApiPolicySummary[] {
  return Object.keys(project.policies)
    .sort(asc)
    .map((policyId) => {
      const spec = requireSourcedPolicy(sources, policyId);
      return {
        id: policyId,
        name: spec.name,
        description: spec.description ?? null,
        // `policyId` is a key of `project.policies`, so the lookup is defined (noUncheckedIndexedAccess).
        path: project.policies[policyId] as string,
      };
    });
}

/**
 * Project one DECLARED policy into its full definition (Python `policy_definition`). The caller
 * must have already 404'd an undeclared id; a declared-but-unsourced policy → 422. `rules` is the
 * policy spec minus its identity fields (`version`/`name`/`description`/`extends`); `policy_hash`
 * is the composed single-policy closure hash (omitted when composition fails).
 */
/** The policy identity fields Python excludes from `rules` (`model_dump(exclude={...})`). */
const RULES_IDENTITY_FIELDS = new Set(["version", "name", "description", "extends"]);

/**
 * Render a parsed value as Python's NULL SKELETON (#571): Pydantic's `model_dump` (without
 * `exclude_none`) emits every unset `X | None = None` field as an explicit `null`, where the
 * zod-parsed object simply lacks the key. Walking the SCHEMA (not a hand-kept field list) fills
 * each absent leaf/subtree with `null`, so the nullable-field set can never drift from the spec
 * as it evolves — the fragility that had this deferred. An unset optional SUBTREE renders as one
 * `null` (Pydantic dumps `provider_retry: None` whole, not an inner skeleton).
 */
function nullMaterialized(schema: unknown, value: unknown): unknown {
  let core = schema;
  while (
    core instanceof z.ZodOptional ||
    core instanceof z.ZodNullable ||
    core instanceof z.ZodDefault ||
    core instanceof z.ZodPrefault
  ) {
    core = core instanceof z.ZodDefault || core instanceof z.ZodPrefault ? core.def.innerType : core.unwrap();
  }
  if (value === undefined || value === null) return null;
  if (core instanceof z.ZodObject && typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(core.shape).map(([key, field]) => [
        key,
        nullMaterialized(field, Object.hasOwn(record, key) ? record[key] : undefined),
      ]),
    );
  }
  if (core instanceof z.ZodRecord && typeof value === "object" && !Array.isArray(value)) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
        key,
        nullMaterialized(core.valueType, entry),
      ]),
    );
  }
  if (core instanceof z.ZodArray && Array.isArray(value)) {
    return value.map((entry) => nullMaterialized(core.element, entry));
  }
  // Scalar/enum leaves (and anything schema-opaque): deep-copy so a caller mutating the
  // response can't reach into the loaded spec.
  return structuredClone(value);
}

export function buildPolicyDefinition(
  project: TypefluxProjectSpec,
  sources: ProjectBundleSources,
  policyId: string,
): ApiPolicyDefinition {
  const spec = requireSourcedPolicy(sources, policyId);

  let policyHash: string | undefined;
  try {
    policyHash = composeProjectPolicyIds(project, sources, [policyId]).policyHash;
  } catch {
    // A composition conflict (e.g. an unresolvable `extends`) is reported by validate(); omit here.
    policyHash = undefined;
  }

  // The config subtrees minus the identity fields (Python `model_dump(exclude={...})`), rendered
  // as the same null skeleton Python's shallow `response_model_exclude_none` leaves intact over
  // the `rules` dict — so `/policies/{id}` payloads match across the two servers (#571).
  const rules = Object.fromEntries(
    Object.entries(typefluxProjectPolicySpec.shape)
      .filter(([key]) => !RULES_IDENTITY_FIELDS.has(key))
      .map(([key, field]) => [
        key,
        nullMaterialized(field, Object.hasOwn(spec, key) ? (spec as Record<string, unknown>)[key] : undefined),
      ]),
  );

  return {
    id: policyId,
    name: spec.name,
    ...(spec.description != null ? { description: spec.description } : {}),
    extends: [...spec.extends],
    ...(policyHash !== undefined ? { policy_hash: policyHash } : {}),
    rules,
    used_by: policyUsedBy(project, policyId),
  };
}
