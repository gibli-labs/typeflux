import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, StructuredCallParams } from "@typeflux/temporal";
import { executeActivity, InlinePromptRegistry, ModerationBlockedError, TransportPromptRegistry } from "@typeflux/temporal";

import {
  defineActivitiesFromSpec,
  inlineRegistryFromSpec,
  loadYamlSpec,
  registryFromSpec,
  toPromptRef,
} from "../src/index.js";
// Spec sub-schemas are internal composition — deliberately not on the package surface (#493).
import { inlinePromptMessageSpec, promptRefSpec } from "../src/spec.js";

class CapturingProvider implements ModelProvider {
  lastMessages: { role: string; content: unknown }[] = [];
  constructor(private readonly response: unknown) {}
  structuredCall(params: StructuredCallParams): unknown {
    this.lastMessages = params.messages;
    return this.response;
  }
}

const SPEC = `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry:
    type: inline
    prompts:
      p/summarize:
        messages:
          - role: system
            content: You summarize.
          - role: user
            content: "summarize: {{ text }}"
        model: gpt-4o
        temperature: 0.2
      p/plain: just a plain string prompt
  provider:
    type: openai
activities:
  definitions:
    - name: summarize
      input: schemas:In
      output: schemas:Out
      prompt: p/summarize
      validation_retries: 2
workflow:
  name: W
  input: schemas:In
  steps:
    - id: s
      activity: summarize
`;

const schemas = {
  "schemas:In": z.object({ text: z.string() }),
  "schemas:Out": z.object({ summary: z.string() }),
};

describe("toPromptRef (#452)", () => {
  it("maps a bare string and a ref object", () => {
    expect(toPromptRef("p/x")).toEqual({ name: "p/x" });
    expect(toPromptRef({ name: "p/x", label: "production", type: "chat" })).toEqual({
      name: "p/x",
      label: "production",
      promptType: "chat",
    });
  });
});

describe("spec schema coercion/validation (#452)", () => {
  it("coerces a string prompt version (env-default form)", () => {
    expect(promptRefSpec.parse({ name: "p", version: "3" }).version).toBe(3);
  });

  it("rejects an inline-prompt content part with no type discriminant (fails loudly)", () => {
    expect(() => inlinePromptMessageSpec.parse({ role: "user", content: [{ foo: "bar" }] })).toThrow();
  });

  it("accepts a typed content part", () => {
    const m = inlinePromptMessageSpec.parse({ role: "user", content: [{ type: "text", text: "hi" }] });
    expect(m.content).toEqual([{ type: "text", text: "hi" }]);
  });
});

describe("inlineRegistryFromSpec (#452)", () => {
  it("builds an InlinePromptRegistry from inline prompts", () => {
    const registry = inlineRegistryFromSpec(loadYamlSpec(SPEC));
    expect(registry).toBeDefined();
    const resolved = registry!.resolve({ name: "p/summarize" });
    expect(resolved.messages).toEqual([
      { role: "system", content: "You summarize." },
      { role: "user", content: "summarize: {{ text }}" },
    ]);
    expect(resolved.model).toBe("gpt-4o");
    expect(resolved.temperature).toBe(0.2);
    expect(resolved.resolvedVersion).toBe("inline");
    // A bare-string prompt becomes a single user message.
    expect(registry!.resolve({ name: "p/plain" }).messages).toEqual([
      { role: "user", content: "just a plain string prompt" },
    ]);
  });

  it("returns undefined for a non-inline registry (caller supplies the client)", () => {
    const langfuse = loadYamlSpec(SPEC.replace("type: inline", "type: langfuse"));
    expect(inlineRegistryFromSpec(langfuse)).toBeUndefined();
  });
});

describe("registryFromSpec (#452)", () => {
  it("returns an InlinePromptRegistry for an inline spec", () => {
    expect(registryFromSpec(loadYamlSpec(SPEC))).toBeInstanceOf(InlinePromptRegistry);
  });

  it("returns a TransportPromptRegistry for langfuse when a transport is given", () => {
    const langfuse = loadYamlSpec(SPEC.replace("type: inline", "type: langfuse"));
    const registry = registryFromSpec(langfuse, { fetchPrompt: () => ({ messages: [] }) });
    expect(registry).toBeInstanceOf(TransportPromptRegistry);
  });

  it("returns undefined for langfuse without a transport", () => {
    const langfuse = loadYamlSpec(SPEC.replace("type: inline", "type: langfuse"));
    expect(registryFromSpec(langfuse)).toBeUndefined();
  });
});

