/**
 * Wire a validated spec into a runnable worker (parity Epic 5, #452; Python
 * `yaml/runtime.py` `build_runtime` + `TypefluxYamlRuntime.execute_workflow`).
 *
 * `assembleYamlRuntime` is the pure, unit-testable core (activities map + workflow
 * plan, no server); `buildRuntime` adds the live `createTypefluxWorker`. The provider
 * is injected or built from the spec (#449); observers wire via `observerFromSpec`
 * (#451); the artifact admission policy wires from `runtime.artifacts` (#481);
 * `runtime.provider_limits` wires one shared `ProviderRateLimitController` across
 * the assembled activities (#529).
 */

import type { DataConverter } from "@temporalio/common";
import {
  isSubjectErasableCacheStore,
  type ActivityDescriptor,
  type ActivityObserver,
  type ArtifactPolicy,
  artifactPolicy,
  type CacheStore,
  canonicalJson,
  mergeProviderParams,
  type ModelProvider,
  type ModerationPolicyBlock,
  type PromptRegistry,
  type ProviderCallLimits,
  type ProviderModelGuard,
  ProviderRateLimitController,
  type RegistryTransport,
} from "@typeflux/temporal";
import {
  artifactInputResolver,
  buildTemporalActivities,
  createTypefluxWorker,
  executeWorkflow,
  startWorkflow,
  type TemporalActivities,
  type TypefluxWorkerOptions,
} from "@typeflux/temporal-worker";

import { defineActivitiesFromSpec, type ActivityResolver } from "./build-activities.js";
import { assertConsistentComposedObservability } from "./observability-composition.js";
import { composeRuntimeRegistry, type RegistrySource } from "./registry-composition.js";
import { langfuseObserverFromSpec } from "./langfuse-observer.js";
import { langfuseRegistryTransportFromSpec } from "./langfuse-registry.js";
import { langsmithObserverFromSpec } from "./langsmith-observer.js";
import { langsmithRegistryTransportFromSpec } from "./langsmith-registry.js";
import { workflowPlanFromSpec, type SubworkflowSpecResolver } from "./build-workflow.js";
import type { ComposedProjectPolicy } from "./policy-composition.js";
import { enforcePolicyCompliance, ProjectPolicyEnforcementError, RuntimePolicyGuard } from "./policy-enforcement.js";
import {
  assertRiskTierEnforced, validateSubworkflowClosurePolicy } from "./project-enforcement.js";
import {
  enforceFrozenWorkflowVersion,
  workflowIdentityMemo,
  workflowPlanDigest,
  type WorkflowListClient,
} from "./frozen-version.js";
import {
  defaultModelForProviderType,
  ENGINE_PROVIDER_RETRY_DEFAULTS,
  providerFromSpec,
  type ProviderTransports,
} from "./provider-from-spec.js";
import { specReferencesSubworkflows } from "./project-validation.js";
import {
  buildSubjectAwarePayloadCodec,
  InMemorySubjectKeystore,
  SubjectScopedPayloadCodec,
  type SubjectBindingClient,
  type SubjectKeystore,
} from "./subject-keystore.js";
import { SUBJECT_IDS_SEARCH_ATTRIBUTE, effectiveSubjectIds } from "./subjects.js";
import { providerTransportsFromSpec } from "./provider-transports-from-env.js";
import { providerParamsRecord, type TypefluxYamlSpec } from "./spec.js";
import {
  flattenCompensationActivities,
  flattenPlanSteps,
  flattenSubworkflowSteps,
  type WorkflowPlan,
  type WorkflowPlanStep,
} from "./workflow-plan.js";

/** The workflow type the generic interpreter is registered under (see `workflows.ts`). */
export const YAML_WORKFLOW_TYPE = "typefluxYamlWorkflow";

export interface AssembleYamlRuntimeOptions {
  /**
   * The model provider every activity calls. Supply this directly, OR omit it and pass
   * `transports` to build the provider from `spec.runtime.provider` (one is required).
   */
  provider?: ModelProvider;
  /** Build the provider from `spec.runtime.provider` via injected transports (when `provider` is omitted). */
  transports?: ProviderTransports;
  /** Resolves each definition's input/output schema ref to a Zod schema. */
  schemas: ActivityResolver["schemas"];
  /** Optional per-activity normalization hooks, keyed by activity name. */
  hooks?: ActivityResolver["hooks"];
  /**
   * Optional per-activity input-aware output checks (#745), keyed by activity name — the
   * pre-acceptance analogue of {@link hooks}. Injected onto each YAML definition's descriptor
   * `outputCheck` (parent + every composed child), so a grounding contract that zod cannot
   * express feeds the validation repair loop instead of failing loud. See {@link ActivityResolver.outputChecks}.
   */
  outputChecks?: ActivityResolver["outputChecks"];
  /** Optional per-activity output moderators, keyed by activity name (a definition's `moderation` selects intent). */
  moderators?: ActivityResolver["moderators"];
  /** A prompt registry, used when neither the spec's inline registry nor `registryTransport` resolves one. */
  registry?: PromptRegistry;
  /** A registry transport — builds a `TransportPromptRegistry` for a langfuse/langsmith/custom spec. */
  registryTransport?: RegistryTransport;
  /**
   * Observes every activity execution (e.g. a `TraceWriter`, or `observerFromSpec(spec, transport)`).
   * An explicitly-supplied observer SATISFIES a policy's `observability.required` by construction
   * (#756) — the caller owns its transport and lifecycle; the fail-closed gate only fires when the
   * policy requires observability and NO observer reached assembly.
   */
  observer?: ActivityObserver;
  /**
   * A cross-run activity output cache (#398/#753): the store `executeActivity` reads/writes to
   * memoize the VALIDATED output of any activity whose definition declares `cross_run_cache:`
   * (or an injected AI descriptor carrying a cross-run {@link CacheStore} `cache` config).
   * Threaded into every activity's shared options — parent AND every composed child — exactly
   * like `observer`/`outputChecks`. DISTINCT from the session `cache:` prefix cache (#478), which
   * needs no store. Absent ⇒ cross-run caching is inert (a declared `cross_run_cache:` is a no-op),
   * so wiring `InMemoryCacheStore` (or a durable store) is what makes YAML cross-run caching engage.
   */
  cacheStore?: CacheStore;
  /**
   * CODE-DEFINED activity descriptors merged into the assembled map (#496 — the
   * TS replacement for Python's `activities.modules` importlib loading): workflow
   * steps can reference them by name. They get the SAME shared options as
   * spec-defined activities (provider/registry/artifact resolver/provider
   * defaults/retry/observer); a name colliding with a spec definition throws.
   * For bespoke per-activity options, use `buildTemporalActivities` directly and
   * merge the maps at worker creation.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  extraActivities?: Record<string, ActivityDescriptor<any, any>>;
  /** Overrides `spec.task_queue`. */
  taskQueue?: string;
  /**
   * A composed project policy (#454) to enforce as a FAIL-CLOSED pre-flight: the
   * spec is validated against it BEFORE any activity map, provider, or worker is
   * built, so a non-compliant workflow throws {@link ProjectPolicyEnforcementError}
   * and never starts. Omit it to skip governance. Compose one with
   * `composeProjectPolicies` from the org/tenant policy specs that apply.
   */
  policy?: ComposedProjectPolicy;
  /**
   * Resolves `workflow:` / `map.workflow` sub-workflow references (#55 §3.4) to their sibling
   * RESOLVED specs, so the derived plan embeds each child's plan + identity. Absent ⇒ the spec
   * is planned standalone and any sub-workflow step rejects (a reference resolves only through a
   * project manifest). A project worker builds one via {@link projectSubworkflowResolver}.
   */
  subworkflows?: SubworkflowSpecResolver;
}

