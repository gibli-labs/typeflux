import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import type {
  CachedSessionHandle,
  ChatMessage,
  ModelProvider,
  PrepareCachedSessionParams,
  ResolvedArtifactGroup,
  StructuredCallParams,
} from "../src/index.js";
import {
  artifactInput,
  artifactRefSchema,
  defineActivity,
  executeActivity,
  InMemoryCacheStore,
  InlinePromptRegistry,
  noSessionCacheHandle,
  prepareSessionCache,
  providerIdentifier,
  sessionCacheIdentity,
  UnstableCachePrefixError,
} from "../src/index.js";

const Input = z.object({ text: z.string(), documents: z.array(z.string()).optional() });
const Output = z.object({ label: z.string() });
const PROMPT = { name: "p/classify", label: "production" } as const;

const SYSTEM_PROMPT: ChatMessage[] = [
  { role: "system", content: "You are a careful reviewer." },
  { role: "user", content: "Classify {{ text }}" },
];

function registry(messages: ChatMessage[] = SYSTEM_PROMPT) {
  return new InlinePromptRegistry({ "p/classify": messages });
}

function activity(extra: Record<string, unknown> = {}) {
  return defineActivity({
    name: "classify",
    prompt: PROMPT,
    input: Input,
    output: Output,
    ...extra,
  });
}

function resolvedGroup(name: string, count = 1): ResolvedArtifactGroup {
  return {
    name,
    artifacts: Array.from({ length: count }, (_, index) => ({
      group: name,
      index,
      ref: artifactRefSchema.parse({ source: "doc.pdf" }),
      source_kind: "local_path" as const,
      kind: "document" as const,
      media_type: "application/pdf",
      sha256: "e".repeat(64),
      size_bytes: 128,
    })),
  };
}

/** A capable prefix-style provider that records its prepare calls. */
class PrefixProvider implements ModelProvider {
  readonly providerName: string = "fake-prefix";
  readonly supportsSessionCache = true;
  readonly sessionCacheStyle: "prefix" | "reference" = "prefix";
  prepared: PrepareCachedSessionParams[] = [];
  structuredCall(): unknown {
    return { label: "ok" };
  }
  prepareCachedSession(params: PrepareCachedSessionParams): CachedSessionHandle {
    this.prepared.push(params);
    return {
      ...noSessionCacheHandle({ provider: this.providerName, identityHash: params.identityHash }),
      supported: true,
      style: this.sessionCacheStyle,
    };
  }
}

class ReferenceProvider extends PrefixProvider {
  override readonly providerName: string = "fake-reference";
  override readonly sessionCacheStyle: "prefix" | "reference" = "reference";
}

