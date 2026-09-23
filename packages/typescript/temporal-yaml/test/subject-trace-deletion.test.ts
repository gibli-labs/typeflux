/**
 * Subject-scoped Langfuse trace deletion (#715 slice 2) — behavioral parity with
 * the Python `test_subject_trace_deletion.py` suite. A recording fake of the
 * native langfuse Trace API pins the compliance invariants: a dry run never
 * mutates, multi-subject traces are excluded (never collateral-deleted), a
 * truncated scan warns, and a real run reports what it deleted AND what failed.
 */

import { describe, expect, it } from "vitest";

import {
  deleteTracesForSubject,
  SUBJECT_TRACE_DELETION_UNSUPPORTED,
  SUBJECT_TRACE_INDEX_COVERAGE,
  type LangfuseTraceApiClient,
} from "../src/index.js";

interface ListCall {
  page?: number;
  tags?: string[];
  userId?: string;
  fromTimestamp?: string;
  toTimestamp?: string;
}

/**
 * Records list/delete calls; serves page-numbered results per channel.
 * `tagsById` overrides a row's `tags` (default `[]` — a single-subject trace);
 * `null` models a row whose tags are unreadable (the conflicted-unknown path).
 */
class RecordingTraceApi {
  readonly listCalls: ListCall[] = [];
  readonly deleteBatches: string[][] = [];
  readonly deleted: string[] = [];

  constructor(
    private readonly tagPages: string[][] = [[]],
    private readonly userPages: string[][] = [[]],
    private readonly failIds: ReadonlySet<string> = new Set(),
    private readonly tagsById: ReadonlyMap<string, string[] | null> = new Map(),
    /**
     * Per-channel override for the user_id channel's rows (defaults to the
     * shared mapping) — lets a test serve DIFFERENT tag views of the same trace
     * per channel (the cross-channel merge scenarios).
     */
    private readonly userTagsById?: ReadonlyMap<string, string[] | null>,
  ) {}

  private row(id: string, channel: "tag" | "user"): { id: string; tags?: string[] | null } {
    const mapping = channel === "user" && this.userTagsById !== undefined ? this.userTagsById : this.tagsById;
    const tags = mapping.has(id) ? mapping.get(id)! : [];
    return tags === null ? { id } : { id, tags };
  }

  get client(): LangfuseTraceApiClient {
    return {
      api: {
        traceList: async (query) => {
          this.listCalls.push(query);
          const channel = query.tags ? "tag" : "user";
          const pages = query.tags ? this.tagPages : this.userPages;
          const page = query.page ?? 1;
          const rows = page >= 1 && page <= pages.length ? pages[page - 1]! : [];
          return {
            data: rows.map((id) => this.row(id, channel)),
            meta: { totalPages: pages.length },
          };
        },
        traceDeleteMultiple: async ({ traceIds }) => {
          this.deleteBatches.push([...traceIds]);
          const failing = traceIds.filter((id) => this.failIds.has(id));
          if (failing.length > 0) {
            throw new Error(`delete rejected: ${failing.join(",")}`);
          }
          this.deleted.push(...traceIds);
          return { message: `deleted ${traceIds.length}` };
        },
      },
    };
  }
}

