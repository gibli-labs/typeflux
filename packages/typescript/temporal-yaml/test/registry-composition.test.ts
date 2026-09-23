// Sub-workflow registry composition (#748): the ONE registry a composed worker serves is
// the MERGE of the parent's registry and every (transitively) referenced child's registry —
// disjoint prompts merged in, byte-identical duplicates deduped, genuine conflicts a loud
// load-time error, and conflicting EXTERNAL registry configs rejected too. Exercised at two
// levels: the pure `composeRuntimeRegistry` core, and `assembleYamlRuntime` end to end (a
// child prompt absent from the parent still resolves at runtime through the merge).

import { describe, expect, it } from "vitest";
import { z } from "zod";

import type { ModelProvider, PromptRef } from "@typeflux/temporal";

import {
  assembleYamlRuntime,
  composeRuntimeRegistry,
  loadYamlSpec,
  projectSubworkflowResolver,
  RegistryCompositionError,
} from "../src/index.js";

const provider: ModelProvider = { structuredCall: () => ({ id: "ok" }) };
const schemas = { "schemas:X": z.object({ id: z.string() }) };

/**
 * A workflow spec with a caller-chosen inline registry block and steps. `registry` is the
 * YAML under `runtime.registry` (2-space indented body); `defs`/`steps` default to a single
 * self-contained activity + step so a spec can stand alone or reference a child.
 */
const specText = (opts: { name: string; registry: string; defs?: string; steps?: string }): string => `
project: p
name: ${opts.name}
task_queue: q
runtime:
  temporal: {}
  registry:
${opts.registry}
  provider: { type: openai }
activities:
  definitions:
${opts.defs ?? '    - { name: act, input: "schemas:X", output: "schemas:X", prompt: shared-prompt }'}
workflow:
  name: ${opts.name}
  input: schemas:X
  output: schemas:X
  steps:
${opts.steps ?? "    - { id: run, activity: act }"}
`;

const inlineRegistry = (prompts: Record<string, string>): string =>
  ["    type: inline", "    prompts:", ...Object.entries(prompts).map(([k, v]) => `      ${k}: ${JSON.stringify(v)}`)].join(
    "\n",
  );

const resolverFor = (selfId: string, specs: Record<string, string>) =>
  projectSubworkflowResolver(selfId, (id) => (id in specs ? loadYamlSpec(specs[id]!) : undefined));

const src = (id: string, yaml: string) => ({ id, spec: loadYamlSpec(yaml) });