export interface AssembledYamlRuntime {
  activities: TemporalActivities;
  plan: WorkflowPlan;
  taskQueue: string;
}

/**
 * Pure assembly (no server): build the activity map + workflow plan from the spec.
 * Throws if there is no prompt registry — the spec's `runtime.registry` is not
 * `inline` and none was injected, so the activities could not resolve their prompts.
 */
/**
 * The full fail-closed governance pre-flight for the SDK build entries (#454 + #300):
 * the per-workflow checks (`enforcePolicyCompliance`) PLUS the sub-workflow CLOSURE
 * admission (`validateSubworkflowClosurePolicy` — the #298 tree-wide composition
 * ceilings and the #300 risk-tier cascade ride that walk). Without the closure leg, a
 * composed program built through `assembleYamlRuntime`/`buildRuntime` (rather than the
 * project guard, which already runs it) would skip cascade + tree-wide admission. The
 * resolver derives from `options.subworkflows`; when the spec HAS workflow references
 * but no resolver is supplied, the closure check itself fails closed (#699 rule — a
 * composed spec must never silently skip closure admission).
 */
function enforcePolicyPreflight(
  spec: TypefluxYamlSpec,
  policy: ComposedProjectPolicy,
  subworkflows: SubworkflowSpecResolver | undefined,
): void {
  enforcePolicyCompliance(spec, policy);
  // #788: an elevated declared tier (closure-aware) must actually be enforced by the
  // composed policy — the direct-SDK runtime preflight shares the same fail-close as
  // the guard builder / worker entry / admission.
  assertRiskTierEnforced(
    spec,
    policy.selectedPolicyIds,
    policy.payload,
    subworkflows === undefined ? undefined : (id) => subworkflows.specFor(id),
    subworkflows?.selfId ?? spec.workflow.name,
  );
  const closure = validateSubworkflowClosurePolicy(
    spec,
    policy,
    subworkflows === undefined ? undefined : (id) => subworkflows.specFor(id),
    // The manifest self-id when known; else the workflow name (the same fallback the
    // plan derivation uses for its parent label) — it only seeds cycle detection here.
    subworkflows?.selfId ?? spec.workflow.name,
  );
  if (closure !== undefined && closure.status === "failed") {
    const message = closure.message || `policy check failed: ${closure.code}`;
    throw new ProjectPolicyEnforcementError(
      `project policy enforcement failed: ${closure.code}: ${message}`,
      [closure],
    );
  }
}

/**
 * Whether the effective composed policy sets `observability.required: true` (#756). Reads the
 * SAME `policy.payload.observability` mapping admission's `validateObservability` evaluates, so the
 * runtime fail-closed gate and the admission shape-check agree on what "required" means. Non-boolean
 * / absent ⇒ not required (back-compat: no gate for an ungoverned or observability-silent policy).
 */
function observabilityRequiredByPolicy(policy: ComposedProjectPolicy): boolean {
  const observability = policy.payload["observability"];
  return (
    typeof observability === "object" &&
    observability !== null &&
    !Array.isArray(observability) &&
    (observability as Record<string, unknown>)["required"] === true
  );
}

/**
 * The ONE fail-closed required-observability gate (#756), run by `assembleYamlRuntime` — the
 * site EVERY build route traverses (`buildRuntime` and the worker entrypoint arrive here through
 * `autoWireRuntimeOptions`, which sets `observer` when the OOTB credentials resolve; direct
 * `assembleYamlRuntime` callers arrive with whatever they injected). When the effective composed
 * policy sets `observability.required` and NO observer reached assembly, the workflow would run
 * untraced — a regulated-tier adopter compliance gap — so refuse with a pointed error naming
 * the backend's exact env vars instead of degrading. An explicitly-supplied `observer` satisfies
 * the requirement by construction (the caller owns its transport and lifecycle). Admission
 * (`validateObservability`, part of the pre-flight that runs just before this) already refused a
 * `type: none`/absent spec under this policy, so the declared backend here is a real one.
 */
function enforceRequiredObservabilityResolves(spec: TypefluxYamlSpec, policy: ComposedProjectPolicy): void {
  if (!observabilityRequiredByPolicy(policy)) {
    return;
  }
  // `|| "none"` — the SAME empty-string collapse admission's validateObservability applies.
  const backend = spec.runtime.observability?.type || "none";
  if (backend === "none") {
    return; // Unreachable after the pre-flight (admission fails `none` under `required`); defensive.
  }
  const remedy =
    backend === "langfuse"
      ? "declare runtime.observability.langfuse.public_key/secret_key or export LANGFUSE_PUBLIC_KEY " +
        "and LANGFUSE_SECRET_KEY (plus LANGFUSE_HOST for self-hosted) so the runtime can build the " +
        "langfuse observer, or supply `observer` explicitly"
      : backend === "langsmith"
        ? "declare runtime.observability.langsmith.api_key or export LANGSMITH_API_KEY (plus " +
          "LANGSMITH_ENDPOINT for self-hosted and LANGSMITH_PROJECT to pick the project) so the " +
          "runtime can build the langsmith observer, or supply `observer` explicitly"
        : "supply `observer` explicitly (a non-OOTB backend has no environment auto-wiring)";
  throw new Error(
    `runtime.observability.type is ${backend} and the governing project policy sets ` +
      `observability.required, but no observer resolved — a required-observability workflow ` +
      `must not run untraced. ${remedy}.`,
  );
}