describe("defineActivitiesFromSpec moderation (#452)", () => {
  const withModeration = (extra: string) =>
    loadYamlSpec(SPEC.replace("      validation_retries: 2", `      validation_retries: 2\n      moderation: { ${extra} }`));

  it("wires an injected moderator and blocks a flagged output", async () => {
    const spec = withModeration("provider: openai");
    const activities = defineActivitiesFromSpec(spec, {
      schemas,
      moderators: { summarize: () => ({ flagged: true, categories: ["x"] }) },
    });
    await expect(
      executeActivity(activities["summarize"]!, { text: "x" }, {
        provider: new CapturingProvider({ summary: "bad" }),
        registry: inlineRegistryFromSpec(spec)!,
      }),
    ).rejects.toBeInstanceOf(ModerationBlockedError);
  });

  it("passes a flagged output through when on_violation is `flag`", async () => {
    const spec = withModeration("provider: openai, on_violation: flag");
    const activities = defineActivitiesFromSpec(spec, {
      schemas,
      moderators: { summarize: () => ({ flagged: true }) },
    });
    const out = await executeActivity(activities["summarize"]!, { text: "x" }, {
      provider: new CapturingProvider({ summary: "flagged" }),
      registry: inlineRegistryFromSpec(spec)!,
    });
    expect(out).toEqual({ summary: "flagged" });
  });

  it("throws when a definition declares moderation but no moderator is injected", () => {
    expect(() => defineActivitiesFromSpec(withModeration("provider: openai"), { schemas })).toThrow(
      /declares moderation but no moderator/,
    );
  });

  it("does not pick up an inherited member as a moderator (prototype safety)", () => {
    const spec = loadYamlSpec(
      SPEC.replace("name: summarize", "name: toString").replace(
        "      validation_retries: 2",
        "      validation_retries: 2\n      moderation: { provider: openai }",
      ),
    );
    // Empty moderators (no OWN "toString" entry) must throw, not run Object.prototype.toString.
    expect(() => defineActivitiesFromSpec(spec, { schemas, moderators: {} })).toThrow(
      /declares moderation but no moderator/,
    );
  });
});

