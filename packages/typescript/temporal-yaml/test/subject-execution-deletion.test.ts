// Subject-scoped DeleteWorkflowExecution driver (#715 slice 4) — parity with the Python
// project/erase_executions.py suite. Dry-run / execute / conflict (multi-subject,
// stale-index, unknown-status, unreadable-subjects) / running paths, driven by a fake
// client. No live server. Also pins the omit-absent-keys report shape (Python
// exclude_none parity) and the `deleted` outcome vs `deletable` plan split.

import { describe, expect, it } from "vitest";

import { SUBJECT_IDS_SEARCH_ATTRIBUTE } from "../src/index.js";
import type { SubjectExecutionListItem } from "../src/subject-enumeration.js";
import {
  deleteExecutionsForSubject,
  type SubjectDeletionClient,
} from "../src/subject-execution-deletion.js";

interface Exec {
  id: string;
  status?: string;
  /** null → unreadable search attributes; a non-array leaks through verbatim. */
  subjects: unknown;
  runId?: string | undefined;
  omitSearchAttributes?: boolean;
  typedRaises?: boolean;
}

function listItem(exec: Exec): SubjectExecutionListItem {
  return {
    workflowId: exec.id,
    ...(exec.runId !== undefined ? { runId: exec.runId } : {}),
    ...(exec.status !== undefined ? { status: { name: exec.status } } : {}),
    ...(exec.typedRaises
      ? {
          typedSearchAttributes: {
            get: () => {
              throw new Error("decode broke");
            },
          },
        }
      : {}),
    ...(exec.omitSearchAttributes
      ? {}
      : {
          searchAttributes:
            exec.subjects === null ? {} : { [SUBJECT_IDS_SEARCH_ATTRIBUTE]: exec.subjects },
        }),
  };
}

class FakeClient implements SubjectDeletionClient {
  deleted: Array<{ workflowId: string; runId?: string }> = [];
  raiseFor = new Set<string>();
  lastQuery = "";
  readonly options: { namespace?: string };

  constructor(
    private readonly execs: Exec[],
    namespace = "ns",
  ) {
    this.options = namespace.length > 0 ? { namespace } : {};
  }

  workflow = {
    list: (options: { query: string }): AsyncIterable<SubjectExecutionListItem> => {
      this.lastQuery = options.query;
      const execs = this.execs;
      return {
        async *[Symbol.asyncIterator]() {
          for (const exec of execs) yield listItem(exec);
        },
      };
    },
  };

  workflowService = {
    deleteWorkflowExecution: async (request: {
      namespace: string;
      workflowExecution: { workflowId: string; runId?: string };
    }): Promise<unknown> => {
      if (this.raiseFor.has(request.workflowExecution.workflowId)) throw new Error("boom");
      this.deleted.push(request.workflowExecution);
      return {};
    },
  };
}

const closed = (id: string, subjects: unknown, extra: Partial<Exec> = {}): Exec => ({
  id,
  status: "COMPLETED",
  subjects,
  runId: "run-1",
  ...extra,
});

