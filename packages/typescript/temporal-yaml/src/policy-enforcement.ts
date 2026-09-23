/**
 * Project policy ENFORCEMENT (governance parity, #454; Python
 * `project/policy_enforcement.py`). Validates a workflow's resolved runtime
 * config against a composed policy, **fail-closed**: a provider/model/registry-
 * host/artifact-source the policy does not allow refuses to build.
 *
 * Ported against the loaded `TypefluxYamlSpec` (the resolved config for a
 * single-workflow project); when the project-bundle layer lands, this extends to
 * the richer multi-workflow shape. Each check SKIPS when the policy does not
 * constrain that dimension — a policy governs only what it declares.
 *
 * Two check families:
 *   • the deny-by-default ALLOW-LISTS — provider/model, registry host, artifacts;
 *   • the REQUIRE-SIDE posture — secrets, observability/redaction, Temporal
 *     connection, review routes, moderation semantics, provider retry, provider
 *     call limits.
 *
 * `runtime.imports` (Python's importlib policy) is a permanent divergence: the TS
 * SDK loads no modules — providers/registries/observers/moderators are injected —
 * so its `class:`/`modules:` surfaces are rejected at spec load (#496). An imports
 * policy is therefore reported SKIPPED (structurally satisfied), never enforced.
 */

import { secretReferenceRecords } from "./secret-references.js";
import { statSync } from "node:fs";

import type { ResolvedProviderCall } from "@typeflux/temporal";

import type { ComposedProjectPolicy } from "./policy-composition.js";
import { ProjectPolicyError } from "./policy-composition.js";
import { defaultModelForProviderType } from "./provider-from-spec.js";
import {
  RISK_TIER_ORDER,
  type ProviderLimitsSpec,
  type SecretValueSpec,
  type TypefluxYamlSpec,
  type WorkflowStepSpec,
} from "./spec.js";

export type PolicyCheckStatus = "passed" | "failed" | "skipped";

export interface PolicyValidationCheck {
  code: string;
  status: PolicyCheckStatus;
  message?: string;
  details?: Record<string, unknown>;
}

/** Raised when a workflow violates the selected project policy (fail-closed). */
export class ProjectPolicyEnforcementError extends ProjectPolicyError {
  readonly checks: PolicyValidationCheck[];
  constructor(message: string, checks: PolicyValidationCheck[]) {
    super(message);
    this.name = "ProjectPolicyEnforcementError";
    this.checks = checks;
  }
}

const passed = (code: string, details?: Record<string, unknown>): PolicyValidationCheck => ({
  code,
  status: "passed",
  ...(details !== undefined ? { details } : {}),
});
const failed = (code: string, message: string): PolicyValidationCheck => ({ code, status: "failed", message });
const skipped = (code: string, message: string): PolicyValidationCheck => ({ code, status: "skipped", message });

// ── payload navigation (Python `_mapping_at*` / `_list_at*`) ─────────────────

export type Payload = Record<string, unknown>;
const isRecord = (v: unknown): v is Payload => typeof v === "object" && v !== null && !Array.isArray(v);

function mappingAtOrNone(value: unknown, ...path: string[]): Payload | null {
  let current: unknown = value;
  for (const key of path) {
    if (!isRecord(current) || !Object.hasOwn(current, key)) return null;
    current = current[key];
  }
  return isRecord(current) ? current : null;
}

function listAtOrNone(value: unknown, ...path: string[]): unknown[] | null {
  let current: unknown = value;
  for (const key of path) {
    if (!isRecord(current) || !Object.hasOwn(current, key)) return null;
    current = current[key];
  }
  return Array.isArray(current) ? current : null;
}

// ── media-type matching (Python `_media_type_matches`) ──────────────────────

function mediaTypeMatches(candidate: string, pattern: string): boolean {
  if (pattern === "*/*") return true;
  if (candidate === pattern) return true;
  if (pattern.endsWith("/*") && !candidate.endsWith("/*")) return candidate.startsWith(pattern.slice(0, -1));
  if (candidate.endsWith("/*") && pattern.endsWith("/*")) return candidate === pattern;
  return false;
}
const mediaAllowed = (candidate: string, allowed: unknown[]): boolean =>
  allowed.some((p) => typeof p === "string" && mediaTypeMatches(candidate, p));

/**
 * The EFFECTIVE model the runtime will use — `provider.model`, else the
 * params-record model, else the provider-type default. Python's
 * `ProviderSpec._materialize_default_model` sets this at LOAD, so both the
 * provider/model allow-list AND the provider-limit tier selection see the same
 * concrete model; the TS spec leaves `model` optional, so resolve it here. Returns
 * undefined only for an unknown/custom type with no default (Python's
 * `DEFAULT_PROVIDER_MODELS.get(type) -> None`). Uses `||` (not `??`) so an
 * interpolated empty `model: ${MODEL:-}` falls through to the default.
 */
export function effectiveModelFor(provider: TypefluxYamlSpec["runtime"]["provider"]): string | undefined {
  return provider.model || (provider.params?.model ?? defaultModelForProviderType(provider.type));
}

/**
 * The models a call could OVERRIDE the provider model with, declared statically in
 * the spec: each inline-prompt `model` / prompt-params model, and each activity
 * `provider_params.model` (the executor's higher merge layers, build-activities.ts).
 * Python enforces per-call overrides in its RUNTIME guard; a static YAML spec lets
 * admission enumerate them up front. Over-approximates on purpose — a model
 * shadowed by a higher merge layer is still required to be allowed (fail-closed:
 * declare only models you may use).
 */
function collectOverrideModels(spec: TypefluxYamlSpec): Set<string> {
  const models = new Set<string>();
  for (const prompt of Object.values(spec.runtime.registry.prompts ?? {})) {
    if (typeof prompt === "object" && prompt !== null) {
      if (prompt.model) models.add(prompt.model);
      if (prompt.provider_params?.model !== undefined) models.add(prompt.provider_params.model);
    }
  }
  for (const definition of spec.activities.definitions ?? []) {
    if (definition.provider_params?.model !== undefined) models.add(definition.provider_params.model);
  }
  return models;
}

/**
 * The failure message for a single provider/model pair against `providers.allowed`
 * (Python `_provider_model_policy_failure`), or undefined when allowed / unconstrained.
 * Shared by admission ({@link validateProvider}) and the per-call runtime guard so
 * both apply the identical rule. `providerName` is a user-controlled key → own-keys only.
 */
function providerModelPolicyFailure(
  payload: Payload,
  providerName: string,
  providerModel: string | undefined,
): string | undefined {
  const allowed = mappingAtOrNone(payload, "providers", "allowed");
  if (allowed === null) return undefined;
  const providerPolicy = Object.hasOwn(allowed, providerName) ? allowed[providerName] : undefined;
  if (providerPolicy === undefined) {
    return `provider '${providerName}' is not allowed by selected project policy`;
  }
  const models = listAtOrNone(providerPolicy, "models");
  if (models !== null && !models.includes(providerModel)) {
    return `provider model '${providerModel ?? ""}' is not allowed for provider '${providerName}'`;
  }
  return undefined;
}

// ── validators ──────────────────────────────────────────────────────────────

