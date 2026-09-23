/**
 * The cross-surface erasure seam + `ErasureReceipt` audit record (#715 slice 5) —
 * behavioral parity with the Python `project/erase.py` seam.
 *
 * `eraseSubject` orchestrates the four surface drivers slices 1-4 shipped — the
 * keystore crypto-shred (`subject-keystore.ts`), the `DeleteWorkflowExecution` driver
 * (`subject-execution-deletion.ts`), the Langfuse subject-trace deletion
 * (`subject-trace-deletion.ts`), and the cache subject invalidation
 * (`@typeflux/temporal` `eraseSubjectFromCache`) — into ONE dry-run-first operation
 * that emits the compliance artifact: an {@link ErasureReceipt}.
 *
 * Design invariants (Python parity; docs/design §5-§7):
 * - **Dry-run defaults ON** and is provably mutation-free: the keystore is probed with
 *   the NON-MINTING {@link subjectKeyState} introspection (never `create: true`, never
 *   destroy), the execution driver runs enumeration-only, the trace driver lists
 *   without deleting, and the cache reads its index without deleting. The dry-run
 *   receipt has the SAME shape as an executed one (planned vs performed).
 * - **Skipped is loud, never silent**: every surface appears in every receipt; a
 *   surface whose dependency is not configured or that was deselected is reported
 *   `skipped` with an explicit reason.
 * - **One bad surface never aborts the others**: failures are isolated per surface and
 *   per subject, recorded in the receipt; {@link erasureFailed} is the caller's
 *   non-zero-exit signal.
 * - **Ids and counts only** — never erased content, never other subjects' ids.
 * - The always-present `unreachable` block documents the surfaces Typeflux does NOT
 *   control, so the receipt never over-claims.
 *
 * The receipt is the PROOF of erasure: persist it OUTSIDE the erased surfaces.
 */

import { eraseSubjectFromCache, type SubjectCacheErasureReport } from "@typeflux/temporal";

import {
  deleteExecutionsForSubject,
  type SubjectDeletionClient,
  type SubjectExecutionDeletionReport,
} from "./subject-execution-deletion.js";
import {
  deleteTracesForSubject,
  type LangfuseTraceApiClient,
  type SubjectTraceDeletionReport,
} from "./subject-trace-deletion.js";
import {
  subjectKeyState,
  type SubjectKeyState,
  type SubjectKeystore,
} from "./subject-keystore.js";
import { normalizeSubjectIds } from "./subjects.js";

/** The erasure surfaces, in the design's canonical order: temporal, langfuse, cache. */
export const ERASURE_SURFACES = ["temporal", "langfuse", "cache"] as const;
export type ErasureSurface = (typeof ERASURE_SURFACES)[number];

export type SurfaceStatus = "ok" | "skipped" | "failed";

/** One document-only surface the erasure cannot reach (§8 non-goals). */
export interface UnreachableSurfaceNote {
  readonly surface: string;
  readonly note: string;
}

/** The ALWAYS-PRESENT document-only surfaces block (§7/§8) — every receipt carries it. */
export const UNREACHABLE_SURFACES: readonly UnreachableSurfaceNote[] = [
  {
    surface: "provider_logs",
    note:
      "provider-owned: Typeflux cannot delete the model provider's request logs. " +
      "Erase via your provider's retention/deletion controls (see " +
      "docs/typescript/privacy.md 'Retention & Erasure').",
  },
  {
    surface: "exported_artifacts",
    note:
      "caller-owned: manifests and audit bundles exported out of Typeflux are outside " +
      "its control — the exporter is responsible for erasing them.",
  },
  {
    surface: "mixed_workflow_payloads",
    note:
      "granularity limit: per-payload shred INSIDE a mixed-subject workflow is " +
      "infeasible with the content-blind whole-Payload codec. A mixed execution's " +
      "payloads are sealed under a key combined from ALL its subjects' records, so " +
      "erasing any member shreds the shared history wholesale — never one subject's " +
      "slice of it.",
  },
];

/** One per-subject driver failure inside a surface — recorded, never swallowed. */
export interface SubjectSurfaceFailure {
  readonly subjectId: string;
  readonly error: string;
}

