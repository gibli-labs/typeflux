import { defineActivity } from "@typeflux/temporal";
import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { buildActivityCatalog } from "../src/index.js";

// Two activities chained by schema: a (In→Mid), b (Mid→Out) — so a.compatible_next = [b].
const SPEC = loadYamlSpec(
  `project: p\nname: n\ntask_queue: q\n` +
    "runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: hi } }, provider: { type: openai, model: gpt-4o-mini } }\n" +
    "activities:\n  definitions:\n" +
    "    - { name: a, input: schemas:In, output: schemas:Mid, prompt: p/x, validation_retries: 2 }\n" +
    "    - { name: b, input: schemas:Mid, output: schemas:Out, prompt: p/x }\n" +
    "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n    - { id: s2, activity: b }\n",
  { env: {} },
);

const SCHEMAS = {
  "schemas:In": z.object({ text: z.string() }),
  "schemas:Mid": z.object({ mid: z.number() }),
  "schemas:Out": z.object({ out: z.string() }),
};

describe("buildActivityCatalog (#563 slice 2b)", () => {
  it("projects the MANIFEST project + sorted activities with used_by_steps + compatible_next", () => {
    // `project` is the manifest name (here "acme"), not the workflow YAML's own `project` ("p").
    const catalog = buildActivityCatalog(SPEC, "acme", "review", "prod", SCHEMAS);
    expect(catalog).toMatchObject({ catalog_version: "1", project: "acme", workflow_id: "review", environment_id: "prod" });
    expect(catalog.activities.map((a) => a.name)).toEqual(["a", "b"]); // sorted
    const [a, b] = catalog.activities;
    expect(a).toMatchObject({
      name: "a",
      kind: "ai",
      validation_retries: 2,
      used_by_steps: ["s1"],
      compatible_next: ["b"], // b.input (schemas:Mid) == a.output (schemas:Mid)
    });
    expect(b).toMatchObject({ name: "b", validation_retries: 1, used_by_steps: ["s2"], compatible_next: [] });
  });

  it("pins the exact entry shape — no leaked/undefined keys, timeout-less activity emits null (parity)", () => {
    // The /catalog route has no exclude_none, so the None-valued fields are ALWAYS present (null/{}/[]).
    const [a] = buildActivityCatalog(SPEC, "acme", "review", "prod", SCHEMAS).activities;
    expect(Object.keys(a ?? {}).sort()).toEqual([
      "artifact_inputs",
      "compatible_next",
      "definition_source",
      "input_schema",
      "kind",
      "name",
      "output_schema",
      "prompt_ref",
      "provider_params",
      "start_to_close_timeout_seconds",
      "task_queue",
      "used_by_steps",
      "validation_retries",
    ]);
    expect(a?.start_to_close_timeout_seconds).toBeNull(); // no timeout → null, not omitted
    expect(a?.task_queue).toBeNull(); // no per-activity queue in the TS spec → null, not omitted
    expect(a?.provider_params).toEqual({}); // no activity-level params → {} (None-dropped)
    expect(a?.artifact_inputs).toEqual([]); // no artifacts declared → []
    // definition_source is the honest TS-dialect (YAML) shape: kind + the YAML spec's project/name.
    expect(a?.definition_source).toEqual({ kind: "yaml", yaml_project: "p", yaml_name: "n" });
    // prompt_ref is Python `PromptRef.to_dict()` — {name, version, label}; prompt_type is NOT serialized.
    expect(a?.prompt_ref).toEqual({ name: "p/x", version: null, label: null });
  });

  it("projects activity-level provider_params + artifact_inputs + a version-pinned prompt_ref (#568)", () => {
    const spec = loadYamlSpec(
      `project: acme\nname: intake\ntask_queue: q\n` +
        "runtime: { temporal: {}, registry: { type: langfuse }, provider: { type: anthropic, model: claude-sonnet-4-6 } }\n" +
        "activities:\n  definitions:\n" +
        "    - name: a\n      input: schemas:In\n      output: schemas:Out\n" +
        "      prompt: { name: assess, version: 7 }\n" +
        "      provider_params: { temperature: 0.2, max_tokens: 512 }\n" +
        "      artifacts:\n        - { name: doc, from: input.doc, required: false, kind: document, attach: { role: user } }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s, activity: a }\n",
      { env: {} },
    );
    const [a] = buildActivityCatalog(spec, "acme", "review", "prod", {
      "schemas:In": z.object({ text: z.string() }),
      "schemas:Out": z.object({ out: z.string() }),
    }).activities;
    // A version-pinned ref carries the version; label stays null (mutually exclusive).
    expect(a?.prompt_ref).toEqual({ name: "assess", version: 7, label: null });
    // provider_params is the None-dropped record — only the configured keys, `stop` never invented.
    expect(a?.provider_params).toEqual({ temperature: 0.2, max_tokens: 512 });
    // artifact_inputs is the safe_definition() shape: from→from_path, cache→cache_role, attach→{role}.
    expect(a?.artifact_inputs).toEqual([
      { name: "doc", from_path: "input.doc", required: false, kind: "document", attach: { role: "user" } },
    ]);
  });

  it("does not alias the internal used_by_steps array — mutating the response can't corrupt a re-build", () => {
    const catalog = buildActivityCatalog(SPEC, "acme", "review", "prod", SCHEMAS);
    catalog.activities[0]?.used_by_steps.push("MUTATED");
    expect(buildActivityCatalog(SPEC, "acme", "review", "prod", SCHEMAS).activities[0]?.used_by_steps).toEqual(["s1"]);
  });

  it("renders each schema's JSON Schema + content hash from the injected schemas", () => {
    const [a] = buildActivityCatalog(SPEC, "acme", "review", "prod", SCHEMAS).activities;
    expect(a?.input_schema.name).toBe("In");
    expect(a?.input_schema.json_schema).toMatchObject({ type: "object", properties: { text: { type: "string" } } });
    expect(typeof a?.input_schema.hash).toBe("string");
    expect(a?.output_schema.name).toBe("Mid");
  });

  it("normalizes schema slots like the runtime descriptor (input io + $schema strip → same hash) (codex)", () => {
    // An input field with a `.default()` must stay OPTIONAL (io:"input"), not become required.
    const inSchema = z.object({ text: z.string(), tone: z.string().default("neutral") });
    const outSchema = z.object({ mid: z.number() });
    const spec = loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n` +
        "runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: hi } }, provider: { type: openai, model: gpt-4o-mini } }\n" +
        "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Mid, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: {} },
    );
    const [a] = buildActivityCatalog(spec, "acme", "review", "prod", {
      "schemas:In": inSchema,
      "schemas:Mid": outSchema,
    }).activities;
    // The defaulted input field is NOT required (output-mode would have listed it) and `$schema` is gone.
    expect(a?.input_schema.json_schema.required).toEqual(["text"]);
    expect(a?.input_schema.json_schema).not.toHaveProperty("$schema");
    // Hash + normalized schema equal the runtime activity descriptor's — a UI can correlate them.
    const descriptor = defineActivity({ name: "a", prompt: { name: "p/x" }, input: inSchema, output: outSchema });
    expect(a?.input_schema.hash).toBe(descriptor.inputSchemaHash);
    expect(a?.output_schema.hash).toBe(descriptor.outputSchemaHash);
    expect(a?.input_schema.json_schema).toEqual(descriptor.inputJsonSchema);
    expect(a?.output_schema.json_schema).toEqual(descriptor.outputJsonSchema); // both slots correlate
  });

  it("throws when a referenced schema is not injected (the CatalogSchema contract requires it)", () => {
    // Omit schemas:Mid — the contract needs hash+json_schema, so a partial slot is not allowed (codex).
    const partial = { "schemas:In": z.object({ text: z.string() }), "schemas:Out": z.object({ out: z.string() }) };
    expect(() => buildActivityCatalog(SPEC, "acme", "review", "prod", partial)).toThrow(/schemas:Mid/);
    expect(() => buildActivityCatalog(SPEC, "acme", "review", "prod", undefined)).toThrow(/requires an injected schema/);
  });

  it("rejects an activity whose OUTPUT can't be made provider-safe — same gate as defineActivity (finder)", () => {
    // An open record output: defineActivity throws ProviderSchemaError, so the catalog must too.
    const openOut = z.object({ meta: z.record(z.string(), z.string()) });
    expect(() => defineActivity({ name: "a", prompt: { name: "p/x" }, input: z.object({ text: z.string() }), output: openOut })).toThrow();
    expect(() =>
      buildActivityCatalog(SPEC, "acme", "review", "prod", { "schemas:In": z.object({ text: z.string() }), "schemas:Mid": openOut, "schemas:Out": z.object({ out: z.string() }) }),
    ).toThrow();
  });
});
