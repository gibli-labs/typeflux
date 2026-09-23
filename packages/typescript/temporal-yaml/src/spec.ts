/**
 * Zod schema for a `typeflux.yaml` spec (parity Epic 5, #452; Python
 * `yaml/spec.py`). STRICT like Python (`extra="forbid"`, #490): a field validates only
 * if the runtime honors it — wired engine behavior, or caller-wiring metadata a caller
 * reads off the parsed spec to construct transports (`api_key`, `address`, `host`).
 * Unknown keys are rejected (a typo like `maximum_atempts` fails loudly instead of
 * silently getting the default), and known-pending Python config blocks are rejected
 * with pointer errors naming their tracking issue (see `unsupported`). Behavioral
 * parity, not byte-identical: validation is Zod, not Pydantic.
 */

import { z } from "zod";

// One source of truth for the reserved subject-key kid prefix (#715 fix round): the
// codec module OWNS the constant; this import is runtime-safe (payload-codec's own
// import of this module is type-only, so there is no runtime cycle).
import { RESERVED_KID_PREFIX } from "./payload-codec.js";

/**
 * A stub for a Python-valid config block the TS SDK does not honor yet (or by design).
 * Accepting such a block would be silent misbehavior — the user believes the feature is
 * on (a session cache, a rate limit, a lifecycle gate) while nothing enforces it — so
 * presence fails with an actionable message instead of a generic unrecognized-key error.
 */
const unsupported = (message: string) =>
  z
    .unknown()
    .optional()
    .refine((value) => value === undefined, { message });

/**
 * A boolean that ALSO accepts the string form env interpolation produces — the
 * documented `tls: ${TEMPORAL_TLS:-false}` pattern interpolates to the string
 * `"false"`. `z.stringbool()` parses "true"/"false"/"1"/"0"/… → boolean, matching
 * Pydantic's lax bool coercion in the Python loader.
 */
const yamlBoolean = z.union([z.boolean(), z.stringbool()]);

/** An integer that also accepts the string form env interpolation produces. */
const yamlPositiveInt = z.coerce.number().int().positive();

/** First value that occurs more than once in `values`, or `undefined` if all are unique. */
function findDuplicate(values: string[]): string | undefined {
  const seen = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) {
      return value;
    }
    seen.add(value);
  }
  return undefined;
}

/** A list entry that must be non-empty and trimmed (Python artifact runtime validation). */
const trimmedNonEmptyEntry = z
  .string()
  .refine((v) => v.length > 0 && v.trim() === v, { message: "artifact runtime entries must be non-empty and trimmed" });

/** An artifact input field that must be non-empty and trimmed at LOAD time (Python parity). */
const artifactField = z
  .string()
  .refine((v) => v.length > 0 && v.trim() === v, { message: "artifact input fields must be non-empty and trimmed" });

/** A secret reference: `value_from: { env | file, required? }` — exactly one source (Python parity). */
export const secretValueSpec = z
  .object({
    value_from: z
      .object({
        env: z.string().min(1).optional(),
        file: z.string().min(1).optional(),
        required: yamlBoolean.optional(),
      })
      .strict()
      .refine((v) => (v.env !== undefined) !== (v.file !== undefined), {
        message: "value_from requires exactly one of `env` or `file`",
      }),
  })
  .strict();

/** An api_key is either a literal string or a secret reference. */
const apiKey = z.union([z.string(), secretValueSpec]);

/** One AES-256-GCM payload-codec key: a stable id + the secret source for its 32-byte
 * value (#188; Python `PayloadCodecKeySpec`). `value_from` is the bare `{env|file}` source
 * (not the `secretValueSpec` wrapper), so its own slot joins `SECRET_SLOT_PATHS`. */
export const payloadCodecKeySpec = z
  .object({
    // A configured shared-key id may NOT start with the reserved subject-key prefix
    // (#715 slice 4) — that namespace is the subject-scoped kid space, kept disjoint so
    // decode routes unambiguously on the prefix (see payload-codec.ts
    // RESERVED_KID_PREFIX + binding.v1.json payload_codec.subject_key_scheme).
    id: z
      .string()
      .min(1)
      .refine((id) => !id.startsWith(RESERVED_KID_PREFIX), {
        message: `id may not start with the reserved subject-key prefix '${RESERVED_KID_PREFIX}' (#715 slice 4)`,
      }),
    value_from: z
      .object({
        env: z.string().min(1).optional(),
        file: z.string().min(1).optional(),
        required: yamlBoolean.optional(),
      })
      .strict()
      .refine((v) => (v.env !== undefined) !== (v.file !== undefined), {
        message: "value_from requires exactly one of `env` or `file`",
      }),
  })
  .strict();

/**
 * `runtime.temporal.payload_codec.subject_scope` — per-subject crypto-shred (#715
 * slice 4; Python `PayloadCodecSubjectScopeSpec` parity). Declaring the block turns ON
 * subject-scoped sealing (executions with subject ids seal under per-subject keystore
 * records; no-subject executions keep the shared-key wire byte-for-byte). `keystore`
 * names the reference backend — `in_memory` is PROCESS-LOCAL, built by DEFAULT only on
 * the sole-owner path (`buildRuntime`, where one process holds the worker and the
 * start-path converter); every other path (the worker entrypoint, the control plane)
 * FAILS CLOSED unless a shared `SubjectKeystore` is injected
 * (`BuildRuntimeOptions.subjectKeystore` / worker bindings `subjectKeystore`). The
 * default is MATERIALIZED (Python-default parity rule).
 */
export const payloadCodecSubjectScopeSpec = z
  .object({
    keystore: z.literal("in_memory").default("in_memory"),
  })
  .strict();

/**
 * `runtime.temporal.payload_codec` — a keyed-map AES-256-GCM codec (#188; rotation-first,
 * Python `PayloadCodecSpec` parity). Encrypt with `current`; decrypt by the payload's key
 * id. Absent block ⇒ OFF (plaintext). The crypto lives in `payload-codec.ts`; the
 * optional `subject_scope` block layers per-subject crypto-shred on top (#715 slice 4).
 */
export const payloadCodecSpec = z
  .object({
    type: z.literal("aes"),
    current: z.string().min(1),
    keys: z.array(payloadCodecKeySpec),
    subject_scope: payloadCodecSubjectScopeSpec.optional(),
  })
  .strict()
  .superRefine((spec, ctx) => {
    if (spec.keys.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "runtime.temporal.payload_codec of type aes requires at least one key",
      });
      return;
    }
    const ids = spec.keys.map((key) => key.id);
    if (findDuplicate(ids) !== undefined) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "runtime.temporal.payload_codec.keys ids must be unique" });
    }
    if (!ids.includes(spec.current)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "runtime.temporal.payload_codec.current must name a declared key id",
      });
    }
  });

/** A registry prompt reference: a name plus a mutually-exclusive version OR label. */
export const promptRefSpec = z
  .object({
    name: z.string(),
    // Coerced like the other numeric fields so a `version: ${PROMPT_VERSION:-3}` env
    // default (a string after interpolation) is accepted, matching Python.
    version: z.coerce.number().int().optional(),
    label: z.string().optional(),
    type: z.enum(["auto", "text", "chat"]).optional(),
  })
  .strict()
  .refine((p) => !(p.version !== undefined && p.label !== undefined), {
    message: "prompt.version and prompt.label are mutually exclusive",
  });

/**
 * One content part of an inline prompt message — the spec form of the core `ContentPart`
 * union (text / artifact / artifact_group / provider_extension). A `provider_extension`
 * `payload` is an open record by design (a provider-native part passed verbatim, e.g.
 * Gemini `inlineData`); everything else is strict so a malformed part fails loudly here
 * rather than silently entering the message hash.
 */
