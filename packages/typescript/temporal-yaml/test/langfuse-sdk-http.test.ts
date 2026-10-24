/** Real scoped SDK serialization, HTTP errors and cursor responses without a live service. */
import { LangfuseClient } from "@langfuse/client";
import { describe, expect, it, vi } from "vitest";
import { LangfuseRegistryTransport } from "../src/langfuse-registry.js";
import { deleteTracesForSubject, type LangfuseTraceApiClient } from "../src/subject-trace-deletion.js";
import { PromptNotFoundError, PromptRegistryAuthError, PromptRegistryUnavailableError } from "@typeflux/temporal";

function sdk() {
  const client = new LangfuseClient({ publicKey: "pk-http-test", secretKey: "sk-http-test", baseUrl: "http://localhost:9" });
  // Retry behavior belongs to Temporal; make each test's transport failure deterministic.
  return { api: client.api, prompt: { get: (name: string, options?: { version?: number; label?: string }) =>
    client.prompt.get(name, { ...options, maxRetries: 0, cacheTtlSeconds: 0 }) } };
}

describe("Langfuse 6 HTTP contract", () => {
  it.each(["text", "chat"])("resolves %s prompts through the scoped client", async (type) => {
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      expect(url.pathname).toBe("/api/public/v2/prompts/review");
      expect(url.searchParams.get("label")).toBe("production");
      return new Response(JSON.stringify({ name: "review", version: 3, type,
        prompt: type === "text" ? "Hello {{name}}" : [{ role: "user", content: "Hello {{name}}" }],
        config: {}, tags: [], labels: ["production"] }), { headers: { "content-type": "application/json" } });
    });
    try {
      const result = await new LangfuseRegistryTransport(sdk()).fetchPrompt({ name: "review", label: "production" });
      expect(result.messages).toEqual([{ role: "user", content: "Hello {{name}}" }]);
      expect(result.resolvedVersion).toBe("3");
    } finally { fetch.mockRestore(); }
  });

  it.each([[404, PromptNotFoundError], [401, PromptRegistryAuthError], [403, PromptRegistryAuthError],
    [429, PromptRegistryUnavailableError], [503, PromptRegistryUnavailableError]] as const)("classifies SDK HTTP %i", async (status, errorType) => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ message: "opaque" }), { status, headers: { "content-type": "application/json" } }));
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    try { await expect(new LangfuseRegistryTransport(sdk()).fetchPrompt({ name: "review" })).rejects.toBeInstanceOf(errorType); }
    finally { fetch.mockRestore(); log.mockRestore(); }
  });

  it.each([new TypeError("fetch failed"), new DOMException("aborted", "AbortError")])("keeps SDK transport failures retryable: %s", async (error) => {
    const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(error);
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    try { await expect(new LangfuseRegistryTransport(sdk()).fetchPrompt({ name: "review" })).rejects.toBeInstanceOf(PromptRegistryUnavailableError); }
    finally { fetch.mockRestore(); log.mockRestore(); }
  });

  it("uses v2 identity filters then unfiltered cursor hydration, excluding a foreign subject found on page two", async () => {
    const seen: URL[] = [];
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      seen.push(url);
      expect(url.pathname).toBe("/api/public/v2/observations");
      const q = url.searchParams;
      let body;
      if (q.has("traceId")) {
        expect(q.has("filter")).toBe(false);
        body = { data: [{ id: "o", traceId: "shared", tags: ["typeflux.subject:s1"], userId: q.has("cursor") ? "s2" : "s1" }], meta: { cursor: q.has("cursor") ? null : "opaque+/=" } };
      } else {
        expect(JSON.parse(q.get("filter")!)).toEqual(expect.arrayContaining([expect.objectContaining({ column: expect.stringMatching(/traceTags|userId/) })]));
        body = { data: [{ id: "o", traceId: "shared", tags: ["typeflux.subject:s1"] }], meta: { cursor: null } };
      }
      return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
    });
    try {
      const client: LangfuseTraceApiClient = sdk();
      const report = await deleteTracesForSubject(client, "s1", { dryRun: false });
      expect(report.deletedCount).toBe(0);
      expect(report.conflicted).toEqual([{ traceId: "shared", otherSubjectCount: 1 }]);
      expect(seen.at(-1)!.searchParams.get("cursor")).toBe("opaque+/=");
    } finally { fetch.mockRestore(); }
  });
  it("serializes the supported bulk delete after complete ownership checks", async () => {
    const methods: string[] = [];
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      methods.push(method);
      if (method === "DELETE") {
        expect(url.pathname).toBe("/api/public/traces");
        expect(JSON.parse(String(init?.body))).toEqual({ traceIds: ["owned"] });
        return new Response(JSON.stringify({ message: "deleted" }));
      }
      expect(url.pathname).toBe("/api/public/v2/observations");
      return new Response(JSON.stringify({ data: [{ traceId: "owned", tags: ["typeflux.subject:s1"], userId: "s1" }], meta: { cursor: null } }));
    });
    try {
      expect((await deleteTracesForSubject(sdk(), "s1", { dryRun: false })).deletedCount).toBe(1);
      expect(methods).toEqual(["GET", "GET", "GET", "DELETE"]);
    } finally { fetch.mockRestore(); }
  });

});
