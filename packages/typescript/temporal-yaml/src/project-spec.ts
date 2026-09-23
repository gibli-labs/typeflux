/**
 * Project spec (governance parity, #454; Python `project/spec.py`
 * `TypefluxProjectSpec`). The operator-authored `typeflux.project.yaml` manifest
 * that ties a set of workflows to their environments, policies, and component
 * profiles — the bundle the control-plane validates and deploys.
 *
 * This module is the strict SPEC + text loader (parity of Python's project model).
 * It parses the manifest and validates its shape; it does NOT touch the filesystem
 * — the `path`/`directory` references are resolved by the bundle layer (the next
 * increment), which is where the filesystem-vs-injection decision lands. Strict
 * throughout (Python `extra="forbid"`): an unknown key is a typo that could point
 * the deploy at the wrong file, so it fails loudly.
 */

import { parseDocument } from "yaml";
import { z } from "zod";

import { assertSafeKeys } from "./overrides.js";

/** Non-empty AND trimmed (Python `_validate_non_empty_string`). */
const trimmedNonEmpty = (field: string) =>
  z.string().refine((s) => s.length > 0 && s.trim() === s, {
    message: `${field} must be non-empty and trimmed`,
  });

/** A local reference name — non-empty and trimmed (Python `_validate_ref_name`). */
const refName = trimmedNonEmpty("reference name");

/** A workflow id (Python `_ID_PATTERN`): alnum start, then alnum / `_` / `.` / `-`. */
const workflowId = z
  .string()
  .refine((s) => s.length > 0 && s.trim() === s, { message: "workflow id must be non-empty" })
  .refine((s) => /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(s), {
    message: "workflow id must start with an alphanumeric and contain only letters, digits, '.', '_', or '-'",
  });

/** A file-reference map value: a non-empty, trimmed path (Python `_validate_reference_map`). */
const referencePath = trimmedNonEmpty("reference path");

/**
 * A nullable-optional field. Python's `X | None` treats an explicit YAML `null`
 * (or a bare `key:` with no value) as ABSENT, and `model_dump()` round-trips an
 * unused field as `null`; zod's `.optional()` admits only `undefined`, so coerce
 * `null → absent` for cross-SDK/round-trip parity (codex/finder).
 */
const nullToAbsent = <T extends z.ZodType>(schema: T) =>
  z.preprocess((value) => (value === null ? undefined : value), schema.optional());

/** Project-wide defaults applied beneath every workflow YAML (Python `TypefluxProjectDefaultsSpec`). */
export const typefluxProjectDefaultsSpec = z
  .object({
    workflow_filename: z
      .string()
      .refine((s) => s.length > 0 && s.trim() === s, { message: "defaults.workflow_filename must be non-empty" })
      .refine((s) => s !== "." && s !== ".." && !s.includes("/") && !s.includes("\\"), {
        message: "defaults.workflow_filename must be a file name (no path separators)",
      })
      .default("typeflux.yaml"),
    // Project-wide runtime defaults merged UNDER each workflow YAML (engine < these
    // < workflow YAML < profiles < environment). An open map, validated when merged.
    runtime: z.record(z.string(), z.unknown()).default({}),
  })
  .strict();

/** One workflow in the project — located by exactly one of `path` or `directory` (Python `ProjectWorkflowSpec`). */
export const projectWorkflowSpec = z
  .object({
    id: workflowId,
    path: nullToAbsent(trimmedNonEmpty("workflow path")),
    directory: nullToAbsent(trimmedNonEmpty("workflow directory")),
    // Component profile selection by kind, e.g. `{ provider: "anthropic-prod" }`.
    // Python leaves this an unvalidated `dict[str, str]` (no ref refinement), so
    // match it rather than reject manifests Python accepts (parity finder).
    profiles: z.record(z.string(), z.string()).default({}),
  })
  .strict()
  .refine((w) => (w.path !== undefined) !== (w.directory !== undefined), {
    message: "workflow must configure exactly one of `path` or `directory`",
  });

/** A validation target: which workflows to validate, in which environment, under which policies. */
export const projectValidationTargetSpec = z
  .object({
    workflows: z.array(refName).default([]),
    environment: nullToAbsent(refName),
    policies: z.array(refName).default([]),
  })
  .strict();

/** The validation-target map (Python `ProjectValidationSpec`), keyed by target name. */
export const projectValidationSpec = z
  .object({
    targets: z.record(refName, projectValidationTargetSpec).default({}),
  })
  .strict();

/** File-referenced component profiles, keyed by kind then id (#214; Python `ProjectProfilesSpec`). */
export const projectProfilesSpec = z
  .object({
    provider: z.record(refName, referencePath).default({}),
    registry: z.record(refName, referencePath).default({}),
    runtime: z.record(refName, referencePath).default({}),
  })
  .strict();

