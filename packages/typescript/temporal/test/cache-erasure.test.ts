import { describe, expect, it } from "vitest";

import type { CacheKey, CacheRecord } from "../src/index.js";
import {
  CACHE_ERASURE_COVERAGE_CAVEAT,
  cacheKeyDigest,
  eraseSubjectFromCache,
  InMemoryCacheStore,
  isSubjectErasableCacheStore,
} from "../src/index.js";

// Subject-scoped cache erasure (#715 slice 3): the write-time subject index, the
// dry-run/execute primitive, and the honest not-supported fallback. Parity with the
// Python `test_cache_store.py` erasure suite (behavioral, not byte-identical).

function key(inputHash: string, scope: Record<string, string> = {}): CacheKey {
  return { activity: "a", input_hash: inputHash, scope };
}

function record(k: CacheKey, label: string, subjects?: string[]): CacheRecord {
  const rec: CacheRecord = {
    key: k,
    output: { label },
    created_at: "2026-01-01T00:00:00Z",
    output_schema_hash: "h",
  };
  if (subjects && subjects.length > 0) {
    rec.subjects = subjects;
  }
  return rec;
}

describe("capability detection", () => {
  it("InMemoryCacheStore advertises the erasable capability", () => {
    expect(isSubjectErasableCacheStore(new InMemoryCacheStore())).toBe(true);
  });

  it("a plain get/set store is not detected and yields a named fallback", () => {
    const plain = {
      records: new Map<string, CacheRecord>(),
      get(k: CacheKey) {
        return this.records.get(cacheKeyDigest(k));
      },
      set(k: CacheKey, r: CacheRecord) {
        this.records.set(cacheKeyDigest(k), r);
      },
    };
    expect(isSubjectErasableCacheStore(plain)).toBe(false);

    const report = eraseSubjectFromCache(plain, "subject-a", { dryRun: true });
    // Sync path for a plain store — not a promise.
    expect(report).not.toBeInstanceOf(Promise);
    if (report instanceof Promise) throw new Error("unreachable");
    expect(report.supported).toBe(false);
    expect(report.keysFound).toBe(0);
    expect(report.keysDeleted).toBe(0);
    expect(report.fullFlushFallback).toBeDefined();
    expect(report.fullFlushFallback?.toLowerCase()).toContain("flush");
    expect(report.warnings).toContain(CACHE_ERASURE_COVERAGE_CAVEAT);
  });

  it("does not narrow an eraseSubject-only object that lacks the get/set surface", () => {
    // Regression: checking eraseSubject alone would narrow this to a full store
    // and crash later on the missing base methods.
    const eraseOnly = {
      eraseSubject: () => {
        throw new Error("not a real store");
      },
    };
    expect(isSubjectErasableCacheStore(eraseOnly)).toBe(false);
    expect(isSubjectErasableCacheStore(null)).toBe(false);
    expect(isSubjectErasableCacheStore("store")).toBe(false);
  });
});

