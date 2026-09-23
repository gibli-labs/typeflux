/**
 * Bundle STEPS (#575; Python `project/bundle.py` `_bundle_steps`): the workflow's execution
 * sequence with the EFFECTIVE per-step Temporal knobs — timeout and retry after every default
 * layer resolved (definition's own > workflow-wide `runtime.activity_retry` > engine defaults),
 * exactly what the interpreter passes to `proxyActivities`. The resolution comes from the SAME
 * plan helpers the interpreter uses (`activityProxyOptions` / `resolvedActivityRetryPlan`), so
 * the bundle cannot drift from execution.
 *
 * Divergence from Python (documented, #575): a TS map step's `collect` is optional (a
 * collect-less map yields an array), so `collect_output_schema`/`collect_field` are omitted
 * for those steps — Python's map spec requires `collect`, so its `BundleMapShape` never
 * has the fields absent.
 */

import { activitySlotJsonSchema, schemaHash } from "@typeflux/temporal";

import { schemaLogicalName } from "./activity-catalog.js";
import {
  activityProxyOptions,
  DEFAULT_MAP_CONCURRENCY,
  flattenPlanSteps,
  resolvedActivityRetryPlan,
  type TypefluxYamlSpec,
  type WorkflowPlan,
  workflowPlanFromSpec,
} from "@typeflux/temporal-yaml";
import type { z } from "zod";

/** Python `BundleRetryPolicy` (seconds; `maximum_attempts: 0` = unlimited sentinel). */
export interface ApiBundleRetryPolicy {
  maximum_attempts: number;
  initial_interval_seconds?: number;
  maximum_interval_seconds?: number;
  backoff_coefficient?: number;
}

/** Python `BundleMapShape`; `collect_*` absent for a (TS-only) collect-less map. */
export interface ApiBundleMapShape {
  over: string;
  concurrency: number;
  collect_output_schema?: Record<string, unknown>;
  collect_field?: string;
}

/** Python `BundleStep`. */
export interface ApiBundleStep {
  id: string;
  kind: "activity" | "map";
  activity: string;
  map?: ApiBundleMapShape;
  effective_start_to_close_timeout_seconds: number;
  effective_retry: ApiBundleRetryPolicy;
}

/** The collect's schema identity (Python `schema_identity(...).to_dict()` — name + hash;
 * the `module*` fields are Python-import provenance with no TS analogue). */
function collectSchemaIdentity(
  ref: string,
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
): Record<string, unknown> {
  const schema = schemas !== undefined && Object.hasOwn(schemas, ref) ? schemas[ref] : undefined;
  if (schema === undefined) {
    throw new Error(`bundle steps require an injected schema for map collect ref '${ref}' (none was supplied)`);
  }
  const jsonSchema = activitySlotJsonSchema(schema, "output") as Record<string, unknown>;
  return { name: schemaLogicalName(ref), hash: schemaHash(jsonSchema) };
}

/**
 * Build the bundle's `steps` (always-present array; Python `()` default). The spec has
 * already been plan-validated by the bundle's graph assertion, so the plan build here
 * cannot fail for a bundle that reaches this point.
 */
export function buildBundleSteps(
  spec: TypefluxYamlSpec,
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
  sharedPlan?: WorkflowPlan,
): ApiBundleStep[] {
  // Accepts the caller's already-built plan so a bundle request derives it once (#575 review).
  const plan = sharedPlan ?? workflowPlanFromSpec(spec);
  // The spec step carries the map's collect refs; the plan (deliberately schema-free so the
  // workflow sandbox can import it) only carries `collectField`. Same ids, same (flattened) order.
  // Recursive: maps nested in parallel branches (#55) carry collect refs too.
  const specMapById = new Map<string, NonNullable<TypefluxYamlSpec["workflow"]["steps"][number]["map"]>>();
  const collectMapSpecs = (steps: TypefluxYamlSpec["workflow"]["steps"]): void => {
    for (const step of steps) {
      if (step.map !== undefined) {
        specMapById.set(step.id, step.map);
      }
      for (const branch of step.parallel?.branches ?? []) {
        collectMapSpecs(branch.steps);
      }
    }
  };
  collectMapSpecs(spec.workflow.steps);
  // LEAF steps, depth-first (#55): `steps` carries per-activity effective options, so a
  // parallel node — which calls no activity — contributes its branches' steps, not itself;
  // the block structure lives in the topology projection.
  return flattenPlanSteps(plan.steps).map((step) => {
    const proxy = activityProxyOptions(plan, step.activity);
    const retry = resolvedActivityRetryPlan(plan, step.activity);
    const effectiveRetry: ApiBundleRetryPolicy = {
      maximum_attempts: retry.maximumAttempts,
      initial_interval_seconds: retry.initialIntervalMs / 1000,
      ...(retry.maximumIntervalMs !== undefined
        ? { maximum_interval_seconds: retry.maximumIntervalMs / 1000 }
        : {}),
      backoff_coefficient: retry.backoffCoefficient,
    };
    if (step.kind !== "map") {
      return {
        id: step.id,
        kind: "activity",
        activity: step.activity,
        effective_start_to_close_timeout_seconds: proxy.startToCloseTimeout / 1000,
        effective_retry: effectiveRetry,
      };
    }
    const collect = specMapById.get(step.id)?.collect;
    return {
      id: step.id,
      kind: "map",
      activity: step.activity,
      map: {
        over: step.over,
        concurrency: step.concurrency ?? DEFAULT_MAP_CONCURRENCY,
        ...(collect !== undefined
          ? {
              collect_output_schema: collectSchemaIdentity(collect.output, schemas),
              collect_field: collect.field,
            }
          : {}),
      },
      effective_start_to_close_timeout_seconds: proxy.startToCloseTimeout / 1000,
      effective_retry: effectiveRetry,
    };
  });
}