/**
 * A project manifest (Python `TypefluxProjectSpec`). `environments`/`policies` map
 * an id to a file reference; `workflows` are the project's workflow set (>= 1,
 * unique ids). The filesystem-only `manifest_path`/`resolved_path` fields are a
 * bundle-layer concern and are not modeled here — this is the parsed manifest.
 */
export const typefluxProjectSpec = z
  .object({
    version: z.literal("1").default("1"),
    name: trimmedNonEmpty("project name"),
    defaults: typefluxProjectDefaultsSpec.prefault({}),
    workflows: z.array(projectWorkflowSpec).min(1, "project workflows must contain at least one workflow"),
    environments: z.record(refName, referencePath).default({}),
    policies: z.record(refName, referencePath).default({}),
    profiles: nullToAbsent(projectProfilesSpec),
    validation: projectValidationSpec.prefault({}),
  })
  .strict()
  .superRefine((spec, ctx) => {
    // Duplicate workflow ids would make an id → workflow lookup ambiguous (the
    // bundle resolves targets by id), so reject them loudly (Python `_validate_workflows`).
    const seen = new Set<string>();
    const duplicates = new Set<string>();
    for (const workflow of spec.workflows) {
      if (seen.has(workflow.id)) duplicates.add(workflow.id);
      seen.add(workflow.id);
    }
    if (duplicates.size > 0) {
      ctx.addIssue({
        code: "custom",
        message: `duplicate project workflow id(s): ${[...duplicates].sort().join(", ")}`,
        path: ["workflows"],
      });
    }
  });

export type TypefluxProjectDefaultsSpec = z.infer<typeof typefluxProjectDefaultsSpec>;
export type ProjectWorkflowSpec = z.infer<typeof projectWorkflowSpec>;
export type ProjectValidationTargetSpec = z.infer<typeof projectValidationTargetSpec>;
export type ProjectValidationSpec = z.infer<typeof projectValidationSpec>;
export type ProjectProfilesSpec = z.infer<typeof projectProfilesSpec>;
export type TypefluxProjectSpec = z.infer<typeof typefluxProjectSpec>;

/** Bytes/alias bounds for operator-trusted manifests (parity with the policy + YAML loaders). */
const MAX_PROJECT_BYTES = 1024 * 1024;
const MAX_PROJECT_ALIASES = 1000;

export interface LoadProjectSpecOptions {
  /** A label for error messages (e.g. the manifest file path). */
  sourceLabel?: string;
}

/**
 * Parse + strict-validate a project manifest (Python `load_project_spec`, the model
 * half). Duplicate keys are rejected and size/alias expansion is bounded, exactly
 * like the policy + workflow-spec loaders. Text-only: `path`/`directory` references
 * are validated for shape but NOT resolved against the filesystem here.
 */
export function loadProjectSpec(text: string, options: LoadProjectSpecOptions = {}): TypefluxProjectSpec {
  const sourceLabel = options.sourceLabel ?? "project";
  if (Buffer.byteLength(text, "utf-8") > MAX_PROJECT_BYTES) {
    throw new Error(`project manifest exceeds the ${MAX_PROJECT_BYTES} byte limit (${sourceLabel})`);
  }
  const doc = parseDocument(text, { uniqueKeys: true, merge: true });
  // Duplicate mapping keys must FAIL, not silently last-win (the manifest would then
  // resolve against a different effective document than the author read), matching
  // loadPolicySpec / loadYamlSpec.
  const fatal = [...doc.errors, ...doc.warnings.filter((w) => w.code === "DUPLICATE_KEY")];
  if (fatal.length > 0) {
    throw new Error(`invalid project YAML (${sourceLabel}): ${fatal[0]?.message ?? "parse error"}`);
  }
  const raw: unknown = doc.toJS({ maxAliasCount: MAX_PROJECT_ALIASES });
  if (raw === null || raw === undefined) {
    throw new Error(`empty project manifest: ${sourceLabel}`);
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new TypeError(`project manifest must be a mapping (${sourceLabel})`);
  }
  // Reject an own `__proto__` key that zod's strict schema would silently IGNORE
  // rather than reject — Python's `extra=forbid` rejects it, and `loadYamlSpec` guards
  // the same class (`constructor`/`prototype` are already rejected by strict itself).
  assertSafeKeys(raw, sourceLabel);
  const result = typefluxProjectSpec.safeParse(raw);
  if (!result.success) {
    throw new Error(`invalid project manifest (${sourceLabel}):\n${z.prettifyError(result.error)}`);
  }
  return result.data;
}
