/**
 * The resolved-workflow ACTIVITY CATALOG projection (governance parity, #563 slice 2b; Python
 * `project/catalog.py` `resolve_activity_catalog` + `ActivityCatalog`). Re-projects a resolved
 * workflow's declared activities into a UI-friendly, secret-free shape: schema identities +
 * JSON Schemas for inspection, the activity kind, and static type-compatibility edges
 * (`compatible_next`) so a control plane can reason about valid step chains.
 *
 * INJECTION note: the JSON Schemas come from the caller-supplied Zod `schemas` (the same map
 * injected into `defineActivitiesFromSpec`) — the control plane holds no activity code. Every
 * referenced schema MUST be supplied (the `CatalogSchema` contract requires `hash`+`json_schema`);
 * a missing ref fails the catalog (422 via the CP) rather than emitting a partial, unparseable slot.
 * Slots are normalized + hashed through the runtime's OWN `activitySlotJsonSchema`/`schemaHash`, so
 * the catalog's `json_schema`/`hash` equal the activity descriptor's by construction, and an output
 * that can't be made provider-safe is rejected (422) exactly as `defineActivity` rejects it.
 *
 * SCOPE: the catalog's core (schema identities, kinds, `used_by_steps`, `compatible_next`), the
 * clear spec fields (`validation_retries`, `start_to_close_timeout_seconds`), AND the AI/source
 * metadata Python carries (#568): `prompt_ref`, `provider_params`, `artifact_inputs`,
 * `definition_source`, `task_queue`. `definition_source` is the honest TS-dialect shape — every
 * YAML-declared activity is `{kind: "yaml", yaml_project, yaml_name}` (Python `ActivityDefinitionSource`
 * for a YAML activity). Python's `decide` is a plain Temporal module activity (`kind: python`,
 * `kind: temporal`); the TS dialect declares it inline-prompted, so its projection reads `kind: ai`
 * with a yaml source — a recorded ts-cp edition divergence (#496/#568), not a bug.
 */

import { activitySlotJsonSchema, schemaHash, toProviderSafe } from "@typeflux/temporal";
import {
  type ActivityDefinitionSpec,
  flattenPlanSteps,
  flattenSubworkflowSteps,
  type ProviderParamsSpec,
  providerParamsRecord,
  type SubworkflowSpecResolver,
  type TypefluxYamlSpec,
  type WorkflowPlan,
  workflowPlanFromSpec,
  workflowSchemaChainError,
} from "@typeflux/temporal-yaml";
import type { z } from "zod";

export const CATALOG_VERSION = "1" as const;

/** A schema slot with its JSON Schema for inspection (Python `CatalogSchema` — all fields required). */
export interface ApiCatalogSchema {
  name: string;
  /** sha256 over the schema's canonical JSON — equals the runtime activity descriptor's schema hash
   * (same `io`/`$schema` normalization + shared `schemaHash`); TS-native, not byte-identical to Python's. */
  hash: string;
  /** The JSON Schema (from the injected Zod schema), normalized like the runtime descriptor. */
  json_schema: Record<string, unknown>;
}

/**
 * One catalog entry (Python `CatalogActivity`, core subset — see the module SCOPE note).
 * NB: the `/catalog` route is the one read route Python serializes WITHOUT `exclude_none`, so
 * `validation_retries` and `start_to_close_timeout_seconds` are ALWAYS present (the latter `null`
 * when the activity has no timeout) — not omitted like the exclude_none DTOs elsewhere.
 */