export function assembleYamlRuntime(
  spec: TypefluxYamlSpec,
  options: AssembleYamlRuntimeOptions,
): AssembledYamlRuntime {

  // Governance pre-flight (#454), FIRST: a non-compliant spec must refuse to build
  // before any provider/registry/worker is constructed. Fail-closed — throws
  // ProjectPolicyEnforcementError carrying the failed checks. Includes the closure
  // admission (#300/#298): cascade + tree-wide ceilings run on this entry too.
  if (options.policy !== undefined) {
    enforcePolicyPreflight(spec, options.policy, options.subworkflows);
    // Required-observability gate (#756), at THIS site so every build route traverses it —
    // including a direct assembleYamlRuntime caller that never runs autoWireRuntimeOptions.
    if (options.observer === undefined) {
      enforceRequiredObservabilityResolves(spec, options.policy);
    }
  }
  // #795 (after the governance pre-flight, which stays FIRST; unconditional — a spec needs
  // no policy for its own declaration to bind): a declared targeted-cache-erasure
  // requirement fails CLOSED at assembly when the wired store cannot honor it — wiring a
  // store that per-subject erasure cannot precisely reach would make the declaration a
  // dead letter.
  if (spec.runtime.cache_erasure === "targeted" && options.cacheStore !== undefined) {
    if (!isSubjectErasableCacheStore(options.cacheStore)) {
      throw new Error(
        "runtime.cache_erasure is 'targeted' but the wired cacheStore does not implement " +
          "eraseSubject (SubjectErasableCacheStore) — per-subject invalidation is declared " +
          "REQUIRED, so this store must not be wired. Implement eraseSubject on the store, " +
          "or drop the cache_erasure declaration.",
      );
    }
  }
  // The per-call RUNTIME guard (#454): catches what admission cannot see — a
  // dynamic backend-prompt model, or a moderator's reported categories/score.
  // Keyed to the spec provider identity admission validated.
  const policyGuard =
    options.policy !== undefined ? new RuntimePolicyGuard(options.policy, spec.runtime.provider.type) : undefined;
  // Admission validates the moderation posture (semantics.required/require_block)
  // over SPEC definitions only, so CODE-DEFINED activities would bypass it — close
  // that here, as part of the fail-closed pre-flight, before anything is built.
  if (policyGuard !== undefined) {
    for (const [name, descriptor] of Object.entries(options.extraActivities ?? {})) {
      // Pure-code activities (#746) are EXEMPT from the moderation requirement: the
      // `semantics.required`/`require_block` policy governs LLM semantics — moderating a
      // model's free-form output — and a code activity has no model output to moderate
      // (deterministic code is covered by ordinary code review). It also structurally
      // CANNOT comply: defineCodeActivity rejects the `moderation` option, so enforcing
      // here would make every code activity unusable under a required-moderation policy.
      if (descriptor.kind === "code") {
        continue;
      }
      policyGuard.enforceModerationConfig({
        activityName: name,
        moderationConfigured: descriptor.moderation !== undefined,
        onViolation: descriptor.moderation?.onViolation,
      });
    }
  }
  const specBuiltProvider = options.provider === undefined && options.transports !== undefined;
  const provider =
    options.provider ?? (options.transports !== undefined ? providerFromSpec(spec, options.transports) : undefined);
  if (provider === undefined) {
    throw new Error(
      "buildRuntime: provide either `provider` (a ModelProvider) or `transports` " +
        "(to build it from spec.runtime.provider)",
    );
  }
  const descriptors = defineActivitiesFromSpec(spec, {
    schemas: options.schemas,
    ...(options.hooks !== undefined ? { hooks: options.hooks } : {}),
    ...(options.outputChecks !== undefined ? { outputChecks: options.outputChecks } : {}),
    ...(options.moderators !== undefined ? { moderators: options.moderators } : {}),
  });
  // The artifact resolver is always injected: it only runs for descriptors that declare
  // artifacts, and the policy comes from `runtime.artifacts` (defaults = local_path-only,
  // NO local roots — a spec must configure `runtime.artifacts.local_roots` to read files).
  const resolver_ = artifactInputResolver(artifactPolicyFromSpec(spec));
  // The provider-wide DEFAULTS layer (#495): runtime.provider.params + the legacy
  // top-level model folded in (the spec validated they match when both set). The
  // truthy model guard keeps an interpolated `model: ${MODEL:-}` empty string from
  // selecting a model (Python `provider.model or ...`). NOT options.model — that is
  // the highest-precedence call override; defaults must lose to prompt/activity params.
  // For a SPEC-BUILT provider with no explicit model, fold the type's default so the
  // EFFECTIVE model always enters cache keys — a future default bump must miss, not
  // serve stale entries recorded under the old implicit model (Python materializes
  // `params.model or DEFAULT` into the resolved spec). An INJECTED provider's default
  // is its own business — left unfolded.
  const implicitModel =
    specBuiltProvider && !spec.runtime.provider.model && spec.runtime.provider.params?.model === undefined
      ? defaultModelForProviderType(spec.runtime.provider.type)
      : undefined;
  const providerDefaults = mergeProviderParams(
    spec.runtime.provider.params !== undefined ? providerParamsRecord(spec.runtime.provider.params) : undefined,
    spec.runtime.provider.model ? { model: spec.runtime.provider.model } : undefined,
    implicitModel !== undefined ? { model: implicitModel } : undefined,
  );
  // In-activity provider retry (#495; Python ProviderRetrySpec -> ProviderRetryPolicy):
  // max_attempts is the TOTAL attempt count (Python), so retries = max_attempts - 1;
  // seconds -> ms onto the core's exponential backoff.
  const retrySpec = spec.runtime.provider_retry;
  const engineRetry = ENGINE_PROVIDER_RETRY_DEFAULTS;
  const providerRetryOptions =
    retrySpec !== undefined
      ? {
          transientRetries: (retrySpec.max_attempts ?? engineRetry.max_attempts) - 1,
          transientBackoff: {
            initialMs: Math.round(
              (retrySpec.initial_backoff_seconds ?? engineRetry.initial_backoff_seconds) * 1000,
            ),
            multiplier: retrySpec.backoff_multiplier ?? engineRetry.backoff_multiplier,
            ...(retrySpec.max_backoff_seconds != null
              ? { maxMs: Math.round(retrySpec.max_backoff_seconds * 1000) }
              : {}),
            jitterRatio: retrySpec.jitter_ratio ?? engineRetry.jitter_ratio,
          },
          retryRateLimits: retrySpec.retry_rate_limits,
          retryTransientErrors: retrySpec.retry_transient_errors,
        }
      : {};
  // provider_limits (#529 PR B; Python _provider_rate_limit_policy): ONE controller
  // shared by every registration, so concurrent activities contend on the same
  // per-policy-key limiter — per-activity controllers would multiply the quota.
  const limitsSpec = spec.runtime.provider_limits;
  const providerLimitOptions =
    limitsSpec !== undefined
      ? {
          providerLimitController: new ProviderRateLimitController({
            ...(() => {
              const defaults = callLimits(limitsSpec.default);
              return defaults !== undefined ? { default: defaults } : {};
            })(),
            providers: Object.fromEntries(
              Object.entries(limitsSpec.providers).map(([name, providerSpec]) => {
                const limits = callLimits(providerSpec);
                return [
                  name,
                  {
                    ...(limits !== undefined ? { limits } : {}),
                    models: Object.fromEntries(
                      Object.entries(providerSpec.models).flatMap(([model, tier]) => {
                        // An EMPTY tier is absent, not a model-specific "no limits":
                        // it must fall through to the provider/default tier (Python
                        // _provider_call_limits returns None — codex P2).
                        const modelLimits = callLimits(tier);
                        return modelLimits !== undefined ? [[model, modelLimits]] : [];
                      }),
                    ),
                  },
                ];
              }),
            ),
          }),
        }
      : {};
  const plan = workflowPlanFromSpec(
    spec,
    options.subworkflows !== undefined ? { subworkflows: options.subworkflows } : {},
  );
  const parentLabel = options.subworkflows?.selfId ?? spec.workflow.name;
  // The specs that contribute to the ONE registry this worker serves (#748): the parent plus,
  // once the child block resolves them, every transitively-referenced child. Their registries
  // are MERGED (byte-identical dedupe, loud conflicts) so a composed worker no longer forces a
  // child's prompts to be duplicated into the parent spec.
  const childRegistrySources: RegistrySource[] = [];
  // Child-workflow activities (#55 §6): a child runs on THIS worker (one generic
  // `typefluxYamlWorkflow` type registered here), so ITS activities must be in the assembled
  // map too, or the child would fail "activity not registered" mid-run. Collect every referenced
  // child (recursively through embedded child plans — grandchildren included), resolve its spec
  // via the same project resolver, and merge its descriptors.
  //
  // Cross-workflow name-collision rule (both editions, identically): an activity name declared
  // by MULTIPLE project workflows is allowed only when the DEFINITIONS are identical (canonical
  // form) — one activity, declared twice, registers once. Divergent definitions under one name
  // reject loudly naming both declaring workflows: a silent first-wins would execute one
  // workflow's steps with the other's prompt/schemas/options depending on merge order.
  if (options.subworkflows !== undefined) {
    const childIds = new Set<string>();
    const collectChildIds = (steps: readonly WorkflowPlanStep[]): void => {
      for (const node of flattenSubworkflowSteps(steps)) {
        childIds.add(node.workflowId);
        collectChildIds(node.plan.steps);
      }
    };
    collectChildIds(plan.steps);
    const declaredBy = new Map<string, { workflow: string; canonical: string }>();
    for (const definition of spec.activities.definitions ?? []) {
      declaredBy.set(definition.name, { workflow: parentLabel, canonical: canonicalJson(definition) });
    }
    for (const childId of childIds) {
      const childSpec = options.subworkflows.specFor(childId);
      if (childSpec === undefined) {
        continue; // Unresolvable refs already failed plan derivation with the precise error.
      }
      // Contribute the child's registry to the merged worker registry (#748).
      childRegistrySources.push({ id: childId, spec: childSpec });
      for (const definition of childSpec.activities.definitions ?? []) {
        const prior = declaredBy.get(definition.name);
        const canonical = canonicalJson(definition);
        if (prior === undefined) {
          declaredBy.set(definition.name, { workflow: childId, canonical });
        } else if (prior.canonical !== canonical) {
          throw new Error(
            `activity ${JSON.stringify(definition.name)} is declared with DIFFERENT definitions by project ` +
              `workflows ${JSON.stringify(prior.workflow)} and ${JSON.stringify(childId)} — a worker registers one ` +
              "activity per name, so composed workflows must either share an identical definition or use " +
              "distinct activity names (#55)",
          );
        }
      }
      const childDescriptors = defineActivitiesFromSpec(childSpec, {
        schemas: options.schemas,
        ...(options.hooks !== undefined ? { hooks: options.hooks } : {}),
        ...(options.outputChecks !== undefined ? { outputChecks: options.outputChecks } : {}),
        ...(options.moderators !== undefined ? { moderators: options.moderators } : {}),
      });
      for (const [name, descriptor] of Object.entries(childDescriptors)) {
        // Identical-by-canonical-form guaranteed above, so first-in wins is now a pure dedupe.
        if (!Object.hasOwn(descriptors, name)) {
          descriptors[name] = descriptor;
        }
      }
    }
  }
  // The ONE observer this (possibly composed) worker threads into every activity is built
  // from the PARENT spec (#756, the #748 singleton rule): a child declaring a DIFFERENT
  // backend would be silently ignored — never resolved, never credential-checked — so require
  // the closure to agree (absent/none inherits) before anything is served.
  assertConsistentComposedObservability({ id: parentLabel, spec }, childRegistrySources);
  // The ONE registry served by this (possibly composed) worker (#748): the merge of the
  // parent's and every referenced child's registry. With no sub-workflows this is exactly
  // `registryFromSpec(spec, transport) ?? options.registry` (unchanged single-spec behavior).
  const registry = composeRuntimeRegistry({
    parent: { id: parentLabel, spec },
    children: childRegistrySources,
    transport: options.registryTransport,
    injected: options.registry,
  });
  if (registry === undefined) {
    throw new Error(
      "buildRuntime: no prompt registry — the spec's runtime.registry is not `inline` and no " +
        "`registryTransport` was given, so pass `registry` (a PromptRegistry) or `registryTransport`",
    );
  }
  const sharedOptions = {
    provider,
    registry,
    artifactResolver: resolver_,
    ...(Object.keys(providerDefaults).length > 0 ? { providerParams: providerDefaults } : {}),
    ...providerRetryOptions,
    ...providerLimitOptions,
    ...(options.observer !== undefined ? { observer: options.observer } : {}),
    // The cross-run output cache store (#398/#753): reaches EVERY activity's execute options
    // (parent + composed children, since sharedOptions is the one options object every
    // registration gets) so a `cross_run_cache:`-declaring activity actually memoizes. Without
    // it, execute.ts's `descriptor.cache?.enabled && options.cacheStore` gate stays closed.
    ...(options.cacheStore !== undefined ? { cacheStore: options.cacheStore } : {}),
    // Per-call policy guards (#454): the model check runs before each provider call,
    // the moderation escalation at each output checkpoint. Bound to the guard so a
    // custom provider's per-call model and a moderator's verdict are policed.
    ...(policyGuard !== undefined
      ? {
          providerModelGuard: ((call) => policyGuard.enforceProviderModel(call)) satisfies ProviderModelGuard,
          moderationPolicyBlock: ((verdict) => policyGuard.moderationPolicyBlock(verdict)) satisfies ModerationPolicyBlock,
        }
      : {}),
  };
  // Code-defined descriptors (#496) join the spec-defined ones; the duplicate-name
  // guard in buildTemporalActivities makes a collision with a spec definition loud.
  const extraDescriptors = Object.values(options.extraActivities ?? {});
  const activities = buildTemporalActivities(
    [...Object.values(descriptors), ...extraDescriptors].map((descriptor) => ({
      descriptor,
      options: sharedOptions,
    })),
  );
  // Plan derivation reads only spec definitions — an INJECTED descriptor's sessionCache
  // must reach map steps too, or a cache-enabled code-defined activity would register
  // its prep/release companions and then silently run uncached (codex #531). Applied to one
  // step slice (the parent's, or an embedded child plan's) — returns whether it mutated.
  const patchInjectedSessionCache = (steps: readonly WorkflowPlanStep[]): boolean => {
    let mutated = false;
    for (const step of flattenPlanSteps(steps)) {
      if (step.kind === "map" && step.sessionCache === undefined) {
        // Own-property lookup (not `extraActivities?.[name]`) so a map step named like an
        // inherited member ("constructor", …) can't pick up a prototype value as its
        // descriptor — parity with the `Object.hasOwn(activities, …)` check just below.
        const extra =
          options.extraActivities !== undefined && Object.hasOwn(options.extraActivities, step.activity)
            ? options.extraActivities[step.activity]
            : undefined;
        if (extra?.sessionCache?.enabled === true) {
          step.sessionCache = {
            enabled: true,
            ...(extra.sessionCache.ttlSeconds !== undefined ? { ttlSeconds: extra.sessionCache.ttlSeconds } : {}),
          };
          mutated = true;
        }
      }
    }
    return mutated;
  };
  // Every activity a step slice calls must be in the assembled map, else the failure is
  // deferred to a cryptic Temporal "activity not registered" error mid-run. (A module-based
  // spec — no `activities.definitions` — has an empty map here; module loading is not yet
  // supported, so this surfaces that clearly too.)
  // Nested branch steps included: a parallel block's activities register like any other.
  const assertActivitiesAvailable = (steps: readonly WorkflowPlanStep[], owner: string): void => {
    for (const step of flattenPlanSteps(steps)) {
      if (!Object.hasOwn(activities, step.activity)) {
        throw new Error(
          `${owner} step ${JSON.stringify(step.id)} references activity ${JSON.stringify(step.activity)} ` +
            `which is not among the spec's activity definitions (${JSON.stringify(Object.keys(activities))})`,
        );
      }
    }
    // A compensating activity is ordinary (#299) but not a leaf step, so it must be checked
    // explicitly — else a typo'd `compensate.activity` only surfaces mid-unwind as a caught
    // compensation failure instead of at build.
    for (const { stepId, activity } of flattenCompensationActivities(steps)) {
      if (!Object.hasOwn(activities, activity)) {
        throw new Error(
          `${owner} step ${JSON.stringify(stepId)} compensate references activity ${JSON.stringify(activity)} ` +
            `which is not among the spec's activity definitions (${JSON.stringify(Object.keys(activities))})`,
        );
      }
    }
  };
  // Embedded CHILD plans get the same two walks (#55 §6): a child runs on this worker, so its
  // map steps deserve the injected-sessionCache patch and its activities the availability check.
  // Bottom-up: a mutated descendant plan changes this node's embedded plan bytes, so the node's
  // `childDigest` is recomputed to keep the child identity memo consistent with the plan the
  // interpreter actually dispatches (the parent digest is computed AFTER this, in buildRuntime).
  const patchAndCheckChildren = (steps: readonly WorkflowPlanStep[]): boolean => {
    let mutated = false;
    for (const node of flattenSubworkflowSteps(steps)) {
      const descendantsMutated = patchAndCheckChildren(node.plan.steps);
      const ownMutated = patchInjectedSessionCache(node.plan.steps);
      assertActivitiesAvailable(node.plan.steps, `sub-workflow ${JSON.stringify(node.workflowId)}`);
      if (descendantsMutated || ownMutated) {
        node.childDigest = workflowPlanDigest(node.plan);
        mutated = true;
      }
    }
    return mutated;
  };
  patchInjectedSessionCache(plan.steps);
  assertActivitiesAvailable(plan.steps, "workflow");
  patchAndCheckChildren(plan.steps);
  return { activities, plan, taskQueue: options.taskQueue ?? spec.task_queue };
}

