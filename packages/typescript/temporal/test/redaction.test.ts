import { describe, expect, it } from "vitest";

import { redactMetadata } from "../src/index.js";

describe("redactMetadata (#451)", () => {
  it("redacts emails, SSNs, and phone numbers", () => {
    expect(redactMetadata("contact a.b+x@example.com now")).toBe("contact [REDACTED_EMAIL] now");
    expect(redactMetadata("ssn 123-45-6789 end")).toBe("ssn [REDACTED_SSN] end");
    expect(redactMetadata("call 555-123-4567")).toBe("call [REDACTED_PHONE]");
  });

  it("redacts a Luhn-valid card but NOT a non-Luhn 16-digit number", () => {
    expect(redactMetadata("card 4242424242424242")).toBe("card [REDACTED_CARD]");
    expect(redactMetadata("id 1111111111111111")).toBe("id 1111111111111111"); // fails Luhn -> kept
  });

  it("honors per-rule toggles", () => {
    expect(redactMetadata("a@b.com", { emails: false })).toBe("a@b.com");
    expect(redactMetadata("123-45-6789", { ssn: false })).toBe("123-45-6789");
  });

  it("skips excluded paths (glob), redacts the rest", () => {
    const out = redactMetadata(
      { safe: "a@b.com", other: "c@d.com", nested: { keep: "e@f.com" } },
      { excludePaths: ["safe", "nested.*"] },
    );
    expect(out).toEqual({ safe: "a@b.com", other: "[REDACTED_EMAIL]", nested: { keep: "e@f.com" } });
  });

  it("recurses through nested objects and arrays", () => {
    const out = redactMetadata({ items: ["a@b.com", { note: "ssn 123-45-6789" }], n: 5 });
    expect(out).toEqual({ items: ["[REDACTED_EMAIL]", { note: "ssn [REDACTED_SSN]" }], n: 5 });
  });

  it("preserves internal Typeflux/Temporal metadata paths by default", () => {
    const meta = { temporal: { workflow_id: "wf@x.com" }, user: { email: "u@x.com" } };
    expect(redactMetadata(meta)).toEqual({
      temporal: { workflow_id: "wf@x.com" }, // default-excluded -> preserved
      user: { email: "[REDACTED_EMAIL]" },
    });
    // With preservation off, the internal path is redacted too.
    expect(redactMetadata(meta, { preserveTypefluxMetadata: false }).temporal.workflow_id).toBe("[REDACTED_EMAIL]");
  });

  it("passes non-plain values (Date, Map) through unchanged instead of rebuilding to {}", () => {
    const when = new Date("2020-01-01T00:00:00Z");
    const map = new Map([["k", "a@b.com"]]);
    const out = redactMetadata({ when, map, email: "reach a@b.com" });
    expect(out.when).toBe(when); // same Date instance, not {}
    expect(out.map).toBe(map);
    expect(out.email).toBe("reach [REDACTED_EMAIL]");
  });

  it("is idempotent (re-redaction is a no-op) and respects enabled:false", () => {
    const once = redactMetadata({ a: "a@b.com" });
    expect(redactMetadata(once)).toEqual(once);
    expect(redactMetadata({ a: "a@b.com" }, { enabled: false })).toEqual({ a: "a@b.com" });
  });

  it("applies custom rules AFTER the built-in catalog (#188 D188-4)", () => {
    const out = redactMetadata("email jane@example.com case CASE-123456", {
      customRules: [{ name: "case_id", pattern: "CASE-\\d{6}", replacement: "[REDACTED_CASE]" }],
    });
    expect(out).toBe("email [REDACTED_EMAIL] case [REDACTED_CASE]");
  });

  it("replaces EVERY occurrence of a custom rule (global flag)", () => {
    const out = redactMetadata("CASE-111111 and CASE-222222", {
      customRules: [{ name: "case_id", pattern: "CASE-\\d{6}", replacement: "[X]" }],
    });
    expect(out).toBe("[X] and [X]");
  });

  it("keeps DEFAULT_EXCLUDED_PATHS winning over a greedy custom rule", () => {
    const out = redactMetadata(
      { typeflux: { workflow: { workflow_id: "wf-123" } }, user_note: "CASE-999999" },
      { customRules: [{ name: "all", pattern: ".+", replacement: "[X]" }] },
    );
    expect(out).toEqual({
      typeflux: { workflow: { workflow_id: "wf-123" } }, // governance metadata preserved
      user_note: "[X]",
    });
  });

  it("treats a custom replacement LITERALLY — `\\1` and `$1` are not interpreted (#188 parity)", () => {
    // PARITY with the Python suite (test_redaction.py
    // `test_custom_rule_replacement_cross_edition_parity_literal`): identical fixture,
    // identical output. The replacer FUNCTION return is never `$`-interpreted, and there is
    // no re-injection of the captured `(\d{6})` — the PII must be absent from the output.
    const out = redactMetadata("ref CASE-123456 end", {
      emails: false,
      phones: false,
      ssn: false,
      creditCards: false,
      customRules: [{ name: "case", pattern: "CASE-(\\d{6})", replacement: "[C \\1 $1]" }],
    });
    expect(out).toBe("ref [C \\1 $1] end");
    expect(out).not.toContain("123456");
  });

  it("memoizes compiled rules per config-object identity — buildRules runs once (#188)", () => {
    // The compiled-rule cache is keyed on config IDENTITY. Proof: mutating the SAME config
    // object after the first call is ignored (cached rules reused, no rebuild), while a
    // DISTINCT object rebuilds and reflects the mutation.
    const rule = { name: "case", pattern: "CASE-\\d+", replacement: "[C]" };
    const config = { customRules: [rule] };
    expect(redactMetadata("CASE-123", config)).toBe("[C]");
    rule.replacement = "[CHANGED]";
    expect(redactMetadata("CASE-123", config)).toBe("[C]"); // same identity → cached
    expect(redactMetadata("CASE-123", { ...config })).toBe("[CHANGED]"); // new object → rebuilt
  });
});