describe("prepareSessionCache fail-soft ladder (#478 PR3, Python prepare_session_cache)", () => {
  const createdAt = "2026-07-02T00:00:00+00:00";

  it("no sessionCache config -> fail-soft with the system-prefix identity", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity(), { provider, registry: registry(), createdAt });
    expect(handle.supported).toBe(false);
    expect(handle.provider).toBe("fake-prefix");
    expect(handle.identity_hash).toBe(
      sessionCacheIdentity({
        providerName: "fake-prefix",
        systemMessages: [SYSTEM_PROMPT[0] as ChatMessage],
        outputSchema: activity().outputProviderSchema,
      }),
    );
    expect(provider.prepared).toHaveLength(0);
  });

  it("enabled: false or an incapable provider -> fail-soft", async () => {
    const provider = new PrefixProvider();
    const disabled = await prepareSessionCache(activity({ sessionCache: { enabled: false } }), {
      provider,
      registry: registry(),
      createdAt,
    });
    expect(disabled.supported).toBe(false);
    const incapable = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider: { structuredCall: () => ({ label: "ok" }) },
      registry: registry(),
      createdAt,
    });
    expect(incapable.supported).toBe(false);
    expect(provider.prepared).toHaveLength(0);
  });

  it("a capable provider prepares over the system prefix and gets created_at stamped", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: { ttlSeconds: 600 } }), {
      provider,
      registry: registry(),
      createdAt,
      model: "fake-model",
    });
    expect(handle.supported).toBe(true);
    expect(handle.style).toBe("prefix");
    expect(handle.created_at).toBe(createdAt);
    expect(handle.reference_cached).toBe(false);
    expect(provider.prepared).toHaveLength(1);
    expect(provider.prepared[0]?.messages).toEqual([SYSTEM_PROMPT[0]]);
    expect(provider.prepared[0]?.ttlSeconds).toBe(600);
    expect(provider.prepared[0]?.model).toBe("fake-model");
  });

  it("a prompt-pinned model reaches the prep + identity when options.model is unset", async () => {
    // resolveCall parity (codex P2): per-item calls will use the prompt's model, so a
    // model-scoped (reference-style) cache must be prepared + identified under the same one.
    const provider = new PrefixProvider();
    const pinned = new InlinePromptRegistry({
      "p/classify": { ref: PROMPT, messages: SYSTEM_PROMPT, model: "prompt-pinned-model" },
    });
    const handle = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: pinned,
      createdAt,
    });
    expect(provider.prepared[0]?.model).toBe("prompt-pinned-model");
    expect(handle.identity_hash).toBe(
      sessionCacheIdentity({
        providerName: "fake-prefix",
        model: "prompt-pinned-model",
        // The merged params carry the folded prompt model (#495 PR-A1).
        providerParams: { model: "prompt-pinned-model" },
        systemMessages: [SYSTEM_PROMPT[0] as ChatMessage],
        outputSchema: activity().outputProviderSchema,
      }),
    );
    // An explicit options.model still wins (same precedence as executeActivity).
    await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: pinned,
      createdAt,
      model: "explicit-model",
    });
    expect(provider.prepared[1]?.model).toBe("explicit-model");
  });

  it("nothing stable to cache (no system, no reference artifacts) -> fail-soft", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: registry([{ role: "user", content: "Classify {{ text }}" }]),
      createdAt,
    });
    expect(handle.supported).toBe(false);
    expect(provider.prepared).toHaveLength(0);
  });

  it("a templated system prefix throws LOUDLY (the one non-soft failure)", async () => {
    const provider = new PrefixProvider();
    await expect(
      prepareSessionCache(activity({ sessionCache: {} }), {
        provider,
        registry: registry([{ role: "system", content: "Review for {{ customer }}." }]),
        createdAt,
      }),
    ).rejects.toBeInstanceOf(UnstableCachePrefixError);
    expect(provider.prepared).toHaveLength(0);
  });

  it("provider prep failure degrades to uncached with a warning", async () => {
    const provider = new PrefixProvider();
    provider.prepareCachedSession = () => {
      throw new Error("free-tier explicit-cache cap");
    };
    const warnings: string[] = [];
    const handle = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: registry(),
      createdAt,
      onWarning: (message) => warnings.push(message),
    });
    expect(handle.supported).toBe(false);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain("proceeding uncached");
  });

  it("defaults onWarning to console.warn (degradation visible by default)", async () => {
    const provider = new PrefixProvider();
    provider.prepareCachedSession = () => {
      throw new Error("boom");
    };
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      await prepareSessionCache(activity({ sessionCache: {} }), { provider, registry: registry(), createdAt });
      expect(spy).toHaveBeenCalledOnce();
    } finally {
      spy.mockRestore();
    }
  });
});

