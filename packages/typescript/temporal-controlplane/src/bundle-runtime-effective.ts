/**
 * Bundle RUNTIME_EFFECTIVE (#575; Python `project/bundle.py` `_bundle_runtime_effective`):
 * per-knob effective values tagged with WHERE each came from, so operators see what an
 * unset knob actually resolves to. Sources, in classification order:
 *
 *  - `project_default` — the effective value equals a value set at this dotted path in
 *    `project.defaults.runtime` (the project layer supplied it). A higher-precedence layer
 *    (workflow YAML, profile, environment) that overrode it shows as `configured`.
 *  - `engine_default` — the effective value equals the engine's own constant.
 *  - `configured` — anything else: the workflow/profile/environment set it.
 *
 * Knobs mirror Python exactly: `provider.model` (raw spec value vs the per-type default),
 * the six `provider_retry.*` fields (vs `ENGINE_PROVIDER_RETRY_DEFAULTS` — the same
 * constant the runtime's backoff wiring consumes, so classification cannot drift from
 * behavior), `registry.label` for langfuse/langsmith registries (default `production`),
 * and `lifecycle.history.status_event_limit` when the lifecycle is enabled (default 50).
 *
 * A `null` effective value (unset model on a defaultless provider type; uncapped
 * `max_backoff_seconds`) omits the `value` key — Python's `exclude_none` serialization.
 */

import {
  defaultModelForProviderType,
  effectiveModelFor,
  ENGINE_PROVIDER_RETRY_DEFAULTS,
  type TypefluxProjectSpec,
  type TypefluxYamlSpec,
} from "@typeflux/temporal-yaml";

/** Python `BundleRuntimeEffective`; `value` omitted when null (exclude_none parity). */
export interface ApiBundleRuntimeEffective {
  path: string;
  value?: unknown;
  source: "engine_default" | "project_default" | "configured";
}

const MISSING = Symbol("missing");

/** Python `_path_get`: walk a dotted path through nested plain objects; MISSING when absent. */
function pathGet(data: unknown, path: string): unknown {
  let node = data;
  for (const segment of path.split(".")) {
    if (typeof node !== "object" || node === null || Array.isArray(node) || !Object.hasOwn(node, segment)) {
      return MISSING;
    }
    node = (node as Record<string, unknown>)[segment];
  }
  return node;
}

/** Build the bundle's `runtime_effective` (always-present array; Python `()` default). */
export function buildBundleRuntimeEffective(
  spec: TypefluxYamlSpec,
  project: TypefluxProjectSpec,
): ApiBundleRuntimeEffective[] {
  const defaults = project.defaults.runtime;
  // (path, effective, engine_default) — effective/engine use null (not undefined) for
  // "no value", matching Python None, so the equality checks below compare like Python.
  const entries: [string, unknown, unknown][] = [];

  const provider = spec.runtime.provider;
  // Python materializes provider.model at PARSE time (params.model, else the per-type default —
  // ProviderSpec._materialize_default_model); the TS spec keeps the raw field absent-aware and
  // derives at consumption, so mirror the materialization here via effectiveModelFor. Null only
  // for a defaultless type (fake/custom) with no model anywhere — Python emits the same.
  entries.push(["provider.model", effectiveModelFor(provider) ?? null, defaultModelForProviderType(provider.type) ?? null]);

  const retry = spec.runtime.provider_retry;
  const engineRetry = ENGINE_PROVIDER_RETRY_DEFAULTS;
  for (const field of [
    "max_attempts",
    "initial_backoff_seconds",
    "max_backoff_seconds",
    "backoff_multiplier",
    "jitter_ratio",
    "retry_rate_limits",
  ] as const) {
    const engineValue = engineRetry[field];
    // The zod spec keeps these absent-aware (no schema defaults — the parity trap), so an
    // unset field resolves to the engine value here exactly like Python's Pydantic defaults.
    const effective = retry?.[field] ?? engineValue;
    entries.push([`provider_retry.${field}`, effective, engineValue]);
  }

  if (spec.runtime.registry.type === "langfuse" || spec.runtime.registry.type === "langsmith") {
    const label = spec.runtime.registry.label ?? "production";
    entries.push(["registry.label", label === "" ? "production" : label, "production"]);
  }

  const lifecycle = spec.workflow.lifecycle;
  if (lifecycle !== undefined && lifecycle.enabled) {
    entries.push(["lifecycle.history.status_event_limit", lifecycle.history.status_event_limit, 50]);
  }

  return entries.map(([path, effective, engineDefault]) => {
    const projectValue = pathGet(defaults, path);
    const source: ApiBundleRuntimeEffective["source"] =
      projectValue !== MISSING && effective === projectValue
        ? "project_default"
        : effective === engineDefault
          ? "engine_default"
          : "configured";
    return {
      path,
      ...(effective !== null && effective !== undefined ? { value: effective } : {}),
      source,
    };
  });
}
