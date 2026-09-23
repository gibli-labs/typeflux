// The cross-surface erasure seam + ErasureReceipt (#715 slice 5) — parity with the
// Python tests/test_erase.py suite. Orchestration across all four surface drivers with
// fakes: happy path (dry-run and execute), per-surface failure isolation,
// skipped-with-reason (missing dependency AND deselected), dry-run PROVABLY
// mutation-free (the fakes record every mutating call and must see zero),
// empty-subject/actor rejection, windowing pass-through + the window-less notes,
// other-subject non-leakage, and the receipt's exact serialized key sets (the
// cross-edition shape pin).

import { InMemoryCacheStore, type CacheKey, type CacheRecord } from "@typeflux/temporal";
import { describe, expect, it } from "vitest";

import {
  ERASURE_SURFACES,
  eraseSubject,
  erasureFailed,
  UNREACHABLE_SURFACES,
  type EraseSubjectDeps,
} from "../src/erase.js";
import { SUBJECT_IDS_SEARCH_ATTRIBUTE } from "../src/index.js";
import type { SubjectExecutionListItem } from "../src/subject-enumeration.js";
import type { SubjectDeletionClient } from "../src/subject-execution-deletion.js";
import type { LangfuseTraceApiClient } from "../src/subject-trace-deletion.js";
import { InMemorySubjectKeystore, subjectKeyState } from "../src/subject-keystore.js";

// --- fakes (the slice-4 test conventions) ---------------------------------------------

interface Exec {
  id: string;
  status?: string;
  subjects: unknown;
}

function listItem(exec: Exec): SubjectExecutionListItem {
  return {
    workflowId: exec.id,
    runId: "run-1",
    status: { name: exec.status ?? "COMPLETED" },
    searchAttributes: { [SUBJECT_IDS_SEARCH_ATTRIBUTE]: exec.subjects },
  };
}

class FakeClient implements SubjectDeletionClient {
  deleted: string[] = [];
  raiseOnDelete = false;
  readonly options = { namespace: "ns" };

  constructor(private readonly execsBySubject: Record<string, Exec[]>) {}

  workflow = {
    list: (options: { query: string }): AsyncIterable<SubjectExecutionListItem> => {
      const matches = Object.entries(this.execsBySubject)
        .filter(([subjectId]) => options.query.includes(`'${subjectId}'`))
        .flatMap(([, execs]) => execs);
      return {
        async *[Symbol.asyncIterator]() {
          for (const exec of matches) yield listItem(exec);
        },
      };
    },
  };

  workflowService = {
    deleteWorkflowExecution: async (request: {
      namespace: string;
      workflowExecution: { workflowId: string; runId?: string };
    }): Promise<unknown> => {
      if (this.raiseOnDelete) throw new Error("delete refused");
      this.deleted.push(request.workflowExecution.workflowId);
      return {};
    },
  };
}

/** Records every traceList/traceDeleteMultiple call; mutates only on delete. */
class FakeLangfuse implements LangfuseTraceApiClient {
  listCalls: Array<Record<string, unknown>> = [];
  deleted: string[] = [];

  constructor(private readonly traceIdsBySubject: Record<string, string[]>) {}

  api = {
    traceList: async (query: {
      page?: number;
      limit?: number;
      tags?: string[];
      userId?: string;
      fromTimestamp?: string;
      toTimestamp?: string;
    }) => {
      this.listCalls.push({ ...query });
      const subjectId =
        query.userId ?? query.tags?.[0]?.replace("typeflux.subject:", "") ?? "";
      const ids = (query.page ?? 1) === 1 ? (this.traceIdsBySubject[subjectId] ?? []) : [];
      return {
        data: ids.map((id) => ({ id, tags: [`typeflux.subject:${subjectId}`] })),
        meta: { totalPages: 1 },
      };
    },
    traceDeleteMultiple: async (payload: { traceIds: string[] }) => {
      this.deleted.push(...payload.traceIds);
      return { message: "ok" };
    },
  };
}

