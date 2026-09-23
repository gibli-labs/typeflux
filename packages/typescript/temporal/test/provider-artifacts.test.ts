import { mkdtemp, open as fsOpen, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, ResolvedArtifact, ResolvedArtifactGroup, StructuredCallParams } from "../src/index.js";
import {
  AnthropicProvider,
  artifactAttachment,
  artifactInput,
  artifactRefSchema,
  defineActivity,
  executeActivity,
  GeminiProvider,
  OpenAIProvider,
  ProviderConfigError,
} from "../src/index.js";

const outputSchema = { type: "object" as const, properties: {}, additionalProperties: false };

let dir: string;
let pngPath: string;
let txtPath: string;
let pdfPath: string;
const PNG_BYTES = Buffer.from([0x89, 0x50, 0x4e, 0x47]);

beforeAll(async () => {
  dir = await mkdtemp(join(tmpdir(), "tf-artifacts-"));
  pngPath = join(dir, "logo.png");
  txtPath = join(dir, "notes.txt");
  pdfPath = join(dir, "doc.pdf");
  await writeFile(pngPath, PNG_BYTES);
  await writeFile(txtPath, "plain notes");
  await writeFile(pdfPath, Buffer.from("%PDF-1.4 fake"));
});
afterAll(() => undefined);

const localArtifact = (over: Partial<ResolvedArtifact> & { group: string; local_path: string }): ResolvedArtifact => ({
  index: 0,
  ref: artifactRefSchema.parse({ source: over.local_path }),
  source_kind: "local_path",
  kind: "image",
  media_type: "image/png",
  role: over.group,
  sha256: "a".repeat(64),
  size_bytes: PNG_BYTES.length,
  ...over,
});

const urlArtifact = (over: Partial<ResolvedArtifact> & { group: string; url: string }): ResolvedArtifact => ({
  index: 0,
  ref: artifactRefSchema.parse({ source: { type: "url", url: over.url } }),
  source_kind: "url",
  kind: "image",
  media_type: "image/png",
  role: over.group,
  ...over,
});

const group = (name: string, ...artifacts: ResolvedArtifact[]): ResolvedArtifactGroup => ({ name, artifacts });

describe("OpenAI artifact mapping (#481 PR2)", () => {
  const capture = () => {
    const seen: { requests: unknown[] } = { requests: [] };
    const provider = new OpenAIProvider(
      {
        chat: {
          completions: {
            create: async (request: unknown) => {
              seen.requests.push(request);
              return { choices: [{ message: { content: "{}" } }] };
            },
          },
        },
      },
      { model: "gpt-test" },
    );
    return { seen, provider };
  };
  const messagesFor = (content: StructuredCallParams["messages"][number]["content"]) => [
    { role: "user" as const, content },
  ];

  it("maps a local image to a base64 data-URL image_url part (preamble text first)", async () => {
    const { seen, provider } = capture();
    await provider.structuredCall({
      messages: messagesFor([{ type: "artifact", artifact: "logo", text: "the logo:" }]),
      outputSchema,
      artifacts: [group("logo", localArtifact({ group: "logo", local_path: pngPath }))],
    });
    const content = (seen.requests[0] as { messages: { content: unknown }[] }).messages[0]?.content as Record<
      string,
      unknown
    >[];
    expect(content[0]).toEqual({ type: "text", text: "the logo:" });
    expect(content[1]).toEqual({
      type: "image_url",
      image_url: { url: `data:image/png;base64,${PNG_BYTES.toString("base64")}` },
    });
  });

  it("maps URL images, text-like files, PDFs, and provider files", async () => {
    const { seen, provider } = capture();
    await provider.structuredCall({
      messages: messagesFor([{ type: "artifact_group", group: "docs" }]),
      outputSchema,
      artifacts: [
        group(
          "docs",
          urlArtifact({ group: "docs", url: "https://x/logo.png" }),
          localArtifact({ group: "docs", index: 1, local_path: txtPath, kind: "document", media_type: "text/plain" }),
          localArtifact({ group: "docs", index: 2, local_path: pdfPath, kind: "document", media_type: "application/pdf" }),
          {
            group: "docs",
            index: 3,
            ref: artifactRefSchema.parse({ source: { type: "provider_file", provider: "openai", file_id: "file-1" } }),
            source_kind: "provider_file",
            kind: "document",
          },
        ),
      ],
    });
    const content = (seen.requests[0] as { messages: { content: unknown }[] }).messages[0]?.content as Record<
      string,
      unknown
    >[];
    expect(content[0]).toEqual({ type: "image_url", image_url: { url: "https://x/logo.png" } });
    expect(content[1]).toEqual({ type: "text", text: "plain notes" });
    expect(content[2]).toMatchObject({ type: "file", file: { filename: "doc.pdf" } });
    expect(content[3]).toEqual({ type: "file", file: { file_id: "file-1" } });
  });

  it("treats an empty-string media_type as missing (Python `or` truthiness -> image/png default)", async () => {
    const { seen, provider } = capture();
    await provider.structuredCall({
      messages: messagesFor([{ type: "artifact", artifact: "logo" }]),
      outputSchema,
      artifacts: [group("logo", localArtifact({ group: "logo", local_path: pngPath, media_type: "" }))],
    });
    const content = (seen.requests[0] as { messages: { content: unknown }[] }).messages[0]?.content as Record<
      string,
      unknown
    >[];
    expect(content[0]).toEqual({
      type: "image_url",
      image_url: { url: `data:image/png;base64,${PNG_BYTES.toString("base64")}` },
    });
  });

  it("fails loud on an artifact the chat path cannot carry (URL-sourced PDF)", async () => {
    const { provider } = capture();
    await expect(
      provider.structuredCall({
        messages: messagesFor([{ type: "artifact", artifact: "docs" }]),
        outputSchema,
        artifacts: [
          group("docs", urlArtifact({ group: "docs", url: "https://x/doc.pdf", kind: "document", media_type: "application/pdf" })),
        ],
      }),
    ).rejects.toThrow(/cannot attach this artifact through the chat-completions path/);
  });
});

