import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ChatMessage, ModelProvider, ProviderUsage, ResolvedArtifactGroup, StructuredCallParams } from "../src/index.js";
import {
  assertStableSystemPrefix,
  artifactRefSchema,
  CACHE_PREP_ACTIVITY_SUFFIX,
  CACHE_RELEASE_ACTIVITY_SUFFIX,
  cachePrepActivityName,
  cacheReleaseActivityName,
  cachedSessionHandleSchema,
  CollectingObserver,
  defineActivity,
  executeActivity,
  hasTemplateVariables,
  noSessionCacheHandle,
  providerUsageCacheHit,
  sessionCacheIdentity,
  sessionCacheStyleOf,
  supportsSessionCache,
  UnstableCachePrefixError,
} from "../src/index.js";

const SYSTEM: ChatMessage[] = [{ role: "system", content: "You are a careful reviewer." }];

function artifactGroup(sha256: string): ResolvedArtifactGroup {
  return {
    name: "docs",
    artifacts: [
      {
        group: "docs",
        index: 0,
        ref: artifactRefSchema.parse({ source: "contract.pdf" }),
        source_kind: "local_path",
        kind: "document",
        media_type: "application/pdf",
        sha256,
        size_bytes: 2048,
      },
    ],
  };
}

describe("CachedSessionHandle (#478, Python core/contracts.py)", () => {
  it("parses a minimal payload with Python's defaults", () => {
    const handle = cachedSessionHandleSchema.parse({ provider: "anthropic", identity_hash: "a".repeat(64) });
    expect(handle).toEqual({
      provider: "anthropic",
      identity_hash: "a".repeat(64),
      supported: false,
      style: null,
      cache_id: null,
      model: null,
      created_at: null,
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    });
  });

  it("round-trips a full reference-style handle", () => {
    const wire = {
      provider: "gemini",
      identity_hash: "b".repeat(64),
      supported: true,
      style: "reference",
      cache_id: "cachedContents/abc123",
      model: "gemini-2.5-pro",
      created_at: "2026-07-01T00:00:00+00:00",
      ttl_seconds: 600,
      reference_cached: true,
      prefix_stable_messages: null,
      per_item_artifact_messages: false,
    };
    expect(cachedSessionHandleSchema.parse(wire)).toEqual(wire);
  });

  it("carries prefix_stable_messages for a prefix-style handle (#362)", () => {
    // Additive count field: null default (legacy conversation[-2]), and round-trips
    // a positive int that steers the Anthropic breakpoint over the reference span.
    expect(
      cachedSessionHandleSchema.parse({ provider: "anthropic", identity_hash: "h" })
        .prefix_stable_messages,
    ).toBeNull();
    const wire = {
      provider: "anthropic",
      identity_hash: "h".repeat(64),
      supported: true,
      style: "prefix",
      cache_id: null,
      model: "claude-x",
      created_at: null,
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: 2,
      per_item_artifact_messages: false,
    };
    expect(cachedSessionHandleSchema.parse(wire)).toEqual(wire);
  });

  it("carries per_item_artifact_messages for a prefix-style handle (#698)", () => {
    // Additive boolean: false default (legacy conversation[-2]), and round-trips true
    // (the shape where a per-item artifact trails the varying turn → no stable span).
    expect(
      cachedSessionHandleSchema.parse({ provider: "anthropic", identity_hash: "h" })
        .per_item_artifact_messages,
    ).toBe(false);
    const wire = {
      provider: "anthropic",
      identity_hash: "h".repeat(64),
      supported: true,
      style: "prefix",
      cache_id: null,
      model: "claude-x",
      created_at: null,
      ttl_seconds: null,
      reference_cached: false,
      prefix_stable_messages: null,
      per_item_artifact_messages: true,
    };
    expect(cachedSessionHandleSchema.parse(wire)).toEqual(wire);
  });

  it("rejects unknown fields and a non-positive ttl (Python extra=forbid + ttl >= 1)", () => {
    expect(() =>
      cachedSessionHandleSchema.parse({ provider: "p", identity_hash: "h", cached_content: "leak" }),
    ).toThrow(/cached_content|unrecognized/i);
    expect(() =>
      cachedSessionHandleSchema.parse({ provider: "p", identity_hash: "h", ttl_seconds: 0 }),
    ).toThrow(/ttl_seconds|small|>=1|greater/i);
    expect(() =>
      cachedSessionHandleSchema.parse({ provider: "p", identity_hash: "h", ttl_seconds: 600.5 }),
    ).toThrow(/int/i);
  });

  it("allows supported: true with a null style (both SDKs do)", () => {
    const handle = cachedSessionHandleSchema.parse({
      provider: "p",
      identity_hash: "h",
      supported: true,
    });
    expect(handle.supported).toBe(true);
    expect(handle.style).toBeNull();
  });

  it("noSessionCacheHandle is the fail-soft fallback", () => {
    const handle = noSessionCacheHandle({ provider: "openai", identityHash: "c".repeat(64) });
    expect(handle.supported).toBe(false);
    expect(handle.style).toBeNull();
    expect(handle.cache_id).toBeNull();
    expect(handle.reference_cached).toBe(false);
    expect(noSessionCacheHandle({ provider: "openai", identityHash: "h", model: "gpt-5" }).model).toBe("gpt-5");
  });
});

