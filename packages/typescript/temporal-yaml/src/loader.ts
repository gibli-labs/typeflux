/**
 * `typeflux.yaml` loader (parity Epic 5, #452; Python `yaml/loader.py`
 * `load_yaml_spec`). Strict-parses YAML text, env-interpolates it, and validates it
 * into a typed {@link TypefluxYamlSpec}.
 *
 * "Strict" matches the Python loader: duplicate mapping keys are rejected (silent
 * last-wins would let a file show one value to a reader while the runtime resolves
 * another), and document size + alias expansion are bounded so a pathological
 * document can't amplify downstream.
 */

import { parseDocument } from "yaml";
import { z } from "zod";

import { interpolateEnv } from "./env-interpolation.js";
import { assertSafeKeys, deepMerge, validateYamlOverrides } from "./overrides.js";
import { type TypefluxYamlSpec, typefluxYamlSpec } from "./spec.js";

// Bounds for operator-trusted config files (parity with the Python loader).
export const MAX_YAML_BYTES = 1024 * 1024;
export const MAX_YAML_ALIASES = 1000;

export interface LoadYamlSpecOptions {
  /** Environment to resolve `${VAR}` references against (defaults to `process.env`). */
  env?: Record<string, string | undefined>;
  /** A label for error messages (e.g. the spec file path). */
  sourceLabel?: string;
  /**
   * Project-wide runtime defaults applied BENEATH the workflow YAML (Python
   * `runtime_defaults`): merged as `{ runtime: … }` under the spec, so the YAML
   * (and any `overrides`) still win. Validated against the runtime-override allow-list.
   */
  runtimeDefaults?: Record<string, unknown>;
  /**
   * Environment/profile overrides applied OVER the workflow YAML (Python
   * `overrides`): deep-merged on top, so a deployment target can retarget the
   * allow-listed `task_queue` / `runtime.*` blocks. Validated against the allow-list;
   * applied before interpolation so an override value may reference `${VAR}`.
   */
  overrides?: Record<string, unknown>;
}

/** Parse, env-interpolate, and validate a `typeflux.yaml` document. */
export function loadYamlSpec(text: string, options: LoadYamlSpecOptions = {}): TypefluxYamlSpec {
  const sourceLabel = options.sourceLabel ?? "<yaml>";
  if (Buffer.byteLength(text, "utf-8") > MAX_YAML_BYTES) {
    throw new Error(`YAML document exceeds the ${MAX_YAML_BYTES} byte limit (${sourceLabel})`);
  }

  // `merge: true` expands YAML `<<` merge keys (a spec may inherit fields from an
  // anchor — parity with the Python SafeLoader, which expands them); `uniqueKeys` still
  // rejects author-written duplicates.
  const doc = parseDocument(text, { uniqueKeys: true, merge: true });
  // Reject hard parse errors and duplicate-key findings (which some versions surface
  // as warnings) — a duplicate key must fail, not silently last-win.
  const fatal = [...doc.errors, ...doc.warnings.filter((w) => w.code === "DUPLICATE_KEY")];
  if (fatal.length > 0) {
    throw new Error(`invalid YAML spec (${sourceLabel}): ${fatal[0]?.message}`);
  }

  // `maxAliasCount` bounds alias expansion at materialization (billion-laughs guard).
  const raw: unknown = doc.toJS({ maxAliasCount: MAX_YAML_ALIASES });
  if (raw === null || raw === undefined) {
    throw new Error(`empty YAML spec: ${sourceLabel}`);
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new TypeError(`Typeflux YAML spec must be a mapping (${sourceLabel})`);
  }

  // Layer project defaults + environment overrides BEFORE interpolation (Python
  // `load_yaml_spec`): runtime defaults sit beneath the YAML (it wins over them);
  // overrides sit on top (they win over both). Each is bounded to the override
  // allow-list — a deployment target retargets connections/providers, not the graph.
  let config = raw as Record<string, unknown>;
  // `!= null` (not `!== undefined`): a caller passing `null` (Python's `None`) must
  // no-op like an omitted layer — `Object.keys(null)` would otherwise throw.
  if (options.runtimeDefaults != null && Object.keys(options.runtimeDefaults).length > 0) {
    const defaultsLayer = { runtime: options.runtimeDefaults };
    validateYamlOverrides(defaultsLayer);
    config = deepMerge(defaultsLayer, config);
  }
  if (options.overrides != null && Object.keys(options.overrides).length > 0) {
    validateYamlOverrides(options.overrides);
    config = deepMerge(config, options.overrides);
  }

  // Reject prototype-aliasing keys (from the YAML or an override) that zod's strict
  // schema would ignore — Python's `extra=forbid` rejects them (codex).
  assertSafeKeys(config, sourceLabel);

  const interpolated = interpolateEnv(
    config,
    options.env !== undefined ? { env: options.env, sourceLabel } : { sourceLabel },
  );
  const result = typefluxYamlSpec.safeParse(interpolated);
  if (!result.success) {
    // The spec is strict (#490) — unknown keys and unsupported blocks are rejected here. A
    // prettified message (paths + messages) beats a raw ZodError dump for a hand-edited file.
    throw new Error(`invalid Typeflux spec (${sourceLabel}):\n${z.prettifyError(result.error)}`);
  }
  return result.data;
}
