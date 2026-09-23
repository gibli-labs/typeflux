import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../src/index.js";
import { renderMessages, renderTemplate } from "../src/index.js";

// Direct edge-case tests for render.ts (#448). prompt-resolution.test.ts covers the
// happy path (basic substitution, bool formatting, prototype-member rejection) — this
// suite covers grammar boundaries, escaping, and content-part rendering.

describe("renderTemplate substitution", () => {
  it("substitutes repeated and adjacent placeholders", () => {
    expect(renderTemplate("{{ a }}{{ a }}-{{ b }}", { a: "x", b: "y" })).toBe("xx-y");
  });

  it("accepts whitespace-flexible braces and deep dot paths", () => {
    expect(renderTemplate("{{name}}|{{  name  }}", { name: "n" })).toBe("n|n");
    expect(renderTemplate("{{ a.b.c }}", { a: { b: { c: "deep" } } })).toBe("deep");
  });

  it("renders numbers via String (0 and floats included)", () => {
    expect(renderTemplate("{{ n }}/{{ f }}", { n: 0, f: 3.5 })).toBe("0/3.5");
    expect(renderTemplate("{{ u }}", { u: undefined })).toBe(""); // like null
  });

  it("leaves non-matching syntax literal (invalid identifiers, single braces)", () => {
    expect(renderTemplate("{ name }", { name: "x" })).toBe("{ name }");
    expect(renderTemplate("{{ 1bad }}", { "1bad": "x" })).toBe("{{ 1bad }}");
    expect(renderTemplate("{{ a-b }}", { "a-b": "x" })).toBe("{{ a-b }}");
    expect(renderTemplate("{{ a..b }}", {})).toBe("{{ a..b }}");
  });

  it("is plain-text substitution: values round-trip byte-for-byte", () => {
    // No recursive expansion — a substituted value containing {{...}} stays literal.
    expect(renderTemplate("{{ v }}", { v: "{{ other }}", other: "boom" })).toBe("{{ other }}");
    // No String.replace $-pattern injection ($& = "matched substring", $' = "suffix").
    expect(renderTemplate("{{ v }}!", { v: "$&$'" })).toBe("$&$'!");
    // No HTML escaping.
    expect(renderTemplate("{{ v }}", { v: "<b>&\"</b>" })).toBe("<b>&\"</b>");
  });

  it("throws the full dot path when an intermediate segment is missing or unwalkable", () => {
    expect(() => renderTemplate("{{ a.b.c }}", { a: { b: {} } })).toThrow(/missing prompt field: a\.b\.c/);
    expect(() => renderTemplate("{{ a.b }}", { a: null })).toThrow(/missing prompt field: a\.b/);
    expect(() => renderTemplate("{{ a.b }}", { a: "scalar" })).toThrow(/missing prompt field: a\.b/);
    // Arrays are not addressable path segments.
    expect(() => renderTemplate("{{ a.b }}", { a: [{ b: 1 }] })).toThrow(/missing prompt field: a\.b/);
  });
});

describe("renderMessages", () => {
  it("renders every message and does not mutate the originals", () => {
    const messages: ChatMessage[] = [
      { role: "system", content: "You review {{ kind }}." },
      { role: "user", content: "Item: {{ id }}" },
    ];
    const rendered = renderMessages(messages, { kind: "claims", id: "C-1" });
    expect(rendered).toEqual([
      { role: "system", content: "You review claims." },
      { role: "user", content: "Item: C-1" },
    ]);
    expect(messages[0]?.content).toBe("You review {{ kind }}."); // input untouched
    expect(rendered[0]).not.toBe(messages[0]);
  });

  it("renders only the text of content parts; non-text parts pass through", () => {
    const messages: ChatMessage[] = [
      {
        role: "user",
        content: [
          { type: "text", text: "Claim {{ id }}" },
          { type: "artifact", artifact: "doc-1", text: "caption {{ id }}" },
          { type: "artifact", artifact: "doc-2" }, // no text -> untouched
          { type: "provider_extension", provider: "gemini", payload: { data: "{{ id }}" } },
        ],
      },
    ];
    const [rendered] = renderMessages(messages, { id: "C-9" });
    expect(rendered?.content).toEqual([
      { type: "text", text: "Claim C-9" },
      { type: "artifact", artifact: "doc-1", text: "caption C-9" },
      { type: "artifact", artifact: "doc-2" },
      // A provider-extension payload is provider-native and never templated.
      { type: "provider_extension", provider: "gemini", payload: { data: "{{ id }}" } },
    ]);
  });

  it("propagates a missing-field error from any message", () => {
    const messages: ChatMessage[] = [
      { role: "system", content: "fine" },
      { role: "user", content: "{{ nope }}" },
    ];
    expect(() => renderMessages(messages, {})).toThrow(/missing prompt field: nope/);
  });

  it("renders object/array values as JSON, never '[object Object]' (#455)", () => {
    // Python str() gives a content-bearing repr; JSON is the TS behavioral
    // equivalent — a collected batch templated into a consolidate prompt must
    // reach the model as readable structure.
    const reviews = [
      { evidence_id: "EV-1", decision: "support" },
      { evidence_id: "EV-2", decision: "needs_follow_up" },
    ];
    const rendered = renderTemplate("Reviews:\n{{ reviews }}", { reviews });
    expect(rendered).toContain('"evidence_id":"EV-2"');
    expect(rendered).not.toContain("[object Object]");
    expect(renderTemplate("{{ nested }}", { nested: { a: 1 } })).toBe('{"a":1}');
    // A Date renders as bare ISO text, not a JSON-quoted string.
    expect(renderTemplate("{{ at }}", { at: new Date("2026-05-21T14:30:00Z") })).toBe(
      "2026-05-21T14:30:00.000Z",
    );
  });
});
