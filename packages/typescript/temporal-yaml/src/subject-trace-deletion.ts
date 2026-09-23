/**
 * Subject-scoped Langfuse trace deletion (#715 slice 2) — behavioral parity with
 * the Python `LangfuseTraceReader.delete_traces_for_subject` driver.
 *
 * The Langfuse surface's erasure primitive. The pinned `langfuse` SDK (v3)
 * exposes the full public Trace API on `client.api` — `traceList` (page-numbered)
 * and `traceDeleteMultiple` — so this uses the NATIVE SDK, not a hand-rolled REST
 * call (evidence: `langfuse@3.38.20` d.ts declares `api.traceList` /
 * `api.traceDeleteMultiple`).
 *
 * DUAL-CHANNEL per the ratified decision: slice 1 stamps every trace with BOTH
 * the portable `typeflux.subject:{id}` tag and the native `userId`, so this lists
 * by BOTH and UNIONs the ids — neither channel alone is authoritative. Each
 * channel's pages are walked fully; hitting the page cap yields an explicit
 * warning, never a silently partial set.
 *
 * FAIL-CLOSED subject filter (#715 review, P1): unlike Python's kwargs-compat
 * layer there is NO argument-stripping on this path — the filter rides a single
 * typed query object passed verbatim to `traceList`, so it cannot be silently
 * dropped. The reachable failure mode here is a client whose `api` lacks the
 * trace methods entirely; that raises a pointed not-supported error up front
 * rather than proceeding (and a deletion never starts without a filtered list).
 *
 * Multi-subject traces are EXCLUDED and reported as `conflicted` — deleting a
 * trace that also carries OTHER subjects' markers would silently destroy their
 * audit trails. `dryRun` (default true; safe by default) returns what it WOULD
 * delete without mutating; a real run batches the delete and records per-batch
 * failures. Every report carries the index-coverage caveat: traces predating
 * subject tagging carry neither carrier and are invisible here.
 */

import { subjectTraceTags } from "./subjects.js";

/** Page size for the `traceList` scan, and the `traceDeleteMultiple` batch size. */
const TRACE_LIST_PAGE_SIZE = 100;
const TRACE_DELETE_BATCH_SIZE = 100;
/** A hard ceiling on the pagination walk (a backend that never advances cannot spin forever). */
const TRACE_LIST_MAX_PAGES = 10_000;

/** The tag prefix that marks a subject carrier on a trace (#715 slice 1). */
const SUBJECT_TAG_PREFIX = "typeflux.subject:";

/**
 * The audit-honesty caveat every deletion report carries (#715 slice 2). The two
 * query channels only see traces STAMPED with the subject index (slice 1 onward);
 * older traces carry neither carrier and are invisible here.
 */
export const SUBJECT_TRACE_INDEX_COVERAGE =
  "Only traces stamped with the subject index (native userId + the " +
  "typeflux.subject:<id> tag, emitted since #715 slice 1) are visible to the " +
  "tag and user_id query channels; traces written before subject tagging carry " +
  "neither carrier and are invisible here. This report is complete for " +
  "post-slice-1 traces only — not proof that no older traces exist.";

/** Message raised when a backend has no deletable trace store (parity with Python). */
export const SUBJECT_TRACE_DELETION_UNSUPPORTED =
  "subject-trace deletion is not supported by this observability backend " +
  "(no deletable trace store); configure the Langfuse backend to erase a " +
  "subject's traces (see docs/privacy.md 'Retention & Erasure').";

/** One trace the delete call could not remove — id plus the failure reason. */
export interface SubjectTraceDeletionFailure {
  readonly traceId: string;
  readonly reason: string;
}

/**
 * A matched trace EXCLUDED from deletion because it is not solely this subject's.
 *
 * Slice 1 stamps a trace with ALL of a run's subjects, so deleting a
 * multi-subject trace while erasing ONE subject would silently destroy the other
 * subjects' audit trails. `otherSubjectCount` is the number of OTHER
 * `typeflux.subject:` markers — a COUNT, never the other subject ids: listing
 * them would leak other subjects' presence into this subject's erasure report.
 * `null` means the listing row carried no readable `tags`, so the question is
 * unanswerable — excluded fail-safe as conflicted-unknown.
 */
export interface SubjectTraceDeletionConflict {
  readonly traceId: string;
  readonly otherSubjectCount: number | null;
}

