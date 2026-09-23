import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ChatMessage, ModelProvider, ResolvedPrompt, StructuredCallParams } from "../src/index.js";
import {
  defineActivity,
  executeActivity,
  InlinePromptRegistry,
  PromptNotFoundError,
  renderMessages,
  renderTemplate,
} from "../src/index.js";

class CapturingProvider implements ModelProvider {
  lastParams: StructuredCallParams | undefined;
  constructor(private readonly response: unknown) {}
  structuredCall(params: StructuredCallParams): unknown {
    this.lastParams = params;
    return this.response;
  }
}

describe("renderTemplate / renderMessages (#448)", () => {
  it("substitutes {{var}} and dot-paths from the input", () => {
    expect(renderTemplate("Hi {{ name }}!", { name: "Avery" })).toBe("Hi Avery!");
    expect(renderTemplate("{{ a.b }}", { a: { b: "deep" } })).toBe("deep");
  });

  it("renders null/undefined as empty and throws on a missing field", () => {
    expect(renderTemplate("[{{ x }}]", { x: null })).toBe("[]");
    expect(() => renderTemplate("{{ missing }}", {})).toThrow(/missing prompt field: missing/);
  });

  it("renders booleans as Python str(bool) for cross-SDK prompt parity", () => {
    expect(renderTemplate("approved={{ ok }}", { ok: true })).toBe("approved=True");
    expect(renderTemplate("approved={{ ok }}", { ok: false })).toBe("approved=False");
  });

  it("does not resolve inherited/prototype members", () => {
    // {{ toString }} / {{ constructor }} exist on the prototype but are not input fields.
    expect(() => renderTemplate("{{ toString }}", { real: "x" })).toThrow(/missing prompt field/);
    expect(() => renderTemplate("{{ constructor }}", {})).toThrow(/missing prompt field/);
  });

  it("renders each message's content, preserving role and name", () => {
    const rendered = renderMessages(
      [
        { role: "system", content: "no vars" },
        { role: "user", content: "Claim {{ id }}", name: "alice" },
      ],
      { id: "CLM-1" },
    );
    expect(rendered).toEqual([
      { role: "system", content: "no vars" },
      { role: "user", content: "Claim CLM-1", name: "alice" },
    ]);
  });
});

describe("InlinePromptRegistry (#448)", () => {
  const ref = (name: string) => ({ name });

  it("normalizes string / message / array / ResolvedPrompt values", () => {
    const reg = new InlinePromptRegistry({
      "p/str": "Hello {{ n }}",
      "p/msg": { role: "system", content: "sys" } as ChatMessage,
      "p/arr": [
        { role: "system", content: "s" },
        { role: "user", content: "u" },
      ],
      "p/resolved": {
        ref: ref("p/resolved"),
        messages: [{ role: "user", content: "r" }],
        model: "preset-model",
      } as ResolvedPrompt,
    });

    expect(reg.resolve(ref("p/str")).messages).toEqual([{ role: "user", content: "Hello {{ n }}" }]);
    expect(reg.resolve(ref("p/msg")).messages).toEqual([{ role: "system", content: "sys" }]);
    expect(reg.resolve(ref("p/arr")).messages).toHaveLength(2);
    expect(reg.resolve(ref("p/resolved")).model).toBe("preset-model");
  });

  it("throws PromptNotFoundError for an unknown ref", () => {
    const reg = new InlinePromptRegistry({});
    expect(() => reg.resolve(ref("missing"))).toThrow(PromptNotFoundError);
  });
});

describe("executeActivity prompt resolution (#448)", () => {
  const activity = defineActivity({
    name: "classify",
    prompt: { name: "p/classify", label: "production" },
    input: z.object({ text: z.string() }),
    output: z.object({ label: z.string() }),
  });

  it("resolves the prompt ref and renders it against the input", async () => {
    const provider = new CapturingProvider({ label: "billing" });
    const registry = new InlinePromptRegistry({
      "p/classify": [
        { role: "system", content: "Classify the ticket." },
        { role: "user", content: "Ticket: {{ text }}" },
      ],
    });

    const out = await executeActivity(activity, { text: "refund please" }, { provider, registry });

    expect(out).toEqual({ label: "billing" });
    // The provider received the RENDERED messages — the SDK resolved + rendered them.
    expect(provider.lastParams?.messages).toEqual([
      { role: "system", content: "Classify the ticket." },
      { role: "user", content: "Ticket: refund please" },
    ]);
  });

  it("uses the resolved prompt's model when the call sets none", async () => {
    const provider = new CapturingProvider({ label: "x" });
    const registry = new InlinePromptRegistry({
      "p/classify": {
        ref: { name: "p/classify" },
        messages: [{ role: "user", content: "go" }],
        model: "registry-model",
      } as ResolvedPrompt,
    });
    await executeActivity(activity, { text: "hi" }, { provider, registry });
    expect(provider.lastParams?.model).toBe("registry-model");
  });

  it("explicit messages override the registry (back-compat)", async () => {
    const provider = new CapturingProvider({ label: "x" });
    const registry = new InlinePromptRegistry({ "p/classify": "from registry: {{ text }}" });
    await executeActivity(activity, { text: "hi" }, {
      provider,
      registry,
      messages: [{ role: "user", content: "explicit" }],
    });
    expect(provider.lastParams?.messages).toEqual([{ role: "user", content: "explicit" }]);
  });

  it("throws when neither messages nor registry is given", async () => {
    const provider = new CapturingProvider({ label: "x" });
    await expect(executeActivity(activity, { text: "hi" }, { provider })).rejects.toThrow(
      /requires either/,
    );
  });
});
