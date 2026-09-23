/**
 * `defineActivity` (#400): the typed-activity authoring primitive, the TS
 * counterpart to Python's `AIActivity` / `@ai_activity.defn`.
 *
 * Input/output are Zod schemas. The output is mapped to a provider-safe JSON
 * Schema (#389) for structured-output dispatch, and both schemas are
 * content-hashed (#390) for manifest / cache identity (#391). The schemas map
 * via Zod 4's native `z.toJSONSchema()`.
 */

import type { ArtifactInput } from "./artifacts.js";
import { z } from "zod";

import { schemaHash } from "./manifest-hashing.js";
import type { ModerationConfig } from "./moderation.js";
import type { PromptRef } from "./prompt-ref.js";
import { type JsonSchema, toProviderSafe } from "./provider-schema.js";

/**
 * Runtime context passed to a context-aware hook (parity with the Python
 * `ActivityContext`, #397). Not serialized — a runtime object, hence camelCase.
 */
export interface ActivityContext {
  activityName: string;
  namespace?: string;
  workflowId?: string;
  runId?: string;
  activityId?: string;
  attempt?: number;
  taskQueue?: string;
  /** Tenancy/partition keys (e.g. `company_id`, `product_id`). */
  tenant: Record<string, string>;
  /** Worker-registered dependencies (DB client, cache store, …). */
  deps: unknown;
  /**
   * Subject id(s) this execution processes (#715 slice 1; Python
   * `AIInvocationContext.subject_ids`). The carrier that lets a cross-run cache
   * WRITE record which subjects produced an entry so a later per-subject
   * invalidation can target it.
   */
  subjectIds?: readonly string[];
}

/**
 * The runtime context fields a caller may supply to enrich the hook's
 * `ActivityContext` — e.g. a Temporal worker mapping the activity `Info`
 * (workflowId/runId/attempt/taskQueue/…), or the workflow-threaded `subjectIds`
 * (#715 slice 1). `activityName`/`tenant`/`deps` are owned by `executeActivity`
 * and are not overridable here.
 */
export type ActivityContextOverrides = Partial<
  Pick<
    ActivityContext,
    "namespace" | "workflowId" | "runId" | "activityId" | "attempt" | "taskQueue" | "subjectIds"
  >
>;

/** Opt-in cross-run cache policy (parity with the Python `CacheConfig`, #398). */
export interface CacheConfig {
  /** Defaults to `true` (Python parity); set `false` to explicitly opt out. */
  enabled?: boolean;
  /** Env var whose presence skips cache reads (writes still happen). */
  bypassReadsEnv?: string;
}

/**
 * Opt-in provider SESSION cache (#478; Python `SessionCacheSpec`, #60): a
 * prefix/reference cache prepared once over the activity's stable prefix and
 * reused across map/fan-out items. Distinct from the cross-run {@link CacheConfig}
 * memoization — this caches provider-side context, not validated outputs.
 */
export interface SessionCacheConfig {
  /** Defaults to `true` when the block is present (Python parity). */
  enabled?: boolean;
  /** Cache TTL, honored by reference-style providers only (Gemini); >= 1. */
  ttlSeconds?: number;
}

/**
 * Runs after validation with the typed input + output and the activity context.
 * A 2-arg `(input, output)` hook is structurally assignable, so the context is
 * always passed (no Python-style arity detection needed).
 *
 * The hook is POST-acceptance (#745): it runs after the output has already been
 * accepted (schema-parsed + any {@link ActivityOutputCheck} passed) and can
 * transform/observe it, but a hook throw fails the activity WITHOUT triggering a
 * repair retry. For a cross-field contract that should give the model its
 * `validationRetries` chances to self-correct, use {@link ActivityOutputCheck}.
 */
export type ActivityHook<Input, Output> = (
  input: Input,
  output: Output,
  ctx: ActivityContext,
) => Output | Promise<Output>;

/**
 * A single cross-field output-contract violation reported by an
 * {@link ActivityOutputCheck} (#745). `message` is shown to the model verbatim in
 * the repair prompt, so it should describe what is wrong legibly; `path` (dotted
 * into the output) is optional context. Author-written, never model output.
 */
export interface OutputCheckViolation {
  message: string;
  path?: (string | number)[];
}