/**
 * One subject's keystore outcome, same shape on dry-run and execute. `stateBefore` is
 * the record's state before this operation — probed WITHOUT minting on a dry run,
 * derived from the destruction result on execute. `wouldShred` is the PLAN (a live
 * record exists to destroy); `shredded` is the PERFORMED outcome (always false on a
 * dry run). A dry run never writes: no mint, no tombstone.
 */
export interface KeystoreShredEntry {
  readonly subjectId: string;
  readonly stateBefore: SubjectKeyState;
  readonly wouldShred: boolean;
  readonly shredded: boolean;
}

/** The crypto-shred half of the temporal surface (§4.1 primary mechanism). */
export interface TemporalKeystoreSection {
  readonly status: SurfaceStatus;
  readonly skipReason?: string;
  readonly entries?: readonly KeystoreShredEntry[];
  /** The PLAN count: live records that would be (or were about to be) destroyed. */
  readonly shreddableKeyRecords?: number;
  /** The PERFORMED count: live records actually destroyed (0 on a dry run). */
  readonly shreddedKeyRecords?: number;
  readonly failures?: readonly SubjectSurfaceFailure[];
}

/**
 * The `DeleteWorkflowExecution` half of the temporal surface (§4.1 complement).
 * `reports` carries the slice-4 driver's audit-honest report VERBATIM per subject —
 * the receipt aggregates, it does not re-shape.
 */
export interface TemporalExecutionsSection {
  readonly status: SurfaceStatus;
  readonly skipReason?: string;
  readonly reports?: readonly SubjectExecutionDeletionReport[];
  readonly failures?: readonly SubjectSurfaceFailure[];
}

/**
 * The temporal surface: keystore shred + execution deletion, independently wired.
 * The two mechanisms have independent dependencies, so each half carries its own
 * skipped/failed state; the surface `status` is `failed` if either half failed,
 * `skipped` only when BOTH are, else `ok`.
 */
export interface TemporalSurfaceSection {
  readonly status: SurfaceStatus;
  readonly skipReason?: string;
  readonly keystore?: TemporalKeystoreSection;
  readonly executions?: TemporalExecutionsSection;
}

/** The Langfuse surface: the slice-2 dual-channel trace-deletion reports verbatim. */
export interface LangfuseSurfaceSection {
  readonly status: SurfaceStatus;
  readonly skipReason?: string;
  readonly reports?: readonly SubjectTraceDeletionReport[];
  readonly failures?: readonly SubjectSurfaceFailure[];
}

/** The cache surface: the slice-3 erasure reports verbatim (incl. not-supported). */
export interface CacheSurfaceSection {
  readonly status: SurfaceStatus;
  readonly skipReason?: string;
  readonly reports?: readonly SubjectCacheErasureReport[];
  readonly failures?: readonly SubjectSurfaceFailure[];
}

/**
 * The audit record of one erasure run (§7) — ids/counts only, NEVER erased content.
 * The same shape on dry-run and execute (planned-vs-performed lives inside the surface
 * sections). Every receipt carries all three surfaces (selected or skipped with a
 * reason), the always-present `unreachable` document-only block, and the aggregated
 * `warnings`. The receipt is the proof of erasure — persist it OUTSIDE the erased
 * surfaces.
 */
export interface ErasureReceipt {
  readonly subjectIds: readonly string[];
  readonly executedAt: string;
  readonly actor: string;
  readonly dryRun: boolean;
  readonly surfaces: {
    readonly temporal: TemporalSurfaceSection;
    readonly langfuse: LangfuseSurfaceSection;
    readonly cache: CacheSurfaceSection;
  };
  readonly unreachable: readonly UnreachableSurfaceNote[];
  readonly warnings: readonly string[];
}

/**
 * True when ANY surface failed — the CLI's non-zero-exit signal. A failure is
 * unmistakable: a driver threw, or an executed driver reported per-item failures.
 * Skipped surfaces are not failures (they are loudly reported as skipped instead).
 */
export function erasureFailed(receipt: ErasureReceipt): boolean {
  return (
    receipt.surfaces.temporal.status === "failed" ||
    receipt.surfaces.langfuse.status === "failed" ||
    receipt.surfaces.cache.status === "failed"
  );
}

