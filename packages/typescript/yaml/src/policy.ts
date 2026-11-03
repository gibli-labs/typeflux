/**
 * Project policy spec (governance parity, #454; Python `project/policy.py`
 * `TypefluxProjectPolicySpec`). An operator-authored policy that governs the
 * inference envelope: which providers/models, redaction/observability floors,
 * Temporal posture, provider retry/limit ceilings, registry hosts, artifact
 * sources, review requirements, semantic-moderation requirements, secret
 * references, and import allowances.
 *
 * This module is the strict SPEC + loader — the parity of Python's policy model.
 * Policy COMPOSITION (`extends` closure, most-restrictive merge) and a content
 * hash are the next increment; ENFORCEMENT (wiring the loaded policy into the
 * runtime's guard seams) follows. Parity is BEHAVIORAL — both SDKs make the same
 * governance decisions; the content hash needs only internal determinism (drift
 * detection), not byte-identity with Python (single-language-per-deployment).
 *
 * Strict throughout (Python `extra="forbid"`): an unknown key is a typo that
 * would silently widen a governance allowance, so it fails loudly.
 */

import { parseDocument } from "yaml";
import { z } from "zod";

import { MAX_PARALLEL_NESTING_DEPTH } from "./build-workflow.js";
import { assertSafeKeys } from "./overrides.js";
import { RISK_TIER_ORDER } from "./spec.js";

/** Non-empty AND trimmed (Python `_validate_non_empty_string`). */
const trimmedNonEmpty = (field: string) =>
  z.string().refine((s) => s.length > 0 && s.trim() === s, {
    message: `${field} must be non-empty and trimmed`,
  });

const stringList = (field: string) => z.array(trimmedNonEmpty(field));

/**
 * A non-empty, trimmed record KEY (Python applies `_validate_non_empty_string` to
 * provider/model/address keys). Without this a typo'd key like `" openai"` yields
 * a silently-INEFFECTIVE policy entry — fail-closed, but a real governance footgun
 * for the operator, so reject it loudly.
 */
const nonEmptyKey = z.string().refine((s) => s.length > 0 && s.trim() === s, {
  message: "policy record key must be non-empty and trimmed",
});

/** A local project reference — no path/URL separators (Python `_validate_policy_id`). */
const policyId = z
  .string()
  .refine((s) => s.length > 0 && s.trim() === s, { message: "policy id must be non-empty and trimmed" })
  .refine((s) => !/[/\\:]/.test(s), {
    message: "policy ids must be local project references, not paths or URLs",
  });

export const ARTIFACT_SOURCE_POLICIES = ["local_path", "url", "object_uri", "provider_file"] as const;
export const OBSERVABILITY_BACKEND_POLICIES = ["none", "langfuse"] as const;
export const REVIEW_INVALID_DECISION_POLICIES = ["warn", "fail"] as const;

const providerAllowanceSpec = z
  .object({
    models: stringList("providers.allowed.<provider>.models").nullable().optional(),
    base_urls: stringList("providers.allowed.<provider>.base_urls").nullable().optional(),
  })
  .strict();

const providersSpec = z
  .object({
    // A provider allowlist keyed by provider name (non-empty keys). null ⇒ unset.
    allowed: z.record(nonEmptyKey, providerAllowanceSpec).nullable().optional(),
  })
  .strict();

const redactionSpec = z
  .object({
    required: z.boolean().nullable().optional(),
    preserve_typeflux_metadata: z.boolean().nullable().optional(),
    // Named custom rules (#188 D188-4) a workflow's redaction config MUST declare. A plain
    // (non-allowlist) list, so composed policies UNION the required names; missing → fail-closed.
    require_custom_rules: stringList("observability.redaction.require_custom_rules")
      .nullable()
      .optional(),
  })
  .strict();

const observabilitySpec = z
  .object({
    required: z.boolean().nullable().optional(),
    allowed_backends: z.array(z.enum(OBSERVABILITY_BACKEND_POLICIES)).nullable().optional(),
    redaction: redactionSpec.prefault({}),
  })
  .strict();

