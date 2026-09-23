import { describe, expect, it } from "vitest";

import { loadProfileSpec, profileContentHash } from "../src/profile.js";

const PROVIDER_PROFILE =
  "name: anthropic-prod\nkind: provider\nruntime:\n  provider:\n    type: anthropic\n" +
  "    model: claude-sonnet-4-6\n    api_key: { value_from: { env: ANTHROPIC_API_KEY } }\n";

describe("loadProfileSpec (#570; Python load_project_profile)", () => {
  it("loads a provider profile and computes a stable 64-hex content hash", () => {
    const profile = loadProfileSpec(PROVIDER_PROFILE, { declaredKind: "provider" });
    expect(profile.name).toBe("anthropic-prod");
    expect(profile.kind).toBe("provider");
    const hash = profileContentHash(profile);
    expect(hash).toMatch(/^[0-9a-f]{64}$/);
    // Stability: reloading identical content hashes identically.
    expect(profileContentHash(loadProfileSpec(PROVIDER_PROFILE))).toBe(hash);
  });

  it("hashes byte-identically to Python's ProjectProfileSpec.content_hash", () => {
    // Pinned from the live Pydantic model over the same payloads — the cross-SDK contract:
    // the same profile file must report the same content_hash from either control plane.
    expect(profileContentHash(loadProfileSpec(PROVIDER_PROFILE))).toBe(
      "3a703ba5fc5852c7a78cb49cdadab8cc26b603aff2930cb020896bab21648386",
    );
    // Integral floats neutralize (canonicalJson #391): Python 3.0 and TS 3 agree.
    const hardened = loadProfileSpec(
      "name: hardened\nkind: runtime\nruntime:\n  provider_retry:\n    max_attempts: 3.0\n",
    );
    expect(profileContentHash(hardened)).toBe(
      "354acef93e535a3ebb924432bbe247eb11dd3fada20c3989daf2300d43b17a14",
    );
  });

  it("hashes 1.2-core ambiguous scalars identically to Python (#602)", () => {
    // Pinned from the live Python loader AFTER its move to YAML 1.2 core scalars: `on`
    // stays a string, 0x1A -> 26, 019 -> decimal 19, `2.` -> 2.0 — both SDKs read the
    // same VALUES from this file, so the cross-SDK content_hash agrees on it too.
    const zoo = loadProfileSpec(
      'version: "1"\nname: scalar-zoo\nkind: runtime\nruntime:\n  temporal:\n' +
        "    namespace: on\n    api_key_hint: 0x1A\n  provider_retry:\n" +
        "    max_attempts: 019\n    initial_backoff_seconds: 2.\n",
    );
    expect(zoo.runtime).toEqual({
      temporal: { namespace: "on", api_key_hint: 26 },
      provider_retry: { max_attempts: 19, initial_backoff_seconds: 2 },
    });
    expect(profileContentHash(zoo)).toBe(
      "12959f31145c266095ef5bea7eab6b39a8d2ef9ef17d3919f041adf6c5deb01b",
    );
  });

  it("rejects a profile referenced under one kind that declares another (Python message shape)", () => {
    expect(() => loadProfileSpec(PROVIDER_PROFILE, { sourceLabel: "p.yaml", declaredKind: "runtime" })).toThrow(
      /referenced under profiles\.runtime but declares kind: provider/,
    );
  });

  it("rejects runtime keys outside the kind's owned subtree", () => {
    expect(() =>
      loadProfileSpec("name: p\nkind: provider\nruntime:\n  temporal: { namespace: prod }\n"),
    ).toThrow(/outside its owned subtree: temporal.*owned keys: provider/s);
  });

  it("accepts a runtime-kind profile across all four of its owned subtrees", () => {
    // The owned keys are a subset of the override allowlist, so the allowlist re-run is
    // belt-and-braces (Python parity) — a well-formed fragment passes both layers.
    const profile = loadProfileSpec(
      "name: hardened\nkind: runtime\nruntime:\n  temporal: { namespace: prod }\n" +
        "  observability: { backend: langfuse }\n  provider_retry: { max_attempts: 3 }\n" +
        "  provider_limits: { default: { max_concurrent: 2 } }\n",
      { declaredKind: "runtime" },
    );
    expect(Object.keys(profile.runtime).sort()).toEqual([
      "observability",
      "provider_limits",
      "provider_retry",
      "temporal",
    ]);
  });

  it("rejects empty and non-mapping documents", () => {
    expect(() => loadProfileSpec("", { sourceLabel: "x.yaml" })).toThrow(/empty component profile: x\.yaml/);
    expect(() => loadProfileSpec("- a\n- b\n", { sourceLabel: "x.yaml" })).toThrow(
      /component profile must be a YAML mapping/,
    );
  });
});