export interface BuildRuntimeOptions extends AssembleYamlRuntimeOptions {
  /**
   * Path to the workflow module that registers `typefluxYamlWorkflow`. Defaults to this
   * package's BUILT module (`dist/workflows.js`); override it to point at the TS source
   * (e.g. when running through a bundler/vitest where `dist` is not used).
   */
  workflowsPath?: string;
  /** Passthrough to `createTypefluxWorker` (connection, namespace, …). */
  worker?: Omit<TypefluxWorkerOptions, "taskQueue" | "activities" | "workflowsPath" | "workflowBundle">;
  /**
   * The crypto-shred keystore backend for a spec declaring
   * `payload_codec.subject_scope` (#715 slice 4). Absent ⇒ the process-local in-memory
   * reference backend is built (test/dev ONLY — a starter and worker in different
   * processes would mint different keys for the same subject; see docs/privacy.md).
   */
  subjectKeystore?: SubjectKeystore;
}

export interface TypefluxYamlRuntime {
  worker: Awaited<ReturnType<typeof createTypefluxWorker>>;
  plan: WorkflowPlan;
  /** sha256 over the plan's canonical JSON (#530) — stamped into every start's memo. */
  specDigest: string;
  taskQueue: string;
  /**
   * The payload-codec `dataConverter` this runtime wired into its worker (#188), or
   * `undefined` when the spec declares no `runtime.temporal.payload_codec`. Callers that
   * START workflows against this runtime MUST construct their Temporal `Client` with it —
   * `new Client({ connection, namespace, dataConverter: runtime.dataConverter })` — so a
   * workflow's START payloads (plan + input) are encrypted in history exactly like the
   * results the worker encrypts. `runWorkflow` fail-closes (D188-3) when a codec is declared
   * but the supplied client lacks one. Typed like `@temporalio/client`'s
   * `ClientOptions.dataConverter`.
   */
  dataConverter?: DataConverter;
  /** Start the YAML workflow and await its result. */
  runWorkflow(
    client: Parameters<typeof executeWorkflow>[0],
    input: unknown,
    options: {
      workflowId: string;
      /**
       * Explicit subject id override (#715 slice 1). A NON-EMPTY override wins
       * over the spec `subjects:` extraction; an empty array is treated as "no
       * override" and falls through to extraction (subjects are erasure-critical
       * — there is no "explicitly no subjects" opt-out of a declared block).
       * Stamps the `TypefluxSubjectIds` keyword-list search attribute and fans
       * to the observer / cache carriers.
       */
      subjectIds?: readonly string[];
    },
  ): Promise<unknown>;
  /**
   * Await every trace still in flight from the runtime-built observer
   * (streams per activity; this is the graceful-exit drain for short-lived
   * processes). BEST-EFFORT: a failed tail flush is logged, never thrown —
   * safe to call from a finally. No-op when the spec doesn't trace or the
   * caller supplied their own observer (whose lifecycle they own).
   */
  drainObservability(): Promise<void>;
}

