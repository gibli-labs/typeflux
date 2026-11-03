/** Shared policy, provider, registry and activity assembly. No Temporal SDK dependency. */
import { runtimeEngine, validateLocalCapabilities, requireTemporalWorkflowGraph } from "./engine.js";
import {
  artifactInputResolver,
  type ExecuteActivityOptions,
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

import { defineActivitiesFromSpec, type ActivityResolver } from "./build-activities.js";
import { assertConsistentComposedObservability } from "./observability-composition.js";
import { composeRuntimeRegistry, type RegistrySource } from "./registry-composition.js";
import { workflowPlanFromSpec, type SubworkflowSpecResolver } from "./build-workflow.js";
import type { ComposedProjectPolicy } from "./policy-composition.js";
import { enforcePolicyCompliance, ProjectPolicyEnforcementError, RuntimePolicyGuard } from "./policy-enforcement.js";
import {
  assertRiskTierEnforced, validateSubworkflowClosurePolicy } from "./project-enforcement.js";
import {
  workflowPlanDigest,
} from "./frozen-version.js";
import {
  defaultModelForProviderType,
  ENGINE_PROVIDER_RETRY_DEFAULTS,
  providerFromSpec,
  type ProviderTransports,
} from "./provider-from-spec.js";
import { providerParamsRecord, type TypefluxYamlSpec } from "./spec.js";
import {
  flattenCompensationActivities,
  flattenPlanSteps,
  flattenSubworkflowSteps,
  type WorkflowPlan,
  type WorkflowPlanStep,
} from "./workflow-plan.js";


export interface YamlRuntimeOptions {
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

export interface AssembledRuntimeServices {
  descriptors: Record<string, ActivityDescriptor<any, any>>;
  plan: WorkflowPlan;
  options: ExecuteActivityOptions;
}

export function enforcePolicyPreflight(
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

export function assembleRuntimeServices(
  spec: TypefluxYamlSpec,
  options: YamlRuntimeOptions,
  validate?: (descriptors: AssembledRuntimeServices["descriptors"], plan: WorkflowPlan) => void,
): AssembledRuntimeServices {

  // Governance pre-flight (#454), FIRST: a non-compliant spec must refuse to build
  // before any provider/registry/worker is constructed. Fail-closed — throws
  // ProjectPolicyEnforcementError carrying the failed checks. Includes the closure
  // admission (#300/#298): cascade + tree-wide ceilings run on this entry too.
  if (options.policy !== undefined) {
    enforcePolicyPreflight(spec, options.policy, options.subworkflows);
    // Required-observability gate (#756), at THIS site so every build route traverses it —
    // including a direct assembleRuntimeServices caller that never runs autoWireRuntimeOptions.
    if (options.observer === undefined) {
      enforceRequiredObservabilityResolves(spec, options.policy);
    }
  }
  if (runtimeEngine(spec) === "local") validateLocalCapabilities(spec, options.subworkflows);
  else requireTemporalWorkflowGraph(spec, options.subworkflows);
  // #795 (after the governance pre-flight, which stays FIRST; unconditional — a spec needs
  // no policy for its own declaration to bind): a declared targeted-cache-erasure
  // requirement fails CLOSED at assembly when the wired store cannot honor it — wiring a
  // store that per-subject erasure cannot precisely reach would make the declaration a
  // dead letter.
  const assertCacheStore = (current: TypefluxYamlSpec): void => {
    if (current.runtime.cache_erasure === "targeted" && options.cacheStore !== undefined) {
      if (!isSubjectErasableCacheStore(options.cacheStore)) {
        throw new Error(
          "runtime.cache_erasure is 'targeted' but the wired cacheStore does not implement " +
            "eraseSubject (SubjectErasableCacheStore) — per-subject invalidation is declared " +
            "REQUIRED, so this store must not be wired. Implement eraseSubject on the store, " +
            "or drop the cache_erasure declaration.",
        );
      }
    }
  };
  assertCacheStore(spec);
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
  const descriptors = defineActivitiesFromSpec(spec, {
    schemas: options.schemas,
    ...(options.hooks !== undefined ? { hooks: options.hooks } : {}),
    ...(options.outputChecks !== undefined ? { outputChecks: options.outputChecks } : {}),
    ...(options.moderators !== undefined ? { moderators: options.moderators } : {}),
  });
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
      assertCacheStore(childSpec);
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
  // Code-defined descriptors (#496) join the spec-defined ones; the duplicate-name
  // guard in buildTemporalActivities makes a collision with a spec definition loud.
  const extraDescriptors = Object.values(options.extraActivities ?? {});
  const activities: Record<string, ActivityDescriptor<any, any>> = Object.create(null);
  for (const descriptor of [...Object.values(descriptors), ...extraDescriptors]) {
    if (Object.hasOwn(activities, descriptor.name)) {
      throw new Error(`duplicate activity name: ${JSON.stringify(descriptor.name)}`);
    }
    activities[descriptor.name] = descriptor;
  }
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
  validate?.(activities, plan);
  const specBuiltProvider = options.provider === undefined && options.transports !== undefined;
  const provider =
    options.provider ?? (options.transports !== undefined ? providerFromSpec(spec, options.transports) : undefined);
  if (provider === undefined) {
    throw new Error(
      "buildRuntime: provide either `provider` (a ModelProvider) or `transports` " +
        "(to build it from spec.runtime.provider)",
    );
  }
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
  return { descriptors: activities, plan, options: sharedOptions };
}

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
