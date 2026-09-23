/**
 * Workflow starter (parity Epic 3, #450; Python `execute_workflow`) — thin wrappers
 * over an `@temporalio/client` `Client` to start a workflow and either await its
 * result (`executeWorkflow`) or return a handle (`startWorkflow`). The option
 * assembly is the pure, unit-testable `workflowStartOptions`; the client calls
 * themselves connect to a live server.
 */

import type { Client, WorkflowHandleWithFirstExecutionRunId, WorkflowStartOptions } from "@temporalio/client";
import {
  defineSearchAttributeKey,
  SearchAttributeType,
  TypedSearchAttributes,
  type SearchAttributePair,
} from "@temporalio/common";

export interface ExecuteWorkflowOptions {
  /** The registered workflow type (its function name). */
  workflowType: string;
  /** The task queue the workflow runs on (required). */
  taskQueue: string;
  /** A unique, caller-chosen workflow id for idempotency (required). */
  workflowId: string;
  /** Positional arguments passed to the workflow (defaults to `[]`). */
  args?: unknown[];
  /**
   * KEYWORD search attributes set on the start (#495; Python
   * `_workflow_start_search_attributes`): caller-supplied pairs in
   * `startOptions.typedSearchAttributes` are preserved, but a key configured HERE is
   * authoritative on conflict. Each attribute must be REGISTERED in the Temporal
   * namespace before use (`temporal operator search-attribute create --name … --type Keyword`).
   */
  keywordSearchAttributes?: Record<string, string>;
  /**
   * KEYWORD_LIST search attributes set on the start (#715 slice 1; Python parity —
   * `TypefluxSubjectIds`). Same authoritative-on-conflict / caller-preservation rule
   * as `keywordSearchAttributes`. Each must be REGISTERED in the namespace as a
   * KeywordList (`temporal operator search-attribute create --name … --type KeywordList`).
   */
  keywordListSearchAttributes?: Record<string, string[]>;
  /** Passthrough of additional `@temporalio` start options (id-reuse policy, …). */
  startOptions?: Partial<Omit<WorkflowStartOptions, "taskQueue" | "workflowId" | "args">>;
}

/**
 * Assemble the `@temporalio` `WorkflowStartOptions` (pure — no server). Throws if
 * `taskQueue` or `workflowId` is missing; `args` defaults to `[]`.
 */
export function workflowStartOptions(options: ExecuteWorkflowOptions): WorkflowStartOptions {
  if (!options.taskQueue) {
    throw new Error("executeWorkflow: taskQueue is required");
  }
  if (!options.workflowId) {
    throw new Error("executeWorkflow: workflowId is required");
  }
  const assembled: WorkflowStartOptions = {
    ...options.startOptions,
    taskQueue: options.taskQueue,
    workflowId: options.workflowId,
    args: options.args ?? [],
  };
  const configuredKeyword = options.keywordSearchAttributes ?? {};
  const configuredKeywordList = options.keywordListSearchAttributes ?? {};
  const configuredNames = new Set([
    ...Object.keys(configuredKeyword),
    ...Object.keys(configuredKeywordList),
  ]);
  if (configuredNames.size > 0) {
    const pairs: SearchAttributePair[] = [];
    for (const [name, value] of Object.entries(configuredKeyword)) {
      pairs.push({ key: defineSearchAttributeKey(name, SearchAttributeType.KEYWORD), value });
    }
    for (const [name, value] of Object.entries(configuredKeywordList)) {
      pairs.push({ key: defineSearchAttributeKey(name, SearchAttributeType.KEYWORD_LIST), value });
    }
    // Preserve caller pairs; the configured keys are authoritative (Python parity).
    const existing = options.startOptions?.typedSearchAttributes;
    const existingPairs: SearchAttributePair[] =
      existing instanceof TypedSearchAttributes ? existing.getAll() : (existing ?? []);
    for (const pair of existingPairs) {
      if (!configuredNames.has(pair.key.name)) {
        pairs.push(pair);
      }
    }
    assembled.typedSearchAttributes = new TypedSearchAttributes(pairs);
  }
  return assembled;
}

/** Start a workflow and await its result. */
export function executeWorkflow(client: Client, options: ExecuteWorkflowOptions): Promise<unknown> {
  return client.workflow.execute(options.workflowType, workflowStartOptions(options));
}

/** Start a workflow and return its handle (does not await the result). The
 * handle carries `firstExecutionRunId` — what `client.workflow.start` actually
 * returns; callers correlate traces with it. */
export function startWorkflow(
  client: Client,
  options: ExecuteWorkflowOptions,
): Promise<WorkflowHandleWithFirstExecutionRunId> {
  return client.workflow.start(options.workflowType, workflowStartOptions(options));
}