/** The auto-observer the OOTB wiring built (kept for the graceful-exit drain). */
type AutoWiredObserver =
  | NonNullable<Awaited<ReturnType<typeof langfuseObserverFromSpec>>>
  | NonNullable<Awaited<ReturnType<typeof langsmithObserverFromSpec>>>;

/**
 * The ONE out-of-the-box options assembly (Python `build_runtime` parity), shared by
 * {@link buildRuntime} and the `typeflux-yaml-worker` entrypoint's `--preflight` (#687
 * review: preflight MUST compose exactly the wiring the live worker runs with, or the
 * generated command fails supported OOTB deployments before the readiness marker):
 *
 * - Governance pre-flight FIRST, even before the auto-observer: a non-compliant spec
 *   must refuse to build before ANYTHING is constructed — including the langfuse SDK
 *   client — and its error must be the policy violation, never a missing-peer install
 *   hint (finder). Idempotent; assembleYamlRuntime re-runs it as its own fail-closed
 *   contract. Includes the closure admission (#300/#298) — cascade + tree-wide ceilings.
 * - Out-of-the-box langfuse/langsmith OBSERVERS: a spec declaring either tracing backend
 *   traces with NO caller wiring — the official SDK is lazy-loaded and the observer
 *   built from the environment (each helper self-gates on its own type). An explicit
 *   `observer` always wins; `type: none` is unchanged.
 * - Out-of-the-box langfuse/langsmith PROMPT REGISTRIES (Python `_build_registry`):
 *   an explicit `registry`/`registryTransport` wins; `inline` self-builds in
 *   `registryFromSpec`. Unlike the observer, a declared-but-unbuildable registry THROWS
 *   inside the helper — prompts are load-bearing, so there is no untraced-style degrade.
 * - Out-of-the-box vendor PROVIDERS (Python `_build_provider`): openai/anthropic/gemini
 *   execute with NO caller wiring — the official SDK is lazy-loaded and the thin
 *   transport built from the environment. An explicit `provider`/`transports` wins.
 *   Like the registry, a missing credential THROWS: a run cannot degrade past its
 *   provider.
 *
 * Returns the assembled options plus the auto-built observer (for the drain helper).
 */
