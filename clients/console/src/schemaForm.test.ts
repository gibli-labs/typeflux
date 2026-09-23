import { describe, expect, it } from "vitest";

import { assemble, planFields, validate } from "./schemaForm";

const SCHEMA = {
  type: "object",
  required: ["case_id", "amount"],
  properties: {
    case_id: { type: "string", title: "Case id" },
    amount: { type: "number", description: "claim amount" },
    priority: { type: "string", enum: ["low", "high"] },
    urgent: { type: "boolean" },
    evidence: { type: "array", items: { type: "string" } },
  },
};

describe("planFields", () => {
  it("plans typed fields and falls back to complex for arrays", () => {
    const plan = planFields(SCHEMA);
    expect(plan.structured).toBe(true);
    const byKey = Object.fromEntries(plan.fields.map((f) => [f.key, f]));
    expect(byKey.case_id).toMatchObject({ kind: "string", required: true, label: "Case id" });
    expect(byKey.amount).toMatchObject({ kind: "number", required: true });
    expect(byKey.priority).toMatchObject({ kind: "enum", enumValues: ["low", "high"] });
    expect(byKey.urgent.kind).toBe("boolean");
    expect(byKey.evidence.kind).toBe("complex");
  });

  it("reports non-object schemas as unstructured", () => {
    expect(planFields({ type: "string" }).structured).toBe(false);
    expect(planFields(null).structured).toBe(false);
  });
});

describe("assemble", () => {
  it("coerces by field kind and drops blanks", () => {
    const plan = planFields(SCHEMA);
    const value = assemble(plan, {
      case_id: "c-1",
      amount: "1200",
      urgent: "true",
      evidence: '["a","b"]',
      priority: "",
    });
    expect(value).toEqual({ case_id: "c-1", amount: 1200, urgent: true, evidence: ["a", "b"] });
  });
});

describe("integer enums", () => {
  it("coerces an integer-typed enum to a number that validates", () => {
    const schema = {
      type: "object",
      required: ["tier"],
      properties: { tier: { type: "integer", enum: [1, 2, 3] } },
    };
    const plan = planFields(schema);
    expect(plan.fields[0]).toMatchObject({ kind: "enum", enumType: "integer" });
    const value = assemble(plan, { tier: "2" });
    expect(value).toEqual({ tier: 2 });
    expect(validate(schema, value)).toEqual([]);
  });
});

describe("validate", () => {
  it("passes a well-typed value and reports type/required errors", () => {
    expect(validate(SCHEMA, { case_id: "c-1", amount: 10 })).toEqual([]);
    const missing = validate(SCHEMA, { case_id: "c-1" });
    expect(missing.join(" ")).toContain("amount");
    const wrong = validate(SCHEMA, { case_id: 5, amount: "nope" });
    expect(wrong.length).toBeGreaterThan(0);
  });
});