const temporalSpec = z
  .object({
    allowed_addresses: stringList("runtime.temporal allow-list").nullable().optional(),
    allowed_namespaces: stringList("runtime.temporal allow-list").nullable().optional(),
    allowed_regions: stringList("runtime.temporal allow-list").nullable().optional(),
    address_regions: z
      .record(nonEmptyKey, trimmedNonEmpty("runtime.temporal.address_regions region"))
      .nullable()
      .optional(),
    require_tls: z.boolean().nullable().optional(),
    require_api_key: z.boolean().nullable().optional(),
    // Reject a workflow whose `runtime.temporal.payload_codec` is absent (#188 D188-2) — the
    // fail-closed sibling of require_tls/require_api_key; OR-merges across composed policies.
    require_payload_codec: z.boolean().nullable().optional(),
  })
  .strict();

const providerRetrySpec = z
  .object({
    max_attempts: z.number().int().min(1, "runtime.provider_retry.max_attempts must be >= 1").nullable().optional(),
    initial_backoff_seconds: z
      .number()
      .min(0, "runtime.provider_retry backoff values must be >= 0")
      .nullable()
      .optional(),
    max_backoff_seconds: z
      .number()
      .min(0, "runtime.provider_retry backoff values must be >= 0")
      .nullable()
      .optional(),
    backoff_multiplier: z
      .number()
      .min(1, "runtime.provider_retry.backoff_multiplier must be >= 1")
      .nullable()
      .optional(),
    retry_rate_limits: z.boolean().nullable().optional(),
    retry_transient_errors: z.boolean().nullable().optional(),
  })
  .strict();

const providerCallLimitsSpec = z
  .object({
    max_concurrent: z
      .number()
      .int()
      .min(1, "runtime.provider_limits max_concurrent must be >= 1")
      .nullable()
      .optional(),
    min_interval_seconds: z
      .number()
      .min(0, "runtime.provider_limits min_interval_seconds must be >= 0")
      .nullable()
      .optional(),
  })
  .strict();

const providerRateLimitProviderSpec = z
  .object({
    limits: providerCallLimitsSpec.nullable().optional(),
    models: z.record(nonEmptyKey, providerCallLimitsSpec).default({}),
  })
  .strict();

const providerLimitsSpec = z
  .object({
    default: providerCallLimitsSpec.nullable().optional(),
    providers: z.record(nonEmptyKey, providerRateLimitProviderSpec).default({}),
  })
  .strict();

const registrySpec = z
  .object({
    allowed_hosts: stringList("runtime.registry.allowed_hosts").nullable().optional(),
  })
  .strict();

const runtimeSpec = z
  .object({
    temporal: temporalSpec.prefault({}),
    registry: registrySpec.prefault({}),
    provider_retry: providerRetrySpec.nullable().optional(),
    provider_limits: providerLimitsSpec.nullable().optional(),
  })
  .strict();

const artifactsSpec = z
  .object({
    allowed_sources: z.array(z.enum(ARTIFACT_SOURCE_POLICIES)).nullable().optional(),
    allowed_media_types: stringList("artifacts.allowed_media_types").nullable().optional(),
    max_bytes: z.number().int().min(0, "artifacts.max_bytes must be >= 0").nullable().optional(),
  })
  .strict();

const reviewSpec = z
  .object({
    require_review_routes: z.boolean().nullable().optional(),
    invalid_user_decision: z.enum(REVIEW_INVALID_DECISION_POLICIES).nullable().optional(),
  })
  .strict();

const semanticsSpec = z
  .object({
    required: z.boolean().nullable().optional(),
    require_block: z.boolean().nullable().optional(),
    categories: stringList("semantics.categories").nullable().optional(),
    score_threshold: z
      .number()
      .min(0, "semantics.score_threshold must be between 0.0 and 1.0")
      .max(1, "semantics.score_threshold must be between 0.0 and 1.0")
      .nullable()
      .optional(),
  })
  .strict();

const secretsSpec = z
  .object({
    require_secret_references: z.boolean().nullable().optional(),
  })
  .strict();

const importsSpec = z
  .object({
    allow_absolute_activity_modules: z.boolean().nullable().optional(),
    allow_provider_class: z.boolean().nullable().optional(),
    allow_registry_class: z.boolean().nullable().optional(),
    allow_observability_class: z.boolean().nullable().optional(),
    allow_moderator_callable: z.boolean().nullable().optional(),
    allowed_module_roots: stringList("imports.allowed_module_roots").nullable().optional(),
  })
  .strict();

