/**
 * Build SDK objects from a validated spec (parity Epic 5, #452; Python
 * `yaml/runtime.py` `_build_registry` + `yaml/imports.py` `collect_activities`).
 *
 * Resolution divergence (documented, behavioral parity): Python resolves a
 * definition's input/output schemas + activity functions by import path
 * (`schemas:ClaimInput`). TS takes an INJECTED resolver — the caller supplies a
 * `name -> ZodSchema` map (and optional hooks) — so this stays unit-testable without
 * dynamic import. The same descriptors + registry are built; only the resolution
 * mechanism differs.
 */

import {
  type ActivityDescriptor,
  type ActivityHook,
  type ActivityOutputCheck,
  artifactAttachment,
  artifactInput,
  type ArtifactInput,
  type ChatContent,
  type ChatMessage,
  defineActivity,
  type InlinePromptValue,
  InlinePromptRegistry,
  type ModerationConfig,
  type Moderator,
  type PromptRef,
  type PromptRegistry,
  type RegistryTransport,
  type ResolvedPrompt,
  TransportPromptRegistry,
} from "@typeflux/temporal";
import type { z } from "zod";

import {
  providerParamsRecord,
  type ActivityDefinitionSpec,
  type InlinePromptMessageSpec,
  type InlinePromptSpec,
  type PromptRefSpec,
  type TypefluxYamlSpec,
} from "./spec.js";

/** Convert a spec prompt (a bare name or a `{ name, version|label, type }` ref) to a `PromptRef`. */
export function toPromptRef(prompt: string | PromptRefSpec): PromptRef {
  if (typeof prompt === "string") {
    return { name: prompt };
  }
  const ref: PromptRef = { name: prompt.name };
  if (prompt.version !== undefined) {
    ref.version = prompt.version;
  }
  if (prompt.label !== undefined) {
    ref.label = prompt.label;
  }
  if (prompt.type !== undefined) {
    ref.promptType = prompt.type;
  }
  return ref;
}

function toChatMessage(message: InlinePromptMessageSpec): ChatMessage {
  // Content parts are fully validated by `inlineContentPartSpec` (the spec form of the core
  // `ContentPart` union, #490). The cast bridges only zod's `.optional()` inference
  // (`text?: string | undefined`) vs the core's `text?: string` under
  // `exactOptionalPropertyTypes` — the shapes are otherwise field-for-field identical.
  const out: ChatMessage = { role: message.role, content: message.content as ChatContent };
  if (message.name !== undefined) {
    out.name = message.name;
  }
  return out;
}

function inlinePromptToResolved(name: string, spec: InlinePromptSpec): ResolvedPrompt {
  const resolved: ResolvedPrompt = {
    ref: { name },
    messages: spec.messages.map(toChatMessage),
    resolvedVersion: "inline",
  };
  if (spec.model !== undefined) {
    resolved.model = spec.model;
  }
  if (spec.temperature !== undefined) {
    resolved.temperature = spec.temperature;
  }
  if (spec.provider_params !== undefined) {
    // The prompt-layer params (#495) — resolveCall merges them UNDER the prompt's
    // dedicated model/temperature fields (the spec validated they match anyway).
    resolved.providerParams = providerParamsRecord(spec.provider_params);
  }
  return resolved;
}

/**
 * Convert one spec registry prompt entry (a bare string or an inline-prompt spec) to the
 * core `InlinePromptValue`. Shared by {@link inlineRegistryFromSpec} and the sub-workflow
 * registry composition (#748), which merges several specs' prompt maps into one served
 * registry before constructing the `InlinePromptRegistry`.
 */
export function inlinePromptValueFromSpec(name: string, value: string | InlinePromptSpec): InlinePromptValue {
  return typeof value === "string" ? value : inlinePromptToResolved(name, value);
}

/**
 * Build an `InlinePromptRegistry` from `runtime.registry` when its `type` is
 * `inline`. A string prompt becomes a user message; an inline-prompt spec becomes a
 * `ResolvedPrompt`. Other registry types (langfuse/langsmith/custom) need a client
 * and return `undefined` — the caller supplies that registry (a later PR wires them).
 */
export function inlineRegistryFromSpec(spec: TypefluxYamlSpec): InlinePromptRegistry | undefined {
  const registry = spec.runtime.registry;
  if (registry.type !== "inline") {
    return undefined;
  }
  const prompts: Record<string, InlinePromptValue> = {};
  for (const [name, value] of Object.entries(registry.prompts ?? {})) {
    prompts[name] = inlinePromptValueFromSpec(name, value);
  }
  return new InlinePromptRegistry(prompts);
}