/** The injected surface dependencies — a selected surface with a missing dependency is
 * reported skipped-with-reason, never silently omitted, never a crash. */
export interface EraseSubjectDeps {
  readonly temporalClient?: SubjectDeletionClient;
  readonly temporalNamespace?: string;
  readonly subjectKeystore?: SubjectKeystore;
  readonly langfuseClient?: LangfuseTraceApiClient;
  readonly cacheStore?: object;
}

export interface EraseSubjectOptions {
  /** Principal recorded on the receipt (a compliance artifact must name who ran it). */
  readonly actor: string;
  /** Default TRUE: report without mutating. Execute requires an explicit `false`. */
  readonly dryRun?: boolean;
  /** Surface subset; default all three. Unknown names are a loud error. */
  readonly surfaces?: readonly string[];
  /** ISO-8601 lower bound for the Langfuse trace scan window. */
  readonly since?: string;
  /** ISO-8601 upper bound for the Langfuse trace scan window. */
  readonly until?: string;
  /** Execution enumeration limit per subject (truncation is reported). */
  readonly executionLimit?: number;
  /** #795: the spec's `runtime.cache_erasure: targeted` declaration — an incapable store
   * becomes a loud per-subject FAILURE, never the documented full-flush fallback note. */
  readonly requireTargetedCache?: boolean;
}

const DESELECTED = "surface not selected for this erasure run (surfaces option)";

/**
 * Run one surface driver per subject, fail-isolated (the shared skeleton).
 *
 * Sequential per-subject: erasure subject sets are small in practice and every driver
 * already bounds its own work; bounded fan-out (the PLAN_PR_LOOKUP_CONCURRENCY
 * pattern) is deferred until a real large-scale need — the same deferral the slice-4
 * delete loop records. `failed` is true when any subject's call threw OR any returned
 * report carries per-item failures (an executed driver's partial failure is still a
 * failed surface).
 */
async function forEachSubject<R extends { readonly failures: readonly unknown[] }>(
  subjectIds: readonly string[],
  call: (subjectId: string) => Promise<R>,
): Promise<{ reports: R[]; failures: SubjectSurfaceFailure[]; failed: boolean }> {
  const reports: R[] = [];
  const failures: SubjectSurfaceFailure[] = [];
  for (const subjectId of subjectIds) {
    try {
      reports.push(await call(subjectId));
    } catch (error) {
      const reason = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      failures.push({ subjectId, error: reason });
    }
  }
  const failed = failures.length > 0 || reports.some((report) => report.failures.length > 0);
  return { reports, failures, failed };
}

function normalizeSurfaces(surfaces: readonly string[]): readonly ErasureSurface[] {
  const known = new Set<string>(ERASURE_SURFACES);
  const unknown = [...new Set(surfaces.filter((name) => !known.has(name)))].sort();
  if (unknown.length > 0) {
    throw new Error(
      `unknown erasure surface(s) ${JSON.stringify(unknown)}; valid surfaces are ` +
        JSON.stringify(ERASURE_SURFACES),
    );
  }
  const selected = new Set(surfaces);
  if (selected.size === 0) {
    throw new Error(
      `eraseSubject needs at least one surface; pass surfaces from ${JSON.stringify(ERASURE_SURFACES)}`,
    );
  }
  return ERASURE_SURFACES.filter((name) => selected.has(name));
}

function shredSubjects(
  keystore: SubjectKeystore,
  subjectIds: readonly string[],
  dryRun: boolean,
): TemporalKeystoreSection {
  const entries: KeystoreShredEntry[] = [];
  const failures: SubjectSurfaceFailure[] = [];
  for (const subjectId of subjectIds) {
    try {
      if (dryRun) {
        // NON-MINTING introspection: a dry run must never mint a record
        // (mint-on-first-use is encode-path only) nor leave a tombstone.
        const state = subjectKeyState(keystore, subjectId);
        entries.push({
          subjectId,
          stateBefore: state,
          wouldShred: state === "live",
          shredded: false,
        });
      } else {
        const result = keystore.destroySubjectKey(subjectId);
        const stateBefore: SubjectKeyState = result.keyExisted
          ? "live"
          : result.alreadyDestroyed
            ? "destroyed"
            : "absent";
        entries.push({
          subjectId,
          stateBefore,
          wouldShred: result.keyExisted,
          shredded: result.keyExisted,
        });
      }
    } catch (error) {
      const reason = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      failures.push({ subjectId, error: reason });
    }
  }
  return {
    status: failures.length > 0 ? "failed" : "ok",
    entries,
    shreddableKeyRecords: entries.filter((entry) => entry.wouldShred).length,
    shreddedKeyRecords: entries.filter((entry) => entry.shredded).length,
    failures,
  };
}

