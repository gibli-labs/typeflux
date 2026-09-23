/**
 * Provider-safe JSON Schema profile + normalizer (#389), the TypeScript mirror
 * of the Python `contracts/schema.py`. The neutral cross-SDK form for structured
 * output is JSON Schema (draft 2020-12); the provider-safe profile is the subset
 * every supported mode accepts (Gemini `response_schema`, OpenAI strict
 * `json_schema`, Anthropic `json_schema`). Gemini is the binding constraint: no
 * `$ref`/`$defs`, no open objects.
 *
 * `toProviderSafe` normalizes an arbitrary schema to the profile;
 * `lintProviderSafe` reports constructs that cannot be normalized. Both SDKs run
 * this same normalizer over their native-mapped draft-2020-12 schema, so the
 * same typed shape yields equivalent provider-safe output everywhere.
 */

export type JsonSchema = Record<string, unknown>;

/** A single reason a schema is not provider-safe. */
export interface Violation {
  pointer: string;
  rule: string;
  detail: string;
}

/** Thrown by `toProviderSafe` when normalization cannot make a schema safe. */
export class ProviderSchemaError extends Error {
  readonly violations: Violation[];

  constructor(violations: Violation[]) {
    const joined = violations.map((v) => `${v.pointer} [${v.rule}]: ${v.detail}`).join("; ");
    super(`schema is not provider-safe: ${joined}`);
    this.name = "ProviderSchemaError";
    this.violations = [...violations];
  }
}

// Annotation/metadata keys dropped from the wire schema.
const STRIP_KEYS = new Set([
  "$schema",
  "$id",
  "$anchor",
  "$comment",
  "$defs",
  "definitions",
  "default",
  "examples",
  "discriminator",
]);

// `format` values kept; anything else is dropped (providers reject unknowns).
const ALLOWED_FORMATS = new Set([
  "date-time",
  "date",
  "time",
  "duration",
  "uri",
  "uri-reference",
  "email",
  "uuid",
]);

// Keywords with no portable structured-output expression (rule id -> surfaced).
const DISALLOWED_KEYS: Record<string, string> = {
  patternProperties: "pattern-properties",
  dependentSchemas: "dependent-schemas",
  dependentRequired: "dependent-required",
  dependencies: "dependencies",
  if: "conditional",
  then: "conditional",
  else: "conditional",
  not: "not",
  allOf: "allOf-unsupported",
  prefixItems: "tuple-items",
};

const SCHEMA_VALUE_KEYS = ["items", "additionalProperties", "contains"];
const SCHEMA_LIST_KEYS = ["anyOf", "allOf", "oneOf", "prefixItems"];
const SCHEMA_MAP_KEYS = ["properties", "$defs", "definitions", "patternProperties"];

/** Return the provider-safe schema, or throw `ProviderSchemaError`. */
export function toProviderSafe(schema: JsonSchema): JsonSchema {
  // Keep every $defs entry regardless of value type, matching Python's
  // `defs.update(value)`: a $ref to a non-object def resolves to that value.
  const defs: Record<string, unknown> = {};
  for (const key of ["$defs", "definitions"]) {
    const value = schema[key];
    if (isPlainObject(value)) {
      Object.assign(defs, value);
    }
  }
  const inlined = inline(structuredClone(schema), defs, []);
  const normalized = normalize(inlined) as JsonSchema;
  const violations = lintProviderSafe(normalized);
  if (violations.length > 0) {
    throw new ProviderSchemaError(violations);
  }
  return normalized;
}

/** Return the constructs in an already-inlined/normalized schema that aren't safe. */
export function lintProviderSafe(schema: JsonSchema): Violation[] {
  const out: Violation[] = [];
  lint(schema, "#", out);
  return out;
}

// --- internals ---------------------------------------------------------------

