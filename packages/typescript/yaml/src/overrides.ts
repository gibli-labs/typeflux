/**
 * YAML runtime-override allow-list + deep-merge (parity, #454; Python
 * `yaml/overrides.py` + `yaml/loader.py::_deep_merge`). A low-level primitive
 * shared by the workflow loader (project-defaults + environment overrides applied
 * to a spec) and the project environment model.
 *
 * The override surface is DELIBERATELY bounded: an environment/project-default may
 * retarget only `task_queue` and a fixed set of `runtime.*` connection/provider
 * blocks — never the workflow graph, activities, or prompts — so a deployment
 * target can't silently rewrite what a workflow DOES.
 */

/** Top-level keys an environment may override (Python `_ALLOWED_OVERRIDE_KEYS`). */
export const ALLOWED_OVERRIDE_KEYS = ["task_queue", "runtime"] as const;
/** `runtime.*` blocks an environment may override (Python `_ALLOWED_RUNTIME_OVERRIDE_KEYS`). */
export const ALLOWED_RUNTIME_OVERRIDE_KEYS = [
  "temporal",
  "registry",
  "provider",
  "provider_limits",
  "provider_retry",
  "observability",
] as const;

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * Only `__proto__` is rejected — it is the ONE key zod's `.strict()` silently IGNORES
 * (so a `__proto__` extra key would slip past strict validation) and the only real
 * prototype hazard. `constructor` / `prototype` are deliberately NOT here: zod's strict
 * schema already rejects them as unknown keys on a closed object, and they are LEGITIMATE
 * keys inside an open `z.record` (a workflow, provider, or prompt so named) that Python's
 * `dict[str, …]` accepts — rejecting them would diverge from Python.
 */
const UNSAFE_KEYS = new Set(["__proto__"]);

/**
 * Reject an own `__proto__` key anywhere in a parsed config. The JS `yaml` lib
 * materializes it as an own property and zod's `.strict()` silently IGNORES it (Python's
 * pydantic `extra=forbid` rejects it) — so the loaders reject it explicitly, closing the
 * strict-validation-bypass class at the boundary (codex). Pairs with the prototype-safe
 * {@link deepMerge} / interpolation so the key never routes through a prototype setter
 * en route to this check.
 */
export function assertSafeKeys(value: unknown, sourceLabel: string, path = ""): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertSafeKeys(item, sourceLabel, `${path}[${index}]`));
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, item] of Object.entries(value)) {
    if (UNSAFE_KEYS.has(key)) {
      throw new Error(`unsafe key '${key}' is not allowed (${sourceLabel})${path ? ` at ${path}` : ""}`);
    }
    assertSafeKeys(item, sourceLabel, path ? `${path}.${key}` : key);
  }
}

/** The allow-list violations of an override map (Python `validate_yaml_overrides`). */
export function overrideViolations(overrides: Record<string, unknown>, prefix: string): string[] {
  const violations: string[] = [];
  for (const key of Object.keys(overrides)) {
    if (!(ALLOWED_OVERRIDE_KEYS as readonly string[]).includes(key)) {
      violations.push(`${prefix}.${key} is not an allowed environment override`);
    }
  }
  if (Object.hasOwn(overrides, "runtime")) {
    const runtime = overrides["runtime"];
    if (!isRecord(runtime)) {
      violations.push(`${prefix}.runtime must be a mapping`);
    } else {
      for (const key of Object.keys(runtime)) {
        if (!(ALLOWED_RUNTIME_OVERRIDE_KEYS as readonly string[]).includes(key)) {
          violations.push(`${prefix}.runtime.${key} is not an allowed runtime override`);
        }
      }
    }
  }
  return violations;
}

/**
 * Validate an override map against the allow-list (Python `validate_yaml_overrides`),
 * throwing on the first violation. Exposed for the loader/overlay + callers building
 * overrides programmatically.
 */
export function validateYamlOverrides(overrides: Record<string, unknown>, prefix = "overrides"): void {
  const [first] = overrideViolations(overrides, prefix);
  if (first !== undefined) throw new Error(first);
}

/**
 * The sorted dotted leaf-paths of an override map (Python `yaml_override_paths`) —
 * a non-empty mapping recurses; a leaf (scalar / empty mapping) terminates a path.
 * Used to record override PROVENANCE.
 */
export function yamlOverridePaths(overrides: Record<string, unknown>): string[] {
  const paths: string[] = [];
  const walk = (path: string, value: unknown): void => {
    if (!isRecord(value) || Object.keys(value).length === 0) {
      paths.push(path);
      return;
    }
    for (const [key, child] of Object.entries(value)) {
      walk(`${path}.${key}`, child);
    }
  };
  for (const [key, value] of Object.entries(overrides)) {
    walk(key, value);
  }
  return paths.sort();
}

/**
 * Recursively merge `right` over `left` (Python `_deep_merge`): a key present in
 * both AND a mapping in both is merged deeply; otherwise `right` wins. Arrays and
 * scalars are replaced wholesale (not element-merged). Neither input is MUTATED, but
 * the result is not a deep clone — a subtree present on only one side is ALIASED into
 * the result (matching Python), so treat the merged object as read-only.
 */
export function deepMerge(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): Record<string, unknown> {
  const merged: Record<string, unknown> = {};
  // Assign via defineProperty (never `merged[key] = …`) so a dangerous key like
  // `__proto__` becomes an OWN enumerable property rather than routing through the
  // prototype setter — this both blocks prototype pollution AND preserves the key so
  // the strict schema still REJECTS it after the merge (Python keeps it too; codex).
  // Reads use own-descriptor lookups for the same reason.
  const put = (key: string, value: unknown): void => {
    Object.defineProperty(merged, key, { value, enumerable: true, writable: true, configurable: true });
  };
  const own = (key: string): unknown => Object.getOwnPropertyDescriptor(merged, key)?.value;
  for (const [key, value] of Object.entries(left)) {
    put(key, value);
  }
  for (const [key, value] of Object.entries(right)) {
    const existing = own(key);
    put(key, isRecord(existing) && isRecord(value) ? deepMerge(existing, value) : value);
  }
  return merged;
}