/** Provider + model against `providers.allowed` (Python `_validate_provider`). */
function validateProvider(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const allowed = mappingAtOrNone(payload, "providers", "allowed");
  if (allowed === null) return skipped("policy_provider", "policy does not constrain provider/model");
  const provider = spec.runtime.provider;
  // The provider + its EFFECTIVE model (Python materializes the model before
  // comparing; own-keys lookup guards a prototype-named provider.type). The runtime
  // guard applies the same `providerModelPolicyFailure` per call.
  const effectiveModel = effectiveModelFor(provider);
  const failure = providerModelPolicyFailure(payload, provider.type, effectiveModel);
  if (failure !== undefined) {
    return failed("policy_provider", failure);
  }
  const providerPolicy = Object.hasOwn(allowed, provider.type) ? allowed[provider.type] : undefined;
  const models = providerPolicy !== undefined ? listAtOrNone(providerPolicy, "models") : null;
  if (models !== null) {
    // Every model a prompt/activity could OVERRIDE the provider model with — else the
    // allow-list is bypassed by pinning a disallowed model on an activity/inline
    // prompt (codex fail-open). The provider type is shared, so all run under it.
    for (const model of collectOverrideModels(spec)) {
      if (!models.includes(model)) {
        return failed("policy_provider", `provider model '${model}' is not allowed for provider '${provider.type}'`);
      }
    }
    // A backend registry with allow_prompt_model_override supplies a prompt model
    // at RUNTIME that admission cannot see; refuse rather than under-enforce a
    // model allow-list (an inline registry is fully static, so it is exempt).
    if (provider.allow_prompt_model_override && spec.runtime.registry.type !== "inline") {
      return failed(
        "policy_provider",
        "a model allow-list cannot be enforced with allow_prompt_model_override and a non-inline registry — pin models in the spec or disable allow_prompt_model_override",
      );
    }
  }
  // provider.base_url is a permanent TS divergence (injected transport), so the
  // base_url allow-list has nothing to validate here.
  return passed("policy_provider", { provider: provider.type, model: effectiveModel });
}

/** Prompt-registry host against `runtime.registry.allowed_hosts` (Python `_validate_registry`). */
function validateRegistry(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const registryPolicy = mappingAtOrNone(payload, "runtime", "registry");
  const allowedHosts = registryPolicy === null ? null : listAtOrNone(registryPolicy, "allowed_hosts");
  if (allowedHosts === null) return skipped("policy_registry", "policy does not constrain prompt registry");
  const registry = spec.runtime.registry;
  // Inline registries have no host; custom registries own their connection.
  if (registry.type === "inline" || registry.type === "custom") {
    return passed("policy_registry", { registry: registry.type });
  }
  // Env-interpolation already folded ${LANGFUSE_HOST}/… into registry.host at load.
  const host = registry.host;
  if (host === undefined) {
    return failed(
      "policy_registry",
      "policy constrains registry hosts, but no registry host is configured (set runtime.registry.host)",
    );
  }
  if (!allowedHosts.includes(host)) {
    return failed("policy_registry", `registry host '${host}' is not allowed by selected project policy`);
  }
  return passed("policy_registry", { registry: registry.type, host });
}

/** Artifact sources/media-types/max_bytes against `artifacts.*` (Python `_validate_artifacts`). */
function validateArtifacts(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const artifactPolicy = mappingAtOrNone(payload, "artifacts");
  if (artifactPolicy === null) return skipped("policy_artifacts", "policy does not constrain artifacts");
  const failures: string[] = [];
  const runtimeArtifacts = spec.runtime.artifacts;
  // Python's spec model materializes the artifacts block with defaults even when
  // the author omits it (allowed_sources → ["local_path"]). Match that, or a
  // deny-by-default SOURCE policy is bypassed by simply omitting runtime.artifacts
  // (Bugbot High — a fail-open).
  const runtimeSources = runtimeArtifacts?.allowed_sources ?? ["local_path"];
  const runtimeMedia = runtimeArtifacts?.allowed_media_types ?? [];
  const runtimeMaxBytes = runtimeArtifacts?.max_bytes;

  const allowedSources = listAtOrNone(artifactPolicy, "allowed_sources");
  if (allowedSources !== null) {
    for (const source of runtimeSources) {
      if (!allowedSources.includes(source)) failures.push(`artifact source '${source}' is not allowed`);
    }
  }
  const allowedMedia = listAtOrNone(artifactPolicy, "allowed_media_types");
  if (allowedMedia !== null) {
    if (runtimeMedia.length === 0) {
      failures.push("runtime.artifacts.allowed_media_types must constrain media types");
    }
    for (const media of runtimeMedia) {
      if (!mediaAllowed(media, allowedMedia)) failures.push(`artifact media type '${media}' is not allowed`);
    }
  }
  const maxBytes = artifactPolicy["max_bytes"];
  if (typeof maxBytes === "number") {
    if (runtimeMaxBytes === undefined || runtimeMaxBytes === null) {
      failures.push("runtime.artifacts.max_bytes must be configured");
    } else if (runtimeMaxBytes > maxBytes) {
      failures.push(`runtime.artifacts.max_bytes ${runtimeMaxBytes} exceeds ${maxBytes}`);
    }
  }

  // Per-activity artifact declarations.
  for (const definition of spec.activities.definitions ?? []) {
    for (const artifact of definition.artifacts ?? []) {
      if (allowedMedia !== null) {
        for (const media of artifact.media_types ?? []) {
          if (!mediaAllowed(media, allowedMedia)) {
            failures.push(
              `activity '${definition.name}' artifact '${artifact.name}' media type '${media}' is not allowed`,
            );
          }
        }
      }
      if (typeof maxBytes === "number" && artifact.max_bytes != null && artifact.max_bytes > maxBytes) {
        failures.push(
          `activity '${definition.name}' artifact '${artifact.name}' max_bytes ${artifact.max_bytes} exceeds ${maxBytes}`,
        );
      }
    }
  }

  return failures.length > 0 ? failed("policy_artifacts", failures.join("; ")) : passed("policy_artifacts");
}

// ── secret-reference resolution (Python `secret_value_configured`) ───────────

/**
 * Whether a credential slot is actually satisfied — a non-empty literal, or a
 * `value_from` reference whose env var / file resolves to non-empty content.
 * Mirrors Python `secret_value_configured`: a declared-but-unset reference is NOT
 * configured, so a `require_api_key` policy fails closed.
 */
export function secretValueConfigured(value: string | SecretValueSpec | undefined): boolean {
  if (value === undefined) return false;
  if (typeof value === "string") return value.length > 0;
  const { env, file } = value.value_from;
  if (env !== undefined) {
    const resolved = process.env[env];
    return resolved !== undefined && resolved.trim().length > 0;
  }
  if (file !== undefined) {
    try {
      const stat = statSync(file);
      return stat.isFile() && stat.size > 0;
    } catch {
      return false; // unreadable/absent → not configured (fail-closed, like Python's OSError guard)
    }
  }
  return false;
}

// ── require-side validators (Python `_validate_*`, the posture slice) ────────

