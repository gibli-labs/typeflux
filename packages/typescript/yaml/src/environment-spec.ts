/**
 * Project ENVIRONMENT spec (governance parity, #454; Python
 * `project/environment.py` `ProjectEnvironmentSpec` + `yaml/overrides.py`). An
 * environment binds a project's workflows to a deployment target: `.env` file
 * references, inline variables, and a bounded set of runtime OVERRIDES (which the
 * bundle overlay applies onto each workflow's config).
 *
 * This module is the strict SPEC + text loader + the override allow-list. It is
 * TEXT-ONLY — the `env_files` paths are validated for shape but not read (the
 * caller reads them and supplies the values; the overlay slice merges them). The
 * filesystem-only `profile_path` is not modeled.
 *
 * The override surface is DELIBERATELY bounded (Python `_ALLOWED_OVERRIDE_KEYS` /
 * `_ALLOWED_RUNTIME_OVERRIDE_KEYS`): an environment may retarget only `task_queue`
 * and a fixed set of `runtime.*` connection/provider blocks — never the workflow
 * graph, activities, or prompts — so a deployment target can't silently rewrite
 * what the workflow DOES.
 */

import { parseDocument } from "yaml";
import { z } from "zod";

import { assertSafeKeys, overrideViolations } from "./overrides.js";

/** Non-empty AND trimmed (Python `_validate_non_empty_string`). */
const trimmedNonEmpty = (field: string) =>
  z.string().refine((s) => s.length > 0 && s.trim() === s, {
    message: `${field} must be non-empty and trimmed`,
  });

/**
 * A boolean that also accepts the string bool-words (`yes`/`no`/`on`/`off`/…). The
 * JS `yaml` lib parses to the YAML 1.2 core schema, so `required: yes` resolves to
 * the STRING "yes" — whereas PyYAML's 1.1 schema makes it a bool. Coercing here keeps
 * a Python-loadable environment loadable in TS (the broader YAML 1.1↔1.2 scalar
 * divergence on `variables`/numeric values is an SDK-wide trait of the JS YAML lib).
 */
const yamlBoolean = z.union([z.boolean(), z.stringbool()]);

/** An environment variable name — POSIX-ish identifier (Python `_ENV_KEY_PATTERN`). */
const envKey = z.string().refine((s) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(s), {
  message: "environment variable name must match [A-Za-z_][A-Za-z0-9_]*",
});

/**
 * An env value stringified at application time (Python `EnvValue = str | bool | int
 * | float`). An unquoted integer beyond JS's safe range is rejected rather than
 * silently ACCEPTED after the YAML parser rounded it — Python keeps arbitrary-size
 * ints and stringifies them exactly, so a rounded value would corrupt an id/account
 * number (codex); the fix says "quote it". `z.number()` also narrows out non-finite
 * floats (a pathological `.inf`/`.nan` variable). The broader YAML 1.1↔1.2 scalar
 * divergence (bool-words, octal/underscore/sci-notation) is an SDK-wide trait of the
 * JS YAML lib, not specific to this module.
 */
const envValue = z.union([
  z.string(),
  z.boolean(),
  z.number().refine((n) => !Number.isInteger(n) || Number.isSafeInteger(n), {
    message: "environment variable integer value exceeds the JS safe-integer range — quote it as a string",
  }),
]);

/** A bounded runtime-override map — only the allow-listed paths (Python `validate_yaml_overrides`). */
const overrideMap = (prefix: string) =>
  z
    .record(z.string(), z.unknown())
    .default({})
    .superRefine((overrides, ctx) => {
      for (const message of overrideViolations(overrides, prefix)) {
        ctx.addIssue({ code: "custom", message });
      }
    });

/** A referenced `.env` file (Python `ProjectEnvironmentEnvFileSpec`). */
export const projectEnvironmentEnvFileSpec = z
  .object({
    path: trimmedNonEmpty("environment env_files.path"),
    required: yamlBoolean.default(true),
  })
  .strict();