/**
 * Input-aware, PRE-acceptance output validation (#745): a cross-field validator
 * that sees the PARSED input (defaults materialized) and the candidate output
 * BEFORE it is accepted. Return nothing / an empty array to accept; throw or
 * return a non-empty {@link OutputCheckViolation} list to REJECT.
 *
 * On the provider-backed (AI) path a rejection counts as a validation failure: it
 * consumes a repair attempt and feeds the SAME repair-message path as a
 * schema-parse miss, so the model gets its `validationRetries` chances to
 * self-correct (e.g. a hallucinated citation index, a non-verbatim quote — the
 * class of error zod cannot express). On the pure-code path there is no repair
 * loop, so a rejection is terminal ({@link ../execute.js#ActivityValidationError}).
 *
 * Contrast with {@link ActivityHook}: outputCheck is PRE-acceptance validation
 * (can trigger repair, cannot transform); hook is POST-acceptance (transforms/
 * observes, cannot trigger repair). An outputCheck-rejected output is never
 * cached, and a cache hit re-runs the check (a failing hit is treated as a miss
 * and regenerates), so tightening a check invalidates stale cached outputs.
 */
export type ActivityOutputCheck<Input, Output> = (
  input: Input,
  output: Output,
) => void | OutputCheckViolation[];

/**
 * A pure-code activity's deterministic handler (#746): the TS analogue of a Python
 * `activities.modules` function. It receives the validated input and returns the output
 * (parsed against the output schema by the executor). No prompt, no provider call, no
 * repair loop — a mismatch fails loud. An optional {@link ActivityHook} still runs
 * post-output exactly as for a provider-backed activity (the grounding seam keeps working).
 */
export type CodeActivityHandler<Input, Output> = (input: Input) => Output | Promise<Output>;

export interface DefineActivityOptions<In extends z.ZodType, Out extends z.ZodType> {
  /** Optional schema identity names + definition source for the execution
   * manifest (YAML definitions pass the ref name and a yaml source). */
  inputSchemaName?: string;
  outputSchemaName?: string;
  definitionSource?: import("./manifest.js").DefinitionSource;
  name: string;
  prompt: PromptRef;
  input: In;
  output: Out;
  hook?: ActivityHook<z.infer<In>, z.infer<Out>>;
  /**
   * Input-aware, pre-acceptance output validation (#745): sees the parsed input +
   * candidate output and can trigger a repair retry. See {@link ActivityOutputCheck}.
   */
  outputCheck?: ActivityOutputCheck<z.infer<In>, z.infer<Out>>;
  cache?: CacheConfig;
  sessionCache?: SessionCacheConfig;
  /**
   * Activity-level provider params (#495): the HIGHEST-precedence merge layer —
   * over the call's defaults and the resolved prompt's params (Python
   * `provider_default_params.merge(prompt, activity)` order).
   */
  providerParams?: Record<string, unknown>;
  validationRetries?: number;
  /** Optional moderation checkpoint over the validated (post-hook) output (#453). */
  moderation?: ModerationConfig<z.infer<Out>>;
  /**
   * Declared artifact groups (#481): where in the input each group's refs live and how it
   * attaches. Resolution happens per call via `executeActivity`'s `artifactResolver` (the worker
   * package's `artifactInputResolver(policy)`), or pre-resolved groups via `options.artifacts`.
   */
  artifacts?: readonly ArtifactInput[];
}