/** Secret references required in place of literal credentials (Python `_validate_secrets`). */
function validateSecrets(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const secretsPolicy = mappingAtOrNone(payload, "secrets");
  if (secretsPolicy === null || secretsPolicy["require_secret_references"] !== true) {
    return skipped("policy_secrets", "policy does not require secret references");
  }
  const failures: string[] = [];
  // Env interpolation resolves BEFORE spec validation, so an interpolated
  // `${OPENAI_API_KEY}` is indistinguishable from a hardcoded literal here — both
  // are strings and both fail. The enforced contract is typed `value_from` only.
  // Driven by the shared secret-slot inventory (SECRET_SLOT_PATHS, Python parity #792/#793):
  // every slot the bundle reports — api keys AND observability credentials — is enforced by
  // the same walk; a slot cannot be inventoried yet escape this policy.
  for (const record of secretReferenceRecords(spec)) {
    if (record.source_kind === "literal") {
      failures.push(
        `${record.runtime_path} must use a value_from secret reference, not a literal credential value`,
      );
    }
  }
  return failures.length > 0
    ? failed("policy_secrets", failures.join("; "))
    : passed("policy_secrets", { require_secret_references: true });
}

/** Observability backend + redaction posture (Python `_validate_observability`). */
function validateObservability(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const obsPolicy = mappingAtOrNone(payload, "observability");
  if (obsPolicy === null) return skipped("policy_observability", "policy does not constrain observability");
  const failures: string[] = [];
  const observability = spec.runtime.observability;
  // `type or "none"` — an interpolated empty `""` type collapses to "none" (use
  // `||`, not `??`, for the empty-string parity that bit us before).
  const backend = observability?.type || "none";
  if (obsPolicy["required"] === true && backend === "none") {
    failures.push("observability backend is required by project policy");
  }
  const allowedBackends = listAtOrNone(obsPolicy, "allowed_backends");
  if (allowedBackends !== null && !allowedBackends.includes(backend)) {
    failures.push(`observability backend '${backend}' is not allowed`);
  }
  const redactionPolicy = mappingAtOrNone(obsPolicy, "redaction");
  if (redactionPolicy !== null) {
    // Python's RedactionSpec defaults `enabled`/`preserve_typeflux_metadata` to
    // True, so an unset field reads as ON — only an EXPLICIT `false` (backend
    // present, redaction turned off) fails. The backend `required` check above is
    // the gate for a workflow with no observability at all.
    const redactionEnabled = observability?.redaction?.enabled ?? true;
    const preserveMetadata = observability?.redaction?.preserve_typeflux_metadata ?? true;
    if (redactionPolicy["required"] === true && !redactionEnabled) {
      failures.push("observability redaction is required by project policy");
    }
    if (redactionPolicy["preserve_typeflux_metadata"] === true && !preserveMetadata) {
      failures.push("redaction must preserve typeflux metadata");
    }
    const requiredCustomRules = listAtOrNone(redactionPolicy, "require_custom_rules");
    if (requiredCustomRules !== null && requiredCustomRules.length > 0) {
      // Requiring named custom rules means requiring they actually RUN. `redactMetadata`
      // no-ops EVERYTHING when enabled is false, so a policy that names required rules while
      // redaction is turned off would report the control satisfied while PII flows plaintext.
      // `redactionEnabled` above is the same default-on-unless-explicit-false predicate.
      if (!redactionEnabled) {
        failures.push(
          `observability policy requires custom redaction rules [${[...requiredCustomRules]
            .map(String)
            .sort()
            .join(", ")}] but observability.redaction.enabled is false — required rules must actually run`,
        );
      }
      // Names are validated as strings by the policy zod (`stringList`); coerce the
      // untyped payload entries so the predicate compares like-for-like.
      const missing = missingCustomRedactionRules(spec, requiredCustomRules.map(String));
      if (missing.length > 0) {
        failures.push(
          `observability redaction is missing required custom rules: ${[...missing].sort().join(", ")}`,
        );
      }
    }
  }
  return failures.length > 0
    ? failed("policy_observability", failures.join("; "))
    : passed("policy_observability", { backend });
}

/** Temporal address/namespace/region/TLS/api-key posture (Python `_validate_temporal`). */
function validateTemporal(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const temporalPolicy = mappingAtOrNone(payload, "runtime", "temporal");
  if (temporalPolicy === null) return skipped("policy_temporal", "policy does not constrain Temporal");
  const failures: string[] = [];
  const temporal = spec.runtime.temporal;
  // Python's TemporalSpec materializes `address`/`namespace` defaults at load;
  // the TS spec leaves them optional, so materialize the SAME defaults here — else
  // a policy allow-listing "localhost:7233"/"default" would wrongly reject a spec
  // that simply omits the field (it connects to exactly those defaults).
  const address = temporal.address ?? "localhost:7233";
  const namespace = temporal.namespace ?? "default";
  let region = process.env["TYPEFLUX_TEMPORAL_REGION"];

  const allowedAddresses = listAtOrNone(temporalPolicy, "allowed_addresses");
  if (allowedAddresses !== null && !allowedAddresses.includes(address)) {
    failures.push(`Temporal address '${address}' is not allowed`);
  }
  const allowedNamespaces = listAtOrNone(temporalPolicy, "allowed_namespaces");
  if (allowedNamespaces !== null && !allowedNamespaces.includes(namespace)) {
    failures.push(`Temporal namespace '${namespace}' is not allowed`);
  }
  const allowedRegions = listAtOrNone(temporalPolicy, "allowed_regions");
  const addressRegions = mappingAtOrNone(temporalPolicy, "address_regions");
  if (addressRegions !== null) {
    // The address→region mapping is policy-authored truth; a self-attested
    // TYPEFLUX_TEMPORAL_REGION can only corroborate it (never override). `address`
    // is a user-controlled key → own-keys only.
    const mappedRegion = Object.hasOwn(addressRegions, address) ? addressRegions[address] : undefined;
    if (typeof mappedRegion !== "string") {
      failures.push(`Temporal address '${address}' has no region mapping in project policy`);
    } else {
      if (allowedRegions !== null && !allowedRegions.includes(mappedRegion)) {
        failures.push(`Temporal address '${address}' maps to region '${mappedRegion}', which is not allowed`);
      }
      if (region !== undefined && region !== mappedRegion) {
        failures.push(
          `self-attested Temporal region '${region}' conflicts with the policy mapping '${mappedRegion}' for address '${address}'`,
        );
      }
      region = mappedRegion;
    }
  } else if (allowedRegions !== null && !allowedRegions.includes(region)) {
    failures.push(`Temporal region '${region ?? ""}' is not allowed`);
  }
  // Python's TemporalSpec defaults `tls = False`, so `tls is not False` counts
  // only True / a TLS-options record as enabled — an UNSET field is disabled. The
  // TS spec leaves `tls` optional (undefined), so treat undefined as disabled too,
  // or a `require_tls` policy fails OPEN on a spec that omits tls.
  const tlsEnabled = temporal.tls !== undefined && temporal.tls !== false;
  if (temporalPolicy["require_tls"] === true && !tlsEnabled) {
    failures.push("Temporal TLS is required by project policy");
  }
  const apiKeyConfigured = secretValueConfigured(temporal.api_key);
  if (temporalPolicy["require_api_key"] === true && !apiKeyConfigured) {
    failures.push("Temporal API key is required by project policy");
  }
  const payloadCodecConfigured = payloadCodecConfiguredForSpec(spec);
  if (temporalPolicy["require_payload_codec"] === true && !payloadCodecConfigured) {
    failures.push(
      "Temporal payload codec is required by project policy but runtime.temporal.payload_codec is not declared",
    );
  }
  return failures.length > 0
    ? failed("policy_temporal", failures.join("; "))
    : passed("policy_temporal", {
        address,
        namespace,
        region,
        tls_enabled: tlsEnabled,
        api_key_configured: apiKeyConfigured,
        payload_codec_configured: payloadCodecConfigured,
      });
}

