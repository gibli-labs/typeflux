import { describe, expect, it } from "vitest";

import {
  SUBJECT_IDS_SEARCH_ATTRIBUTE,
  effectiveSubjectIds,
  normalizeSubjectIds,
  resolveSubjectIds,
  subjectIndexQuery,
  subjectTraceTags,
  subjectUserId,
} from "../src/index.js";
import { workflowSpec } from "../src/spec.js";

describe("subject extraction (#715 slice 1)", () => {
  it("resolves a single subject off the input", () => {
    expect(resolveSubjectIds({ patient_ref: "pt-1" }, [{ fromPath: "input.patient_ref", required: true }])).toEqual([
      "pt-1",
    ]);
  });

  it("resolves multi-value and de-dups preserving order (primary first)", () => {
    expect(
      resolveSubjectIds({ patient_ref: "pt-1", others: ["pt-2", "pt-1", "pt-3"] }, [
        { fromPath: "input.patient_ref", required: true },
        { fromPath: "input.others", required: true },
      ]),
    ).toEqual(["pt-1", "pt-2", "pt-3"]);
  });

  it("throws for a required selector that resolves to nothing", () => {
    expect(() => resolveSubjectIds({ patient_ref: "pt-1" }, [{ fromPath: "input.absent", required: true }])).toThrow(
      /resolved to no value/,
    );
  });

  it("skips an optional selector that resolves to nothing", () => {
    expect(resolveSubjectIds({ patient_ref: "pt-1" }, [{ fromPath: "input.absent", required: false }])).toEqual([]);
  });

  it("throws for a required selector that resolves to an EMPTY list (#715 review, finding 2)", () => {
    expect(() => resolveSubjectIds({ refs: [] }, [{ fromPath: "input.refs", required: true }])).toThrow(
      /resolved to no value/,
    );
  });

  it("skips an optional selector that resolves to an empty list", () => {
    expect(resolveSubjectIds({ refs: [] }, [{ fromPath: "input.refs", required: false }])).toEqual([]);
  });

  it("throws for a required selector whose list holds only whitespace strings", () => {
    expect(() => resolveSubjectIds({ refs: ["   "] }, [{ fromPath: "input.refs", required: true }])).toThrow(
      /non-empty-string/,
    );
  });

  it("rejects an empty-string subject value", () => {
    expect(() => resolveSubjectIds({ patient_ref: "  " }, [{ fromPath: "input.patient_ref", required: true }])).toThrow(
      /non-empty-string/,
    );
  });

  it("normalizes an explicit override (validate + de-dup)", () => {
    expect(normalizeSubjectIds(["a", "a", "b"])).toEqual(["a", "b"]);
    expect(() => normalizeSubjectIds(["a", ""])).toThrow(/non-empty strings/);
  });

  it("userId is the primary subject; tags are one per subject", () => {
    expect(subjectUserId(["pt-1", "pt-2"])).toBe("pt-1");
    expect(subjectUserId([])).toBeUndefined();
    expect(subjectTraceTags(["pt-1", "pt-2"])).toEqual(["typeflux.subject:pt-1", "typeflux.subject:pt-2"]);
  });

  it("index query matches keyword-list membership and escapes quotes", () => {
    expect(subjectIndexQuery("pt-1")).toBe(`${SUBJECT_IDS_SEARCH_ATTRIBUTE} = 'pt-1'`);
    // Single quotes escape by DOUBLING (Temporal visibility SQL convention).
    expect(subjectIndexQuery("o'brien")).toBe(`${SUBJECT_IDS_SEARCH_ATTRIBUTE} = 'o''brien'`);
  });
});

describe("effectiveSubjectIds — explicit override semantics (#715 review round 2)", () => {
  const selectors = [{ fromPath: "input.patient_ref", required: true }];

  it("an EMPTY override falls through to declarative extraction (never a silent bypass)", () => {
    expect(effectiveSubjectIds([], { patient_ref: "pt-1" }, selectors)).toEqual(["pt-1"]);
  });

  it("an EMPTY override + a required selector that contributes zero ids still throws", () => {
    expect(() => effectiveSubjectIds([], {}, selectors)).toThrow(/resolved to no value/);
  });

  it("an EMPTY override with no subjects block is inert (unchanged behavior)", () => {
    expect(effectiveSubjectIds([], { patient_ref: "pt-1" }, [])).toEqual([]);
  });

  it("a NON-EMPTY override still wins over extraction", () => {
    expect(effectiveSubjectIds(["explicit-a"], { patient_ref: "pt-1" }, selectors)).toEqual(["explicit-a"]);
  });

  it("no override (undefined) runs extraction", () => {
    expect(effectiveSubjectIds(undefined, { patient_ref: "pt-1" }, selectors)).toEqual(["pt-1"]);
  });
});

describe("subjects spec block (#715 slice 1)", () => {
  const base = { name: "W", input: "schemas:In", output: "schemas:Out", steps: [{ id: "s", activity: "a" }] };

  it("parses subjects with required defaulting to true", () => {
    const spec = workflowSpec.parse({
      ...base,
      subjects: [{ from: "input.value" }, { from: "input.other", required: false }],
    });
    expect(spec.subjects).toEqual([
      { from: "input.value", required: true },
      { from: "input.other", required: false },
    ]);
  });

  it("rejects a subject `from` that does not start with input.", () => {
    expect(() => workflowSpec.parse({ ...base, subjects: [{ from: "context.value" }] })).toThrow(
      /must start with 'input\.'/,
    );
  });

  it("rejects an unknown key on a subject selector (strict, spec-model-equals-wired)", () => {
    expect(() => workflowSpec.parse({ ...base, subjects: [{ from: "input.value", bogus: 1 }] })).toThrow();
  });
});