describe("prepareSessionCache reference artifacts (#478 PR3)", () => {
  const createdAt = "2026-07-02T00:00:00+00:00";
  const referenceActivity = () =>
    activity({
      sessionCache: {},
      artifacts: [
        artifactInput({
          name: "docs",
          from_path: "input.documents",
          cache_role: "reference",
          attach: { role: "user", text: "Reference document:" },
        }),
      ],
    });

  it("reference style resolves + attaches reference artifacts and stamps reference_cached", async () => {
    const provider = new ReferenceProvider();
    const handle = await prepareSessionCache(referenceActivity(), {
      provider,
      registry: registry(),
      createdAt,
      inputValue: { text: "hi", documents: ["doc.pdf"] },
      artifactResolver: () => [resolvedGroup("docs")],
    });
    expect(handle.supported).toBe(true);
    expect(handle.reference_cached).toBe(true);
    expect(provider.prepared[0]?.artifacts).toEqual([resolvedGroup("docs")]);
    // The assembled prefix carries the attachment message, and the identity
    // covers it (differs from the bare system prefix).
    expect(provider.prepared[0]?.messages.length).toBeGreaterThan(1);
    const bare = await prepareSessionCache(activity(), { provider, registry: registry(), createdAt });
    expect(handle.identity_hash).not.toBe(bare.identity_hash);
  });

  it("a polite provider decline (supported: false, no throw) keeps reference_cached false", async () => {
    // The per-item skip gates on reference_cached ALONE, so an unengaged handle
    // carrying the flag would drop documents that were never cached (Bugbot #515).
    const provider = new ReferenceProvider();
    provider.prepareCachedSession = (params) =>
      noSessionCacheHandle({ provider: "fake-reference", identityHash: params.identityHash });
    const handle = await prepareSessionCache(referenceActivity(), {
      provider,
      registry: registry(),
      createdAt,
      inputValue: { text: "hi", documents: ["doc.pdf"] },
      artifactResolver: () => [resolvedGroup("docs")],
    });
    expect(handle.supported).toBe(false);
    expect(handle.reference_cached).toBe(false);
  });

  it("fail-soft handles record the prompt-pinned model too (not only engaged ones)", async () => {
    const provider = new ReferenceProvider();
    provider.prepareCachedSession = () => {
      throw new Error("quota");
    };
    const pinned = new InlinePromptRegistry({
      "p/classify": { ref: PROMPT, messages: SYSTEM_PROMPT, model: "prompt-pinned-model" },
    });
    const handle = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: pinned,
      createdAt,
      onWarning: () => {},
    });
    expect(handle.supported).toBe(false);
    expect(handle.model).toBe("prompt-pinned-model");
  });

  it("an EMPTY resolved reference group keeps reference_cached false (no silent drops)", async () => {
    const provider = new ReferenceProvider();
    const handle = await prepareSessionCache(referenceActivity(), {
      provider,
      registry: registry(),
      createdAt,
      inputValue: { text: "hi", documents: [] },
      artifactResolver: () => [{ name: "docs", artifacts: [] }],
    });
    expect(handle.supported).toBe(true);
    expect(handle.reference_cached).toBe(false);
  });

  it("reference-resolution failure (incl. a missing resolver) fails soft with a warning", async () => {
    const provider = new ReferenceProvider();
    const warnings: string[] = [];
    const failed = await prepareSessionCache(referenceActivity(), {
      provider,
      registry: registry(),
      createdAt,
      inputValue: { text: "hi", documents: ["missing.pdf"] },
      artifactResolver: () => {
        throw new Error("unreadable file");
      },
      onWarning: (message) => warnings.push(message),
    });
    expect(failed.supported).toBe(false);
    const noResolver = await prepareSessionCache(referenceActivity(), {
      provider,
      registry: registry(),
      createdAt,
      inputValue: { text: "hi", documents: ["doc.pdf"] },
      onWarning: (message) => warnings.push(message),
    });
    expect(noResolver.supported).toBe(false);
    expect(warnings).toHaveLength(2);
    expect(provider.prepared).toHaveLength(0);
  });

  it("prefix style never resolves reference artifacts (a wasted read)", async () => {
    const provider = new PrefixProvider();
    const resolver = vi.fn(() => [resolvedGroup("docs")]);
    const handle = await prepareSessionCache(referenceActivity(), {
      provider,
      registry: registry(),
      createdAt,
      inputValue: { text: "hi", documents: ["doc.pdf"] },
      artifactResolver: resolver,
    });
    expect(resolver).not.toHaveBeenCalled();
    expect(handle.supported).toBe(true);
    expect(handle.reference_cached).toBe(false);
  });
});

