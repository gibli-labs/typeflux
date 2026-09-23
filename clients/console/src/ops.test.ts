import { describe, expect, it } from "vitest";

import { generateExecutionId, parseWorkflowInput, traceQuerySnippet } from "./ops";

describe("parseWorkflowInput", () => {
  it("accepts a JSON object", () => {
    const parsed = parseWorkflowInput('{"case_id": "c-1", "amount": 1200}');
    expect(parsed).toEqual({ ok: true, value: { case_id: "c-1", amount: 1200 } });
  });

  it("rejects empty input, invalid JSON, and non-objects", () => {
    expect(parseWorkflowInput("   ")).toMatchObject({ ok: false });
    const invalid = parseWorkflowInput("{nope");
    expect(invalid.ok).toBe(false);
    if (!invalid.ok) expect(invalid.error).toContain("not valid JSON");
    expect(parseWorkflowInput("[1, 2]")).toMatchObject({
      ok: false,
      error: "workflow input must be a JSON object",
    });
    expect(parseWorkflowInput('"text"')).toMatchObject({ ok: false });
    expect(parseWorkflowInput("null")).toMatchObject({ ok: false });
  });
});

describe("generateExecutionId", () => {
  it("is workflow-prefixed and time-derived", () => {
    const id = generateExecutionId("claims", 1765432100000);
    expect(id.startsWith("claims-")).toBe(true);
    expect(id).toBe(`claims-${(1765432100000).toString(36)}`);
  });
});

describe("traceQuerySnippet", () => {
  it("renders the receipt hint as TraceListQuery kwargs", () => {
    expect(traceQuerySnippet({ workflow_id: "case-1", limit: 1 })).toBe(
      'TraceListQuery(workflow_id="case-1", limit=1)',
    );
  });
});

describe("lifecycle gating", () => {
  it("allows review only while waiting at the gate", async () => {
    const { canSubmitReview, isTerminal } = await import("./ops");
    expect(canSubmitReview("waiting_for_review")).toBe(true);
    for (const state of ["running", "completed", "failed", "cancelled", null, undefined]) {
      expect(canSubmitReview(state as string | null | undefined)).toBe(false);
    }
    expect(isTerminal("completed")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("waiting_for_review")).toBe(false);
  });
});

describe("reviewerAttribution", () => {
  it("uses the trusted proxy principal as the reviewer, ignoring free text", async () => {
    const { reviewerAttribution } = await import("./ops");
    expect(reviewerAttribution("alice", "typed-something-else")).toEqual({
      value: "alice",
      fromIdentity: true,
    });
  });

  it("falls back to trimmed free text (or null) without an identity", async () => {
    const { reviewerAttribution } = await import("./ops");
    expect(reviewerAttribution(null, "  bob  ")).toEqual({ value: "bob", fromIdentity: false });
    expect(reviewerAttribution(undefined, "")).toEqual({ value: null, fromIdentity: false });
    expect(reviewerAttribution(null, "   ")).toEqual({ value: null, fromIdentity: false });
  });

  it("treats an empty or whitespace identity as absent (defensive; the server already nulls it)", async () => {
    const { reviewerAttribution } = await import("./ops");
    expect(reviewerAttribution("", "carol")).toEqual({ value: "carol", fromIdentity: false });
    expect(reviewerAttribution("   ", "")).toEqual({ value: null, fromIdentity: false });
  });
});
