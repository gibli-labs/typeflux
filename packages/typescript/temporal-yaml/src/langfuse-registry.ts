/**
 * Out-of-the-box Langfuse prompt registry (Python parity): when the spec
 * declares `runtime.registry.type: langfuse`, `buildRuntime` wires a
 * `RegistryTransport` over the OFFICIAL `langfuse` SDK (user directive:
 * official SDK) — no transport code in the caller, exactly like Python's
 * `_build_registry` constructing a `LangfusePromptRegistry` from the
 * environment. The SDK is an OPTIONAL peer dependency loaded lazily (the TS
 * mirror of Python's `[langfuse]` extra), shared with `langfuse-observer.ts`.
 *
 * Credentials come from the standard environment — `LANGFUSE_PUBLIC_KEY`,
 * `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST`/`LANGFUSE_BASEURL` for
 * self-hosted (`runtime.registry.host` wins when set, Python `_langfuse_host`).
 * UNLIKE observability — which degrades to an untraced run — absent registry
 * credentials THROW: prompts are load-bearing, an activity cannot execute
 * without its registry, so a silent fallback would only defer the failure to a
 * cryptic mid-run resolve.
 *
 * LangSmith mirrors this in `langsmith-registry.ts`. Custom backends keep the
 * injected seam: pass your own `registry`/`registryTransport` and this module
 * never runs. Model/label governance is NOT here — `TransportPromptRegistry`
 * (via `registryFromSpec`) owns the default-label fill and the #495
 * model-override strip; this transport reports what the backend said.
 */

import {
  type ChatMessage,
  type ContentPart,
  PromptNotFoundError,
  type PromptRef,
  PromptRegistryAuthError,
  PromptRegistryConfigError,
  PromptRegistryUnavailableError,
  PromptResolutionError,
  type RawPrompt,
  type RegistryTransport,
} from "@typeflux/temporal";

import type { TypefluxYamlSpec } from "./spec.js";

/**
 * The slice of the official SDK's prompt client this module reads (structural —
 * tests fake it): `getPrompt` resolves to a `TextPromptClient` (`prompt` is the
 * template string) or `ChatPromptClient` (`prompt` is a message list), each
 * carrying the registry `version` and the prompt-level `config` object.
 */
export interface LangfuseRegistryPromptClient {
  type: "text" | "chat";
  prompt: unknown;
  version?: number;
  config?: unknown;
}

/** The slice of the official SDK's client this module drives (structural — tests fake it). */
export interface LangfuseRegistrySdkClient {
  getPrompt(
    name: string,
    version?: number,
    options?: { label?: string },
  ): Promise<LangfuseRegistryPromptClient>;
}

/** Python `PromptRef.selector`: the human-readable selector in error messages. */
function selector(ref: PromptRef): string {
  if (ref.version != null) {
    return `v${ref.version}`;
  }
  if (ref.label != null) {
    return ref.label;
  }
  return "default-label";
}

const CHAT_ROLES = new Set(["system", "user", "assistant"]);

/**
 * Structurally validate registry message content as Typeflux `ChatContent`
 * (Python `normalize_content_parts` posture: a string passes through, a
 * sequence must be Typeflux-shaped content parts — anything else degrades to a
 * clean config error upstream, not a runtime surprise mid-render).
 */
function chatContent(value: unknown): string | ContentPart[] | undefined {
  if (typeof value === "string") {
    return value;
  }
  if (!Array.isArray(value)) {
    return undefined;
  }
  const parts: ContentPart[] = [];
  for (const part of value) {
    if (typeof part !== "object" || part === null) {
      return undefined;
    }
    const record = part as Record<string, unknown>;
    const validShape =
      (record["type"] === "text" && typeof record["text"] === "string") ||
      (record["type"] === "artifact" && typeof record["artifact"] === "string") ||
      (record["type"] === "artifact_group" && typeof record["group"] === "string") ||
      (record["type"] === "provider_extension" &&
        typeof record["provider"] === "string" &&
        typeof record["payload"] === "object" &&
        record["payload"] !== null);
    if (!validShape) {
      return undefined;
    }
    parts.push(part as ContentPart);
  }
  return parts;
}

/** Mirror Python `_chat_message_from_langfuse_message`: role gate, content shape, optional name. */
function chatMessageFromLangfuseMessage(message: unknown, ref: PromptRef, index: number): ChatMessage {
  const record = typeof message === "object" && message !== null ? (message as Record<string, unknown>) : {};
  const role = record["role"];
  // A langfuse `placeholder` entry has no role and fails here LOUD (Python
  // parity: unresolved placeholders cannot become renderable messages).
  if (typeof role !== "string" || !CHAT_ROLES.has(role)) {
    throw new PromptRegistryConfigError(
      `resolved chat prompt ${ref.name}@${selector(ref)} message ${index} ` +
        "role must be system, user, or assistant",
      { ref },
    );
  }
  const content = chatContent(record["content"]);
  if (content === undefined) {
    throw new PromptRegistryConfigError(
      `resolved chat prompt ${ref.name}@${selector(ref)} message ${index} ` +
        "content must be text or Typeflux content parts",
      { ref },
    );
  }
  const name = record["name"];
  if (name !== undefined && name !== null && typeof name !== "string") {
    throw new PromptRegistryConfigError(
      `resolved chat prompt ${ref.name}@${selector(ref)} message ${index} ` +
        "name must be text when provided",
      { ref },
    );
  }
  return { role, content, ...(typeof name === "string" ? { name } : {}) };
}