describe("providerIdentifier (#478 PR3, Python provider_identifier)", () => {
  it("prefers the declared providerName, else kebab-cases the class name", () => {
    expect(providerIdentifier(new PrefixProvider())).toBe("fake-prefix");
    class MyCustomThingProvider {
      structuredCall(): unknown {
        return {};
      }
    }
    expect(providerIdentifier(new MyCustomThingProvider())).toBe("my-custom-thing");
    expect(providerIdentifier({ structuredCall: () => ({}) })).toBe("object");
    expect(providerIdentifier(null)).toBe("provider");
  });
});

describe("executeActivity cachedSession threading (#478 PR3)", () => {
  class CapturingProvider implements ModelProvider {
    calls: StructuredCallParams[] = [];
    constructor(private readonly responses: unknown[]) {}
    structuredCall(params: StructuredCallParams): unknown {
      this.calls.push(params);
      return this.responses.shift();
    }
  }

  const referenceActivity = (required = true) =>
    activity({
      cache: {},
      artifacts: [
        artifactInput({
          name: "docs",
          from_path: "input.documents",
          cache_role: "reference",
          required,
          attach: { role: "user", text: "Reference document:" },
        }),
      ],
    });

  const supportedHandle = (identityHash: string, referenceCached: boolean): CachedSessionHandle => ({
    ...noSessionCacheHandle({ provider: "fake-reference", identityHash }),
    supported: true,
    style: "reference",
    reference_cached: referenceCached,
  });

  it("skips reference artifacts ONLY when reference_cached is true (required guard included)", async () => {
    const provider = new CapturingProvider([{ label: "a" }, { label: "b" }]);
    // reference_cached: the REQUIRED reference input is filtered before resolution,
    // attachment, and the required-group guard — no resolver needed, no attachment sent.
    const out = await executeActivity(referenceActivity(), { text: "hi", documents: ["doc.pdf"] }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
      cachedSession: supportedHandle("i".repeat(64), true),
    });
    expect(out).toEqual({ label: "a" });
    expect(provider.calls[0]?.messages).toEqual([{ role: "user", content: "Classify: hi" }]);
    expect(provider.calls[0]?.cachedSession?.identity_hash).toBe("i".repeat(64));

    // reference_cached false (e.g. the prep cached only the system prefix): the
    // document still attaches — pre-resolved groups keep flowing.
    await executeActivity(referenceActivity(), { text: "hi", documents: ["doc.pdf"] }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
      cachedSession: supportedHandle("i".repeat(64), false),
      artifacts: [resolvedGroup("docs")],
    });
    expect(provider.calls[1]?.messages.length).toBeGreaterThan(1);
  });

  it("drops a pre-resolved group for a skipped reference input (provider + key never see it)", async () => {
    const provider = new CapturingProvider([{ label: "a" }]);
    await executeActivity(referenceActivity(), { text: "hi", documents: ["doc.pdf"] }, {
      provider,
      messages: [{ role: "user", content: "Classify: hi" }],
      cachedSession: supportedHandle("i".repeat(64), true),
      // The caller pre-resolved BOTH groups; the reference one must not leak to the
      // provider payload (a prompt artifact_group part naming it would attach the
      // document natively despite the cache) — Python's prepared.artifacts excludes it.
      artifacts: [resolvedGroup("docs"), resolvedGroup("other")],
    });
    expect(provider.calls[0]?.artifacts).toEqual([resolvedGroup("other")]);
  });

  it("a required reference input with reference_cached:false still enforces the guard", async () => {
    const provider = new CapturingProvider([{ label: "a" }]);
    await expect(
      executeActivity(referenceActivity(), { text: "hi", documents: ["doc.pdf"] }, {
        provider,
        messages: [{ role: "user", content: "Classify: hi" }],
        cachedSession: supportedHandle("i".repeat(64), false),
        artifacts: [],
      }),
    ).rejects.toThrow(/requires artifact input/);
  });

  it("folds __session_identity into the cross-run cache key (fail-soft handles too)", async () => {
    const store = new InMemoryCacheStore();
    const provider = new CapturingProvider([{ label: "uncached" }, { label: "session-a" }, { label: "session-b" }]);
    const cached = activity({ cache: {} });
    const base = { provider, messages: [{ role: "user", content: "Classify: hi" }], cacheStore: store };

    const plain = await executeActivity(cached, { text: "hi" }, base);
    expect(plain).toEqual({ label: "uncached" });

    // A fail-soft handle still partitions the cache (Python folds ANY handle).
    const failSoft = noSessionCacheHandle({ provider: "p", identityHash: "a".repeat(64) });
    const withA = await executeActivity(cached, { text: "hi" }, { ...base, cachedSession: failSoft });
    expect(withA).toEqual({ label: "session-a" });

    // Same handle identity -> hit; different identity -> miss.
    const hitA = await executeActivity(cached, { text: "hi" }, { ...base, cachedSession: failSoft });
    expect(hitA).toEqual({ label: "session-a" });
    const withB = await executeActivity(cached, { text: "hi" }, {
      ...base,
      cachedSession: noSessionCacheHandle({ provider: "p", identityHash: "b".repeat(64) }),
    });
    expect(withB).toEqual({ label: "session-b" });
    expect(provider.calls).toHaveLength(3);
  });
});