export const inlineContentPartSpec = z.discriminatedUnion("type", [
  z.object({ type: z.literal("text"), text: z.string() }).strict(),
  z.object({ type: z.literal("artifact"), artifact: z.string(), text: z.string().optional() }).strict(),
  z.object({ type: z.literal("artifact_group"), group: z.string(), text: z.string().optional() }).strict(),
  z
    .object({
      type: z.literal("provider_extension"),
      provider: z.string(),
      payload: z.record(z.string(), z.unknown()),
    })
    .strict(),
]);

/** One message of an inline prompt: string content, or a list of typed content parts. */
export const inlinePromptMessageSpec = z
  .object({
    role: z.enum(["system", "user", "assistant"]),
    content: z.union([z.string(), z.array(inlineContentPartSpec)]),
    name: z.string().optional(),
  })
  .strict();

/**
 * `null` and `""` (an interpolated `${VAR:-}` empty default) mean ABSENT, matching
 * Python's None — `z.coerce.number()` would otherwise turn both into 0 and silently
 * SELECT the value (temperature 0, seed 0, …; the codebase's max_bytes null→0 trap).
 */
const absentAware = <T extends z.ZodType>(schema: T): z.ZodType<z.infer<T> | undefined> =>
  z.preprocess((value) => (value === null || value === "" ? undefined : value), schema.optional());

/**
 * Provider behavior params (#495; Python `ProviderParamsSpec` field-for-field, incl.
 * bounds). `stop` accepts a bare string or a list; entries must be trimmed non-empty.
 * `seed` is an unbounded integer (Python bounds only max_tokens/top_k).
 */
/** Provider-params string fields carry their OWN messages (Python yaml/spec.py parity). */
const providerParamsModel = z
  .string()
  .refine((v) => v.length > 0 && v.trim() === v, { message: "provider params model must be non-empty and trimmed" });
const providerParamsStopEntry = z
  .string()
  .refine((v) => v.length > 0 && v.trim() === v, { message: "provider params stop entries must be non-empty and trimmed" });

export const providerParamsSpec = z
  .object({
    model: absentAware(providerParamsModel),
    temperature: absentAware(z.coerce.number().min(0, "provider params temperature must be between 0 and 2").max(2)),
    max_tokens: absentAware(z.coerce.number().int().min(1, "provider params integer values must be >= 1")),
    top_p: absentAware(z.coerce.number().min(0, "provider params top_p must be between 0 and 1").max(1)),
    top_k: absentAware(z.coerce.number().int().min(1, "provider params integer values must be >= 1")),
    stop: absentAware(z.union([providerParamsStopEntry, z.array(providerParamsStopEntry)])),
    seed: absentAware(z.coerce.number().int()),
    timeout: absentAware(z.coerce.number().positive("provider params timeout must be > 0")),
    frequency_penalty: absentAware(z.coerce.number().min(-2, "provider params penalties must be between -2 and 2").max(2)),
    presence_penalty: absentAware(z.coerce.number().min(-2, "provider params penalties must be between -2 and 2").max(2)),
    thinking_budget: absentAware(z.coerce.number().int().min(0, "provider params thinking_budget must be >= 0")),
  })
  .strict();

/**
 * Per-provider supported behavior params (#495; Python `supported_provider_params` on
 * each provider, enforced by `validate_provider_params_supported`): a configured param
 * the selected provider never maps would be silently ignored — rejected at LOAD instead.
 * Unknown provider types (`custom`/`fake`) skip the check (their provider is injected).
 */
const SUPPORTED_PROVIDER_PARAMS: Record<string, ReadonlySet<string>> = {
  openai: new Set(["model", "temperature", "max_tokens", "top_p", "stop", "seed", "timeout", "frequency_penalty", "presence_penalty"]),
  anthropic: new Set(["model", "temperature", "max_tokens", "top_p", "top_k", "stop", "timeout"]),
  gemini: new Set(["model", "temperature", "max_tokens", "top_p", "top_k", "stop", "timeout", "thinking_budget"]),
};

/** The keys of a params block the given provider type does not support (empty = fine). */
export function unsupportedProviderParamKeys(
  providerType: string,
  params: ProviderParamsSpec | undefined,
): string[] {
  const supported = SUPPORTED_PROVIDER_PARAMS[providerType];
  if (supported === undefined || params === undefined) {
    return [];
  }
  return Object.entries(params)
    .filter(([key, value]) => value !== undefined && !supported.has(key))
    .map(([key]) => key)
    .sort();
}
export type ProviderParamsSpec = z.infer<typeof providerParamsSpec>;

/** A spec params block as the runtime record: `stop` normalized to a string array. */
export function providerParamsRecord(params: ProviderParamsSpec): Record<string, unknown> {
  const { stop, ...rest } = params;
  const record: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(rest)) {
    if (value !== undefined) {
      record[key] = value;
    }
  }
  if (stop !== undefined) {
    const stops = typeof stop === "string" ? [stop] : [...stop];
    // Python `ProviderParams.to_dict()` drops an EMPTY stop list (`key != "stop" or value` —
    // truthiness): `stop: []` must not serialize where Python omits it.
    if (stops.length > 0) {
      record["stop"] = stops;
    }
  }
  return record;
}

/**
 * An inline prompt defined in the spec (vs fetched from a registry). `model` and `temperature`
 * flow into the core `ResolvedPrompt` (`model` is consumed by `resolveCall`; the `temperature`
 * provider wiring is audited under #495).
 */
export const inlinePromptSpec = z
  .object({
    messages: z.array(inlinePromptMessageSpec).min(1, "inline prompt messages must not be empty"),
    model: z.string().optional(),
    temperature: z.number().optional(),
    /** Prompt-level behavior params (#495) — merged UNDER the prompt's dedicated fields. */
    provider_params: providerParamsSpec.optional(),
  })
  .strict()
  .superRefine((prompt, ctx) => {
    // Python InlinePromptSpec compatibility validator: the dedicated model/temperature
    // fields and the params record must MATCH when both set.
    if (
      prompt.model !== undefined &&
      prompt.provider_params?.model !== undefined &&
      prompt.model !== prompt.provider_params.model
    ) {
      ctx.addIssue({
        code: "custom",
        message: "legacy model and provider_params.model must match",
        path: ["provider_params", "model"],
      });
    }
    if (
      prompt.temperature !== undefined &&
      prompt.provider_params?.temperature !== undefined &&
      prompt.temperature !== prompt.provider_params.temperature
    ) {
      ctx.addIssue({
        code: "custom",
        message: "legacy temperature and provider_params.temperature must match",
        path: ["provider_params", "temperature"],
      });
    }
  });

/** Blank-string-to-absent for the TLS block's plain string fields — an interpolated
 * `${VAR:-}` empty default means UNSET (Python `_normalize_blank_strings`). */
const blankAsAbsent = z.preprocess((value) => (value === "" ? undefined : value), z.string().optional());

/**
 * Structured TLS (custom CA / client mTLS certs; #685) — Python `TemporalTLSConfigSpec`
 * field-for-field. Each cert slot is EITHER an inline secret reference (`value_from`)
 * OR a `*_file` path, never both; a client cert and its private key travel together.
 * `temporalTlsOptions` (temporal-tls.ts) maps a validated block to the
 * `@temporalio/*` connection TLS options.
 */
export const temporalTlsSpec = z
  .object({
    server_root_ca_cert_file: blankAsAbsent,
    server_root_ca_cert: secretValueSpec.optional(),
    domain: blankAsAbsent,
    client_cert_file: blankAsAbsent,
    client_cert: secretValueSpec.optional(),
    client_private_key_file: blankAsAbsent,
    client_private_key: secretValueSpec.optional(),
  })
  .strict()
  .superRefine((tls, ctx) => {
    const exclusive: ReadonlyArray<readonly ["server_root_ca_cert" | "client_cert" | "client_private_key", unknown, unknown]> = [
      ["server_root_ca_cert", tls.server_root_ca_cert_file, tls.server_root_ca_cert],
      ["client_cert", tls.client_cert_file, tls.client_cert],
      ["client_private_key", tls.client_private_key_file, tls.client_private_key],
    ];
    for (const [slot, file, inline] of exclusive) {
      if (file !== undefined && inline !== undefined) {
        ctx.addIssue({
          code: "custom",
          message: `runtime.temporal.tls.${slot}_file and runtime.temporal.tls.${slot} cannot both be configured`,
          path: [slot],
        });
      }
    }
    const hasClientCert = tls.client_cert_file !== undefined || tls.client_cert !== undefined;
    const hasClientKey = tls.client_private_key_file !== undefined || tls.client_private_key !== undefined;
    if (hasClientCert !== hasClientKey) {
      ctx.addIssue({
        code: "custom",
        message:
          "runtime.temporal.tls.client_cert/client_cert_file and " +
          "runtime.temporal.tls.client_private_key/client_private_key_file must be configured together",
        path: ["client_cert"],
      });
    }
  });

