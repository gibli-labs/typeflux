/**
 * Build a `ModelProvider` from `spec.runtime.provider` (parity Epic 2/5; Python
 * `yaml/runtime.py` `_build_provider`). The TS providers take INJECTED thin
 * transports (no vendor SDK deps), so the caller supplies a `transports` map keyed by
 * provider type and this picks the matching one. `model` defaults to the per-provider
 * default when the spec omits it.
 */

import {
  AnthropicProvider,
  type AnthropicMessagesTransport,
  GeminiProvider,
  type GeminiGenerateContentTransport,
  type ModelProvider,
  OpenAIProvider,
  type OpenAIChatTransport,
} from "@typeflux/temporal";

import type { TypefluxYamlSpec } from "./spec.js";

/** Per-provider default models (parity with the Python `DEFAULT_*_MODEL` constants). */
const DEFAULT_MODELS = {
  openai: "gpt-4o-mini",
  anthropic: "claude-sonnet-4-6",
  gemini: "gemini-2.5-flash",
} as const;

/**
 * The default model a spec-built provider of `type` would use (undefined for
 * custom/unknown types — an injected provider's default is its own business).
 * Exposed so the runtime can fold the EFFECTIVE model into cache keys even when
 * the spec pins none (#495): otherwise a future default-model bump would serve
 * stale cross-run cache entries recorded under the old implicit model.
 */
export function defaultModelForProviderType(type: string): string | undefined {
  return type in DEFAULT_MODELS ? DEFAULT_MODELS[type as keyof typeof DEFAULT_MODELS] : undefined;
}

/**
 * Engine defaults for `runtime.provider_retry` (Python `ProviderRetryPolicy()` in
 * `execution/controls.py`). The zod spec deliberately leaves these fields absent-aware —
 * a schema `.default()` would make a schema-defaulted value indistinguishable from a
 * configured one (the spec-default-parity trap) — so every consumption site falls back
 * to THIS one constant: the runtime's backoff wiring and the control-plane bundle's
 * `runtime_effective` source classification cannot drift. `max_backoff_seconds: null`
 * is the explicit no-cap (Python `None`).
 */
export const ENGINE_PROVIDER_RETRY_DEFAULTS = {
  max_attempts: 1,
  initial_backoff_seconds: 0,
  max_backoff_seconds: null,
  backoff_multiplier: 2,
  jitter_ratio: 0.1,
  retry_rate_limits: true,
} as const;

/** The injected thin transports, keyed by provider type — supply the one(s) your spec uses. */
export interface ProviderTransports {
  openai?: OpenAIChatTransport;
  anthropic?: AnthropicMessagesTransport;
  gemini?: GeminiGenerateContentTransport;
}

/**
 * Construct the `ModelProvider` the spec's `runtime.provider.type` selects, wired to the
 * matching injected transport. Throws if the type has no provided transport or is
 * unsupported (openai / anthropic / gemini).
 */
export function providerFromSpec(spec: TypefluxYamlSpec, transports: ProviderTransports): ModelProvider {
  const providerSpec = spec.runtime.provider;
  const type = providerSpec.type;

  // `|| DEFAULT` (truthy), not `?? DEFAULT`: an empty-string model (e.g. from a
  // `model: ${MODEL:-}` env default) must fall back to the built-in default rather than
  // sending `model: ""` to the vendor — parity with Python's `provider.model or DEFAULT`.
  switch (type) {
    case "openai": {
      const transport = requireTransport(transports.openai, type);
      return new OpenAIProvider(transport, { model: providerSpec.model || DEFAULT_MODELS.openai });
    }
    case "anthropic": {
      const transport = requireTransport(transports.anthropic, type);
      return new AnthropicProvider(transport, { model: providerSpec.model || DEFAULT_MODELS.anthropic });
    }
    case "gemini": {
      const transport = requireTransport(transports.gemini, type);
      return new GeminiProvider(transport, { model: providerSpec.model || DEFAULT_MODELS.gemini });
    }
    default:
      throw new Error(
        `providerFromSpec: unsupported provider type ${JSON.stringify(type)} ` +
          `(supported: openai, anthropic, gemini)`,
      );
  }
}

function requireTransport<T>(transport: T | undefined, type: string): T {
  if (transport === undefined) {
    throw new Error(
      `providerFromSpec: the spec uses provider type ${JSON.stringify(type)} but no ` +
        `${JSON.stringify(type)} transport was provided in \`transports\``,
    );
  }
  return transport;
}