describe("Anthropic artifact mapping (#481 PR2)", () => {
  const capture = () => {
    const seen: { requests: unknown[] } = { requests: [] };
    const provider = new AnthropicProvider(
      {
        messages: {
          create: async (request: unknown) => {
            seen.requests.push(request);
            return { content: [{ type: "output_json", parsed_output: {} }] };
          },
        },
      } as never,
      { model: "claude-test" },
    );
    return { seen, provider };
  };

  it("maps a local image to a base64 image block and a PDF to a document block", async () => {
    const { seen, provider } = capture();
    await provider.structuredCall({
      messages: [{ role: "user", content: [{ type: "artifact_group", group: "docs" }] }],
      outputSchema,
      artifacts: [
        group(
          "docs",
          localArtifact({ group: "docs", local_path: pngPath }),
          localArtifact({ group: "docs", index: 1, local_path: pdfPath, kind: "document", media_type: "application/pdf" }),
        ),
      ],
    });
    const content = (seen.requests[0] as { messages: { content: unknown }[] }).messages[0]?.content as Record<
      string,
      unknown
    >[];
    expect(content[0]).toEqual({
      type: "image",
      source: { type: "base64", media_type: "image/png", data: PNG_BYTES.toString("base64") },
    });
    expect(content[1]).toMatchObject({ type: "document", source: { type: "base64", media_type: "application/pdf" } });
  });

  it("normalizes text-like media types (parameters + aliases; Anthropic-only Python behavior)", async () => {
    const { seen, provider } = capture();
    await provider.structuredCall({
      messages: [{ role: "user", content: [{ type: "artifact", artifact: "notes" }] }],
      outputSchema,
      artifacts: [
        group("notes", localArtifact({ group: "notes", local_path: txtPath, kind: "document", media_type: "text/plain; charset=utf-8" })),
      ],
    });
    const content = (seen.requests[0] as { messages: { content: unknown }[] }).messages[0]?.content as Record<
      string,
      unknown
    >[];
    expect(content[0]).toEqual({ type: "text", text: "plain notes" });
  });

  it("rejects unsupported image media types and foreign provider files", async () => {
    const { provider } = capture();
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "img" }] }],
        outputSchema,
        artifacts: [group("img", localArtifact({ group: "img", local_path: pngPath, media_type: "image/bmp" }))],
      }),
    ).rejects.toThrow(/does not support image media type "image\/bmp"/);
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "f" }] }],
        outputSchema,
        artifacts: [
          group("f", {
            group: "f",
            index: 0,
            ref: artifactRefSchema.parse({ source: { type: "provider_file", provider: "openai", file_id: "x" } }),
            source_kind: "provider_file",
            kind: "document",
          }),
        ],
      }),
    ).rejects.toThrow(/provider file "openai" is not for Anthropic/);
  });
});

