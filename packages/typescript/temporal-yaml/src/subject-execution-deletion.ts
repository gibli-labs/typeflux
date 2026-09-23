/**
 * Subject-scoped `DeleteWorkflowExecution` driver (#715 slice 4, option iii) —
 * behavioral parity with the Python `project/erase_executions.py` driver.
 *
 * The crypto-shred keystore (`subject-keystore.ts`) makes a subject's Temporal history
 * permanently UNREADABLE; this driver is the complement that REMOVES the history outright
 * for CLOSED subject-dedicated executions via `DeleteWorkflowExecution` (deletes the whole
 * execution's history + visibility record; async, closed-execution-oriented).
 *
 * `DeleteWorkflowExecution` is BLUNT — it deletes the entire execution, every subject on
 * it — so it is applied ONLY where the `TypefluxSubjectIds` index (slice 1) confirms the
 * execution's subject set is EXACTLY the target subject (subject-dedicated). Everything
 * else is excluded FAIL-SAFE and reported by category (multi-subject by COUNT, never the
 * other subjects' ids — matching the slice-2 conflicted-trace convention). Running
 * executions are reported, never touched (termination is slice 5 / CLI territory), and an
 * execution whose status is absent/unrecognized is excluded too (never deleted on a
 * status guess).
 *
 * Enumeration goes through the slice-1 seam (`listExecutionsForSubject`) — one
 * query/limit/status/subject-extraction implementation, not a drifting copy.
 *
 * Dry-run defaults ON: reports what it WOULD delete and mutates nothing unless
 * `dryRun: false`. The report is audit-honest: the `deletable` PLAN set, the
 * authoritative `deleted` outcome, conflicted-excluded by category, still-running, the
 * index-coverage caveat, and any enumeration-truncation warning. Absent optional fields
 * are OMITTED from the report (Python `exclude_none` parity).
 */

import { listExecutionsForSubject, type SubjectExecutionRef, type SubjectListClient } from "./subject-enumeration.js";

/** The audit-honesty caveat every execution-deletion report carries (#715 slice 4). */
export const SUBJECT_EXECUTION_INDEX_COVERAGE =
  "Only executions stamped with the TypefluxSubjectIds index (emitted since #715 slice 1) " +
  "are visible to this enumeration; executions started before subject plumbing, or that " +
  "touched this subject's data through a path that declared no subject, carry no index " +
  "entry and are invisible here. This report is complete for post-slice-1 executions only " +
  "— not proof that no older executions exist.";

/** One execution id/run id — a plan entry (`deletable`) or an outcome entry (`deleted`). */
export interface DeletedExecutionRef {
  readonly executionId: string;
  readonly runId?: string;
  readonly status?: string;
}

/**
 * A matched execution EXCLUDED from deletion, fail-safe. `reason`:
 * - `"multi_subject"` — its `TypefluxSubjectIds` set carries OTHER subjects (deleting it
 *   would destroy their history);
 * - `"unreadable_subjects"` — the set could not be read, so subject-dedicated is
 *   unanswerable;
 * - `"stale_index"` — the set WAS readable but does not contain the target subject (the
 *   index matched a row its own attributes disown) — split from unreadable so an
 *   index-integrity bug is never masked as a data-access problem;
 * - `"unknown_status"` — the status was absent/unrecognized; never deleted on a guess.
 *
 * `otherSubjectCount` is the COUNT of other subjects, never their ids (a per-subject
 * report must not become a subject directory); present only for `multi_subject`.
 */
export interface SubjectExecutionConflict {
  readonly executionId: string;
  readonly runId?: string;
  readonly reason: "multi_subject" | "unreadable_subjects" | "stale_index" | "unknown_status";
  readonly otherSubjectCount?: number;
}

/** One execution the delete call could not remove — id plus the failure reason. */
export interface SubjectExecutionDeletionFailure {
  readonly executionId: string;
  readonly runId?: string;
  readonly reason: string;
}

/**
 * The outcome of a subject-scoped execution deletion — ids/counts only, never PII.
 *
 * `deletable` is the PLAN: the closed subject-DEDICATED executions the enumeration
 * identified — on a dry run, exactly what an execute WOULD delete; on an executed report
 * it remains the plan set (attempts), NOT the outcome. The authoritative outcome is
 * `deleted` (the executions actually removed) / `deletedCount` plus `failures` — a
 * consumer must never read `deletable.length` as an erasure result.
 */
export interface SubjectExecutionDeletionReport {
  readonly subjectId: string;
  readonly dryRun: boolean;
  readonly namespace: string;
  readonly executionsMatched: number;
  /** The PLAN set (closed subject-dedicated executions); NOT the outcome. */
  readonly deletable: readonly DeletedExecutionRef[];
  /** The authoritative outcome: executions actually removed. */
  readonly deleted: readonly DeletedExecutionRef[];
  readonly deletedCount: number;
  readonly conflicted: readonly SubjectExecutionConflict[];
  /** Running executions — REPORTED but never touched (termination is slice 5). */
  readonly stillRunning: readonly DeletedExecutionRef[];
  readonly failures: readonly SubjectExecutionDeletionFailure[];
  readonly indexCoverage: string;
  readonly warnings: readonly string[];
}

/** The client slice this driver needs (structural — tests fake it). */
export interface SubjectDeletionClient extends SubjectListClient {
  workflowService: {
    deleteWorkflowExecution(request: {
      namespace: string;
      workflowExecution: { workflowId: string; runId?: string };
    }): Promise<unknown>;
  };
  /** Where the connected client's namespace lives (`@temporalio/client` Client.options). */
  readonly options?: { namespace?: string };
}