describe("defineActivitiesFromSpec (#452)", () => {
  it("builds descriptors and runs spec -> activity -> provider end to end", async () => {
    const spec = loadYamlSpec(SPEC);
    const activities = defineActivitiesFromSpec(spec, { schemas });
    const registry = inlineRegistryFromSpec(spec);
    expect(Object.keys(activities)).toEqual(["summarize"]);

    const provider = new CapturingProvider({ summary: "done" });
    const out = await executeActivity(activities["summarize"]!, { text: "hello" }, { provider, registry: registry! });

    expect(out).toEqual({ summary: "done" });
    // The inline prompt's user message rendered against the input.
    expect(provider.lastMessages.at(-1)).toEqual({ role: "user", content: "summarize: hello" });
  });

  it("applies an injected hook keyed by activity name", async () => {
    const spec = loadYamlSpec(SPEC);
    const activities = defineActivitiesFromSpec(spec, {
      schemas,
      hooks: { summarize: (_input, output) => ({ summary: (output as { summary: string }).summary.toUpperCase() }) },
    });
    const out = await executeActivity(
      activities["summarize"]!,
      { text: "x" },
      { provider: new CapturingProvider({ summary: "loud" }), registry: inlineRegistryFromSpec(spec)! },
    );
    expect(out).toEqual({ summary: "LOUD" });
  });

  it("applies an injected outputCheck (keyed by activity name) that feeds the repair loop (#745)", async () => {
    const spec = loadYamlSpec(SPEC);
    // The grounding contract zod cannot express: the summary must ECHO the input text — it
    // needs the parsed INPUT to check. A first response that violates it must be repaired.
    const activities = defineActivitiesFromSpec(spec, {
      schemas,
      outputChecks: {
        summarize: (input, output) =>
          (output as { summary: string }).summary === (input as { text: string }).text
            ? []
            : [{ message: "summary must echo input.text", path: ["summary"] }],
      },
    });
    // A scripted provider: bad first (violates the check → repair), good on the retry.
    class ScriptedProvider implements ModelProvider {
      calls = 0;
      structuredCall(): unknown {
        this.calls += 1;
        return this.calls === 1 ? { summary: "WRONG" } : { summary: "hi" };
      }
    }
    const provider = new ScriptedProvider();
    const out = await executeActivity(
      activities["summarize"]!,
      { text: "hi" },
      { provider, registry: inlineRegistryFromSpec(spec)! },
    );
    expect(out).toEqual({ summary: "hi" }); // self-corrected on attempt 2
    expect(provider.calls).toBe(2);
  });

  it("looks up an injected outputCheck by OWN property (not the prototype chain)", () => {
    // Parity with the hook/schema/moderator lookups: a `Record` inherits `toString`, so an
    // activity NOT in the map must resolve to undefined, never an inherited member.
    const spec = loadYamlSpec(SPEC);
    const activities = defineActivitiesFromSpec(spec, { schemas, outputChecks: {} });
    expect(activities["summarize"]!.outputCheck).toBeUndefined();
  });

  it("throws on an unresolved schema ref", () => {
    const spec = loadYamlSpec(SPEC);
    expect(() => defineActivitiesFromSpec(spec, { schemas: {} })).toThrow(
      /unresolved input schema "schemas:In" for activity "summarize"/,
    );
  });

  it("treats a ref named like a prototype member as UNRESOLVED (not the inherited Object member)", () => {
    // `input`/`output` are free strings, so a ref of "constructor" must MISS rather than
    // resolve to `Object` via the prototype chain — else a non-Zod value reaches the runtime.
    const spec = loadYamlSpec(SPEC.replace("input: schemas:In\n      output: schemas:Out", "input: constructor\n      output: schemas:Out"));
    expect(() => defineActivitiesFromSpec(spec, { schemas })).toThrow(
      /unresolved input schema "constructor" for activity "summarize"/,
    );
  });

  it("throws on a duplicate activity definition name", () => {
    const dup = SPEC.replace(
      "    - name: summarize\n      input: schemas:In\n      output: schemas:Out\n      prompt: p/summarize\n      validation_retries: 2",
      "    - name: dup\n      input: schemas:In\n      output: schemas:Out\n      prompt: p/plain\n    - name: dup\n      input: schemas:In\n      output: schemas:Out\n      prompt: p/plain",
    );
    expect(() => defineActivitiesFromSpec(loadYamlSpec(dup), { schemas })).toThrow(/duplicate activity definition/);
  });
});

