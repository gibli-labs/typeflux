import { describe, expect, it } from "vitest";

import type { ResolvedArtifact, ResolvedArtifactGroup } from "../src/index.js";
import {
  artifactAttachment,
  artifactForName,
  artifactGroupsCacheIdentity,
  artifactGroupsSummary,
  artifactInput,
  artifactPolicy,
  artifactRefSchema,
  artifactSafeSummary,
  artifactSourceFromValue,
  artifactSourceSchema,
  artifactsForGroup,
  attachArtifactMessages,
  ProviderConfigError,
} from "../src/index.js";

const resolved = (over: Partial<ResolvedArtifact> & { group: string; index: number }): ResolvedArtifact => ({
  ref: artifactRefSchema.parse({ source: "a.png" }),
  source_kind: "local_path",
  kind: "image",
  media_type: "image/png",
  role: over.group,
  sha256: "a".repeat(64),
  size_bytes: 3,
  local_path: "/tmp/a.png",
  ...over,
});

describe("artifactSource (#481)", () => {
  it("coerces a bare string to a local_path source", () => {
    expect(artifactSourceFromValue("docs/a.pdf")).toEqual({ type: "local_path", path: "docs/a.pdf" });
  });

  it("requires the fields of its type", () => {
    expect(() => artifactSourceSchema.parse({ type: "url" })).toThrow(/artifact source 'url' requires url/);
    expect(() => artifactSourceSchema.parse({ type: "provider_file", provider: "openai" })).toThrow(
      /requires file_id/,
    );
  });

  it("rejects fields of other source kinds (mutual exclusion)", () => {
    expect(() => artifactSourceSchema.parse({ type: "local_path", path: "a", url: "http://x" })).toThrow(
      /artifact source 'local_path' cannot set url/,
    );
  });

  it("rejects unknown keys (strict)", () => {
    expect(() => artifactSourceSchema.parse({ type: "local_path", path: "a", extra: 1 })).toThrow();
  });
});

describe("artifactRef (#481)", () => {
  it("coerces a bare-string source and defaults metadata", () => {
    const ref = artifactRefSchema.parse({ source: "a.png", kind: "image" });
    expect(ref.source).toEqual({ type: "local_path", path: "a.png" });
    expect(ref.metadata).toEqual({});
  });

  it("validates sha256 as 64-char lowercase hex", () => {
    expect(() => artifactRefSchema.parse({ source: "a", sha256: "XYZ" })).toThrow(/lowercase hex/);
    expect(artifactRefSchema.parse({ source: "a", sha256: "0".repeat(64) }).sha256).toBe("0".repeat(64));
  });

  it("rejects negative size_bytes and untrimmed role/display_name", () => {
    expect(() => artifactRefSchema.parse({ source: "a", size_bytes: -1 })).toThrow(/>= 0/);
    expect(() => artifactRefSchema.parse({ source: "a", role: " padded " })).toThrow(/non-empty and trimmed/);
    expect(() => artifactRefSchema.parse({ source: "a", display_name: "" })).toThrow(/non-empty and trimmed/);
  });
});