describe("prefix-style reference composition (#362)", () => {
  const createdAt = "2026-07-02T00:00:00+00:00";

  class CapturingProvider implements ModelProvider {
    calls: StructuredCallParams[] = [];
    constructor(private readonly responses: unknown[]) {}
    structuredCall(params: StructuredCallParams): unknown {
      this.calls.push(params);
      return this.responses.shift();
    }
  }

  const referenceInput = (attachRole: "system" | "user" | "assistant" = "user") =>
    artifactInput({
      name: "docs",
      from_path: "input.documents",
      cache_role: "reference",
      attach: { role: attachRole, text: "Reference:" },
    });
  const perItemInput = () =>
    artifactInput({ name: "evidence", from_path: "input.documents", attach: { role: "user", text: "Evidence:" } });

  const composeActivity = (withReference = true) =>
    activity({ cache: {}, artifacts: withReference ? [referenceInput(), perItemInput()] : [perItemInput()] });

  const prefixHandle = (prefixStableMessages: number | null): CachedSessionHandle => ({
    ...noSessionCacheHandle({ provider: "anthropic", identityHash: "i".repeat(64) }),
    supported: true,
    style: "prefix",
    prefix_stable_messages: prefixStableMessages,
  });

  const labelOf = (message: ChatMessage): string => {
    if (message.role === "system") {
      return "system";
    }
    if (typeof message.content === "string") {
      return "query";
    }
    const groups = message.content.map((part) => (part as { group?: string }).group);
    if (groups.includes("docs")) {
      return "reference";
    }
    if (groups.includes("evidence")) {
      return "evidence";
    }
    return "query";
  };

  const runLabels = async (
    cachedSession: CachedSessionHandle | undefined,
    withReference = true,
  ): Promise<string[]> => {
    const provider = new CapturingProvider([{ label: "ok" }]);
    const groups = withReference ? [resolvedGroup("docs"), resolvedGroup("evidence")] : [resolvedGroup("evidence")];
    await executeActivity(composeActivity(withReference), { text: "hi", documents: ["doc.pdf"] }, {
      provider,
      messages: [
        { role: "system", content: "You extract citations." },
        { role: "user", content: "Find: hi" },
      ],
      ...(cachedSession !== undefined ? { cachedSession } : {}),
      artifacts: groups,
    });
    return (provider.calls[0]?.messages ?? []).map(labelOf);
  };

  it("prefix-style places the reference artifact before the per-item turn", async () => {
    expect(await runLabels(prefixHandle(1))).toEqual(["system", "reference", "query", "evidence"]);
  });

  it("uncached keeps append-last order byte-for-byte", async () => {
    expect(await runLabels(undefined)).toEqual(["system", "query", "reference", "evidence"]);
  });

  it("a reference-style hit drops the reference and keeps append-last", async () => {
    const referenceHit: CachedSessionHandle = {
      ...noSessionCacheHandle({ provider: "fake-reference", identityHash: "i".repeat(64) }),
      supported: true,
      style: "reference",
      reference_cached: true,
    };
    // reference_cached drops "docs"; only the per-item evidence appends last.
    expect(await runLabels(referenceHit)).toEqual(["system", "query", "evidence"]);
  });

  it("prefix-style with no reference input keeps append-last", async () => {
    expect(await runLabels(prefixHandle(null), false)).toEqual(["system", "query", "evidence"]);
  });

  it("prep sets prefix_stable_messages for a prefix-style cache and leaves reference_cached false", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: {}, artifacts: [referenceInput()] }), {
      provider,
      registry: registry(),
      createdAt,
    });
    expect(handle.style).toBe("prefix");
    expect(handle.prefix_stable_messages).toBe(1);
    expect(handle.reference_cached).toBe(false);
  });

  it("prep leaves prefix_stable_messages null without reference inputs", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: registry(),
      createdAt,
    });
    expect(handle.style).toBe("prefix");
    expect(handle.prefix_stable_messages).toBeNull();
  });

  it("prep excludes a system-role reference from the count", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(
      activity({ sessionCache: {}, artifacts: [referenceInput("system")] }),
      { provider, registry: registry(), createdAt },
    );
    expect(handle.prefix_stable_messages).toBeNull();
  });

  it("prep leaves prefix_stable_messages null for a reference-style cache", async () => {
    const provider = new ReferenceProvider();
    const handle = await prepareSessionCache(
      activity({ sessionCache: {}, artifacts: [referenceInput()] }),
      {
        provider,
        registry: registry(),
        createdAt,
        inputValue: { text: "hi", documents: ["doc.pdf"] },
        artifactResolver: () => [resolvedGroup("docs")],
      },
    );
    expect(handle.style).toBe("reference");
    expect(handle.reference_cached).toBe(true);
    expect(handle.prefix_stable_messages).toBeNull();
  });

  it("#698: prep flags per_item_artifact_messages for a prefix-style per-item attach", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: {}, artifacts: [perItemInput()] }), {
      provider,
      registry: registry(),
      createdAt,
    });
    expect(handle.style).toBe("prefix");
    expect(handle.prefix_stable_messages).toBeNull();
    expect(handle.per_item_artifact_messages).toBe(true);
  });

  it("#698: prep leaves per_item_artifact_messages false without a per-item attach", async () => {
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: {} }), {
      provider,
      registry: registry(),
      createdAt,
    });
    expect(handle.style).toBe("prefix");
    expect(handle.per_item_artifact_messages).toBe(false);
  });

  it("#698: prep excludes a system-role per-item attach from the flag", async () => {
    const provider = new PrefixProvider();
    const systemAttachInput = artifactInput({
      name: "evidence",
      from_path: "input.documents",
      attach: { role: "system", text: "Evidence:" },
    });
    const handle = await prepareSessionCache(
      activity({ sessionCache: {}, artifacts: [systemAttachInput] }),
      { provider, registry: registry(), createdAt },
    );
    expect(handle.per_item_artifact_messages).toBe(false);
  });

  it("#698 + #362: prep sets both the reference count and the per-item flag together", async () => {
    // An activity with BOTH a reference input and a per-item artifact sets
    // prefix_stable_messages=1 (authoritative) AND per_item_artifact_messages=true;
    // the provider marks the reference span and the flag is inert. End-to-end
    // confirmation of the combined truth-table row.
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(
      activity({ sessionCache: {}, artifacts: [referenceInput(), perItemInput()] }),
      { provider, registry: registry(), createdAt },
    );
    expect(handle.prefix_stable_messages).toBe(1);
    expect(handle.per_item_artifact_messages).toBe(true);
  });

  it("#698: prep leaves per_item_artifact_messages false for a reference-style cache", async () => {
    const provider = new ReferenceProvider();
    const handle = await prepareSessionCache(
      activity({ sessionCache: {}, artifacts: [perItemInput()] }),
      {
        provider,
        registry: registry(),
        createdAt,
        inputValue: { text: "hi", documents: ["doc.pdf"] },
        artifactResolver: () => [resolvedGroup("docs")],
      },
    );
    expect(handle.style).toBe("reference");
    expect(handle.per_item_artifact_messages).toBe(false);
  });

  it("#362 review: a reference-only prompt (no system) still engages a prefix-style cache", async () => {
    // codex P2: prefix-style prep never resolves reference artifacts, so the
    // "nothing stable" guard must count the declared reference INPUTS — an
    // activity whose only stable content is the reference document gets a
    // supported handle with the count set. (The true nothing-stable case — no
    // system AND no reference input — still fails soft, covered above.)
    const provider = new PrefixProvider();
    const handle = await prepareSessionCache(activity({ sessionCache: {}, artifacts: [referenceInput()] }), {
      provider,
      registry: registry([{ role: "user", content: "Classify {{ text }}" }]),
      createdAt,
    });
    expect(handle.supported).toBe(true);
    expect(handle.style).toBe("prefix");
    expect(handle.prefix_stable_messages).toBe(1);
    expect(provider.prepared).toHaveLength(1);
  });

  it("#362 review: composition applies for a reference-only prompt (no system)", async () => {
    const provider = new CapturingProvider([{ label: "ok" }]);
    await executeActivity(
      activity({ cache: {}, artifacts: [referenceInput(), perItemInput()] }),
      { text: "hi", documents: ["doc.pdf"] },
      {
        provider,
        messages: [{ role: "user", content: "Find: hi" }],
        cachedSession: prefixHandle(1),
        artifacts: [resolvedGroup("docs"), resolvedGroup("evidence")],
      },
    );
    expect((provider.calls[0]?.messages ?? []).map(labelOf)).toEqual(["reference", "query", "evidence"]);
  });

  it("#362 review: the static count matches the composed reference turns for valid shapes", async () => {
    // Invariant by construction: required+textless (group non-empty) and
    // optional+text (group EMPTY — the attach message still emits, text-only)
    // both compose exactly one reference turn, matching prefix_stable_messages=1.
    const shapes: { input: ReturnType<typeof artifactInput>; groups: ResolvedArtifactGroup[] }[] = [
      {
        input: artifactInput({
          name: "docs",
          from_path: "input.documents",
          cache_role: "reference",
          attach: { role: "user" },
        }),
        groups: [resolvedGroup("docs")],
      },
      {
        input: artifactInput({
          name: "docs",
          from_path: "input.documents",
          cache_role: "reference",
          required: false,
          attach: { role: "user", text: "Docs:" },
        }),
        groups: [{ name: "docs", artifacts: [] }],
      },
    ];
    for (const shape of shapes) {
      const provider = new CapturingProvider([{ label: "ok" }]);
      await executeActivity(
        activity({ cache: {}, artifacts: [shape.input] }),
        { text: "hi", documents: ["doc.pdf"] },
        {
          provider,
          messages: [
            { role: "system", content: "You extract citations." },
            { role: "user", content: "Find: hi" },
          ],
          cachedSession: prefixHandle(1),
          artifacts: shape.groups,
        },
      );
      // Prompt renders [system, user]; everything beyond is the reference turn.
      expect(provider.calls[0]?.messages).toHaveLength(3);
    }
  });
});
