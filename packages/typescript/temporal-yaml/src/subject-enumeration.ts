/**
 * The subject enumeration seam (#715 slice 1; Python
 * `project.runs.list_executions_for_subject`).
 *
 * A thin visibility query over the `TypefluxSubjectIds` keyword-list search
 * attribute — the same `workflow.list` mechanism the frozen-version scan uses. THE
 * erasure enumeration seam: the slice-4 delete driver and the slice-5 plan both walk
 * it; it performs NO mutation. Each ref carries the row's own subject set
 * (`subjectIds`, `null` when unreadable — consumers exclude fail-safe) and a
 * status classification computed against the SDK's known status-name set
 * (`isRunning`/`isClosed`; both false = absent/unrecognized status, which consumers
 * must treat fail-safe). The result flags an honest `truncated` when the limit
 * stopped the walk. The caller supplies a connected `@temporalio/client` `Client`
 * (the erase op owns the connection).
 */

import { defineSearchAttributeKey, SearchAttributeType } from "@temporalio/common";

import { SUBJECT_IDS_SEARCH_ATTRIBUTE, subjectIndexQuery } from "./subjects.js";

/** The SDK's CLOSED execution-status names (`WorkflowExecutionStatusName` minus
 * RUNNING/UNSPECIFIED/PAUSED/UNKNOWN). Membership — never a truthiness/string-default
 * trick — decides closed-ness; anything unrecognized is neither running nor closed. */
const CLOSED_STATUS_NAMES: ReadonlySet<string> = new Set([
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "TERMINATED",
  "CONTINUED_AS_NEW",
  "TIMED_OUT",
]);

/** One execution the subject index attributes to a subject. */
export interface SubjectExecutionRef {
  readonly executionId: string;
  readonly runId?: string;
  readonly workflowType?: string;
  readonly status?: string;
  /** Name-set classification (both false = unknown status; treat fail-safe). */
  readonly isRunning: boolean;
  readonly isClosed: boolean;
  /** The row's OWN indexed subject set, or `null` when unreadable (fail-safe exclude). */
  readonly subjectIds: readonly string[] | null;
  readonly startTime?: string;
  readonly closeTime?: string;
}

/** The result of a subject enumeration: refs plus an honest truncation flag. */
export interface SubjectExecutionEnumeration {
  readonly executions: readonly SubjectExecutionRef[];
  /** True when `limit` stopped the walk with more matches possibly remaining. */
  readonly truncated: boolean;
}

/** One listing row (the structural slice `@temporalio/client` `workflow.list` yields). */
export interface SubjectExecutionListItem {
  workflowId?: string;
  runId?: string;
  type?: { name?: string } | string | undefined;
  status?: { name?: string } | string | undefined;
  startTime?: Date | string | undefined;
  closeTime?: Date | string | undefined;
  /** The typed search-attribute surface (preferred; real SDK `TypedSearchAttributes`). */
  typedSearchAttributes?: { get(key: unknown): unknown } | undefined;
  /** The legacy record surface (`{ TypefluxSubjectIds: [...] }`); fallback. */
  searchAttributes?: Record<string, unknown> | null | undefined;
}

/** The visibility slice of a client this seam needs (`@temporalio/client` shape). */
export interface SubjectListClient {
  workflow: {
    list(options: { query: string }): AsyncIterable<SubjectExecutionListItem>;
  };
}

function typeName(type: { name?: string } | string | undefined): string | undefined {
  if (type === undefined) return undefined;
  return typeof type === "string" ? type : type.name;
}

function statusName(status: { name?: string } | string | undefined): string | undefined {
  if (status === undefined) return undefined;
  return typeof status === "string" ? status : status.name;
}

function isoOrUndefined(value: Date | string | undefined): string | undefined {
  if (value === undefined) return undefined;
  return value instanceof Date ? value.toISOString() : value;
}

/**
 * Read the row's `TypefluxSubjectIds` set, or `null` when unreadable: missing
 * attributes, a non-array value (a bare string is NOT a subject list), or ANY error
 * from the typed-attribute surface. Wrapped per row so one undecodable row degrades to
 * `null` (excluded fail-safe by consumers) instead of aborting the enumeration.
 */
function readSubjectIds(item: SubjectExecutionListItem): readonly string[] | null {
  try {
    const typed = item.typedSearchAttributes;
    if (typed !== undefined && typed !== null) {
      const key = defineSearchAttributeKey(SUBJECT_IDS_SEARCH_ATTRIBUTE, SearchAttributeType.KEYWORD_LIST);
      const value = typed.get(key);
      if (Array.isArray(value)) return value.map((entry) => String(entry));
      // Fall through to the legacy record — an absent typed value is not proof the
      // legacy surface lacks it (structural fakes may carry either).
    }
    const raw = item.searchAttributes?.[SUBJECT_IDS_SEARCH_ATTRIBUTE];
    if (!Array.isArray(raw)) return null;
    return raw.map((entry) => String(entry));
  } catch {
    return null;
  }
}

/**
 * Enumerate every execution the subject index attributes to `subjectId`, via the
 * `TypefluxSubjectIds = '<id>'` (keyword-list membership) visibility query.
 */
export async function listExecutionsForSubject(
  client: SubjectListClient,
  subjectId: string,
  options: { limit?: number } = {},
): Promise<SubjectExecutionEnumeration> {
  const limit = options.limit ?? 1000;
  const refs: SubjectExecutionRef[] = [];
  let truncated = false;
  for await (const execution of client.workflow.list({ query: subjectIndexQuery(subjectId) })) {
    if (refs.length >= limit) {
      truncated = true;
      break;
    }
    const workflowType = typeName(execution.type);
    const status = statusName(execution.status);
    const startTime = isoOrUndefined(execution.startTime);
    const closeTime = isoOrUndefined(execution.closeTime);
    const isRunning = status === "RUNNING";
    const isClosed = status !== undefined && CLOSED_STATUS_NAMES.has(status);
    refs.push({
      executionId: execution.workflowId ?? "",
      ...(execution.runId !== undefined ? { runId: execution.runId } : {}),
      ...(workflowType !== undefined ? { workflowType } : {}),
      ...(status !== undefined ? { status } : {}),
      isRunning,
      isClosed,
      subjectIds: readSubjectIds(execution),
      ...(startTime !== undefined ? { startTime } : {}),
      ...(closeTime !== undefined ? { closeTime } : {}),
    });
  }
  return { executions: refs, truncated };
}