async function deleteSubjectExecutions(
  client: SubjectDeletionClient,
  subjectIds: readonly string[],
  options: { namespace?: string; dryRun: boolean; limit: number },
): Promise<TemporalExecutionsSection> {
  const { reports, failures, failed } = await forEachSubject<SubjectExecutionDeletionReport>(
    subjectIds,
    (subjectId) =>
      deleteExecutionsForSubject(client, subjectId, {
        ...(options.namespace !== undefined ? { namespace: options.namespace } : {}),
        dryRun: options.dryRun,
        limit: options.limit,
      }),
  );
  return { status: failed ? "failed" : "ok", reports, failures };
}

async function deleteSubjectTraces(
  client: LangfuseTraceApiClient,
  subjectIds: readonly string[],
  options: { dryRun: boolean; since?: string; until?: string },
): Promise<LangfuseSurfaceSection> {
  const { reports, failures, failed } = await forEachSubject<SubjectTraceDeletionReport>(
    subjectIds,
    (subjectId) =>
      deleteTracesForSubject(client, subjectId, {
        dryRun: options.dryRun,
        ...(options.since !== undefined ? { since: options.since } : {}),
        ...(options.until !== undefined ? { until: options.until } : {}),
      }),
  );
  return { status: failed ? "failed" : "ok", reports, failures };
}

async function eraseSubjectCache(
  store: object,
  subjectIds: readonly string[],
  dryRun: boolean,
  requireTargeted: boolean,
): Promise<CacheSurfaceSection> {
  // A store without the capability yields the slice-3 not-supported report
  // (supported: false + the full-flush fallback) — honest, but NOT a failure UNLESS the
  // spec declares `runtime.cache_erasure: targeted` (#795): a declared requirement makes
  // the incapable store a loud per-subject failure, never a fallback note.
  const { reports, failures, failed } = await forEachSubject<SubjectCacheErasureReport>(
    subjectIds,
    async (subjectId) => {
      const report = await eraseSubjectFromCache(store, subjectId, { dryRun });
      if (requireTargeted && !report.supported) {
        const storeName = store.constructor?.name ?? "unknown";
        throw new Error(
          `runtime.cache_erasure is 'targeted' but the wired cache store (${storeName}) does ` +
            "not implement eraseSubject (SubjectErasableCacheStore) — the declared requirement " +
            "forbids the full-flush fallback; wire an erasable store or drop the declaration",
        );
      }
      return report;
    },
  );
  return { status: failed ? "failed" : "ok", reports, failures };
}

/**
 * Roll the surface-level caveats an operator must see up to the receipt (§7): driver
 * scan-completeness warnings, still-running executions (reported, never touched),
 * conflicted exclusions (fail-safe skips the operator must handle deliberately), and
 * the cache full-flush fallback. The always-present index-coverage caveats stay inside
 * the embedded reports.
 */