describe("composeRuntimeRegistry (#748)", () => {
  it("merges DISJOINT inline registries into one (each source's prompts served)", () => {
    const parent = src("parent", specText({ name: "P", registry: inlineRegistry({ "shared-prompt": "hi", a: "parent-a" }) }));
    const child = src("child", specText({ name: "C", registry: inlineRegistry({ "shared-prompt": "hi", b: "child-b" }) }));
    const registry = composeRuntimeRegistry({ parent, children: [child] });
    const resolve = (name: string) => (registry!.resolve({ name } as PromptRef) as { messages: { content: unknown }[] });
    expect(resolve("a").messages[0]!.content).toBe("parent-a");
    expect(resolve("b").messages[0]!.content).toBe("child-b");
  });

  it("dedupes a byte-identical duplicate prompt (declared in both, merged once)", () => {
    const parent = src("parent", specText({ name: "P", registry: inlineRegistry({ dup: "same" }) }));
    const child = src("child", specText({ name: "C", registry: inlineRegistry({ dup: "same" }) }));
    const registry = composeRuntimeRegistry({ parent, children: [child] });
    const resolved = registry!.resolve({ name: "dup" } as PromptRef) as { messages: { content: unknown }[] };
    expect(resolved.messages[0]!.content).toBe("same");
  });

  it("REJECTS a same-name prompt with conflicting definitions, naming both sources + the field", () => {
    const parent = src("parent-wf", specText({ name: "P", registry: inlineRegistry({ dup: "parent text" }) }));
    const child = src("child-wf", specText({ name: "C", registry: inlineRegistry({ dup: "child text" }) }));
    expect(() => composeRuntimeRegistry({ parent, children: [child] })).toThrow(RegistryCompositionError);
    expect(() => composeRuntimeRegistry({ parent, children: [child] })).toThrow(
      /prompt "dup".*DIFFERENT.*"parent-wf".*"child-wf"/s,
    );
  });

  it("names the FIRST differing field for a structured inline-prompt conflict", () => {
    const structured = (text: string): string =>
      [
        "    type: inline",
        "    prompts:",
        "      p:",
        "        messages:",
        `          - { role: user, content: ${JSON.stringify(text)} }`,
      ].join("\n");
    const parent = src("parent-wf", specText({ name: "P", registry: structured("alpha") }));
    const child = src("child-wf", specText({ name: "C", registry: structured("beta") }));
    expect(() => composeRuntimeRegistry({ parent, children: [child] })).toThrow(/first differing field: messages\[0\]\.content/);
  });

  it("reports an OPTIONAL field set in one source and absent in the other (no crash)", () => {
    // #748 review: `model` set only in the child — the union-of-keys walk sees `undefined`
    // on the parent side, which must be a first-class "absent" diff state, not a
    // canonicalJson TypeError swallowing the intended RegistryCompositionError.
    const structured = (extra: string): string =>
      [
        "    type: inline",
        "    prompts:",
        "      p:",
        "        messages:",
        '          - { role: user, content: "same" }',
        ...(extra !== "" ? [`        ${extra}`] : []),
      ].join("\n");
    const parent = src("parent-wf", specText({ name: "P", registry: structured("") }));
    const child = src("child-wf", specText({ name: "C", registry: structured("model: gpt-4o") }));
    expect(() => composeRuntimeRegistry({ parent, children: [child] })).toThrow(RegistryCompositionError);
    expect(() => composeRuntimeRegistry({ parent, children: [child] })).toThrow(
      /first differing field: model \(set in "child-wf", absent in "parent-wf"\)/,
    );
  });

  it("merges structured prompts with the optional field absent in BOTH (identical dedupe)", () => {
    const structured = [
      "    type: inline",
      "    prompts:",
      "      p:",
      "        messages:",
      '          - { role: user, content: "same" }',
    ].join("\n");
    const parent = src("parent-wf", specText({ name: "P", registry: structured }));
    const child = src("child-wf", specText({ name: "C", registry: structured }));
    const registry = composeRuntimeRegistry({ parent, children: [child] });
    const resolved = registry!.resolve({ name: "p" } as PromptRef) as { messages: { content: unknown }[] };
    expect(resolved.messages[0]!.content).toBe("same");
  });

  it("REJECTS a child declaring a DIFFERENT registry type (mixed inline / external)", () => {
    const parent = src("parent-wf", specText({ name: "P", registry: inlineRegistry({ shared: "hi" }) }));
    const child = src("child-wf", specText({ name: "C", registry: "    type: langfuse\n    host: https://cloud.langfuse.com" }));
    expect(() => composeRuntimeRegistry({ parent, children: [child] })).toThrow(
      /workflow "child-wf" declares a "langfuse" registry but the composed worker serves.*"inline"/s,
    );
  });

  it("REJECTS two external registries with DIFFERENT backend config (host)", () => {
    const parent = src("parent-wf", specText({ name: "P", registry: "    type: langfuse\n    host: https://a.example" }));
    const child = src("child-wf", specText({ name: "C", registry: "    type: langfuse\n    host: https://b.example" }));
    // A transport is present so the parent's non-inline registry builds, but the config check fires first.
    expect(() =>
      composeRuntimeRegistry({ parent, children: [child], transport: { fetchPrompt: () => ({ messages: [] }) } }),
    ).toThrow(/DIFFERENT backend config \(label\/host\)/);
  });

  it("ALLOWS two external registries with IDENTICAL config (nothing to merge)", () => {
    const cfg = "    type: langfuse\n    host: https://same.example";
    const parent = src("parent-wf", specText({ name: "P", registry: cfg }));
    const child = src("child-wf", specText({ name: "C", registry: cfg }));
    const transport = { fetchPrompt: () => ({ messages: [{ role: "user" as const, content: "backend" }] }) };
    const registry = composeRuntimeRegistry({ parent, children: [child], transport });
    expect(registry).toBeDefined();
  });
});

