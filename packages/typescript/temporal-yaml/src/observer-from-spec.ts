/**
 * Build a trace observer from `spec.runtime.observability` (parity Epic 4/5; Python
 * `yaml/runtime.py` `_build_observability`). Mirrors `providerFromSpec`: the caller
 * wires a real backend `TraceTransport` (Langfuse/LangSmith/OTel/HTTP — no SDK deps),
 * and this returns a `TraceWriter` over it when the spec opts into a tracing backend.
 */

import { type ActivityObserver, type RedactionConfig, type TraceTransport, TraceWriter } from "@typeflux/temporal";

import type { TypefluxYamlSpec } from "./spec.js";

/** Observability backend types that emit traces (vs `none`, which records nothing). */
const TRACING_TYPES = new Set(["langfuse", "langsmith", "custom"]);

/** Map the spec's `runtime.observability.redaction` (`RedactionSpec`) to a `RedactionConfig`. */
export function redactionFromSpec(spec: TypefluxYamlSpec): RedactionConfig | undefined {
  const redaction = spec.runtime.observability?.redaction;
  if (redaction === undefined) {
    return undefined;
  }
  const config: RedactionConfig = {};
  if (redaction.enabled !== undefined) config.enabled = redaction.enabled;
  if (redaction.emails !== undefined) config.emails = redaction.emails;
  if (redaction.phones !== undefined) config.phones = redaction.phones;
  if (redaction.ssn !== undefined) config.ssn = redaction.ssn;
  if (redaction.credit_cards !== undefined) config.creditCards = redaction.credit_cards;
  if (redaction.preserve_typeflux_metadata !== undefined) {
    config.preserveTypefluxMetadata = redaction.preserve_typeflux_metadata;
  }
  if (redaction.exclude_paths !== undefined) config.excludePaths = redaction.exclude_paths;
  if (redaction.custom_rules !== undefined) {
    // Patterns are validated (compiled) at spec load in `redactionRuleSpec`, so passing
    // them straight through as `{name, pattern, replacement}` is safe; `buildRules`
    // recompiles each with the global flag before applying.
    config.customRules = redaction.custom_rules.map((rule) => ({
      name: rule.name,
      pattern: rule.pattern,
      replacement: rule.replacement,
    }));
  }
  return config;
}

/**
 * Return a `TraceWriter(transport)` when `spec.runtime.observability.type` opts into a
 * tracing backend, else `undefined` (type `none` / unset → no observer). Pass the result
 * as `buildRuntime`'s `observer`, and `await writer.flush()` when the worker drains.
 */
export function observerFromSpec(spec: TypefluxYamlSpec, transport: TraceTransport): ActivityObserver | undefined {
  const type = spec.runtime.observability?.type;
  if (type === undefined || type === "none") {
    return undefined; // no observability requested
  }
  if (!TRACING_TYPES.has(type)) {
    // A misspelled/unsupported type must fail loudly rather than silently disable the
    // requested tracing (parity with providerFromSpec's unknown-type rejection).
    throw new Error(
      `observerFromSpec: unsupported observability type ${JSON.stringify(type)} ` +
        `(supported: none, langfuse, langsmith, custom)`,
    );
  }
  // Redaction defaults to ON for a tracing backend (parity with Python's `RedactionSpec`
  // default), so omitting a `redaction` block does NOT leak raw PII to the backend.
  return new TraceWriter(transport, redactionFromSpec(spec) ?? {});
}
