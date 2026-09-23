// Cross-run activity caching reachable from YAML (#398/#753): the `cross_run_cache:` surface
// on an activity definition sets `descriptor.cache`, and a `cacheStore` threaded into
// assembleYamlRuntime reaches every activity's execute options (parent AND composed children).
// Together they satisfy execute.ts's `descriptor.cache?.enabled && options.cacheStore` gate, so a
// YAML-built, worker-run activity memoizes its VALIDATED output across runs — the reachability
// an adopter needs. Also proves the #745 ride-along (a cache HIT re-runs the outputCheck) and that
// code activities cannot be cross-run cached.

import { describe, expect, it } from "vitest";
import { z } from "zod";

import {
  type ActivityOutputCheck,
  type ModelProvider,
  type StructuredCallParams,
  InMemoryCacheStore,
  defineCodeActivity,
} from "@typeflux/temporal";

import { assembleYamlRuntime, loadYamlSpec, projectSubworkflowResolver } from "../src/index.js";

/** A provider that counts structuredCall invocations, so a cache HIT is observable as zero new calls. */
class CountingProvider implements ModelProvider {
  calls = 0;
  constructor(private readonly response: unknown = { summary: "s" }) {}
  structuredCall(_params: StructuredCallParams): unknown {
    this.calls += 1;
    return this.response;
  }
}

const schemas = {
  "schemas:Doc": z.object({ id: z.string() }),
  "schemas:Summary": z.object({ summary: z.string() }),
};

/** A single-activity spec; `extra` is spliced under the `summarize` definition (e.g. a cache block). */
const specText = (extra: string): string => `
project: p
name: n
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/sum: summarize it } }
  provider: { type: openai }
activities:
  definitions:
    - name: summarize
      input: schemas:Doc
      output: schemas:Summary
      prompt: p/sum
${extra}
workflow:
  name: W
  input: schemas:Doc
  output: schemas:Summary
  steps:
    - id: s
      activity: summarize
`;

const CROSS_RUN = "      cross_run_cache: { enabled: true }\n";

describe("cross-run activity caching from YAML (#398/#753)", () => {
  it("memoizes across runs: a second run with the same input makes ZERO provider calls", async () => {
    const provider = new CountingProvider();
    const cacheStore = new InMemoryCacheStore();
    const { activities } = assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), {
      provider,
      schemas,
      cacheStore,
    });
    const first = await activities["summarize"]!({ id: "d1" });
    const second = await activities["summarize"]!({ id: "d1" });
    expect(first).toEqual({ summary: "s" });
    expect(second).toEqual(first);
    // The adopter-required proof: the second run served from cache, no new provider spend.
    expect(provider.calls).toBe(1);
  });

  it("is INERT without a cacheStore threaded in (a declared cross_run_cache is a no-op)", async () => {
    const provider = new CountingProvider();
    const { activities } = assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), { provider, schemas });
    await activities["summarize"]!({ id: "d1" });
    await activities["summarize"]!({ id: "d1" });
    // No store ⇒ execute.ts's gate stays closed ⇒ both runs hit the provider.
    expect(provider.calls).toBe(2);
  });

  it("does NOT memoize when the block is present but disabled (enabled: false)", async () => {
    const provider = new CountingProvider();
    const cacheStore = new InMemoryCacheStore();
    const { activities } = assembleYamlRuntime(
      loadYamlSpec(specText("      cross_run_cache: { enabled: false }\n")),
      { provider, schemas, cacheStore },
    );
    await activities["summarize"]!({ id: "d1" });
    await activities["summarize"]!({ id: "d1" });
    expect(provider.calls).toBe(2);
  });

  it("bypass_reads_env skips cache READS when the env var is present (writes still happen)", async () => {
    const provider = new CountingProvider();
    const cacheStore = new InMemoryCacheStore();
    const envName = "TF_CROSS_RUN_BYPASS_753";
    const { activities } = assembleYamlRuntime(
      loadYamlSpec(specText(`      cross_run_cache: { enabled: true, bypass_reads_env: ${envName} }\n`)),
      { provider, schemas, cacheStore },
    );
    process.env[envName] = "1";
    try {
      await activities["summarize"]!({ id: "d1" });
      await activities["summarize"]!({ id: "d1" });
      // Reads bypassed ⇒ the second run regenerates despite a warm cache.
      expect(provider.calls).toBe(2);
    } finally {
      delete process.env[envName];
    }
    // The write still happened: with the bypass gone, the next run serves from cache.
    await activities["summarize"]!({ id: "d1" });
    expect(provider.calls).toBe(2);
  });

  it("re-runs the injected outputCheck on a cache HIT (#745 rides along)", async () => {
    const provider = new CountingProvider();
    const cacheStore = new InMemoryCacheStore();
    let checkCalls = 0;
    const outputCheck: ActivityOutputCheck<unknown, unknown> = () => {
      checkCalls += 1;
      return []; // always accept
    };
    const { activities } = assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), {
      provider,
      schemas,
      cacheStore,
      outputChecks: { summarize: outputCheck },
    });
    await activities["summarize"]!({ id: "d1" }); // miss: generation validates via the check
    await activities["summarize"]!({ id: "d1" }); // hit: RE-runs the check over the cached output
    expect(provider.calls).toBe(1);
    // Once on the generation (write) path, once on the hit re-validation — the #745 seam.
    expect(checkCalls).toBe(2);
  });

  it("threads the cacheStore through a COMPOSED CHILD workflow's activities", async () => {
    // Parent references a child sub-workflow; the child's `summarize` declares cross_run_cache.
    // sharedOptions is the ONE options object every registration (parent + child) receives, so the
    // child activity must memoize through the same threaded store.
    const parentText = `
project: p
name: Parent
task_queue: q
runtime:
  temporal: {}
  registry: { type: inline, prompts: { p/sum: summarize it } }
  provider: { type: openai }
activities:
  definitions: []
workflow:
  name: Parent
  input: schemas:Doc
  output: schemas:Summary
  steps:
    - { id: sub, workflow: child }
`;
    const childText = specText(CROSS_RUN);
    const provider = new CountingProvider();
    const cacheStore = new InMemoryCacheStore();
    const { activities } = assembleYamlRuntime(loadYamlSpec(parentText), {
      provider,
      schemas,
      cacheStore,
      subworkflows: projectSubworkflowResolver("parent", (id) =>
        id === "child" ? loadYamlSpec(childText) : undefined,
      ),
    });
    // The child's activity is in the assembled map and memoizes through the threaded store.
    await activities["summarize"]!({ id: "d1" });
    await activities["summarize"]!({ id: "d1" });
    expect(provider.calls).toBe(1);
  });

  describe("code activities cannot be cross-run cached (#746)", () => {
    it("defineCodeActivity rejects a `cache` option loudly", () => {
      expect(() =>
        defineCodeActivity({
          name: "pure",
          input: schemas["schemas:Doc"],
          output: schemas["schemas:Summary"],
          handler: (doc) => ({ summary: doc.id }),
          // @ts-expect-error — a code activity has no provider and cannot memoize.
          cache: { enabled: true },
        }),
      ).toThrow(/"cache" is not .*supported for a pure-code activity/);
    });

    it("errors loudly when an injected code descriptor collides with a cross_run_cache definition", () => {
      // A YAML `cross_run_cache:` definition builds an AI descriptor; injecting a code descriptor
      // of the SAME name to 'satisfy' it collides at map assembly rather than silently overriding.
      const code = defineCodeActivity({
        name: "summarize",
        input: schemas["schemas:Doc"],
        output: schemas["schemas:Summary"],
        handler: (doc) => ({ summary: doc.id }),
      });
      expect(() =>
        assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), {
          provider: new CountingProvider(),
          schemas,
          cacheStore: new InMemoryCacheStore(),
          extraActivities: { summarize: code },
        }),
      ).toThrow(/duplicate activity name: "summarize"/);
    });
  });
});

