/**
 * Out-of-the-box vendor providers (Python parity): when the spec declares
 * `runtime.provider.type: openai`/`anthropic`/`gemini`, `buildRuntime` wires
 * the matching thin transport over the OFFICIAL vendor SDK (user directive:
 * official SDK) — no transport code in the caller, exactly like Python's
 * `_build_provider` constructing each provider from the environment. Each SDK
 * is an OPTIONAL peer dependency loaded lazily (the TS mirror of Python's
 * `[openai]`/`[anthropic]`/`[gemini]` extras): specs that never select a type
 * never load its SDK; specs that do fail loudly when it is missing.
 *
 * Credentials resolve in Python `_build_provider`'s order: the spec's
 * `runtime.provider.api_key` (a literal, or `value_from: {env|file}` — the
 * `resolve_optional_secret_text` rules, incl. the required-but-unset throw)
 * wins; otherwise the vendor's standard variable — `OPENAI_API_KEY`,
 * `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` falling back to `GOOGLE_API_KEY`
 * (Python's exact `or` chain). `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` are
 * honored the way Python's vendor SDKs read them when no `base_url` is passed.
 * UNLIKE observability — which degrades to an untraced run — an absent key
 * THROWS: the provider is load-bearing, a run cannot degrade past it, so a
 * silent fallback would only defer the failure to a cryptic mid-run 401.
 *
 * The transports here are deliberately THIN one-line adapters (the exact
 * shapes each provider's module doc prescribes): message/schema mapping,
 * error classification, and usage reporting all live in the PROVIDER classes
 * (`OpenAIProvider`/`AnthropicProvider`/`GeminiProvider`), which already
 * speak each vendor's wire format. Custom endpoints keep the injected seam:
 * pass your own `provider` or `transports` and this module never runs.
 */

import { resolveOptionalSecretText } from "./secret-references.js";

import type {
  AnthropicMessagesRequest,
  AnthropicMessagesResponse,
  AnthropicMessagesTransport,
  ChatCompletionRequest,
  ChatCompletionResponse,
  GeminiFile,
  GeminiGenerateContentResponse,
  GeminiGenerateContentTransport,
  OpenAIChatTransport,
  TransportCallOptions,
} from "@typeflux/temporal";

import type { ProviderTransports } from "./provider-from-spec.js";
import type { TypefluxYamlSpec } from "./spec.js";

/** The per-request options the OpenAI/Anthropic SDKs share: abort signal + ms timeout. */
interface VendorRequestOptions {
  signal?: AbortSignal;
  timeout?: number;
}

/** The slice of the official `openai` client this module drives (structural — tests fake it). */
export interface OpenAiSdkClient {
  chat: {
    completions: {
      /** `never` params: SDK methods are CONTRAVARIANT in their request type,
       * so the official client's narrower overloads would not be assignable
       * to a typed signature — the adapter constructs the request and casts
       * the call (codex). */
      create(request: never, options?: never): Promise<unknown>;
    };
  };
}

/** The slice of the official `@anthropic-ai/sdk` client this module drives (structural — tests fake it). */
export interface AnthropicSdkClient {
  messages: {
    /** `never` params — see OpenAiSdkClient. */
    create(request: never, options?: never): Promise<unknown>;
  };
}

/**
 * The slice of the official `@google/genai` client this module drives (structural — tests
 * fake it). `generateContent` takes the SDK's single-params shape (request fields + a flat
 * `config`); `files`/`caches` power oversize artifacts (#503) and reference-style session
 * caching (#478).
 */
export interface GoogleGenAiSdkClient {
  models: {
    /** `never` params — see OpenAiSdkClient. */
    generateContent(params: never): Promise<unknown>;
  };
  files: {
    upload(params: never): Promise<unknown>;
    get(params: never): Promise<unknown>;
  };
  caches: {
    create(params: never): Promise<unknown>;
    delete(params: never): Promise<unknown>;
  };
}

/** Map the engine's `TransportCallOptions` onto the OpenAI/Anthropic SDKs' request options. */
function vendorRequestOptions(options: TransportCallOptions | undefined): VendorRequestOptions | undefined {
  if (options === undefined) {
    return undefined;
  }
  return {
    ...(options.signal !== undefined ? { signal: options.signal } : {}),
    ...(options.timeoutMs !== undefined ? { timeout: options.timeoutMs } : {}),
  };
}