/** Per-workflow environment override + profile selection (Python `ProjectEnvironmentWorkflowSpec`). */
export const projectEnvironmentWorkflowSpec = z
  .object({
    overrides: overrideMap("workflows.<workflow>.overrides"),
    // Per-kind profile selection replaces the workflow-level selection for that kind
    // (whole-reference replacement). Unvalidated like the project-level selection.
    profiles: z.record(z.string(), z.string()).default({}),
  })
  .strict();

/**
 * A project environment (Python `ProjectEnvironmentSpec`): `.env` file references
 * + inline `variables` (the interpolation context) and the bounded `overrides`
 * applied to every workflow, plus per-workflow overrides. The filesystem-only
 * `profile_path` is a bundle-layer concern and not modeled here.
 */
export const projectEnvironmentSpec = z
  .object({
    version: z.literal("1").default("1"),
    name: trimmedNonEmpty("environment name"),
    env_files: z.array(projectEnvironmentEnvFileSpec).default([]),
    variables: z.record(envKey, envValue).default({}),
    overrides: overrideMap("overrides"),
    workflows: z.record(trimmedNonEmpty("environment workflow override id"), projectEnvironmentWorkflowSpec).default({}),
  })
  .strict();

export type ProjectEnvironmentEnvFileSpec = z.infer<typeof projectEnvironmentEnvFileSpec>;
export type ProjectEnvironmentWorkflowSpec = z.infer<typeof projectEnvironmentWorkflowSpec>;
export type ProjectEnvironmentSpec = z.infer<typeof projectEnvironmentSpec>;

/** Bytes/alias bounds for operator-trusted environment files (parity with the sibling loaders). */
const MAX_ENVIRONMENT_BYTES = 1024 * 1024;
const MAX_ENVIRONMENT_ALIASES = 1000;

export interface LoadEnvironmentSpecOptions {
  /** A label for error messages (e.g. the environment file path). */
  sourceLabel?: string;
}

/**
 * Parse + strict-validate a project environment document (Python
 * `load_project_environment`, the model half). Duplicate keys are rejected and
 * size/alias expansion is bounded, exactly like the policy/project/workflow loaders.
 * Text-only: `env_files` paths are validated for shape but NOT read here.
 */
export function loadEnvironmentSpec(text: string, options: LoadEnvironmentSpecOptions = {}): ProjectEnvironmentSpec {
  const sourceLabel = options.sourceLabel ?? "environment";
  if (Buffer.byteLength(text, "utf-8") > MAX_ENVIRONMENT_BYTES) {
    throw new Error(`environment document exceeds the ${MAX_ENVIRONMENT_BYTES} byte limit (${sourceLabel})`);
  }
  const doc = parseDocument(text, { uniqueKeys: true, merge: true });
  const fatal = [...doc.errors, ...doc.warnings.filter((w) => w.code === "DUPLICATE_KEY")];
  if (fatal.length > 0) {
    throw new Error(`invalid environment YAML (${sourceLabel}): ${fatal[0]?.message ?? "parse error"}`);
  }
  const raw: unknown = doc.toJS({ maxAliasCount: MAX_ENVIRONMENT_ALIASES });
  if (raw === null || raw === undefined) {
    throw new Error(`empty environment document: ${sourceLabel}`);
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new TypeError(`environment must be a mapping (${sourceLabel})`);
  }
  // Reject an own `__proto__` key that zod's strict schema would silently IGNORE
  // rather than reject — Python's `extra=forbid` rejects it, and `loadYamlSpec` guards
  // the same class (`constructor`/`prototype` are already rejected by strict itself).
  assertSafeKeys(raw, sourceLabel);
  const result = projectEnvironmentSpec.safeParse(raw);
  if (!result.success) {
    throw new Error(`invalid environment (${sourceLabel}):\n${z.prettifyError(result.error)}`);
  }
  return result.data;
}
