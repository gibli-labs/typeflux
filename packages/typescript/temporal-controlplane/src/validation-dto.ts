/**
 * The `/validate` response contract (governance parity, #563 slice 2; Python
 * `ProjectValidationReport` as the FastAPI `response_model` with `exclude_none`). The SDK's
 * `validateProjectBundle` returns an internal camelCase report; the control plane must emit the
 * snake_case API JSON the generated client / console expect — this module is that translation
 * layer (pure functions; optional fields are omitted when absent to mirror `response_model_exclude_none`).
 */

import type { ProjectResolvedWorkflowValidation, ProjectValidationIssue, ProjectValidationReport } from "@typeflux/temporal-yaml";

/** One check in a resolved-workflow validation (Python `ProjectValidationCheck`). */
export interface ApiValidationCheck {
  code: string;
  status: "passed" | "failed" | "skipped";
  message?: string;
  /** Always present — Python's model defaults `details` to `{}` and `exclude_none` keeps it. */
  details: Record<string, unknown>;
}

/** A structural/semantic project issue (Python `ProjectValidationIssue`). */
export interface ApiValidationIssue {
  code: string;
  message: string;
  reference?: string;
  path?: string;
}

/** A declared workflow's loaded summary (Python `ProjectWorkflowSummary`). */
export interface ApiValidationWorkflowSummary {
  id: string;
  path: string;
  yaml_project?: string;
  yaml_name?: string;
  workflow_name?: string;
  task_queue?: string;
}

/**
 * A resolved workflow's per-check validation (Python `ProjectResolvedWorkflowValidation`).
 * `workflow_path` / `environment_profile_path` carry the MANIFEST references on success —
 * Python fills the resolved absolute paths; the injection SDK holds references (#565).
 */
export interface ApiResolvedWorkflowValidation {
  workflow_id: string;
  environment_id: string;
  ok: boolean;
  yaml_project?: string;
  yaml_name?: string;
  workflow_name?: string;
  task_queue?: string;
  workflow_path?: string;
  environment_profile_path?: string;
  checks: ApiValidationCheck[];
}

/** The full validation report (Python `ProjectValidationReport`). */
export interface ApiProjectValidationReport {
  project_name: string;
  manifest_path: string;
  ok: boolean;
  issues: ApiValidationIssue[];
  workflows: ApiValidationWorkflowSummary[];
  resolved_workflows: ApiResolvedWorkflowValidation[];
}

/** Include `key: value` only when `value` is defined — mirrors `response_model_exclude_none`. */
const when = <K extends string, V>(key: K, value: V | undefined): Record<K, V> | Record<string, never> =>
  value !== undefined ? ({ [key]: value } as Record<K, V>) : {};

/** Map one internal validation check to its snake_case DTO (shared with the resolved-workflow bundle). */
export const mapCheck = (check: { code: string; status: "passed" | "failed" | "skipped"; message?: string; details?: Record<string, unknown> }): ApiValidationCheck => ({
  code: check.code,
  status: check.status,
  ...when("message", check.message),
  details: check.details ?? {}, // always present (Python default `{}`)
});

/** Map one internal validation issue to its snake_case DTO (shared with the resolved-workflow bundle). */
export const mapIssue = (issue: ProjectValidationIssue): ApiValidationIssue => ({
  code: issue.code,
  message: issue.message,
  ...when("reference", issue.reference),
  ...when("path", issue.path),
});

const mapResolved = (workflow: ProjectResolvedWorkflowValidation): ApiResolvedWorkflowValidation => ({
  workflow_id: workflow.workflowId,
  environment_id: workflow.environmentId,
  ok: workflow.ok,
  ...when("yaml_project", workflow.yamlProject),
  ...when("yaml_name", workflow.yamlName),
  ...when("workflow_name", workflow.workflowName),
  ...when("task_queue", workflow.taskQueue),
  ...when("workflow_path", workflow.workflowPath),
  ...when("environment_profile_path", workflow.environmentProfilePath),
  checks: workflow.checks.map(mapCheck),
});

/**
 * Translate the SDK's internal report into the snake_case API DTO. Only `manifest_path` is
 * control-plane-supplied; the `workflows` summary and the parse checks now come from the SDK
 * report itself (#565), so everything maps field-for-field from `validateProjectBundle`.
 */
export function toApiValidationReport(
  report: ProjectValidationReport,
  manifestPath: string,
): ApiProjectValidationReport {
  return {
    project_name: report.projectName,
    manifest_path: manifestPath,
    ok: report.ok,
    issues: report.issues.map(mapIssue),
    workflows: report.workflows.map((workflow) => ({
      id: workflow.workflowId,
      path: workflow.path,
      yaml_project: workflow.yamlProject,
      yaml_name: workflow.yamlName,
      workflow_name: workflow.workflowName,
      task_queue: workflow.taskQueue,
    })),
    resolved_workflows: report.resolvedWorkflows.map(mapResolved),
  };
}