/**
 * Adapt an official `openai` client to the `OpenAIChatTransport` seam — the exact
 * one-line adapter `openai-provider.ts`'s module doc prescribes. The provider owns
 * the wire format; this only forwards signal/timeout.
 */
export function openaiTransportFromSdk(client: OpenAiSdkClient): OpenAIChatTransport {
  return {
    chat: {
      completions: {
        create: (request, options) =>
          client.chat.completions.create(request as never, vendorRequestOptions(options) as never) as Promise<ChatCompletionResponse>,
      },
    },
  };
}

/**
 * Adapt an official `@anthropic-ai/sdk` client to the `AnthropicMessagesTransport`
 * seam — the exact one-line adapter `anthropic-provider.ts`'s module doc prescribes.
 */
export function anthropicTransportFromSdk(client: AnthropicSdkClient): AnthropicMessagesTransport {
  return {
    messages: {
      create: (request, options) =>
        client.messages.create(request as never, vendorRequestOptions(options) as never) as Promise<AnthropicMessagesResponse>,
    },
  };
}

/**
 * Adapt an official `@google/genai` client to the `GeminiGenerateContentTransport`
 * seam — the exact adapter `gemini-provider.ts`'s module doc prescribes:
 * `systemInstruction`/`cachedContent` + the generationConfig flatten into the SDK's
 * single `config` (forwarding `cachedContent` is what keeps session-cached calls
 * (#478) referencing their prefix), abort/timeout ride `abortSignal`/`httpOptions`,
 * and the `files` (#503) + `caches` (#478) surfaces are wired so oversize artifacts
 * and reference-style session caching work out of the box.
 */
export function geminiTransportFromSdk(client: GoogleGenAiSdkClient): GeminiGenerateContentTransport {
  return {
    generateContent: ({ model, contents, systemInstruction, cachedContent, generationConfig }, options) =>
      client.models.generateContent({
        model,
        contents,
        config: {
          ...(systemInstruction !== undefined ? { systemInstruction } : {}),
          ...(cachedContent !== undefined ? { cachedContent } : {}),
          ...generationConfig,
          ...(options?.signal !== undefined ? { abortSignal: options.signal } : {}),
          ...(options?.timeoutMs !== undefined ? { httpOptions: { timeout: options.timeoutMs } } : {}),
        },
      } as never) as Promise<GeminiGenerateContentResponse>,
    files: {
      upload: (params) => client.files.upload(params as never) as Promise<GeminiFile>,
      get: (params) => client.files.get(params as never) as Promise<GeminiFile>,
    },
    caches: {
      create: (params) => client.caches.create(params as never) as Promise<{ name?: string }>,
      delete: (params) => client.caches.delete(params as never),
    },
  };
}

/**
 * Resolve the spec's `runtime.provider.api_key` slot to a credential, or undefined
 * when the slot is absent/optional-and-unset (Python `resolve_optional_secret_text`
 * with its exact error texts): a literal passes through ("" is absent); a
 * `value_from.env` reads the given ENVIRONMENT and trims; a `value_from.file` reads
 * the file and trims. A `required` reference (the default) that resolves to
 * nothing THROWS — a declared credential slot must not silently fall through to a
 * different variable.
 */
export function resolveProviderApiKey(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): string | undefined {
  // Delegates to THE one string|value_from resolution (#793 extraction) — behavior
  // unchanged: literals un-trimmed, env/file trimmed, required-but-unset throws.
  return resolveOptionalSecretText(spec.runtime.provider.api_key, "runtime.provider.api_key", environment);
}

/** The missing-credential throw, uniform across vendors: the run cannot degrade past its provider. */
function missingKeyError(type: string, envNames: string): Error {
  return new Error(
    `runtime.provider.type is ${type} but ${envNames} is not set — the provider is REQUIRED ` +
      "(a run cannot degrade past its provider). Export the key (or set " +
      "runtime.provider.api_key), or inject `provider`/`transports` yourself.",
  );
}

/** The missing-peer throw, uniform across vendors: name the package and the install command. */
function missingSdkError(type: string, packageName: string, extra: string, cause: unknown): Error {
  return new Error(
    `runtime.provider.type is ${type} but the ${packageName} SDK is not installed — ` +
      `add it to your app (npm/pnpm add ${packageName}). It is an optional peer dependency, ` +
      `loaded only when a spec builds the ${type} provider from the environment ` +
      `(Python's [${extra}] extra).`,
    { cause },
  );
}

