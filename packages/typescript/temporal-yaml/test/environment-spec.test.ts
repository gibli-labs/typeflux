import { describe, expect, it } from "vitest";

import { loadEnvironmentSpec, validateYamlOverrides, yamlOverridePaths } from "../src/index.js";

const env = (body: string) => loadEnvironmentSpec(`version: "1"\nname: prod\n${body}`);

describe("loadEnvironmentSpec (#454 — environment model)", () => {
  it("loads a minimal environment with defaults", () => {
    const spec = env("");
    expect(spec).toEqual({ version: "1", name: "prod", env_files: [], variables: {}, overrides: {}, workflows: {} });
  });

  it("loads the full surface (env_files, variables, overrides, per-workflow overrides)", () => {
    const spec = env(
      [
        "env_files:",
        "  - path: .env.prod",
        "  - { path: .env.local, required: false }",
        "variables:",
        "  LOG_LEVEL: info",
        "  MAX_RETRIES: 3",
        "  DEBUG: false",
        "overrides:",
        "  task_queue: prod-queue",
        "  runtime:",
        "    provider: { type: anthropic }",
        "workflows:",
        "  review:",
        "    overrides: { runtime: { temporal: { namespace: prod } } }",
        "    profiles: { provider: anthropic-prod }",
      ].join("\n"),
    );
    expect(spec.env_files).toEqual([
      { path: ".env.prod", required: true },
      { path: ".env.local", required: false },
    ]);
    expect(spec.variables).toEqual({ LOG_LEVEL: "info", MAX_RETRIES: 3, DEBUG: false });
    expect(spec.overrides).toEqual({ task_queue: "prod-queue", runtime: { provider: { type: "anthropic" } } });
    expect(spec.workflows["review"]).toEqual({
      overrides: { runtime: { temporal: { namespace: "prod" } } },
      profiles: { provider: "anthropic-prod" },
    });
  });

  it("rejects a top-level override outside the allow-list (task_queue | runtime)", () => {
    expect(() => env("overrides:\n  workflow: { name: X }\n")).toThrow(/workflow is not an allowed environment override/);
  });

  it("rejects a runtime override outside the allow-list", () => {
    expect(() => env("overrides:\n  runtime:\n    activity_retry: { maximum_attempts: 9 }\n")).toThrow(
      /runtime.activity_retry is not an allowed runtime override/,
    );
  });

  it("allows every allow-listed runtime override key", () => {
    const spec = env(
      "overrides:\n  runtime:\n    temporal: {}\n    registry: {}\n    provider: {}\n    provider_limits: {}\n    provider_retry: {}\n    observability: {}\n",
    );
    expect(Object.keys(spec.overrides["runtime"] as object).sort()).toEqual(
      ["observability", "provider", "provider_limits", "provider_retry", "registry", "temporal"],
    );
  });

  it("rejects an invalid environment variable name (anchored pattern — leading digit or spaces)", () => {
    // A failed record-KEY refinement surfaces zod's "Invalid key in record" — specific
    // enough to prove the envKey pattern fired (vs a value/strict/other failure), and
    // it still catches an anchor regression (a dropped $ would ACCEPT 'X ' → no throw).
    expect(() => env("variables:\n  '1BAD': x\n")).toThrow(/Invalid key in record/);
    expect(() => env("variables:\n  ' X': x\n")).toThrow(/Invalid key in record/);
    expect(() => env("variables:\n  'X ': x\n")).toThrow(/Invalid key in record/);
  });

  it("coerces a YAML string bool-word for `required` (JS yaml parses `yes` as a string — Python 1.1 parity)", () => {
    // The JS `yaml` lib (YAML 1.2) resolves `yes` to the string "yes"; yamlBoolean
    // coerces it so a Python-loadable manifest still loads in TS.
    expect(env("env_files:\n  - { path: .env, required: yes }\n").env_files[0]?.required).toBe(true);
    expect(env("env_files:\n  - { path: .env, required: off }\n").env_files[0]?.required).toBe(false);
  });

  it("rejects an unquoted integer beyond the JS safe range (would be silently rounded — codex)", () => {
    // A large unquoted id/account number loses precision in the JS YAML parser;
    // reject it loudly rather than accept a corrupted value (quote it as a string).
    expect(() => env("variables:\n  ACCOUNT_ID: 123456789012345678\n")).toThrow(/safe-integer range/);
    // Quoting it preserves it exactly (as a string, matching an author's intent).
    expect(env("variables:\n  ACCOUNT_ID: '123456789012345678'\n").variables["ACCOUNT_ID"]).toBe("123456789012345678");
    // A safe integer and a float still load.
    expect(env("variables:\n  N: 42\n  R: 1.5\n").variables).toEqual({ N: 42, R: 1.5 });
  });

  it("rejects an unknown key (strict) and a non-mapping/empty document", () => {
    expect(() => env("bogus: 1\n")).toThrow(/invalid environment/);
    expect(() => loadEnvironmentSpec("- a\n")).toThrow(/must be a mapping/);
    expect(() => loadEnvironmentSpec("")).toThrow(/empty environment/);
  });

  it("rejects a duplicate mapping key (no silent last-win)", () => {
    expect(() => loadEnvironmentSpec("version: \"1\"\nname: a\nname: b\n")).toThrow(/invalid environment YAML/);
  });
});

describe("validateYamlOverrides + yamlOverridePaths (#454)", () => {
  it("throws on the first allow-list violation", () => {
    expect(() => validateYamlOverrides({ task_queue: "q", nope: 1 })).toThrow(/nope is not an allowed/);
    expect(() => validateYamlOverrides({ runtime: { provider: {}, bad: 1 } })).toThrow(
      /runtime.bad is not an allowed runtime override/,
    );
    expect(() => validateYamlOverrides({ task_queue: "q", runtime: { provider: { type: "openai" } } })).not.toThrow();
  });

  it("a non-mapping runtime override is rejected", () => {
    expect(() => validateYamlOverrides({ runtime: "oops" })).toThrow(/runtime must be a mapping/);
  });

  it("computes sorted dotted leaf-paths (empty mapping is a leaf)", () => {
    expect(yamlOverridePaths({ task_queue: "q", runtime: { provider: { type: "openai" }, temporal: {} } })).toEqual([
      "runtime.provider.type",
      "runtime.temporal",
      "task_queue",
    ]);
  });
});

describe("prototype-key hardening (#454; assertSafeKeys parity with loadYamlSpec)", () => {
  it("rejects a top-level __proto__ key that zod's strict schema would silently ignore", () => {
    expect(() => loadEnvironmentSpec("name: e\n__proto__: { x: 1 }\n")).toThrow(/unsafe key '__proto__'/);
    expect(({} as Record<string, unknown>).x).toBeUndefined(); // Object.prototype untouched
  });

  it("rejects a nested prototype-aliasing key, naming its path", () => {
    expect(() => loadEnvironmentSpec("name: e\noverrides: { runtime: { __proto__: {} } }\n")).toThrow(
      /unsafe key '__proto__'.*overrides\.runtime/,
    );
  });
});