describe("deleteTracesForSubject (#715 slice 2)", () => {
  it("dry run lists the union without deleting", async () => {
    const api = new RecordingTraceApi([["t1", "t2"]], [["t2", "t3"]]);
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });

    expect(report.dryRun).toBe(true);
    expect(report.traceIds).toEqual(["t1", "t2", "t3"]);
    expect(report.matchedByTag).toEqual(["t1", "t2"]);
    expect(report.matchedByUserId).toEqual(["t2", "t3"]);
    expect(report.deletedCount).toBe(0);
    expect(report.failures).toEqual([]);
    // The load-bearing invariant: a dry run performs NO deletion.
    expect(api.deleteBatches).toEqual([]);
    expect(api.deleted).toEqual([]);
  });

  it("defaults to dry run when no option is given", async () => {
    const api = new RecordingTraceApi([["t1"]], [[]]);
    const report = await deleteTracesForSubject(api.client, "subj-1");
    expect(report.dryRun).toBe(true);
    expect(api.deleteBatches).toEqual([]);
  });

  it("queries by both the subject tag and the native userId", async () => {
    const api = new RecordingTraceApi([["t1"]], [["t1"]]);
    await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });

    const tagCall = api.listCalls.find((call) => call.tags !== undefined);
    const userCall = api.listCalls.find((call) => call.userId !== undefined);
    expect(tagCall?.tags).toEqual(["typeflux.subject:subj-1"]);
    expect(userCall?.userId).toBe("subj-1");
  });

  it("de-duplicates traces present in both channels", async () => {
    const api = new RecordingTraceApi([["shared", "tag-only"]], [["shared", "user-only"]]);
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });

    expect(report.traceIds).toEqual(["shared", "tag-only", "user-only"]);
    expect(report.traceIds.filter((id) => id === "shared")).toHaveLength(1);
  });

  it("walks every page of a channel", async () => {
    const api = new RecordingTraceApi([["a", "b"], ["c", "d"], ["e"]], [["f"]]);
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });

    expect(report.matchedByTag).toEqual(["a", "b", "c", "d", "e"]);
    expect(report.matchedByUserId).toEqual(["f"]);
    const tagPages = api.listCalls.filter((call) => call.tags).map((call) => call.page);
    expect(tagPages).toEqual([1, 2, 3]);
    expect(report.warnings).toEqual([]);
  });

  it("execute deletes the union and reports counts", async () => {
    const api = new RecordingTraceApi([["t1", "t2"]], [["t3"]]);
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(report.dryRun).toBe(false);
    expect(report.traceIds).toEqual(["t1", "t2", "t3"]);
    expect(report.deletedCount).toBe(3);
    expect(report.failures).toEqual([]);
    expect([...api.deleted].sort()).toEqual(["t1", "t2", "t3"]);
  });

  it("reports a batch failure instead of swallowing it", async () => {
    // The whole batch fails together (bulk API has no per-id status); with the
    // real 100-batch a single failing id and its neighbours are reported as one.
    const api = new RecordingTraceApi([["ok-1", "boom", "ok-2"]], [[]], new Set(["boom"]));
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(report.deletedCount).toBe(0);
    expect(report.failures.map((f) => f.traceId)).toEqual(["ok-1", "boom", "ok-2"]);
    expect(report.failures[0]!.reason).toContain("delete rejected");
    expect(api.deleted).toEqual([]);
  });

  it("always carries the index-coverage caveat", async () => {
    const api = new RecordingTraceApi([["t1"]], [[]]);
    const dry = await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });
    const live = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(dry.indexCoverage).toBe(SUBJECT_TRACE_INDEX_COVERAGE);
    expect(live.indexCoverage).toBe(SUBJECT_TRACE_INDEX_COVERAGE);
    expect(dry.indexCoverage).toContain("post-slice-1");
  });

  it("passes the timestamp window bounds to the list call", async () => {
    const api = new RecordingTraceApi([["t1"]], [[]]);
    await deleteTracesForSubject(api.client, "subj-1", {
      dryRun: true,
      since: "2026-01-01T00:00:00Z",
      until: "2026-06-01T00:00:00Z",
    });

    const tagCall = api.listCalls.find((call) => call.tags !== undefined);
    expect(tagCall?.fromTimestamp).toBe("2026-01-01T00:00:00Z");
    expect(tagCall?.toTimestamp).toBe("2026-06-01T00:00:00Z");
  });

  it("excludes a multi-subject trace and reports it with a COUNT of other markers", async () => {
    // Deleting a trace that also carries OTHER subjects' markers would destroy
    // their audit trails — excluded and reported with a count, never their ids.
    const api = new RecordingTraceApi(
      [["solo", "shared"]],
      [[]],
      new Set(),
      new Map([
        ["solo", ["typeflux.subject:subj-1"]],
        [
          "shared",
          ["typeflux.subject:subj-1", "typeflux.subject:subj-2", "typeflux.subject:subj-3"],
        ],
      ]),
    );
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(report.traceIds).toEqual(["solo"]);
    expect(api.deleted).toEqual(["solo"]);
    expect(report.conflicted).toEqual([{ traceId: "shared", otherSubjectCount: 2 }]);
    // The other subjects' IDs must not appear anywhere in the report.
    expect(JSON.stringify(report)).not.toContain("subj-2");
  });

  it("dry run reports conflicts identically", async () => {
    const api = new RecordingTraceApi(
      [["shared"]],
      [[]],
      new Set(),
      new Map([["shared", ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]]]),
    );
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });

    expect(report.traceIds).toEqual([]);
    expect(report.conflicted).toEqual([{ traceId: "shared", otherSubjectCount: 1 }]);
    expect(api.deleteBatches).toEqual([]);
  });

  it("excludes a row without readable tags as conflicted-unknown", async () => {
    // No readable tags → the other-subject question is unanswerable; fail-safe
    // exclusion with an unknown count, never deletion on a guess.
    const api = new RecordingTraceApi([[]], [["opaque"]], new Set(), new Map([["opaque", null]]));
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(report.traceIds).toEqual([]);
    expect(api.deleted).toEqual([]);
    expect(report.conflicted).toEqual([{ traceId: "opaque", otherSubjectCount: null }]);
  });

  it("empty tag-channel tags cannot mask a user-channel conflict", async () => {
    // The Bugbot HIGH scenario verbatim: the tag channel serves the trace with
    // an EMPTY tags array while the user_id channel's row carries another
    // subject's marker. A first-readable-wins merge would read the trace as
    // single-subject and delete it; the union merge must exclude it.
    const api = new RecordingTraceApi(
      [["shared"]],
      [["shared"]],
      new Set(),
      new Map([["shared", []]]),
      new Map([["shared", ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]]]),
    );
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(report.traceIds).toEqual([]);
    expect(api.deleted).toEqual([]);
    expect(report.conflicted).toEqual([{ traceId: "shared", otherSubjectCount: 1 }]);
  });

  it("unions disjoint readable tag lists before counting", async () => {
    // Both channels readable but each carrying a DIFFERENT other-subject marker:
    // the union counts both (de-duplicated), not just one channel's view.
    const api = new RecordingTraceApi(
      [["shared"]],
      [["shared"]],
      new Set(),
      new Map([["shared", ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]]]),
      new Map([["shared", ["typeflux.subject:subj-1", "typeflux.subject:subj-3"]]]),
    );
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: true });

    expect(report.traceIds).toEqual([]);
    expect(report.conflicted).toEqual([{ traceId: "shared", otherSubjectCount: 2 }]);
  });

  it("an unreadable channel does not poison a readable union", async () => {
    // One channel's row has no readable tags, the other's is readable: the
    // readable union governs — a clean single-subject list stays deletable and
    // a readable conflict is counted; unknown only when NO channel is readable.
    const api = new RecordingTraceApi(
      [["clean", "shared"]],
      [["clean", "shared"]],
      new Set(),
      new Map([
        ["clean", null],
        ["shared", null],
      ]),
      new Map([
        ["clean", ["typeflux.subject:subj-1"]],
        ["shared", ["typeflux.subject:subj-1", "typeflux.subject:subj-2"]],
      ]),
    );
    const report = await deleteTracesForSubject(api.client, "subj-1", { dryRun: false });

    expect(report.traceIds).toEqual(["clean"]);
    expect(api.deleted).toEqual(["clean"]);
    expect(report.conflicted).toEqual([{ traceId: "shared", otherSubjectCount: 1 }]);
  });

  it("warns on page-cap truncation and still deletes the found subset", async () => {
    const api = new RecordingTraceApi([["a"], ["b"], ["c"], ["d"]], [[]]);
    const report = await deleteTracesForSubject(api.client, "subj-1", {
      dryRun: false,
      maxListPages: 2,
    });

    expect(report.traceIds).toEqual(["a", "b"]);
    expect([...api.deleted].sort()).toEqual(["a", "b"]);
    expect(
      report.warnings.some(
        (warning) =>
          warning.includes("stopped after scanning 2 pages") &&
          warning.includes("more candidate pages remain"),
      ),
    ).toBe(true);

    const dry = await deleteTracesForSubject(
      new RecordingTraceApi([["a"], ["b"], ["c"], ["d"]], [[]]).client,
      "subj-1",
      { dryRun: true, maxListPages: 2 },
    );
    expect(dry.warnings.some((warning) => warning.includes("more candidate pages remain"))).toBe(
      true,
    );
  });

  it("fails closed when the client lacks the trace API endpoints", async () => {
    // The TS path has no kwargs-stripping (the filter rides a typed query object
    // verbatim); the reachable failure mode is a client without the endpoints —
    // a pointed up-front error, never a TypeError mid-listing.
    const noApi = {} as LangfuseTraceApiClient;
    await expect(deleteTracesForSubject(noApi, "subj-1")).rejects.toThrow(
      SUBJECT_TRACE_DELETION_UNSUPPORTED,
    );

    const listOnly = {
      api: { traceList: async () => ({ data: [] }) },
    } as unknown as LangfuseTraceApiClient;
    await expect(deleteTracesForSubject(listOnly, "subj-1")).rejects.toThrow(
      SUBJECT_TRACE_DELETION_UNSUPPORTED,
    );
  });
});