describe("capability probes (#478, Python supports_session_cache)", () => {
  it("is opt-in: only a literal `supportsSessionCache: true` counts", () => {
    expect(supportsSessionCache({ supportsSessionCache: true })).toBe(true);
    expect(supportsSessionCache({ supportsSessionCache: 1 })).toBe(false);
    expect(supportsSessionCache({})).toBe(false);
    expect(supportsSessionCache(null)).toBe(false);
    expect(supportsSessionCache("anthropic")).toBe(false);
  });

  it("sessionCacheStyleOf returns only a valid declared style", () => {
    expect(sessionCacheStyleOf({ sessionCacheStyle: "prefix" })).toBe("prefix");
    expect(sessionCacheStyleOf({ sessionCacheStyle: "reference" })).toBe("reference");
    expect(sessionCacheStyleOf({ sessionCacheStyle: "implicit" })).toBeUndefined();
    expect(sessionCacheStyleOf({})).toBeUndefined();
    expect(sessionCacheStyleOf(undefined)).toBeUndefined();
  });
});

describe("cache activity names (#478, cross-SDK strings)", () => {
  it("brackets the activity name with the Python suffixes", () => {
    expect(cachePrepActivityName("summarize")).toBe("summarize.__prepare_cache__");
    expect(cacheReleaseActivityName("summarize")).toBe("summarize.__release_cache__");
    expect(CACHE_PREP_ACTIVITY_SUFFIX).toBe(".__prepare_cache__");
    expect(CACHE_RELEASE_ACTIVITY_SUFFIX).toBe(".__release_cache__");
  });
});

describe("assertStableSystemPrefix (#478, Python #60)", () => {
  it("accepts a static prefix (string and parts)", () => {
    expect(() =>
      assertStableSystemPrefix([
        { role: "system", content: "Review the claim." },
        { role: "system", content: [{ type: "text", text: "Follow the rubric." }] },
      ]),
    ).not.toThrow();
  });

  it("rejects a {{var}} in string content", () => {
    expect(() =>
      assertStableSystemPrefix([{ role: "system", content: "Review claim {{ claim_id }}." }]),
    ).toThrow(UnstableCachePrefixError);
  });

  it("rejects a {{var}} in ANY part text field (artifact preamble included)", () => {
    expect(() =>
      assertStableSystemPrefix([
        {
          role: "system",
          content: [
            { type: "text", text: "static" },
            { type: "artifact_group", group: "docs", text: "Docs for {{ customer }}:" },
          ],
        },
      ]),
    ).toThrow(UnstableCachePrefixError);
  });

  it("hasTemplateVariables is stateless across calls (non-global regex)", () => {
    expect(hasTemplateVariables("a {{ x }} b")).toBe(true);
    expect(hasTemplateVariables("a {{ x }} b")).toBe(true);
    expect(hasTemplateVariables("static")).toBe(false);
  });
});

describe("sessionCacheIdentity (#478, Python session_cache_identity)", () => {
  const base = {
    providerName: "anthropic",
    model: "claude-x",
    systemMessages: SYSTEM,
    referenceArtifacts: [artifactGroup("d".repeat(64))],
    outputSchema: { type: "object", properties: { label: { type: "string" } } },
  } as const;

  it("is deterministic (64-hex) and ignores nothing-changed", () => {
    const a = sessionCacheIdentity({ ...base });
    const b = sessionCacheIdentity({ ...base });
    expect(a).toBe(b);
    expect(a).toMatch(/^[0-9a-f]{64}$/);
  });

  it("is sensitive to every prefix-determining field", () => {
    const original = sessionCacheIdentity({ ...base });
    expect(sessionCacheIdentity({ ...base, model: "claude-y" })).not.toBe(original);
    expect(sessionCacheIdentity({ ...base, providerName: "openai" })).not.toBe(original);
    expect(
      sessionCacheIdentity({ ...base, systemMessages: [{ role: "system", content: "Different." }] }),
    ).not.toBe(original);
    expect(
      sessionCacheIdentity({ ...base, referenceArtifacts: [artifactGroup("e".repeat(64))] }),
    ).not.toBe(original);
    expect(
      sessionCacheIdentity({ ...base, outputSchema: { type: "object", properties: {} } }),
    ).not.toBe(original);
    expect(sessionCacheIdentity({ ...base, providerParams: { temperature: 0.2 } })).not.toBe(original);
    expect(sessionCacheIdentity({ ...base, providerProfile: { backend: "vertex" } })).not.toBe(original);
  });

  it("defaults optional fields the way Python does (None/{}/())", () => {
    const minimal = sessionCacheIdentity({ systemMessages: SYSTEM });
    expect(minimal).toBe(
      sessionCacheIdentity({
        providerName: null,
        providerProfile: {},
        providerParams: {},
        model: null,
        systemMessages: SYSTEM,
        referenceArtifacts: [],
        outputSchema: null,
      }),
    );
  });

  it("reproduces Python's identity hashes byte-for-byte (cross-SDK pin)", () => {
    // The same inputs pinned in Python test_session_cache.py — the recipes are
    // byte-identical for aligned shapes, not merely "best-effort". Both suites
    // carry these constants, so either SDK changing its recipe breaks loudly.
    const referenceArtifacts = [
      {
        name: "docs",
        artifacts: [
          {
            group: "docs",
            index: 0,
            ref: artifactRefSchema.parse({ source: { type: "local_path", path: "/data/contract.pdf" } }),
            source_kind: "local_path",
            kind: "document",
            media_type: "application/pdf",
            sha256: "d".repeat(64),
            size_bytes: 2048,
          },
        ],
      },
    ] satisfies ResolvedArtifactGroup[];
    const shared = {
      providerName: "anthropic",
      providerProfile: { region: "us-east5" },
      model: "claude-x",
      referenceArtifacts,
    } as const;
    expect(
      sessionCacheIdentity({
        ...shared,
        systemMessages: [{ role: "system", content: "You are a careful reviewer." }],
      }),
    ).toBe("9f5f392619d4266fb0da95f94857853464af62ea99f3555726b73cb8ea3293df");
    expect(
      sessionCacheIdentity({
        ...shared,
        systemMessages: [
          {
            role: "system",
            content: [
              { type: "text", text: "Follow the rubric." },
              { type: "artifact_group", group: "docs" },
            ],
          },
        ],
      }),
    ).toBe("9b3c06e45c73f44ca65cac469638df94f50da484aa3d94ebcd651a4b1f9a5436");
  });
});

