/**
 * Bundle ACTIVITIES (#568; Python `project/bundle.py` `_bundle_activities` + `BundleActivity`):
 * the planned activity set with its secret-free AI/source metadata. Reuses the activity-catalog
 * field helpers (`definition_source`, `prompt_ref`, `provider_params`, `artifact_inputs`) so the
 * two projections cannot drift.
 *
 * Shape differs from the catalog in three ways, all Python-parity:
 *   1. The bundle serializes with `exclude_none`, so a plain-Temporal activity OMITS `prompt_ref`
 *      / `validation_retries`, and an activity with no timeout / retry / task queue OMITS those.
 *   2. Schema identity is `{name, hash}` — the bundle records `schema_identity().to_dict()` (no
 *      JSON Schema; the Python-only `module` key is edition-native, #644 / masked).
 *   3. The bundle carries the activity's OWN `retry` (Python `_bundle_retry_policy`), which the
 *      catalog does not; the catalog carries `provider_params`/`task_queue`/`compatible_next`,
 *      which the bundle does not.
 *
 * The `decide` `kind: temporal` / `definition_source.kind: python` divergence is the same #496
 * dialect artifact the catalog carries (the TS fixture declares `decide` inline-prompted): a
 * recorded ts-cp edition variant, not a bug.
 */

import { activitySlotJsonSchema, schemaHash } from "@typeflux/temporal";
import {
  type ActivityRetrySpec,
  flattenPlanSteps,
  type TypefluxYamlSpec,
  type WorkflowPlan,
  workflowPlanFromSpec,
} from "@typeflux/temporal-yaml";
import type { z } from "zod";

import {
  promptRefDto,
  safeArtifactInputs,
  schemaLogicalName,
  yamlDefinitionSource,
} from "./activity-catalog.js";
import type { ApiBundleRetryPolicy } from "./bundle-steps.js";

/** One bundle activity descriptor (Python `BundleActivity`, exclude_none). */
export interface ApiBundleActivity {
  name: string;
  kind: "ai" | "temporal";
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  /** Omitted for a non-AI activity (exclude_none) — Python `prompt_ref` is None there. */
  prompt_ref?: Record<string, unknown>;
  definition_source: Record<string, unknown>;
  /** Omitted when the activity has no queue override (exclude_none) — always absent in the TS spec. */
  task_queue?: string;
  /** Omitted when the activity has no timeout (exclude_none). */
  start_to_close_timeout_seconds?: number;
  /** Omitted when the activity declares no per-activity retry (exclude_none). */
  retry?: ApiBundleRetryPolicy;
  /** Omitted for a non-AI activity (exclude_none). */
  validation_retries?: number;
  artifact_inputs: Record<string, unknown>[];
  used_by_steps: string[];
}

const asc = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** Schema identity as the bundle records it (Python `schema_identity(...).to_dict()` — name + hash;
 * the `module*` fields are Python-import provenance with no TS analogue, so they never appear). */
function bundleActivitySchemaIdentity(
  ref: string,
  io: "input" | "output",
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
): Record<string, unknown> {
  const schema = schemas !== undefined && Object.hasOwn(schemas, ref) ? schemas[ref] : undefined;
  if (schema === undefined) {
    throw new Error(`resolved bundle requires an injected schema for activity ref '${ref}' (none was supplied)`);
  }
  const jsonSchema = activitySlotJsonSchema(schema, io) as Record<string, unknown>;
  return { name: schemaLogicalName(ref), hash: schemaHash(jsonSchema) };
}

/** The activity's own retry policy as the bundle DTO (Python `_bundle_retry_policy`), or undefined. */
function bundleRetryPolicy(retry: ActivityRetrySpec | undefined): ApiBundleRetryPolicy | undefined {
  if (retry === undefined) return undefined;
  return {
    maximum_attempts: retry.maximum_attempts,
    initial_interval_seconds: retry.initial_interval_seconds,
    ...(retry.maximum_interval_seconds !== null ? { maximum_interval_seconds: retry.maximum_interval_seconds } : {}),
    backoff_coefficient: retry.backoff_coefficient,
  };
}

/**
 * Build the bundle's `activities` (always-present array; Python `()` default), sorted by name to
 * match Python's `sorted(activities)`. Every TS-dialect activity is a YAML-declared AI activity
 * (module activities are injected, #496), so `kind` is `ai` and `definition_source` is the YAML shape.
 */
export function buildBundleActivities(
  spec: TypefluxYamlSpec,
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
  sharedPlan?: WorkflowPlan,
): ApiBundleActivity[] {
  const definitions = spec.activities.definitions ?? [];
  const plan = sharedPlan ?? workflowPlanFromSpec(spec);
  const usedBySteps = new Map<string, string[]>();
  // Leaf steps, depth-first (#55): nested branch steps use activities like any other.
  for (const step of flattenPlanSteps(plan.steps)) {
    usedBySteps.set(step.activity, [...(usedBySteps.get(step.activity) ?? []), step.id]);
  }
  const definitionSource = yamlDefinitionSource(spec);
  return [...definitions]
    .sort((a, b) => asc(a.name, b.name))
    .map((definition) => {
      const retry = bundleRetryPolicy(definition.retry);
      return {
        name: definition.name,
        kind: "ai" as const,
        input_schema: bundleActivitySchemaIdentity(definition.input, "input", schemas),
        output_schema: bundleActivitySchemaIdentity(definition.output, "output", schemas),
        // exclude_none: an AI activity always has a prompt_ref, so it is always present here.
        prompt_ref: promptRefDto(definition.prompt),
        definition_source: definitionSource,
        // task_queue: the TS spec has no per-activity queue → always None → omitted (exclude_none).
        ...(definition.start_to_close_timeout_seconds !== undefined
          ? { start_to_close_timeout_seconds: definition.start_to_close_timeout_seconds }
          : {}),
        ...(retry !== undefined ? { retry } : {}),
        validation_retries: definition.validation_retries,
        artifact_inputs: safeArtifactInputs(definition),
        used_by_steps: [...(usedBySteps.get(definition.name) ?? [])],
      };
    });
}
