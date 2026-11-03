/**
 * Out-of-the-box LangSmith prompt registry (Python parity, mirroring
 * `langfuse-registry.ts`): when the spec declares `runtime.registry.type:
 * langsmith`, `buildRuntime` wires a `RegistryTransport` over the OFFICIAL
 * `langsmith` SDK (user directive: official SDK) — no transport code in the
 * caller, like Python's `_build_registry` constructing a
 * `LangSmithPromptRegistry`. The SDK is an OPTIONAL peer dependency loaded
 * lazily (the TS mirror of Python's `[langsmith]` extra), shared with
 * `langsmith-observer.ts`.
 *
 * LangSmith stores prompts as LangChain serializations, so this module parses
 * the commit MANIFEST (a plain JSON dict — no langchain runtime needed) into
 * Typeflux `ChatMessage` templates, preserving roles (the faithful port of
 * Python `prompts/langsmith.py`). Each message's template is normalized to
 * Typeflux's mustache `{{var}}` syntax: `mustache` templates pass through,
 * `f-string` `{var}` templates are converted (honoring `{{`/`}}`
 * literal-brace escapes). Selection is by LangSmith tag/commit via the ref's
 * `label` (e.g. `production` or a commit hash); the resolved commit hash is
 * the `resolvedVersion` so it lands in the manifests.
 *
 * An absent `LANGSMITH_API_KEY` THROWS (prompts are load-bearing — the
 * registry cannot degrade the way tracing does). Custom backends keep the
 * injected seam: pass your own `registry`/`registryTransport` and this module
 * never runs.
 */