describe("providerUsageCacheHit (#478, Python ProviderUsage.cache_hit)", () => {
  it("is a tri-state", () => {
    expect(providerUsageCacheHit({ cacheReadTokens: 1024 })).toBe(true);
    expect(providerUsageCacheHit({ cacheReadTokens: 0, cacheWriteTokens: 2048 })).toBe(false);
    expect(providerUsageCacheHit({ cacheWriteTokens: 2048 })).toBe(false);
    expect(providerUsageCacheHit({ inputTokens: 10, outputTokens: 5 })).toBeUndefined();
    expect(providerUsageCacheHit({})).toBeUndefined();
  });
});

describe("usage sink threading through executeActivity (#478)", () => {
  const Input = z.object({ text: z.string() });
  const Output = z.object({ label: z.string() });

  class UsageReportingProvider implements ModelProvider {
    structuredCall(params: StructuredCallParams): unknown {
      params.usageSink?.({ inputTokens: 100, outputTokens: 7, cacheReadTokens: 90, model: "fake-model" });
      return { label: "ok" };
    }
  }

  it("forwards provider-reported usage to the caller's sink AND the generation observation", async () => {
    const observer = new CollectingObserver();
    const seen: ProviderUsage[] = [];
    const activity = defineActivity({
      name: "classify",
      prompt: { name: "p/classify", label: "production" },
      input: Input,
      output: Output,
    });
    const out = await executeActivity(activity, { text: "hi" }, {
      provider: new UsageReportingProvider(),
      messages: [{ role: "user", content: "Classify: hi" }],
      observer,
      usageSink: (usage) => seen.push(usage),
    });
    expect(out).toEqual({ label: "ok" });
    expect(seen).toEqual([{ inputTokens: 100, outputTokens: 7, cacheReadTokens: 90, model: "fake-model" }]);
    expect(providerUsageCacheHit(seen[0] as ProviderUsage)).toBe(true);
    const generation = observer.activities[0]?.generations[0];
    expect(generation?.metadata["usage"]).toEqual({
      inputTokens: 100,
      outputTokens: 7,
      cacheReadTokens: 90,
      model: "fake-model",
    });
  });

  it("a double report overwrites: the last usage wins (Python reported_usage[-1])", async () => {
    const observer = new CollectingObserver();
    const doubleReporting: ModelProvider = {
      structuredCall(params: StructuredCallParams): unknown {
        params.usageSink?.({ inputTokens: 1 });
        params.usageSink?.({ inputTokens: 2 });
        return { label: "ok" };
      },
    };
    const activity = defineActivity({
      name: "classify",
      prompt: { name: "p/classify", label: "production" },
      input: Input,
      output: Output,
    });
    await executeActivity(activity, { text: "hi" }, {
      provider: doubleReporting,
      messages: [{ role: "user", content: "Classify: hi" }],
      observer,
    });
    expect(observer.activities[0]?.generations[0]?.metadata["usage"]).toEqual({ inputTokens: 2 });
  });

  it("a provider that reports nothing leaves no usage metadata (no sink required)", async () => {
    const observer = new CollectingObserver();
    const silent: ModelProvider = { structuredCall: () => ({ label: "ok" }) };
    const activity = defineActivity({
      name: "classify",
      prompt: { name: "p/classify", label: "production" },
      input: Input,
      output: Output,
    });
    await executeActivity(activity, { text: "hi" }, {
      provider: silent,
      messages: [{ role: "user", content: "Classify: hi" }],
      observer,
    });
    expect(observer.activities[0]?.generations[0]?.metadata["usage"]).toBeUndefined();
  });
});