/**
 * Temporal connection config — caller-wiring metadata (the caller constructs the
 * `NativeConnection` and reads these off the parsed spec). `tls` is a boolean or the
 * structured block (#685); the engine-wired `workflow_search_attribute` is #495.
 */
export const temporalSpec = z
  .object({
    address: z.string().optional(),
    namespace: z.string().optional(),
    tls: z.union([yamlBoolean, temporalTlsSpec]).optional(),
    api_key: apiKey.optional(),
    /**
     * AES-256-GCM Temporal payload codec (#188). Absent = OFF (plaintext). When declared,
     * every referenced key MUST resolve at worker/client build time or startup fails.
     */
    payload_codec: payloadCodecSpec.optional(),
    /**
     * A KEYWORD search attribute carrying the workflow's logical name on every
     * start (#495) — one visibility query across versioned workflow types. Must be
     * REGISTERED in the Temporal namespace before use (as in Python).
     */
    workflow_search_attribute: absentAware(
      z.string().regex(/^[A-Za-z][A-Za-z0-9_]*$/, {
        message:
          "runtime.temporal.workflow_search_attribute must start with a letter and contain only letters, digits, and underscores",
      }),
    ),
  })
  .strict();

export const registrySpec = z
  .object({
    type: z.string(),
    label: z.string().optional(),
    host: z.string().optional(),
    prompts: z.record(z.string(), z.union([z.string(), inlinePromptSpec])).optional(),
    class: unsupported(
      "registry `class` (a Python importlib path) is not supported by the TS SDK; inject a `registryTransport` instead",
    ),
    // #792: config accompanies a custom `class`, which the TS SDK rejects above — an
    // injected transport takes its configuration as ordinary constructor arguments.
    config: unsupported(
      "registry `config` accompanies a Python custom `class`, which is not supported by the TS SDK; configure your injected `registryTransport` in code instead",
    ),
  })
  .strict();

export const providerSpec = z
  .object({
    type: z.string(),
    model: z.string().optional(),
    // The only Python-legal value, and what the TS providers already do — a typo'd mode now
    // fails here instead of being silently ignored.
    structured_mode: z.literal("json_schema").optional(),
    api_key: apiKey.optional(),
    class: unsupported(
      "provider `class` (a Python importlib path) is not supported by the TS SDK; pass a `provider` or `transports` to the runtime instead",
    ),
    // #792: config accompanies a custom `class`, which the TS SDK rejects above — an
    // injected provider/transport takes its configuration as ordinary constructor arguments.
    config: unsupported(
      "provider `config` accompanies a Python custom `class`, which is not supported by the TS SDK; configure your injected provider or transport in code instead",
    ),
    /** Provider-wide default behavior params (#495) — the LOWEST merge layer. */
    params: providerParamsSpec.optional(),
    // PERMANENT divergences (not #495 gaps): the TS SDK never constructs a vendor client —
    // transports are injected (thin-transport philosophy, #499) — so endpoint and Vertex
    // configuration belong to the ADAPTER that builds the client, not the spec.
    vertex: unsupported(
      "runtime.provider.vertex is not supported by the TS SDK: clients are injected transports (#499), so Vertex selection (genai.Client({vertexai: true, ...})) belongs to the adapter that constructs the client",
    ),
    base_url: unsupported(
      "runtime.provider.base_url is not supported by the TS SDK: clients are injected transports (#499), so the endpoint belongs to the adapter that constructs the client",
    ),
    /**
     * When false (Python default), a BACKEND registry prompt's model is stripped at
     * resolution — the spec stays authoritative for the execution model (#495).
     */
    allow_prompt_model_override: yamlBoolean.default(false),
  })
  .strict()
  .superRefine((provider, ctx) => {
    // Python ProviderSpec: a legacy top-level model and params.model must MATCH when
    // both set (then they are one value); a mismatch is a config error, not a merge.
    if (
      provider.model !== undefined &&
      provider.params?.model !== undefined &&
      provider.model !== provider.params.model
    ) {
      ctx.addIssue({
        code: "custom",
        message: "runtime.provider.model and runtime.provider.params.model must match",
        path: ["params", "model"],
      });
    }
  });

/**
 * A custom redaction rule (#188 D188-4; Python `RedactionRuleSpec`). `pattern` is compiled
 * with the JS `RegExp` engine at load — an invalid pattern fails validation fail-closed (a
 * redaction control must never silently no-op). Patterns are the JS `RegExp` dialect, which
 * differs from Python's `re` on the other edition; a portable rule sticks to shared features
 * (see docs/typescript/privacy.md).
 */
export const redactionRuleSpec = z
  .object({
    // Name must be non-empty AND trimmed (Python `RedactionRuleSpec._validate_name`):
    // policy-side require_custom_rules matches names by exact trim-enforced membership, so
    // an untrimmed "case_reference " would load but then fail admission with a misleading
    // missing-rule error. pattern/replacement are min(1) only — trimming is not enforced
    // there (leading/trailing spaces can be legitimate in a replacement).
    name: z
      .string()
      .refine((v) => v.length > 0 && v.trim() === v, {
        message: "observability.redaction.custom_rules name must be non-empty and trimmed",
      }),
    pattern: z.string().min(1),
    // Non-empty after trim (Python `_validate_replacement`'s `value.strip()` check): a
    // whitespace-only replacement is rejected in BOTH editions so the same portable YAML
    // loads (or fails) identically, while " [X] " (inner padding) stays legal.
    replacement: z.string().refine((v) => v.trim().length > 0, {
      message: "observability.redaction.custom_rules replacement must be non-empty",
    }),
  })
  .strict()
  .superRefine((rule, ctx) => {
    try {
      new RegExp(rule.pattern);
    } catch (error) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["pattern"],
        message: `observability.redaction.custom_rules pattern is not a valid regex: ${
          error instanceof Error ? error.message : String(error)
        }`,
      });
    }
  });

/** PII redaction config (Python `RedactionSpec`). */
export const redactionSpec = z
  .object({
    enabled: yamlBoolean.optional(),
    emails: yamlBoolean.optional(),
    phones: yamlBoolean.optional(),
    ssn: yamlBoolean.optional(),
    credit_cards: yamlBoolean.optional(),
    preserve_typeflux_metadata: yamlBoolean.optional(),
    exclude_paths: z.array(z.string()).optional(),
    custom_rules: z.array(redactionRuleSpec).optional(),
  })
  .strict()
  .superRefine((redaction, ctx) => {
    const names = (redaction.custom_rules ?? []).map((rule) => rule.name);
    if (findDuplicate(names) !== undefined) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["custom_rules"],
        message: "observability.redaction.custom_rules names must be unique",
      });
    }
  });

/** Spec-declared backend credentials (#793): the same `string | value_from` contract as
 * every sibling secret slot; unset fields fall back to the standard env vars, so the block
 * is additive — what it buys is inventory honesty and admission-checkable references. */
const observabilityLangfuseSpec = z
  .object({
    public_key: apiKey.optional(),
    secret_key: apiKey.optional(),
    // Plain string (env interpolation covers the env-var pattern): a host is not a
    // credential, so it stays out of the secret inventory/scaffolding contract.
    host: z.string().optional(),
  })
  .strict();

