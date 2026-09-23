import { describe, expect, it } from "vitest";

import {
  composeProjectPolicies,
  loadPolicySpec,
  ProjectPolicyError,
  type AppliedPolicy,
} from "../src/index.js";

/** Build an applied-policy entry from inline YAML. */
const applied = (id: string, yaml: string): AppliedPolicy => ({ id, spec: loadPolicySpec(`name: ${id}\n${yaml}`) });

/** Navigate a nested record payload without fighting noUncheckedIndexedAccess. */
const dig = (obj: unknown, ...keys: string[]): unknown =>
  keys.reduce<unknown>((o, k) => (o as Record<string, unknown> | undefined)?.[k], obj);

const compose = (entries: AppliedPolicy[]) =>
  composeProjectPolicies(entries, entries.map((e) => e.id));

describe("composeProjectPolicies (#454 — monotonic most-restrictive merge)", () => {
  it("a single policy composes to its own constraints + identity", () => {
    const result = compose([applied("base", "artifacts: { max_bytes: 1000 }")]);
    expect(result.appliedPolicyIds).toEqual(["base"]);
    expect(result.policyNames).toEqual(["base"]);
    expect(result.payload).toMatchObject({ version: "1", artifacts: { max_bytes: 1000 } });
    expect(result.policyHash).toMatch(/^[0-9a-f]{64}$/);
  });

  it("required-flags OR (a requirement, once imposed, stays imposed)", () => {
    const result = compose([
      applied("org", "observability: { required: false }"),
      applied("tenant", "observability: { required: true }"),
    ]);
    expect(dig(result.payload, "observability", "required")).toBe(true);
  });

  it("allow-flags AND (an allowance survives only if BOTH layers permit it)", () => {
    const result = compose([
      applied("org", "imports: { allow_provider_class: true }"),
      applied("tenant", "imports: { allow_provider_class: false }"),
    ]);
    expect(dig(result.payload, "imports", "allow_provider_class")).toBe(false);
  });

  it("bounded numerics take the stricter side (max_bytes→min, min_interval→max, threshold→min)", () => {
    const result = compose([
      applied("org", "artifacts: { max_bytes: 2000 }\nsemantics: { score_threshold: 0.8 }"),
      applied("tenant", "artifacts: { max_bytes: 500 }\nsemantics: { score_threshold: 0.3 }"),
    ]);
    expect(dig(result.payload, "artifacts", "max_bytes")).toBe(500);
    expect(dig(result.payload, "semantics", "score_threshold")).toBe(0.3);
  });

  it("provider-model allow-lists INTERSECT, never union", () => {
    const result = compose([
      applied("org", "providers: { allowed: { openai: { models: [gpt-4o, gpt-4o-mini] } } }"),
      applied("tenant", "providers: { allowed: { openai: { models: [gpt-4o-mini, o1] } } }"),
    ]);
    expect(dig(result.payload, "providers", "allowed", "openai", "models")).toEqual(["gpt-4o-mini"]);
  });

  it("a provider allow-list intersects to the shared PROVIDERS (drops one the tenant omits)", () => {
    const result = compose([
      applied("org", "providers: { allowed: { openai: {}, anthropic: {} } }"),
      applied("tenant", "providers: { allowed: { openai: {} } }"),
    ]);
    expect(Object.keys(dig(result.payload, "providers", "allowed") as Record<string, unknown>)).toEqual(["openai"]);
  });

  it("two disjoint allow-lists are a CONFLICT, not a silent empty (fail-closed loudly)", () => {
    expect(() =>
      compose([
        applied("org", "providers: { allowed: { openai: { models: [gpt-4o] } } }"),
        applied("tenant", "providers: { allowed: { openai: { models: [o1] } } }"),
      ]),
    ).toThrow(ProjectPolicyError);
    expect(() =>
      compose([
        applied("org", "runtime: { registry: { allowed_hosts: [a.com] } }"),
        applied("tenant", "runtime: { registry: { allowed_hosts: [b.com] } }"),
      ]),
    ).toThrow(/no overlapping/);
  });

  it("contradictory scalars with no restrictive direction hard-fail", () => {
    expect(() =>
      compose([
        applied("org", "runtime: { temporal: { require_tls: true } }"),
        // require_tls is a require_* bool → ORs, so pick a genuinely conflicting scalar instead:
        applied("tenant", "review: { invalid_user_decision: warn }"),
        applied("other", "review: { invalid_user_decision: fail }"),
      ]),
    ).toThrow(/conflicting project policy values/);
  });

  it("the hash is deterministic and order-insensitive over the id lists", () => {
    const a = applied("org", "artifacts: { max_bytes: 100 }");
    const b = applied("tenant", "observability: { required: true }");
    // Same effective payload, different id ordering → same hash (ids sorted for hashing).
    const h1 = composeProjectPolicies([a, b], ["org", "tenant"]).policyHash;
    const h2 = composeProjectPolicies([a, b], ["tenant", "org"]).policyHash;
    expect(h1).toBe(h2);
    // A real constraint change moves the hash.
    const c = applied("tenant", "observability: { required: false }");
    expect(composeProjectPolicies([a, c], ["org", "tenant"]).policyHash).not.toBe(h1);
  });

  it("a provider named like an Object.prototype key composes by OWN keys, not the prototype (/code-review)", () => {
    // Both policies legitimately name a provider "constructor" → intersect its models.
    const both = compose([
      applied("org", "providers: { allowed: { constructor: { models: [a, b] } } }"),
      applied("tenant", "providers: { allowed: { constructor: { models: [b] } } }"),
    ]);
    expect(dig(both.payload, "providers", "allowed", "constructor", "models")).toEqual(["b"]);
    // Disjoint providers where one is a prototype name → a real conflict (no false
    // intersect via the inherited member).
    expect(() =>
      compose([
        applied("org", "providers: { allowed: { openai: {} } }"),
        applied("tenant", "providers: { allowed: { toString: {} } }"),
      ]),
    ).toThrow(/no overlapping keys/);
  });

  it("an empty policy list is rejected", () => {
    expect(() => composeProjectPolicies([], [])).toThrow(/at least one project policy id/);
    // An empty resolved closure for a non-empty request is fail-OPEN — refuse it.
    expect(() => composeProjectPolicies([], ["org"])).toThrow(/no policies resolved/);
  });

  it("a non-governing policy contributes nothing (unset ≠ override)", () => {
    // The tenant governs only artifacts; it must not erase the org's provider allow-list.
    const result = compose([
      applied("org", "providers: { allowed: { openai: { models: [gpt-4o] } } }"),
      applied("tenant", "artifacts: { max_bytes: 10 }"),
    ]);
    expect(dig(result.payload, "providers", "allowed", "openai", "models")).toEqual(["gpt-4o"]);
    expect(dig(result.payload, "artifacts", "max_bytes")).toBe(10);
  });
});