/**
 * Composition ceilings on a workflow's graph shape (#298 Phase A, the #55 §9
 * handoff; Python `PolicyCompositionSpec`). Pure tree-walk checks that land at every
 * admission point through the same pipeline as every other dimension. Scope of each
 * knob: `max_steps` / `max_parallel_width` / `max_parallel_nesting` are PER WORKFLOW
 * — evaluated against each closure member individually under the parent's composed
 * policy; `max_total_steps` (flattened sum over the whole sub-workflow closure) and
 * `max_subworkflow_depth` (reference-tree depth) are TREE-WIDE, enforced by the
 * closure walk. `max_parallel_nesting` may only tighten the in-spec hard ceiling
 * (the canonical `MAX_PARALLEL_NESTING_DEPTH` from build-workflow; a value above it
 * is a policy-load error).
 *
 * Every ceiling is >= 1 by contract: 0 is never a silent no-op — to forbid
 * `map.workflow` fan-out use `allow_map_over_workflow: false`, and to forbid a
 * workflow entirely omit it from the validation targets.
 */
const CEILING_MIN_MESSAGE =
  "composition ceilings must be >= 1 (a zero ceiling is not expressible: use allow_map_over_workflow: false to forbid map-over-workflow fan-out, or omit the workflow from validation targets to forbid it entirely)";

const compositionSpec = z
  .object({
    max_steps: z.number().int().min(1, CEILING_MIN_MESSAGE).nullable().optional(),
    max_total_steps: z.number().int().min(1, CEILING_MIN_MESSAGE).nullable().optional(),
    max_parallel_width: z.number().int().min(1, CEILING_MIN_MESSAGE).nullable().optional(),
    max_parallel_nesting: z
      .number()
      .int()
      .min(1, CEILING_MIN_MESSAGE)
      .max(
        MAX_PARALLEL_NESTING_DEPTH,
        `composition.max_parallel_nesting may only tighten the in-spec ceiling of ${MAX_PARALLEL_NESTING_DEPTH}`,
      )
      .nullable()
      .optional(),
    max_subworkflow_depth: z.number().int().min(1, CEILING_MIN_MESSAGE).nullable().optional(),
    allow_map_over_workflow: z.boolean().nullable().optional(),
  })
  .strict();

/**
 * A per-tier provider allowance (#300 D300-6; Python
 * `PolicyRiskTierProviderAllowanceSpec`): `{models?: [...]}` — `providers.allowed`'s
 * value shape restricted to `models`, evaluated through the EXACT provider/model
 * predicate so the two levels can never drift. `base_urls` is deliberately absent:
 * the macro checks provider+model; accepting a knob it never evaluates would be a
 * silent fail-open.
 */
const riskTierProviderAllowanceSpec = z
  .object({
    models: stringList("risk_tiers.<tier>.constrain_providers.<provider>.models").nullable().optional(),
  })
  .strict();

/**
 * The requirements one risk tier expands to (#300 D300-4; Python
 * `PolicyRiskTierRequirementsSpec`), the MACRO body. Each knob maps to an EXISTING
 * control so a tier is a named macro over checks already in place: `require_review`
 * → a lifecycle review gate, `require_moderation` → every activity declares
 * moderation, `require_redaction` → observability redaction on, `constrain_providers`
 * → the workflow's provider AND model must pass this per-tier allow-list (D300-6:
 * the same mapping shape + predicate as `providers.allowed`), `require_compensation` →
 * every `side_effecting` activity STEP declares `compensate:` (#299 D299-5, the saga
 * guard). `require_*` OR-merge on composition; `constrain_providers` INTERSECTS like
 * `providers.allowed` (shared provider keys survive, their `models` lists intersect).
 */
const riskTierRequirementsSpec = z
  .object({
    require_review: z.boolean().nullable().optional(),
    require_moderation: z.boolean().nullable().optional(),
    require_redaction: z.boolean().nullable().optional(),
    require_payload_codec: z.boolean().nullable().optional(),
    require_compensation: z.boolean().nullable().optional(),
    constrain_providers: z.record(nonEmptyKey, riskTierProviderAllowanceSpec).nullable().optional(),
  })
  .strict();

/**
 * The `risk_tiers` policy dimension (#300; Python `PolicyRiskTiersSpec`): the
 * DEFINITION half of risk tiers. A workflow DECLARES its tier; this policy DEFINES
 * what each requires and the floor. `min_tier` is a floor — every workflow is
 * evaluated at `max(declared, min_tier)` (a floor lifts, never errors) and merges to
 * the HIGHEST tier across composed policies (an ordered-enum rule beside the numeric
 * most-restrictive table). `require_declared` additionally rejects an undeclared
 * workflow. Reaching `prohibited` as the effective tier denies admission outright.
 */
