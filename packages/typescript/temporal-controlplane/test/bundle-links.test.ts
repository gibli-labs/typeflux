import { loadYamlSpec } from "@typeflux/temporal-yaml";
import { afterEach, describe, expect, it, vi } from "vitest";

import { buildBundleLinks } from "../src/index.js";

const spec = (temporal: string) =>
  loadYamlSpec(
    `project: p\nname: n\ntask_queue: q\nruntime:\n  temporal: ${temporal}\n` +
      "  registry: { type: inline, prompts: { p/x: hi } }\n  provider: { type: openai }\n" +
      "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
      "workflow:\n  name: W\n  input: schemas:In\n  steps:\n    - { id: s1, activity: a }\n",
    { env: {} },
  );

const ENV_VARS = ["TEMPORAL_UI_URL", "LANGFUSE_PROJECT_URL"] as const;
const saved = new Map<string, string | undefined>(ENV_VARS.map((k) => [k, process.env[k]]));

afterEach(() => {
  for (const [key, value] of saved) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  vi.restoreAllMocks();
});

describe("buildBundleLinks (#575)", () => {
  it("derives the local dev UI from a localhost address (and the spec's address default)", () => {
    for (const key of ENV_VARS) delete process.env[key];
    expect(buildBundleLinks(spec("{ address: 'localhost:7233' }"))).toEqual({
      temporal_ui: "http://localhost:8233",
    });
    // No address in the spec -> the engine's localhost:7233 default, same derivation.
    expect(buildBundleLinks(spec("{}"))).toEqual({ temporal_ui: "http://localhost:8233" });
  });

  it("derives Temporal Cloud for a cloud address and returns undefined for an unknown host", () => {
    for (const key of ENV_VARS) delete process.env[key];
    expect(buildBundleLinks(spec("{ address: 'ns.a1b2c.tmprl.cloud:7233' }"))).toEqual({
      temporal_ui: "https://cloud.temporal.io",
    });
    expect(buildBundleLinks(spec("{ address: 'temporal.internal.corp:7233' }"))).toBeUndefined();
  });

  it("lets an explicit TEMPORAL_UI_URL win over the derivation and reads LANGFUSE_PROJECT_URL", () => {
    process.env["TEMPORAL_UI_URL"] = " https://temporal-ui.corp.example ";
    process.env["LANGFUSE_PROJECT_URL"] = "https://langfuse.corp.example/project/p1";
    expect(buildBundleLinks(spec("{ address: 'localhost:7233' }"))).toEqual({
      temporal_ui: "https://temporal-ui.corp.example",
      langfuse_project: "https://langfuse.corp.example/project/p1",
    });
  });

  it("rejects a URL that embeds credentials — the bundle is secret-safe", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    process.env["LANGFUSE_PROJECT_URL"] = "https://token@langfuse.corp.example/project/p1";
    delete process.env["TEMPORAL_UI_URL"];
    expect(buildBundleLinks(spec("{ address: 'temporal.internal.corp:7233' }"))).toBeUndefined();
    expect(warn).toHaveBeenCalledWith(
      "ignoring LANGFUSE_PROJECT_URL: URL embeds credentials; external links stay hidden",
    );
  });

  it("warns and hides a non-http(s) env URL instead of rendering it", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    process.env["TEMPORAL_UI_URL"] = "javascript:alert(1)";
    delete process.env["LANGFUSE_PROJECT_URL"];
    // The invalid env var is dropped; the derivation still fills temporal_ui in.
    expect(buildBundleLinks(spec("{ address: 'localhost:7233' }"))).toEqual({
      temporal_ui: "http://localhost:8233",
    });
    expect(warn).toHaveBeenCalledWith("ignoring TEMPORAL_UI_URL: not an http(s) URL; external links stay hidden");
  });
});