/** The outcome of a subject-scoped trace deletion — ids/counts only, never trace PII. */
export interface SubjectTraceDeletionReport {
  readonly subjectId: string;
  readonly dryRun: boolean;
  /** Raw per-channel hits (a trace may appear in both). */
  readonly matchedByTag: readonly string[];
  readonly matchedByUserId: readonly string[];
  /** The de-duplicated UNION minus `conflicted` — what a dry run WOULD delete. */
  readonly traceIds: readonly string[];
  readonly deletedCount: number;
  readonly failures: readonly SubjectTraceDeletionFailure[];
  /** Matched traces excluded because they also carry OTHER subjects (or tags were unreadable). */
  readonly conflicted: readonly SubjectTraceDeletionConflict[];
  /** The always-present audit caveat (`SUBJECT_TRACE_INDEX_COVERAGE`). */
  readonly indexCoverage: string;
  /** Scan-completeness caveats (e.g. a page-cap truncation). */
  readonly warnings: readonly string[];
}

/** The public-API slice of the langfuse SDK this driver needs (structural — tests fake it). */
export interface LangfuseTraceApiClient {
  api: {
    traceList(query: {
      page?: number;
      limit?: number;
      tags?: string[];
      userId?: string;
      fromTimestamp?: string;
      toTimestamp?: string;
    }): Promise<{
      data: Array<{ id?: string | null; tags?: string[] | null }>;
      meta?: { totalPages?: number | null } | null;
    }>;
    traceDeleteMultiple(payload: { traceIds: string[] }): Promise<{ message?: string }>;
  };
}

interface DeleteOptions {
  readonly dryRun?: boolean;
  /** ISO-8601 lower bound on trace timestamp. */
  readonly since?: string;
  /** ISO-8601 upper bound on trace timestamp. */
  readonly until?: string;
  /** Per-channel page-walk ceiling (a safety valve; default 10000). */
  readonly maxListPages?: number;
}

interface ChannelListing {
  readonly rows: ReadonlyArray<{ traceId: string; tags: string[] | null }>;
  readonly complete: boolean;
  readonly pagesScanned: number;
}

function requireTraceApi(client: LangfuseTraceApiClient): LangfuseTraceApiClient["api"] {
  const api = client?.api;
  // Fail-closed: a client without the trace endpoints must raise up front, not
  // TypeError mid-listing (and never fall back to an unfiltered path).
  if (typeof api?.traceList !== "function" || typeof api?.traceDeleteMultiple !== "function") {
    throw new Error(SUBJECT_TRACE_DELETION_UNSUPPORTED);
  }
  return api;
}

/** Walk `traceList` (page-numbered) for one query channel, keeping each row's tags. */
async function listSubjectTraceRows(
  api: LangfuseTraceApiClient["api"],
  channel: { tags?: string[]; userId?: string; since?: string; until?: string },
  maxPages: number,
): Promise<ChannelListing> {
  const rows: Array<{ traceId: string; tags: string[] | null }> = [];
  let complete = false;
  let pagesScanned = 0;
  let page = 1;
  while (page <= maxPages) {
    const response = await api.traceList({
      page,
      limit: TRACE_LIST_PAGE_SIZE,
      ...(channel.tags !== undefined ? { tags: channel.tags } : {}),
      ...(channel.userId !== undefined ? { userId: channel.userId } : {}),
      ...(channel.since !== undefined ? { fromTimestamp: channel.since } : {}),
      ...(channel.until !== undefined ? { toTimestamp: channel.until } : {}),
    });
    const pageRows = response.data ?? [];
    pagesScanned += 1;
    for (const row of pageRows) {
      if (typeof row.id === "string" && row.id.length > 0) {
        rows.push({ traceId: row.id, tags: Array.isArray(row.tags) ? [...row.tags] : null });
      }
    }
    const totalPages = response.meta?.totalPages ?? undefined;
    if (pageRows.length === 0) {
      complete = true;
      break;
    }
    if (totalPages != null) {
      if (page >= totalPages) {
        complete = true;
        break;
      }
    } else if (pageRows.length < TRACE_LIST_PAGE_SIZE) {
      complete = true;
      break;
    }
    page += 1;
  }
  return { rows, complete, pagesScanned };
}

function channelTruncationWarnings(
  subjectId: string,
  channel: string,
  listing: ChannelListing,
  maxPages: number,
): string[] {
  if (listing.complete) {
    return [];
  }
  return [
    `Langfuse subject-trace listing incomplete for '${subjectId}' on the ${channel} channel: ` +
      `stopped after scanning ${listing.pagesScanned} pages because the page cap (${maxPages}) ` +
      "was reached; more candidate pages remain. The deletion covers only the traces found " +
      "— narrow since/until and rerun to reach the rest.",
  ];
}

function* chunked<T>(items: readonly T[], size: number): Generator<T[]> {
  for (let start = 0; start < items.length; start += size) {
    yield items.slice(start, start + size);
  }
}