const riskTiersSpec = z
  .object({
    min_tier: z.enum(RISK_TIER_ORDER).nullable().optional(),
    require_declared: z.boolean().nullable().optional(),
    safe: riskTierRequirementsSpec.prefault({}),
    policy_gated: riskTierRequirementsSpec.prefault({}),
    human_gated: riskTierRequirementsSpec.prefault({}),
    prohibited: riskTierRequirementsSpec.prefault({}),
  })
  .strict();

/**
 * A project policy document (Python `TypefluxProjectPolicySpec`). `extends` names
 * other local policy ids to compose with (resolution is the next increment); the
 * per-block sub-specs default to empty so a policy declares only what it governs.
 */
export const typefluxProjectPolicySpec = z
  .object({
    version: z.literal("1").default("1"),
    name: trimmedNonEmpty("policy name"),
    description: trimmedNonEmpty("policy description").nullable().optional(),
    extends: z.array(policyId).default([]),
    providers: providersSpec.prefault({}),
    observability: observabilitySpec.prefault({}),
    runtime: runtimeSpec.prefault({}),
    artifacts: artifactsSpec.prefault({}),
    review: reviewSpec.prefault({}),
    semantics: semanticsSpec.prefault({}),
    imports: importsSpec.prefault({}),
    secrets: secretsSpec.prefault({}),
    composition: compositionSpec.prefault({}),
    risk_tiers: riskTiersSpec.prefault({}),
  })
  .strict();

export type TypefluxProjectPolicySpec = z.infer<typeof typefluxProjectPolicySpec>;

/** Bytes/alias bounds for operator-trusted policy files (parity with the YAML loader). */
const MAX_POLICY_BYTES = 1024 * 1024;
const MAX_POLICY_ALIASES = 1000;

export interface LoadPolicySpecOptions {
  /** A label for error messages (e.g. the policy file path). */
  sourceLabel?: string;
}

/**
 * Parse + strict-validate a project policy document (Python `load_project_policy`,
 * the model half). Duplicate keys are rejected and size/alias expansion is bounded,
 * exactly like the workflow-spec loader. Policy allowlists are literal — NOT
 * env-interpolated (a `${VAR}` in an allowlist would be a footgun, not a feature).
 */
export function loadPolicySpec(text: string, options: LoadPolicySpecOptions = {}): TypefluxProjectPolicySpec {
  const sourceLabel = options.sourceLabel ?? "policy";
  if (Buffer.byteLength(text, "utf-8") > MAX_POLICY_BYTES) {
    throw new Error(`policy document exceeds the ${MAX_POLICY_BYTES} byte limit (${sourceLabel})`);
  }
  const doc = parseDocument(text, { uniqueKeys: true, merge: true });
  // Duplicate mapping keys must FAIL, not silently last-win (a policy would then
  // govern against a different effective document than the author read). The yaml
  // lib surfaces them as warnings even with uniqueKeys, so filter both — matching
  // loadYamlSpec (Bugbot: governance integrity).
  const fatal = [...doc.errors, ...doc.warnings.filter((w) => w.code === "DUPLICATE_KEY")];
  if (fatal.length > 0) {
    throw new Error(`invalid policy YAML (${sourceLabel}): ${fatal[0]?.message ?? "parse error"}`);
  }
  const raw: unknown = doc.toJS({ maxAliasCount: MAX_POLICY_ALIASES });
  if (raw === null || raw === undefined) {
    throw new Error(`empty policy document: ${sourceLabel}`);
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new TypeError(`project policy must be a mapping (${sourceLabel})`);
  }
  // Reject an own `__proto__` key that zod's strict schema would silently IGNORE
  // rather than reject — Python's `extra=forbid` rejects it, and `loadYamlSpec` guards
  // the same class (`constructor`/`prototype` are already rejected by strict itself).
  assertSafeKeys(raw, sourceLabel);
  const result = typefluxProjectPolicySpec.safeParse(raw);
  if (!result.success) {
    throw new Error(`invalid project policy (${sourceLabel}):\n${z.prettifyError(result.error)}`);
  }
  return result.data;
}