export async function autoWireRuntimeOptions<T extends AssembleYamlRuntimeOptions>(
  spec: TypefluxYamlSpec,
  options: T,
): Promise<{ options: T; autoObserver: AutoWiredObserver | undefined }> {
  if (options.policy !== undefined) {
    enforcePolicyPreflight(spec, options.policy, options.subworkflows);
  }
  const autoObserver =
    options.observer === undefined
      ? ((await langfuseObserverFromSpec(spec)) ?? (await langsmithObserverFromSpec(spec)))
      : undefined;
  const autoRegistryTransport =
    options.registry === undefined && options.registryTransport === undefined
      ? ((await langfuseRegistryTransportFromSpec(spec)) ?? (await langsmithRegistryTransportFromSpec(spec)))
      : undefined;
  const autoTransports =
    options.provider === undefined && options.transports === undefined
      ? await providerTransportsFromSpec(spec)
      : undefined;
  return {
    options: {
      ...options,
      ...(autoObserver !== undefined ? { observer: autoObserver } : {}),
      ...(autoRegistryTransport !== undefined ? { registryTransport: autoRegistryTransport } : {}),
      ...(autoTransports !== undefined ? { transports: autoTransports } : {}),
    },
    autoObserver: autoObserver ?? undefined,
  };
}

/**
 * Build a Temporal worker that serves the spec's activities + the generic YAML
 * workflow, plus a `runWorkflow` helper that starts `typefluxYamlWorkflow` with the
 * derived plan. `createTypefluxWorker` connects to a server.
 */