describe("InMemoryCacheStore.eraseSubject", () => {
  it("indexes on set and erases via dry-run then execute", () => {
    const store = new InMemoryCacheStore();
    const k = key("h1");
    store.set(k, record(k, "v1", ["subject-a"]));

    const dry = store.eraseSubject("subject-a", { dryRun: true });
    expect(dry.supported).toBe(true);
    expect(dry.dryRun).toBe(true);
    expect(dry.keysFound).toBe(1);
    expect(dry.keysDeleted).toBe(0);
    expect(dry.keyDigests).toEqual([cacheKeyDigest(k)]);
    // Dry run mutated nothing.
    expect(store.get(k)).toBeDefined();

    const done = store.eraseSubject("subject-a", { dryRun: false });
    expect(done.keysFound).toBe(1);
    expect(done.keysDeleted).toBe(1);
    expect(store.get(k)).toBeUndefined();
    // Index emptied — a repeat erase finds nothing.
    expect(store.eraseSubject("subject-a", { dryRun: false }).keysFound).toBe(0);
  });

  it("never indexes subject-free records and leaves them untouched", () => {
    const store = new InMemoryCacheStore();
    const subjectKey = key("s");
    const freeKey = key("f");
    store.set(subjectKey, record(subjectKey, "s", ["subject-a"]));
    store.set(freeKey, record(freeKey, "f")); // no subjects → never indexed

    const report = store.eraseSubject("subject-a", { dryRun: false });
    expect(report.keysDeleted).toBe(1);
    expect(store.get(subjectKey)).toBeUndefined();
    expect(store.get(freeKey)).toBeDefined();
  });

  it("re-indexes on overwrite, dropping the stale subject entry", () => {
    const store = new InMemoryCacheStore();
    const k = key("h");
    store.set(k, record(k, "v1", ["subject-a"]));
    store.set(k, record(k, "v2", ["subject-b"])); // overwrite, different subject

    expect(store.eraseSubject("subject-a", { dryRun: true }).keysFound).toBe(0);
    const b = store.eraseSubject("subject-b", { dryRun: false });
    expect(b.keysDeleted).toBe(1);
    expect(store.get(k)).toBeUndefined();
  });

  it("erases a two-subject record under either subject and prunes the co-subject", () => {
    const store = new InMemoryCacheStore();
    const k = key("h");
    store.set(k, record(k, "v", ["subject-a", "subject-b"]));

    expect(store.eraseSubject("subject-a", { dryRun: true }).keysFound).toBe(1);
    expect(store.eraseSubject("subject-b", { dryRun: true }).keysFound).toBe(1);

    const report = store.eraseSubject("subject-a", { dryRun: false });
    expect(report.keysDeleted).toBe(1);
    expect(store.get(k)).toBeUndefined();
    // Co-subject index pruned — no dangling pointer.
    expect(store.eraseSubject("subject-b", { dryRun: true }).keysFound).toBe(0);
  });

  it("deletes only the named subject's keys", () => {
    const store = new InMemoryCacheStore();
    const keyA = key("a");
    const keyB = key("b");
    store.set(keyA, record(keyA, "a", ["subject-a"]));
    store.set(keyB, record(keyB, "b", ["subject-b"]));

    store.eraseSubject("subject-a", { dryRun: false });
    expect(store.get(keyA)).toBeUndefined();
    expect(store.get(keyB)).toBeDefined();
  });

  it("always carries the coverage caveat on a supported erase", () => {
    const store = new InMemoryCacheStore();
    const k = key("h");
    store.set(k, record(k, "v", ["subject-a"]));
    for (const dryRun of [true, false]) {
      const report = store.eraseSubject("subject-a", { dryRun });
      expect(report.warnings).toContain(CACHE_ERASURE_COVERAGE_CAVEAT);
    }
  });

  it("adopter-shaped: erasing one subject leaves other subjects' cache hits intact", () => {
    const store = new InMemoryCacheStore();
    const scope = { company_id: "co-1", product_id: "prod-1" };
    const claimKey = (claimId: string) => key(`digest::${claimId}`, scope);

    const keyA = claimKey("claim-A");
    const keyB1 = claimKey("claim-B1");
    const keyB2 = claimKey("claim-B2");
    store.set(keyA, record(keyA, "verdict-A", ["subject-a"]));
    store.set(keyB1, record(keyB1, "verdict-B1", ["subject-b"]));
    store.set(keyB2, record(keyB2, "verdict-B2", ["subject-b"]));

    const report = store.eraseSubject("subject-a", { dryRun: false });
    expect(report.keysFound).toBe(1);
    expect(report.keysDeleted).toBe(1);

    expect(store.get(keyA)).toBeUndefined(); // a re-review would MISS
    expect(store.get(keyB1)).toBeDefined(); // still HIT
    expect(store.get(keyB2)).toBeDefined();
    const b = eraseSubjectFromCache(store, "subject-b", { dryRun: true });
    if (b instanceof Promise) throw new Error("unreachable");
    expect(b.keysFound).toBe(2);
  });
});
