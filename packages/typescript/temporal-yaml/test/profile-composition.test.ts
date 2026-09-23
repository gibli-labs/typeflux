import { describe, expect, it } from "vitest";
import { providerParamsRecord } from "../src/spec.js";

import { loadEnvironmentSpec } from "../src/environment-spec.js";
import { resolveEnvironmentWorkflow } from "../src/environment-overlay.js";
import {
  composeProfileOverrides,
  emptyProfileSources,
  loadProfileSpec,
  profileContentHash,
  ProjectProfileError,
  type ProfileSourceIndex,
  resolveSelectedProfiles,
  validateProfileSelection,
} from "../src/profile.js";

/** A profile source index with the given kind→id→spec map, paths derived as `profiles/<id>.yaml`. */
const indexOf = (
  entries: { kind: "provider" | "registry" | "runtime"; id: string; text: string }[],
): ProfileSourceIndex => {
  const specs = emptyProfileSources();
  const paths = { provider: {}, registry: {}, runtime: {} } as Record<
    "provider" | "registry" | "runtime",
    Record<string, string>
  >;
  for (const { kind, id, text } of entries) {
    specs[kind][id] = loadProfileSpec(text, { declaredKind: kind });
    paths[kind][id] = `profiles/${id}.yaml`;
  }
  return { specs, paths };
};

const PROVIDER = "name: fast\nkind: provider\nruntime:\n  provider: { type: openai, model: gpt-4o-mini }\n";
const REGISTRY = "name: lf\nkind: registry\nruntime:\n  registry: { type: langfuse }\n";

describe("composeProfileOverrides (#568; Python _resolved_profile_overrides)", () => {
  it("no selection is a no-op — empty overrides + no provenance, sources untouched", () => {
    const result = composeProfileOverrides(indexOf([]), {
      workflowSelection: {},
      environmentSelection: {},
      workflowContext: "wf",
      environmentContext: "env",
    });
    expect(result).toEqual({ overrides: {}, provenance: [] });
  });

  it("assembles disjoint kinds into ONE runtime override map + one provenance per profile", () => {
    const index = indexOf([
      { kind: "provider", id: "fast", text: PROVIDER },
      { kind: "registry", id: "lf", text: REGISTRY },
    ]);
    const { overrides, provenance } = composeProfileOverrides(index, {
      workflowSelection: { provider: "fast", registry: "lf" },
      environmentSelection: {},
      workflowContext: "wf",
      environmentContext: "env",
    });
    // Kinds own disjoint subtrees, so both fragments land in one `runtime` map without conflict.
    expect(overrides).toEqual({
      runtime: { provider: { type: "openai", model: "gpt-4o-mini" }, registry: { type: "langfuse" } },
    });
    // Provenance is one AppliedComponentProfile per profile, in fixed PROFILE_KINDS order.
    expect(provenance).toEqual([
      {
        kind: "provider",
        id: "fast",
        name: "fast",
        content_hash: profileContentHash(index.specs.provider.fast!),
        source_path: "profiles/fast.yaml",
        override_paths: ["runtime.provider.model", "runtime.provider.type"],
      },
      {
        kind: "registry",
        id: "lf",
        name: "lf",
        content_hash: profileContentHash(index.specs.registry.lf!),
        source_path: "profiles/lf.yaml",
        override_paths: ["runtime.registry.type"],
      },
    ]);
  });

  it("environment selection REPLACES the workflow selection per kind (whole-reference, no mixing)", () => {
    const index = indexOf([
      { kind: "provider", id: "fast", text: PROVIDER },
      { kind: "provider", id: "slow", text: "name: slow\nkind: provider\nruntime:\n  provider: { type: anthropic, model: claude-sonnet-4-6 }\n" },
    ]);
    const { overrides, provenance } = composeProfileOverrides(index, {
      workflowSelection: { provider: "fast" },
      environmentSelection: { provider: "slow" },
      workflowContext: "wf",
      environmentContext: "env",
    });
    // The environment's `slow` wins the provider kind wholesale — not merged with `fast`.
    expect(overrides).toEqual({ runtime: { provider: { type: "anthropic", model: "claude-sonnet-4-6" } } });
    expect(provenance.map((p) => p.id)).toEqual(["slow"]);
  });

  it("an undeclared profile id is a ProjectProfileError with the Python message shape", () => {
    expect(() =>
      composeProfileOverrides(indexOf([]), {
        workflowSelection: { provider: "missing" },
        environmentSelection: {},
        workflowContext: "workflow 'wf' profile selection",
        environmentContext: "env",
      }),
    ).toThrow(new ProjectProfileError("unknown project provider profile: missing"));
  });

  it("an unknown selection KIND fails closed (validated before id resolution, workflow first)", () => {
    // A typo'd kind on the WORKFLOW selection is caught before the environment's — Python order.
    expect(() =>
      composeProfileOverrides(indexOf([]), {
        workflowSelection: { providr: "fast" },
        environmentSelection: {},
        workflowContext: "workflow 'wf' profile selection",
        environmentContext: "environment 'e' profile selection for 'wf'",
      }),
    ).toThrow(/workflow 'wf' profile selection selects unknown profile kind 'providr'/);
  });

  it("validateProfileSelection rejects an unknown kind directly", () => {
    expect(() => validateProfileSelection({ nope: "x" }, "ctx")).toThrow(
      /ctx selects unknown profile kind 'nope'; valid kinds: provider, registry, runtime/,
    );
    expect(() => validateProfileSelection({ provider: "x" }, "ctx")).not.toThrow();
  });

  it("truthiness trap: an EMPTY-string profile id is a real selection, not an absent one", () => {
    // Python keys off `effective.get(kind) is None` (only a MISSING key skips); an empty-string id
    // is a present selection that must resolve — and fail closed as undeclared, not be silently
    // dropped. A `??`/falsy check would wrongly skip it; guard the discriminated failure.
    expect(() =>
      resolveSelectedProfiles(indexOf([]), { provider: "" }, {}),
    ).toThrow(new ProjectProfileError("unknown project provider profile: "));
  });
});