function aggregateWarnings(
  temporal: TemporalSurfaceSection,
  langfuse: LangfuseSurfaceSection,
  cache: CacheSurfaceSection,
): string[] {
  const warnings: string[] = [];
  for (const report of temporal.executions?.reports ?? []) {
    warnings.push(...report.warnings.map((warning) => `temporal: ${warning}`));
    if (report.stillRunning.length > 0) {
      warnings.push(
        `temporal: ${report.stillRunning.length} running execution(s) for subject ` +
          `'${report.subjectId}' were reported but NOT touched — erase never terminates; ` +
          "re-run after they close, or cancel/migrate them first.",
      );
    }
    if (report.conflicted.length > 0) {
      warnings.push(
        `temporal: ${report.conflicted.length} matched execution(s) for subject ` +
          `'${report.subjectId}' were excluded fail-safe (multi-subject, unreadable ` +
          "subjects, stale index, or unknown status) — see the temporal surface report " +
          "and handle them deliberately.",
      );
    }
  }
  for (const report of langfuse.reports ?? []) {
    warnings.push(...report.warnings.map((warning) => `langfuse: ${warning}`));
    if (report.conflicted.length > 0) {
      warnings.push(
        `langfuse: ${report.conflicted.length} matched trace(s) for subject ` +
          `'${report.subjectId}' were excluded fail-safe (they carry other subjects' ` +
          "markers, or their tags were unreadable) — see the langfuse surface report " +
          "and handle them deliberately.",
      );
    }
  }
  for (const report of cache.reports ?? []) {
    if (!report.supported && report.fullFlushFallback !== undefined) {
      warnings.push(`cache: ${report.fullFlushFallback}`);
    }
    warnings.push(
      ...report.failures.map(
        (failure) => `cache: erasure failure for subject '${report.subjectId}': ${failure}`,
      ),
    );
  }
  // Order-preserving dedupe: repeated per-subject caveats collapse to one line.
  return [...new Set(warnings)];
}

/**
 * Erase subject(s) across the configured surfaces and return the audit receipt.
 *
 * `subjectIds` is one id or an array (validated non-empty strings, order preserved,
 * de-duplicated); an EMPTY set is a loud error — an erasure for zero subjects is
 * meaningless. `options.dryRun` defaults ON and performs ZERO mutation.
 *
 * `since`/`until` bound the Langfuse trace scan window. The temporal and cache
 * surfaces have NO windowing: selecting them alongside a window adds an explicit
 * unsupported note to the receipt, and a window whose selection includes no windowable
 * surface is a loud error (it would be entirely inert).
 */