describe("artifactInput (#481)", () => {
  it("requires from_path to start with input.", () => {
    expect(() => artifactInput({ name: "docs", from_path: "documents" })).toThrow(/must start with 'input\.'/);
  });

  it("defaults required to true and freezes the shape", () => {
    const input = artifactInput({ name: "docs", from_path: "input.docs" });
    expect(input.required).toBe(true);
    expect(Object.isFrozen(input)).toBe(true);
  });

  it("rejects cache_role reference without an attach rule (the artifact would silently vanish)", () => {
    expect(() => artifactInput({ name: "docs", from_path: "input.docs", cache_role: "reference" })).toThrow(
      /reference artifact must declare how it attaches/,
    );
    // With attach it is valid.
    expect(
      artifactInput({
        name: "docs",
        from_path: "input.docs",
        cache_role: "reference",
        attach: artifactAttachment({ role: "user" }),
      }).cache_role,
    ).toBe("reference");
  });

  it("#362: rejects an optional, textless cache_role reference (unstable prefix shape)", () => {
    // An optional, textless reference artifact emits no attach message for items
    // where it resolves empty — the conversation shape (and the prefix breakpoint
    // index derived from the static count) would vary across items. Fail-closed.
    expect(() =>
      artifactInput({
        name: "docs",
        from_path: "input.docs",
        cache_role: "reference",
        required: false,
        attach: artifactAttachment({ role: "user" }),
      }),
    ).toThrow(/must always produce its attach message/);
    // The two stable shapes are allowed: required + textless…
    expect(
      artifactInput({
        name: "docs",
        from_path: "input.docs",
        cache_role: "reference",
        attach: artifactAttachment({ role: "user" }),
      }).required,
    ).toBe(true);
    // …and optional WITH static attach text (the message is always emitted).
    expect(
      artifactInput({
        name: "docs",
        from_path: "input.docs",
        cache_role: "reference",
        required: false,
        attach: artifactAttachment({ role: "user", text: "Docs:" }),
      }).required,
    ).toBe(false);
  });

  it("re-validates a structural attach literal (an invalid attachment is unrepresentable)", () => {
    expect(() => artifactInput({ name: "d", from_path: "input.d", attach: { role: "user", text: "" } })).toThrow(
      /attachment text must be non-empty/,
    );
  });

  it("validates bounds and media types", () => {
    expect(() => artifactInput({ name: "d", from_path: "input.d", max_count: 0 })).toThrow(/max_count/);
    expect(() => artifactInput({ name: "d", from_path: "input.d", max_bytes: -1 })).toThrow(/max_bytes/);
    expect(() => artifactInput({ name: "d", from_path: "input.d", media_types: [" image/png"] })).toThrow(
      /media_types/,
    );
  });
});

describe("artifactAttachment + artifactPolicy (#481)", () => {
  it("defaults the attachment role to user and rejects empty text", () => {
    expect(artifactAttachment({}).role).toBe("user");
    expect(() => artifactAttachment({ text: "" })).toThrow(/non-empty/);
  });

  it("policy defaults to local_path-only sources and validates bounds", () => {
    const policy = artifactPolicy({});
    expect(policy.allowed_source_kinds).toEqual(["local_path"]);
    expect(policy.local_roots).toEqual([]);
    expect(() => artifactPolicy({ max_bytes: -1 })).toThrow(/max_bytes/);
    expect(() => artifactPolicy({ allowed_media_types: [""] })).toThrow(/media types/);
  });
});

describe("artifact lookups (#481, Python providers/_shared)", () => {
  const groups: ResolvedArtifactGroup[] = [
    { name: "logo", artifacts: [resolved({ group: "logo", index: 0 })] },
    {
      name: "pages",
      artifacts: [resolved({ group: "pages", index: 0 }), resolved({ group: "pages", index: 1 })],
    },
    { name: "empty", artifacts: [] },
  ];

  it("resolves a single-artifact group by bare name", () => {
    expect(artifactForName(groups, "logo", "gemini").index).toBe(0);
  });

  it("resolves group[index] syntax, including Python int()'s lax forms", () => {
    expect(artifactForName(groups, "pages[1]", "gemini").index).toBe(1);
    expect(artifactForName(groups, "pages[01]", "gemini").index).toBe(1); // int("01") == 1
    expect(artifactForName(groups, "pages[+1]", "gemini").index).toBe(1); // int("+1") == 1
    expect(artifactForName(groups, "pages[ 1 ]", "gemini").index).toBe(1); // int(" 1 ") == 1
  });

  it("throws the group[index] guidance for a bare multi-artifact reference", () => {
    expect(() => artifactForName(groups, "pages", "gemini")).toThrow(/use group\[index\] syntax/);
  });

  it("throws typed ProviderConfigError for unknown groups, bad indices, and negatives", () => {
    for (const [name, pattern] of [
      ["nope", /unknown artifact group: nope/],
      ["pages[9]", /unknown artifact reference: pages\[9\]/],
      ["pages[-1]", /invalid artifact reference: pages\[-1\]/],
      ["pages[x]", /invalid artifact reference: pages\[x\]/],
      ["pages[1.0]", /invalid artifact reference/], // Number() would accept; Python int() rejects
      ["pages[1e0]", /invalid artifact reference/],
      ["pages[0x1]", /invalid artifact reference/],
    ] as const) {
      let caught: unknown;
      try {
        artifactForName(groups, name, "openai");
      } catch (error) {
        caught = error;
      }
      expect(caught).toBeInstanceOf(ProviderConfigError);
      expect((caught as Error).message).toMatch(pattern);
      expect((caught as ProviderConfigError).provider).toBe("openai");
    }
  });

  it("artifactsForGroup returns all artifacts and throws on a miss", () => {
    expect(artifactsForGroup(groups, "pages", "anthropic")).toHaveLength(2);
    expect(() => artifactsForGroup(groups, "nope", "anthropic")).toThrow(ProviderConfigError);
  });
});