const observabilityLangsmithSpec = z
  .object({
    api_key: apiKey.optional(),
    // Plain strings: not credentials (see host note above).
    endpoint: z.string().optional(),
    project: z.string().optional(),
  })
  .strict();

export const observabilitySpec = z
  .object({
    type: z.string().optional(),
    langfuse: observabilityLangfuseSpec.optional(),
    langsmith: observabilityLangsmithSpec.optional(),
    execution_manifest: yamlBoolean.optional(),
    redaction: redactionSpec.optional(),
    class: unsupported(
      "observability `class` (a Python importlib path) is not supported by the TS SDK; build the observer via `observerFromSpec(spec, transport)` instead",
    ),
    // #792: config accompanies a custom `class`, which the TS SDK rejects above — an
    // injected observer transport takes its configuration as ordinary constructor arguments.
    config: unsupported(
      "observability `config` accompanies a Python custom `class`, which is not supported by the TS SDK; configure your injected observer transport in code instead",
    ),
  })
  .strict()
  .superRefine((value, ctx) => {
    // Mirrors the Python edition: a credentials block on a different backend would be
    // silently dead configuration (#793).
    if (value.langfuse !== undefined && value.type !== "langfuse") {
      ctx.addIssue({
        code: "custom",
        path: ["langfuse"],
        message: "runtime.observability.langfuse credentials are only valid with type: langfuse",
      });
    }
    if (value.langsmith !== undefined && value.type !== "langsmith") {
      ctx.addIssue({
        code: "custom",
        path: ["langsmith"],
        message: "runtime.observability.langsmith credentials are only valid with type: langsmith",
      });
    }
  });

/**
 * Bounded activity retry policy (Python `ActivityRetrySpec`). Generated activity calls carry an
 * explicit bounded policy because Temporal's own default is unlimited attempts — a deterministic
 * provider failure would otherwise retry (and spend) without end. `maximum_attempts: 0` is
 * Temporal's explicit unlimited sentinel; `maximum_interval_seconds: null` means no backoff cap.
 */
export const activityRetrySpec = z
  .object({
    maximum_attempts: z.coerce.number().int().min(0).default(5),
    initial_interval_seconds: z.coerce.number().positive().default(1),
    maximum_interval_seconds: z.coerce.number().positive().nullable().default(60),
    backoff_coefficient: z.coerce.number().min(1).default(2),
  })
  .strict();

/**
 * Compensation for a completed step (#299 D299-1): run a normally-declared activity to
 * undo the step's side effect. `activity` is an ordinary catalog-validated activity name
 * (an undeclared name is a load error); `input_from` is a context dot-path (DEFAULT = the
 * compensated step's OWN output — for a map step, each completed item's own result); `retry`
 * overrides the default bounded activity retry for this compensation only. Available on
 * activity / map / subworkflow steps and steps inside parallel branches (the shared step
 * schema). `compensate.workflow` (a compensating child workflow) is deferred to v2.
 */
export const workflowCompensateSpec = z
  .object({
    activity: z.string(),
    input_from: z.string().optional(),
    retry: activityRetrySpec.optional(),
  })
  .strict();
export type WorkflowCompensateSpec = z.infer<typeof workflowCompensateSpec>;

/** How an artifact group attaches to the conversation (Python `ArtifactAttachmentSpec`). */
export const artifactAttachmentSpec = z
  .object({
    role: z.enum(["system", "user", "assistant"]).default("user"),
    text: z.string().optional(),
  })
  .strict();

/**
 * A declared artifact group on an activity definition (Python `ArtifactInputSpec`). The YAML key
 * for the input dot-path is `from` (aliased to the contract's `from_path`); `cache: reference`
 * maps to the contract's `cache_role` and requires `attach` (enforced by the contract factory).
 */
export const artifactInputSpec = z
  .object({
    // Load-time trimming (Python parity): an untrimmed `from` would otherwise pass the contract
    // factory (which only checks the `input.` prefix) and fail every execution with a misleading
    // "resolved to null" — or silently resolve empty when `required: false`.
    name: artifactField,
    from: artifactField,
    required: yamlBoolean.default(true),
    kind: z
      .enum(["document", "image", "audio", "video", "data", "archive", "provider_file", "external_uri", "other"])
      .optional(),
    media_types: z.array(artifactField).default([]),
    // `null` is Python's explicit "no limit" — nullable BEFORE coercion, else z.coerce turns
    // null into a hard 0-byte cap (the codebase's maximum_interval_seconds precedent).
    max_count: z.coerce.number().int().min(1).nullable().optional(),
    max_bytes: z.coerce.number().int().min(0).nullable().optional(),
    attach: artifactAttachmentSpec.optional(),
    /**
     * `reference` marks the group as part of a session-cached stable prefix (#478):
     * reference-style providers cache the document once at prep and per-item calls skip
     * re-sending it. Maps to the contract's `cache_role`, which requires `attach`.
     */
    cache: z.literal("reference").optional(),
  })
  .strict();

/**
 * In-activity provider retry (#495; Python `ProviderRetrySpec`): a bounded retry of
 * the PROVIDER CALL inside one activity attempt, mapped onto the core's
 * `transientRetries` + `transientBackoff` (exponential with jitter). The two
 * class-selection booleans map to the executor's `retryRateLimits` /
 * `retryTransientErrors` (#529): a rate-limit error (`ProviderRateLimitError`)
 * is selected by `retry_rate_limits` alone, every other transient by
 * `retry_transient_errors`.
 */
export const providerRetrySpec = z
  .object({
    max_attempts: absentAware(z.coerce.number().int().min(1, "provider retry max_attempts must be >= 1")),
    initial_backoff_seconds: absentAware(z.coerce.number().min(0, "provider retry initial_backoff_seconds must be >= 0")),
    // `""` (an interpolated `${MAX_BACKOFF:-}` empty default) is ABSENT — z.coerce would
    // turn it into a 0 cap that silently disables backoff. `null` stays the explicit
    // "no cap" and an explicit 0 still means an immediate-retry cap.
    max_backoff_seconds: z.preprocess(
      (value) => (value === "" ? undefined : value),
      z.coerce.number().min(0, "provider retry max_backoff_seconds must be >= 0").nullable().optional(),
    ),
    backoff_multiplier: absentAware(z.coerce.number().min(1, "provider retry backoff_multiplier must be >= 1")),
    jitter_ratio: absentAware(
      z.coerce.number().min(0, "provider retry jitter_ratio must be between 0 and 1").max(1),
    ),
    retry_rate_limits: yamlBoolean.default(true),
    retry_transient_errors: yamlBoolean.default(true),
  })
  .strict();
export type ProviderRetrySpec = z.infer<typeof providerRetrySpec>;

/**
 * One rate-limit tier (#529; Python `ProviderLimitSpec`): `max_concurrent`
 * (in-worker semaphore) + `min_interval_seconds` (call spacing). Wired into the
 * core's `ProviderRateLimitController`; the most specific tier wins
 * (model > provider > default).
 */
const providerLimitSpec = z
  .object({
    max_concurrent: absentAware(z.coerce.number().int().min(1, "provider limits max_concurrent must be >= 1")),
    min_interval_seconds: absentAware(
      z.coerce.number().min(0, "provider limits min_interval_seconds must be >= 0"),
    ),
  })
  .strict();

const providerLimitProviderSpec = providerLimitSpec.extend({
  models: z.record(z.string(), providerLimitSpec).default({}),
});

/** Python `ProviderLimitsSpec`: a `default` tier + per-provider (+ per-model) overrides. */
export const providerLimitsSpec = z
  .object({
    default: providerLimitSpec.optional(),
    providers: z.record(z.string(), providerLimitProviderSpec).default({}),
  })
  .strict();
export type ProviderLimitsSpec = z.infer<typeof providerLimitsSpec>;

