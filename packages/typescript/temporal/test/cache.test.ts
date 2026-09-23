import { describe, expect, it } from "vitest";

import type { CacheKey } from "../src/index.js";
import { cacheInputHash, cacheKey, cacheKeyDigest, cacheRecord } from "../src/index.js";

// Direct API-surface tests for cache.ts (#391). Python-parity of the actual hash
// VALUES is covered by the golden-file checks in conformance.test.ts — here we test
// construction semantics, defaulting, optional-field handling, and hash structure.

const HEX64 = /^[0-9a-f]{64}$/;

describe("cacheKey", () => {
  it("defaults scope to global (empty) and carries the inputs through", () => {
    expect(cacheKey("classify", "hash-1")).toEqual({
      activity: "classify",
      input_hash: "hash-1",
      scope: {},
    });
  });

  it("copies the scope so later caller mutation cannot alias the key", () => {
    const scope = { company_id: "co-1" };
    const key = cacheKey("classify", "hash-1", scope);
    scope.company_id = "co-2";
    expect(key.scope).toEqual({ company_id: "co-1" });
  });
});

describe("cacheKeyDigest", () => {
  const base: CacheKey = { activity: "a", input_hash: "h", scope: { t: "1" } };

  it("is a sha256 hex digest, stable for equal keys", () => {
    const digest = cacheKeyDigest(base);
    expect(digest).toMatch(HEX64);
    expect(cacheKeyDigest({ ...base, scope: { t: "1" } })).toBe(digest);
  });

  it("changes when any component changes (activity, input_hash, scope)", () => {
    const digest = cacheKeyDigest(base);
    expect(cacheKeyDigest({ ...base, activity: "b" })).not.toBe(digest);
    expect(cacheKeyDigest({ ...base, input_hash: "h2" })).not.toBe(digest);
    expect(cacheKeyDigest({ ...base, scope: { t: "2" } })).not.toBe(digest);
    expect(cacheKeyDigest({ ...base, scope: {} })).not.toBe(digest);
  });
});

describe("cacheInputHash", () => {
  const base = {
    activity: "classify",
    inputSchemaHash: "in-schema",
    renderedMessagesHash: "rendered",
  };

  it("treats omitted, null, and empty providerParams identically (all -> {})", () => {
    const omitted = cacheInputHash(base);
    expect(omitted).toMatch(HEX64);
    expect(cacheInputHash({ ...base, providerParams: null })).toBe(omitted);
    expect(cacheInputHash({ ...base, providerParams: {} })).toBe(omitted);
  });

  it("changes when any call-determining input changes", () => {
    const hash = cacheInputHash(base);
    expect(cacheInputHash({ ...base, activity: "other" })).not.toBe(hash);
    expect(cacheInputHash({ ...base, inputSchemaHash: "x" })).not.toBe(hash);
    expect(cacheInputHash({ ...base, renderedMessagesHash: "x" })).not.toBe(hash);
    expect(cacheInputHash({ ...base, providerParams: { temperature: 1 } })).not.toBe(hash);
  });
});

describe("cacheRecord", () => {
  const key: CacheKey = { activity: "classify", input_hash: "h", scope: { company_id: "co-1" } };
  const required = {
    key,
    output: { label: "billing" },
    createdAt: "2026-01-01T00:00:00Z",
    outputSchemaHash: "out-schema",
  };

  it("omits optional provenance keys entirely when not provided (not undefined-valued)", () => {
    const record = cacheRecord(required);
    expect(record).toEqual({
      key,
      output: { label: "billing" },
      created_at: "2026-01-01T00:00:00Z",
      output_schema_hash: "out-schema",
    });
    // Key ABSENCE, not `undefined` presence — the record must JSON-round-trip cleanly.
    expect(Object.keys(record)).toEqual(["key", "output", "created_at", "output_schema_hash"]);
  });

  it("treats explicit null provenance the same as omitted (matching Python cache_record)", () => {
    const record = cacheRecord({ ...required, manifestHash: null, tokensSaved: null });
    expect("manifest_hash" in record).toBe(false);
    expect("tokens_saved" in record).toBe(false);
  });

  it("includes provenance when provided — tokensSaved: 0 is a real value, not absence", () => {
    const record = cacheRecord({ ...required, manifestHash: "m".repeat(64), tokensSaved: 0 });
    expect(record.manifest_hash).toBe("m".repeat(64));
    expect(record.tokens_saved).toBe(0);
  });

  it("deep-copies the key so mutating the input scope cannot alias the stored record", () => {
    const scope = { company_id: "co-1" };
    const record = cacheRecord({ ...required, key: { ...key, scope } });
    scope.company_id = "co-2";
    expect(record.key.scope).toEqual({ company_id: "co-1" });
    expect(record.key).not.toBe(key);
  });

  it("carries subjects present-only and never perturbs the key digest (#715)", () => {
    const without = cacheRecord(required);
    const withSubjects = cacheRecord({ ...required, subjects: ["pt-1", "pt-2"] });
    expect("subjects" in without).toBe(false); // absent when empty
    expect(withSubjects.subjects).toEqual(["pt-1", "pt-2"]);
    // Subjects on the RECORD, never the KEY: the digest is unchanged.
    expect(cacheKeyDigest(withSubjects.key)).toBe(cacheKeyDigest(without.key));
    // An empty subjects array is treated as absence (Python `if subjects:` parity).
    expect("subjects" in cacheRecord({ ...required, subjects: [] })).toBe(false);
  });
});