describe("attachArtifactMessages (#481)", () => {
  const base = [{ role: "user", content: "review this" }];

  it("appends a message with preamble text + the artifact_group part", () => {
    const input = artifactInput({
      name: "docs",
      from_path: "input.docs",
      attach: artifactAttachment({ role: "user", text: "Reference documents:" }),
    });
    const groups: ResolvedArtifactGroup[] = [{ name: "docs", artifacts: [resolved({ group: "docs", index: 0 })] }];
    const out = attachArtifactMessages(base, [input], groups);
    expect(out).toHaveLength(2);
    expect(out[1]).toEqual({
      role: "user",
      content: [
        { type: "text", text: "Reference documents:" },
        { type: "artifact_group", group: "docs" },
      ],
    });
  });

  it("skips a group with no artifacts and no preamble; keeps preamble-only", () => {
    const bare = artifactInput({ name: "docs", from_path: "input.docs", attach: artifactAttachment({}) });
    expect(attachArtifactMessages(base, [bare], [])).toHaveLength(1);
    const withText = artifactInput({
      name: "docs",
      from_path: "input.docs",
      attach: artifactAttachment({ text: "None supplied." }),
    });
    const out = attachArtifactMessages(base, [withText], []);
    expect(out[1]?.content).toEqual([{ type: "text", text: "None supplied." }]);
  });

  it("does nothing for inputs without attach and never mutates the original list", () => {
    const input = artifactInput({ name: "docs", from_path: "input.docs" });
    const out = attachArtifactMessages(base, [input], []);
    expect(out).toHaveLength(1);
    expect(out).not.toBe(base);
  });
});

describe("safe summaries (#481)", () => {
  it("summarizes without sensitive fields (no ref/local_path) and drops undefined", () => {
    const artifact = resolved({ group: "docs", index: 0 });
    const summary = artifactSafeSummary(artifact);
    expect(summary).toEqual({
      group: "docs",
      index: 0,
      source_kind: "local_path",
      kind: "image",
      media_type: "image/png",
      role: "docs",
      sha256: "a".repeat(64),
      size_bytes: 3,
    });
    expect("local_path" in summary).toBe(false);
    expect("ref" in summary).toBe(false);
  });

  it("artifactGroupsSummary carries name + count", () => {
    const groups: ResolvedArtifactGroup[] = [{ name: "docs", artifacts: [resolved({ group: "docs", index: 0 })] }];
    expect(artifactGroupsSummary(groups)[0]).toMatchObject({ name: "docs", count: 1 });
  });
});

describe("artifactGroupsCacheIdentity (#504)", () => {
  const urlArtifact = (url: string): ResolvedArtifact => ({
    group: "docs",
    index: 0,
    ref: artifactRefSchema.parse({ source: { type: "url", url } }),
    source_kind: "url",
    kind: "image",
    media_type: "image/png",
  });

  it("folds the source for an unhashed artifact, so a URL swap changes the identity", () => {
    const a = artifactGroupsCacheIdentity([{ name: "docs", artifacts: [urlArtifact("https://example.com/a.png")] }]);
    const b = artifactGroupsCacheIdentity([{ name: "docs", artifacts: [urlArtifact("https://example.com/b.png")] }]);
    expect((a[0] as { artifacts: Record<string, unknown>[] }).artifacts[0]?.["source"]).toEqual({
      type: "url",
      url: "https://example.com/a.png",
    });
    expect(a).not.toEqual(b);
  });

  it("keys a sha256-pinned artifact on content only (no source in the fold)", () => {
    const identity = artifactGroupsCacheIdentity([{ name: "docs", artifacts: [resolved({ group: "docs", index: 0 })] }]);
    const entry = (identity[0] as { artifacts: Record<string, unknown>[] }).artifacts[0];
    expect(entry).toBeDefined();
    expect("source" in (entry as Record<string, unknown>)).toBe(false);
  });

  it("drops empty groups, so an optional input resolving to nothing keeps the key stable", () => {
    expect(artifactGroupsCacheIdentity([{ name: "docs", artifacts: [] }])).toEqual([]);
  });
});
