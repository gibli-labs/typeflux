/**
 * Prompt registry (parity Epic 1, #448) — resolves a `PromptRef` to a
 * `ResolvedPrompt`, mirroring the Python `prompts/base.py` (`PromptRegistry`) +
 * `inline.py` (`InlinePromptRegistry`). `executeActivity` uses it to resolve the
 * activity's `prompt` ref, then renders the messages against the input.
 */

import type { ChatMessage } from "./manifest-hashing.js";
import type { PromptRef } from "./prompt-ref.js";

/** A resolved prompt: the messages plus the resolve-time facts the executor records. */
export interface ResolvedPrompt {
  ref: PromptRef;
  messages: ChatMessage[];
  resolvedVersion?: string | null;
  /** The model the prompt selects (becomes the default when the call sets none). */
  model?: string | null;
  temperature?: number | null;
  /**
   * Prompt-level provider params (#495; Python `ResolvedPrompt.provider_params`):
   * merged OVER the call's defaults and UNDER the activity's own params in
   * `executeActivity`'s resolution (defaults < prompt < activity, later wins).
   */
  providerParams?: Record<string, unknown> | null;
}

/** A source that resolves prompt refs (inline, LangSmith, Langfuse, ...). */
export interface PromptRegistry {
  resolve(ref: PromptRef): ResolvedPrompt | Promise<ResolvedPrompt>;
}

/**
 * Typed prompt-resolution failures (Python `prompts/errors.py` parity). The
 * `retryable` flag is the load-bearing contract: the worker's terminal-error
 * classification fails a NON-retryable resolution error on attempt 1 (a config
 * error, bad credentials, or a missing prompt reproduce deterministically),
 * while a retryable one (registry outage/timeout) passes through for the
 * Temporal RetryPolicy — exactly Python's `retryable=` posture per subclass.
 */
export class PromptResolutionError extends Error {
  readonly ref?: PromptRef;
  readonly retryable: boolean;
  readonly statusCode?: number;
  constructor(
    reason: string,
    options: { ref?: PromptRef; retryable?: boolean; statusCode?: number; cause?: unknown } = {},
  ) {
    super(reason, ...(options.cause !== undefined ? [{ cause: options.cause }] : []));
    this.name = "PromptResolutionError";
    if (options.ref !== undefined) {
      this.ref = options.ref;
    }
    this.retryable = options.retryable ?? false;
    if (options.statusCode !== undefined) {
      this.statusCode = options.statusCode;
    }
  }
}

export class PromptNotFoundError extends PromptResolutionError {
  declare readonly ref: PromptRef;
  constructor(ref: PromptRef, options: { reason?: string; statusCode?: number; cause?: unknown } = {}) {
    super(options.reason ?? `prompt not found: ${ref.name}`, { ...options, ref, retryable: false });
    this.name = "PromptNotFoundError";
  }
}

/** Bad or missing registry credentials (Python `PromptRegistryAuthError`): retrying cannot fix a key. */
export class PromptRegistryAuthError extends PromptResolutionError {
  constructor(reason: string, options: { ref?: PromptRef; statusCode?: number; cause?: unknown } = {}) {
    super(reason, { ...options, retryable: false });
    this.name = "PromptRegistryAuthError";
  }
}

/** A malformed/unusable resolved prompt or request (Python `PromptRegistryConfigError`): deterministic, non-retryable. */
export class PromptRegistryConfigError extends PromptResolutionError {
  constructor(reason: string, options: { ref?: PromptRef; statusCode?: number; cause?: unknown } = {}) {
    super(reason, { ...options, retryable: false });
    this.name = "PromptRegistryConfigError";
  }
}

/** A transient registry failure (Python `PromptRegistryUnavailableError`): the one RETRYABLE class. */
export class PromptRegistryUnavailableError extends PromptResolutionError {
  constructor(reason: string, options: { ref?: PromptRef; statusCode?: number; cause?: unknown } = {}) {
    super(reason, { ...options, retryable: true });
    this.name = "PromptRegistryUnavailableError";
  }
}

/** A registry value: a bare user-message string, one message, many, or a full ResolvedPrompt. */
export type InlinePromptValue = string | ChatMessage | ChatMessage[] | ResolvedPrompt;

function isResolvedPrompt(value: ChatMessage | ResolvedPrompt): value is ResolvedPrompt {
  return "messages" in value && "ref" in value;
}

