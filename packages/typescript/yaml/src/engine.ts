/** Static engine boundaries. No providers, activities or Temporal SDK are imported. */
import type { SubworkflowSpecResolver } from "./build-workflow.js";
import type { TypefluxYamlSpec, WorkflowStepSpec } from "./spec.js";

export class RuntimeCapabilityError extends Error {
  constructor(
    public readonly workflow: string,
    public readonly path: string,
    public readonly feature: string,
    guidance: string,
  ) {
    super(`workflow ${JSON.stringify(workflow)}: ${feature} at ${path}. ${guidance}`);
    this.name = "RuntimeCapabilityError";
  }
}

export function runtimeEngine(spec: TypefluxYamlSpec): "local" | "temporal" {
  return spec.runtime.engine ?? "temporal";
}

/** Guard an existing Temporal-only surface and return its required queue. */
export function requireTemporalEngine(spec: TypefluxYamlSpec): string {
  if (runtimeEngine(spec) !== "temporal") {
    throw new RuntimeCapabilityError(spec.workflow.name, "runtime.engine",
      "local engine is not supported by this Temporal entry point",
      "Use runtime.engine: temporal and configure task_queue to use this entry point. " +
      "Local execution is not available through Temporal workers or control-plane APIs.");
  }
  if (spec.task_queue === undefined) {
    throw new RuntimeCapabilityError(spec.workflow.name, "task_queue", "Temporal task queue is missing",
      "Configure task_queue for runtime.engine: temporal.");
  }
  return spec.task_queue;
}

/**
 * Check every reachable YAML workflow, including conditional branches. Returns visited IDs.
 * This neither executes nor attests graph types, project policy compliance, or capabilities
 * of injected activity descriptors. The resolver retains its existing trusted-code boundary.
 */
export function validateLocalCapabilities(
  spec: TypefluxYamlSpec,
  subworkflows?: SubworkflowSpecResolver,
): readonly string[] {
  return walkWorkflowEngines(spec, subworkflows, true);
}

/** Check the complete Temporal closure before constructing any runtime services. */
export function requireTemporalWorkflowGraph(spec: TypefluxYamlSpec, subworkflows?: SubworkflowSpecResolver): void {
  walkWorkflowEngines(spec, subworkflows, false);
}

function walkWorkflowEngines(
  spec: TypefluxYamlSpec,
  subworkflows: SubworkflowSpecResolver | undefined,
  local: boolean,
): readonly string[] {
  const visited = new Set<string>();
  const labels: string[] = [];
  const active = new Set<string>();
  const activeSpecs = new Set<TypefluxYamlSpec>();
  function reject(current: TypefluxYamlSpec, path: string, feature: string): never {
    const guidance = feature.includes("reference") || feature.includes("resolver")
      ? "Fix the project workflow reference; local and Temporal graphs both require valid same-project references."
      : "Use runtime.engine: temporal for this capability; local execution does not provide it.";
    throw new RuntimeCapabilityError(current.workflow.name, path, feature, guidance);
  }
  function visit(current: TypefluxYamlSpec, key: string | undefined): void {
    if (local) {
      if (runtimeEngine(current) !== "local") {
        throw new RuntimeCapabilityError(current.workflow.name, "runtime.engine",
          "local graph requires local engine on every workflow",
          "Use runtime.engine: local on every reachable workflow and remove Temporal-only settings.");
      }
      if (current.task_queue !== undefined) reject(current, "task_queue", "Temporal task queue");
      // Empty/default connection objects carry no configured behavior. Explicit defaults do.
      for (const name of Object.keys(current.runtime.temporal).sort()) {
        reject(current, `runtime.temporal.${name}`, "Temporal connection configuration");
      }
      if (current.runtime.activity_retry !== undefined) {
        reject(current, "runtime.activity_retry", "Temporal activity retry policy");
      }
      if (current.workflow.lifecycle !== undefined) {
        reject(current, "workflow.lifecycle", "workflow lifecycle/review control");
      }
      if (current.workflow.risk_tier !== undefined && current.workflow.risk_tier !== "safe") {
        reject(current, "workflow.risk_tier", "risk-tier enforcement requires a policy-aware runtime");
      }
      for (const [index, definition] of (current.activities.definitions ?? []).entries()) {
        for (const name of ["retry", "start_to_close_timeout_seconds", "heartbeat_timeout_seconds"] as const) {
          if (definition[name] !== undefined) {
            reject(current, `activities.definitions[${index}].${name}`, "Temporal activity policy");
          }
        }
      }
    } else {
      requireTemporalEngine(current);
    }
    labels.push(key ?? current.name);
    if (key !== undefined) { visited.add(key); active.add(key); }
    activeSpecs.add(current);
    function walk(steps: WorkflowStepSpec[], prefix: string): void {
      for (const [index, step] of steps.entries()) {
        const path = `${prefix}[${index}]`;
        if (local && step.compensate !== undefined) {
          reject(current, `${path}.compensate`, "compensation");
        }
        let reference: string | undefined;
        let referencePath = `${path}.workflow`;
        if (step.workflow !== undefined) reference = step.workflow;
        else if (step.map !== undefined) {
          reference = step.map.workflow;
          referencePath = `${path}.map.workflow`;
        } else if (step.parallel !== undefined) {
          for (const [branchIndex, branch] of step.parallel.branches.entries()) {
            walk(branch.steps, `${path}.parallel.branches[${branchIndex}].steps`);
          }
        }
        if (reference === undefined) continue;
        if (active.has(reference)) reject(current, referencePath, "cyclic workflow reference");
        if (visited.has(reference)) continue;
        if (subworkflows === undefined) reject(current, referencePath, "workflow reference requires a project resolver");
        const child = subworkflows.specFor(reference);
        if (child === undefined) reject(current, referencePath, "workflow reference is not declared in the project");
        if (child.project !== spec.project) reject(current, referencePath, "cross-project workflow reference");
        if (activeSpecs.has(child)) reject(current, referencePath, "cyclic workflow reference");
        visit(child, reference);
      }
    }
    walk(current.workflow.steps, "workflow.steps");
    if (key !== undefined) active.delete(key);
    activeSpecs.delete(current);
  }
  visit(spec, subworkflows?.selfId);
  return labels;
}
