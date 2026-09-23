import { describe, expect, it } from "vitest";

import type { PromptRef, ResolvedPrompt } from "../src/index.js";
import { InlinePromptRegistry, PromptNotFoundError, promptRefToDict } from "../src/index.js";

// Direct tests for prompt-ref.ts (#390) plus the InlinePromptRegistry edge cases
// prompt-resolution.test.ts leaves untested (it covers value-shape normalization and
// the bare not-found throw). Note: InlinePromptRegistry has no register() method —
// registration is constructor-only, so "overwrite" is last-key-wins on the record.

describe("promptRefToDict", () => {
  it("serializes name-only refs with explicit null version/label", () => {
    expect(promptRefToDict({ name: "p/classify" })).toEqual({
      name: "p/classify",
      version: null,
      label: null,
    });
  });

  it("carries a pinned version or a label through", () => {
    expect(promptRefToDict({ name: "p", version: 3 })).toEqual({ name: "p", version: 3, label: null });
    expect(promptRefToDict({ name: "p", label: "canary" })).toEqual({ name: "p", label: "canary", version: null });
    // version 0 is a real pin, not "unset".
    expect(promptRefToDict({ name: "p", version: 0 }).version).toBe(0);
  });

  it("throws when version and label are both pinned (mutually exclusive)", () => {
    expect(() => promptRefToDict({ name: "p", version: 1, label: "production" })).toThrow(
      /mutually exclusive/,
    );
    // Explicit nulls do not count as pinned.
    expect(promptRefToDict({ name: "p", version: null, label: null })).toEqual({
      name: "p",
      version: null,
      label: null,
    });
  });

  it("never serializes the resolve-time promptType hint", () => {
    const dict = promptRefToDict({ name: "p", promptType: "chat" });
    expect(dict).toEqual({ name: "p", version: null, label: null });
    expect("promptType" in dict).toBe(false);
  });

  it("round-trips: the wire dict is itself a valid PromptRef and re-serializes identically", () => {
    const dict = promptRefToDict({ name: "p", version: 2, promptType: "text" });
    const roundTripped = promptRefToDict(dict as PromptRef);
    expect(roundTripped).toEqual({ name: "p", version: 2, label: null });
  });
});

describe("InlinePromptRegistry edge cases", () => {
  it("throws PromptNotFoundError carrying the unresolved ref", () => {
    const registry = new InlinePromptRegistry({ "p/known": "hi" });
    const ref: PromptRef = { name: "p/unknown", label: "production" };
    let caught: unknown;
    try {
      registry.resolve(ref);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(PromptNotFoundError);
    expect((caught as PromptNotFoundError).ref).toBe(ref);
    expect((caught as PromptNotFoundError).message).toBe("prompt not found: p/unknown");
    expect((caught as PromptNotFoundError).name).toBe("PromptNotFoundError");
  });

  // No duplicate-key test: registration is constructor-only via a Record, so
  // duplicate keys collapse in JavaScript before the registry ever sees them.

  it("derives resolvedVersion from the ref's pinned version, null otherwise", () => {
    const registry = new InlinePromptRegistry({ "p/x": "go" });
    expect(registry.resolve({ name: "p/x", version: 7 }).resolvedVersion).toBe("7");
    expect(registry.resolve({ name: "p/x" }).resolvedVersion).toBeNull();
    // A label selects a mutable pointer; it never fabricates a version.
    expect(registry.resolve({ name: "p/x", label: "canary" }).resolvedVersion).toBeNull();
  });

  it("returns a stored ResolvedPrompt as-is, ignoring the incoming ref's version", () => {
    const stored: ResolvedPrompt = {
      ref: { name: "p/full" },
      messages: [{ role: "user", content: "r" }],
      model: "preset",
    };
    const registry = new InlinePromptRegistry({ "p/full": stored });
    const resolved = registry.resolve({ name: "p/full", version: 9 });
    expect(resolved).toBe(stored); // passthrough, not a rebuilt prompt
    expect(resolved.resolvedVersion).toBeUndefined(); // no version injected
  });

  it("normalizes an empty-string prompt to a single empty user message", () => {
    const registry = new InlinePromptRegistry({ "p/empty": "" });
    expect(registry.resolve({ name: "p/empty" }).messages).toEqual([{ role: "user", content: "" }]);
  });
});