/** In-memory registry keyed by prompt name (parity with `InlinePromptRegistry`). */
export class InlinePromptRegistry implements PromptRegistry {
  private readonly prompts: Map<string, InlinePromptValue>;

  constructor(prompts: Record<string, InlinePromptValue>) {
    this.prompts = new Map(Object.entries(prompts));
  }

  resolve(ref: PromptRef): ResolvedPrompt {
    const value = this.prompts.get(ref.name);
    if (value === undefined) {
      throw new PromptNotFoundError(ref);
    }
    const resolvedVersion = ref.version != null ? String(ref.version) : null;
    if (typeof value === "string") {
      return { ref, messages: [{ role: "user", content: value }], resolvedVersion };
    }
    if (Array.isArray(value)) {
      return { ref, messages: value, resolvedVersion };
    }
    if (isResolvedPrompt(value)) {
      return value;
    }
    return { ref, messages: [value], resolvedVersion };
  }
}

/** A prompt fetched from a backend registry — the raw shape the transport returns. */
export interface RawPrompt {
  messages: ChatMessage[];
  model?: string | null;
  temperature?: number | null;
  /** Prompt-config provider params (#495) — subject to the model-override gate. */
  providerParams?: Record<string, unknown> | null;
  /** The backend's resolved version/label (defaults to the ref's version). */
  resolvedVersion?: string | null;
}

/** The injected fetch — wire a real Langfuse/LangSmith client (or HTTP) via an adapter. */
export interface RegistryTransport {
  fetchPrompt(ref: PromptRef): RawPrompt | Promise<RawPrompt>;
}

export interface TransportPromptRegistryOptions {
  /** Default label applied to a ref that pins NEITHER a version NOR a label (parity with
   * Python's `_LabelOverrideRegistry` — the spec's `registry.label`). */
  defaultLabel?: string;
  /**
   * When false (the default — Python `allow_prompt_model_override` parity), a BACKEND
   * prompt's `model` (and any `providerParams.model`) is STRIPPED at resolution: the
   * provider/YAML configuration stays authoritative for the execution model, and the
   * prompt's model remains visible only as backend metadata. Inline prompts are
   * code-authored and exempt — this gate is for prompts fetched from a registry a
   * prompt-editor can change without a deploy.
   */
  allowPromptModelOverride?: boolean;
}

/**
 * A `PromptRegistry` backed by an injected `RegistryTransport` (Langfuse/LangSmith/custom).
 * `resolve` fetches the prompt by ref and maps it to a `ResolvedPrompt`. No vendor SDK
 * dependency — the caller adapts the real client's `getPrompt` to `fetchPrompt`.
 */
export class TransportPromptRegistry implements PromptRegistry {
  constructor(
    private readonly transport: RegistryTransport,
    private readonly options: TransportPromptRegistryOptions = {},
  ) {}

  async resolve(ref: PromptRef): Promise<ResolvedPrompt> {
    const effectiveRef = this.withDefaultLabel(ref);
    const raw = await this.transport.fetchPrompt(effectiveRef);
    const resolved: ResolvedPrompt = {
      ref: effectiveRef,
      messages: raw.messages,
      resolvedVersion: raw.resolvedVersion ?? (effectiveRef.version != null ? String(effectiveRef.version) : null),
    };
    // The model-override gate (#495; Python langfuse.py parity): unless explicitly
    // allowed, a backend prompt's model — top-level AND inside providerParams — is
    // stripped so the provider/YAML config stays authoritative for the execution model.
    const allowModel = this.options.allowPromptModelOverride === true;
    if (allowModel && raw.model !== undefined) {
      resolved.model = raw.model;
    }
    if (raw.temperature !== undefined) {
      resolved.temperature = raw.temperature;
    }
    if (raw.providerParams != null) {
      const params = { ...raw.providerParams };
      if (!allowModel) {
        delete params["model"];
      }
      resolved.providerParams = params;
    }
    return resolved;
  }

  /** Fill the default label only when set (truthy) and the ref pins neither a version nor a label. */
  private withDefaultLabel(ref: PromptRef): PromptRef {
    // `!defaultLabel` (not just `=== undefined`): an empty-string default is no override (parity
    // with Python's truthy `if registry_spec.label:`).
    if (!this.options.defaultLabel || ref.version != null || ref.label != null) {
      return ref;
    }
    return { ...ref, label: this.options.defaultLabel };
  }
}