/** The fields both descriptor kinds share (#746) — schema identity, hooks, manifest metadata. */
export interface ActivityDescriptorCommon<In extends z.ZodType, Out extends z.ZodType> {
  name: string;
  /** Zod input schema (use `.parse()` and `z.infer` for typing). */
  input: In;
  /** Zod output schema. */
  output: Out;
  /** Raw draft-2020-12 JSON Schema of the input. */
  inputJsonSchema: JsonSchema;
  /** Raw draft-2020-12 JSON Schema of the output. */
  outputJsonSchema: JsonSchema;
  /** Provider-safe output schema for the provider's structured-output mode. */
  outputProviderSchema: JsonSchema;
  /** Content hashes (sha256 over canonical JSON) for manifest / cache identity. */
  inputSchemaHash: string;
  outputSchemaHash: string;
  /** Schema identity NAMES for the execution manifest (Python uses the model
   * class name; YAML definitions pass the schema ref's simple name). Zod
   * schemas carry no name, so code-defined activities default to the slot. */
  inputSchemaName?: string;
  outputSchemaName?: string;
  /** Where the definition came from (yaml/code) — recorded in the manifest. */
  definitionSource?: import("./manifest.js").DefinitionSource;
  hook?: ActivityHook<z.infer<In>, z.infer<Out>>;
  /**
   * Input-aware, pre-acceptance output validator (#745): both descriptor kinds
   * carry it. On the AI path a rejection feeds the repair loop; on the code path
   * it is terminal. See {@link ActivityOutputCheck}.
   */
  outputCheck?: ActivityOutputCheck<z.infer<In>, z.infer<Out>>;
  validationRetries: number;
}

/**
 * A provider-backed (AI) activity descriptor — what {@link defineActivity} returns. The
 * `kind` discriminant is optional-`"ai"` for back-compat (every pre-#746 descriptor has no
 * `kind`), so `prompt` stays a REQUIRED field: `defineActivity(...).prompt.name` compiles
 * without optional-chaining, and a hand-built AI-shaped literal cannot silently omit it.
 */
export interface AiActivityDescriptor<In extends z.ZodType, Out extends z.ZodType>
  extends ActivityDescriptorCommon<In, Out> {
  kind?: "ai";
  prompt: PromptRef;
  cache?: CacheConfig;
  sessionCache?: SessionCacheConfig;
  providerParams?: Record<string, unknown>;
  moderation?: ModerationConfig<z.infer<Out>>;
  artifacts?: readonly ArtifactInput[];
}

/**
 * A pure-code activity descriptor (#746) — what {@link defineCodeActivity} returns. `kind` is
 * the required `"code"` discriminant and `handler` is required; there is NO `prompt` field at
 * all (nothing to resolve). The provider-only options are typed `undefined` (not omitted) so
 * shared executor code can read them off the {@link ActivityDescriptor} union without
 * narrowing — they can never be set on a code descriptor.
 */
export interface CodeActivityDescriptor<In extends z.ZodType, Out extends z.ZodType>
  extends ActivityDescriptorCommon<In, Out> {
  kind: "code";
  handler: CodeActivityHandler<z.infer<In>, z.infer<Out>>;
  cache?: undefined;
  sessionCache?: undefined;
  providerParams?: undefined;
  moderation?: undefined;
  artifacts?: undefined;
}

/**
 * A typed activity descriptor (#746): the discriminated union of the provider-backed
 * {@link AiActivityDescriptor} and the pure-code {@link CodeActivityDescriptor}. Narrow with
 * `descriptor.kind === "code"` — the executor and projections branch on it.
 */
export type ActivityDescriptor<In extends z.ZodType, Out extends z.ZodType> =
  | AiActivityDescriptor<In, Out>
  | CodeActivityDescriptor<In, Out>;

/** Drop the `$schema` draft-URL keyword so a schema (and its content hash) does
 * not depend on the draft URL Zod emits — keeps hashes stable across Zod bumps. */
function stripSchemaKeyword(schema: JsonSchema): JsonSchema {
  const copy = { ...schema };
  delete copy["$schema"];
  return copy;
}

/**
 * The JSON Schema a typed-activity slot is serialized/hashed as — the single source of truth for
 * activity schema identity, shared with the control-plane catalog so its `json_schema`/`hash` equal
 * the descriptor's. INPUT slots convert with `{ io: "input" }` (a `.default()` field stays OPTIONAL
 * on the wire, since the caller may omit it); OUTPUT slots use Zod's default (output) mode. Both
 * strip `$schema` for hash stability across Zod bumps.
 */
export function activitySlotJsonSchema(schema: z.ZodType, io: "input" | "output"): JsonSchema {
  const raw = (io === "input" ? z.toJSONSchema(schema, { io: "input" }) : z.toJSONSchema(schema)) as JsonSchema;
  return stripSchemaKeyword(raw);
}

