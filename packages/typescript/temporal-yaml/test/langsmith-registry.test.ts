/**
 * Out-of-the-box LangSmith prompt registry (`langsmith-registry.ts`): the
 * manifest parsing (ChatPromptTemplate/PromptTemplate, role map, f-string →
 * mustache conversion, model-bound rejection), the `name:selector` identifier
 * rule, and the spec/env gate `buildRuntime` calls. The SDK client is faked
 * structurally (`LangsmithRegistrySdkClient`); the real `langsmith` package is
 * only touched by the construct-only test. Env is always passed EXPLICITLY:
 * the shared network guard scrubs LANGSMITH_* from unit-test env.
 */

import { describe, expect, it } from "vitest";

import {
  PromptNotFoundError,
  PromptRegistryConfigError,
  PromptRegistryUnavailableError,
} from "@typeflux/temporal";

import {
  langsmithPromptIdentifier,
  langsmithRegistryTransportFromSpec,
  LangsmithRegistryTransport,
  loadYamlSpec,
} from "../src/index.js";
import type { LangsmithPromptCommit } from "../src/index.js";

const YAML = `
project: reg_demo
name: reg_demo
task_queue: reg-demo
runtime:
  temporal:
    address: localhost:7233
  registry:
    type: langsmith
  provider:
    type: openai
activities:
  definitions:
    - name: act
      input: schemas:Item
      output: schemas:Item
      prompt: p
workflow:
  name: RegDemoWorkflow
  input: schemas:Item
  output: schemas:Item
  steps:
    - id: act
      activity: act
`;

function specWithRegistry(type: string, extra = "") {
  return loadYamlSpec(YAML.replace("    type: langsmith", `    type: ${type}${extra}`));
}

class FakeClient {
  identifiers: string[] = [];
  constructor(private readonly result: LangsmithPromptCommit) {}

  async pullPromptCommit(identifier: string): Promise<LangsmithPromptCommit> {
    this.identifiers.push(identifier);
    return this.result;
  }
}

/** A LangChain chat-prompt commit manifest as the hub serializes it. */
function chatManifest(
  messages: unknown[],
  kind = "ChatPromptTemplate",
): Record<string, unknown> {
  return {
    lc: 1,
    type: "constructor",
    id: ["langchain", "prompts", "chat", kind],
    kwargs: { messages },
  };
}

function manifestMessage(messageClass: string, template: string, format?: string): unknown {
  return {
    lc: 1,
    type: "constructor",
    id: ["langchain", "prompts", "chat", messageClass],
    kwargs: {
      prompt: {
        lc: 1,
        type: "constructor",
        id: ["langchain", "prompts", "prompt", "PromptTemplate"],
        kwargs: { template, ...(format !== undefined ? { template_format: format } : {}) },
      },
    },
  };
}

describe("langsmithPromptIdentifier", () => {
  it("selects by label, then version-as-string, then the bare name (Python rule)", () => {
    expect(langsmithPromptIdentifier({ name: "p", label: "production" })).toBe("p:production");
    expect(langsmithPromptIdentifier({ name: "p", version: 7 })).toBe("p:7");
    expect(langsmithPromptIdentifier({ name: "p" })).toBe("p");
    // Truthy guard: an empty-string label is no selector (Python `if selector`).
    expect(langsmithPromptIdentifier({ name: "p", label: "" })).toBe("p");
  });
});