export interface ApiCatalogActivity {
  name: string;
  kind: "ai" | "temporal";
  input_schema: ApiCatalogSchema;
  output_schema: ApiCatalogSchema;
  /** Where the activity is defined (Python `ActivityDefinitionSource.to_dict()`, None-dropped). A
   * TS-dialect (YAML) activity is `{kind: "yaml", yaml_project, yaml_name}`. */
  definition_source: Record<string, unknown>;
  /** The activity's own task queue override, or `null` (the TS spec has no per-activity queue). */
  task_queue: string | null;
  /** The registry prompt reference (Python `PromptRef.to_dict()` → `{name, version, label}`), or
   * `null` for a non-AI activity. NB: `prompt_type` is NOT serialized (Python parity). */
  prompt_ref: Record<string, unknown> | null;
  /** The activity's OWN behavior params (Python `activity.provider_params.to_dict()`, None-dropped) —
   * NOT merged with prompt/provider layers. `{}` when the activity declares none. */
  provider_params: Record<string, unknown>;
  /** Always present for an AI activity (the spec defaults it to 1). */
  validation_retries: number;
  /** Secret-free artifact-input descriptors (Python `ArtifactInput.safe_definition()`), `[]` when none. */
  artifact_inputs: Record<string, unknown>[];
  /** The activity's start-to-close timeout in seconds, or `null` when it has none. */
  start_to_close_timeout_seconds: number | null;
  /** Steps in the resolved workflow that schedule this activity. */
  used_by_steps: string[];
  /** Activities whose input schema ref matches this activity's output — statically-valid successors. */
  compatible_next: string[];
}

/**
 * The definition source for a YAML-declared activity (Python `ActivityDefinitionSource(kind="yaml",
 * yaml_project=…, yaml_name=…).to_dict()` — None keys dropped). Every TS-dialect activity is
 * YAML-declared (module activities are injected, #496), so `module`/`export` never appear.
 */
export function yamlDefinitionSource(spec: TypefluxYamlSpec): Record<string, unknown> {
  return { kind: "yaml", yaml_project: spec.project, yaml_name: spec.name };
}

/**
 * A registry prompt reference as its DTO (Python `PromptRef.to_dict()`): `{name, version, label}`
 * with `version`/`label` defaulting to `null`. A bare-string `prompt:` is a name-only ref (no
 * version/label). `prompt_type` is deliberately NOT serialized — Python's `to_dict` omits it.
 */
export function promptRefDto(prompt: ActivityDefinitionSpec["prompt"]): Record<string, unknown> {
  if (typeof prompt === "string") {
    return { name: prompt, version: null, label: null };
  }
  return { name: prompt.name, version: prompt.version ?? null, label: prompt.label ?? null };
}

/**
 * The activity's OWN provider params as the None-dropped record (Python
 * `activity.provider_params.to_dict()` with `include_empty=False`). {@link providerParamsRecord}
 * already omits absent keys and normalizes `stop` to an array, matching Python's drop-None + `list(stop)`.
 */
export function activityProviderParams(params: ProviderParamsSpec | undefined): Record<string, unknown> {
  return params === undefined ? {} : providerParamsRecord(params);
}

/**
 * Secret-free artifact-input descriptors (Python `ArtifactInput.safe_definition()`): the safe
 * subset with None/empty dropped, `attach` reduced to `{role}`, and the spec's `from`→`from_path`
 * / `cache`→`cache_role` renames. Never carries text/payload that could leak.
 */
export function safeArtifactInputs(spec: ActivityDefinitionSpec): Record<string, unknown>[] {
  return (spec.artifacts ?? []).map((artifact) => {
    const safe: Record<string, unknown> = {
      name: artifact.name,
      from_path: artifact.from,
      required: artifact.required,
    };
    if (artifact.kind !== undefined) safe.kind = artifact.kind;
    if (artifact.media_types.length > 0) safe.media_types = [...artifact.media_types];
    if (artifact.max_count !== undefined && artifact.max_count !== null) safe.max_count = artifact.max_count;
    if (artifact.max_bytes !== undefined && artifact.max_bytes !== null) safe.max_bytes = artifact.max_bytes;
    if (artifact.attach !== undefined) safe.attach = { role: artifact.attach.role };
    if (artifact.cache !== undefined) safe.cache_role = artifact.cache;
    return safe;
  });
}

/** The activity catalog for one resolved workflow/environment (Python `ActivityCatalog`). */
export interface ApiActivityCatalog {
  catalog_version: typeof CATALOG_VERSION;
  project: string;
  workflow_id: string;
  environment_id: string;
  activities: ApiCatalogActivity[];
}