/**
 * Define a typed AI activity. Eagerly maps the schemas and throws
 * `ProviderSchemaError` if the output type cannot be made provider-safe (e.g.
 * an open record / a recursive type).
 *
 * Output parse-compatibility: `toProviderSafe` turns an optional output field
 * into a **required, nullable** one, so the provider may emit `null` for it.
 * Author such fields with `.nullish()` / `.nullable()` (not bare `.optional()`,
 * which is `T | undefined` and rejects `null`) so a provider's `null`
 * round-trips through `output.parse(...)`. (Python's `Optional[T]` is `T | None`,
 * so it accepts the `null` natively; Zod draws the distinction.)
 */
export function defineActivity<In extends z.ZodType, Out extends z.ZodType>(
  options: DefineActivityOptions<In, Out>,
): AiActivityDescriptor<In, Out> {
  // The single normalization path (shared with the control-plane catalog): inputs convert with
  // `io: "input"` (a `.default()` field stays optional on the wire), both strip `$schema`.
  const inputJsonSchema = activitySlotJsonSchema(options.input, "input");
  const outputJsonSchema = activitySlotJsonSchema(options.output, "output");

  const descriptor: AiActivityDescriptor<In, Out> = {
    name: options.name,
    prompt: options.prompt,
    input: options.input,
    output: options.output,
    ...(options.inputSchemaName !== undefined ? { inputSchemaName: options.inputSchemaName } : {}),
    ...(options.outputSchemaName !== undefined ? { outputSchemaName: options.outputSchemaName } : {}),
    ...(options.definitionSource !== undefined ? { definitionSource: options.definitionSource } : {}),
    inputJsonSchema,
    outputJsonSchema,
    // Throws ProviderSchemaError here if the output type isn't provider-safe.
    outputProviderSchema: toProviderSafe(outputJsonSchema),
    inputSchemaHash: schemaHash(inputJsonSchema),
    outputSchemaHash: schemaHash(outputJsonSchema),
    validationRetries: options.validationRetries ?? 1,
  };
  if (options.hook !== undefined) {
    descriptor.hook = options.hook;
  }
  if (options.outputCheck !== undefined) {
    descriptor.outputCheck = options.outputCheck;
  }
  if (options.cache !== undefined) {
    // Default `enabled` to true (Python CacheConfig parity) so a cache config
    // with only `bypassReadsEnv` is opt-in, not silently treated as disabled.
    descriptor.cache = { ...options.cache, enabled: options.cache.enabled ?? true };
  }
  if (options.sessionCache !== undefined) {
    const ttl = options.sessionCache.ttlSeconds;
    if (ttl !== undefined && (!Number.isInteger(ttl) || ttl < 1)) {
      // Python SessionCacheSpec: ttl_seconds must be an integer >= 1.
      throw new Error(`session cache ttlSeconds must be an integer >= 1, got ${ttl}`);
    }
    descriptor.sessionCache = { ...options.sessionCache, enabled: options.sessionCache.enabled ?? true };
  }
  if (options.moderation !== undefined) {
    descriptor.moderation = options.moderation;
  }
  if (options.providerParams !== undefined) {
    descriptor.providerParams = { ...options.providerParams };
  }
  if (options.artifacts !== undefined && options.artifacts.length > 0) {
    // Duplicate group names would be ambiguous: attachments and provider lookups address groups
    // by name and take the first match, so the later declaration silently becomes unreachable.
    const seen = new Set<string>();
    for (const artifactInput of options.artifacts) {
      if (seen.has(artifactInput.name)) {
        throw new Error(`duplicate artifact input name: ${JSON.stringify(artifactInput.name)}`);
      }
      seen.add(artifactInput.name);
    }
    descriptor.artifacts = Object.freeze([...options.artifacts]);
  }
  return descriptor;
}

/** Options for {@link defineCodeActivity} — the pure-code authoring primitive (#746). */
export interface DefineCodeActivityOptions<In extends z.ZodType, Out extends z.ZodType> {
  name: string;
  input: In;
  output: Out;
  /** The deterministic handler: validated input in, output (parsed against `output`) out. */
  handler: CodeActivityHandler<z.infer<In>, z.infer<Out>>;
  /** Optional post-output hook — runs identically to a provider-backed activity's hook. */
  hook?: ActivityHook<z.infer<In>, z.infer<Out>>;
  /**
   * Optional input-aware, pre-acceptance output validator (#745). No repair loop
   * exists for deterministic code, so a rejection is terminal
   * ({@link ../execute.js#ActivityValidationError}). See {@link ActivityOutputCheck}.
   */
  outputCheck?: ActivityOutputCheck<z.infer<In>, z.infer<Out>>;
  /** Schema identity names for the execution manifest (default to the slot names). */
  inputSchemaName?: string;
  outputSchemaName?: string;
  definitionSource?: import("./manifest.js").DefinitionSource;
}

