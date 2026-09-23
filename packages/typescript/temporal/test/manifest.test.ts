import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { ChatMessage, SchemaIdentity } from "../src/index.js";
import {
  buildActivityExecutionManifest,
  buildActivityRollupEntry,
  buildWorkflowExecutionManifest,
  schemaHash,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const contractsDir = resolve(here, "../../../../contracts");

function golden(name: string): Record<string, unknown> {
  return JSON.parse(
    readFileSync(resolve(contractsDir, `manifest/golden/${name}`), "utf-8"),
  ) as Record<string, unknown>;
}

// Schema identity pinned to match the Python golden's `schema_identity(_Input/_Output)`.
const inputSchema: SchemaIdentity = {
  module: "typeflux_temporal_contracts_golden",
  name: "_Input",
  hash: schemaHash(golden("input_schema.json")),
};
const outputSchema: SchemaIdentity = {
  module: "typeflux_temporal_contracts_golden",
  name: "_Output",
  hash: schemaHash(golden("output_schema.json")),
};
const promptRef = { name: "support/classify", label: "production" };
const promptMessages: ChatMessage[] = [{ role: "user", content: "Classify: {{ text }}" }];

describe("full manifest builder (#425)", () => {
  it("reproduces the activity execution manifest golden byte-for-byte", () => {
    const manifest = buildActivityExecutionManifest({
      activityName: "classify_ticket",
      inputSchema,
      outputSchema,
      promptRef,
      resolvedPromptVersion: "commit-abc123",
      promptMessages,
      renderedMessages: [{ role: "user", content: "Classify: hello" }],
      providerModel: "fake-model",
      providerModelSource: "prompt_config",
      providerParams: { model: "fake-model" },
      validationAttempt: 0,
    });
    expect(manifest).toEqual(golden("activity_execution.json"));
  });

  it("emits the full definition_source and folds it into the hash (#425 review)", () => {
    const base = {
      activityName: "classify_ticket",
      inputSchema,
      outputSchema,
      promptRef,
      resolvedPromptVersion: "commit-abc123",
      promptMessages,
      renderedMessages: promptMessages,
      providerModel: "fake-model",
      providerParams: { model: "fake-model" },
      validationAttempt: 0,
    };
    const yaml = buildActivityExecutionManifest({
      ...base,
      definitionSource: { kind: "yaml", module: "pkg.acts", yamlProject: "proj", yamlName: "classify" },
    });
    // The full (drop-null'd) definition source is emitted, not just the kind.
    expect(yaml["definition_source"]).toEqual({
      kind: "yaml",
      module: "pkg.acts",
      yaml_project: "proj",
      yaml_name: "classify",
    });
    // definition_source participates in the hash: a different module => different manifest_hash.
    const other = buildActivityExecutionManifest({
      ...base,
      definitionSource: { kind: "yaml", module: "pkg.other", yamlProject: "proj", yamlName: "classify" },
    });
    expect(yaml["manifest_hash"]).not.toBe(other["manifest_hash"]);
  });

  it("derives top-level temperature from providerParams (#425 review)", () => {
    const manifest = buildActivityExecutionManifest({
      activityName: "classify_ticket",
      inputSchema,
      outputSchema,
      promptRef,
      resolvedPromptVersion: "commit-abc123",
      promptMessages,
      renderedMessages: promptMessages,
      providerModel: "fake-model",
      providerParams: { model: "fake-model", temperature: 0.5 },
      validationAttempt: 0,
    });
    expect(manifest["temperature"]).toBe(0.5);
    expect((manifest["provider_params"] as Record<string, unknown>)["temperature"]).toBe(0.5);
  });

  it("reproduces the workflow execution manifest golden byte-for-byte", () => {
    const rollup = buildActivityRollupEntry({
      activityName: "classify_ticket",
      inputSchema,
      outputSchema,
      promptRef,
      resolvedPromptVersion: "commit-abc123",
      promptMessages,
      providerModel: "fake-model",
      providerModelSource: "prompt_config",
      providerParams: { model: "fake-model", max_tokens: 4096 },
    });
    const manifest = buildWorkflowExecutionManifest({
      workflowName: "SupportTriageWorkflow",
      workflowId: "wf-golden",
      taskQueue: "support-ai",
      activities: [rollup],
      codeProvenance: {
        available: true,
        source: "golden",
        git_ref: "refs/heads/main",
        git_sha: "0000000000000000000000000000000000000000",
        dirty: false,
        deployment_id: "golden-deployment",
        environment: "golden",
        package_version: "0.0.0-golden",
      },
      sdkVersion: "0.0.0-golden",
    });
    expect(manifest).toEqual(golden("workflow_execution.json"));
  });
});