export async function buildRuntime(
  spec: TypefluxYamlSpec,
  options: BuildRuntimeOptions,
): Promise<TypefluxYamlRuntime> {
  // The OOTB auto-wiring + governance-first ordering live in ONE shared assembly (see
  // autoWireRuntimeOptions) so the worker entrypoint's --preflight composes exactly this.
  const { options: assembled, autoObserver } = await autoWireRuntimeOptions(spec, options);
  const { activities, plan, taskQueue } = assembleYamlRuntime(spec, assembled);
  // Build the AES-256-GCM codec ONCE from the resolved spec (#188), fail-closed on a
  // missing/wrong-length key. Absent payload_codec ⇒ no codec ⇒ plaintext (digests
  // unchanged). A declared `subject_scope` (#715 slice 4) wraps it in the
  // subject-scoped codec (per-subject crypto-shred).
  let subjectKeystore = options.subjectKeystore;
  if (spec.runtime.temporal.payload_codec?.subject_scope !== undefined && subjectKeystore === undefined) {
    // SOLE-OWNER justification for the in-memory default (#715 Bugbot): buildRuntime
    // constructs the worker AND the start-path data converter in this ONE process, and
    // runWorkflow fail-closes unless the caller's Client carries that same converter —
    // so every encode and decode shares one codec + keystore instance, making the
    // process-local reference backend coherent HERE (and only here; the live suite
    // proves the round-trip). Split starter/worker deployments (worker-entry) must
    // inject a shared backend — worker-entry enforces that loudly.
    subjectKeystore = new InMemorySubjectKeystore();
  }
  const codec = buildSubjectAwarePayloadCodec(spec.runtime.temporal.payload_codec, subjectKeystore);
  if (codec instanceof SubjectScopedPayloadCodec && specReferencesSubworkflows(spec.workflow.steps)) {
    // #715 slice 4 boundary: a child's start payloads are encoded (with the CHILD's
    // serialization context) before the child exists, so its subject binding cannot be
    // resolved. Reject the combination loudly rather than fail mid-workflow.
    throw new Error(
      "runtime.temporal.payload_codec.subject_scope does not yet support sub-workflow " +
        "composition (#715 slice 4): a child's input is encoded before the child execution " +
        "exists, so its subject binding cannot be resolved. Remove subject_scope or the " +
        "sub-workflow references.",
    );
  }
  // The dataConverter that carries the spec-declared codec (#188). Wired into BOTH the worker
  // (below) AND exposed on the returned runtime so callers build their START-path Client with
  // the SAME converter — otherwise workflow start payloads (plan + input) ride to history as
  // PLAINTEXT while the worker encrypts results (D188-3 fail-closed; Python wires both sites).
  const codecDataConverter: DataConverter | undefined =
    codec !== undefined ? { payloadCodecs: [codec] } : undefined;
  const workerOptions = {
    ...(options.worker ?? {}),
    taskQueue,
    activities,
    // The generic interpreter ships in this package's built workflow module; callers
    // running from source (vitest/bundler) override `workflowsPath` to the `.ts`.
    workflowsPath: options.workflowsPath ?? new URL("./workflows.js", import.meta.url).pathname,
  };
  if (codec !== undefined) {
    // FAIL-CLOSED (D188-3): a declared codec must never be silently dropped in favor of a
    // caller's converter — that would ride PII to Temporal as PLAINTEXT. A caller supplying
    // their own dataConverter alongside a spec-declared codec is ambiguous; reject it rather
    // than guess. (Drop the spec block to bring your own converter.)
    // Detect a caller's converter at EITHER level: the top-level `dataConverter` OR the
    // nested `workerOptions.workerOptions?.dataConverter` passthrough (TypefluxWorkerOptions),
    // which `workerCreateOptions` spreads and the codec would otherwise silently override.
    if (
      workerOptions.dataConverter !== undefined ||
      workerOptions.workerOptions?.dataConverter !== undefined
    ) {
      throw new Error(
        "runtime.temporal.payload_codec is declared but a worker dataConverter was also supplied; " +
          "remove one — a spec-declared codec must not be silently overridden (fail-closed, #188)",
      );
    }
    workerOptions.dataConverter = codecDataConverter;
  }
  const worker = await createTypefluxWorker(workerOptions);
  const specDigest = workflowPlanDigest(plan);
  const identityMemo = workflowIdentityMemo(spec, specDigest);
  // Frozen workflow.version (#530): checked once per runtime instance, before the
  // first start. Python enforces at worker start (its worker can replay an edited
  // graph); TS's plan-as-argument makes in-flight executions immune, so NEW starts
  // are the only thing a frozen label protects — start time is the natural gate.
  let frozenVersionChecked: Promise<void> | undefined;
  return {
    worker,
    plan,
    specDigest,
    taskQueue,
    // exactOptionalPropertyTypes: only present the field when a codec is declared, so a
    // no-codec runtime reports `dataConverter` absent (⇒ runWorkflow performs no check).
    ...(codecDataConverter !== undefined ? { dataConverter: codecDataConverter } : {}),
    drainObservability: async () => {
      try {
        await autoObserver?.drain();
      } catch (error) {
        // Graceful-exit helper: a failed tail flush must not abort the
        // caller's finally (connection close) — tracing is observability,
        // not control flow (Bugbot). Strict callers drain the writer they
        // injected themselves.
        console.error("observability drain failed:", error);
      }
    },
    runWorkflow: async (client, input, runOptions) => {
      // #715 slice 1: a NON-EMPTY explicit subjectIds override wins over the
      // declarative `subjects:` extraction; an EMPTY override falls through to
      // extraction (see effectiveSubjectIds — `subjectIds: []` must never
      // silently bypass a declared block, #715 review round 2). A required
      // selector that contributes zero ids throws HERE, at start.
      const subjectIds = effectiveSubjectIds(
        runOptions.subjectIds,
        input,
        (spec.workflow.subjects ?? []).map((s) => ({ fromPath: s.from, required: s.required })),
      );
      if (codec instanceof SubjectScopedPayloadCodec) {
        // #715 slice 4: pin the execution's subject set for the subject-scoped codec
        // BEFORE the start encodes the input (the execution does not exist in
        // visibility yet, so the registry is the only resolvable channel). Registers
        // the SAME ids stamped into TypefluxSubjectIds; the empty set pins "no
        // subjects". Also (re-)bind the caller's client as the visibility fallback
        // for executions other processes started.
        codec.bindings.register(runOptions.workflowId, subjectIds);
        codec.bindings.bindClient(client as unknown as SubjectBindingClient);
      }
      if (codec !== undefined) {
        // FAIL-CLOSED (D188-3): the worker encrypts activity/result payloads, but a workflow's
        // START payloads (plan + input) are serialized by the CALLER-supplied Client. If that
        // Client was not built with the codec, the start rides to Temporal history as PLAINTEXT
        // while the worker encrypts everything else — silently breaking the codec's promise.
        // Codec instances can't be meaningfully deep-compared across the client boundary, so the
        // practical check is PRESENCE of any payloadCodec on the client's loaded dataConverter.
        // `options.loadedDataConverter.payloadCodecs` is the robust surface: the SDK always
        // normalizes it to an array (`[]` when the caller passed no converter, since
        // defaultBaseClientOptions sets `dataConverter: {}`), unlike the raw `options.dataConverter`.
        const clientPayloadCodecs = client.options?.loadedDataConverter?.payloadCodecs;
        if (clientPayloadCodecs === undefined || clientPayloadCodecs.length === 0) {
          throw new Error(
            "runtime.temporal.payload_codec is declared but the Temporal Client used to start the " +
              "workflow was not built with a payload codec — start payloads (plan + input) would be " +
              "persisted in history as PLAINTEXT. Pass `dataConverter: runtime.dataConverter` when " +
              "constructing the Client (fail-closed, #188).",
          );
        }
      }
      if (spec.workflow.version !== undefined) {
        // Cached promise: one visibility query per runtime instance. A digest
        // mismatch rejects — and KEEPS rejecting on retry (the graph didn't
        // change); visibility failures warn-and-resolve inside the check.
        frozenVersionChecked ??= enforceFrozenWorkflowVersion(client as unknown as WorkflowListClient, {
          workflowType: YAML_WORKFLOW_TYPE,
          workflowName: spec.workflow.name,
          project: spec.project,
          versionLabel: spec.workflow.version,
          planDigest: specDigest,
          // Narrows the scan to THIS workflow's starts when the attribute is
          // configured (it is stamped with the logical name on every start).
          ...(spec.runtime.temporal.workflow_search_attribute !== undefined
            ? {
                searchAttribute: {
                  name: spec.runtime.temporal.workflow_search_attribute,
                  value: spec.workflow.name,
                },
              }
            : {}),
        });
        await frozenVersionChecked;
      }
      const handle = await startWorkflow(client, {
        workflowType: YAML_WORKFLOW_TYPE,
        taskQueue,
        workflowId: runOptions.workflowId,
        // The optional third arg is the non-identity start context (#55 §6): the configured
        // search attribute name, so the interpreter can stamp each CHILD's own logical name into
        // it. Passed only when configured, so pre-existing starts stay byte-identical (2-arg).
        args:
          spec.runtime.temporal.workflow_search_attribute !== undefined
            ? [plan, input, { searchAttribute: spec.runtime.temporal.workflow_search_attribute }]
            : [plan, input],
        // Identity memo on EVERY start (Python _workflow_start_memo): the digest is
        // what a future frozen-label check compares against.
        startOptions: { memo: identityMemo },
        // The logical-name keyword attribute (#495): one visibility query across
        // versioned workflow types. Must be REGISTERED in the namespace (as in Python).
        ...(spec.runtime.temporal.workflow_search_attribute !== undefined
          ? {
              keywordSearchAttributes: {
                [spec.runtime.temporal.workflow_search_attribute]: spec.workflow.name,
              },
            }
          : {}),
        // The subject->execution index (#715 slice 1): a fixed KEYWORD_LIST attribute
        // stamped whenever the execution has subjects, independent of the opt-in
        // logical-name attribute. Must be registered on the namespace as a KeywordList.
        ...(subjectIds.length > 0
          ? { keywordListSearchAttributes: { [SUBJECT_IDS_SEARCH_ATTRIBUTE]: subjectIds } }
          : {}),
      });
      // Workflow-level facts on the grouped PARENT trace (Python parity: the
      // workflow span carries the run's input/output + identity metadata).
      // Redacted + best-effort inside the writer; identity = the start memo.
      const runId = handle.firstExecutionRunId;
      await autoObserver?.recordWorkflowRun({
        runId,
        workflowId: runOptions.workflowId,
        input,
        metadata: { ...identityMemo, workflow_id: runOptions.workflowId },
        // #715 slice 1: the primary subject becomes the trace's native userId and
        // every subject a `typeflux.subject:{id}` tag (set inside the observer).
        ...(subjectIds.length > 0 ? { subjectIds } : {}),
      });
      try {
        const result = await handle.result();
        await autoObserver?.recordWorkflowRun({
          runId,
          workflowId: runOptions.workflowId,
          output: result,
          // Re-carried on every record (#715 review, finding 3): a Langfuse upsert
          // replaces the fields it carries, so the completion upsert must not omit
          // the subject identity the start recorded.
          ...(subjectIds.length > 0 ? { subjectIds } : {}),
        });
        return result;
      } catch (error) {
        await autoObserver?.recordWorkflowRun({
          runId,
          workflowId: runOptions.workflowId,
          error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
          ...(subjectIds.length > 0 ? { subjectIds } : {}),
        });
        throw error;
      }
    },
  };
}