/** The runtime's artifact admission policy (Python `ArtifactRuntimeSpec`). */
export const artifactRuntimeSpec = z
  .object({
    local_roots: z.array(trimmedNonEmptyEntry).default([]),
    allowed_sources: z.array(z.enum(["local_path", "url", "object_uri", "provider_file"])).default(["local_path"]),
    allowed_media_types: z.array(trimmedNonEmptyEntry).default([]),
    max_bytes: z.coerce.number().int().min(0).nullable().optional(),
  })
  .strict();

export const runtimeSpec = z
  .object({
    temporal: temporalSpec,
    registry: registrySpec,
    provider: providerSpec,
    observability: observabilitySpec.optional(),
    /** The declared cache-erasure REQUIREMENT (#795): `targeted` demands per-subject
     * invalidation — runtime assembly fails closed when a wired `CacheStore` lacks
     * `eraseSubject`, and the erase CLI's cache surface refuses the full-flush fallback.
     * Absent ≡ `any` (wired-store dependent). */
    cache_erasure: z.enum(["targeted", "any"]).optional(),
    /** Workflow-wide activity retry policy; a definition's own `retry` overrides it. */
    activity_retry: activityRetrySpec.optional(),
    /** Per-provider/per-model call admission (#529; Python `ProviderLimitsSpec`) — see {@link providerLimitsSpec}. */
    provider_limits: providerLimitsSpec.optional(),
    /** In-activity provider retry (#495; Python `ProviderRetrySpec`) — see {@link providerRetrySpec}. */
    provider_retry: providerRetrySpec.optional(),
    // PERMANENT divergence (#496): this block polices Python's importlib module loading —
    // the TS SDK loads no modules (activities are injected), so there is nothing to police.
    imports: unsupported(
      "runtime.imports (Python importlib policy) is not supported by the TS SDK — activities are injected, so there is no module loading to police (#496)",
    ),
    artifacts: artifactRuntimeSpec.optional(),
  })
  .strict();

/**
 * Output-moderation config for an activity (Python `ModerationSpec`). The TS moderator is
 * always an injected function (`ActivityResolver.moderators`), so `provider`/`moderator` select
 * intent (and admission policy) but resolve to the same injected function; Python's `model`
 * field is not modeled (no built-in provider moderator in the TS core yet). Python ships built-in
 * `openai` and (since #382) `gemini` provider moderators; the TS core stays injected-only, but a
 * shared YAML declaring either provider loads in both editions (`provider` is intent here).
 */
export const moderationSpec = z
  .object({
    provider: z.enum(["openai", "gemini"]).optional(),
    moderator: z.string().optional(),
    on_violation: z.enum(["block", "flag"]).default("block"),
    model: unsupported(
      "moderation `model` is not supported by the TS SDK — moderators are injected functions, so model selection belongs to the injected moderator",
    ),
  })
  .strict()
  .refine((m) => (m.provider !== undefined) !== (m.moderator !== undefined), {
    message: "moderation requires exactly one of `provider` or `moderator`",
  });


/** Provider session cache for an activity (Python `SessionCacheSpec`, #60/#478). */
export const sessionCacheSpec = z
  .object({
    enabled: yamlBoolean.default(true),
    /** Honored by reference-style providers only (Gemini); provider-managed otherwise. */
    ttl_seconds: z.coerce.number().int().min(1).optional(),
  })
  .strict();

/**
 * Cross-run activity output cache (Python `CacheConfig`, #398/#753) — the `cross_run_cache:`
 * block. DISTINCT from the session `cache:` block above: `cache:` is the provider-side PREFIX
 * cache prepared once per map fan-out and reused across ITEMS (#478); `cross_run_cache:`
 * memoizes the VALIDATED output across RUNS in a worker-registered `CacheStore`. The two live
 * on different axes and are INDEPENDENT — a definition may declare both, one, or neither. The
 * memoization only ENGAGES when a `cacheStore` is threaded into the runtime (assembleYamlRuntime/
 * buildRuntime `cacheStore`); declaring it with no store wired is an inert no-op, never an error.
 *
 * Fields mirror the Python `CacheConfig` vocabulary (`enabled`, `bypass_reads_env`) for
 * cross-edition config-shape parity. Python's YAML does not (yet) surface cross-run caching —
 * its `cache:` key maps to the SESSION cache too — so this TS key has no Python-YAML counterpart
 * to match; Python-YAML parity is a follow-up (Python side is code-only via `AIActivity.cache`).
 */
export const crossRunCacheSpec = z
  .object({
    /** Defaults to `true` (Python parity); set `false` to explicitly opt out while keeping the block. */
    enabled: yamlBoolean.default(true),
    /**
     * Names an environment variable whose PRESENCE skips cache READS (writes still happen), to
     * force regeneration — mirrors Python `CacheConfig.bypass_reads_env` and the regenerate-flag
     * pattern in adopter caches. Wired onto the descriptor's `cache.bypassReadsEnv`.
     */
    bypass_reads_env: z.string().min(1).optional(),
  })
  .strict();

/**
 * An inline activity definition. `defineActivitiesFromSpec` consumes name/input/output/prompt/
 * validation_retries/moderation; the timeout fields feed the workflow proxy (#479/#484) and
 * `retry` the bounded retry policy (#486).
 */
export const activityDefinitionSpec = z
  .object({
    name: z.string(),
    input: z.string(),
    output: z.string(),
    prompt: z.union([z.string(), promptRefSpec]),
    validation_retries: z.coerce.number().int().min(0).default(1),
    start_to_close_timeout_seconds: z.coerce.number().positive().optional(),
    heartbeat_timeout_seconds: z.coerce.number().positive().optional(),
    moderation: moderationSpec.optional(),
    /** Per-activity retry policy; overrides `runtime.activity_retry` and the bounded default. */
    retry: activityRetrySpec.optional(),
    /** Provider session cache (#478): a prefix/reference cache prepared once per map fan-out. */
    cache: sessionCacheSpec.optional(),
    /**
     * Cross-run output memoization (#398/#753) — DISTINCT from the session `cache:` block above.
     * `cache:` is the provider PREFIX cache reused across map ITEMS; `cross_run_cache:` memoizes
     * the VALIDATED output across RUNS in a worker-registered `CacheStore`, reached only when a
     * `cacheStore` is threaded into the runtime. The two are independent and may coexist. Wired
     * onto the descriptor's `cache` field (the core cross-run {@link CacheConfig}, Python parity).
     */
    cross_run_cache: crossRunCacheSpec.optional(),
    artifacts: z.array(artifactInputSpec).optional(),
    /** Activity-level behavior params (#495) — the HIGHEST merge layer. */
    provider_params: providerParamsSpec.optional(),
    /**
     * The author's declaration that this activity performs an EXTERNAL side effect (a
     * downstream write, a notification, a charge) (#299 D299-5). Same trust model as
     * `cache: reference`: an author assertion the runtime does not verify, consumed by
     * governance — a `risk_tiers.require_compensation` tier demands every side-effecting
     * activity STEP declare `compensate:`. Pure GOVERNANCE metadata: never a digest input
     * (definitions are not digest-fed; steps carry only the activity name + present-only
     * `compensate`), so marking an activity side-effecting leaves every registered workflow
     * type byte-identical — exactly like `moderation`.
     */
    side_effecting: yamlBoolean.default(false),
  })
  .strict()
  .superRefine((def, ctx) => {
    // A session-cached activity must not attach a NON-reference artifact with role
    // `system` (#478 PR5): on a reference-style cache hit the provider sends
    // `cached_content` and Gemini REJECTS `system_instruction` alongside it, so that
    // per-item system content would be silently dropped. Reference inputs are fine —
    // they join the cached prefix at prep. Rejected at load, not mid-fan-out.
    if (def.cache === undefined || def.cache.enabled === false) {
      return;
    }
    for (const artifact of def.artifacts ?? []) {
      if (artifact.cache !== "reference" && artifact.attach?.role === "system") {
        ctx.addIssue({
          code: "custom",
          message:
            `activity ${JSON.stringify(def.name)} is session-cached, but artifact input ` +
            `${JSON.stringify(artifact.name)} attaches with role "system" without cache: reference — ` +
            "per-item system content is dropped on reference-style cache hits (the provider cannot " +
            'send it alongside the cached prefix); use an attach role of "user", or mark the input cache: reference',
          path: ["artifacts"],
        });
      }
    }
  });