/**
 * The provider-backed options a pure-code activity has no meaning for (#746): a code activity
 * makes no provider call, runs no repair loop, and does not memoize, so each is rejected LOUDLY
 * rather than silently ignored (a `defineCodeActivity({ cache })` caller has the wrong primitive).
 */
const CODE_ACTIVITY_REJECTED_OPTIONS = [
  "prompt",
  "providerParams",
  "validationRetries",
  "cache",
  "sessionCache",
  "moderation",
  "artifacts",
] as const;

/**
 * Define a pure-code (non-LLM) activity (#746): the TS analogue of a Python `activities.modules`
 * function, injectable via `assembleYamlRuntime`'s `extraActivities`. Its `handler` runs
 * deterministically with NO provider call and NO repair loop; the result is parsed against the
 * `output` schema (a mismatch fails the activity with {@link ActivityValidationError}). Schema
 * JSON-Schema/hash identity is computed exactly as {@link defineActivity}, so a code activity and
 * an AI activity with the same schemas share slot hashes. An optional `hook` runs post-output.
 *
 * Provider-only options (`prompt`/`cache`/`sessionCache`/`moderation`/`validationRetries`/…) are
 * rejected — a code activity has no provider to configure. Unlike {@link defineActivity}, the
 * output is NOT run through `toProviderSafe`: it is returned to the workflow, never sent to a
 * provider's structured-output mode, so open-record / recursive shapes are allowed.
 */
export function defineCodeActivity<In extends z.ZodType, Out extends z.ZodType>(
  options: DefineCodeActivityOptions<In, Out>,
): CodeActivityDescriptor<In, Out> {
  for (const key of CODE_ACTIVITY_REJECTED_OPTIONS) {
    if ((options as unknown as Record<string, unknown>)[key] !== undefined) {
      throw new Error(
        `defineCodeActivity(${JSON.stringify(options.name)}): option ${JSON.stringify(key)} is not ` +
          "supported for a pure-code activity (no prompt/provider call, no repair loop, no cache, " +
          "no moderation) — use defineActivity for a provider-backed activity.",
      );
    }
  }
  if (typeof options.handler !== "function") {
    throw new Error(`defineCodeActivity(${JSON.stringify(options.name)}): handler must be a function`);
  }
  const inputJsonSchema = activitySlotJsonSchema(options.input, "input");
  const outputJsonSchema = activitySlotJsonSchema(options.output, "output");
  const descriptor: CodeActivityDescriptor<In, Out> = {
    kind: "code",
    name: options.name,
    input: options.input,
    output: options.output,
    handler: options.handler,
    ...(options.inputSchemaName !== undefined ? { inputSchemaName: options.inputSchemaName } : {}),
    ...(options.outputSchemaName !== undefined ? { outputSchemaName: options.outputSchemaName } : {}),
    ...(options.definitionSource !== undefined ? { definitionSource: options.definitionSource } : {}),
    inputJsonSchema,
    outputJsonSchema,
    // No provider call: the output is returned to the workflow, never sent to a provider's
    // structured-output mode, so it is NOT run through `toProviderSafe` (a code activity may
    // legitimately return an open-record / recursive shape a provider could not). The field is
    // populated for shape-compat with the AI descriptor and is unused on the code executor path.
    outputProviderSchema: outputJsonSchema,
    inputSchemaHash: schemaHash(inputJsonSchema),
    outputSchemaHash: schemaHash(outputJsonSchema),
    // Deterministic code has no repair loop — a single output-parse mismatch is terminal.
    validationRetries: 0,
  };
  if (options.hook !== undefined) {
    descriptor.hook = options.hook;
  }
  if (options.outputCheck !== undefined) {
    descriptor.outputCheck = options.outputCheck;
  }
  return descriptor;
}