describe("Gemini artifact mapping (#481 PR2)", () => {
  const capture = () => {
    const seen: { requests: unknown[] } = { requests: [] };
    const provider = new GeminiProvider({
      generateContent: async (request: unknown) => {
        seen.requests.push(request);
        return { candidates: [{ content: { parts: [{ text: "{}" }] }, finishReason: "STOP" }] };
      },
    });
    return { seen, provider };
  };

  it("maps a local image to base64 inlineData and a URL document to fileData", async () => {
    const { seen, provider } = capture();
    await provider.structuredCall({
      messages: [{ role: "user", content: [{ type: "artifact_group", group: "docs" }] }],
      outputSchema,
      artifacts: [
        group(
          "docs",
          localArtifact({ group: "docs", local_path: pngPath }),
          urlArtifact({ group: "docs", index: 1, url: "https://x/d.pdf", kind: "document", media_type: "application/pdf" }),
        ),
      ],
    });
    const parts = (seen.requests[0] as { contents: { parts: unknown[] }[] }).contents[0]?.parts as Record<
      string,
      unknown
    >[];
    expect(parts[0]).toEqual({ inlineData: { mimeType: "image/png", data: PNG_BYTES.toString("base64") } });
    expect(parts[1]).toEqual({ fileData: { fileUri: "https://x/d.pdf", mimeType: "application/pdf" } });
  });

  it("fails loud over the 20MB inline cap without a files surface, and for URL audio/video", async () => {
    const { provider } = capture(); // the fake transport has no `files` surface
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "big" }] }],
        outputSchema,
        artifacts: [
          group("big", localArtifact({ group: "big", local_path: pngPath, size_bytes: 21 * 1024 * 1024 })),
        ],
      }),
    ).rejects.toThrow(/transport has no `files` surface/);
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "vid" }] }],
        outputSchema,
        artifacts: [
          group("vid", urlArtifact({ group: "vid", url: "https://x/v.mp4", kind: "video", media_type: "video/mp4" })),
        ],
      }),
    ).rejects.toThrow(/URL-sourced audio\/video is not supported/);
  });

  it("stats the file when size_bytes is unset so an oversize artifact still fails loud", async () => {
    const { provider } = capture();
    const bigPath = join(dir, "big.png");
    const handle = await fsOpen(bigPath, "w");
    await handle.truncate(21 * 1024 * 1024); // sparse 21MB file, no bytes written
    await handle.close();
    const artifact = (({ size_bytes: _sb, ...rest }) => rest)(localArtifact({ group: "big", local_path: bigPath }));
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "big" }] }],
        outputSchema,
        artifacts: [group("big", artifact)],
      }),
    ).rejects.toThrow(/over the 20MB inline cap.*#503/);
  });

  it("requires a media type (non-image local artifact without one)", async () => {
    const { provider } = capture();
    await expect(
      provider.structuredCall({
        messages: [{ role: "user", content: [{ type: "artifact", artifact: "d" }] }],
        outputSchema,
        artifacts: [
          group(
            "d",
            (({ media_type: _mt, ...rest }) => rest)(localArtifact({ group: "d", local_path: txtPath, kind: "data" })),
          ),
        ],
      }),
    ).rejects.toThrow(/cannot attach an artifact without a media type/);
  });
});