export async function eraseSubject(
  subjectIds: string | readonly string[],
  deps: EraseSubjectDeps,
  options: EraseSubjectOptions,
): Promise<ErasureReceipt> {
  const normalizedIds = normalizeSubjectIds(
    typeof subjectIds === "string" ? [subjectIds] : subjectIds,
  );
  if (normalizedIds.length === 0) {
    throw new Error(
      "eraseSubject requires at least one subject id — an erasure for zero subjects is meaningless",
    );
  }
  if (options.actor.length === 0 || options.actor.trim() !== options.actor) {
    throw new Error(
      "eraseSubject requires a non-empty, trimmed actor (the receipt is a compliance " +
        "artifact and must name who ran it)",
    );
  }
  if (
    options.executionLimit !== undefined &&
    (!Number.isInteger(options.executionLimit) || options.executionLimit < 1)
  ) {
    // Fail closed (#715 Bugbot): a non-positive limit would enumerate NOTHING and
    // yield an empty, healthy-looking plan with only a truncation note — the
    // temporal deletion would silently do nothing.
    throw new Error(
      `executionLimit must be an integer >= 1 (got ${options.executionLimit}); a ` +
        "non-positive limit would enumerate no executions and report an empty plan " +
        "as if the subject had none",
    );
  }
  const selected = normalizeSurfaces(options.surfaces ?? ERASURE_SURFACES);
  const dryRun = options.dryRun ?? true;
  const hasWindow = options.since !== undefined || options.until !== undefined;
  if (hasWindow && !selected.includes("langfuse")) {
    throw new Error(
      "since/until bound the Langfuse trace scan window, but the langfuse surface is " +
        "not selected — the window would be silently inert. Select langfuse or drop " +
        "the window (the temporal and cache surfaces are window-less).",
    );
  }
  const seamWarnings: string[] = [];
  if (hasWindow) {
    for (const windowless of ["temporal", "cache"] as const) {
      if (selected.includes(windowless)) {
        seamWarnings.push(
          `${windowless}: since/until do not apply to this surface (its enumeration ` +
            "and indexes are window-less); the window bounds only the langfuse trace scan.",
        );
      }
    }
  }

  // --- temporal (keystore shred + execution deletion), design order first -----------
  let temporal: TemporalSurfaceSection;
  if (!selected.includes("temporal")) {
    temporal = { status: "skipped", skipReason: DESELECTED };
  } else {
    const keystore: TemporalKeystoreSection =
      deps.subjectKeystore === undefined
        ? {
            status: "skipped",
            skipReason:
              "no SubjectKeystore backend was provided: the crypto-shred surface " +
              "cannot be driven from this process. Inject the deployment's SHARED " +
              "keystore backend (the process-local in-memory reference keystore holds " +
              "no records minted elsewhere) — see docs/typescript/privacy.md " +
              "'Keystore backends'.",
          }
        : shredSubjects(deps.subjectKeystore, normalizedIds, dryRun);
    const executions: TemporalExecutionsSection =
      deps.temporalClient === undefined
        ? {
            status: "skipped",
            skipReason:
              "no Temporal client was provided: subject-dedicated execution deletion " +
              "(DeleteWorkflowExecution) cannot run. Pass a connected client to drive it.",
          }
        : await deleteSubjectExecutions(deps.temporalClient, normalizedIds, {
            ...(deps.temporalNamespace !== undefined ? { namespace: deps.temporalNamespace } : {}),
            dryRun,
            limit: options.executionLimit ?? 1000,
          });
    const status: SurfaceStatus =
      keystore.status === "failed" || executions.status === "failed"
        ? "failed"
        : keystore.status === "skipped" && executions.status === "skipped"
          ? "skipped"
          : "ok";
    temporal = { status, keystore, executions };
  }

  // --- langfuse ----------------------------------------------------------------------
  let langfuse: LangfuseSurfaceSection;
  if (!selected.includes("langfuse")) {
    langfuse = { status: "skipped", skipReason: DESELECTED };
  } else if (deps.langfuseClient === undefined) {
    langfuse = {
      status: "skipped",
      skipReason:
        "no Langfuse client was provided: the Langfuse surface cannot be driven from " +
        "this process. Configure the langfuse observability backend " +
        "(LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY, plus LANGFUSE_HOST for self-hosted) " +
        "or pass a client exposing the trace API.",
    };
  } else {
    langfuse = await deleteSubjectTraces(deps.langfuseClient, normalizedIds, {
      dryRun,
      ...(options.since !== undefined ? { since: options.since } : {}),
      ...(options.until !== undefined ? { until: options.until } : {}),
    });
  }

  // --- cache -------------------------------------------------------------------------
  let cache: CacheSurfaceSection;
  if (!selected.includes("cache")) {
    cache = { status: "skipped", skipReason: DESELECTED };
  } else if (deps.cacheStore === undefined) {
    // #795: under a declared targeted requirement, a SELECTED cache surface with no store
    // wired is a loud failure — "skipped" would read as the requirement being satisfied
    // while nothing was verified or erased.
    cache =
      options.requireTargetedCache === true
        ? {
            status: "failed",
            reports: [],
            failures: normalizedIds.map((subjectId) => ({
              subjectId,
              error:
                "runtime.cache_erasure is 'targeted' and the cache surface was selected, but " +
                "no cache store was provided to this erase invocation — pass the deployment's " +
                "SubjectErasableCacheStore (bindings/cache store option) so the declared " +
                "requirement can actually be verified and driven",
            })),
          }
        : {
            status: "skipped",
            skipReason:
              "no cache store was provided: the cache surface cannot be driven from this " +
              "process. Pass the deployment's CacheStore (a SubjectErasableCacheStore for " +
              "per-subject invalidation) to drive it.",
          };
  } else {
    cache = await eraseSubjectCache(
      deps.cacheStore,
      normalizedIds,
      dryRun,
      options.requireTargetedCache === true,
    );
  }

  const warnings = [...seamWarnings, ...aggregateWarnings(temporal, langfuse, cache)];
  return {
    subjectIds: normalizedIds,
    executedAt: new Date().toISOString(),
    actor: options.actor,
    dryRun,
    surfaces: { temporal, langfuse, cache },
    unreachable: UNREACHABLE_SURFACES,
    warnings,
  };
}