describe("cross-run cache subject carrier through the REAL worker activity path (#715 slice 1)", () => {
  /** A spy store: records every written CacheRecord so the subjects field is observable. */
  class RecordingStore extends InMemoryCacheStore {
    written: unknown[] = [];
    override set(key: Parameters<InMemoryCacheStore["set"]>[0], record: Parameters<InMemoryCacheStore["set"]>[1]): void {
      this.written.push(record);
      super.set(key, record);
    }
  }

  it("a write through the built worker activity fn carries the threaded subjects on the record", async () => {
    const provider = new CountingProvider();
    const cacheStore = new RecordingStore();
    const { activities } = assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), {
      provider,
      schemas,
      cacheStore,
    });
    // The REAL registered activity fn (the exact function the Temporal worker runs),
    // invoked with the interpreter's third-arg subject envelope — the path the
    // review found dead: boundary parse -> ActivityContext.subjectIds -> cacheRecord.
    await activities["summarize"]!({ id: "d1" }, undefined, ["pt-1", "pt-2"]);
    expect(cacheStore.written).toHaveLength(1);
    expect((cacheStore.written[0] as { subjects?: string[] }).subjects).toEqual(["pt-1", "pt-2"]);
  });

  it("a write with NO subject envelope stays subjects-less (byte-unchanged records)", async () => {
    const provider = new CountingProvider();
    const cacheStore = new RecordingStore();
    const { activities } = assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), {
      provider,
      schemas,
      cacheStore,
    });
    await activities["summarize"]!({ id: "d1" });
    expect(cacheStore.written).toHaveLength(1);
    expect("subjects" in (cacheStore.written[0] as Record<string, unknown>)).toBe(false);
  });

  it("an off-shape subject envelope rejects at the worker boundary (never a silent drop)", async () => {
    const provider = new CountingProvider();
    const { activities } = assembleYamlRuntime(loadYamlSpec(specText(CROSS_RUN)), {
      provider,
      schemas,
      cacheStore: new InMemoryCacheStore(),
    });
    await expect(activities["summarize"]!({ id: "d1" }, undefined, [42] as unknown as string[])).rejects.toThrow();
  });
});
