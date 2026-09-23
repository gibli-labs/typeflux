/**
 * Exercises the session-cache example on every CI run: the REAL typeflux.yaml +
 * schemas, asserting the map step opts into the session cache and that
 * assembling the runtime auto-registers the prep/release companion activities
 * (#478). The prep/release ORCHESTRATION runs in the workflow sandbox and needs
 * a server, so that end-to-end bracket is covered by `main.ts`.
 */

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { cachePrepActivityName, cacheReleaseActivityName } from "@typeflux/temporal";

import { RecordingReferenceCacheProvider } from "../examples/session-cache-review/fakes.js";
import { schemas } from "../examples/session-cache-review/schemas.js";
import { assembleYamlRuntime, loadYamlSpec, workflowPlanFromSpec } from "../src/index.js";

const EXAMPLE_YAML = new URL("../examples/session-cache-review/typeflux.yaml", import.meta.url);

describe("session-cache-review example (#455)", () => {
  const spec = () =>
    loadYamlSpec(readFileSync(EXAMPLE_YAML, "utf-8"), {
      sourceLabel: "examples/session-cache-review/typeflux.yaml",
    });

  it("derives a session-cache-enabled map step", () => {
    const plan = workflowPlanFromSpec(spec());
    const mapStep = plan.steps.find((step) => step.kind === "map");
    expect(mapStep).toBeDefined();
    expect((mapStep as { sessionCache?: { enabled: boolean } }).sessionCache).toEqual({ enabled: true });
  });

  it("auto-registers the prep/release companion activities for the cached activity", () => {
    const { activities } = assembleYamlRuntime(spec(), {
      provider: new RecordingReferenceCacheProvider(),
      schemas,
    });
    // The map activity + its two session-cache companions are all registered.
    expect(activities).toHaveProperty("review_item");
    expect(activities).toHaveProperty(cachePrepActivityName("review_item"));
    expect(activities).toHaveProperty(cacheReleaseActivityName("review_item"));
  });

  it("the recording provider prepares an engaged reference handle and releases it", async () => {
    // Drive the companion activities directly (the workflow does this in order):
    // prep once, then per-item calls threading the handle, then release.
    const provider = new RecordingReferenceCacheProvider();
    const { activities } = assembleYamlRuntime(spec(), { provider, schemas });

    const handle = (await activities[cachePrepActivityName("review_item")]!({ id: "A", text: "first" })) as {
      supported: boolean;
      cache_id: string | null;
    };
    expect(handle.supported).toBe(true);
    expect(handle.cache_id).toBe("cachedContents/session-cache-demo");
    expect(provider.prepared).toHaveLength(1);

    await activities[cacheReleaseActivityName("review_item")]!(handle);
    expect(provider.released).toEqual(["cachedContents/session-cache-demo"]);
  });
});