describe("deleteExecutionsForSubject", () => {
  it("dry-run by default: reports but deletes nothing", async () => {
    const client = new FakeClient([closed("wf-1", ["subject-0001"])]);
    const report = await deleteExecutionsForSubject(client, "subject-0001");
    expect(report.dryRun).toBe(true);
    expect(report.executionsMatched).toBe(1);
    expect(report.deletable.map((d) => d.executionId)).toEqual(["wf-1"]);
    expect(report.deleted).toEqual([]);
    expect(report.deletedCount).toBe(0);
    expect(client.deleted).toEqual([]);
    expect(client.lastQuery).toBe(`${SUBJECT_IDS_SEARCH_ATTRIBUTE} = 'subject-0001'`);
    expect(report.indexCoverage).toContain("post-slice-1");
  });

  it("execute deletes only closed subject-dedicated executions", async () => {
    const client = new FakeClient([
      closed("wf-closed", ["subject-0001"]),
      { id: "wf-running", status: "RUNNING", subjects: ["subject-0001"], runId: "run-1" },
      { id: "wf-multi", status: "FAILED", subjects: ["subject-0001", "subject-0002"], runId: "run-1" },
    ]);
    const report = await deleteExecutionsForSubject(client, "subject-0001", { dryRun: false });
    // `deleted` is the authoritative outcome; `deletable` remains the plan set.
    expect(report.deleted.map((d) => d.executionId)).toEqual(["wf-closed"]);
    expect(report.deletedCount).toBe(1);
    expect(client.deleted).toEqual([{ workflowId: "wf-closed", runId: "run-1" }]);
    expect(report.stillRunning.map((r) => r.executionId)).toEqual(["wf-running"]);
    expect(report.conflicted).toHaveLength(1);
    expect(report.conflicted[0]).toMatchObject({
      executionId: "wf-multi",
      reason: "multi_subject",
      otherSubjectCount: 1,
    });
    // Never leaks other subjects' ids.
    expect(JSON.stringify(report)).not.toContain("subject-0002");
  });

  it("unknown status is excluded fail-safe (never treated as closed)", async () => {
    const client = new FakeClient([
      { id: "wf-none", subjects: ["subject-0001"], runId: "run-1" }, // no status at all
      { id: "wf-weird", status: "", subjects: ["subject-0001"], runId: "run-1" }, // falsy name
      { id: "wf-paused", status: "PAUSED", subjects: ["subject-0001"], runId: "run-1" },
    ]);
    const report = await deleteExecutionsForSubject(client, "subject-0001", { dryRun: false });
    expect(report.deletedCount).toBe(0);
    expect(client.deleted).toEqual([]);
    expect(report.conflicted.map((c) => c.executionId).sort()).toEqual(["wf-none", "wf-paused", "wf-weird"]);
    expect(report.conflicted.every((c) => c.reason === "unknown_status")).toBe(true);
  });

  it("unreadable subject sets are conflicted-unknown fail-safe without aborting the rest", async () => {
    const client = new FakeClient([
      closed("wf-missing", null),
      closed("wf-no-attrs", null, { omitSearchAttributes: true }),
      closed("wf-str", "subject-0001"), // bare string is NOT a subject list
      closed("wf-int", 42),
      closed("wf-raises", null, { typedRaises: true, omitSearchAttributes: true }),
      closed("wf-good", ["subject-0001"]),
    ]);
    const report = await deleteExecutionsForSubject(client, "subject-0001", { dryRun: false });
    // The good row still deletes: one bad row never aborts the enumeration.
    expect(report.deleted.map((d) => d.executionId)).toEqual(["wf-good"]);
    expect(report.conflicted.map((c) => c.executionId).sort()).toEqual([
      "wf-int",
      "wf-missing",
      "wf-no-attrs",
      "wf-raises",
      "wf-str",
    ]);
    expect(report.conflicted.every((c) => c.reason === "unreadable_subjects")).toBe(true);
    expect(report.conflicted.every((c) => c.otherSubjectCount === undefined)).toBe(true);
  });

  it("a readable set without the target is stale_index, not unreadable", async () => {
    const client = new FakeClient([closed("wf-x", ["subject-9999"])]);
    const report = await deleteExecutionsForSubject(client, "subject-0001", { dryRun: false });
    expect(report.deletedCount).toBe(0);
    expect(report.conflicted.map((c) => c.reason)).toEqual(["stale_index"]);
    // The foreign subject id is not leaked.
    expect(JSON.stringify(report)).not.toContain("subject-9999");
  });

  it("records a delete failure rather than swallowing it", async () => {
    const client = new FakeClient([closed("wf-1", ["subject-0001"]), closed("wf-2", ["subject-0001"])]);
    client.raiseFor.add("wf-1");
    const report = await deleteExecutionsForSubject(client, "subject-0001", { dryRun: false });
    // The plan lists both; the outcome lists only the success.
    expect(report.deletable.map((d) => d.executionId)).toEqual(["wf-1", "wf-2"]);
    expect(report.deleted.map((d) => d.executionId)).toEqual(["wf-2"]);
    expect(report.deletedCount).toBe(1);
    expect(client.deleted).toEqual([{ workflowId: "wf-2", runId: "run-1" }]);
    expect(report.failures.map((f) => f.executionId)).toEqual(["wf-1"]);
    expect(report.failures[0]!.reason).toContain("boom");
  });

  it("warns when the enumeration limit truncates (and not at the exact limit)", async () => {
    const many = Array.from({ length: 5 }, (_, i) => closed(`wf-${i}`, ["subject-0001"]));
    const truncatedReport = await deleteExecutionsForSubject(new FakeClient(many), "subject-0001", { limit: 2 });
    expect(truncatedReport.executionsMatched).toBe(2);
    expect(truncatedReport.warnings.some((w) => w.includes("hit the limit"))).toBe(true);
    const exact = Array.from({ length: 2 }, (_, i) => closed(`wf-${i}`, ["subject-0001"]));
    const exactReport = await deleteExecutionsForSubject(new FakeClient(exact), "subject-0001", { limit: 2 });
    expect(exactReport.executionsMatched).toBe(2);
    expect(exactReport.warnings).toEqual([]);
  });

  it("fails closed on an empty subject id", async () => {
    await expect(deleteExecutionsForSubject(new FakeClient([]), "")).rejects.toThrow(/non-empty/);
  });

  it("fails closed when no namespace can be resolved", async () => {
    await expect(deleteExecutionsForSubject(new FakeClient([], ""), "subject-0001")).rejects.toThrow(/namespace/);
  });

  it("explicit namespace overrides the client's", async () => {
    const client = new FakeClient([closed("wf-1", ["subject-0001"])], "client-ns");
    const report = await deleteExecutionsForSubject(client, "subject-0001", {
      namespace: "explicit-ns",
      dryRun: false,
    });
    expect(report.namespace).toBe("explicit-ns");
  });

  it("serialized report omits absent optionals (Python exclude_none parity)", async () => {
    const client = new FakeClient([
      closed("wf-ok", ["subject-0001"], { runId: undefined }),
      closed("wf-unreadable", null, { runId: undefined }),
    ]);
    const report = await deleteExecutionsForSubject(client, "subject-0001");
    const deletableKeys = Object.keys(report.deletable[0]!);
    expect(deletableKeys).not.toContain("runId"); // absent → omitted, not null
    const conflictKeys = Object.keys(report.conflicted[0]!).sort();
    expect(conflictKeys).toEqual(["executionId", "reason"]); // no runId, no null count
  });
});
