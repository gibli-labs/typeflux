import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { bundleErasure } from "../src/index.js";

const spec = (runtimeExtra: string, workflowExtra: string) =>
  loadYamlSpec(
    `project: p\nname: n\ntask_queue: q\nruntime:\n${runtimeExtra}  temporal: {}\n` +
      "  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }\n" +
      "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
      `workflow:\n${workflowExtra}  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n`,
    { env: {} },
  );

describe("bundleErasure (#795)", () => {
  it("is absent without load-bearing declarations (existing bundles unchanged)", () => {
    expect(bundleErasure(spec("", ""))).toBeUndefined();
  });

  it("states the targeted requirement (Python byte-identical text)", () => {
    const erasure = bundleErasure(
      spec("  cache_erasure: targeted\n", "  subjects:\n    - from: input.id\n"),
    );
    expect(erasure).toMatchObject({ subject_selectors: 1 });
    expect(erasure!.cache.declared).toBe("targeted");
    // EXACT cross-edition string (the Python edition pins the same literal).
    expect(erasure!.cache.behavior).toBe(
      "targeted per-subject invalidation (declared REQUIRED: wiring a store without " +
        "SubjectErasableCacheStore fails runtime assembly, an erase run on the cache " +
        "surface without a wired store fails loudly, and a deployment that wires no " +
        "cache store satisfies the requirement vacuously — an empty cache has nothing " +
        "to erase)",
    );
  });

  it("states the wired-store-dependent contract when subjects alone are declared", () => {
    const erasure = bundleErasure(spec("", "  subjects:\n    - from: input.id\n"));
    expect(erasure!.cache.declared).toBe("any");
    expect(erasure!.cache.behavior).toBe(
      "wired-store dependent: targeted per-subject invalidation when the injected " +
        "CacheStore implements SubjectErasableCacheStore, else the documented " +
        "full-cache-flush fallback (the erasure receipt records which behavior ran)",
    );
  });
});