/**
 * `modules` is a PERMANENT divergence (#496): Python loads activity modules via importlib
 * (policed by `runtime.imports`) — machinery with no honest TS analogue. Code-defined
 * activities are INJECTED instead (`AssembleYamlRuntimeOptions.extraActivities`), per the
 * thin-injection philosophy: a dynamic `import()` of caller paths would smuggle a
 * code-loading policy surface into a library that otherwise takes everything by injection.
 */
export const activitiesSpec = z
  .object({
    modules: unsupported(
      "activities.modules (Python importlib module loading) is not supported by the TS SDK — inject code-defined activity descriptors via assembleYamlRuntime's `extraActivities` instead (#496)",
    ),
    definitions: z.array(activityDefinitionSpec).optional(),
  })
  .strict();

/**
 * A JSON literal a `when:` predicate compares against (#55 §3.2). Pure data — no user
 * code runs in the workflow, so predicates stay deterministic, digest-stable, and
 * statically analyzable for admission (#298).
 */
const whenLiteral = z.union([z.string(), z.number(), z.boolean(), z.null()]);

/** Ordering comparisons (`lt`/`lte`/`gt`/`gte`) only make sense on orderable literals. */
const whenOrderable = z.union([z.number(), z.string()]);

/** A non-empty trimmed context path (`input.x`, `<stepId>.field`). */
const whenPath = z
  .string()
  .refine((v) => v.length > 0 && v.trim() === v, { message: "when.path must be non-empty and trimmed" });

/** The operator keys of a leaf predicate, in the order the plan normalizer probes them. */
export const WHEN_OPERATORS = ["eq", "neq", "lt", "lte", "gt", "gte", "in", "exists"] as const;

/**
 * Risk-tier vocabulary (#300 D300-1; Python `RISK_TIER_ORDER`): a FIXED, ordered,
 * low-cardinality enum — `safe` ⊂ `policy_gated` ⊂ `human_gated` ⊂ `prohibited`,
 * ascending by strictness. Fixed (not free-form) so composition has a total order
 * (floor merges take the highest tier), `extends` chains share one vocabulary, and the
 * search tag stays 4-valued. The rank is the index into this tuple.
 */
export const RISK_TIER_ORDER = ["safe", "policy_gated", "human_gated", "prohibited"] as const;
export type RiskTier = (typeof RISK_TIER_ORDER)[number];

/**
 * One leaf predicate of the `when:` DSL (#55 §3.2): `{path, <op>: literal}` with exactly
 * one operator. `eq: null` is a real comparison against null (presence is checked with
 * `!== undefined`, so a null literal never reads as "operator absent").
 */
export const whenLeafSpec = z
  .object({
    path: whenPath,
    eq: whenLiteral.optional(),
    neq: whenLiteral.optional(),
    lt: whenOrderable.optional(),
    lte: whenOrderable.optional(),
    gt: whenOrderable.optional(),
    gte: whenOrderable.optional(),
    in: z.array(whenLiteral).optional(),
    exists: yamlBoolean.optional(),
  })
  .strict()
  .refine((leaf) => WHEN_OPERATORS.filter((op) => leaf[op] !== undefined).length === 1, {
    message: "a when predicate requires exactly one operator (eq, neq, lt, lte, gt, gte, in, exists)",
  });

/**
 * Reserved forward-compatible syntax (#55 §3.2): named injected predicates stay
 * deferred — they would move branch logic out of the reviewable YAML — so their
 * presence points at the literal DSL instead of failing as an unknown key.
 */
const whenPredicateStub = z
  .object({ predicate: z.string() })
  .strict()
  .refine(() => false, {
    message:
      "when.predicate (named injected predicates) is not supported — express the gate with the " +
      "literal predicate DSL: {path, eq/neq/lt/lte/gt/gte/in/exists}, optionally one all:/any: level (#55)",
  });

/**
 * A `when:` gate on a step or parallel branch (#55 §3.2): a leaf predicate, or ONE
 * `all:`/`any:` composition level over leaf predicates (never nested — a deeper boolean
 * tree in YAML is unreviewable; decision D1). Evaluated once, when the gated step/branch
 * is reached, against recorded context only.
 */
export const whenSpec = z.union([
  whenLeafSpec,
  z.object({ all: z.array(whenLeafSpec).min(1, "when.all must not be empty") }).strict(),
  z.object({ any: z.array(whenLeafSpec).min(1, "when.any must not be empty") }).strict(),
  whenPredicateStub,
]);

/**
 * The `collect.max_bytes` guard, shared by map and parallel collects. Python semantics
 * EXACTLY: absent defaults the guard ON at 1.5MB (the whole point is an actionable
 * error before Temporal's opaque ~2MB payload limit), an explicit 0 DISABLES it, and
 * null is REJECTED (the union admits only number|string before coercion — a bare null
 * would otherwise coerce to a silent 0 = disabled). "" (an interpolated empty default)
 * is absent.
 */
const collectMaxBytesSpec = z.preprocess(
  (value) => (value === "" ? undefined : value),
  z
    .union([z.number(), z.string()])
    .transform((value) => (typeof value === "string" ? Number(value) : value))
    .refine((value) => Number.isInteger(value) && value >= 0, {
      message: "collect.max_bytes must be >= 0",
    })
    .default(1_500_000),
);

/**
 * A map step: fan `activity` — or, with `workflow:`, a sibling project workflow as a
 * CHILD workflow per item (#55 §3.4) — over `over` with bounded `concurrency`, then
 * collect. Exactly one of `activity`/`workflow`; a `workflow` value is a workflow id
 * in the same project manifest, resolvable only when the spec loads through its
 * project (standalone specs reject at plan derivation).
 */
export const workflowMapSpec = z
  .object({
    activity: z.string().optional(),
    /** Fan a sibling project workflow (by manifest workflow id) over the items (#55 §3.4). */
    workflow: z.string().optional(),
    concurrency: yamlPositiveInt.optional(),
    collect: z
      .object({
        output: z.string(),
        field: z.string(),
        max_bytes: collectMaxBytesSpec,
      })
      .strict()
      .optional(),
    over: z.string(),
  })
  .strict()
  .refine((map) => (map.activity !== undefined) !== (map.workflow !== undefined), {
    message: "a map step requires exactly one of `activity` or `workflow`",
  });

/**
 * Explicit OUTPUT types for the recursive step/parallel schemas. Zod's getter recursion
 * infers these structurally, but tsc's DECLARATION EMIT elides the deep recursion
 * ("elided any" ZodObject shapes), collapsing nested branch steps to Record-of-unknown
 * for downstream packages — so the recursive schemas are annotated with these
 * interfaces instead (the standard zod recursive-type pattern).
 */
export interface WorkflowStepSpec {
  id: string;
  activity?: string | undefined;
  map?: WorkflowMapSpec | undefined;
  parallel?: WorkflowParallelSpec | undefined;
  when?: WhenSpec | undefined;
  /** Run a sibling project workflow (by manifest workflow id) as a child workflow (#55 §3.4). */
  workflow?: string | undefined;
  /** Compensation for this completed step (#299 D299-1); not valid on a `parallel` step (its branches' steps carry their own). */
  compensate?: WorkflowCompensateSpec | undefined;
}

export interface WorkflowParallelBranchSpec {
  id: string;
  when?: WhenSpec | undefined;
  steps: WorkflowStepSpec[];
}

export interface WorkflowParallelSpec {
  branches: WorkflowParallelBranchSpec[];
  collect: WorkflowParallelCollectSpec;
}