/**
 * Erase every Langfuse trace attributed to `subjectId` (#715 slice 2). Lists by
 * both the `typeflux.subject:{id}` tag and the native `userId`, UNIONs the ids,
 * excludes multi-subject conflicts, and — unless `dryRun` (default true) —
 * batches `traceDeleteMultiple`, recording any batch failure rather than
 * swallowing it.
 */
export async function deleteTracesForSubject(
  client: LangfuseTraceApiClient,
  subjectId: string,
  options: DeleteOptions = {},
): Promise<SubjectTraceDeletionReport> {
  const api = requireTraceApi(client);
  const dryRun = options.dryRun ?? true;
  const maxPages = options.maxListPages ?? TRACE_LIST_MAX_PAGES;
  const window = {
    ...(options.since !== undefined ? { since: options.since } : {}),
    ...(options.until !== undefined ? { until: options.until } : {}),
  };
  const [ownTag] = subjectTraceTags([subjectId]);
  const tagListing = await listSubjectTraceRows(api, { tags: [ownTag!], ...window }, maxPages);
  const userListing = await listSubjectTraceRows(api, { userId: subjectId, ...window }, maxPages);
  const warnings = [
    ...channelTruncationWarnings(subjectId, "tag", tagListing, maxPages),
    ...channelTruncationWarnings(subjectId, "user_id", userListing, maxPages),
  ];

  // Merge per-id tag knowledge across channels by UNIONING every READABLE tags
  // list (Bugbot HIGH): keeping only the first readable list would let a
  // tag-channel row with an empty/partial list MASK the user-id channel's richer
  // list carrying other subjects' markers — reading multi-subject as
  // single-subject and re-opening the collateral-deletion hole. A channel with
  // unreadable tags contributes nothing but does NOT poison a readable union;
  // only when NO channel yielded readable tags is the row conflicted-unknown.
  const tagsById = new Map<string, string[] | null>();
  for (const row of [...tagListing.rows, ...userListing.rows]) {
    if (row.tags === null) {
      if (!tagsById.has(row.traceId)) {
        tagsById.set(row.traceId, null);
      }
      continue;
    }
    const known = tagsById.get(row.traceId);
    if (known == null) {
      tagsById.set(row.traceId, [...row.tags]);
    } else {
      for (const tag of row.tags) {
        if (!known.includes(tag)) {
          known.push(tag);
        }
      }
    }
  }

  const matchedByTag = tagListing.rows.map((row) => row.traceId);
  const matchedByUserId = userListing.rows.map((row) => row.traceId);
  const seen = new Set<string>();
  const union: string[] = [];
  for (const traceId of [...matchedByTag, ...matchedByUserId]) {
    if (!seen.has(traceId)) {
      seen.add(traceId);
      union.push(traceId);
    }
  }

  const traceIds: string[] = [];
  const conflicted: SubjectTraceDeletionConflict[] = [];
  for (const traceId of union) {
    const rowTags = tagsById.get(traceId) ?? null;
    if (rowTags === null) {
      // No readable tags → the other-subject question is unanswerable; exclude
      // fail-safe rather than delete on a guess.
      conflicted.push({ traceId, otherSubjectCount: null });
      continue;
    }
    const otherSubjects = rowTags.filter(
      (tag) => tag.startsWith(SUBJECT_TAG_PREFIX) && tag !== ownTag,
    ).length;
    if (otherSubjects > 0) {
      conflicted.push({ traceId, otherSubjectCount: otherSubjects });
    } else {
      traceIds.push(traceId);
    }
  }

  if (dryRun) {
    return {
      subjectId,
      dryRun: true,
      matchedByTag,
      matchedByUserId,
      traceIds,
      deletedCount: 0,
      failures: [],
      conflicted,
      indexCoverage: SUBJECT_TRACE_INDEX_COVERAGE,
      warnings,
    };
  }

  let deletedCount = 0;
  const failures: SubjectTraceDeletionFailure[] = [];
  for (const batch of chunked(traceIds, TRACE_DELETE_BATCH_SIZE)) {
    try {
      await api.traceDeleteMultiple({ traceIds: batch });
      deletedCount += batch.length;
    } catch (error) {
      const reason = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      for (const traceId of batch) {
        failures.push({ traceId, reason });
      }
    }
  }
  return {
    subjectId,
    dryRun: false,
    matchedByTag,
    matchedByUserId,
    traceIds,
    deletedCount,
    failures,
    conflicted,
    indexCoverage: SUBJECT_TRACE_INDEX_COVERAGE,
    warnings,
  };
}