function seededCache(subjectId: string): InMemoryCacheStore {
  const store = new InMemoryCacheStore();
  const key: CacheKey = { activity: "judge", input_hash: "a".repeat(64), scope: {} };
  store.set(key, { output: { ok: true }, subjects: [subjectId] } as unknown as CacheRecord);
  return store;
}

function fullDeps(subjectId = "subject-0001"): {
  deps: EraseSubjectDeps;
  keystore: InMemorySubjectKeystore;
  client: FakeClient;
  langfuse: FakeLangfuse;
  cache: InMemoryCacheStore;
} {
  const keystore = new InMemorySubjectKeystore();
  keystore.dataKey(subjectId, { create: true });
  const client = new FakeClient({ [subjectId]: [{ id: "wf-1", subjects: [subjectId] }] });
  const langfuse = new FakeLangfuse({ [subjectId]: ["trace-1", "trace-2"] });
  const cache = seededCache(subjectId);
  return {
    deps: { subjectKeystore: keystore, temporalClient: client, langfuseClient: langfuse, cacheStore: cache },
    keystore,
    client,
    langfuse,
    cache,
  };
}

// --- happy paths ----------------------------------------------------------------------

describe("eraseSubject requireTargetedCache (#795)", () => {
  const plainStore = { get: () => undefined, set: () => undefined };

  it("makes an incapable store a loud per-subject failure instead of the fallback note", async () => {
    const receipt = await eraseSubject("subject-0001", { cacheStore: plainStore }, {
      actor: "ops",
      surfaces: ["cache"],
      requireTargetedCache: true,
    });
    expect(receipt.surfaces.cache.status).toBe("failed");
    expect(receipt.surfaces.cache.failures![0]!.error).toMatch(/cache_erasure is 'targeted'/);
  });

  it("fails the selected cache surface when no store is wired under the requirement", async () => {
    const receipt = await eraseSubject("subject-0001", {}, {
      actor: "ops",
      surfaces: ["cache"],
      requireTargetedCache: true,
    });
    expect(receipt.surfaces.cache.status).toBe("failed");
    expect(receipt.surfaces.cache.failures![0]!.error).toMatch(/no cache store was provided/);
  });

  it("keeps the documented fallback note without the requirement", async () => {
    const receipt = await eraseSubject("subject-0001", { cacheStore: plainStore }, {
      actor: "ops",
      surfaces: ["cache"],
    });
    expect(receipt.surfaces.cache.status).toBe("ok");
    expect(receipt.warnings.some((w) => w.includes("Flush the whole store"))).toBe(true);
  });
});