/**
 * The `collect` of a parallel block (#55 §3.1) — the map collect shape minus `field`:
 * the merged object's fields ARE the branch ids (decision D4), so only the output
 * schema ref and the payload bound are declared. `max_bytes` semantics are exactly the
 * map's (#495): absent defaults the guard ON at 1.5MB, an explicit 0 disables it.
 */
export const workflowParallelCollectSpec = z
  .object({
    output: z.string(),
    max_bytes: collectMaxBytesSpec,
  })
  .strict();

/**
 * A workflow step keyed by `id`, with exactly one of `activity`, `map`, `parallel`,
 * or `workflow` (enforced by `workflowPlanFromSpec`), optionally gated by `when`
 * (#55 §3.2 — a false gate skips the step and the remainder of its enclosing
 * sequence). A `workflow:` step runs a sibling project workflow — referenced by its
 * project-manifest workflow id — as a Temporal CHILD workflow (#55 §3.4). Lifecycle
 * review is a workflow-level gate in Python (`workflow.lifecycle`), not a step kind —
 * see `workflowSpec`. The `parallel` getter breaks the step -> parallel -> branch ->
 * step recursion (Zod v4 getter recursion; `z.strictObject` because `.strict()` would
 * materialize the getter before `workflowParallelSpec` initializes).
 */
export const workflowStepSpec: z.ZodType<WorkflowStepSpec> = z.strictObject({
  id: z.string(),
  activity: z.string().optional(),
  map: workflowMapSpec.optional(),
  get parallel() {
    return workflowParallelSpec.optional();
  },
  when: whenSpec.optional(),
  workflow: z.string().optional(),
  compensate: workflowCompensateSpec.optional(),
});

/**
 * One branch of a parallel block (#55 §3.1): a nested sequence of steps (any kinds,
 * including nested `parallel` up to the depth ceiling), optionally gated by `when`
 * (a gated-out branch contributes null to its collect field). Branch ids share the
 * workflow's single flat id namespace.
 */
export const workflowParallelBranchSpec: z.ZodType<WorkflowParallelBranchSpec> = z.strictObject({
  id: z.string(),
  when: whenSpec.optional(),
  get steps() {
    return z.array(workflowStepSpec).min(1, "parallel branch steps must not be empty");
  },
});

/**
 * A `parallel:` block (#55 §3.1): heterogeneous branches running concurrently, merged
 * into a typed collect object keyed by branch id. Requires >= 1 branch and a
 * `collect.output`; the merge-field/Optional typing rule is checked against resolved
 * schemas by Python — TS's injected-schema model catches a mismatch at the strict
 * activity boundary instead (the documented ref-vs-type asymmetry).
 */
export const workflowParallelSpec: z.ZodType<WorkflowParallelSpec> = z.strictObject({
  branches: z.array(workflowParallelBranchSpec).min(1, "parallel.branches must not be empty"),
  collect: workflowParallelCollectSpec,
});

/** Status-event history bounds (Python `WorkflowLifecycleHistorySpec`). */
export const workflowLifecycleHistorySpec = z
  .object({
    status_event_limit: z.coerce.number().int().min(0).default(50),
  })
  .strict();

/** One review route: the step the workflow jumps to for a decision (Python `WorkflowLifecycleReviewRouteSpec`). */
export const workflowLifecycleReviewRouteSpec = z
  .object({
    route: artifactField,
  })
  .strict();

/** The review timeout (Python `WorkflowLifecycleReviewTimeoutSpec`): `route` required iff `on_timeout: route`. */
export const workflowLifecycleReviewTimeoutSpec = z
  .object({
    seconds: z.coerce.number().int().min(1),
    on_timeout: z.enum(["fail", "cancel", "route"]).default("fail"),
    route: artifactField.optional(),
  })
  .strict()
  .refine((t) => (t.on_timeout === "route") === (t.route !== undefined), {
    message: "review timeout `route` is required exactly when on_timeout is `route`",
  });

/**
 * The human-in-the-loop review gate (Python `WorkflowLifecycleReviewSpec`): after `after_step`
 * completes the workflow waits for a `typeflux_submit_review` signal and routes on
 * `user_decisions` (forward-only, validated in `workflowPlanFromSpec`).
 */
export const workflowLifecycleReviewSpec = z
  .object({
    after_step: artifactField,
    user_decisions: z
      .record(artifactField, workflowLifecycleReviewRouteSpec)
      .refine((d) => Object.keys(d).length > 0, { message: "review user_decisions must not be empty" }),
    invalid_user_decision: z.enum(["warn", "fail"]).default("warn"),
    timeout: workflowLifecycleReviewTimeoutSpec.optional(),
  })
  .strict();

/**
 * One named review gate (#55 §8, Python `WorkflowLifecycleGateSpec`): the multi-gate
 * generalization of `review`. Same fields plus a required `id`; the singular `review`
 * stays as sugar for one gate named `"review"`. Routes are validated forward-only per
 * gate in `workflowPlanFromSpec` (the §3.3 routed-tail checks generalize per gate).
 */
export const workflowLifecycleGateSpec = z
  .object({
    id: artifactField,
    after_step: artifactField,
    user_decisions: z
      .record(artifactField, workflowLifecycleReviewRouteSpec)
      .refine((d) => Object.keys(d).length > 0, { message: "gate user_decisions must not be empty" }),
    invalid_user_decision: z.enum(["warn", "fail"]).default("warn"),
    timeout: workflowLifecycleReviewTimeoutSpec.optional(),
  })
  .strict();

/**
 * Workflow lifecycle config (#482, Python `WorkflowLifecycleSpec`): the status query, the cancel
 * signal, progress tracking, the bounded event history, and the optional review gate(s). A
 * workflow declares EITHER `review` (one gate, the V1 form — digest-stable) OR `gates` (a list
 * of named gates, #55 slice 4), never both.
 */
export const workflowLifecycleSpec = z
  .object({
    enabled: yamlBoolean.default(false),
    progress: yamlBoolean.default(true),
    cancellation: yamlBoolean.default(true),
    history: workflowLifecycleHistorySpec.default({ status_event_limit: 50 }),
    review: workflowLifecycleReviewSpec.optional(),
    gates: z.array(workflowLifecycleGateSpec).min(1, "workflow.lifecycle.gates must not be empty").optional(),
  })
  .strict()
  .superRefine((lifecycle, ctx) => {
    // `review` is the single-gate sugar; `gates` is the multi-gate form — mutually exclusive
    // so there is one unambiguous representation (and one digest) per spec.
    if (lifecycle.review !== undefined && lifecycle.gates !== undefined) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "workflow.lifecycle: use either `review` (a single gate) or `gates` (multiple named gates), not both",
        path: ["gates"],
      });
    }
    if (lifecycle.gates !== undefined) {
      // Unique gate ids (they name gates in the status wire + the `gate` signal field) and
      // DISTINCT after_step (each gate sits after a distinct top-level step — decision DS4-1:
      // guarantees at most one gate waits at a time in a v1 sequence, no route ambiguity).
      const seenIds = new Set<string>();
      const seenAfter = new Set<string>();
      // Decision DS4-6: a decision NAME reused across gates must route to the SAME target
      // (mirrors the activity-name collision rule) — the control plane's flat
      // valid_user_decisions fallback unions gates' decisions, so a divergent reuse would
      // advertise a route the runtime may route differently. Identical reuse stays allowed,
      // keeping the flat union well-defined.
      const decisionRoutes = new Map<string, { gateId: string; route: string }>();
      lifecycle.gates.forEach((gate, index) => {
        if (seenIds.has(gate.id)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: `workflow.lifecycle.gates: duplicate gate id ${JSON.stringify(gate.id)}`,
            path: ["gates", index, "id"],
          });
        }
        seenIds.add(gate.id);
        if (seenAfter.has(gate.after_step)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message:
              `workflow.lifecycle.gates: gate ${JSON.stringify(gate.id)} shares after_step ` +
              `${JSON.stringify(gate.after_step)} with an earlier gate — each gate must follow a ` +
              "distinct step (combine decisions into one gate for a single checkpoint)",
            path: ["gates", index, "after_step"],
          });
        }
        seenAfter.add(gate.after_step);
        for (const [decision, route] of Object.entries(gate.user_decisions)) {
          const prior = decisionRoutes.get(decision);
          if (prior !== undefined && prior.route !== route.route) {
            ctx.addIssue({
              code: z.ZodIssueCode.custom,
              message:
                `workflow.lifecycle.gates: decision ${JSON.stringify(decision)} routes to ` +
                `${JSON.stringify(prior.route)} on gate ${JSON.stringify(prior.gateId)} but to ` +
                `${JSON.stringify(route.route)} on gate ${JSON.stringify(gate.id)} — a decision ` +
                "name reused across gates must keep identical route semantics (rename one decision, DS4-6)",
              path: ["gates", index, "user_decisions", decision],
            });
          }
          if (prior === undefined) {
            decisionRoutes.set(decision, { gateId: gate.id, route: route.route });
          }
        }
      });
    }
  });

