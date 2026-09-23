import { loadProjectSpec, loadYamlSpec } from "@typeflux/temporal-yaml";
import { describe, expect, it } from "vitest";

import { buildBundleRuntimeEffective } from "../src/index.js";

const spec = (runtime: string, workflowExtra = "") =>
  loadYamlSpec(
    `project: p\nname: n\ntask_queue: q\nruntime:\n${runtime}\n` +
      "activities:\n  definitions:\n    - { name: a, input: schemas:In, output: schemas:Out, prompt: p/x }\n" +
      `workflow:\n  name: W\n  input: schemas:In\n${workflowExtra}  steps:\n    - { id: s1, activity: a }\n`,
    { env: {} },
  );

const project = (defaultsRuntime: string) =>
  loadProjectSpec(`version: "1"\nname: demo\n${defaultsRuntime}workflows:\n  - id: w\n    path: w.yaml\n`);

const INLINE_REGISTRY = "  temporal: {}\n  registry: { type: inline, prompts: { p/x: hi } }\n";

describe("buildBundleRuntimeEffective — source classification (#575)", () => {
  it("tags engine defaults, project defaults, and configured values (Python parity test shape)", () => {
    const yamlSpec = spec(
      INLINE_REGISTRY + "  provider: { type: openai, model: gpt-4o-mini }\n  provider_retry: { max_attempts: 4 }\n",
    );
    const proj = project("defaults:\n  runtime:\n    provider_retry:\n      max_attempts: 4\n");
    const byPath = Object.fromEntries(buildBundleRuntimeEffective(yamlSpec, proj).map((e) => [e.path, e]));
    expect(byPath["provider_retry.max_attempts"]).toEqual({
      path: "provider_retry.max_attempts",
      value: 4,
      source: "project_default",
    });
    expect(byPath["provider_retry.jitter_ratio"]).toEqual({
      path: "provider_retry.jitter_ratio",
      value: 0.1,
      source: "engine_default",
    });
    // The model equals the openai engine default even though the spec pins it explicitly —
    // equality classification, exactly like Python.
    expect(byPath["provider.model"]).toEqual({ path: "provider.model", value: "gpt-4o-mini", source: "engine_default" });
  });

  it("materializes an unset provider.model to the per-type default (Python parse-time parity)", () => {
    const yamlSpec = spec(INLINE_REGISTRY + "  provider: { type: openai }\n");
    const byPath = Object.fromEntries(buildBundleRuntimeEffective(yamlSpec, project("")).map((e) => [e.path, e]));
    expect(byPath["provider.model"]).toEqual({
      path: "provider.model",
      value: "gpt-4o-mini",
      source: "engine_default",
    });
  });

  it("tags a value the workflow sets away from both layers as configured", () => {
    const yamlSpec = spec(
      INLINE_REGISTRY + "  provider: { type: openai, model: o3-mini }\n  provider_retry: { backoff_multiplier: 7 }\n",
    );
    const proj = project("");
    const byPath = Object.fromEntries(buildBundleRuntimeEffective(yamlSpec, proj).map((e) => [e.path, e]));
    expect(byPath["provider.model"]?.source).toBe("configured");
    expect(byPath["provider_retry.backoff_multiplier"]).toEqual({
      path: "provider_retry.backoff_multiplier",
      value: 7,
      source: "configured",
    });
  });

  it("omits the value key when the effective value is null (uncapped max_backoff; exclude_none parity)", () => {
    const yamlSpec = spec(INLINE_REGISTRY + "  provider: { type: openai }\n");
    const byPath = Object.fromEntries(buildBundleRuntimeEffective(yamlSpec, project("")).map((e) => [e.path, e]));
    expect(byPath["provider_retry.max_backoff_seconds"]).toEqual({
      path: "provider_retry.max_backoff_seconds",
      source: "engine_default",
    });
    expect(Object.hasOwn(byPath["provider_retry.max_backoff_seconds"] ?? {}, "value")).toBe(false);
  });

  it("emits registry.label only for langfuse/langsmith and lifecycle limit only when enabled", () => {
    const inline = spec(INLINE_REGISTRY + "  provider: { type: openai }\n");
    const inlinePaths = buildBundleRuntimeEffective(inline, project("")).map((e) => e.path);
    expect(inlinePaths).not.toContain("registry.label");
    expect(inlinePaths).not.toContain("lifecycle.history.status_event_limit");

    const langfuse = spec(
      "  temporal: {}\n  registry: { type: langfuse, prompts: { p/x: hi } }\n  provider: { type: openai }\n",
      "  lifecycle:\n    enabled: true\n",
    );
    const byPath = Object.fromEntries(buildBundleRuntimeEffective(langfuse, project("")).map((e) => [e.path, e]));
    // Unset label -> the engine's production default.
    expect(byPath["registry.label"]).toEqual({ path: "registry.label", value: "production", source: "engine_default" });
    expect(byPath["lifecycle.history.status_event_limit"]).toEqual({
      path: "lifecycle.history.status_event_limit",
      value: 50,
      source: "engine_default",
    });
  });
});