/** Mirror Python `_messages_from_langfuse_prompt`: text → one user message; chat → the message list. */
function messagesFromLangfusePrompt(prompt: LangfuseRegistryPromptClient, ref: PromptRef): ChatMessage[] {
  if (prompt.type === "text") {
    if (typeof prompt.prompt !== "string") {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} did not contain text content`,
        { ref },
      );
    }
    return [{ role: "user", content: prompt.prompt }];
  }
  if (!Array.isArray(prompt.prompt) || prompt.prompt.length === 0) {
    throw new PromptRegistryConfigError(
      `resolved prompt ${ref.name}@${selector(ref)} did not contain chat messages`,
      { ref },
    );
  }
  return prompt.prompt.map((message, index) => chatMessageFromLangfuseMessage(message, ref, index));
}

/**
 * Classify an SDK fetch failure into the typed hierarchy (Python
 * `_classify_langfuse_prompt_error`). The JS SDK folds most HTTP failures into
 * a plain `Error(data.message)` (no status attribute), so classification here
 * is by error NAME for the SDK's typed network/5xx errors and by message text
 * for the rest — the same buckets Python lands in, reached from what the JS
 * SDK actually exposes.
 */
function classifyLangfusePromptError(error: unknown, ref: PromptRef): PromptResolutionError {
  const reason = `failed to resolve prompt ${ref.name}@${selector(ref)}`;
  const name = error instanceof Error ? error.name : "";
  const message = error instanceof Error ? error.message : String(error);
  // LangfuseFetchNetworkError (timeout/DNS/refused) and LangfuseFetchHttpError
  // (the SDK throws it for 5xx only) are the transient bucket.
  if (name.includes("LangfuseFetchNetworkError") || name.includes("LangfuseFetchHttpError")) {
    return new PromptRegistryUnavailableError(reason, { ref, cause: error });
  }
  // The v3 SDK folds non-5xx HTTP failures into a plain Error(message) — a
  // rate limit (429) or timeout (408) is TRANSIENT and must stay retryable,
  // or Temporal fails the activity on the first backoff-worthy blip (codex).
  if (/rate.?limit|too many requests|\b429\b|\b408\b|timed?\s*out/i.test(message)) {
    return new PromptRegistryUnavailableError(reason, { ref, cause: error });
  }
  if (/not\s*found/i.test(message)) {
    return new PromptNotFoundError(ref, { reason, cause: error });
  }
  if (/unauthorized|access denied|forbidden|invalid credentials/i.test(message)) {
    return new PromptRegistryAuthError(reason, { ref, cause: error });
  }
  return new PromptResolutionError(reason, { ref, retryable: false, cause: error });
}

/**
 * Adapt the official SDK's `getPrompt` to the `RegistryTransport` seam
 * (Python `LangfusePromptRegistry.resolve`). A ref's `version` pins an
 * immutable registry version; otherwise its `label` selects, defaulting to
 * `production` (Python's explicit default — `registryFromSpec` has already
 * applied any `runtime.registry.label` before the ref reaches here). The ref's
 * `promptType` hint is validated against the RESOLVED type: unlike Python's
 * SDK — where `get_prompt(type=...)` raises on mismatch and `auto` retries
 * text-then-chat — the JS SDK returns whichever client matches the stored
 * prompt, so one fetch settles `auto` and a mismatched hint fails loud here
 * with Python's exact config-error text.
 */
export class LangfuseRegistryTransport implements RegistryTransport {
  constructor(private readonly client: LangfuseRegistrySdkClient) {}

  async fetchPrompt(ref: PromptRef): Promise<RawPrompt> {
    let prompt: LangfuseRegistryPromptClient;
    try {
      prompt =
        ref.version != null
          ? await this.client.getPrompt(ref.name, ref.version)
          : await this.client.getPrompt(ref.name, undefined, { label: ref.label ?? "production" });
    } catch (error) {
      throw classifyLangfusePromptError(error, ref);
    }
    const hint = ref.promptType ?? "auto";
    if (hint === "text" && prompt.type !== "text") {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} did not contain text content`,
        { ref },
      );
    }
    if (hint === "chat" && prompt.type !== "chat") {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} did not contain chat messages`,
        { ref },
      );
    }
    const messages = messagesFromLangfusePrompt(prompt, ref);

    // Prompt-level config (Python parity): `model`/`provider_model` +
    // `temperature` + `provider_params` ride the prompt's config object. The
    // #495 model-override strip is NOT here — `TransportPromptRegistry`
    // removes the model unless the spec allows it.
    const rawConfig = prompt.config ?? {};
    if (typeof rawConfig !== "object" || Array.isArray(rawConfig)) {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} config must be a mapping`,
        { ref },
      );
    }
    const config = rawConfig as Record<string, unknown>;
    // Truthy `||`, not `??` (Python `config.get("model") or config.get("provider_model")`):
    // an empty-string model falls through, it never selects.
    const model = config["model"] || config["provider_model"];
    const temperature = config["temperature"];
    if (model !== undefined && model !== null && typeof model !== "string") {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} model must be a string`,
        { ref },
      );
    }
    if (temperature !== undefined && temperature !== null && typeof temperature !== "number") {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} temperature must be numeric`,
        { ref },
      );
    }
    const providerParams = this.providerParamsFromConfig(config, ref, model as string | undefined, temperature);
    return {
      messages,
      ...(typeof model === "string" ? { model } : {}),
      ...(typeof temperature === "number" ? { temperature } : {}),
      ...(providerParams !== undefined ? { providerParams } : {}),
      // Python: `str(prompt.version) if ... is not None else None`.
      resolvedVersion: prompt.version != null ? String(prompt.version) : null,
    };
  }

  /**
   * Mirror Python `_provider_params_from_config`: `provider_params` must be a
   * mapping, and a legacy top-level `model`/`temperature` that CONTRADICTS the
   * params copy is a config error, not a silent merge (then they are one value).
   */
  private providerParamsFromConfig(
    config: Record<string, unknown>,
    ref: PromptRef,
    model: string | undefined,
    temperature: unknown,
  ): Record<string, unknown> | undefined {
    const raw = config["provider_params"];
    if (raw === undefined || raw === null) {
      return undefined;
    }
    if (typeof raw !== "object" || Array.isArray(raw)) {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} provider_params must be a mapping`,
        { ref },
      );
    }
    const params = raw as Record<string, unknown>;
    if (model != null && params["model"] != null && params["model"] !== model) {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} provider_params invalid: ` +
          "model and provider_params.model must match",
        { ref },
      );
    }
    if (temperature != null && params["temperature"] != null && params["temperature"] !== temperature) {
      throw new PromptRegistryConfigError(
        `resolved prompt ${ref.name}@${selector(ref)} provider_params invalid: ` +
          "temperature and provider_params.temperature must match",
        { ref },
      );
    }
    return params;
  }
}

