/**
 * The full activity/workflow execution manifest builder (#425), reproducing the
 * Python `manifests/{activity,workflow}.py` composite layer byte-for-byte: the
 * `activity_manifest_hash`, the execution `manifest_hash`, the
 * `workflow_contract_hash`, and the `to_dict()` shapes.
 *
 * This is a CONTRACT-level builder: it takes explicit, already-resolved inputs
 * (schema identity, prompt-ref, resolved provider params, definition-source kind,
 * code-provenance) rather than introspecting types or collecting environment
 * provenance the way Python does. The cross-SDK contract is "same resolved inputs
 * -> same hashes + same dict", which is exactly what these functions guarantee.
 *
 * A subtlety reproduced faithfully: the hash BASE is not drop-none'd (it keeps
 * explicit nulls such as `temperature`, `start_to_close_timeout_seconds`,
 * `hook_name`, prompt-ref `version`), while `to_dict` IS drop-none'd — so the
 * hashed bytes and the emitted dict legitimately differ in which null keys appear.
 */

import { createHash } from "node:crypto";

import { canonicalJson } from "./canonical-json.js";
import { type ChatMessage, messagesHash } from "./manifest-hashing.js";
import { type PromptRef, promptRefToDict } from "./prompt-ref.js";

export type Json = Record<string, unknown>;

function sha256Hex(value: string): string {
  return createHash("sha256").update(value, "utf-8").digest("hex");
}

/** `sha256(canonical_json(base))` — the composite-hash primitive. */
function hashOf(base: unknown): string {
  return sha256Hex(canonicalJson(base));
}