import {
  type ChatMessage,
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

/** The slice of the SDK's `PromptCommit` this module reads (structural — tests fake it). */
export interface LangsmithPromptCommit {
  commit_hash?: unknown;
  manifest?: unknown;
}

/** The slice of the official SDK's client this module drives (structural — tests fake it). */
export interface LangsmithRegistrySdkClient {
  pullPromptCommit(promptIdentifier: string): Promise<LangsmithPromptCommit>;
}

const ROLE_BY_MESSAGE_CLASS: Record<string, "system" | "user" | "assistant"> = {
  SystemMessagePromptTemplate: "system",
  HumanMessagePromptTemplate: "user",
  AIMessagePromptTemplate: "assistant",
};
const PASSTHROUGH_FORMATS = new Set(["mustache"]);
const CONVERT_FORMATS = new Set(["f-string", "fstring"]);

/** Python `repr()` for error texts: `None` for null-ish, single-quoted otherwise. */
function pyRepr(value: unknown): string {
  return value == null ? "None" : `'${String(value)}'`;
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

/**
 * Python `_prompt_identifier`: LangSmith selects by tag or commit hash (a
 * string), carried on the ref's `label`; an integer `version` is accepted as
 * a string selector too. Neither → the bare name (the SDK pulls `latest`).
 */
export function langsmithPromptIdentifier(ref: PromptRef): string {
  const sel = ref.label != null ? ref.label : ref.version != null ? String(ref.version) : undefined;
  // Truthy guard (Python `if selector`): an empty-string label is no selector.
  return sel ? `${ref.name}:${sel}` : ref.name;
}

/** Python `_manifest_kind`: the LangChain class name is the last element of the manifest `id`. */
function manifestKind(manifest: Record<string, unknown>): string | undefined {
  const identifier = manifest["id"];
  return Array.isArray(identifier) && identifier.length > 0
    ? String(identifier[identifier.length - 1])
    : undefined;
}

/**
 * Python `_mapping`: manifest fields are external SDK-shaped data — a
 * present-but-wrong-shape value must degrade to a clean config error
 * downstream, not an opaque property access on undefined.
 */
function mapping(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

// Python `_FSTRING_VARIABLE` / `_TYPEFLUX_IDENTIFIER`: Typeflux's renderer only
// substitutes plain dotted identifiers (mirrors core render's mustache
// variable pattern); anything else would pass through as un-substituted
// literal text.
const FSTRING_VARIABLE = /\{([^{}]+)\}/g;
const TYPEFLUX_IDENTIFIER = /^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/;

/**
 * Translate Python f-string `{var}` placeholders to mustache `{{var}}`
 * (Python `_fstring_to_mustache`). In f-string format `{{`/`}}` are literal
 * braces and `{name}` is a variable; map variables to `{{name}}` while
 * preserving literal braces. A variable carrying a format spec or conversion
 * (`{x:.2f}`, `{x!r}`) cannot be rendered by Typeflux (it substitutes raw
 * field values, not Python formatting), so it is rejected loud rather than
 * silently passed through as literal text.
 */
function fstringToMustache(template: string, ref: PromptRef): string {
  const openMarker = "\u0000";
  const closeMarker = "\u0001";
  const staged = template.replaceAll("{{", openMarker).replaceAll("}}", closeMarker);
  const converted = staged.replace(FSTRING_VARIABLE, (match, group: string) => {
    const name = group.trim();
    if (!TYPEFLUX_IDENTIFIER.test(name)) {
      throw new PromptRegistryConfigError(
        `LangSmith prompt ${pyRepr(ref.name)} has an f-string placeholder ${pyRepr(match)} ` +
          "that is not a plain {{name}} Typeflux can render; pre-format it in the prompt " +
          "or store the prompt in mustache format",
        { ref },
      );
    }
    return `{{${name}}}`;
  });
  return converted.replaceAll(openMarker, "{").replaceAll(closeMarker, "}");
}

/** Python `_to_mustache`: pass mustache through, convert f-string, reject the rest loud. */
function toMustache(template: string, templateFormat: unknown, ref: PromptRef): string {
  const normalized = (typeof templateFormat === "string" ? templateFormat : "f-string").toLowerCase();
  if (PASSTHROUGH_FORMATS.has(normalized)) {
    return template;
  }
  if (CONVERT_FORMATS.has(normalized)) {
    return fstringToMustache(template, ref);
  }
  throw new PromptRegistryConfigError(
    `LangSmith prompt ${pyRepr(ref.name)} uses unsupported template_format ` +
      `${pyRepr(templateFormat)}; Typeflux supports 'mustache' and 'f-string'`,
    { ref },
  );
}

/** Python `_template_text`: the template string, normalized to mustache. */
function templateText(templateKwargs: Record<string, unknown>, ref: PromptRef): string {
  const template = templateKwargs["template"];
  if (typeof template !== "string") {
    throw new PromptRegistryConfigError(
      `LangSmith prompt ${pyRepr(ref.name)} message has no template text`,
      { ref },
    );
  }
  return toMustache(template, templateKwargs["template_format"] ?? "f-string", ref);
}

/** Python `_chat_message_from_manifest`: role by LangChain message class, template as content. */
function chatMessageFromManifest(rawMessage: unknown, ref: PromptRef): ChatMessage {
  const message = mapping(rawMessage);
  const identifier = message["id"];
  const messageClass =
    Array.isArray(identifier) && identifier.length > 0
      ? String(identifier[identifier.length - 1])
      : undefined;
  const role = ROLE_BY_MESSAGE_CLASS[messageClass ?? ""];
  if (role === undefined) {
    throw new PromptRegistryConfigError(
      `unsupported LangSmith chat message ${pyRepr(messageClass)} for ${pyRepr(ref.name)}`,
      { ref },
    );
  }
  const promptKwargs = mapping(mapping(mapping(message["kwargs"])["prompt"])["kwargs"]);
  return { role, content: templateText(promptKwargs, ref) };
}

/**
 * Python `_messages_from_manifest`: `PromptTemplate` → one user message;
 * `ChatPromptTemplate` → the message list (empty is a config error);
 * model-bound kinds (`RunnableSequence`/`RunnableBinding`) are rejected —
 * Typeflux owns provider selection — and anything else fails loud.
 */
export function messagesFromLangsmithManifest(
  manifest: Record<string, unknown>,
  ref: PromptRef,
): ChatMessage[] {
  const kind = manifestKind(manifest);
  const kwargs = mapping(manifest["kwargs"]);
  if (kind === "PromptTemplate") {
    return [{ role: "user", content: templateText(kwargs, ref) }];
  }
  if (kind === "ChatPromptTemplate") {
    const rawMessages = Array.isArray(kwargs["messages"]) ? (kwargs["messages"] as unknown[]) : [];
    const messages = rawMessages.map((message) => chatMessageFromManifest(message, ref));
    if (messages.length === 0) {
      throw new PromptRegistryConfigError(
        `LangSmith prompt ${pyRepr(ref.name)} contained no messages`,
        { ref },
      );
    }
    return messages;
  }
  if (kind === "RunnableSequence" || kind === "RunnableBinding") {
    throw new PromptRegistryConfigError(
      `LangSmith prompt ${pyRepr(ref.name)} is bound to a model (${kind}); Typeflux owns ` +
        "provider selection, so store the prompt without a model binding",
      { ref },
    );
  }
  throw new PromptRegistryConfigError(
    `unsupported LangSmith prompt type ${pyRepr(kind)} for ${pyRepr(ref.name)}`,
    { ref },
  );
}

/** Python `providers/_shared.status_code`: a numeric status from the SDK error, best-effort. */
function statusCode(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null) {
    return undefined;
  }
  const record = error as Record<string, unknown>;
  for (const attr of ["status_code", "status", "code"]) {
    if (typeof record[attr] === "number") {
      return record[attr];
    }
  }
  const response = record["response"];
  const fromResponse =
    typeof response === "object" && response !== null
      ? (response as Record<string, unknown>)["status"]
      : undefined;
  return typeof fromResponse === "number" ? fromResponse : undefined;
}

/**
 * Classify an SDK failure into the typed hierarchy (Python
 * `_classify_langsmith_prompt_error`): name tokens first (the JS SDK throws
 * `LangSmithNotFoundError`, `LangSmithAuthError`, `LangSmithRateLimitError`,
 * `LangSmithConflictError`, …), then status codes, then the network/timeout
 * heuristic — plain fetch failures carry none of the tokens but are transient
 * and worth retrying.
 */
function classifyLangsmithPromptError(error: unknown, ref: PromptRef): PromptResolutionError {
  const code = statusCode(error);
  const name = error instanceof Error ? error.name : "";
  const message = error instanceof Error ? error.message : String(error);
  const reason = `failed to resolve LangSmith prompt ${ref.name}@${selector(ref)}`;
  const options = { ref, ...(code !== undefined ? { statusCode: code } : {}), cause: error };
  if (name.includes("NotFound") || code === 404) {
    return new PromptNotFoundError(ref, { reason, ...(code !== undefined ? { statusCode: code } : {}), cause: error });
  }
  if (["Auth", "Unauthorized", "Forbidden"].some((token) => name.includes(token)) || code === 401 || code === 403) {
    return new PromptRegistryAuthError(reason, options);
  }
  if (
    ["RateLimit", "Connection", "Timeout", "Unavailable"].some((token) => name.includes(token)) ||
    code === 408 ||
    code === 429 ||
    (code !== undefined && code >= 500) ||
    // Plain fetch/socket failures (TypeError: fetch failed, ECONNREFUSED, …)
    // carry none of the tokens above but are transient (Python's
    // `is_network_or_timeout_error` OSError branch).
    /fetch failed|network|ECONN|ETIMEDOUT|EAI_AGAIN|socket/i.test(message)
  ) {
    return new PromptRegistryUnavailableError(reason, options);
  }
  if (code === 400 || code === 405 || code === 422) {
    return new PromptRegistryConfigError(reason, options);
  }
  return new PromptResolutionError(reason, { ...options, retryable: false });
}

/**
 * Adapt the official SDK's `pullPromptCommit` to the `RegistryTransport` seam
 * (Python `LangSmithPromptRegistry.resolve`): pull the commit by the ref's
 * `name:selector` identifier, parse the manifest into Typeflux messages, and
 * record the commit hash as the resolved version. LangSmith prompts carry no
 * Typeflux model/temperature config (Python parity — a model binding is
 * rejected outright), so only `messages` + `resolvedVersion` ride the RawPrompt.
 */
export class LangsmithRegistryTransport implements RegistryTransport {
  constructor(private readonly client: LangsmithRegistrySdkClient) {}

  async fetchPrompt(ref: PromptRef): Promise<RawPrompt> {
    let commit: LangsmithPromptCommit;
    try {
      commit = await this.client.pullPromptCommit(langsmithPromptIdentifier(ref));
    } catch (error) {
      throw classifyLangsmithPromptError(error, ref);
    }
    const manifest = commit.manifest;
    if (typeof manifest !== "object" || manifest === null || Array.isArray(manifest)) {
      throw new PromptRegistryConfigError(
        `LangSmith prompt ${pyRepr(ref.name)} returned no manifest`,
        { ref },
      );
    }
    const messages = messagesFromLangsmithManifest(manifest as Record<string, unknown>, ref);
    // Truthy guard (Python `str(commit_hash) if commit_hash else None`).
    const commitHash = commit.commit_hash;
    return {
      messages,
      resolvedVersion: commitHash ? String(commitHash) : null,
    };
  }
}

/**
 * Build the out-of-the-box registry transport for a `type: langsmith` spec.
 * Returns undefined for every other type (inline self-builds in
 * `registryFromSpec`; langfuse has its own module; custom keeps the injected
 * seam). THROWS — does not degrade — when `LANGSMITH_API_KEY` is absent (a
 * run cannot proceed without its prompts) or when the spec opts in but the
 * optional `langsmith` peer is not installed.
 */
export async function langsmithRegistryTransportFromSpec(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): Promise<RegistryTransport | undefined> {
  if (spec.runtime.registry.type !== "langsmith") {
    return undefined;
  }
  const apiKey = environment["LANGSMITH_API_KEY"];
  if (!apiKey) {
    throw new Error(
      "runtime.registry.type is langsmith but LANGSMITH_API_KEY is not set — prompts are REQUIRED " +
        "(unlike tracing, a run cannot degrade past its registry). Export it (plus LANGSMITH_HOST " +
        "for self-hosted), or inject `registry`/`registryTransport` yourself.",
    );
  }
  let sdk: { Client: new (options: Record<string, unknown>) => LangsmithRegistrySdkClient };
  try {
    sdk = (await import("langsmith")) as unknown as typeof sdk;
  } catch (error) {
    throw new Error(
      "runtime.registry.type is langsmith but the langsmith SDK is not installed — " +
        "add it to your app (npm/pnpm add langsmith). It is an optional peer dependency, " +
        "loaded only when a spec opts into the langsmith prompt registry (Python's [langsmith] extra).",
      { cause: error },
    );
  }
  // Python `_langsmith_host`: the spec's `registry.host` wins, then
  // LANGSMITH_HOST, then LANGCHAIN_ENDPOINT — the REGISTRY env contract
  // (docs + the governance host-allowlist key on these; LANGSMITH_ENDPOINT
  // is the OBSERVABILITY contract, finder). Truthy `||` — an interpolated
  // empty host is no override (the ported truthiness trap).
  const apiUrl =
    spec.runtime.registry.host ||
    environment["LANGSMITH_HOST"] ||
    environment["LANGCHAIN_ENDPOINT"];
  const client = new sdk.Client({
    apiKey,
    ...(apiUrl ? { apiUrl } : {}),
  });
  return new LangsmithRegistryTransport(client);
}