/**
 * Build the prompt registry from `runtime.registry`: `inline` → an `InlinePromptRegistry`;
 * `langfuse`/`langsmith`/`custom` → a `TransportPromptRegistry` over the injected `transport`
 * (the registry's `label` becomes the default label), or `undefined` if no transport is
 * supplied (the caller must then provide a registry directly).
 */
export function registryFromSpec(spec: TypefluxYamlSpec, transport?: RegistryTransport): PromptRegistry | undefined {
  const registry = spec.runtime.registry;
  if (registry.type === "inline") {
    return inlineRegistryFromSpec(spec);
  }
  if (transport === undefined) {
    return undefined;
  }
  // Truthy guard (not `!== undefined`): a `registry.label: ""` is no override (Python parity).
  return new TransportPromptRegistry(transport, {
    ...(registry.label ? { defaultLabel: registry.label } : {}),
    // #495: unless the spec opts in, a backend prompt's model is stripped at
    // resolution — the spec stays authoritative for the execution model.
    ...(spec.runtime.provider.allow_prompt_model_override === true ? { allowPromptModelOverride: true } : {}),
  });
}

/** Caller-injected resolution for activity definitions. */
export interface ActivityResolver {
  /** Maps a definition's `input`/`output` schema ref (e.g. `"schemas:ClaimInput"`) to a Zod schema. */
  schemas: Record<string, z.ZodType>;
  /** Optional per-activity normalization hook, keyed by the definition `name`. */
  hooks?: Record<string, ActivityHook<unknown, unknown>>;
  /**
   * Optional per-activity input-aware output check (#745), keyed by the definition `name` —
   * the pre-acceptance analogue of {@link hooks}. A definition's grounding contract cannot be
   * expressed in YAML (it needs the parsed INPUT + candidate output), so the function is
   * injected here exactly as `hooks` is; a rejection FEEDS the validation repair loop (the
   * model gets its `validation_retries` chances to self-correct) rather than failing loud like
   * a hook throw. Wired onto the descriptor's `outputCheck` — no definition change required.
   */
  outputChecks?: Record<string, ActivityOutputCheck<unknown, unknown>>;
  /**
   * Per-activity output moderator, keyed by the definition `name`. A `Moderator` is a function
   * (not expressible in YAML), so the spec's `moderation` block selects intent + `on_violation`
   * while the function is injected here. A definition declaring `moderation` without a matching
   * entry throws.
   */
  moderators?: Record<string, Moderator<unknown>>;
}

/**
 * Build `defineActivity` descriptors from `activities.definitions`, resolving each
 * input/output schema ref via `resolver.schemas`, attaching an optional injected hook,
 * and wiring a definition's `moderation` to an injected moderator. The timeout/`retry`
 * fields are consumed by `workflowPlanFromSpec` (#479/#484/#486); a session `cache`
 * block is rejected at load until #478. Throws on a duplicate name, an unresolved
 * schema ref, or moderation without an injected moderator.
 */
/** The manifest's schema identity name: `"schemas:Item"` → `"Item"`. */
function schemaRefName(ref: string): string {
  const idx = ref.indexOf(":");
  return idx >= 0 ? ref.slice(idx + 1) : ref;
}