describe("executeActivity artifact resolution (#481 PR3)", () => {
  const echo = defineActivity({
    name: "echoDocs",
    prompt: { name: "p/echo", label: "production" },
    input: z.object({ docs: z.array(z.string()) }),
    output: z.object({ out: z.string() }),
    artifacts: [
      artifactInput({
        name: "docs",
        from_path: "input.docs",
        attach: artifactAttachment({ role: "user", text: "Reference documents:" }),
      }),
    ],
  });

  it("resolves via the injected artifactResolver, attaches the group message, and forwards groups", async () => {
    let seen: StructuredCallParams | undefined;
    const provider: ModelProvider = {
      structuredCall: (params) => {
        seen = params;
        return { out: "x" };
      },
    };
    const groups = [group("docs", localArtifact({ group: "docs", local_path: pngPath }))];
    const resolved: unknown[] = [];
    await executeActivity(echo, { docs: ["logo.png"] }, {
      provider,
      messages: [{ role: "user", content: "review" }],
      artifactResolver: (inputValue, inputs) => {
        resolved.push([inputValue, inputs.map((i) => i.name)]);
        return groups;
      },
    });
    expect(resolved).toEqual([[{ docs: ["logo.png"] }, ["docs"]]]);
    expect(seen?.artifacts).toBe(groups);
    // The attach message was appended after the rendered messages.
    expect(seen?.messages.at(-1)).toEqual({
      role: "user",
      content: [
        { type: "text", text: "Reference documents:" },
        { type: "artifact_group", group: "docs" },
      ],
    });
  });

  it("fails loud when a descriptor declares artifacts but no resolver or groups are supplied", async () => {
    const provider: ModelProvider = { structuredCall: () => ({ out: "x" }) };
    await expect(
      executeActivity(echo, { docs: [] }, { provider, messages: [{ role: "user", content: "go" }] }),
    ).rejects.toThrow(/declares artifacts but neither/);
  });

  it("enforces required-group presence even for pre-resolved options.artifacts", async () => {
    // Python's resolver always runs in prep, so a required input can never silently attach
    // nothing; the pre-resolved path must restore that invariant (empty groups -> loud failure).
    const provider: ModelProvider = { structuredCall: () => ({ out: "x" }) };
    await expect(
      executeActivity(echo, { docs: [] }, {
        provider,
        messages: [{ role: "user", content: "go" }],
        artifacts: [],
      }),
    ).rejects.toThrow(/requires artifact input "docs" but the resolved groups contain no artifacts/);
  });

  it("prefers pre-resolved options.artifacts over the resolver", async () => {
    let seen: StructuredCallParams | undefined;
    const provider: ModelProvider = {
      structuredCall: (params) => {
        seen = params;
        return { out: "x" };
      },
    };
    const groups = [group("docs", localArtifact({ group: "docs", local_path: pngPath }))];
    await executeActivity(echo, { docs: [] }, {
      provider,
      messages: [{ role: "user", content: "go" }],
      artifacts: groups,
      artifactResolver: () => {
        throw new Error("resolver must not run");
      },
    });
    expect(seen?.artifacts).toBe(groups);
  });
});

describe("defineActivity artifact declarations (#481 PR3)", () => {
  it("rejects duplicate artifact input names (later groups would be unreachable)", () => {
    expect(() =>
      defineActivity({
        name: "dup",
        prompt: { name: "p/dup", label: "production" },
        input: z.object({}),
        output: z.object({ out: z.string() }),
        artifacts: [
          artifactInput({ name: "docs", from_path: "input.a" }),
          artifactInput({ name: "docs", from_path: "input.b" }),
        ],
      }),
    ).toThrow(/duplicate artifact input name: "docs"/);
  });
});

describe("executeActivity artifact forwarding (#481 PR2)", () => {
  it("forwards options.artifacts into the provider call params", async () => {
    let seen: StructuredCallParams | undefined;
    const provider: ModelProvider = {
      structuredCall: (params) => {
        seen = params;
        return { out: "x" };
      },
    };
    const echo = defineActivity({
      name: "echo",
      prompt: { name: "p/echo", label: "production" },
      input: z.object({ text: z.string() }),
      output: z.object({ out: z.string() }),
    });
    const groups = [group("docs", localArtifact({ group: "docs", local_path: "/tmp/x.png" }))];
    await executeActivity(echo, { text: "hi" }, {
      provider,
      messages: [{ role: "user", content: "go" }],
      artifacts: groups,
    });
    expect(seen?.artifacts).toBe(groups);
  });
});