describe("resolveEnvironmentWorkflow profile layer (#568 precedence)", () => {
  const WORKFLOW =
    "project: p\nname: n\ntask_queue: q\n" +
    "runtime:\n  temporal: { namespace: base-ns }\n  registry: { type: inline, prompts: { p/x: hi } }\n" +
    "  provider: { type: openai, model: gpt-4o-mini }\n" +
    "activities:\n  definitions: [{ name: a, input: schemas:In, output: schemas:Out, prompt: p/x }]\n" +
    "workflow:\n  name: W\n  input: schemas:In\n  steps: [{ id: s, activity: a }]\n";

  it("profile overrides slot BETWEEN the workflow YAML and the environment overrides", () => {
    // The profile sets namespace prod-ns (above the workflow's base-ns); the environment overrides
    // nothing here, so the profiled namespace wins — precedence workflow YAML < profiles < env.
    const environment = loadEnvironmentSpec("name: e\noverrides: {}\n");
    const spec = resolveEnvironmentWorkflow(WORKFLOW, {
      environment,
      workflowId: "wf",
      profileOverrides: { runtime: { temporal: { namespace: "prod-ns" } } },
      baseEnv: {},
    });
    expect(spec.runtime.temporal.namespace).toBe("prod-ns");
  });

  it("environment overrides WIN over the profile layer for the same key", () => {
    // Both target runtime.temporal.namespace; the environment override (higher precedence) wins,
    // proving the profile fragment merges UNDER the environment overrides (Python `_deep_merge`).
    const environment = loadEnvironmentSpec("name: e\noverrides:\n  runtime:\n    temporal: { namespace: env-ns }\n");
    const spec = resolveEnvironmentWorkflow(WORKFLOW, {
      environment,
      workflowId: "wf",
      profileOverrides: { runtime: { temporal: { namespace: "prof-ns" } } },
      baseEnv: {},
    });
    expect(spec.runtime.temporal.namespace).toBe("env-ns");
  });

  it("an empty/omitted profile fragment leaves the environment overrides untouched", () => {
    const environment = loadEnvironmentSpec("name: e\noverrides:\n  runtime:\n    temporal: { namespace: env-ns }\n");
    const withEmpty = resolveEnvironmentWorkflow(WORKFLOW, { environment, workflowId: "wf", profileOverrides: {}, baseEnv: {} });
    const without = resolveEnvironmentWorkflow(WORKFLOW, { environment, workflowId: "wf", baseEnv: {} });
    expect(withEmpty.runtime.temporal.namespace).toBe("env-ns");
    expect(without.runtime.temporal.namespace).toBe("env-ns");
  });
});

it("providerParamsRecord drops an EMPTY stop list (Python to_dict truthiness, #568 review)", () => {
  expect(providerParamsRecord({ stop: [] } as never)).toEqual({});
  expect(providerParamsRecord({ stop: "halt" } as never)).toEqual({ stop: ["halt"] });
});