describe("assembleYamlRuntime registry merge (#748)", () => {
  const parentReferencing = (childStep: string, parentPrompts: Record<string, string>) =>
    specText({
      name: "Parent",
      registry: inlineRegistry(parentPrompts),
      defs: '    - { name: act, input: "schemas:X", output: "schemas:X", prompt: parent-prompt }',
      steps: `    - { id: run, activity: act }\n${childStep}`,
    });

  it("serves a child's prompt absent from the parent (resolves at runtime through the merge)", async () => {
    // The parent registry has ONLY its own prompt; the child declares `child-prompt`.
    const parent = loadYamlSpec(
      parentReferencing("    - { id: sub, workflow: child }\n", { "parent-prompt": "hi" }),
    );
    const childSpec = specText({
      name: "Child",
      registry: inlineRegistry({ "child-prompt": "child says {{id}}" }),
      defs: '    - { name: childAct, input: "schemas:X", output: "schemas:X", prompt: child-prompt }',
      steps: "    - { id: run, activity: childAct }",
    });
    const { activities } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor("parent", { child: childSpec }),
    });
    // Invoking the co-registered child activity resolves `child-prompt` from the MERGED
    // registry (it exists nowhere in the parent) — a throw would mean the merge failed.
    await expect(activities["childAct"]!({ id: "42" })).resolves.toEqual({ id: "ok" });
  });

  it("merges a GRANDCHILD's registry transitively (child-of-child prompt served)", async () => {
    const parent = loadYamlSpec(parentReferencing("    - { id: sub, workflow: child }\n", { "parent-prompt": "hi" }));
    const child = specText({
      name: "Child",
      registry: inlineRegistry({ "child-prompt": "c" }),
      defs: '    - { name: childAct, input: "schemas:X", output: "schemas:X", prompt: child-prompt }',
      steps: "    - { id: run, activity: childAct }\n    - { id: sub2, workflow: grandchild }",
    });
    const grandchild = specText({
      name: "Grandchild",
      registry: inlineRegistry({ "grandchild-prompt": "gc {{id}}" }),
      defs: '    - { name: grandAct, input: "schemas:X", output: "schemas:X", prompt: grandchild-prompt }',
      steps: "    - { id: run, activity: grandAct }",
    });
    const { activities } = assembleYamlRuntime(parent, {
      provider,
      schemas,
      subworkflows: resolverFor("parent", { child, grandchild }),
    });
    await expect(activities["grandAct"]!({ id: "7" })).resolves.toEqual({ id: "ok" });
  });

  it("REJECTS assembly when a child conflicts with the parent on a shared prompt name", () => {
    const parent = loadYamlSpec(
      parentReferencing("    - { id: sub, workflow: child }\n", { "parent-prompt": "hi", shared: "parent copy" }),
    );
    const child = specText({
      name: "Child",
      registry: inlineRegistry({ shared: "child copy" }),
      defs: '    - { name: childAct, input: "schemas:X", output: "schemas:X", prompt: child-prompt }',
      steps: "    - { id: run, activity: childAct }",
    });
    expect(() =>
      assembleYamlRuntime(parent, { provider, schemas, subworkflows: resolverFor("parent", { child }) }),
    ).toThrow(/prompt "shared".*DIFFERENT.*"parent".*"child"/s);
  });
});