/**
 * Imports policy is structurally satisfied by the TS injection model (Python
 * `_validate_imports` has no honest analogue — there is no module loading to
 * police). SKIPPED rather than enforced (see the module header).
 */
function validateImports(_spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const importsPolicy = mappingAtOrNone(payload, "imports");
  if (importsPolicy === null) return skipped("policy_imports", "policy does not constrain imports");
  return skipped(
    "policy_imports",
    "runtime imports policy is not applicable to the TS SDK — providers/registries/observers/moderators are injected, not imported (#496), so there is no module-loading surface to police",
  );
}

/** Workflow lifecycle review routes (Python `_validate_review`). */
function validateReview(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const reviewPolicy = mappingAtOrNone(payload, "review");
  if (reviewPolicy === null) return skipped("policy_review", "policy does not constrain review routes");
  const failures: string[] = [];
  // The review gate only RUNS when the lifecycle is enabled — `workflowPlanFromSpec`
  // attaches the review plan only for `lifecycle.enabled === true` (build-workflow.ts).
  // A review block under a disabled (or omitted) lifecycle is inert, so it must NOT
  // satisfy a require_review_routes policy (codex fail-open).
  const lifecycle = spec.workflow.lifecycle;
  // The check quantifies over ALL gates (#55 §9): the single `review` and the named `gates`
  // list both count, so a multi-gate workflow satisfies require_review_routes and every gate
  // must honor the invalid_user_decision constraint. (Still only under an ENABLED lifecycle —
  // an inert block must not satisfy the policy; codex fail-open.)
  const singleReview = lifecycle?.enabled === true ? lifecycle.review : undefined;
  const gates = enabledReviewGates(spec);
  if (reviewPolicy["require_review_routes"] === true && gates.length === 0) {
    failures.push("workflow lifecycle review routes are required by project policy");
  }
  const invalidUserDecision = reviewPolicy["invalid_user_decision"];
  if (invalidUserDecision !== undefined) {
    if (gates.length === 0) {
      failures.push("workflow lifecycle review is required for invalid_user_decision");
    }
    for (const gate of gates) {
      if (gate.invalid_user_decision !== invalidUserDecision) {
        const label = singleReview !== undefined ? "workflow lifecycle review" : `workflow lifecycle gate '${gate.id}'`;
        failures.push(
          `${label} invalid_user_decision '${gate.invalid_user_decision}' does not match policy '${String(invalidUserDecision)}'`,
        );
      }
    }
  }
  return failures.length > 0 ? failed("policy_review", failures.join("; ")) : passed("policy_review");
}

/**
 * Config-level moderation posture (Python `_validate_semantics`): when the policy
 * requires moderation, every activity DEFINITION must declare it (and use
 * `on_violation: block` when the policy mandates fail-closed).
 *
 * `semantics.categories` / `semantics.score_threshold` VERDICT escalation is NOT
 * checkable at admission — it applies to the moderator's runtime OUTPUT — so, like
 * Python's `_validate_semantics`, this validator does not enforce them. Python
 * escalates them in `RuntimePolicyGuard.moderation_policy_block` at execution; the
 * TS runtime moderation guard is the next slice. Until then a policy that sets only
 * categories/threshold (no required/require_block) reports SKIPPED here.
 */
function validateSemantics(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const semanticsPolicy = mappingAtOrNone(payload, "semantics");
  if (semanticsPolicy === null) return skipped("policy_semantics", "policy does not constrain moderation");
  const requiresModeration = semanticsPolicy["required"] === true || semanticsPolicy["require_block"] === true;
  const requireBlock = semanticsPolicy["require_block"] === true;
  const definitions = spec.activities.definitions ?? [];
  if (!requiresModeration || definitions.length === 0) {
    return skipped("policy_semantics", "moderation policy is also enforced at the runtime output checkpoint");
  }
  const failures: string[] = [];
  const missing = new Set(activitiesMissingModeration(spec));
  for (const definition of definitions) {
    const moderation = definition.moderation;
    if (missing.has(definition.name)) {
      failures.push(`activity '${definition.name}' must declare moderation (required by selected project policy)`);
    } else if (moderation !== undefined && requireBlock && moderation.on_violation !== "block") {
      failures.push(
        `activity '${definition.name}' moderation must use on_violation='block' (required by selected project policy), not '${moderation.on_violation}'`,
      );
    }
  }
  return failures.length > 0
    ? failed("policy_semantics", failures.join("; "))
    : passed("policy_semantics", { activities: definitions.length });
}

/** In-activity provider retry bounds (Python `_validate_provider_retry`). */
function validateProviderRetry(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const retryPolicy = mappingAtOrNone(payload, "runtime", "provider_retry");
  if (retryPolicy === null) return skipped("policy_provider_retry", "policy does not constrain provider retry");
  const failures: string[] = [];
  const retry = spec.runtime.provider_retry;
  // No provider_retry (or max_attempts unset) → 1 attempt, matching Python's
  // yaml ProviderRetrySpec default (`max_attempts: int = 1`).
  const maxAttempts = retry?.max_attempts ?? 1;
  const policyMaxAttempts = retryPolicy["max_attempts"];
  if (typeof policyMaxAttempts === "number" && maxAttempts > policyMaxAttempts) {
    failures.push(`provider retry max_attempts ${maxAttempts} exceeds ${policyMaxAttempts}`);
  }
  const retryRateLimits = retry?.retry_rate_limits ?? true;
  if (retryPolicy["retry_rate_limits"] === false && retryRateLimits) {
    failures.push("provider retry_rate_limits is broader than project policy");
  }
  const retryTransientErrors = retry?.retry_transient_errors ?? true;
  if (retryPolicy["retry_transient_errors"] === false && retryTransientErrors) {
    failures.push("provider retry_transient_errors is broader than project policy");
  }
  return failures.length > 0 ? failed("policy_provider_retry", failures.join("; ")) : passed("policy_provider_retry");
}

// ── provider call-limit selection (Python `_select_*_provider_limit`) ────────

interface RuntimeProviderLimit {
  max_concurrent?: number | undefined;
  min_interval_seconds?: number | undefined;
}

/** A mapping treated as absent when empty (Python `_mapping_at` truthiness: `if m:`). */
const nonEmptyMapping = (m: Payload | null): Payload | null => (m !== null && Object.keys(m).length > 0 ? m : null);