/**
 * A declarative subject selector on the workflow (#715 slice 1; Python
 * `SubjectInputSpec`). Mirrors an activity `artifacts:` input's `from:` shape —
 * `from` is a dotted `input.<path>` selector pulled off the input at start to
 * yield the subject id(s) this execution processes. `required` (default true)
 * makes a missing selection a hard error at start.
 */
export const subjectInputSpec = z
  .object({
    from: z
      .string()
      .refine((v) => v.length > 0 && v.trim() === v, {
        message: "subject input from must be non-empty and trimmed",
      })
      .refine((v) => v.startsWith("input."), {
        message: "subject input from must start with 'input.'",
      }),
    required: z.boolean().default(true),
  })
  .strict();

export type SubjectInputSpec = z.infer<typeof subjectInputSpec>;

export const workflowSpec = z
  .object({
    name: z.string(),
    input: z.string(),
    output: z.string().optional(),
    steps: z.array(workflowStepSpec).min(1, "workflow.steps must not be empty"),
    // A frozen pointer to ONE graph (#530; Python parity — same label charset):
    // starts stamp the plan digest into the execution memo, and the runtime
    // refuses to start under a label whose recorded digest differs from the
    // loaded graph. Enforcement is at START time in TS (see frozen-version.ts):
    // plan-as-argument already makes in-flight executions immune to graph edits.
    version: z
      .string()
      .regex(
        /^[A-Za-z0-9][A-Za-z0-9._-]*$/,
        "workflow.version must start with an alphanumeric character and contain only alphanumerics, '.', '_', or '-'",
      )
      .optional(),
    lifecycle: workflowLifecycleSpec.optional(),
    // The workflow's DECLARED risk tier (#300 D300-2; Python `WorkflowSpec.risk_tier`).
    // Optional — unset reads as `safe` at evaluation time (never materialized here), so
    // it is pure governance metadata with no control-flow effect and stays OUT of the
    // plan digest. The project policy's `risk_tiers` dimension DEFINES what each requires.
    risk_tier: z.enum(RISK_TIER_ORDER).optional(),
    // Declarative subject selectors (#715 slice 1; Python `WorkflowSpec.subjects`). Each
    // pulls subject id(s) off the workflow input at submit time; the union stamps the
    // `TypefluxSubjectIds` keyword-list search attribute (the erasure index) and fans to
    // the observer / cache carriers. Submit-time metadata with no control-flow effect, so
    // — like `risk_tier` — it stays OUT of the plan digest.
    subjects: z.array(subjectInputSpec).optional(),
  })
  .strict();

export const typefluxYamlSpec = z
  .object({
    project: z.string(),
    name: z.string(),
    task_queue: z.string(),
    runtime: runtimeSpec,
    activities: activitiesSpec,
    workflow: workflowSpec,
  })
  .strict()
  .superRefine((spec, ctx) => {
    // Per-provider param support (#495; Python validate_provider_params_supported): a
    // configured param the selected provider never maps would be silently ignored —
    // reject at load across all three params sites.
    const providerType = spec.runtime.provider.type;
    const reject = (keys: string[], path: (string | number)[]): void => {
      if (keys.length > 0) {
        ctx.addIssue({
          code: "custom",
          message:
            `provider ${JSON.stringify(providerType)} does not support provider params: ` +
            keys.join(", "),
          path,
        });
      }
    };
    reject(unsupportedProviderParamKeys(providerType, spec.runtime.provider.params), [
      "runtime",
      "provider",
      "params",
    ]);
    for (const [name, prompt] of Object.entries(spec.runtime.registry.prompts ?? {})) {
      if (typeof prompt === "object" && prompt !== null && "provider_params" in prompt) {
        reject(unsupportedProviderParamKeys(providerType, prompt.provider_params), [
          "runtime",
          "registry",
          "prompts",
          name,
          "provider_params",
        ]);
      }
    }
    for (const [index, def] of (spec.activities.definitions ?? []).entries()) {
      reject(unsupportedProviderParamKeys(providerType, def.provider_params), [
        "activities",
        "definitions",
        index,
        "provider_params",
      ]);
    }
  });

export type SecretValueSpec = z.infer<typeof secretValueSpec>;
export type PayloadCodecSpec = z.infer<typeof payloadCodecSpec>;
export type PayloadCodecKeySpec = z.infer<typeof payloadCodecKeySpec>;
export type PayloadCodecSubjectScopeSpec = z.infer<typeof payloadCodecSubjectScopeSpec>;
export type TemporalSpec = z.infer<typeof temporalSpec>;
export type TemporalTlsSpec = z.infer<typeof temporalTlsSpec>;
export type RegistrySpec = z.infer<typeof registrySpec>;
export type ProviderSpec = z.infer<typeof providerSpec>;
export type ObservabilitySpec = z.infer<typeof observabilitySpec>;
export type RedactionSpec = z.infer<typeof redactionSpec>;
export type ModerationSpec = z.infer<typeof moderationSpec>;
export type ActivityRetrySpec = z.infer<typeof activityRetrySpec>;
export type ArtifactAttachmentSpec = z.infer<typeof artifactAttachmentSpec>;
export type ArtifactInputSpec = z.infer<typeof artifactInputSpec>;
export type ArtifactRuntimeSpec = z.infer<typeof artifactRuntimeSpec>;
export type WorkflowLifecycleSpec = z.infer<typeof workflowLifecycleSpec>;
export type WorkflowLifecycleHistorySpec = z.infer<typeof workflowLifecycleHistorySpec>;
export type WorkflowLifecycleReviewSpec = z.infer<typeof workflowLifecycleReviewSpec>;
export type WorkflowLifecycleGateSpec = z.infer<typeof workflowLifecycleGateSpec>;
export type WorkflowLifecycleReviewTimeoutSpec = z.infer<typeof workflowLifecycleReviewTimeoutSpec>;
export type PromptRefSpec = z.infer<typeof promptRefSpec>;
export type InlineContentPartSpec = z.infer<typeof inlineContentPartSpec>;
export type InlinePromptMessageSpec = z.infer<typeof inlinePromptMessageSpec>;
export type InlinePromptSpec = z.infer<typeof inlinePromptSpec>;
export type ActivityDefinitionSpec = z.infer<typeof activityDefinitionSpec>;
export type RuntimeSpec = z.infer<typeof runtimeSpec>;
export type ActivitiesSpec = z.infer<typeof activitiesSpec>;
export type WorkflowMapSpec = z.infer<typeof workflowMapSpec>;
export type WhenLeafSpec = z.infer<typeof whenLeafSpec>;
export type WhenSpec = z.infer<typeof whenSpec>;
export type WorkflowParallelCollectSpec = z.infer<typeof workflowParallelCollectSpec>;
// WorkflowStepSpec / WorkflowParallelBranchSpec / WorkflowParallelSpec are the explicit
// recursive interfaces declared above (declaration-emit elision workaround).
export type WorkflowSpec = z.infer<typeof workflowSpec>;
export type TypefluxYamlSpec = z.infer<typeof typefluxYamlSpec>;
