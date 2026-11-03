import { buildWorkflowExecutionManifest, type ActivityDescriptor, type MetadataContributor } from "@typeflux/temporal";
import type { LocalContract } from "./local-contract.js";
import type { WorkflowPlanStep } from "./workflow-plan.js";

export interface LocalMetadataContext {
  correlationId: string;
  contract: LocalContract;
  descriptors: Record<string, ActivityDescriptor<any, any>>;
}

/** Only the execution shape belongs in the manifest, never implicit Temporal retry defaults. */
function shape(steps: readonly WorkflowPlanStep[]): Record<string, unknown>[] {
  return steps.map(step => ({
    id: step.id, kind: step.kind,
    ...(step.when === undefined ? {} : { when: step.when }),
    ...("activity" in step ? { activity: step.activity } : {}),
    ...("over" in step ? { over: step.over, concurrency: step.concurrency ?? 5,
      ...(step.collectField === undefined ? {} : { collect_field: step.collectField }) } : {}),
    ...(step.kind === "parallel" ? { branches: step.branches.map(branch => ({
      id: branch.id, ...(branch.when === undefined ? {} : { when: branch.when }), steps: shape(branch.steps),
    })) } : {}),
    ...("workflowId" in step ? { workflow: step.workflowId, steps: shape(step.plan.steps) } : {}),
  }));
}

export class LocalRuntimeContributor implements MetadataContributor<LocalMetadataContext> {
  contribute({correlationId, contract, descriptors}: LocalMetadataContext): Record<string, unknown> {
    const manifest = buildWorkflowExecutionManifest({
      workflowName: contract.spec.workflow.name,
      workflowId: correlationId,
      yamlProject: contract.spec.project,
      yamlName: contract.spec.name,
      codeProvenance: {},
      activities: Object.values(descriptors).map(descriptor => ({
        activity_name: descriptor.name,
        input_schema: {name: descriptor.inputSchemaName ?? "input", hash: descriptor.inputSchemaHash},
        output_schema: {name: descriptor.outputSchemaName ?? "output", hash: descriptor.outputSchemaHash},
      })),
      mapSteps: structuredClone(shape(contract.plan.steps)),
      contributions: {runtime: {engine: "local"}},
    });
    // Keep the manifest outside the legacy broad execution_manifest redaction exclusion:
    // predicates can contain user literals. Neither correlation ids nor predicates are tags.
    return {"typeflux.runtime": {engine: "local"}, "typeflux.local": {
      correlation_id: correlationId, workflow_name: contract.spec.workflow.name, manifest,
    }};
  }
}