/** The policy tier that governs this provider/model (model > provider.limits > default), or null. */
function selectPolicyProviderLimit(
  limitsPolicy: Payload,
  providerName: string,
  providerModel: string | undefined,
): Payload | null {
  const providers = nonEmptyMapping(mappingAtOrNone(limitsPolicy, "providers"));
  const providerLimits = providers === null ? null : nonEmptyMapping(mappingAtOrNone(providers, providerName));
  if (providerLimits !== null) {
    if (providerModel !== undefined) {
      const modelLimits = nonEmptyMapping(mappingAtOrNone(providerLimits, "models", providerModel));
      if (modelLimits !== null) return modelLimits;
    }
    const limits = nonEmptyMapping(mappingAtOrNone(providerLimits, "limits"));
    if (limits !== null) return limits;
  }
  return nonEmptyMapping(mappingAtOrNone(limitsPolicy, "default"));
}

/** The runtime tier that will govern this provider/model (model > provider > default), or null. */
function selectRuntimeProviderLimit(
  limits: ProviderLimitsSpec,
  providerName: string,
  providerModel: string | undefined,
): RuntimeProviderLimit | null {
  // provider.type / provider.model are user-controlled keys — own-keys only, so a
  // provider named like `toString` cannot resolve an inherited prototype member.
  const providerSpec = Object.hasOwn(limits.providers, providerName) ? limits.providers[providerName] : undefined;
  if (providerSpec !== undefined) {
    if (providerModel !== undefined && Object.hasOwn(providerSpec.models, providerModel)) {
      return providerSpec.models[providerModel] ?? null;
    }
    if (providerSpec.max_concurrent !== undefined || providerSpec.min_interval_seconds !== undefined) {
      return providerSpec;
    }
  }
  return limits.default ?? null;
}

/** Where a runtime limit tier is broader than the policy tier (Python `_provider_limit_failures`). */
function providerLimitFailures(runtime: RuntimeProviderLimit, policy: Payload): string[] {
  const failures: string[] = [];
  const policyMaxConcurrent = policy["max_concurrent"];
  if (typeof policyMaxConcurrent === "number") {
    if (runtime.max_concurrent === undefined || runtime.max_concurrent > policyMaxConcurrent) {
      failures.push(`provider max_concurrent must be configured and <= ${policyMaxConcurrent}`);
    }
  }
  const policyMinInterval = policy["min_interval_seconds"];
  if (typeof policyMinInterval === "number") {
    if (runtime.min_interval_seconds === undefined || runtime.min_interval_seconds < policyMinInterval) {
      failures.push(`provider min_interval_seconds must be configured and >= ${policyMinInterval}`);
    }
  }
  return failures;
}

/** Provider call-rate limits against `runtime.provider_limits.*` (Python `_validate_provider_limits`). */
function validateProviderLimits(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const limitsPolicy = mappingAtOrNone(payload, "runtime", "provider_limits");
  if (limitsPolicy === null) return skipped("policy_provider_limits", "policy does not constrain provider limits");
  const provider = spec.runtime.provider;
  // Select by the EFFECTIVE model (Python materializes `provider.model` to the
  // type default at load, then selects the per-model tier). Using the literal
  // (possibly-undefined) model would fall back to the provider/default tier and
  // skip a per-model limit the runtime actually engages.
  const effectiveModel = effectiveModelFor(provider);
  const policyLimits = selectPolicyProviderLimit(limitsPolicy, provider.type, effectiveModel);
  if (policyLimits === null) {
    return skipped("policy_provider_limits", "policy does not constrain this provider/model limit");
  }
  const runtimeLimitsSpec = spec.runtime.provider_limits;
  const runtimeLimits =
    runtimeLimitsSpec === undefined ? null : selectRuntimeProviderLimit(runtimeLimitsSpec, provider.type, effectiveModel);
  if (runtimeLimits === null) {
    return failed("policy_provider_limits", "runtime.provider_limits must be configured for constrained provider/model");
  }
  const failures = providerLimitFailures(runtimeLimits, policyLimits);
  return failures.length > 0 ? failed("policy_provider_limits", failures.join("; ")) : passed("policy_provider_limits");
}

// ── composition ceilings (Python `_validate_composition`) ────────────────────

interface CompositionMetrics {
  flattenedStepCount: number;
  maxParallelWidth: number;
  maxParallelNesting: number;
  widestParallelStep: string | undefined;
  deepestParallelStep: string | undefined;
  mapOverWorkflowStepIds: string[];
}

/**
 * Walk a workflow's step tree once, gathering every composition measurement (Python
 * `_collect_composition_metrics`). `flattenedStepCount` counts every id-bearing node
 * (parallel containers AND their branch steps, recursively); `maxParallelWidth` the
 * largest branch count of any single `parallel` block; `maxParallelNesting` the
 * deepest parallel-in-parallel level; `mapOverWorkflowStepIds` every `map.workflow`
 * fan-out. Pure and deterministic — no I/O. Exported for the closure walk
 * (`validateSubworkflowClosurePolicy`), which sums flattened counts tree-wide for the
 * `max_total_steps` ceiling (#298).
 */
export function collectCompositionMetrics(steps: TypefluxYamlSpec["workflow"]["steps"]): CompositionMetrics {
  let total = 0;
  let maxWidth = 0;
  let widestStep: string | undefined;
  let maxNesting = 0;
  let deepestStep: string | undefined;
  const mapWfIds: string[] = [];

  const walk = (nodes: TypefluxYamlSpec["workflow"]["steps"], depth: number): void => {
    for (const step of nodes) {
      total += 1;
      if (step.map?.workflow !== undefined) {
        mapWfIds.push(step.id);
      } else if (step.parallel !== undefined) {
        const width = step.parallel.branches.length;
        if (width > maxWidth) {
          maxWidth = width;
          widestStep = step.id;
        }
        const nesting = depth + 1;
        if (nesting > maxNesting) {
          maxNesting = nesting;
          deepestStep = step.id;
        }
        for (const branch of step.parallel.branches) walk(branch.steps, nesting);
      }
    }
  };

  walk(steps, 0);
  return {
    flattenedStepCount: total,
    maxParallelWidth: maxWidth,
    maxParallelNesting: maxNesting,
    widestParallelStep: widestStep,
    deepestParallelStep: deepestStep,
    mapOverWorkflowStepIds: mapWfIds,
  };
}

/**
 * Composition ceilings on a workflow's OWN graph shape (Python `_validate_composition`,
 * code `policy_composition_ceilings`): flattened step count, parallel width across this
 * workflow's tree, parallel nesting depth, and `map.workflow` presence — PER-WORKFLOW
 * knobs, evaluated against each closure member individually under the parent's composed
 * policy. The TREE-WIDE ceilings — `max_subworkflow_depth` and the closure-summed
 * `max_total_steps` — are cross-workflow and enforced by the closure walk
 * (`validateSubworkflowClosurePolicy`); this validator applies `max_total_steps` only as
 * the single-workflow lower bound.
 */
