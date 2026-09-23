import { describe, expect, it } from "vitest";

import { AnthropicProvider, GeminiProvider, OpenAIProvider } from "@typeflux/temporal";

import { loadYamlSpec, providerFromSpec, type ProviderTransports } from "../src/index.js";

const outputSchema = { type: "object", properties: { ok: { type: "boolean" } }, required: ["ok"] } as const;

function specWith(provider: string): string {
  return `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline }
  provider:
${provider}
activities: {}
workflow:
  name: W
  input: schemas:In
  steps: [{ id: s, activity: a }]
`;
}

// Scripted transports that capture the request and return a canned structured response.
function transports(): ProviderTransports & { openaiModel?: string; anthropicModel?: string; geminiModel?: string } {
  const t: ProviderTransports & { openaiModel?: string; anthropicModel?: string; geminiModel?: string } = {
    openai: {
      chat: {
        completions: {
          create: async (request) => {
            t.openaiModel = request.model;
            return { choices: [{ message: { content: JSON.stringify({ ok: true }) } }] };
          },
        },
      },
    },
    anthropic: {
      messages: {
        create: async (request) => {
          t.anthropicModel = request.model;
          return { content: [{ type: "text", text: JSON.stringify({ ok: true }) }], stop_reason: "end_turn" };
        },
      },
    },
    gemini: {
      generateContent: async (request) => {
        t.geminiModel = request.model;
        return { candidates: [{ content: { parts: [{ text: JSON.stringify({ ok: true }) }] }, finishReason: "STOP" }] };
      },
    },
  };
  return t;
}

const call = { messages: [{ role: "user", content: "go" }], outputSchema };

describe("providerFromSpec (#449)", () => {
  it("builds an OpenAIProvider and applies the default model", async () => {
    const t = transports();
    const provider = providerFromSpec(loadYamlSpec(specWith("    type: openai")), t);
    expect(provider).toBeInstanceOf(OpenAIProvider);
    expect(await provider.structuredCall(call)).toEqual({ ok: true });
    expect(t.openaiModel).toBe("gpt-4o-mini"); // default applied
  });

  it("falls back to the default model for an empty-string model (env default `${X:-}`)", async () => {
    const t = transports();
    // `model: ${TYPEFLUX_MODEL:-}` with the var unset interpolates to "" -> default, not "".
    const provider = providerFromSpec(
      loadYamlSpec(specWith("    type: openai\n    model: ${TYPEFLUX_MODEL:-}"), { env: {} }),
      t,
    );
    await provider.structuredCall(call);
    expect(t.openaiModel).toBe("gpt-4o-mini");
  });

  it("builds an AnthropicProvider with the spec's model", async () => {
    const t = transports();
    const provider = providerFromSpec(
      loadYamlSpec(specWith("    type: anthropic\n    model: claude-opus-4-8")),
      t,
    );
    expect(provider).toBeInstanceOf(AnthropicProvider);
    expect(await provider.structuredCall(call)).toEqual({ ok: true });
    expect(t.anthropicModel).toBe("claude-opus-4-8"); // spec model wins
  });

  it("builds a GeminiProvider with the default model", async () => {
    const t = transports();
    const provider = providerFromSpec(loadYamlSpec(specWith("    type: gemini")), t);
    expect(provider).toBeInstanceOf(GeminiProvider);
    expect(await provider.structuredCall(call)).toEqual({ ok: true });
    expect(t.geminiModel).toBe("gemini-2.5-flash"); // default applied
  });

  it("throws when the spec's provider type has no matching transport", () => {
    expect(() => providerFromSpec(loadYamlSpec(specWith("    type: anthropic")), { openai: transports().openai! })).toThrow(
      /no "anthropic" transport was provided/,
    );
  });

  it("throws on an unsupported provider type", () => {
    expect(() => providerFromSpec(loadYamlSpec(specWith("    type: cohere")), transports())).toThrow(
      /unsupported provider type "cohere"/,
    );
  });
});
