/**
 * Schema-driven input planning and validation (#269). Turns a workflow's
 * input JSON Schema into a flat field plan the console can render as typed
 * inputs, and validates the assembled value with ajv before a start call.
 * Anything the planner can't render as a simple field (nested objects,
 * arrays, unions) is a `complex` field the form edits as JSON.
 */

import Ajv from "ajv";

export type FieldKind = "string" | "number" | "integer" | "boolean" | "enum" | "complex";

export interface FieldPlan {
  key: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  description?: string;
  enumValues?: string[];
  /** Underlying scalar type for an enum field, for value coercion. */
  enumType?: FieldKind;
}

export interface InputPlan {
  /** True when the schema is a plain object with renderable properties. */
  structured: boolean;
  fields: FieldPlan[];
}

type JsonSchema = Record<string, unknown>;

function scalarKind(schema: JsonSchema): FieldKind {
  if (Array.isArray(schema["enum"])) return "enum";
  const type = schema["type"];
  if (type === "string") return "string";
  if (type === "number") return "number";
  if (type === "integer") return "integer";
  if (type === "boolean") return "boolean";
  return "complex";
}

export function planFields(schema: JsonSchema | null | undefined): InputPlan {
  if (!schema || schema["type"] !== "object" || typeof schema["properties"] !== "object") {
    return { structured: false, fields: [] };
  }
  const properties = schema["properties"] as Record<string, JsonSchema>;
  const required = new Set((schema["required"] as string[] | undefined) ?? []);
  const fields: FieldPlan[] = Object.entries(properties).map(([key, prop]) => {
    const kind = scalarKind(prop);
    const baseType: FieldKind =
      prop["type"] === "integer"
        ? "integer"
        : prop["type"] === "number"
          ? "number"
          : prop["type"] === "boolean"
            ? "boolean"
            : "string";
    return {
      key,
      label: (prop["title"] as string | undefined) ?? key,
      kind,
      required: required.has(key),
      description: prop["description"] as string | undefined,
      enumValues:
        kind === "enum" ? (prop["enum"] as unknown[]).map((value) => String(value)) : undefined,
      enumType: kind === "enum" ? baseType : undefined,
    };
  });
  return { structured: true, fields };
}

/** Build the typed object from raw form state per the plan. */
export function assemble(plan: InputPlan, state: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of plan.fields) {
    const raw = state[field.key];
    if (raw === undefined || raw === "") {
      continue;
    }
    if (field.kind === "boolean") {
      out[field.key] = raw === "true";
    } else if (field.kind === "enum") {
      if (field.enumType === "number" || field.enumType === "integer") {
        const parsed = Number(raw);
        out[field.key] = Number.isNaN(parsed) ? raw : parsed;
      } else if (field.enumType === "boolean") {
        out[field.key] = raw === "true";
      } else {
        out[field.key] = raw;
      }
    } else if (field.kind === "number" || field.kind === "integer") {
      const parsed = Number(raw);
      out[field.key] = Number.isNaN(parsed) ? raw : parsed;
    } else if (field.kind === "complex") {
      try {
        out[field.key] = JSON.parse(raw);
      } catch {
        out[field.key] = raw;
      }
    } else {
      out[field.key] = raw;
    }
  }
  return out;
}

const ajv = new Ajv({ allErrors: true, strict: false });

/** Validate a value against the JSON Schema; returns human-readable errors. */
export function validate(schema: JsonSchema, value: unknown): string[] {
  const check = ajv.compile(schema);
  if (check(value)) return [];
  return (check.errors ?? []).map((error) => {
    const path = error.instancePath || "(root)";
    return `${path} ${error.message ?? "is invalid"}`.trim();
  });
}
