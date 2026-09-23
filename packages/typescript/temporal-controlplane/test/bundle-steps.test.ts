import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { buildBundleSteps } from "../src/index.js";

const RUNTIME =
  "runtime: { temporal: {}, registry: { type: inline, prompts: { p/x: hi } }, provider: { type: openai, model: gpt-4o-mini } }";
const SCHEMAS = {
  "schemas:In": z.object({ text: z.string() }),
  "schemas:Items": z.object({ items: z.array(z.string()) }),
  "schemas:Out": z.object({ out: z.string() }),
} as const;

describe("buildBundleSteps — effective per-step knobs (#575)", () => {
  it("materializes engine-default timeout/retry for a plain activity step", () => {
    const spec = loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n${RUNTIME}\n` +
        "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: {} },
    );
    expect(buildBundleSteps(spec, SCHEMAS)).toEqual([
      {
        id: "s1",
        kind: "activity",
        activity: "a",
        // DEFAULT_START_TO_CLOSE_TIMEOUT_MS / DEFAULT_ACTIVITY_RETRY, in seconds — the same
        // resolution the interpreter's proxyActivities receives.
        effective_start_to_close_timeout_seconds: 120,
        effective_retry: {
          maximum_attempts: 5,
          initial_interval_seconds: 1,
          maximum_interval_seconds: 60,
          backoff_coefficient: 2,
        },
      },
    ]);
  });

  it("prefers the definition's own timeout/retry and keeps the 0 = unlimited sentinel", () => {
    const spec = loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n${RUNTIME}\n` +
        "activities:\n  definitions:\n" +
        "    - name: a\n      input: schemas:In\n      output: schemas:Out\n      prompt: p/x\n" +
        "      start_to_close_timeout_seconds: 30\n" +
        "      retry: { maximum_attempts: 0, initial_interval_seconds: 2, backoff_coefficient: 3 }\n" +
        "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
      { env: {} },
    );
    const [step] = buildBundleSteps(spec, SCHEMAS);
    expect(step?.effective_start_to_close_timeout_seconds).toBe(30);
    // The bundle keeps the plan's `0` unlimited sentinel (Python BundleRetryPolicy), which the
    // Temporal-facing proxy options would drop. maximum_interval_seconds is the spec default (60).
    expect(step?.effective_retry).toEqual({
      maximum_attempts: 0,
      initial_interval_seconds: 2,
      maximum_interval_seconds: 60,
      backoff_coefficient: 3,
    });
  });

  it("projects the map shape with materialized concurrency and the collect schema identity", () => {
    const spec = loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n${RUNTIME}\n` +
        "activities:\n  definitions:\n" +
        "    - { name: fan, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:Items\n  steps:\n" +
        "    - id: m\n      map: { activity: fan, over: workflow.input.items, collect: { output: schemas:Out, field: results } }\n",
      { env: {} },
    );
    const [step] = buildBundleSteps(spec, SCHEMAS);
    expect(step?.kind).toBe("map");
    expect(step?.map?.over).toBe("workflow.input.items");
    // Unset in the spec -> the interpreter's DEFAULT_MAP_CONCURRENCY, materialized like
    // Python's required-concurrency calls.
    expect(step?.map?.concurrency).toBe(5);
    expect(step?.map?.collect_field).toBe("results");
    expect(step?.map?.collect_output_schema).toMatchObject({ name: "Out" });
    expect(typeof step?.map?.collect_output_schema?.["hash"]).toBe("string");
  });

  it("omits the collect fields for a collect-less map (TS divergence, documented)", () => {
    const spec = loadYamlSpec(
      `project: p\nname: n\ntask_queue: q\n${RUNTIME}\n` +
        "activities:\n  definitions:\n" +
        "    - { name: fan, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
        "workflow:\n  name: W\n  input: schemas:Items\n  steps:\n" +
        "    - id: m\n      map: { activity: fan, over: workflow.input.items, concurrency: 2 }\n",
      { env: {} },
    );
    const [step] = buildBundleSteps(spec, SCHEMAS);
    expect(step?.map).toEqual({ over: "workflow.input.items", concurrency: 2 });
  });
});