/**
 * Build the out-of-the-box transports for the spec's `runtime.provider.type`.
 * Returns undefined for any other type (an injected `provider` is the seam for
 * custom providers — `assembleYamlRuntime` fails loud when neither resolves).
 * THROWS — does not degrade — when the credential is absent or when the spec
 * selects a vendor whose optional SDK peer is not installed.
 */
export async function providerTransportsFromSpec(
  spec: TypefluxYamlSpec,
  environment: Record<string, string | undefined> = process.env,
): Promise<ProviderTransports | undefined> {
  const type = spec.runtime.provider.type;
  switch (type) {
    case "openai": {
      // The spec's api_key slot wins over the vendor's standard variable
      // (Python `_build_provider`); resolved INSIDE the vendor gate so
      // non-vendor types stay inert — the injected-provider seam (finder).
      // Truthy `||` (Python SDK `os.environ.get` posture + the ported
      // truthiness trap): an interpolated empty env var is no credential.
      const specKey = resolveProviderApiKey(spec, environment);
      const apiKey = specKey ?? (environment["OPENAI_API_KEY"] || undefined);
      if (apiKey === undefined) {
        throw missingKeyError(type, "OPENAI_API_KEY");
      }
      let sdk: { default: new (options: Record<string, unknown>) => OpenAiSdkClient };
      try {
        sdk = (await import("openai")) as unknown as typeof sdk;
      } catch (error) {
        throw missingSdkError(type, "openai", "openai", error);
      }
      // The base-url env the official SDK itself reads when no base_url is passed
      // (Python parity: `_build_provider` passes base_url=None and the vendor SDK
      // resolves it) — read from THIS call's environment so tests see one source.
      // ALWAYS passed: an omitted baseURL makes the SDK read process.env
      // directly (readEnv in its constructor), bypassing the injected
      // environment — a scoped map must be authoritative (finder).
      const baseUrl = environment["OPENAI_BASE_URL"] || "https://api.openai.com/v1";
      const client = new sdk.default({ apiKey, baseURL: baseUrl });
      return { openai: openaiTransportFromSdk(client) };
    }
    case "anthropic": {
      const specKey = resolveProviderApiKey(spec, environment);
      const apiKey = specKey ?? (environment["ANTHROPIC_API_KEY"] || undefined);
      if (apiKey === undefined) {
        throw missingKeyError(type, "ANTHROPIC_API_KEY");
      }
      let sdk: { default: new (options: Record<string, unknown>) => AnthropicSdkClient };
      try {
        sdk = (await import("@anthropic-ai/sdk")) as unknown as typeof sdk;
      } catch (error) {
        throw missingSdkError(type, "@anthropic-ai/sdk", "anthropic", error);
      }
      // ALWAYS passed — same injected-environment authority as openai.
      const baseUrl = environment["ANTHROPIC_BASE_URL"] || "https://api.anthropic.com";
      const client = new sdk.default({ apiKey, baseURL: baseUrl });
      return { anthropic: anthropicTransportFromSdk(client) };
    }
    case "gemini": {
      // Python `_build_client`: `GEMINI_API_KEY or GOOGLE_API_KEY` — truthy `||`,
      // GEMINI wins. (The Vertex path doesn't exist here: the TS spec rejects
      // `runtime.provider.vertex` — ADC clients are injected transports.)
      const specKey = resolveProviderApiKey(spec, environment);
      const apiKey = specKey ?? (environment["GEMINI_API_KEY"] || environment["GOOGLE_API_KEY"] || undefined);
      if (apiKey === undefined) {
        throw missingKeyError(type, "GEMINI_API_KEY/GOOGLE_API_KEY");
      }
      let sdk: { GoogleGenAI: new (options: Record<string, unknown>) => GoogleGenAiSdkClient };
      try {
        sdk = (await import("@google/genai")) as unknown as typeof sdk;
      } catch (error) {
        throw missingSdkError(type, "@google/genai", "gemini", error);
      }
      const client = new sdk.GoogleGenAI({ apiKey });
      return { gemini: geminiTransportFromSdk(client) };
    }
    default:
      return undefined;
  }
}