/**
 * Build the out-of-the-box registry transport for a `type: langfuse` spec.
 * Returns undefined for every other type (inline self-builds in
 * `registryFromSpec`; langsmith has its own module; custom keeps the injected
 * seam). THROWS — does not degrade — when the credentials are absent (a run
 * cannot proceed without its prompts) or when the spec opts in but the
 * optional `langfuse` peer is not installed.
 */
export async function langfuseRegistryTransportFromSpec(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): Promise<RegistryTransport | undefined> {
  if (spec.runtime.registry.type !== "langfuse") {
    return undefined;
  }
  const publicKey = environment["LANGFUSE_PUBLIC_KEY"];
  const secretKey = environment["LANGFUSE_SECRET_KEY"];
  if (!publicKey || !secretKey) {
    throw new Error(
      "runtime.registry.type is langfuse but LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are not set — " +
        "prompts are REQUIRED (unlike tracing, a run cannot degrade past its registry). Export the " +
        "keys (plus LANGFUSE_HOST for self-hosted), or inject `registry`/`registryTransport` yourself.",
    );
  }
  let sdk: { Langfuse: new (options: Record<string, unknown>) => LangfuseRegistrySdkClient };
  try {
    sdk = (await import("langfuse")) as unknown as typeof sdk;
  } catch (error) {
    throw new Error(
      "runtime.registry.type is langfuse but the langfuse SDK is not installed — " +
        "add it to your app (npm/pnpm add langfuse). It is an optional peer dependency, " +
        "loaded only when a spec opts into the langfuse prompt registry (Python's [langfuse] extra).",
      { cause: error },
    );
  }
  // Python `_langfuse_host`: `registry.host or LANGFUSE_HOST or LANGFUSE_BASE_URL` —
  // truthy `||` (the ported truthiness trap), with the JS SDK's `LANGFUSE_BASEURL`
  // spelling as the env fallback (parity with langfuse-observer.ts).
  const baseUrl =
    spec.runtime.registry.host || environment["LANGFUSE_HOST"] || environment["LANGFUSE_BASEURL"];
  const client = new sdk.Langfuse({
    publicKey,
    secretKey,
    ...(baseUrl ? { baseUrl } : {}),
  });
  return new LangfuseRegistryTransport(client);
}