export function defineActivitiesFromSpec(
  spec: TypefluxYamlSpec,
  resolver: ActivityResolver,
): Record<string, ActivityDescriptor<z.ZodType, z.ZodType>> {
  const out: Record<string, ActivityDescriptor<z.ZodType, z.ZodType>> = Object.create(null);
  for (const def of spec.activities.definitions ?? []) {
    if (Object.hasOwn(out, def.name)) {
      throw new Error(`duplicate activity definition: ${JSON.stringify(def.name)}`);
    }
    const input = resolveSchema(resolver.schemas, def.input, def.name, "input");
    const output = resolveSchema(resolver.schemas, def.output, def.name, "output");
    // Own-property lookup (not bracket access) so an activity named like an inherited
    // member doesn't pick up a prototype method as its hook.
    const hook =
      resolver.hooks !== undefined && Object.hasOwn(resolver.hooks, def.name)
        ? resolver.hooks[def.name]
        : undefined;
    // Own-property lookup (matching the hook/moderator/schema lookups) so an activity named
    // like an inherited member can't pick up a prototype method as its output check.
    const outputCheck =
      resolver.outputChecks !== undefined && Object.hasOwn(resolver.outputChecks, def.name)
        ? resolver.outputChecks[def.name]
        : undefined;
    const moderation = resolveModeration(def, resolver.moderators);
    const artifacts = artifactInputsFromSpec(def);
    out[def.name] = defineActivity({
      name: def.name,
      ...(artifacts !== undefined ? { artifacts } : {}),
      prompt: toPromptRef(def.prompt),
      input,
      output,
      // Schema identity + source for the execution manifest (Python parity:
      // its manifests carry the model class name and a yaml source).
      inputSchemaName: schemaRefName(def.input),
      outputSchemaName: schemaRefName(def.output),
      definitionSource: { kind: "yaml", yamlName: spec.name, yamlProject: spec.project },
      validationRetries: def.validation_retries,
      ...(hook !== undefined ? { hook } : {}),
      ...(outputCheck !== undefined ? { outputCheck } : {}),
      ...(moderation !== undefined ? { moderation } : {}),
      // The activity-layer behavior params (#495) — the highest-precedence merge layer.
      ...(def.provider_params !== undefined ? { providerParams: providerParamsRecord(def.provider_params) } : {}),
      // The provider session cache (#478): carried on the descriptor so the worker
      // registers the prep/release activities and prep reads enabled/ttl.
      ...(def.cache !== undefined
        ? {
            sessionCache: {
              enabled: def.cache.enabled,
              ...(def.cache.ttl_seconds !== undefined ? { ttlSeconds: def.cache.ttl_seconds } : {}),
            },
          }
        : {}),
      // The cross-run output cache (#398/#753): carried on the descriptor's `cache` (the core
      // cross-run CacheConfig, DISTINCT from `sessionCache` above) so `executeActivity` memoizes
      // the validated output across runs — but only when a `cacheStore` is threaded into the
      // runtime (assembleYamlRuntime/buildRuntime `cacheStore`). No store ⇒ inert (execute.ts
      // gates on `descriptor.cache?.enabled && options.cacheStore`).
      ...(def.cross_run_cache !== undefined
        ? {
            cache: {
              enabled: def.cross_run_cache.enabled,
              ...(def.cross_run_cache.bypass_reads_env !== undefined
                ? { bypassReadsEnv: def.cross_run_cache.bypass_reads_env }
                : {}),
            },
          }
        : {}),
    });
  }
  return out;
}

/** Build the moderation config for a definition that declares it (the moderator is injected). */
function resolveModeration(
  def: ActivityDefinitionSpec,
  moderators: Record<string, Moderator<unknown>> | undefined,
): ModerationConfig<unknown> | undefined {
  if (def.moderation === undefined) {
    return undefined;
  }
  // `Object.hasOwn` (not bracket access): an activity named like an inherited member
  // (`toString`, `constructor`) must NOT pick up a prototype method as its moderator.
  const moderator = moderators !== undefined && Object.hasOwn(moderators, def.name) ? moderators[def.name] : undefined;
  if (moderator === undefined) {
    throw new Error(
      `activity ${JSON.stringify(def.name)} declares moderation but no moderator was provided ` +
        `in resolver.moderators`,
    );
  }
  return { moderator, onViolation: def.moderation.on_violation };
}

function resolveSchema(
  schemas: Record<string, z.ZodType>,
  ref: string,
  activity: string,
  role: "input" | "output",
): z.ZodType {
  // Own-property lookup (not `schemas[ref]`): a ref is any string, so a ref named like
  // an inherited member ("constructor", "toString", …) would otherwise resolve to a
  // prototype method instead of missing — bypassing the unresolved-ref error and handing
  // a non-Zod value downstream (matches the hook/moderator lookups above).
  const schema = Object.hasOwn(schemas, ref) ? schemas[ref] : undefined;
  if (schema === undefined) {
    throw new Error(
      `unresolved ${role} schema ${JSON.stringify(ref)} for activity ${JSON.stringify(activity)} ` +
        `(provide it in resolver.schemas)`,
    );
  }
  return schema;
}

// Re-exported so `ActivityDefinitionSpec` is reachable from this module's consumers.
export type { ActivityDefinitionSpec };

/**
 * Map a definition's declared `artifacts` (spec form; YAML key `from`) to the core
 * `ArtifactInput` contract (#481) — the validated factories enforce the same invariants as
 * Python (`from` must start `input.`, attach text non-empty, ...).
 */
function artifactInputsFromSpec(def: ActivityDefinitionSpec): ArtifactInput[] | undefined {
  if (def.artifacts === undefined || def.artifacts.length === 0) {
    return undefined;
  }
  return def.artifacts.map((spec) =>
    artifactInput({
      name: spec.name,
      from_path: spec.from,
      required: spec.required,
      ...(spec.kind !== undefined ? { kind: spec.kind } : {}),
      media_types: spec.media_types,
      ...(spec.max_count != null ? { max_count: spec.max_count } : {}),
      ...(spec.max_bytes != null ? { max_bytes: spec.max_bytes } : {}),
      ...(spec.cache !== undefined ? { cache_role: spec.cache } : {}),
      ...(spec.attach !== undefined
        ? {
            attach: artifactAttachment({
              role: spec.attach.role,
              ...(spec.attach.text !== undefined ? { text: spec.attach.text } : {}),
            }),
          }
        : {}),
    }),
  );
}
