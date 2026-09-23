import { describe, expect, it } from "vitest";

import { type EmittedTrace, TraceWriter } from "@typeflux/temporal";

import { loadYamlSpec, observerFromSpec, redactionFromSpec } from "../src/index.js";

const transport = { submitTrace: (_t: EmittedTrace) => {} };

function specWith(observability: string): string {
  return `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider: { type: openai }
${observability}
activities: {}
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
}

describe("observerFromSpec (#451)", () => {
  it("builds a TraceWriter for a tracing backend (langfuse)", () => {
    const spec = loadYamlSpec(specWith("  observability: { type: langfuse }"));
    expect(observerFromSpec(spec, transport)).toBeInstanceOf(TraceWriter);
  });

  it("returns undefined for type `none`", () => {
    const spec = loadYamlSpec(specWith("  observability: { type: none }"));
    expect(observerFromSpec(spec, transport)).toBeUndefined();
  });

  it("redacts PII by default when a tracing backend omits the redaction block", async () => {
    const captured: EmittedTrace[] = [];
    const writer = observerFromSpec(loadYamlSpec(specWith("  observability: { type: langfuse }")), {
      submitTrace: (t) => {
        captured.push(t);
      },
    });
    const observation = writer!.observeActivity({
      activityName: "a",
      input: { email: "reach a@b.com" },
      messages: [],
      model: null,
      tenant: {},
    });
    observation.updateOutput({ note: "ssn 123-45-6789" });
    observation.end();
    await writer!.flush();
    // Default redaction is ON (Python parity) -> no raw PII reaches the backend.
    expect(captured[0]!.input).toEqual({ email: "reach [REDACTED_EMAIL]" });
    expect(captured[0]!.output).toEqual({ note: "ssn [REDACTED_SSN]" });
  });

  it("returns undefined when observability is unset", () => {
    const spec = loadYamlSpec(specWith(""));
    expect(observerFromSpec(spec, transport)).toBeUndefined();
  });

  it("throws on a misspelled/unsupported observability type (fails loud)", () => {
    const spec = loadYamlSpec(specWith("  observability: { type: langfues }"));
    expect(() => observerFromSpec(spec, transport)).toThrow(/unsupported observability type "langfues"/);
  });
});

describe("redactionFromSpec (#451)", () => {
  it("maps the spec redaction (snake_case) to a RedactionConfig (camelCase)", () => {
    const spec = loadYamlSpec(
      specWith("  observability: { type: langfuse, redaction: { emails: false, credit_cards: false, exclude_paths: [my.path] } }"),
    );
    expect(redactionFromSpec(spec)).toEqual({ emails: false, creditCards: false, excludePaths: ["my.path"] });
  });

  it("returns undefined when no redaction is configured", () => {
    const spec = loadYamlSpec(specWith("  observability: { type: langfuse }"));
    expect(redactionFromSpec(spec)).toBeUndefined();
  });

  it("threads custom_rules through to the RedactionConfig (#188 D188-4)", () => {
    const spec = loadYamlSpec(
      specWith(
        "  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: case_id, pattern: 'CASE-\\d{6}', replacement: '[REDACTED_CASE]' }",
      ),
    );
    expect(redactionFromSpec(spec)?.customRules).toEqual([
      { name: "case_id", pattern: "CASE-\\d{6}", replacement: "[REDACTED_CASE]" },
    ]);
  });

  it("fails spec load fail-closed on an invalid custom-rule regex (#188 D188-4)", () => {
    expect(() =>
      loadYamlSpec(
        specWith(
          "  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: bad, pattern: 'CASE-(\\d{6}', replacement: '[X]' }",
        ),
      ),
    ).toThrow(/not a valid regex/);
  });

  it("rejects duplicate custom-rule names", () => {
    expect(() =>
      loadYamlSpec(
        specWith(
          "  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: dup, pattern: 'a', replacement: '[A]' }\n        - { name: dup, pattern: 'b', replacement: '[B]' }",
        ),
      ),
    ).toThrow(/names must be unique/);
  });

  it("rejects an untrimmed custom-rule name at load (policy membership is trim-exact) (#188)", () => {
    // A trailing space would load but then fail admission with a misleading missing-rule
    // error — reject it at the spec boundary instead. Replacement whitespace stays legal.
    expect(() =>
      loadYamlSpec(
        specWith(
          "  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: 'case_reference ', pattern: 'a', replacement: '[A]' }",
        ),
      ),
    ).toThrow(/name must be non-empty and trimmed/);
  });

  it("rejects a whitespace-only custom-rule replacement at load (Python parity) (#188)", () => {
    // Python's `_validate_replacement` rejects a replacement that is empty after strip;
    // the same portable YAML must fail identically here. Inner padding (" [X] ") stays legal.
    expect(() =>
      loadYamlSpec(
        specWith(
          "  observability:\n    type: langfuse\n    redaction:\n      custom_rules:\n        - { name: case_reference, pattern: 'a', replacement: '   ' }",
        ),
      ),
    ).toThrow(/replacement must be non-empty/);
  });
});