/** Drop keys whose value is `null`/`undefined` (shallow; mirrors Python `drop_none`). */
function dropNone(data: Json): Json {
  const out: Json = {};
  for (const [key, value] of Object.entries(data)) {
    if (value !== null && value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

/** The content-addressed identity of an input/output schema (mirrors `SchemaIdentity`). */
export interface SchemaIdentity {
  name: string;
  hash: string;
  module?: string | null;
  moduleStatus?: string | null;
  moduleWarning?: string | null;
}

function schemaIdentityDict(identity: SchemaIdentity): Json {
  return dropNone({
    module: identity.module ?? null,
    name: identity.name,
    hash: identity.hash,
    module_status: identity.moduleStatus ?? null,
    module_warning: identity.moduleWarning ?? null,
  });
}

function schemaIdentityFlat(identity: SchemaIdentity, prefix: string): Json {
  return dropNone({
    [`${prefix}_module`]: identity.module ?? null,
    [`${prefix}_name`]: identity.name,
    [`${prefix}_hash`]: identity.hash,
    [`${prefix}_module_status`]: identity.moduleStatus ?? null,
    [`${prefix}_module_warning`]: identity.moduleWarning ?? null,
  });
}

/** Where an activity was defined (mirrors `ActivityDefinitionSource`). */
export interface DefinitionSource {
  kind?: "yaml" | "python" | "unknown";
  module?: string | null;
  export?: string | null;
  yamlProject?: string | null;
  yamlName?: string | null;
}

function definitionSourceDict(source?: DefinitionSource): Json {
  return dropNone({
    kind: source?.kind ?? "unknown",
    module: source?.module ?? null,
    export: source?.export ?? null,
    yaml_project: source?.yamlProject ?? null,
    yaml_name: source?.yamlName ?? null,
  });
}

/** Resolved provider params (the subset that participates in the manifest). */
export interface ProviderParams {
  model?: string | null;
  temperature?: number | null;
  max_tokens?: number | null;
  top_p?: number | null;
  top_k?: number | null;
  stop?: string[];
  seed?: number | null;
  timeout?: number | null;
  frequency_penalty?: number | null;
  presence_penalty?: number | null;
  thinking_budget?: number | null;
}

/** Mirrors `ProviderParams.to_dict` — drops null and empty `stop`; `timeout` is operational. */
function providerParamsToDict(
  params: ProviderParams,
  { includeOperational = true }: { includeOperational?: boolean } = {},
): Json {
  const payload: Json = {
    model: params.model ?? null,
    temperature: params.temperature ?? null,
    max_tokens: params.max_tokens ?? null,
    top_p: params.top_p ?? null,
    top_k: params.top_k ?? null,
    stop: params.stop ? [...params.stop] : [],
    seed: params.seed ?? null,
    timeout: params.timeout ?? null,
    frequency_penalty: params.frequency_penalty ?? null,
    presence_penalty: params.presence_penalty ?? null,
    thinking_budget: params.thinking_budget ?? null,
  };
  if (!includeOperational) {
    delete payload["timeout"];
  }
  const out: Json = {};
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined) {
      continue;
    }
    if (key === "stop" && Array.isArray(value) && value.length === 0) {
      continue;
    }
    out[key] = value;
  }
  return out;
}

/** Mirrors `ProviderParams.behavior_dict` — operational-excluded, minus model/temperature. */
function providerParamsBehaviorDict(params: ProviderParams): Json {
  const payload = providerParamsToDict(params, { includeOperational: false });
  delete payload["model"];
  delete payload["temperature"];
  return payload;
}

/** Inputs shared by the activity manifest and its execution manifest. */
export interface ActivityManifestSpec {
  activityName: string;
  inputSchema: SchemaIdentity;
  outputSchema: SchemaIdentity;
  promptRef: PromptRef;
  resolvedPromptVersion: string | null;
  /** The resolved provider model recorded as `provider_model`. */
  providerModel: string | null;
  /** Resolved provider params (drives `provider_params`/`behavior_dict`). */
  providerParams?: ProviderParams;
  hookName?: string | null;
  artifactInputs?: Json[];
}

export interface ActivityManifest {
  activityName: string;
  manifestHash: string;
  hookName: string | null;
}

/** Mirrors `build_activity_manifest` — the contract hash over the behavior base. */
export function buildActivityManifest(spec: ActivityManifestSpec): ActivityManifest {
  const hookName = spec.hookName ?? null;
  const behavior = providerParamsBehaviorDict(spec.providerParams ?? {});
  const artifactInputs = spec.artifactInputs ?? [];
  const base: Json = {
    activity_name: spec.activityName,
    input_schema_name: spec.inputSchema.name,
    input_schema_hash: spec.inputSchema.hash,
    output_schema_name: spec.outputSchema.name,
    output_schema_hash: spec.outputSchema.hash,
    prompt_ref: promptRefToDict(spec.promptRef),
    resolved_prompt_version: spec.resolvedPromptVersion,
    provider_model: spec.providerModel,
    ...(Object.keys(behavior).length > 0 ? { provider_params: behavior } : {}),
    hook_name: hookName,
    ...(artifactInputs.length > 0 ? { artifact_inputs: artifactInputs } : {}),
  };
  return { activityName: spec.activityName, manifestHash: hashOf(base), hookName };
}

export interface ActivityExecutionManifestSpec extends ActivityManifestSpec {
  definitionSource?: DefinitionSource;
  promptMessages: ChatMessage[];
  renderedMessages: ChatMessage[];
  providerModelSource?: string | null;
  startToCloseTimeoutSeconds?: number | null;
  validationAttempt: number;
  artifacts?: Json[];
}

/** Mirrors `build_activity_execution_manifest` — returns the `to_dict` payload (incl. `manifest_hash`). */
export function buildActivityExecutionManifest(spec: ActivityExecutionManifestSpec): Json {
  const activityManifest = buildActivityManifest(spec);
  const definitionSource = definitionSourceDict(spec.definitionSource);
  const providerParams = providerParamsToDict(spec.providerParams ?? {});
  const promptMessagesHash = messagesHash(spec.promptMessages);
  const renderedMessagesHash = messagesHash(spec.renderedMessages);
  // Top-level temperature derives from the resolved provider params (as Python
  // does), so it can never diverge from `provider_params.temperature`.
  const temperature = spec.providerParams?.temperature ?? null;
  const timeout = spec.startToCloseTimeoutSeconds ?? null;
  const hookName = activityManifest.hookName;
  const artifactInputs = spec.artifactInputs ?? [];
  const artifacts = spec.artifacts ?? [];

  const base: Json = {
    activity_name: spec.activityName,
    activity_manifest_hash: activityManifest.manifestHash,
    definition_source: definitionSource,
    ...schemaIdentityFlat(spec.inputSchema, "input_schema"),
    ...schemaIdentityFlat(spec.outputSchema, "output_schema"),
    prompt_ref: promptRefToDict(spec.promptRef),
    resolved_prompt_version: spec.resolvedPromptVersion,
    prompt_messages_hash: promptMessagesHash,
    rendered_messages_hash: renderedMessagesHash,
    provider_model: spec.providerModel,
    provider_model_source: spec.providerModelSource ?? null,
    temperature,
    ...(Object.keys(providerParams).length > 0 ? { provider_params: providerParams } : {}),
    start_to_close_timeout_seconds: timeout,
    hook_name: hookName,
    ...(artifactInputs.length > 0 ? { artifact_inputs: artifactInputs } : {}),
    ...(artifacts.length > 0 ? { artifacts } : {}),
    validation_attempt: spec.validationAttempt,
    manifest_version: "1",
  };
  const manifestHash = hashOf(base);

  return dropNone({
    manifest_version: "1",
    manifest_hash: manifestHash,
    activity_name: spec.activityName,
    activity_manifest_hash: activityManifest.manifestHash,
    definition_source: definitionSource,
    input_schema: schemaIdentityDict(spec.inputSchema),
    output_schema: schemaIdentityDict(spec.outputSchema),
    prompt_ref: promptRefToDict(spec.promptRef),
    resolved_prompt_version: spec.resolvedPromptVersion,
    prompt_messages_hash: promptMessagesHash,
    rendered_messages_hash: renderedMessagesHash,
    provider_model: spec.providerModel,
    provider_model_source: spec.providerModelSource ?? null,
    temperature,
    provider_params: Object.keys(providerParams).length > 0 ? providerParams : null,
    start_to_close_timeout_seconds: timeout,
    hook_name: hookName,
    artifact_inputs: artifactInputs.length > 0 ? artifactInputs : null,
    artifacts: artifacts.length > 0 ? artifacts : null,
    validation_attempt: spec.validationAttempt,
  });
}

/** A per-activity rollup entry inside a workflow manifest (`build_activity_rollup_entry` shape). */
export interface ActivityRollupSpec {
  activityName: string;
  definitionSource?: DefinitionSource;
  inputSchema: SchemaIdentity;
  outputSchema: SchemaIdentity;
  promptRef: PromptRef;
  resolvedPromptVersion: string | null;
  promptMessages: ChatMessage[];
  providerModel: string | null;
  providerModelSource?: string | null;
  providerParams?: ProviderParams;
  startToCloseTimeoutSeconds?: number | null;
  hookName?: string | null;
  artifactInputs?: Json[];
}

/** Mirrors `build_activity_rollup_entry` — the resolved rollup dict embedded in the workflow manifest. */
export function buildActivityRollupEntry(spec: ActivityRollupSpec): Json {
  const activityManifest = buildActivityManifest({
    activityName: spec.activityName,
    inputSchema: spec.inputSchema,
    outputSchema: spec.outputSchema,
    promptRef: spec.promptRef,
    resolvedPromptVersion: spec.resolvedPromptVersion,
    providerModel: spec.providerModel,
    ...(spec.providerParams !== undefined ? { providerParams: spec.providerParams } : {}),
    ...(spec.hookName !== undefined ? { hookName: spec.hookName } : {}),
    ...(spec.artifactInputs !== undefined ? { artifactInputs: spec.artifactInputs } : {}),
  });
  const providerParams = providerParamsToDict(spec.providerParams ?? {});
  const artifactInputs = spec.artifactInputs ?? [];
  return dropNone({
    activity_name: spec.activityName,
    activity_manifest_hash: activityManifest.manifestHash,
    definition_source: definitionSourceDict(spec.definitionSource),
    input_schema: schemaIdentityDict(spec.inputSchema),
    output_schema: schemaIdentityDict(spec.outputSchema),
    prompt_ref: promptRefToDict(spec.promptRef),
    resolved_prompt_version: spec.resolvedPromptVersion,
    prompt_messages_hash: messagesHash(spec.promptMessages),
    provider_model: spec.providerModel,
    provider_model_source: spec.providerModelSource ?? null,
    temperature: spec.providerParams?.temperature ?? null,
    provider_params: Object.keys(providerParams).length > 0 ? providerParams : null,
    start_to_close_timeout_seconds: spec.startToCloseTimeoutSeconds ?? null,
    hook_name: spec.hookName ?? null,
    artifact_inputs: artifactInputs.length > 0 ? artifactInputs : null,
  });
}

const ACTIVITY_CONTRACT_FIELDS = [
  "activity_name",
  "activity_manifest_hash",
  "definition_source",
  "input_schema",
  "output_schema",
  "prompt_ref",
  "resolved_prompt_version",
  "prompt_messages_hash",
  "provider_model",
  "temperature",
  "hook_name",
  "artifact_inputs",
] as const;

const PROVIDER_PARAM_CONTRACT_FIELDS = [
  "max_tokens",
  "top_p",
  "top_k",
  "stop",
  "seed",
  "frequency_penalty",
  "presence_penalty",
] as const;

/** Mirrors `_activity_contract_payload` — the contract-defining subset of a rollup entry. */
function activityContractPayload(activity: string | Json): string | Json {
  if (typeof activity === "string") {
    return activity;
  }
  const payload: Json = {};
  for (const fieldName of ACTIVITY_CONTRACT_FIELDS) {
    if (fieldName in activity && activity[fieldName] !== null && activity[fieldName] !== undefined) {
      payload[fieldName] = activity[fieldName];
    }
  }
  const providerParams = activity["provider_params"];
  if (providerParams && typeof providerParams === "object" && !Array.isArray(providerParams)) {
    const contractParams: Json = {};
    for (const fieldName of PROVIDER_PARAM_CONTRACT_FIELDS) {
      const value = (providerParams as Json)[fieldName];
      if (fieldName in providerParams && value !== null && value !== undefined) {
        contractParams[fieldName] = value;
      }
    }
    if (Object.keys(contractParams).length > 0) {
      payload["provider_params"] = contractParams;
    }
  }
  return payload;
}

export interface WorkflowExecutionManifestSpec {
  workflowName: string;
  workflowId: string;
  taskQueue: string;
  activities: (string | Json)[];
  /** Already-resolved code-provenance dict (the `CodeProvenance.to_dict()` shape). */
  codeProvenance: Json;
  mapSteps?: Json[];
  contributions?: Json;
  temporalRunId?: string | null;
  yamlProject?: string | null;
  yamlName?: string | null;
  sdkVersion?: string | null;
}

function workflowContractHash(spec: WorkflowExecutionManifestSpec): string {
  const mapSteps = spec.mapSteps ?? [];
  const base: Json = {
    contract_version: "1",
    workflow_name: spec.workflowName,
    activities: spec.activities.map(activityContractPayload),
    map_steps: mapSteps.length > 0 ? mapSteps : null,
    yaml_project: spec.yamlProject ?? null,
    yaml_name: spec.yamlName ?? null,
  };
  return hashOf(dropNone(base));
}

/** Mirrors `build_workflow_execution_manifest` — returns the `to_dict` payload (incl. both hashes). */
export function buildWorkflowExecutionManifest(spec: WorkflowExecutionManifestSpec): Json {
  const contractHash = workflowContractHash(spec);
  const mapSteps = spec.mapSteps ?? [];
  const contributions = spec.contributions && Object.keys(spec.contributions).length > 0
    ? spec.contributions
    : null;
  const base: Json = {
    manifest_version: "1",
    workflow_name: spec.workflowName,
    workflow_contract_hash: contractHash,
    workflow_id: spec.workflowId,
    temporal_run_id: spec.temporalRunId ?? null,
    task_queue: spec.taskQueue,
    activities: spec.activities,
    map_steps: mapSteps.length > 0 ? mapSteps : null,
    contributions,
    code_provenance: spec.codeProvenance,
    yaml_project: spec.yamlProject ?? null,
    yaml_name: spec.yamlName ?? null,
    sdk_version: spec.sdkVersion ?? null,
  };
  const manifestHash = hashOf(dropNone(base));

  return dropNone({
    manifest_version: "1",
    manifest_hash: manifestHash,
    workflow_contract_hash: contractHash,
    workflow_name: spec.workflowName,
    workflow_id: spec.workflowId,
    temporal_run_id: spec.temporalRunId ?? null,
    task_queue: spec.taskQueue,
    activities: spec.activities,
    map_steps: mapSteps.length > 0 ? mapSteps : null,
    contributions,
    code_provenance: spec.codeProvenance,
    yaml_project: spec.yamlProject ?? null,
    yaml_name: spec.yamlName ?? null,
    sdk_version: spec.sdkVersion ?? null,
  });
}