describe("eraseSubject", () => {
  it("dry-run default reports the plan across all surfaces without mutating", async () => {
    const { deps, keystore, client, langfuse, cache } = fullDeps();
    const receipt = await eraseSubject("subject-0001", deps, { actor: "ops" });

    expect(receipt.dryRun).toBe(true);
    expect(receipt.subjectIds).toEqual(["subject-0001"]);
    expect(receipt.actor).toBe("ops");
    expect(erasureFailed(receipt)).toBe(false);
    const keystoreSection = receipt.surfaces.temporal.keystore!;
    expect(keystoreSection.shreddableKeyRecords).toBe(1);
    expect(keystoreSection.shreddedKeyRecords).toBe(0);
    expect(keystoreSection.entries![0]).toEqual({
      subjectId: "subject-0001",
      stateBefore: "live",
      wouldShred: true,
      shredded: false,
    });
    const executionReport = receipt.surfaces.temporal.executions!.reports![0]!;
    expect(executionReport.deletable.map((ref) => ref.executionId)).toEqual(["wf-1"]);
    expect(executionReport.deletedCount).toBe(0);
    const traceReport = receipt.surfaces.langfuse.reports![0]!;
    expect(traceReport.traceIds).toEqual(["trace-1", "trace-2"]);
    expect(traceReport.deletedCount).toBe(0);
    const cacheReport = receipt.surfaces.cache.reports![0]!;
    expect(cacheReport.keysFound).toBe(1);
    expect(cacheReport.keysDeleted).toBe(0);
    expect(receipt.unreachable).toBe(UNREACHABLE_SURFACES);

    // PROVABLY mutation-free.
    expect(subjectKeyState(keystore, "subject-0001")).toBe("live");
    expect(client.deleted).toEqual([]);
    expect(langfuse.deleted).toEqual([]);
    expect(cache.eraseSubject("subject-0001", { dryRun: true }).keysFound).toBe(1);
  });

  it("dry-run probe never mints for an unknown subject", async () => {
    const keystore = new InMemorySubjectKeystore();
    const receipt = await eraseSubject("subject-0002", { subjectKeystore: keystore }, {
      actor: "ops",
      surfaces: ["temporal"],
    });
    const entry = receipt.surfaces.temporal.keystore!.entries![0]!;
    expect(entry.stateBefore).toBe("absent");
    expect(entry.wouldShred).toBe(false);
    // Still absent after the probe: no record, no tombstone.
    expect(subjectKeyState(keystore, "subject-0002")).toBe("absent");
  });

  it("execute performs and reports across all surfaces", async () => {
    const { deps, keystore, client, langfuse } = fullDeps();
    const receipt = await eraseSubject("subject-0001", deps, { actor: "ops", dryRun: false });

    expect(receipt.dryRun).toBe(false);
    expect(erasureFailed(receipt)).toBe(false);
    expect(receipt.surfaces.temporal.keystore!.shreddedKeyRecords).toBe(1);
    expect(subjectKeyState(keystore, "subject-0001")).toBe("destroyed");
    expect(client.deleted).toEqual(["wf-1"]);
    expect(receipt.surfaces.temporal.executions!.reports![0]!.deletedCount).toBe(1);
    expect(langfuse.deleted).toEqual(["trace-1", "trace-2"]);
    expect(receipt.surfaces.langfuse.reports![0]!.deletedCount).toBe(2);
    expect(receipt.surfaces.cache.reports![0]!.keysDeleted).toBe(1);
  });

  it("execute on an already-destroyed subject is idempotent", async () => {
    const keystore = new InMemorySubjectKeystore();
    keystore.dataKey("subject-0001", { create: true });
    keystore.destroySubjectKey("subject-0001");
    const receipt = await eraseSubject("subject-0001", { subjectKeystore: keystore }, {
      actor: "ops",
      dryRun: false,
      surfaces: ["temporal"],
    });
    const entry = receipt.surfaces.temporal.keystore!.entries![0]!;
    expect(entry.stateBefore).toBe("destroyed");
    expect(entry.shredded).toBe(false);
    expect(receipt.surfaces.temporal.keystore!.shreddedKeyRecords).toBe(0);
    expect(erasureFailed(receipt)).toBe(false);
  });

  it("multiple subjects produce per-subject reports and dupes collapse", async () => {
    const keystore = new InMemorySubjectKeystore();
    keystore.dataKey("subject-0001", { create: true });
    const receipt = await eraseSubject(
      ["subject-0001", "subject-0002", "subject-0001"],
      { subjectKeystore: keystore, langfuseClient: new FakeLangfuse({ "subject-0001": ["trace-1"] }) },
      { actor: "ops", surfaces: ["temporal", "langfuse"] },
    );
    expect(receipt.subjectIds).toEqual(["subject-0001", "subject-0002"]);
    const states = Object.fromEntries(
      receipt.surfaces.temporal.keystore!.entries!.map((entry) => [entry.subjectId, entry.stateBefore]),
    );
    expect(states).toEqual({ "subject-0001": "live", "subject-0002": "absent" });
    expect(receipt.surfaces.langfuse.reports!.map((report) => report.subjectId)).toEqual([
      "subject-0001",
      "subject-0002",
    ]);
  });

  // --- skipped-with-reason -------------------------------------------------------------

  it("missing dependencies are skipped with a reason, never silent", async () => {
    const receipt = await eraseSubject("subject-0001", {}, { actor: "ops" });
    expect(receipt.surfaces.temporal.status).toBe("skipped");
    expect(receipt.surfaces.temporal.keystore!.skipReason).toContain("keystore");
    expect(receipt.surfaces.temporal.executions!.skipReason).toContain("Temporal client");
    expect(receipt.surfaces.langfuse.status).toBe("skipped");
    expect(receipt.surfaces.langfuse.skipReason).toContain("Langfuse client");
    expect(receipt.surfaces.cache.status).toBe("skipped");
    expect(receipt.surfaces.cache.skipReason).toContain("cache store");
    expect(erasureFailed(receipt)).toBe(false);
  });

  it("deselected surfaces are reported skipped", async () => {
    const receipt = await eraseSubject("subject-0001", { cacheStore: new InMemoryCacheStore() }, {
      actor: "ops",
      surfaces: ["cache"],
    });
    expect(receipt.surfaces.temporal.status).toBe("skipped");
    expect(receipt.surfaces.temporal.skipReason).toContain("not selected");
    expect(receipt.surfaces.langfuse.status).toBe("skipped");
    expect(receipt.surfaces.langfuse.skipReason).toContain("not selected");
    expect(receipt.surfaces.cache.status).toBe("ok");
  });

  it("a partial temporal dependency yields mixed sub-statuses", async () => {
    const keystore = new InMemorySubjectKeystore();
    keystore.dataKey("subject-0001", { create: true });
    const receipt = await eraseSubject("subject-0001", { subjectKeystore: keystore }, { actor: "ops" });
    expect(receipt.surfaces.temporal.status).toBe("ok"); // one half ran
    expect(receipt.surfaces.temporal.keystore!.status).toBe("ok");
    expect(receipt.surfaces.temporal.executions!.status).toBe("skipped");
  });

  it("an unindexed cache store reports not-supported and warns, not a failure", async () => {
    const plainStore = {
      get: () => undefined,
      set: () => undefined,
    };
    const receipt = await eraseSubject("subject-0001", { cacheStore: plainStore }, {
      actor: "ops",
      surfaces: ["cache"],
    });
    const report = receipt.surfaces.cache.reports![0]!;
    expect(report.supported).toBe(false);
    expect(receipt.surfaces.cache.status).toBe("ok");
    expect(receipt.warnings.some((warning) => warning.includes("Flush the whole store"))).toBe(true);
  });

  // --- failure isolation ---------------------------------------------------------------

  it("one bad surface does not abort the others and marks the receipt failed", async () => {
    const { deps } = fullDeps();
    const exploding = {
      api: {
        traceList: async () => {
          throw new Error("langfuse exploded");
        },
        traceDeleteMultiple: async () => ({}),
      },
    } as unknown as LangfuseTraceApiClient;
    const receipt = await eraseSubject(
      "subject-0001",
      { ...deps, langfuseClient: exploding },
      { actor: "ops" },
    );
    expect(receipt.surfaces.langfuse.status).toBe("failed");
    expect(receipt.surfaces.langfuse.failures![0]!.subjectId).toBe("subject-0001");
    expect(receipt.surfaces.langfuse.failures![0]!.error).toContain("langfuse exploded");
    expect(receipt.surfaces.temporal.status).toBe("ok");
    expect(receipt.surfaces.cache.status).toBe("ok");
    expect(erasureFailed(receipt)).toBe(true);
  });

  it("an executed driver's per-item failures mark the surface failed", async () => {
    const client = new FakeClient({ "subject-0001": [{ id: "wf-1", subjects: ["subject-0001"] }] });
    client.raiseOnDelete = true;
    const receipt = await eraseSubject("subject-0001", { temporalClient: client }, {
      actor: "ops",
      dryRun: false,
      surfaces: ["temporal"],
    });
    expect(receipt.surfaces.temporal.executions!.status).toBe("failed");
    expect(receipt.surfaces.temporal.executions!.reports![0]!.failures.length).toBeGreaterThan(0);
    expect(erasureFailed(receipt)).toBe(true);
  });

  // --- input validation ----------------------------------------------------------------

  it("rejects an empty subject list", async () => {
    await expect(eraseSubject([], {}, { actor: "ops" })).rejects.toThrow(/at least one subject id/);
  });

  it("rejects a blank subject id", async () => {
    await expect(eraseSubject(["  "], {}, { actor: "ops" })).rejects.toThrow(/non-empty/);
  });

  it("rejects a missing or untrimmed actor", async () => {
    await expect(eraseSubject("subject-0001", {}, { actor: "" })).rejects.toThrow(/actor/);
    await expect(eraseSubject("subject-0001", {}, { actor: " ops " })).rejects.toThrow(/actor/);
  });

  it("rejects unknown surfaces and empty selections", async () => {
    await expect(
      eraseSubject("subject-0001", {}, { actor: "ops", surfaces: ["temporal", "provider"] }),
    ).rejects.toThrow(/unknown erasure surface/);
    await expect(eraseSubject("subject-0001", {}, { actor: "ops", surfaces: [] })).rejects.toThrow(
      /at least one surface/,
    );
  });

  it("rejects a non-positive executionLimit at the seam (#715 Bugbot)", async () => {
    // limit 0 would trip the enumeration's truncation guard on the first match and
    // yield an empty, healthy-looking plan — fail loud instead.
    await expect(
      eraseSubject("subject-0001", {}, { actor: "ops", executionLimit: 0 }),
    ).rejects.toThrow(/executionLimit must be an integer >= 1/);
    await expect(
      eraseSubject("subject-0001", {}, { actor: "ops", executionLimit: -1 }),
    ).rejects.toThrow(/executionLimit must be an integer >= 1/);
    await expect(
      eraseSubject("subject-0001", {}, { actor: "ops", executionLimit: 1.5 }),
    ).rejects.toThrow(/executionLimit must be an integer >= 1/);
  });

  // --- windowing -----------------------------------------------------------------------

  it("passes the window through to the langfuse driver", async () => {
    const langfuse = new FakeLangfuse({ "subject-0001": [] });
    await eraseSubject("subject-0001", { langfuseClient: langfuse }, {
      actor: "ops",
      surfaces: ["langfuse"],
      since: "2026-01-01T00:00:00Z",
      until: "2026-06-30T00:00:00Z",
    });
    expect(langfuse.listCalls[0]).toMatchObject({
      fromTimestamp: "2026-01-01T00:00:00Z",
      toTimestamp: "2026-06-30T00:00:00Z",
    });
  });

  it("a window without the langfuse surface is a loud error", async () => {
    await expect(
      eraseSubject("subject-0001", { subjectKeystore: new InMemorySubjectKeystore() }, {
        actor: "ops",
        surfaces: ["temporal"],
        since: "2026-01-01T00:00:00Z",
      }),
    ).rejects.toThrow(/langfuse surface/);
  });

  it("a window alongside window-less surfaces adds explicit notes", async () => {
    const receipt = await eraseSubject("subject-0001", { langfuseClient: new FakeLangfuse({}) }, {
      actor: "ops",
      since: "2026-01-01T00:00:00Z",
    });
    expect(receipt.warnings.some((w) => w.startsWith("temporal:") && w.includes("window-less"))).toBe(true);
    expect(receipt.warnings.some((w) => w.startsWith("cache:") && w.includes("window-less"))).toBe(true);
  });

  // --- aggregated warnings + non-leakage -----------------------------------------------

  it("conflicted and running executions surface as receipt warnings", async () => {
    const client = new FakeClient({
      "subject-0001": [
        { id: "wf-multi", subjects: ["subject-0001", "subject-0002"] },
        { id: "wf-running", status: "RUNNING", subjects: ["subject-0001"] },
      ],
    });
    const receipt = await eraseSubject("subject-0001", { temporalClient: client }, {
      actor: "ops",
      surfaces: ["temporal"],
    });
    expect(receipt.warnings.some((w) => w.includes("excluded fail-safe"))).toBe(true);
    expect(receipt.warnings.some((w) => w.includes("NOT touched"))).toBe(true);
  });

  it("never leaks other subjects' ids anywhere in the receipt", async () => {
    const client = new FakeClient({
      "subject-0001": [{ id: "wf-multi", subjects: ["subject-0001", "other-subject-9"] }],
    });
    const receipt = await eraseSubject("subject-0001", { temporalClient: client }, {
      actor: "ops",
      surfaces: ["temporal"],
    });
    expect(JSON.stringify(receipt)).not.toContain("other-subject-9");
    const conflict = receipt.surfaces.temporal.executions!.reports![0]!.conflicted[0]!;
    expect(conflict.reason).toBe("multi_subject");
    expect(conflict.otherSubjectCount).toBe(1);
  });

  // --- serialized shape (the cross-edition pin) ----------------------------------------

  it("pins the exact receipt key sets", async () => {
    const { deps } = fullDeps();
    const receipt = await eraseSubject("subject-0001", deps, { actor: "ops" });
    const data = JSON.parse(JSON.stringify(receipt)) as Record<string, unknown>;
    expect(Object.keys(data).sort()).toEqual(
      ["actor", "dryRun", "executedAt", "subjectIds", "surfaces", "unreachable", "warnings"].sort(),
    );
    const surfaces = data["surfaces"] as Record<string, Record<string, unknown>>;
    expect(Object.keys(surfaces)).toEqual(["temporal", "langfuse", "cache"]);
    expect(Object.keys(surfaces["temporal"]!).sort()).toEqual(["executions", "keystore", "status"]);
    const keystoreSection = surfaces["temporal"]!["keystore"] as Record<string, unknown>;
    expect(Object.keys(keystoreSection).sort()).toEqual(
      ["entries", "failures", "shreddableKeyRecords", "shreddedKeyRecords", "status"].sort(),
    );
    const entries = keystoreSection["entries"] as Array<Record<string, unknown>>;
    expect(Object.keys(entries[0]!).sort()).toEqual(
      ["shredded", "stateBefore", "subjectId", "wouldShred"].sort(),
    );
    expect(Object.keys(surfaces["temporal"]!["executions"] as object).sort()).toEqual(
      ["failures", "reports", "status"].sort(),
    );
    expect(Object.keys(surfaces["langfuse"]!).sort()).toEqual(["failures", "reports", "status"].sort());
    expect(Object.keys(surfaces["cache"]!).sort()).toEqual(["failures", "reports", "status"].sort());
    for (const note of data["unreachable"] as Array<Record<string, unknown>>) {
      expect(Object.keys(note).sort()).toEqual(["note", "surface"]);
    }
    expect(() => new Date(data["executedAt"] as string).toISOString()).not.toThrow();
  });

  it("skipped sections serialize to status plus reason only", async () => {
    const receipt = await eraseSubject("subject-0001", {}, { actor: "ops" });
    const data = JSON.parse(JSON.stringify(receipt)) as {
      surfaces: Record<string, Record<string, unknown>>;
    };
    expect(Object.keys(data.surfaces["langfuse"]!).sort()).toEqual(["skipReason", "status"]);
    expect(Object.keys(data.surfaces["cache"]!).sort()).toEqual(["skipReason", "status"]);
    const temporal = data.surfaces["temporal"]!;
    expect(Object.keys(temporal).sort()).toEqual(["executions", "keystore", "status"]);
    expect(Object.keys(temporal["keystore"] as object).sort()).toEqual(["skipReason", "status"]);
  });

  it("keeps the canonical surface order regardless of selection order", async () => {
    const receipt = await eraseSubject(
      "subject-0001",
      {
        cacheStore: new InMemoryCacheStore(),
        subjectKeystore: new InMemorySubjectKeystore(),
        langfuseClient: new FakeLangfuse({}),
      },
      { actor: "ops", surfaces: ["cache", "temporal", "langfuse"] },
    );
    expect(Object.keys(receipt.surfaces)).toEqual([...ERASURE_SURFACES]);
  });
});
