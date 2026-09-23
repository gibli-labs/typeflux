import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { JsonSchema } from "../src/index.js";
import { lintProviderSafe, ProviderSchemaError, toProviderSafe } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "../../../../contracts");

function golden(name: string): JsonSchema {
  const path = resolve(contractsDir, `schema-profile/golden/${name}`);
  return JSON.parse(readFileSync(path, "utf-8")) as JsonSchema;
}

describe("provider-safe schema conformance (#389)", () => {
  it("normalizes the raw input fixture to the golden (reproduces Python to_provider_safe)", () => {
    const input = golden("review_packet_input.json");
    const expected = golden("review_packet.json");
    expect(toProviderSafe(input)).toEqual(expected);
  });

  it("is idempotent on an already provider-safe schema", () => {
    const safe = golden("review_packet.json");
    expect(toProviderSafe(safe)).toEqual(safe);
  });

  it("closes a nullable object (type: [object, null]) — reproduces Python (#422)", () => {
    // Hand-written/raw nullable object (Zod emits anyOf, not a type-array). The
    // top-level and the nested nullable object are both closed; the already-nullable
    // nested object is not double-wrapped. Byte-identical to the Python golden.
    const input = golden("nullable_object_input.json");
    const expected = golden("nullable_object.json");
    expect(toProviderSafe(input)).toEqual(expected);
  });

  it("inlines a $ref to a non-object $defs entry (matches Python defs.update)", () => {
    // Pydantic/Zod never emit this, but parity with the Python contract requires
    // keeping non-object defs: the ref resolves to the value, not unresolved-ref.
    const out = toProviderSafe({
      $defs: { N: 5 },
      type: "object",
      properties: { a: { $ref: "#/$defs/N" } },
      required: ["a"],
    });
    expect((out["properties"] as JsonSchema)["a"]).toBe(5);
  });

  it("inlines $ref + strips $defs, closes nested objects, and wraps optionals nullable", () => {
    const out = toProviderSafe(golden("review_packet_input.json"));
    expect("$defs" in out).toBe(false);
    expect(JSON.stringify(out).includes("$ref")).toBe(false);

    const props = out["properties"] as JsonSchema;
    const item = (props["references"] as JsonSchema)["items"] as JsonSchema;
    expect(item["additionalProperties"]).toBe(false);
    expect(item["required"]).toEqual(["source", "page"]);

    // `escalated` (had a default) was not originally required -> nullable union.
    const escalated = props["escalated"] as JsonSchema;
    expect(escalated["anyOf"]).toContainEqual({ type: "null" });
    expect(out["required"]).toEqual(["subject", "urgency", "references", "summary", "escalated"]);
  });
});

describe("normalization rules", () => {
  it("const -> single-member enum", () => {
    const out = toProviderSafe({
      type: "object",
      properties: { k: { const: "only" } },
      required: ["k"],
    });
    expect((out["properties"] as JsonSchema)["k"]).toMatchObject({ enum: ["only"] });
    expect(JSON.stringify(out).includes("const")).toBe(false);
  });

  it("oneOf (+ discriminator) -> anyOf", () => {
    const out = toProviderSafe({
      type: "object",
      properties: {
        a: {
          oneOf: [{ type: "object", properties: {}, additionalProperties: false, required: [] }],
          discriminator: { propertyName: "k" },
        },
      },
      required: ["a"],
    });
    const a = (out["properties"] as JsonSchema)["a"] as JsonSchema;
    expect(Array.isArray(a["anyOf"])).toBe(true);
    expect("oneOf" in a).toBe(false);
    expect("discriminator" in a).toBe(false);
  });

  it("strips annotation-only keys", () => {
    const out = toProviderSafe({
      $schema: "https://json-schema.org/draft/2020-12/schema",
      type: "object",
      properties: { a: { type: "string", default: "d", examples: ["e"] } },
      required: ["a"],
    });
    const blob = JSON.stringify(out);
    for (const key of ["$schema", "default", "examples"]) {
      expect(blob.includes(`"${key}"`)).toBe(false);
    }
  });
});

describe("lint rejects non-provider-safe constructs", () => {
  it("flags an open object (additionalProperties: true)", () => {
    const violations = lintProviderSafe({
      type: "object",
      properties: { x: { type: "string" } },
      additionalProperties: true,
    });
    expect(violations.some((v) => v.rule === "open-object")).toBe(true);
  });

  it("rejects an open map (additionalProperties is a schema)", () => {
    expect(() =>
      toProviderSafe({ type: "object", properties: {}, additionalProperties: { type: "integer" } }),
    ).toThrow(ProviderSchemaError);
  });

  it("flags unsupported keywords (not / if-then / multi-branch allOf / prefixItems)", () => {
    const violations = lintProviderSafe({
      type: "object",
      properties: {
        a: { not: { type: "string" } },
        b: { if: { type: "string" }, then: { type: "string" } },
        c: { allOf: [{ type: "object" }, { type: "object" }] },
        d: { prefixItems: [{ type: "string" }] },
      },
    });
    const rules = new Set(violations.map((v) => v.rule));
    expect(rules.has("not")).toBe(true);
    expect(rules.has("conditional")).toBe(true);
    expect(rules.has("allOf-unsupported")).toBe(true);
    expect(rules.has("tuple-items")).toBe(true);
  });

  it("rejects a recursive model with a recursive-ref violation", () => {
    let error: unknown;
    try {
      toProviderSafe({
        $defs: {
          Node: {
            type: "object",
            properties: { children: { type: "array", items: { $ref: "#/$defs/Node" } } },
            required: [],
          },
        },
        type: "object",
        properties: { root: { $ref: "#/$defs/Node" } },
        required: ["root"],
      });
    } catch (caught) {
      error = caught;
    }
    expect(error).toBeInstanceOf(ProviderSchemaError);
    expect((error as ProviderSchemaError).violations.some((v) => v.rule === "recursive-ref")).toBe(
      true,
    );
  });
});