function validateComposition(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  // Python `_mapping_at` + `if not composition`: a missing OR empty composition block
  // is "not constrained" (skipped) — so an unset dimension stays byte-identical.
  const composition = nonEmptyMapping(mappingAtOrNone(payload, "composition"));
  if (composition === null) return skipped("policy_composition_ceilings", "policy does not constrain composition");
  const metrics = collectCompositionMetrics(spec.workflow.steps);
  const failures: string[] = [];
  const maxSteps = composition["max_steps"];
  if (typeof maxSteps === "number" && metrics.flattenedStepCount > maxSteps) {
    failures.push(`workflow flattened step count ${metrics.flattenedStepCount} exceeds composition ceiling ${maxSteps}`);
  }
  // max_total_steps is TREE-WIDE (the closure walk enforces the true sum); a single
  // workflow's own count is a lower bound of any tree containing it, so enforcing it
  // here covers the non-composed case (no closure check emitted) and rejects early
  // when even one member already overflows the whole-tree budget.
  const maxTotalSteps = composition["max_total_steps"];
  if (typeof maxTotalSteps === "number" && metrics.flattenedStepCount > maxTotalSteps) {
    failures.push(
      `workflow flattened step count ${metrics.flattenedStepCount} exceeds tree-wide composition ceiling max_total_steps ${maxTotalSteps}`,
    );
  }
  const maxWidth = composition["max_parallel_width"];
  if (typeof maxWidth === "number" && metrics.maxParallelWidth > maxWidth) {
    failures.push(
      `parallel block '${metrics.widestParallelStep ?? ""}' has width ${metrics.maxParallelWidth} exceeding composition ceiling ${maxWidth}`,
    );
  }
  const maxNesting = composition["max_parallel_nesting"];
  if (typeof maxNesting === "number" && metrics.maxParallelNesting > maxNesting) {
    failures.push(
      `parallel nesting depth ${metrics.maxParallelNesting} at step '${metrics.deepestParallelStep ?? ""}' exceeds composition ceiling ${maxNesting}`,
    );
  }
  if (composition["allow_map_over_workflow"] === false && metrics.mapOverWorkflowStepIds.length > 0) {
    const ids = metrics.mapOverWorkflowStepIds.map((id) => `'${id}'`).join(", ");
    failures.push(`map-over-workflow fan-out is not allowed by composition policy (steps: ${ids})`);
  }
  return failures.length > 0
    ? failed("policy_composition_ceilings", failures.join("; "))
    : passed("policy_composition_ceilings", {
        flattened_step_count: metrics.flattenedStepCount,
        max_parallel_width: metrics.maxParallelWidth,
        max_parallel_nesting: metrics.maxParallelNesting,
      });
}

// ── shared control predicates (reused by the standalone checks AND the risk-tier
//    macro, so a tier expands into the SAME logic — one source of truth per control) ─

/**
 * The workflow's ENABLED review gates as `{id, invalid_user_decision}` (Python
 * `_resolved_review_gates`, incl. the enabled-lifecycle guard). The single `review`
 * sugar and the named `gates` both count; an inert block under a disabled/omitted
 * lifecycle yields none (a review block that never runs must not satisfy the policy).
 */
function enabledReviewGates(spec: TypefluxYamlSpec): { id: string; invalid_user_decision: string }[] {
  const lifecycle = spec.workflow.lifecycle;
  if (lifecycle?.enabled !== true) return [];
  if (lifecycle.review !== undefined) {
    return [{ id: "review", invalid_user_decision: lifecycle.review.invalid_user_decision }];
  }
  return (lifecycle.gates ?? []).map((gate) => ({ id: gate.id, invalid_user_decision: gate.invalid_user_decision }));
}

/** Activity DEFINITIONS that declare no moderation (Python `_activities_missing_moderation`). */
function activitiesMissingModeration(spec: TypefluxYamlSpec): string[] {
  return (spec.activities.definitions ?? []).filter((d) => d.moderation === undefined).map((d) => d.name);
}

/** Whether observability redaction is ON (Python `_redaction_enabled`; unset reads as ON). */
function redactionEnabled(spec: TypefluxYamlSpec): boolean {
  return spec.runtime.observability?.redaction?.enabled ?? true;
}

/** Whether a Temporal payload codec is declared (#188 D188-2; Python `_payload_codec_configured`). */
function payloadCodecConfiguredForSpec(spec: TypefluxYamlSpec): boolean {
  return spec.runtime.temporal.payload_codec !== undefined;
}

/** Required custom-rule names absent from the workflow's redaction config (Python `_missing_custom_redaction_rules`). */
function missingCustomRedactionRules(spec: TypefluxYamlSpec, required: readonly string[]): string[] {
  const present = new Set((spec.runtime.observability?.redaction?.custom_rules ?? []).map((rule) => rule.name));
  return required.filter((name) => !present.has(name));
}

/**
 * Step ids that invoke a `side_effecting` activity yet declare no `compensate:` (#299
 * D299-5, the `require_compensation` predicate; Python
 * `_side_effecting_steps_missing_compensation`). `side_effecting` is an author declaration
 * on an inline activity DEFINITION; a STEP is side-effecting when it invokes such an
 * activity — a plain `activity` step or a `map` fanning that activity (both invoke the
 * activity and both accept `compensate:`). Sub-workflow steps invoke a child, not an
 * activity, so they are out of scope (the child governs its own effects; the closure
 * cascade lifts the tier through sub-workflows). Walks the full step tree (recursing into
 * parallel branches). Empty ⇒ satisfied.
 */
function sideEffectingStepsMissingCompensation(spec: TypefluxYamlSpec): string[] {
  const sideEffecting = new Set(
    (spec.activities.definitions ?? []).filter((d) => d.side_effecting === true).map((d) => d.name),
  );
  if (sideEffecting.size === 0) return [];
  const missing: string[] = [];
  const walk = (steps: readonly WorkflowStepSpec[]): void => {
    for (const step of steps) {
      if (step.parallel !== undefined) {
        for (const branch of step.parallel.branches) walk(branch.steps);
        continue;
      }
      const invoked = step.activity ?? step.map?.activity;
      if (invoked !== undefined && sideEffecting.has(invoked) && step.compensate === undefined) {
        missing.push(step.id);
      }
    }
  };
  walk(spec.workflow.steps);
  return missing;
}

// ── risk tiers (#300) ─────────────────────────────────────────────────────────
//
// A risk tier is a NAMED MACRO over controls that already exist (D300-4; Python
// `evaluate_risk_tier` / `_validate_risk_tier`): the effective tier's policy block
// expands into the SAME predicates the other checks use, reported under the ONE
// `policy_risk_tier` check code with each expanded requirement named in details.
//
// The `RiskTierContributor` evidence surface (Python `metadata.RiskTierContributor`,
// stamping `typeflux.risk_tier.*` + the search tag) is Python-only for now (D300-5):
// the TS SDK has no workflow-metadata contributor registry — policy/admission
// provenance isn't stamped into TS execution manifests either — so there is no seam
// to hang it on. Enforcement (this check + the closure cascade) is at full parity; the
// evidence contributor lands when the TS metadata seam does.

export function tierRank(tier: string): number {
  const i = (RISK_TIER_ORDER as readonly string[]).indexOf(tier);
  // An unknown tier ranks as the strictest so a typo fails closed, never open.
  return i < 0 ? RISK_TIER_ORDER.length : i;
}

function maxTier(...tiers: string[]): string {
  return tiers.reduce((best, tier) => (tierRank(tier) > tierRank(best) ? tier : best));
}

interface RiskTierRequirementResult {
  name: string;
  satisfied: boolean;
}