/**
 * Build the artifact admission policy from `runtime.artifacts` (Python `ArtifactRuntimeSpec`).
 * Known divergence (#495): Python resolves RELATIVE local_roots against the spec FILE's
 * directory; `loadYamlSpec` is text-only (no file path to anchor to), so relative roots resolve
 * against the process cwd at call time — prefer absolute roots (or env interpolation) until a
 * file-loading API lands.
 */
/**
 * Map a YAML limit tier (snake_case, absent-aware) onto the core's `ProviderCallLimits`;
 * an EMPTY tier maps to absent (Python `_provider_call_limits` returns None) so it never
 * shadows a broader tier.
 */
function callLimits(
  spec: { max_concurrent?: number | undefined; min_interval_seconds?: number | undefined } | undefined,
): ProviderCallLimits | undefined {
  if (spec === undefined || (spec.max_concurrent === undefined && spec.min_interval_seconds === undefined)) {
    return undefined;
  }
  return {
    ...(spec.max_concurrent !== undefined ? { maxConcurrent: spec.max_concurrent } : {}),
    ...(spec.min_interval_seconds !== undefined ? { minIntervalSeconds: spec.min_interval_seconds } : {}),
  };
}

function artifactPolicyFromSpec(spec: TypefluxYamlSpec): ArtifactPolicy | undefined {
  const runtime = spec.runtime.artifacts;
  if (runtime === undefined) {
    return undefined;
  }
  return artifactPolicy({
    local_roots: runtime.local_roots,
    allowed_source_kinds: runtime.allowed_sources,
    allowed_media_types: runtime.allowed_media_types,
    ...(runtime.max_bytes != null ? { max_bytes: runtime.max_bytes } : {}),
  });
}
