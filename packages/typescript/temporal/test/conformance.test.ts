import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { ResolvedArtifactGroup } from "../src/index.js";
import {
  artifactGroupsCacheIdentity,
  artifactRefSchema,
  cacheInputHash,
  cacheKeyDigest,
  cacheRecord,
  canonicalJson,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
// test/ -> temporal -> typescript -> packages -> repo root -> contracts/
const contractsDir = resolve(here, "../../../../contracts");

function golden<T = unknown>(rel: string): T {
  return JSON.parse(readFileSync(resolve(contractsDir, rel), "utf-8")) as T;
}

// The exact inputs the Python baseline used to generate the cache-key goldens
// (packages/python/tests/test_contracts_cache.py::build_goldens). The TS SDK
// must reproduce the same hashes from the same inputs.
const reproducedInputHash = cacheInputHash({
  activity: "classify_ticket",
  inputSchemaHash: "a".repeat(64),
  renderedMessagesHash: "b".repeat(64),
  providerParams: { model: "fake-model", temperature: 0.0 },
});

// The same resolved artifacts the Python baseline built for the #504 golden
// (test_contracts_cache.py::_artifact_groups): a hashed local file plus a URL
// artifact with no sha256/size/role — pinning that absent fields DROP from the
// folded identity rather than serializing as null, and that an UNHASHED
// artifact folds its source ({type, url}).
const goldenArtifactGroups: ResolvedArtifactGroup[] = [
  {
    name: "contract",
    artifacts: [
      {
        group: "contract",
        index: 0,
        ref: artifactRefSchema.parse({ source: { type: "local_path", path: "/data/contract.pdf" } }),
        source_kind: "local_path",
        kind: "document",
        media_type: "application/pdf",
        role: "user",
        sha256: "e".repeat(64),
        size_bytes: 2048,
      },
      {
        group: "contract",
        index: 1,
        ref: artifactRefSchema.parse({ source: { type: "url", url: "https://example.com/diagram.png" } }),
        source_kind: "url",
        kind: "image",
        media_type: "image/png",
      },
    ],
  },
];

const reproducedArtifactsInputHash = cacheInputHash({
  activity: "classify_ticket",
  inputSchemaHash: "a".repeat(64),
  renderedMessagesHash: "b".repeat(64),
  providerParams: { model: "fake-model", temperature: 0.0 },
  artifacts: artifactGroupsCacheIdentity(goldenArtifactGroups),
});

describe("cache-key contract conformance (#391)", () => {
  it("reproduces the golden input_hash (incl. temperature 0.0 -> 0)", () => {
    const key = golden<{ input_hash: string }>("cache-key/golden/cache_key.json");
    expect(reproducedInputHash).toBe(key.input_hash);
  });

  it("reproduces the golden artifacts input_hash via artifactGroupsCacheIdentity (#504)", () => {
    const key = golden<{ input_hash: string }>("cache-key/golden/cache_key_artifacts.json");
    expect(reproducedArtifactsInputHash).toBe(key.input_hash);
  });

  it("absent or empty artifacts leave the input_hash unchanged (#504)", () => {
    // The pre-#504 recipe byte-for-byte, so stored artifact-free entries stay valid.
    const emptied = cacheInputHash({
      activity: "classify_ticket",
      inputSchemaHash: "a".repeat(64),
      renderedMessagesHash: "b".repeat(64),
      providerParams: { model: "fake-model", temperature: 0.0 },
      artifacts: [],
    });
    expect(emptied).toBe(reproducedInputHash);
    expect(reproducedArtifactsInputHash).not.toBe(reproducedInputHash);
  });

  it("reproduces the golden cache_record byte-for-byte", () => {
    const expected = golden("cache-key/golden/cache_record.json");
    const record = cacheRecord({
      key: {
        activity: "classify_ticket",
        input_hash: reproducedInputHash,
        scope: { company_id: "co-1", product_id: "prod-1" },
      },
      output: { label: "billing", score: 0.97 },
      createdAt: "2026-01-01T00:00:00Z",
      outputSchemaHash: "c".repeat(64),
      manifestHash: "d".repeat(64),
      tokensSaved: 512,
    });
    expect(record).toEqual(expected);
  });

  it("reproduces the golden subject-scoped cache_record byte-for-byte (#715)", () => {
    const expected = golden("cache-key/golden/cache_record_subjects.json");
    const record = cacheRecord({
      key: {
        activity: "classify_ticket",
        input_hash: reproducedInputHash,
        scope: { company_id: "co-1", product_id: "prod-1" },
      },
      output: { label: "billing", score: 0.97 },
      createdAt: "2026-01-01T00:00:00Z",
      outputSchemaHash: "c".repeat(64),
      manifestHash: "d".repeat(64),
      tokensSaved: 512,
      subjects: ["subject-a", "subject-b"],
    });
    expect(record).toEqual(expected);
    // The KEY is byte-identical to the subject-free record's key: subjects never
    // perturb the cache key/digest (over-partitioning guard).
    expect(record.key).toEqual((expected as { key: unknown }).key);
  });

  it("digest is independent of scope insertion order", () => {
    const a = cacheKeyDigest({
      activity: "a",
      input_hash: "h",
      scope: { company_id: "c", product_id: "p" },
    });
    const b = cacheKeyDigest({
      activity: "a",
      input_hash: "h",
      scope: { product_id: "p", company_id: "c" },
    });
    expect(a).toBe(b);
    expect(a).toHaveLength(64);
  });

  it("input_hash is independent of provider_params key order", () => {
    const ordered = cacheInputHash({
      activity: "x",
      inputSchemaHash: "s",
      renderedMessagesHash: "r",
      providerParams: { model: "m", temperature: 0.0 },
    });
    const reordered = cacheInputHash({
      activity: "x",
      inputSchemaHash: "s",
      renderedMessagesHash: "r",
      providerParams: { temperature: 0.0, model: "m" },
    });
    expect(ordered).toBe(reordered);
  });
});

describe("canonical_json cross-SDK parity", () => {
  it("sorts keys recursively and uses compact separators", () => {
    expect(canonicalJson({ b: 1, a: 2 })).toBe('{"a":2,"b":1}');
    expect(canonicalJson({ z: { d: 1, c: 2 } })).toBe('{"z":{"c":2,"d":1}}');
  });

  it("neutralizes integral floats (temperature 0.0 -> 0)", () => {
    expect(canonicalJson({ temperature: 0.0, model: "m" })).toBe('{"model":"m","temperature":0}');
  });

  it("escapes non-ASCII like Python ensure_ascii=True", () => {
    expect(canonicalJson({ x: "café" })).toBe('{"x":"caf\\u00e9"}');
  });

  it("sorts keys by Unicode code point (astral keys) like Python, not UTF-16", () => {
    const bmp = String.fromCodePoint(0xe000); // U+E000 (private use)
    const astral = String.fromCodePoint(0x1f600); // U+1F600 emoji
    // By code point E000 < 1F600, so the BMP key sorts first; JS's default UTF-16
    // sort would order the surrogate-pair emoji first (lead unit 0xD83D < 0xE000).
    const json = canonicalJson({ [astral]: 1, [bmp]: 2 });
    expect(json.indexOf("\\ue000")).toBeLessThan(json.indexOf("\\ud83d"));
  });
});

describe("contract version", () => {
  it("is pinned to 1", () => {
    const version = readFileSync(resolve(contractsDir, "CONTRACT_VERSION"), "utf-8").trim();
    expect(version).toBe("1");
  });
});