interface DeleteExecutionsOptions {
  readonly namespace?: string;
  readonly dryRun?: boolean;
  readonly limit?: number;
}

type Classification =
  | { kind: "running" }
  | { kind: "deletable" }
  | { kind: "conflicted"; conflict: SubjectExecutionConflict };

function classify(ref: SubjectExecutionRef, subjectId: string): Classification {
  const identity = {
    executionId: ref.executionId,
    ...(ref.runId !== undefined ? { runId: ref.runId } : {}),
  };
  if (ref.isRunning) return { kind: "running" };
  if (!ref.isClosed) {
    // Neither running nor provably closed — never delete on a status guess.
    return { kind: "conflicted", conflict: { ...identity, reason: "unknown_status" } };
  }
  if (ref.subjectIds === null) {
    return { kind: "conflicted", conflict: { ...identity, reason: "unreadable_subjects" } };
  }
  if (!ref.subjectIds.includes(subjectId)) {
    // Readable set that disowns the target: an index-integrity signal, not a
    // data-access one.
    return { kind: "conflicted", conflict: { ...identity, reason: "stale_index" } };
  }
  if (ref.subjectIds.length !== 1) {
    // Multi-subject: deleting the whole execution would destroy OTHER subjects'
    // history. Excluded; report the COUNT of others, never their ids.
    return {
      kind: "conflicted",
      conflict: { ...identity, reason: "multi_subject", otherSubjectCount: ref.subjectIds.length - 1 },
    };
  }
  return { kind: "deletable" };
}

/**
 * Delete CLOSED subject-DEDICATED executions for `subjectId` (dry-run by default).
 * Enumerates via the slice-1 `TypefluxSubjectIds` seam, classifies each execution, and —
 * only when `dryRun` is false — calls `DeleteWorkflowExecution` for the closed
 * subject-dedicated ones. Everything not provably closed AND subject-dedicated is
 * excluded fail-safe and reported (counts only); running executions are reported, never
 * touched.
 */
export async function deleteExecutionsForSubject(
  client: SubjectDeletionClient,
  subjectId: string,
  options: DeleteExecutionsOptions = {},
): Promise<SubjectExecutionDeletionReport> {
  if (subjectId.length === 0 || subjectId.trim() !== subjectId) {
    // Fail closed: an empty/untrimmed subject would enumerate the wrong set.
    throw new Error("deleteExecutionsForSubject requires a non-empty, trimmed subject id");
  }
  const namespace = options.namespace ?? client.options?.namespace;
  if (namespace === undefined || namespace.length === 0) {
    // Never issue a namespace-less delete — fail closed rather than guess.
    throw new Error(
      "deleteExecutionsForSubject could not resolve a namespace (pass namespace or a client exposing options.namespace)",
    );
  }
  const dryRun = options.dryRun ?? true;
  const limit = options.limit ?? 1000;

  const enumeration = await listExecutionsForSubject(client, subjectId, { limit });
  const deletable: DeletedExecutionRef[] = [];
  const conflicted: SubjectExecutionConflict[] = [];
  const stillRunning: DeletedExecutionRef[] = [];
  const warnings: string[] = [];

  for (const ref of enumeration.executions) {
    const asDeletedRef: DeletedExecutionRef = {
      executionId: ref.executionId,
      ...(ref.runId !== undefined ? { runId: ref.runId } : {}),
      ...(ref.status !== undefined ? { status: ref.status } : {}),
    };
    const classification = classify(ref, subjectId);
    if (classification.kind === "running") {
      stillRunning.push(asDeletedRef);
    } else if (classification.kind === "deletable") {
      deletable.push(asDeletedRef);
    } else {
      conflicted.push(classification.conflict);
    }
  }

  if (enumeration.truncated) {
    warnings.push(
      `execution enumeration for '${subjectId}' hit the limit (${limit}); more matching ` +
        "executions may remain. Raise the limit or rerun to reach the rest — the deletion " +
        "covers only the executions enumerated.",
    );
  }

  if (dryRun) {
    return {
      subjectId,
      dryRun: true,
      namespace,
      executionsMatched: enumeration.executions.length,
      deletable,
      deleted: [],
      deletedCount: 0,
      conflicted,
      stillRunning,
      failures: [],
      indexCoverage: SUBJECT_EXECUTION_INDEX_COVERAGE,
      warnings,
    };
  }

  const deleted: DeletedExecutionRef[] = [];
  const failures: SubjectExecutionDeletionFailure[] = [];
  // Deletes run sequentially: subject-dedicated sets are small in practice and the
  // server-side delete is itself asynchronous; bounded fan-out (the
  // PLAN_PR_LOOKUP_CONCURRENCY pattern) is deferred until a real large-scale need.
  for (const ref of deletable) {
    try {
      await client.workflowService.deleteWorkflowExecution({
        namespace,
        workflowExecution: {
          workflowId: ref.executionId,
          ...(ref.runId !== undefined ? { runId: ref.runId } : {}),
        },
      });
      deleted.push(ref);
    } catch (error) {
      const reason = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      failures.push({
        executionId: ref.executionId,
        ...(ref.runId !== undefined ? { runId: ref.runId } : {}),
        reason,
      });
    }
  }
  return {
    subjectId,
    dryRun: false,
    namespace,
    executionsMatched: enumeration.executions.length,
    deletable,
    deleted,
    deletedCount: deleted.length,
    conflicted,
    stillRunning,
    failures,
    indexCoverage: SUBJECT_EXECUTION_INDEX_COVERAGE,
    warnings,
  };
}