export interface RiskTierEvaluation {
  declared: string;
  floor: string;
  effective: string;
  floorSource: string;
  undeclared: boolean;
  requireDeclared: boolean;
  denied: boolean;
  requirements: RiskTierRequirementResult[];
}

/**
 * Evaluate the workflow's effective risk tier and expand its macro requirements
 * (Python `evaluate_risk_tier`). Returns `undefined` when the policy declares no
 * `risk_tiers` dimension (nothing in play). `cascadeFloor` lifts the effective tier
 * for the sub-workflow closure cascade (D300-3).
 */
export function evaluateRiskTier(
  spec: TypefluxYamlSpec,
  payload: Payload,
  cascadeFloor?: string,
): RiskTierEvaluation | undefined {
  const riskPolicy = mappingAtOrNone(payload, "risk_tiers");
  if (riskPolicy === null || Object.keys(riskPolicy).length === 0) return undefined;

  const rawDeclared = spec.workflow.risk_tier;
  const undeclared = rawDeclared === undefined;
  const declared = rawDeclared ?? "safe";
  const minTier = riskPolicy["min_tier"];
  const floor = typeof minTier === "string" && minTier.length > 0 ? minTier : "safe";
  let effective = maxTier(declared, floor);
  if (cascadeFloor !== undefined) effective = maxTier(effective, cascadeFloor);

  let floorSource: string;
  if (effective === declared) {
    floorSource = "declared";
  } else if (cascadeFloor !== undefined && effective === cascadeFloor) {
    floorSource = "closure";
  } else {
    floorSource = "policy_floor";
  }

  const tierBlock = mappingAtOrNone(riskPolicy, effective) ?? {};
  const requirements: RiskTierRequirementResult[] = [];
  if (tierBlock["require_review"] === true) {
    requirements.push({ name: "require_review", satisfied: enabledReviewGates(spec).length > 0 });
  }
  if (tierBlock["require_moderation"] === true) {
    requirements.push({ name: "require_moderation", satisfied: activitiesMissingModeration(spec).length === 0 });
  }
  if (tierBlock["require_redaction"] === true) {
    requirements.push({ name: "require_redaction", satisfied: redactionEnabled(spec) });
  }
  if (tierBlock["require_payload_codec"] === true) {
    requirements.push({ name: "require_payload_codec", satisfied: payloadCodecConfiguredForSpec(spec) });
  }
  if (tierBlock["require_compensation"] === true) {
    requirements.push({
      name: "require_compensation",
      satisfied: sideEffectingStepsMissingCompensation(spec).length === 0,
    });
  }
  const constrainProviders = mappingAtOrNone(tierBlock, "constrain_providers");
  if (constrainProviders !== null) {
    // D300-6: the SAME mapping shape + the EXACT predicate as `providers.allowed`
    // (provider AND model), so tier-level and policy-level provider constraints can
    // never drift. The model is the EFFECTIVE model admission validates elsewhere
    // (`effectiveModelFor` — Python materializes it at load).
    const provider = spec.runtime.provider;
    const failure = providerModelPolicyFailure(
      { providers: { allowed: constrainProviders } },
      provider.type,
      effectiveModelFor(provider),
    );
    requirements.push({ name: "constrain_providers", satisfied: failure === undefined });
  }

  return {
    declared,
    floor,
    effective,
    floorSource,
    undeclared,
    requireDeclared: riskPolicy["require_declared"] === true,
    denied: effective === "prohibited",
    requirements,
  };
}

/** The sub-workflow closure LIFT of a parent's effective tier (Python `RiskTierCascade`). */
export interface RiskTierCascade {
  liftedBy: string;
  parentEffective: string;
  /** The parent RE-EVALUATED at the lifted tier (its macro requirements re-expanded). */
  evaluation: RiskTierEvaluation;
}

/**
 * Compute the sub-workflow closure LIFT of a parent's effective tier (#300 D300-3; Python
 * `_risk_tier_cascade`). The parent's effective tier maxes over every closure member's;
 * when the strictest member exceeds the parent's own declared+floor effective, the parent
 * is re-evaluated at that lifted tier. Returns `undefined` when no member lifts it. ONE
 * source of truth for the closure admission check and the bundle risk-tier projection.
 */
export function riskTierCascade(
  parentWorkflowId: string,
  parentEval: RiskTierEvaluation,
  memberRiskTiers: Map<string, string>,
  spec: TypefluxYamlSpec,
  payload: Payload,
): RiskTierCascade | undefined {
  let liftingMember: string | undefined;
  for (const [member, tier] of memberRiskTiers) {
    if (member === parentWorkflowId) continue;
    if (liftingMember === undefined || tierRank(tier) > tierRank(memberRiskTiers.get(liftingMember) as string)) {
      liftingMember = member;
    }
  }
  if (liftingMember === undefined) return undefined;
  const cascadeFloor = memberRiskTiers.get(liftingMember) as string;
  if (tierRank(cascadeFloor) <= tierRank(parentEval.effective)) return undefined;
  const lifted = evaluateRiskTier(spec, payload, cascadeFloor);
  if (lifted === undefined) return undefined;
  return { liftedBy: liftingMember, parentEffective: parentEval.effective, evaluation: lifted };
}

/** Fail-closed risk-tier failures (Python `_risk_tier_failures`). */
export function riskTierFailures(evaluation: RiskTierEvaluation, includeRequireDeclared = true): string[] {
  const failures: string[] = [];
  if (includeRequireDeclared && evaluation.requireDeclared && evaluation.undeclared) {
    failures.push("workflow must declare workflow.risk_tier (required by selected project policy)");
  }
  if (evaluation.denied) {
    failures.push(
      `risk tier '${evaluation.effective}' denies admission (effective tier is prohibited; floor_source=${evaluation.floorSource})`,
    );
  }
  for (const req of evaluation.requirements) {
    if (!req.satisfied) {
      failures.push(`risk tier '${evaluation.effective}' requires ${req.name} (unsatisfied; floor_source=${evaluation.floorSource})`);
    }
  }
  return failures;
}

/**
 * Risk-tier admission (#300, code `policy_risk_tier`), fail-closed (Python
 * `_validate_risk_tier`). Effective tier = `max(declared-or-safe, min_tier floor)`;
 * `require_declared` rejects an undeclared workflow; `effective == prohibited` denies;
 * otherwise the effective tier's block expands into the existing predicates. The
 * closure cascade is enforced separately by `validateSubworkflowClosurePolicy`.
 */
function validateRiskTier(spec: TypefluxYamlSpec, payload: Payload): PolicyValidationCheck {
  const evaluation = evaluateRiskTier(spec, payload);
  if (evaluation === undefined) return skipped("policy_risk_tier", "policy does not constrain risk tiers");
  const details = {
    declared: evaluation.declared,
    floor: evaluation.floor,
    effective: evaluation.effective,
    floor_source: evaluation.floorSource,
    requirements: evaluation.requirements.map((req) => ({ name: req.name, satisfied: req.satisfied })),
  };
  const failures = riskTierFailures(evaluation);
  return failures.length > 0
    ? { code: "policy_risk_tier", status: "failed", message: failures.join("; "), details }
    : passed("policy_risk_tier", details);
}