describe("defineActivitiesFromSpec artifacts (#481 PR4)", () => {
  const withArtifacts = loadYamlSpec(
    SPEC.replace(
      "      validation_retries: 2",
      `      validation_retries: 2
      artifacts:
        - name: docs
          from: input.docs
          media_types: [application/pdf, image/*]
          max_count: 3
          attach: { role: user, text: "Reference documents:" }`,
    ),
  );

  it("maps a definition's artifacts to the descriptor contract (from -> from_path)", () => {
    const activities = defineActivitiesFromSpec(withArtifacts, { schemas });
    const artifacts = activities["summarize"]?.artifacts;
    expect(artifacts).toHaveLength(1);
    expect(artifacts?.[0]).toMatchObject({
      name: "docs",
      from_path: "input.docs",
      required: true,
      media_types: ["application/pdf", "image/*"],
      max_count: 3,
      attach: { role: "user", text: "Reference documents:" },
    });
  });

  it("wires artifact cache: reference to the contract's cache_role (#478 — attach required)", () => {
    // Without attach the contract factory rejects (a reference artifact that never
    // attaches would be silently lost from the cached prefix).
    expect(() =>
      defineActivitiesFromSpec(
        loadYamlSpec(
          SPEC.replace(
            "      validation_retries: 2",
            "      validation_retries: 2\n      artifacts:\n        - name: d\n          from: input.d\n          cache: reference",
          ),
        ),
        { schemas },
      ),
    ).toThrow(/attach/);
    const wired = defineActivitiesFromSpec(
      loadYamlSpec(
        SPEC.replace(
          "      validation_retries: 2",
          `      validation_retries: 2
      artifacts:
        - name: d
          from: input.d
          cache: reference
          attach: { role: user, text: "Doc:" }`,
        ),
      ),
      { schemas },
    );
    expect(wired["summarize"]?.artifacts?.[0]?.cache_role).toBe("reference");
  });

  it("#362: load-rejects an optional, textless cache: reference artifact (naming it)", () => {
    // An optional, textless reference artifact emits no attach message for items
    // where it resolves empty — the conversation shape (and the prefix breakpoint
    // index) would vary across items. Fail-closed at spec load.
    expect(() =>
      defineActivitiesFromSpec(
        loadYamlSpec(
          SPEC.replace(
            "      validation_retries: 2",
            `      validation_retries: 2
      artifacts:
        - name: optional_doc
          from: input.d
          cache: reference
          required: false
          attach: { role: user }`,
          ),
        ),
        { schemas },
      ),
    ).toThrow(/"optional_doc".*must always produce its attach message/);
  });

  it("maps an activity cache block to the descriptor's sessionCache (#478)", () => {
    const cached = defineActivitiesFromSpec(
      loadYamlSpec(
        SPEC.replace(
          "      validation_retries: 2",
          "      validation_retries: 2\n      cache: { enabled: true, ttl_seconds: 600 }",
        ),
      ),
      { schemas },
    );
    expect(cached["summarize"]?.sessionCache).toEqual({ enabled: true, ttlSeconds: 600 });
  });

  it("maps a cross_run_cache block to the descriptor's cross-run cache config (#398/#753)", () => {
    const cached = defineActivitiesFromSpec(
      loadYamlSpec(
        SPEC.replace(
          "      validation_retries: 2",
          "      validation_retries: 2\n      cross_run_cache: { enabled: true, bypass_reads_env: DISABLE_X }",
        ),
      ),
      { schemas },
    );
    // The cross-run cache lands on descriptor.cache (the memoization axis), NOT sessionCache.
    expect(cached["summarize"]?.cache).toEqual({ enabled: true, bypassReadsEnv: "DISABLE_X" });
    expect(cached["summarize"]?.sessionCache).toBeUndefined();
  });

  it("wires session cache and cross_run_cache onto distinct descriptor fields (#478 + #753)", () => {
    const cached = defineActivitiesFromSpec(
      loadYamlSpec(
        SPEC.replace(
          "      validation_retries: 2",
          "      validation_retries: 2\n      cache: { enabled: true, ttl_seconds: 600 }\n      cross_run_cache: { enabled: true }",
        ),
      ),
      { schemas },
    );
    expect(cached["summarize"]?.sessionCache).toEqual({ enabled: true, ttlSeconds: 600 });
    expect(cached["summarize"]?.cache).toEqual({ enabled: true });
  });

  it("rejects a system-role attach on a NON-reference input of a session-cached activity (#478)", () => {
    const spec = SPEC.replace(
      "      validation_retries: 2",
      `      validation_retries: 2
      cache: { enabled: true }
      artifacts:
        - name: d
          from: input.d
          attach: { role: system, text: "Doc:" }`,
    );
    // Per-item system content is dropped on reference-style cache hits (the provider
    // cannot send it alongside the cached prefix) — rejected at LOAD, not mid-fan-out.
    expect(() => loadYamlSpec(spec)).toThrow(/system/);
    // The same attach on a cache: reference input is FINE — it joins the cached prefix.
    const reference = spec.replace("          attach:", "          cache: reference\n          attach:");
    expect(() => loadYamlSpec(reference)).not.toThrow();
  });

  it("rejects an untrimmed from/name at LOAD time (Python parity — not at assemble time)", () => {
    expect(() =>
      loadYamlSpec(
        SPEC.replace(
          "      validation_retries: 2",
          '      validation_retries: 2\n      artifacts:\n        - name: docs\n          from: "input.docs "',
        ),
      ),
    ).toThrow(/artifact input fields must be non-empty and trimmed/);
  });

  it("treats max_bytes/max_count null as no limit (Python None), not a zero cap", () => {
    const spec = loadYamlSpec(
      SPEC.replace(
        "      validation_retries: 2",
        "      validation_retries: 2\n      artifacts:\n        - name: docs\n          from: input.docs\n          max_bytes: null\n          max_count: null",
      ),
    );
    const activities = defineActivitiesFromSpec(spec, { schemas });
    const input = activities["summarize"]?.artifacts?.[0];
    expect(input?.max_bytes).toBeUndefined();
    expect(input?.max_count).toBeUndefined();
  });

  it("rejects a from path that does not start with input. (contract invariant)", () => {
    expect(() =>
      defineActivitiesFromSpec(
        loadYamlSpec(
          SPEC.replace(
            "      validation_retries: 2",
            "      validation_retries: 2\n      artifacts:\n        - name: d\n          from: docs",
          ),
        ),
        { schemas },
      ),
    ).toThrow(/must start with 'input\.'/);
  });
});
