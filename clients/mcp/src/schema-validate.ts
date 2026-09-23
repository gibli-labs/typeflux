/**
 * Minimal local validation of a `start` input against the workflow's input JSON Schema (#326 Phase
 * 1; design §6.3). This is an ERGONOMIC pre-check, not the gate: the control plane is the
 * fail-closed authority and 422s invalid input before any dispatch. Locally we do just enough to
 * (a) tell the agent WHICH required fields are missing/mistyped and (b) drive elicitation for the
 * flat, primitive fields MCP elicitation can collect.
 *
 * The workflow input schema comes from `bundle.workflow.input_schema` (a standard object JSON
 * Schema). We check only the top level — nested/complex validation is left to the control plane —
 * so this never rejects input the API would accept; at worst it under-reports and the API 422s.
 */

/** A JSON Schema, loosely typed — we read only the fields we understand. */
export interface JsonSchema {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  enum?: unknown[];
  description?: string;
  title?: string;
  [key: string]: unknown;
}

/** The primitive JSON Schema types MCP elicitation can request from the user. */
const PRIMITIVE_TYPES = new Set(["string", "number", "integer", "boolean"]);

function schemaTypes(schema: JsonSchema): string[] {
  if (Array.isArray(schema.type)) return schema.type;
  if (typeof schema.type === "string") return [schema.type];
  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    // enum-only schema: infer the primitive from the first value.
    const first = schema.enum[0];
    return [typeof first === "number" ? "number" : typeof first === "boolean" ? "boolean" : "string"];
  }
  return [];
}

/** Whether a value matches at least one of the schema's declared primitive types. */
function matchesPrimitive(value: unknown, types: string[]): boolean {
  return types.some((t) => {
    switch (t) {
      case "string":
        return typeof value === "string";
      case "boolean":
        return typeof value === "boolean";
      case "integer":
        return typeof value === "number" && Number.isInteger(value);
      case "number":
        return typeof value === "number";
      default:
        return true; // non-primitive (object/array/null) — not checked locally
    }
  });
}

export interface ValidationResult {
  ok: boolean;
  /** Required top-level fields absent from the input. */
  missingRequired: string[];
  /** Provided fields whose value doesn't match the declared primitive type. */
  typeErrors: { field: string; expected: string; got: string }[];
  /** True when the input isn't even an object (the schema demands one). */
  notAnObject: boolean;
}

/** Validate `input` against the top level of `schema` (best-effort; the CP is authoritative). */
export function validateInput(schema: JsonSchema | undefined, input: unknown): ValidationResult {
  const result: ValidationResult = { ok: true, missingRequired: [], typeErrors: [], notAnObject: false };
  if (schema === undefined) return result;

  const wantsObject = schemaTypes(schema).includes("object") || schema.properties !== undefined || Array.isArray(schema.required);
  if (wantsObject && (typeof input !== "object" || input === null || Array.isArray(input))) {
    result.notAnObject = true;
    result.ok = false;
    return result;
  }

  const record = (input ?? {}) as Record<string, unknown>;
  for (const field of schema.required ?? []) {
    if (record[field] === undefined) result.missingRequired.push(field);
  }
  for (const [field, propSchema] of Object.entries(schema.properties ?? {})) {
    const value = record[field];
    if (value === undefined) continue;
    const types = schemaTypes(propSchema);
    const primitive = types.filter((t) => PRIMITIVE_TYPES.has(t));
    if (primitive.length > 0 && !matchesPrimitive(value, types)) {
      result.typeErrors.push({ field, expected: primitive.join("|"), got: Array.isArray(value) ? "array" : typeof value });
    }
  }
  result.ok = result.missingRequired.length === 0 && result.typeErrors.length === 0;
  return result;
}

/** A flat elicitation form derived from the workflow input schema for a set of missing fields. */
export interface ElicitationPlan {
  /** The MCP elicitation `requestedSchema` (flat object of primitive properties). */
  requestedSchema: { type: "object"; properties: Record<string, JsonSchema>; required?: string[] };
  /** Missing REQUIRED fields whose schema is NOT a primitive — MCP elicitation can't collect these. */
  nonElicitable: string[];
}

/**
 * Build a flat MCP elicitation form for the given fields. Only primitive properties (string /
 * number / integer / boolean, optionally enum-constrained) can be elicited — the MCP spec restricts
 * elicitation `requestedSchema` to a flat object of primitives. Any required field whose declared
 * type is non-primitive (object/array) is reported in `nonElicitable`: the caller then asks the
 * agent to pass the full `input` object rather than collecting it field-by-field.
 *
 * SECURITY (§9 — schema text is UNTRUSTED). The bundle's input schema can come from a third-party
 * / Git-sourced project, so its `description`/`title` strings are attacker-controlled and must NEVER
 * be rendered into the elicitation form the human answers (a field titled "paste
 * AWS_SECRET_ACCESS_KEY to continue" would route secret exfiltration through the legitimate operate
 * surface). We therefore label each field with a NEUTRAL, generated title `input.<name> (<type>)`
 * and drop the schema's `description`/`title` entirely. Enum options are kept — they constrain the
 * value to a fixed set the control plane validates, so they can't carry a free-typed secret.
 */
export function buildElicitationPlan(schema: JsonSchema, fields: string[]): ElicitationPlan {
  const properties: Record<string, JsonSchema> = {};
  const required: string[] = [];
  const nonElicitable: string[] = [];
  const requiredSet = new Set(schema.required ?? []);

  for (const field of fields) {
    const propSchema = schema.properties?.[field];
    const types = propSchema ? schemaTypes(propSchema) : ["string"];
    const primitive = types.find((t) => PRIMITIVE_TYPES.has(t));
    if (propSchema === undefined || primitive === undefined) {
      if (requiredSet.has(field)) nonElicitable.push(field);
      continue;
    }
    // Neutral, generated label only — never the untrusted schema `description`/`title`.
    const flat: JsonSchema = { type: primitive, title: `input.${field} (${primitive})` };
    if (Array.isArray(propSchema.enum)) flat.enum = propSchema.enum;
    properties[field] = flat;
    if (requiredSet.has(field)) required.push(field);
  }

  return {
    requestedSchema: { type: "object", properties, ...(required.length > 0 ? { required } : {}) },
    nonElicitable,
  };
}