describe("LangsmithRegistryTransport", () => {
  it("maps a ChatPromptTemplate manifest to role-preserving mustache messages", async () => {
    const client = new FakeClient({
      commit_hash: "abc123def",
      manifest: chatManifest([
        manifestMessage("SystemMessagePromptTemplate", "You review {subject}."),
        manifestMessage("HumanMessagePromptTemplate", "Data: {data.field} {{literal}}"),
        manifestMessage("AIMessagePromptTemplate", "ok", "mustache"),
      ]),
    });
    const transport = new LangsmithRegistryTransport(client);
    const raw = await transport.fetchPrompt({ name: "p", label: "production" });
    expect(client.identifiers).toEqual(["p:production"]);
    expect(raw.messages).toEqual([
      // f-string {var} → mustache {{var}} (the default template_format).
      { role: "system", content: "You review {{subject}}." },
      // Dotted identifiers convert; f-string {{ }} literal braces are preserved.
      { role: "user", content: "Data: {{data.field}} {literal}" },
      // mustache passes through untouched.
      { role: "assistant", content: "ok" },
    ]);
    // The commit hash is the resolved version (it lands in the manifests).
    expect(raw.resolvedVersion).toBe("abc123def");
  });

  it("maps a text PromptTemplate manifest to a single user message", async () => {
    const transport = new LangsmithRegistryTransport(
      new FakeClient({
        commit_hash: "c1",
        manifest: {
          id: ["langchain", "prompts", "prompt", "PromptTemplate"],
          kwargs: { template: "Summarize {doc}", template_format: "f-string" },
        },
      }),
    );
    const raw = await transport.fetchPrompt({ name: "p" });
    expect(raw.messages).toEqual([{ role: "user", content: "Summarize {{doc}}" }]);
  });

  it("rejects model-bound prompts with Python's config error", async () => {
    for (const kind of ["RunnableSequence", "RunnableBinding"]) {
      const transport = new LangsmithRegistryTransport(
        new FakeClient({ commit_hash: "c", manifest: chatManifest([], kind) }),
      );
      await expect(transport.fetchPrompt({ name: "p" })).rejects.toThrow(
        `LangSmith prompt 'p' is bound to a model (${kind}); Typeflux owns ` +
          "provider selection, so store the prompt without a model binding",
      );
    }
  });

  it("rejects empty/malformed manifests with Python's texts", async () => {
    const noManifest = new LangsmithRegistryTransport(new FakeClient({ commit_hash: "c" }));
    await expect(noManifest.fetchPrompt({ name: "p" })).rejects.toThrow(
      "LangSmith prompt 'p' returned no manifest",
    );
    const noMessages = new LangsmithRegistryTransport(
      new FakeClient({ commit_hash: "c", manifest: chatManifest([]) }),
    );
    await expect(noMessages.fetchPrompt({ name: "p" })).rejects.toThrow(
      "LangSmith prompt 'p' contained no messages",
    );
    const unknownKind = new LangsmithRegistryTransport(
      new FakeClient({ commit_hash: "c", manifest: chatManifest([], "FewShotPromptTemplate") }),
    );
    await expect(unknownKind.fetchPrompt({ name: "p" })).rejects.toThrow(
      "unsupported LangSmith prompt type 'FewShotPromptTemplate' for 'p'",
    );
    const unknownMessage = new LangsmithRegistryTransport(
      new FakeClient({
        commit_hash: "c",
        manifest: chatManifest([manifestMessage("ToolMessagePromptTemplate", "t")]),
      }),
    );
    await expect(unknownMessage.fetchPrompt({ name: "p" })).rejects.toThrow(
      "unsupported LangSmith chat message 'ToolMessagePromptTemplate' for 'p'",
    );
    const noTemplate = new LangsmithRegistryTransport(
      new FakeClient({
        commit_hash: "c",
        manifest: chatManifest([
          { id: ["langchain", "prompts", "chat", "HumanMessagePromptTemplate"], kwargs: {} },
        ]),
      }),
    );
    await expect(noTemplate.fetchPrompt({ name: "p" })).rejects.toThrow(
      "LangSmith prompt 'p' message has no template text",
    );
  });

  it("rejects unrenderable f-string placeholders and unknown template formats loud", async () => {
    const formatted = new LangsmithRegistryTransport(
      new FakeClient({
        commit_hash: "c",
        manifest: chatManifest([manifestMessage("HumanMessagePromptTemplate", "score: {x:.2f}")]),
      }),
    );
    await expect(formatted.fetchPrompt({ name: "p" })).rejects.toThrow(
      "LangSmith prompt 'p' has an f-string placeholder '{x:.2f}' that is not a plain " +
        "{{name}} Typeflux can render; pre-format it in the prompt or store the prompt " +
        "in mustache format",
    );
    const jinja = new LangsmithRegistryTransport(
      new FakeClient({
        commit_hash: "c",
        manifest: chatManifest([manifestMessage("HumanMessagePromptTemplate", "t", "jinja2")]),
      }),
    );
    await expect(jinja.fetchPrompt({ name: "p" })).rejects.toThrow(
      "LangSmith prompt 'p' uses unsupported template_format 'jinja2'; " +
        "Typeflux supports 'mustache' and 'f-string'",
    );
  });

  it("classifies SDK failures into the typed hierarchy (name tokens + status)", async () => {
    const failing = (error: Error) =>
      new LangsmithRegistryTransport({
        pullPromptCommit: async () => {
          throw error;
        },
      });
    const notFound = new Error("Failed to pull prompt commit. Received status [404]");
    notFound.name = "LangSmithNotFoundError";
    await expect(failing(notFound).fetchPrompt({ name: "missing" })).rejects.toBeInstanceOf(
      PromptNotFoundError,
    );
    await expect(failing(notFound).fetchPrompt({ name: "missing", label: "prod" })).rejects.toThrow(
      "failed to resolve LangSmith prompt missing@prod",
    );
    const outage = new TypeError("fetch failed");
    await expect(failing(outage).fetchPrompt({ name: "p" })).rejects.toBeInstanceOf(
      PromptRegistryUnavailableError,
    );
    const conflict = Object.assign(new Error("bad request"), { status: 422 });
    await expect(failing(conflict).fetchPrompt({ name: "p" })).rejects.toBeInstanceOf(
      PromptRegistryConfigError,
    );
  });
});

describe("langsmithRegistryTransportFromSpec", () => {
  const KEYS = { LANGSMITH_API_KEY: "ls-test" };

  it("returns undefined for every other registry type", async () => {
    await expect(
      langsmithRegistryTransportFromSpec(
        specWithRegistry("inline", "\n    prompts:\n      p: hi"),
        KEYS,
      ),
    ).resolves.toBeUndefined();
    await expect(
      langsmithRegistryTransportFromSpec(specWithRegistry("langfuse"), KEYS),
    ).resolves.toBeUndefined();
  });

  it("THROWS (no observability-style degrade) when the API key is absent", async () => {
    await expect(langsmithRegistryTransportFromSpec(specWithRegistry("langsmith"), {})).rejects.toThrow(
      /LANGSMITH_API_KEY is not set.*registryTransport/s,
    );
  });

  it("builds a transport over the official SDK when the spec + env opt in", async () => {
    // Construct-only: no prompt is pulled, so no network leaves the test.
    const transport = await langsmithRegistryTransportFromSpec(specWithRegistry("langsmith"), {
      ...KEYS,
      LANGSMITH_HOST: "http://localhost:9",
    });
    expect(transport).toBeInstanceOf(LangsmithRegistryTransport);
  });
});