function isPlainObject(value: unknown): value is JsonSchema {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function mapValues(obj: JsonSchema, fn: (value: unknown) => unknown): JsonSchema {
  const out: JsonSchema = {};
  for (const [key, value] of Object.entries(obj)) {
    out[key] = fn(value);
  }
  return out;
}

function refName(ref: string): string | null {
  for (const prefix of ["#/$defs/", "#/definitions/"]) {
    if (ref.startsWith(prefix)) {
      return ref.slice(prefix.length);
    }
  }
  return null;
}

function inline(node: unknown, defs: Record<string, unknown>, stack: string[]): unknown {
  if (Array.isArray(node)) {
    return node.map((item) => inline(item, defs, stack));
  }
  if (!isPlainObject(node)) {
    return node;
  }
  const ref = node["$ref"];
  if (typeof ref === "string") {
    const name = refName(ref);
    if (name === null || !(name in defs)) {
      return mapValues(node, (value) => inline(value, defs, stack));
    }
    if (stack.includes(name)) {
      return { __recursive_ref__: name };
    }
    const target = inline(structuredClone(defs[name]), defs, [...stack, name]);
    if (!isPlainObject(target)) {
      return target;
    }
    const merged: JsonSchema = { ...target };
    for (const [key, value] of Object.entries(node)) {
      if (key === "$ref") {
        continue;
      }
      merged[key] = inline(value, defs, stack);
    }
    return merged;
  }
  return mapValues(node, (value) => inline(value, defs, stack));
}

function normalize(node: unknown): unknown {
  if (Array.isArray(node)) {
    return node.map(normalize);
  }
  if (!isPlainObject(node)) {
    return node;
  }
  const flattened = flattenSingleAllOf(node);

  const out: JsonSchema = {};
  for (const [key, value] of Object.entries(flattened)) {
    if (STRIP_KEYS.has(key)) {
      continue;
    }
    if (key === "format") {
      if (typeof value === "string" && !ALLOWED_FORMATS.has(value)) {
        continue;
      }
      out[key] = value;
    } else if (SCHEMA_MAP_KEYS.includes(key) && isPlainObject(value)) {
      out[key] = mapValues(value, normalize);
    } else if (SCHEMA_LIST_KEYS.includes(key) || SCHEMA_VALUE_KEYS.includes(key)) {
      out[key] = normalize(value);
    } else {
      out[key] = value;
    }
  }

  if ("const" in out) {
    const constValue = out["const"];
    delete out["const"];
    out["enum"] = [constValue];
  }
  if ("oneOf" in out) {
    const oneOf = out["oneOf"] as unknown[];
    delete out["oneOf"];
    const anyOf = Array.isArray(out["anyOf"]) ? (out["anyOf"] as unknown[]) : [];
    out["anyOf"] = [...anyOf, ...oneOf];
  }

  if (isObject(out)) {
    closeObject(out);
  }
  return out;
}

function flattenSingleAllOf(node: JsonSchema): JsonSchema {
  const allOf = node["allOf"];
  if (!(Array.isArray(allOf) && allOf.length === 1 && isPlainObject(allOf[0]))) {
    return node;
  }
  const merged: JsonSchema = { ...(allOf[0] as JsonSchema) };
  for (const [key, value] of Object.entries(node)) {
    if (key === "allOf") {
      continue;
    }
    merged[key] = value;
  }
  return merged;
}

function isObject(schema: JsonSchema): boolean {
  const type = schema["type"];
  if (type === "object") {
    return true;
  }
  // A nullable object declares `type: ["object", "null"]` (hand-written/raw
  // schemas; Zod uses anyOf). Treat any type-array containing "object" as an
  // object so it gets closed; `closeObject` leaves the array intact.
  if (Array.isArray(type) && type.includes("object")) {
    return true;
  }
  return "properties" in schema && !("type" in schema);
}

function closeObject(schema: JsonSchema): void {
  if (!("type" in schema)) {
    schema["type"] = "object";
  }
  const additional = schema["additionalProperties"];
  if (additional === true || isPlainObject(additional)) {
    // Explicitly open object/map: leave untouched so the linter rejects it
    // (checked before the properties branch so an open object that also
    // declares properties still raises).
    return;
  }
  const props = schema["properties"];
  if (isPlainObject(props)) {
    schema["additionalProperties"] = false;
    const required = new Set(Array.isArray(schema["required"]) ? schema["required"] : []);
    for (const [name, prop] of Object.entries(props)) {
      if (!required.has(name) && isPlainObject(prop)) {
        props[name] = makeNullable(prop);
      }
    }
    schema["required"] = Object.keys(props);
    return;
  }
  schema["additionalProperties"] = false;
  if (!("required" in schema)) {
    schema["required"] = [];
  }
}

function makeNullable(schema: JsonSchema): JsonSchema {
  if (isNullable(schema)) {
    return schema;
  }
  return { anyOf: [schema, { type: "null" }] };
}

function isNullable(schema: JsonSchema): boolean {
  const type = schema["type"];
  if (type === "null" || (Array.isArray(type) && type.includes("null"))) {
    return true;
  }
  const anyOf = schema["anyOf"];
  if (Array.isArray(anyOf)) {
    for (const variant of anyOf) {
      if (isPlainObject(variant) && variant["type"] === "null") {
        return true;
      }
    }
  }
  return false;
}

function lint(node: unknown, pointer: string, out: Violation[]): void {
  if (Array.isArray(node)) {
    node.forEach((item, index) => lint(item, `${pointer}/${index}`, out));
    return;
  }
  if (!isPlainObject(node)) {
    return;
  }

  if ("__recursive_ref__" in node) {
    const name = node["__recursive_ref__"];
    out.push({
      pointer,
      rule: "recursive-ref",
      detail: `recursive model ${JSON.stringify(name)} cannot be expressed in a provider-safe schema`,
    });
    return;
  }
  if ("$ref" in node) {
    out.push({
      pointer: `${pointer}/$ref`,
      rule: "unresolved-ref",
      detail: `unresolved $ref ${JSON.stringify(node["$ref"])}`,
    });
  }
  for (const [key, rule] of Object.entries(DISALLOWED_KEYS)) {
    if (key in node) {
      out.push({ pointer: `${pointer}/${key}`, rule, detail: `${key} is not provider-safe` });
    }
  }
  const additional = node["additionalProperties"];
  if (additional === true) {
    out.push({
      pointer: `${pointer}/additionalProperties`,
      rule: "open-object",
      detail: "additionalProperties: true is not provider-safe; use an explicit object",
    });
  } else if (isPlainObject(additional)) {
    out.push({
      pointer: `${pointer}/additionalProperties`,
      rule: "open-map",
      detail:
        "an open map (additionalProperties is a schema, e.g. dict[str, X]) is not " +
        "provider-safe; use an explicit object or a list of {key, value} entries",
    });
  }

  for (const key of SCHEMA_MAP_KEYS) {
    const value = node[key];
    if (isPlainObject(value)) {
      for (const [name, child] of Object.entries(value)) {
        lint(child, `${pointer}/${key}/${name}`, out);
      }
    }
  }
  for (const key of SCHEMA_LIST_KEYS) {
    const value = node[key];
    if (Array.isArray(value)) {
      lint(value, `${pointer}/${key}`, out);
    }
  }
  for (const key of SCHEMA_VALUE_KEYS) {
    const value = node[key];
    if (isPlainObject(value)) {
      lint(value, `${pointer}/${key}`, out);
    }
  }
}