// ── public API ──────────────────────────────────────────────────────────────

/**
 * Validate a workflow spec against a composed policy (Python
 * `validate_project_policy`, the allow-list slice). Returns every check —
 * passed / failed / skipped — for surfacing as evidence. Never throws on a
 * violation; call {@link enforcePolicyCompliance} to fail closed.
 */
export function validatePolicyCompliance(
  spec: TypefluxYamlSpec,
  policy: ComposedProjectPolicy,
): PolicyValidationCheck[] {
  const payload = policy.payload;
  return [
    passed("policy_selection", {
      selected_policy_ids: policy.selectedPolicyIds,
      applied_policy_ids: policy.appliedPolicyIds,
      policy_hash: policy.policyHash,
    }),
    // A static "the allow-list dimensions were evaluated" marker (Python
    // `validate_project_policy` emits it right after policy_selection) — keeps the
    // evidence-check list shape at parity with Python for cross-SDK report comparison.
    passed("policy_allowlists"),
    validateProvider(spec, payload),
    validateSecrets(spec, payload),
    validateRegistry(spec, payload),
    validateImports(spec, payload),
    validateObservability(spec, payload),
    validateTemporal(spec, payload),
    validateArtifacts(spec, payload),
    validateReview(spec, payload),
    validateSemantics(spec, payload),
    validateProviderRetry(spec, payload),
    validateProviderLimits(spec, payload),
    validateComposition(spec, payload),
    validateRiskTier(spec, payload),
  ];
}

/**
 * Fail-closed enforcement: run {@link validatePolicyCompliance} and throw a
 * {@link ProjectPolicyEnforcementError} (carrying the failed checks) if any check
 * failed. `assembleYamlRuntime` (which `buildRuntime` delegates to) runs this as a
 * pre-flight when a `policy` is supplied, so a non-compliant workflow never starts.
 */
export function enforcePolicyCompliance(spec: TypefluxYamlSpec, policy: ComposedProjectPolicy): void {
  const checks = validatePolicyCompliance(spec, policy);
  const violations = checks.filter((c) => c.status === "failed");
  if (violations.length > 0) {
    throw new ProjectPolicyEnforcementError(
      `workflow violates selected project policy: ${violations.map((c) => c.message).join("; ")}`,
      checks,
    );
  }
}

/**
 * Per-call RUNTIME enforcement (#454; Python `RuntimePolicyGuard`) — the layer
 * beyond admission. Admission ({@link enforcePolicyCompliance}) validates the static
 * spec before start; this guard runs INSIDE each activity execution, where the model
 * and the moderator's verdict become concrete, and catches what a static check
 * cannot see (a backend-registry prompt model under `allow_prompt_model_override`, a
 * code-defined activity, or a moderator's reported categories/score).
 *
 * `buildRuntime`/`assembleYamlRuntime` construct one from the composed `policy` and
 * wire its two methods into the executor as the `providerModelGuard` and
 * `moderationPolicyBlock` hooks.
 */
export class RuntimePolicyGuard {
  /** @param providerName the spec provider identity admission used (Python: `self.provider_name`). */
  constructor(
    readonly policy: ComposedProjectPolicy,
    private readonly providerName?: string,
  ) {}

  /**
   * Enforce the per-call model against `providers.allowed` — throws
   * {@link ProjectPolicyEnforcementError} when the resolved model is not allowed.
   * Checks against the spec provider identity (not the caller-derived name), so a
   * custom provider whose object name differs from its spec type can't slip past.
   */
  enforceProviderModel(call: ResolvedProviderCall): void {
    // Python `self.provider_name or provider_name` — `||` (not `??`) so an empty
    // bound identity falls back to the caller-derived name (truthiness parity).
    const providerName = this.providerName || call.providerName;
    // When nothing pinned the model, fold the provider-type default — the SAME
    // value admission's `effectiveModelFor` validated (Python materializes the
    // provider default model, so admission and the runtime guard agree). An injected
    // provider's OWN default beyond the type default is unknowable here (like
    // base_url); the type default is the governed value.
    const model = call.model ?? defaultModelForProviderType(providerName);
    const failure = providerModelPolicyFailure(this.policy.payload, providerName, model);
    if (failure === undefined) return;
    const context = [
      `activity=${call.activityName}`,
      call.promptName !== undefined ? `prompt=${call.promptName}` : undefined,
      `provider=${providerName}`,
      `model=${model ?? ""}`,
    ]
      .filter((part): part is string => part !== undefined)
      .join(", ");
    throw new ProjectPolicyEnforcementError(`${failure} (${context})`, [failed("policy_provider", failure)]);
  }

  /**
   * Config-level moderation posture at build/registration time (Python
   * `enforce_moderation_config`): when the policy requires moderation, an activity
   * must DECLARE it (and use `on_violation: block` when the policy mandates
   * fail-closed). Admission validates spec definitions; this closes the gap for
   * CODE-DEFINED activities (`extraActivities`) admission never sees. Throws
   * {@link ProjectPolicyEnforcementError}.
   */
  enforceModerationConfig(info: { activityName: string; moderationConfigured: boolean; onViolation: string | undefined }): void {
    const semantics = mappingAtOrNone(this.policy.payload, "semantics");
    if (semantics === null) return;
    const requiresModeration = semantics["required"] === true || semantics["require_block"] === true;
    if (requiresModeration && !info.moderationConfigured) {
      const message = `activity '${info.activityName}' must declare moderation (required by selected project policy)`;
      throw new ProjectPolicyEnforcementError(message, [failed("policy_semantics", message)]);
    }
    if (semantics["require_block"] === true && info.moderationConfigured && info.onViolation !== "block") {
      const message = `activity '${info.activityName}' moderation must use on_violation='block' (required by selected project policy), not '${info.onViolation ?? ""}'`;
      throw new ProjectPolicyEnforcementError(message, [failed("policy_semantics", message)]);
    }
  }

  /**
   * Verdict escalation (Python `moderation_policy_block`): apply the org's bar to the
   * moderator's REPORTED categories/score independent of its own `flagged` decision.
   * Returns a block reason when the policy forces a block (a disallowed category, or a
   * score at/over the threshold), else undefined. Can only tighten.
   */
  moderationPolicyBlock(verdict: {
    activityName: string;
    categories: string[];
    maxScore: number | undefined;
  }): string | undefined {
    const semantics = mappingAtOrNone(this.policy.payload, "semantics");
    if (semantics === null) return undefined;
    const disallowed = listAtOrNone(semantics, "categories");
    if (disallowed !== null && disallowed.length > 0) {
      const hit = [...new Set(verdict.categories)].filter((category) => disallowed.includes(category)).sort();
      if (hit.length > 0) {
        const label = hit.length === 1 ? "category" : "categories";
        return `moderation policy blocked activity '${verdict.activityName}': disallowed ${label} ${hit.join(", ")}`;
      }
    }
    const threshold = semantics["score_threshold"];
    if (typeof threshold === "number" && verdict.maxScore !== undefined && verdict.maxScore >= threshold) {
      return `moderation policy blocked activity '${verdict.activityName}': score ${verdict.maxScore} >= threshold ${threshold}`;
    }
    return undefined;
  }
}
