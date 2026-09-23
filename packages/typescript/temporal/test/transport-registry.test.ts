import { describe, expect, it } from "vitest";

import type { PromptRef, RawPrompt, RegistryTransport } from "../src/index.js";
import { TransportPromptRegistry } from "../src/index.js";

class CapturingTransport implements RegistryTransport {
  lastRef: PromptRef | undefined;
  constructor(private readonly raw: RawPrompt) {}
  async fetchPrompt(ref: PromptRef): Promise<RawPrompt> {
    this.lastRef = ref;
    return this.raw;
  }
}

describe("TransportPromptRegistry (#452)", () => {
  it("resolves a ref via the transport — a backend model is STRIPPED by default (#495)", async () => {
    // Python allow_prompt_model_override=False parity: the provider/YAML config stays
    // authoritative for the execution model; the backend prompt's model is metadata.
    const transport = new CapturingTransport({
      messages: [{ role: "user", content: "hi" }],
      model: "gpt-4o",
      temperature: 0.2,
      providerParams: { model: "gpt-4o", top_p: 0.9 },
    });
    const registry = new TransportPromptRegistry(transport);

    const resolved = await registry.resolve({ name: "p/x", version: 3 });

    expect(resolved).toEqual({
      ref: { name: "p/x", version: 3 },
      messages: [{ role: "user", content: "hi" }],
      resolvedVersion: "3",
      temperature: 0.2,
      providerParams: { top_p: 0.9 }, // params survive; the model inside them does not
    });
    expect(transport.lastRef).toEqual({ name: "p/x", version: 3 });
  });

  it("allowPromptModelOverride passes the backend model through (#495)", async () => {
    const transport = new CapturingTransport({
      messages: [{ role: "user", content: "hi" }],
      model: "gpt-4o",
      providerParams: { model: "gpt-4o" },
    });
    const registry = new TransportPromptRegistry(transport, { allowPromptModelOverride: true });
    const resolved = await registry.resolve({ name: "p/x" });
    expect(resolved.model).toBe("gpt-4o");
    expect(resolved.providerParams).toEqual({ model: "gpt-4o" });
  });

  it("applies defaultLabel only when the ref pins neither a version nor a label", async () => {
    const transport = new CapturingTransport({ messages: [] });
    const registry = new TransportPromptRegistry(transport, { defaultLabel: "production" });

    await registry.resolve({ name: "p/x" });
    expect(transport.lastRef).toEqual({ name: "p/x", label: "production" }); // default applied

    await registry.resolve({ name: "p/y", version: 2 });
    expect(transport.lastRef).toEqual({ name: "p/y", version: 2 }); // version pin wins

    await registry.resolve({ name: "p/z", label: "staging" });
    expect(transport.lastRef).toEqual({ name: "p/z", label: "staging" }); // explicit label wins
  });

  it("treats an empty-string defaultLabel as no override", async () => {
    const transport = new CapturingTransport({ messages: [] });
    const registry = new TransportPromptRegistry(transport, { defaultLabel: "" });
    await registry.resolve({ name: "p/x" });
    expect(transport.lastRef).toEqual({ name: "p/x" }); // no label applied
  });

  it("defaults resolvedVersion to the ref version when the transport omits it", async () => {
    const registry = new TransportPromptRegistry(new CapturingTransport({ messages: [] }));
    expect((await registry.resolve({ name: "p", version: 5 })).resolvedVersion).toBe("5");
    expect((await registry.resolve({ name: "p" })).resolvedVersion).toBe(null);
  });
});