const asc = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/**
 * Build a schema slot from its ref, resolving the JSON Schema + hash from the injected `schemas`.
 * The `CatalogSchema` contract requires `hash`+`json_schema`, so a ref the caller did not supply
 * THROWS (the CP maps it to 422) rather than emitting a partial slot a generated client can't parse.
 *
 * Uses the SHARED `activitySlotJsonSchema` + `schemaHash` from the runtime, so the catalog's
 * json_schema + hash are byte-identical to the activity descriptor's by construction (one
 * normalization path, no drift). OUTPUT slots additionally run the same `toProviderSafe` gate
 * `defineActivity` applies — an output that can't be made provider-safe (a recursive / open-map
 * type) can't run, so the catalog rejects it (→ 422) instead of advertising an unrunnable activity.
 */
function catalogSchema(
  ref: string,
  role: "input" | "output",
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
): ApiCatalogSchema {
  const schema = schemas !== undefined && Object.hasOwn(schemas, ref) ? schemas[ref] : undefined;
  if (schema === undefined) {
    throw new Error(`activity catalog requires an injected schema for ref '${ref}' (none was supplied)`);
  }
  const jsonSchema = activitySlotJsonSchema(schema, role) as Record<string, unknown>;
  if (role === "output") {
    // Gate only (mirrors `defineActivity`): throws ProviderSchemaError if unsafe; we still emit +
    // hash the RAW stripped schema (matching `outputJsonSchema`/`outputSchemaHash`, not the safe form).
    toProviderSafe(jsonSchema);
  }
  return { name: schemaLogicalName(ref), hash: schemaHash(jsonSchema), json_schema: jsonSchema };
}

/**
 * The contract's schema `name` is the LOGICAL type name both editions agree on (#644):
 * Python emits the class name; the TS spec references it as a namespaced ref
 * (`schemas:ClaimItem`) — the prefix is a loader artifact, never contract identity.
 */
export function schemaLogicalName(ref: string): string {
  const sep = ref.lastIndexOf(":");
  return sep === -1 ? ref : ref.slice(sep + 1);
}

/**
 * Project a resolved workflow spec into its activity catalog (Python `resolve_activity_catalog`).
 * `used_by_steps` come from the workflow PLAN; `compatible_next` links each activity to the
 * declared activities whose INPUT schema ref equals this activity's OUTPUT ref (Python compares
 * resolved types; the injection CP compares the schema refs that identify them).
 *
 * `project` is the MANIFEST project name (matching `meta()`/validation), not the workflow YAML's
 * own `project` field — they can differ in a multi-project manifest. Rejects an invalid graph
 * (a duplicate activity name, or a step referencing an undeclared activity) so the catalog never
 * silently drops a dangling reference — Python's `collect_activities`/`create_workflow` do the same.
 */
/**
 * Reject an invalid activity graph (Python `collect_activities`/`create_workflow`/`_validate_workflow_graph`)
 * so a projection never advertises an unrunnable workflow. Checks: a duplicate activity definition name;
 * a step referencing an undeclared activity; and the LINEAR SCHEMA CHAIN — each activity step consumes
 * the previous step's output, so its input ref must equal the running ref (starting at `workflow.input`).
 * A map step reads its `over` path and produces a collect object, so the array/item boundary is not
 * ref-checked here (conservative — left to runtime parse); the chain resumes at its `collect.output`.
 * Returns the map of activity name → the step ids using it. Shared by the catalog and the resolved bundle.
 */
