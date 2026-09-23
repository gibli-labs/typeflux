import { describe, expect, it } from "vitest";

import { loadProjectSpec } from "../src/index.js";

/** A minimal valid manifest with overridable body. */
const manifest = (body: string) => `version: "1"\nname: acme\n${body}`;

describe("loadProjectSpec (#454 — project manifest model)", () => {
  it("loads a minimal manifest and applies defaults", () => {
    const spec = loadProjectSpec(manifest("workflows:\n  - id: review\n    path: workflows/review/typeflux.yaml\n"));
    expect(spec.version).toBe("1");
    expect(spec.name).toBe("acme");
    expect(spec.defaults.workflow_filename).toBe("typeflux.yaml");
    expect(spec.defaults.runtime).toEqual({});
    expect(spec.workflows).toEqual([{ id: "review", path: "workflows/review/typeflux.yaml", profiles: {} }]);
    expect(spec.environments).toEqual({});
    expect(spec.policies).toEqual({});
    expect(spec.validation).toEqual({ targets: {} });
  });

  it("loads the full manifest surface (environments, policies, profiles, validation targets)", () => {
    const spec = loadProjectSpec(
      manifest(
        [
          "defaults:",
          "  workflow_filename: flow.yaml",
          "  runtime: { provider: { type: openai } }",
          "workflows:",
          "  - id: review",
          "    directory: workflows/review",
          "    profiles: { provider: openai-prod }",
          "environments:",
          "  prod: envs/prod.yaml",
          "policies:",
          "  org: policies/org.yaml",
          "profiles:",
          "  provider: { openai-prod: profiles/openai.yaml }",
          "validation:",
          "  targets:",
          "    ci:",
          "      workflows: [review]",
          "      environment: prod",
          "      policies: [org]",
        ].join("\n"),
      ),
    );
    expect(spec.defaults.workflow_filename).toBe("flow.yaml");
    expect(spec.defaults.runtime).toEqual({ provider: { type: "openai" } });
    expect(spec.workflows[0]).toEqual({ id: "review", directory: "workflows/review", profiles: { provider: "openai-prod" } });
    expect(spec.environments).toEqual({ prod: "envs/prod.yaml" });
    expect(spec.policies).toEqual({ org: "policies/org.yaml" });
    expect(spec.profiles?.provider).toEqual({ "openai-prod": "profiles/openai.yaml" });
    expect(spec.validation.targets["ci"]).toEqual({ workflows: ["review"], environment: "prod", policies: ["org"] });
  });

  it("requires at least one workflow", () => {
    expect(() => loadProjectSpec(manifest("workflows: []\n"))).toThrow(/at least one workflow/);
  });

  it("rejects a workflow with neither path nor directory, and one with both", () => {
    expect(() => loadProjectSpec(manifest("workflows:\n  - id: review\n"))).toThrow(/exactly one of/);
    expect(() =>
      loadProjectSpec(manifest("workflows:\n  - id: review\n    path: a.yaml\n    directory: a\n")),
    ).toThrow(/exactly one of/);
  });

  it("rejects duplicate workflow ids", () => {
    expect(() =>
      loadProjectSpec(manifest("workflows:\n  - id: dup\n    path: a.yaml\n  - id: dup\n    directory: b\n")),
    ).toThrow(/duplicate project workflow id\(s\): dup/);
  });

  it("rejects a workflow id that violates the id pattern", () => {
    expect(() => loadProjectSpec(manifest("workflows:\n  - id: '.hidden'\n    path: a.yaml\n"))).toThrow(
      /workflow id must start with an alphanumeric/,
    );
  });

  it("rejects an unknown key at the top level AND in a nested block (strict, extra=forbid)", () => {
    expect(() => loadProjectSpec(manifest("workflows:\n  - id: r\n    path: a.yaml\nbogus: 1\n"))).toThrow(
      /invalid project manifest/,
    );
    // Nested strictness — a typo inside a workflow entry must fail too.
    expect(() => loadProjectSpec(manifest("workflows:\n  - id: r\n    path: a.yaml\n    bogus: 1\n"))).toThrow(
      /invalid project manifest/,
    );
  });

  it("rejects an empty reference path (environments/policies map values must be non-empty)", () => {
    expect(() => loadProjectSpec(manifest("workflows:\n  - id: r\n    path: a.yaml\nenvironments:\n  prod: ''\n"))).toThrow(
      /reference path must be non-empty/,
    );
  });

  it("rejects a non-trimmed reference KEY, not just a bad value (zod record-key refinement)", () => {
    expect(() =>
      loadProjectSpec(manifest("workflows:\n  - id: r\n    path: a.yaml\nenvironments:\n  ' prod': envs/prod.yaml\n")),
    ).toThrow(/invalid project manifest/);
  });

  it("rejects a non-file workflow_filename (path separators)", () => {
    expect(() =>
      loadProjectSpec(manifest("defaults:\n  workflow_filename: sub/flow.yaml\nworkflows:\n  - id: r\n    path: a.yaml\n")),
    ).toThrow(/must be a file name/);
  });

  it("rejects a duplicate mapping key (no silent last-win)", () => {
    expect(() =>
      loadProjectSpec("version: \"1\"\nname: a\nname: b\nworkflows:\n  - id: r\n    path: a.yaml\n"),
    ).toThrow(/invalid project YAML/);
  });

  it("rejects a non-mapping document and an empty document", () => {
    expect(() => loadProjectSpec("- a\n- b\n")).toThrow(/must be a mapping/);
    expect(() => loadProjectSpec("")).toThrow(/empty project manifest/);
  });

  it("rejects an unquoted numeric version (must be the string \"1\", like pydantic Literal)", () => {
    expect(() => loadProjectSpec("version: 1\nname: a\nworkflows:\n  - id: r\n    path: a.yaml\n")).toThrow(
      /invalid project manifest/,
    );
  });

  // ── Python nullable-field parity: an explicit YAML null (or bare `key:`) is ABSENT ──

  it("treats an explicit null `path` as absent, so `directory` alone is valid (Python parity)", () => {
    const spec = loadProjectSpec(manifest("workflows:\n  - id: w\n    path:\n    directory: wfdir\n"));
    expect(spec.workflows[0]).toEqual({ id: "w", directory: "wfdir", profiles: {} });
  });

  it("treats an explicit null top-level `profiles` as absent (Python parity)", () => {
    const spec = loadProjectSpec(manifest("workflows:\n  - id: w\n    path: a.yaml\nprofiles:\n"));
    expect(spec.profiles).toBeUndefined();
  });

  it("treats an explicit null validation-target `environment` as absent (Python parity)", () => {
    const spec = loadProjectSpec(
      manifest("workflows:\n  - id: w\n    path: a.yaml\nvalidation:\n  targets:\n    t1:\n      environment:\n      workflows: [w]\n"),
    );
    expect(spec.validation.targets["t1"]?.environment).toBeUndefined();
  });

  it("does not over-validate workflow.profiles (Python leaves it an unvalidated dict)", () => {
    // An empty profile value loads (Python accepts it; the reference resolves later).
    const spec = loadProjectSpec(manifest("workflows:\n  - id: w\n    path: a.yaml\n    profiles: { provider: '' }\n"));
    expect(spec.workflows[0]?.profiles).toEqual({ provider: "" });
  });
});

describe("prototype-key hardening (#454; assertSafeKeys parity with loadYamlSpec)", () => {
  const WORKFLOWS = "workflows:\n  - { id: w, path: a.yaml }\n";

  it("rejects a top-level __proto__ key that zod's strict schema would silently ignore", () => {
    expect(() => loadProjectSpec(manifest(`${WORKFLOWS}__proto__: { x: 1 }\n`))).toThrow(/unsafe key '__proto__'/);
    expect(({} as Record<string, unknown>).x).toBeUndefined(); // Object.prototype untouched
  });

  it("rejects a nested prototype-aliasing key, naming its path", () => {
    expect(() => loadProjectSpec(manifest(`${WORKFLOWS}defaults: { runtime: { __proto__: {} } }\n`))).toThrow(
      /unsafe key '__proto__'.*defaults\.runtime/,
    );
  });
});
