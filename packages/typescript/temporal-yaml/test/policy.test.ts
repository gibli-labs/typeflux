import { describe, expect, it } from "vitest";

import { loadPolicySpec, typefluxProjectPolicySpec } from "../src/index.js";

const MINIMAL = `
version: "1"
name: baseline
`;

describe("loadPolicySpec (#454 — Python TypefluxProjectPolicySpec parity)", () => {
  it("loads a minimal policy with the empty defaults for every governed block", () => {
    const policy = loadPolicySpec(MINIMAL);
    expect(policy.version).toBe("1");
    expect(policy.name).toBe("baseline");
    expect(policy.extends).toEqual([]);
    // Absent blocks default to empty strict objects (a policy governs only what it declares).
    expect(policy.providers).toEqual({});
    expect(policy.observability.redaction).toEqual({});
    expect(policy.runtime.temporal).toEqual({});
    expect(policy.runtime.registry).toEqual({});
    expect(policy.runtime.provider_limits).toBeUndefined();
  });

  it("version defaults to '1' when omitted", () => {
    expect(loadPolicySpec("name: p").version).toBe("1");
  });

  it("loads a full policy across every governed block", () => {
    const policy = loadPolicySpec(`
name: regulated
description: the strict tier
extends: [baseline, tenant-a]
providers:
  allowed:
    openai:
      models: [gpt-4o-mini]
    anthropic: {}
observability:
  required: true
  allowed_backends: [langfuse]
  redaction:
    required: true
    preserve_typeflux_metadata: true
runtime:
  temporal:
    allowed_namespaces: [prod]
    require_tls: true
  registry:
    allowed_hosts: [cloud.langfuse.com]
  provider_retry:
    max_attempts: 3
  provider_limits:
    default:
      max_concurrent: 4
    providers:
      openai:
        models:
          gpt-4o-mini:
            max_concurrent: 1
            min_interval_seconds: 0.25
artifacts:
  allowed_sources: [local_path, provider_file]
  max_bytes: 1500000
review:
  require_review_routes: true
  invalid_user_decision: fail
semantics:
  required: true
  require_block: true
  categories: [violence, self_harm]
  score_threshold: 0.5
imports:
  allow_provider_class: false
  allowed_module_roots: [my_project]
secrets:
  require_secret_references: true
`);
    expect(policy.extends).toEqual(["baseline", "tenant-a"]);
    expect(policy.providers.allowed?.["openai"]?.models).toEqual(["gpt-4o-mini"]);
    expect(policy.runtime.provider_limits?.providers["openai"]?.models["gpt-4o-mini"]).toEqual({
      max_concurrent: 1,
      min_interval_seconds: 0.25,
    });
    expect(policy.artifacts.allowed_sources).toEqual(["local_path", "provider_file"]);
    expect(policy.review.invalid_user_decision).toBe("fail");
    expect(policy.semantics.score_threshold).toBe(0.5);
  });

  it("rejects unknown keys (strict — a typo must not silently widen an allowance)", () => {
    expect(() => loadPolicySpec("name: p\nproivders: {}")).toThrow(/invalid project policy/);
    expect(() => loadPolicySpec("name: p\nproviders:\n  allow: {}")).toThrow(/invalid project policy/);
  });

  it("enforces Python's field constraints", () => {
    expect(() => loadPolicySpec("name: p\nruntime:\n  provider_retry:\n  max_attempts: 0")).toThrow();
    expect(() => loadPolicySpec("name: p\nruntime: { provider_retry: { max_attempts: 0 } }")).toThrow(/max_attempts/);
    expect(() => loadPolicySpec("name: p\nartifacts: { max_bytes: -1 }")).toThrow(/max_bytes/);
    expect(() => loadPolicySpec("name: p\nsemantics: { score_threshold: 1.5 }")).toThrow(/score_threshold/);
    expect(() => loadPolicySpec("name: p\nruntime: { provider_limits: { default: { max_concurrent: 0 } } }")).toThrow(
      /max_concurrent/,
    );
    // A path-shaped policy id in extends is rejected.
    expect(() => loadPolicySpec("name: p\nextends: ['a/b']")).toThrow(/local project references/);
    // Enum members are validated.
    expect(() => loadPolicySpec("name: p\nartifacts: { allowed_sources: [ftp] }")).toThrow();
    expect(() => loadPolicySpec("name: p\nreview: { invalid_user_decision: maybe }")).toThrow();
    // A blank name is not trimmed/non-empty.
    expect(() => loadPolicySpec("name: '  '")).toThrow(/non-empty and trimmed/);
  });

  it("rejects blank/untrimmed record KEYS — a typo'd key must not become a silent no-op (Python parity)", () => {
    // Provider allowlist, provider-limit provider + model, and address_regions keys.
    expect(() => loadPolicySpec("name: p\nproviders: { allowed: { '  ': {} } }")).toThrow(/invalid project policy/);
    expect(() =>
      loadPolicySpec("name: p\nruntime: { provider_limits: { providers: { ' openai': {} } } }"),
    ).toThrow(/invalid project policy/);
    expect(() =>
      loadPolicySpec("name: p\nruntime: { provider_limits: { providers: { openai: { models: { ' gpt': {} } } } } }"),
    ).toThrow(/invalid project policy/);
    expect(() =>
      loadPolicySpec("name: p\nruntime: { temporal: { address_regions: { '': us-east } } }"),
    ).toThrow(/invalid project policy/);
    // A well-formed key is still accepted.
    expect(loadPolicySpec("name: p\nproviders: { allowed: { openai: {} } }").providers.allowed?.["openai"]).toEqual({});
  });

  it("rejects duplicate mapping keys — top-level AND nested (no silent last-win)", () => {
    expect(() => loadPolicySpec("name: p\nname: q")).toThrow(/invalid policy YAML/i);
    // A NESTED duplicate is the one the yaml lib surfaces as a warning; it must
    // still fail, or the effective policy differs from what the author read.
    expect(() =>
      loadPolicySpec("name: p\nartifacts:\n  max_bytes: 1\n  max_bytes: 2"),
    ).toThrow(/invalid policy YAML/i);
    expect(() => loadPolicySpec("name: p\n")).not.toThrow();
  });

  it("rejects a non-mapping document", () => {
    expect(() => loadPolicySpec("- a\n- b")).toThrow(/must be a mapping/);
    expect(() => loadPolicySpec("")).toThrow(/empty policy document/);
  });

  it("the exported schema parses the same as the loader for an already-parsed object", () => {
    const parsed = typefluxProjectPolicySpec.parse({ name: "baseline" });
    expect(parsed.name).toBe("baseline");
    expect(parsed.version).toBe("1");
  });
});

describe("prototype-key hardening (#454; assertSafeKeys parity with loadYamlSpec)", () => {
  it("rejects a top-level __proto__ key that zod's strict schema would silently ignore", () => {
    expect(() => loadPolicySpec("name: p\n__proto__: { x: 1 }\n")).toThrow(/unsafe key '__proto__'/);
    expect(({} as Record<string, unknown>).x).toBeUndefined(); // Object.prototype untouched
  });

  it("rejects a nested __proto__ key, naming its path", () => {
    expect(() => loadPolicySpec("name: p\nproviders: { allowed: { __proto__: {} } }\n")).toThrow(
      /unsafe key '__proto__'.*providers\.allowed/,
    );
  });

  it("ACCEPTS a provider legitimately named 'constructor' (an open-dict key; Python parity)", () => {
    // `providers.allowed` is an open record — a provider so named is valid, not rejected.
    expect(() => loadPolicySpec("name: p\nproviders: { allowed: { constructor: { models: [m] } } }\n")).not.toThrow();
  });
});