export function assertDeclaredActivityGraph(
  spec: TypefluxYamlSpec,
  sharedPlan?: WorkflowPlan,
  subworkflows?: SubworkflowSpecResolver,
): Map<string, string[]> {
  const definitions = spec.activities.definitions ?? [];
  const declared = new Set<string>();
  for (const definition of definitions) {
    if (declared.has(definition.name)) {
      throw new Error(`duplicate activity definition name: ${definition.name}`);
    }
    declared.add(definition.name);
  }
  const usedBySteps = new Map<string, string[]>();
  // Leaf steps, depth-first (#55): nested branch steps use activities like any other.
  const planOptions = subworkflows !== undefined ? { subworkflows } : {};
  const plan = sharedPlan ?? workflowPlanFromSpec(spec, planOptions);
  for (const step of flattenPlanSteps(plan.steps)) {
    usedBySteps.set(step.activity, [...(usedBySteps.get(step.activity) ?? []), step.id]);
  }
  const dangling = [...usedBySteps.keys()].filter((activity) => !declared.has(activity)).sort(asc);
  if (dangling.length > 0) {
    throw new Error(`workflow references undeclared activities: ${dangling.join(", ")}`);
  }

  // The linear schema chain must type-check too — the SAME check `/validate` runs (Python
  // `_validate_workflow_graph`), so the projections and the validation report stay consistent.
  // Sub-workflow steps type against the CHILD's workflow.input/output, resolved via the resolver.
  const chainError = workflowSchemaChainError(spec, planOptions);
  if (chainError !== undefined) {
    throw new Error(chainError);
  }
  // Recurse into embedded CHILD plans (#55 review): a child referencing an undeclared activity
  // must fail the PARENT's projection here, not one child start later at runtime (Python's
  // `create_workflow` on the child rejects during project resolution — the same posture). Each
  // child validates against ITS OWN spec's declarations; embedded plans are acyclic by
  // construction (plan derivation rejected reference cycles).
  if (subworkflows !== undefined) {
    for (const node of flattenSubworkflowSteps(plan.steps)) {
      const childSpec = subworkflows.specFor(node.workflowId);
      if (childSpec !== undefined) {
        try {
          assertDeclaredActivityGraph(childSpec, node.plan, subworkflows);
        } catch (error) {
          throw new Error(
            `sub-workflow ${JSON.stringify(node.workflowId)} (step ${JSON.stringify(node.id)}): ` +
              (error instanceof Error ? error.message : String(error)),
          );
        }
      }
    }
  }
  return usedBySteps;
}

export function buildActivityCatalog(
  spec: TypefluxYamlSpec,
  project: string,
  workflowId: string,
  environmentId: string,
  schemas: Readonly<Record<string, z.ZodType>> | undefined,
  subworkflows?: SubworkflowSpecResolver,
): ApiActivityCatalog {
  const definitions = spec.activities.definitions ?? [];
  const usedBySteps = assertDeclaredActivityGraph(spec, undefined, subworkflows);

  const definitionSource = yamlDefinitionSource(spec);
  const activities: ApiCatalogActivity[] = [...definitions]
    .sort((a, b) => asc(a.name, b.name))
    .map((definition) => ({
      name: definition.name,
      // Every YAML-declared activity is prompt-backed (Python distinguishes plain Temporal
      // activities, which arrive via module discovery / injected `extraActivities` the CP can't see).
      kind: "ai" as const,
      input_schema: catalogSchema(definition.input, "input", schemas),
      output_schema: catalogSchema(definition.output, "output", schemas),
      // Every TS-dialect activity is YAML-declared (the same source for all in one spec).
      definition_source: definitionSource,
      // The TS activity spec has no per-activity task queue (only the workflow-level one) — always null.
      task_queue: null,
      prompt_ref: promptRefDto(definition.prompt),
      provider_params: activityProviderParams(definition.provider_params),
      validation_retries: definition.validation_retries,
      artifact_inputs: safeArtifactInputs(definition),
      // Always present (the `/catalog` route has no `exclude_none`): `null` when there's no timeout.
      start_to_close_timeout_seconds: definition.start_to_close_timeout_seconds ?? null,
      // Copy the map's array so a caller mutating the response can't corrupt the builder state.
      used_by_steps: [...(usedBySteps.get(definition.name) ?? [])],
      compatible_next: definitions
        .filter((other) => other.name !== definition.name && other.input === definition.output)
        .map((other) => other.name)
        .sort(asc),
    }));

  return {
    catalog_version: CATALOG_VERSION,
    project,
    workflow_id: workflowId,
    environment_id: environmentId,
    activities,
  };
}
