import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type {
  GeminiFile,
  GeminiGenerateContentRequest,
  ResolvedArtifact,
  ResolvedArtifactGroup,
} from "../src/index.js";
import { artifactRefSchema, GeminiProvider, ProviderConfigError } from "../src/index.js";

const outputSchema = { type: "object" as const, properties: {}, additionalProperties: false };
const OVERSIZE = 21 * 1024 * 1024;

let bigPath: string;
beforeAll(async () => {
  const dir = await mkdtemp(join(tmpdir(), "tf-gemini-files-"));
  bigPath = join(dir, "video.mp4");
  await writeFile(bigPath, Buffer.from("tiny stand-in")); // size_bytes on the artifact drives the gate
});
afterEach(() => {
  vi.useRealTimers();
});

const oversize = (over?: Partial<ResolvedArtifact>): ResolvedArtifact => ({
  group: "vid",
  index: 0,
  ref: artifactRefSchema.parse({ source: bigPath }),
  source_kind: "local_path",
  kind: "video",
  media_type: "video/mp4",
  role: "vid",
  sha256: "b".repeat(64),
  size_bytes: OVERSIZE,
  local_path: bigPath,
  ...over,
});

const groupsOf = (...artifacts: ResolvedArtifact[]): ResolvedArtifactGroup[] => [{ name: "vid", artifacts }];

/** A scripted transport: `states` is the sequence of file states get() walks through. */
const scripted = (states: string[], fileOver?: Partial<GeminiFile>) => {
  const calls = { uploads: [] as unknown[], gets: 0, requests: [] as GeminiGenerateContentRequest[] };
  const provider = new GeminiProvider({
    generateContent: async (request) => {
      calls.requests.push(request);
      return { candidates: [{ content: { parts: [{ text: "{}" }] }, finishReason: "STOP" }] };
    },
    files: {
      upload: async (params) => {
        calls.uploads.push(params);
        return { name: "files/abc", uri: "gs://files/abc", mimeType: "video/mp4", state: states[0] ?? "ACTIVE", ...fileOver };
      },
      get: async () => {
        calls.gets += 1;
        return {
          name: "files/abc",
          uri: "gs://files/abc",
          mimeType: "video/mp4",
          state: states[Math.min(calls.gets, states.length - 1)] ?? "ACTIVE",
        };
      },
    },
  });
  return { provider, calls };
};

const vidMessage = [{ role: "user" as const, content: [{ type: "artifact" as const, artifact: "vid" }] }];

describe("Gemini Files API (#503)", () => {
  it("uploads a referenced oversize artifact and references it as fileData", async () => {
    const { provider, calls } = scripted(["ACTIVE"]);
    await provider.structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) });
    expect(calls.uploads).toEqual([{ file: bigPath, config: { mimeType: "video/mp4" } }]);
    const parts = calls.requests[0]?.contents[0]?.parts as Record<string, unknown>[];
    expect(parts[0]).toEqual({ fileData: { fileUri: "gs://files/abc", mimeType: "video/mp4" } });
  });

  it("polls a PROCESSING upload until ACTIVE (bounded)", async () => {
    vi.useFakeTimers();
    const { provider, calls } = scripted(["PROCESSING", "PROCESSING", "ACTIVE"]);
    const call = provider.structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) });
    await vi.advanceTimersByTimeAsync(6_000);
    await call;
    expect(calls.gets).toBe(2); // two polls before ACTIVE
  });

  it("fails loud on a FAILED upload and on a poll timeout", async () => {
    const failed = scripted(["FAILED"]);
    await expect(
      failed.provider.structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) }),
    ).rejects.toThrow(/Gemini file upload failed/);

    vi.useFakeTimers();
    const stuck = scripted(["PROCESSING"]);
    const call = stuck.provider
      .structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) })
      .then(
        () => undefined,
        (e: unknown) => e,
      );
    await vi.advanceTimersByTimeAsync(125_000);
    const failure = await call;
    expect(failure).toBeInstanceOf(ProviderConfigError);
    expect((failure as Error).message).toMatch(/did not become ACTIVE within 120s/);
  });

  it("fails loud when the Files API returns no uri", async () => {
    const { provider } = scripted(["ACTIVE"], { uri: "" });
    await expect(
      provider.structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) }),
    ).rejects.toThrow(/returned no uri/);
  });

  it("dedups uploads by content identity within a call", async () => {
    const { provider, calls } = scripted(["ACTIVE"]);
    // The same sha256 referenced twice (index 0 and 1) uploads once.
    await provider.structuredCall({
      messages: [{ role: "user", content: [{ type: "artifact_group", group: "vid" }] }],
      outputSchema,
      artifacts: groupsOf(oversize(), oversize({ index: 1 })),
    });
    expect(calls.uploads).toHaveLength(1);
  });

  it("does not touch the Files API for unreferenced artifacts (pure-text call)", async () => {
    const { provider, calls } = scripted(["ACTIVE"]);
    await provider.structuredCall({
      messages: [{ role: "user", content: "no artifact parts here" }],
      outputSchema,
      artifacts: groupsOf(oversize()), // resolved into the request but never referenced
    });
    expect(calls.uploads).toHaveLength(0);
  });

  it("classifies Files API transport failures (4xx -> ProviderConfigError, 5xx -> transient)", async () => {
    const failing = (status: number) =>
      new GeminiProvider({
        generateContent: async () => ({ candidates: [] }),
        files: {
          upload: async () => {
            throw Object.assign(new Error("upload rejected"), { status });
          },
          get: async () => ({ state: "ACTIVE" }),
        },
      });
    await expect(
      failing(400).structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) }),
    ).rejects.toThrow(/rejected the request \(status 400\)/);
    const transient = await failing(503)
      .structuredCall({ messages: vidMessage, outputSchema, artifacts: groupsOf(oversize()) })
      .then(
        () => undefined,
        (e: unknown) => e,
      );
    expect(transient).toBeInstanceOf(Error);
    expect((transient as Error).name).toBe("ProviderTransientError");
  });

  it("treats an empty-string media_type as missing on the uploaded-hit path (falls back to the file's)", async () => {
    const { provider, calls } = scripted(["ACTIVE"]);
    await provider.structuredCall({
      messages: vidMessage,
      outputSchema,
      artifacts: groupsOf(oversize({ media_type: "" })),
    });
    // The upload config omits the empty mimeType, and the part falls back to the FILE's mime type.
    expect(calls.uploads).toEqual([{ file: bigPath }]);
    const parts = calls.requests[0]?.contents[0]?.parts as Record<string, unknown>[];
    expect(parts[0]).toEqual({ fileData: { fileUri: "gs://files/abc", mimeType: "video/mp4" } });
  });

  it("bails out of a PROCESSING poll when the cancellation signal aborts (#487)", async () => {
    vi.useFakeTimers();
    const { provider } = scripted(["PROCESSING"]);
    const controller = new AbortController();
    const reason = new Error("workflow cancelled");
    const call = provider
      .structuredCall({
        messages: vidMessage,
        outputSchema,
        artifacts: groupsOf(oversize()),
        signal: controller.signal,
      })
      .then(
        () => undefined,
        (e: unknown) => e,
      );
    await vi.advanceTimersByTimeAsync(2_000);
    controller.abort(reason); // lands mid-sleep: the abortable delay ends early
    await vi.advanceTimersByTimeAsync(0);
    expect(await call).toBe(reason); // surfaced immediately, without waiting out the sleep
  });
});
