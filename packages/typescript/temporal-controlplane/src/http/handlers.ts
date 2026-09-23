/**
 * Framework-agnostic HTTP route table for the TS control plane (#620; Python
 * `controlplane/api.py` GET handlers + the dual mount). `buildRoutes(context)` returns a flat list
 * of {method, segments, handler}; the node:http adapter (`server.ts`) matches a request to one and
 * renders `{status, body}` (or the `{error, message}` envelope of a thrown `ProjectControlPlaneError`).
 *
 * DUAL MOUNT (Python parity): every read route is registered TWICE — unprefixed
 * (`/api/v1/…`, serving the registry DEFAULT project) and under `/api/v1/projects/{project}/…`
 * (the full set). The prefixed form binds `params.project`; the unprefixed form leaves it undefined,
 * and the handlers route to the default via `registry.entry(undefined)`. `/api/v1/projects`
 * (the listing) mounts once.
 *
 * PER-REQUEST FRESHNESS: a handler that reads project content re-loads the routed project's bundle
 * from disk and builds a fresh `ProjectControlPlane` (Python re-reads the manifest per request).
 * Capabilities/meta `runtime` come from the registry entry; resolution-gated routes (validate,
 * bundle, catalog) fail closed with 501 for a project whose runtime this server can't resolve.
 *
 * QUERY VALIDATION (#617): a missing REQUIRED query parameter is a 422 `RequestValidationError`
 * with the exact FastAPI-parity message `"1 validation error: query.<name>: Field required"`.
 */

import type { z } from "zod";

import {
  DEFAULT_LIMIT as ENFORCEMENT_DEFAULT_LIMIT,
  decodeCursor,
  filterFingerprint,
  MAX_LIMIT as ENFORCEMENT_MAX_LIMIT,
  parseWindowBound,
  resolveWindow,
} from "../enforcement.js";
import { ProjectControlPlaneError } from "../errors.js";
import {
  buildGithubProvenance,
  type GithubReadResult,
  parseGithubRepo,
  type PlanRef,
  PLAN_PR_LOOKUP_CAP,
  planRefs,
  servedProvenance,
} from "../github-provenance.js";
import type { GithubProvenanceTransport } from "../github-transport.js";
import { isRecord } from "../guards.js";
import type { LangfuseControlPlaneTransport } from "../langfuse-transport.js";
import { type ApiCapabilities, ProjectControlPlane } from "../project-control-plane.js";
import {
  type Actor,
  type Authorizer,
  OpenAuthorizer,
  ProxyHeaderAuthorizer,
  requirePermission,
  type RequestHeaders,
} from "./auth.js";
import type { paths as ContractPaths } from "./contract.js";
import type { ProjectRegistry } from "./registry.js";

/**
 * Compile-time contract anchor (#620): a read route may only be declared with a path the
 * GENERATED contract types (`contract.ts`, from contracts/controlplane/openapi.v1.json) contain
 * under BOTH mounts — unprefixed default and `/projects/{project}`-scoped. Drift between this
 * route table and the contract fails `tsc`, not a conformance run.
 */
type DualMounted<P extends string> = `/api/v1${P}` extends keyof ContractPaths
  ? `/api/v1/projects/{project}${P}` extends keyof ContractPaths
    ? P
    : never
  : never;

const dualMounted = <P extends string>(path: P & DualMounted<P>): string => path;

/**
 * Whether this adapter serves the OPERATION tier (start/review/cancel/status/refresh/repin). Now
 * TRUE (#563): the lifecycle routes are real handlers over the ts-plan-argument operate tier, so
 * `/meta` capabilities track what a caller can actually do against THIS server. `can_start`/
 * `can_resolve` still additionally intersect with resolvability (start derives the plan); review/
 * cancel need only operability (the plan-less binding driver operates a TS execution).
 */
const OPERATIONS_SERVED = true;

/** Everything a route needs: the registry plus the per-project injected catalog/bundle schemas. */
export interface RegistryContext {
  registry: ProjectRegistry;
  /**
   * The activity input/output Zod schemas for a project's bundle/catalog projection, keyed by spec
   * ref (e.g. `"schemas:ClaimItem"`). The EMBEDDING server supplies these (#620 decision: the CP
   * server holds no activity code, so the host injects the schemas its catalog/bundle need). A
   * project with none returns `undefined` — its bundle/catalog 422s on the first referenced ref.
   */
  schemasFor(projectId: string): Readonly<Record<string, z.ZodType>> | undefined;
  /** The authorization boundary; omitted ⇒ the open default (Python `create_app(authorizer=None)`). */
  authorizer?: Authorizer;
  /**
   * The injected langfuse reader transport per project (#573), parallel to {@link schemasFor}: powers
   * the connections reachability probe and the prompt-status live drift tier. A project with none
   * (or an absent `langfuseFor`) degrades both tiers honestly — the CP holds no vendor client.
   */
  langfuseFor?(projectId: string): LangfuseControlPlaneTransport | undefined;
  /**
   * The injected GitHub reader transport per project (#727), a NEW seam parallel to
   * {@link langfuseFor} (a distinct backend — its own host/auth). Powers the github-provenance
   * surface's HEAD-vs-served drift + plan→PR reads. INJECTION-ONLY, exactly like {@link langfuseFor}:
   * absent ⇒ the surface degrades to `not_configured` with NO network call (there is deliberately NO
   * ambient env-token fallback — a CI host carrying GITHUB_TOKEN must not silently light this up, #727
   * F3). On this edition the reader is ALSO only reached when a git source is recorded, which the TS
   * registry never does (`registry.repoSource` is structurally null) — so the surface is
   * `not_configured` on both counts. A future Git-source slice must wire BOTH the transport and the
   * registry source deliberately; until then the seam exists for the tests that inject a served
   * provenance.
   */
  githubFor?(projectId: string): GithubProvenanceTransport | undefined;
}

/** A parsed request handed to a handler: matched path params, decoded query, parsed JSON body. */
export interface RouteRequest {
  /** Matched path parameters (e.g. `{ project, workflow_id }`); `project` is undefined on the default mount. */
  params: Record<string, string | undefined>;
  /** Decoded query — repeated keys collect into arrays (Python `Query(list)`), single keys stay scalar. */
  query: Record<string, string | string[] | undefined>;
  /**
   * LAZY parsed JSON body: calling it parses (and throws a 422 `InvalidRequest` on bad JSON).
   * Lazy so a write route's permission gate runs FIRST — Python's dependency order: an
   * unauthenticated caller with a garbage body gets the 403, never a body-shape error.
   */
  body: () => unknown;
  /** Lowercased request headers — the authorizer's input. Absent keys are undefined. */
  headers: RequestHeaders;
}

/** A handler's result: an HTTP status and a JSON-serializable body. */
export interface RouteResponse {
  status: number;
  body: unknown;
}

export type RouteHandler = (
  request: RouteRequest,
  actor: Actor,
) => RouteResponse | Promise<RouteResponse>;

/** One registered route: a method + a fixed-length segment pattern (`{name}` captures a param). */
export interface Route {
  method: "GET" | "POST";
  /** Path segments; a segment like `"{workflow_id}"` captures into `params.workflow_id`. */
  segments: readonly string[];
  /** Dispatch entry: resolves the actor, gates `inspect` (router-level parity), runs the handler. */
  handler: (request: RouteRequest) => RouteResponse | Promise<RouteResponse>;
}

/** Split a path into non-empty segments (shared by route registration here and request matching in server.ts). */
export const splitPath = (path: string): string[] => path.split("/").filter((segment) => segment.length > 0);

/** The scalar value of a query key (the LAST occurrence if repeated), or undefined when absent. */
function queryScalar(query: RouteRequest["query"], key: string): string | undefined {
  const value = query[key];
  if (Array.isArray(value)) return value[value.length - 1];
  return value;
}

/** Every value of a possibly-repeated query key as an array (Python `Query(list)` — empty when absent). */
function queryList(query: RouteRequest["query"], key: string): string[] {
  const value = query[key];
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

/**
 * A required query parameter's value, or a 422 `RequestValidationError` with the EXACT FastAPI
 * message (#617): `"1 validation error: query.<name>: Field required"`. The count is fixed at 1 —
 * each route validates one required scalar param at a time, matching the golden. A present-but-
 * EMPTY value (`?environment_id=`) is NOT missing — FastAPI passes "" through to the handler,
 * which 404s on the unknown id (the pinned ""-vs-absent parity rule).
 */
/**
 * FastAPI/pydantic boolean query coercion: 1/true/yes/on → true, 0/false/no/off → false
 * (case-insensitive); absent → false (the routes' default); anything else is the 422
 * validation error — a lenient `=== "true"` check would let `?trace=1` slip an auth gate.
 */
function parseBooleanQuery(query: RouteRequest["query"], key: string): boolean {
  const raw = queryScalar(query, key);
  if (raw === undefined) return false;
  const normalized = raw.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "off"].includes(normalized)) return false;
  throw new ProjectControlPlaneError(
    `1 validation error: query.${key}: Input should be a valid boolean, unable to interpret input`,
    422,
    "RequestValidationError",
  );
}

/**
 * Parse a `since`/`until` window-bound query to an aware-UTC `Date`, or `null` when absent (Python
 * FastAPI `datetime | None`). A present-but-unparseable value is the FastAPI-parity datetime 422; a
 * naive (offset-less) value is normalized to UTC (never the server's local offset).
 */
function parseWindowBoundQuery(query: RouteRequest["query"], key: string): Date | null {
  const raw = queryScalar(query, key);
  if (raw === undefined) return null;
  const parsed = parseWindowBound(raw);
  if (parsed === null) {
    throw new ProjectControlPlaneError(
      `1 validation error: query.${key}: Input should be a valid datetime or date, ` +
        "input is malformed",
      422,
      "RequestValidationError",
    );
  }
  return parsed;
}

function requireQuery(query: RouteRequest["query"], key: string): string {
  const value = queryScalar(query, key);
  if (value === undefined) {
    throw new ProjectControlPlaneError(
      `1 validation error: query.${key}: Field required`,
      422,
      "RequestValidationError",
    );
  }
  return value;
}

/**
 * FastAPI-parity body validation for a write route (Python's `extra="forbid"` DTOs → 422
 * `RequestValidationError`). Reads the lazy JSON body, requires it to be a JSON object, and
 * validates it against a field spec: each field is required-or-optional with a type predicate,
 * and any UNKNOWN key is rejected (`extra="forbid"`). Errors carry the FastAPI message shape
 * `"N validation error(s): <loc>: <msg>"`. Guard-order note: the body parses (and 422s on bad
 * JSON) only when a handler calls this — AFTER the permission gate has run.
 */
interface BodyFieldSpec {
  required: boolean;
  /** A human predicate + message (FastAPI-style), returning the value coerced/typed. */
  check: (value: unknown) => { ok: true } | { ok: false; msg: string };
}

const isString = (v: unknown): v is string => typeof v === "string";
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every(isString);

/**
 * FastAPI-parity coercion of an integer query parameter (Python's `limit: int`): a STRICT digits-only
 * string (`-?\d+`, so `Number("")`→0 and `Number("1e2")`→100 are both rejected), else the exact
 * FastAPI 422. Returns `undefined` when the parameter is absent (the caller supplies the default);
 * clamping to a valid range happens in the projection. Shared by the executions + enforcement routes.
 */
function coerceIntQuery(query: RouteRequest["query"], key: string): number | undefined {
  const raw = queryScalar(query, key);
  if (raw === undefined) return undefined;
  if (!/^-?\d+$/.test(raw)) {
    throw new ProjectControlPlaneError(
      `1 validation error: query.${key}: Input should be a valid integer, unable to parse string as an integer`,
      422,
      "RequestValidationError",
    );
  }
  return Number(raw);
}

function validateBody(
  raw: unknown,
  fields: Record<string, BodyFieldSpec>,
): Record<string, unknown> {
  const errors: string[] = [];
  if (!isRecord(raw)) {
    // FastAPI: a non-object body is one validation error at the body root.
    throw new ProjectControlPlaneError(
      "1 validation error: body: Input should be a valid object",
      422,
      "RequestValidationError",
    );
  }
  for (const [name, spec] of Object.entries(fields)) {
    if (!Object.hasOwn(raw, name) || raw[name] === undefined) {
      if (spec.required) errors.push(`body.${name}: Field required`);
      continue;
    }
    const result = spec.check(raw[name]);
    if (!result.ok) errors.push(`body.${name}: ${result.msg}`);
  }
  // extra="forbid": any key not in the spec is rejected (FastAPI "Extra inputs are not permitted").
  for (const key of Object.keys(raw)) {
    if (!Object.hasOwn(fields, key)) {
      errors.push(`body.${key}: Extra inputs are not permitted`);
    }
  }
  if (errors.length > 0) {
    const plural = errors.length === 1 ? "" : "s";
    throw new ProjectControlPlaneError(
      `${errors.length} validation error${plural}: ${errors.join("; ")}`,
      422,
      "RequestValidationError",
    );
  }
  return raw;
}

/** A `ReviewCommand` body field: `user_decision` string required, `reviewer`/`notes` string|null. */
function checkReviewCommand(value: unknown): { ok: true } | { ok: false; msg: string } {
  if (!isRecord(value)) return { ok: false, msg: "Input should be a valid object" };
  if (!isString(value["user_decision"])) {
    return { ok: false, msg: "user_decision: Input should be a valid string" };
  }
  // `gate` (#55 slice 4) targets a specific gate; string|null|absent like reviewer/notes.
  for (const optional of ["reviewer", "notes", "gate"]) {
    const field = value[optional];
    if (field !== undefined && field !== null && !isString(field)) {
      return { ok: false, msg: `${optional}: Input should be a valid string` };
    }
  }
  // ReviewCommand is NOT extra="forbid" in Python (core/contracts.py) — extra keys are tolerated.
  return { ok: true };
}

export function buildRoutes(context: RegistryContext): Route[] {
  const { registry } = context;

  /**
   * Build a fresh control plane for the routed project (per-request freshness). An unknown project id
   * is 404 (from `entry`); a manifest that fails to LOAD (missing / malformed YAML / a Zod spec error)
   * is a CONFIG error, not a server bug — the loader throws a plain `Error`, so wrap it as a 422 with
   * its message (Python maps a `TypefluxError`/`ValueError` from the loader to 422, not 500).
   */
  const controlPlaneFor = (projectId: string | undefined): ProjectControlPlane => {
    const entry = registry.entry(projectId); // 404 on an unknown project id
    let bundle;
    try {
      bundle = registry.resolveProject(projectId);
    } catch (error) {
      if (error instanceof ProjectControlPlaneError) throw error;
      throw new ProjectControlPlaneError(
        error instanceof Error ? error.message : String(error),
        422,
        "ProjectError",
      );
    }
    const schemas = context.schemasFor(entry.id);
    const langfuse = context.langfuseFor?.(entry.id);
    return new ProjectControlPlane(bundle, {
      manifestPath: entry.manifestPath,
      runtime: entry.runtime,
      // Omit (not `undefined`) when the host injects nothing — exactOptionalPropertyTypes.
      ...(schemas !== undefined ? { schemas } : {}),
      ...(langfuse !== undefined ? { langfuse } : {}),
    });
  };

  // A route table entry per Python GET handler, expressed as a relative pattern; mounted twice below.
  const readRoutes: Array<{ path: string; handler: RouteHandler }> = [
    {
      path: dualMounted("/meta"),
      handler: ({ params }, actor) => {
        const cp = controlPlaneFor(params["project"]);
        // Python `ApiCapabilities.for_actor` (#619): the actor's GRANTS intersected with the
        // server's abilities. `resolvable`/`operable` come from the registry (a second resolver
        // runtime tracks both); the operation flags additionally intersect with the tier this
        // server actually serves. The operate tier is now REAL (#563), so `OPERATIONS_SERVED` is
        // true — capabilities stay honest: a `true` flag means the route works (start additionally
        // needs `resolvable`; review/cancel need `operable`). `can_resolve`: validate/bundle/catalog.
        const resolvable = registry.entryRuntimeResolvable(params["project"]);
        const operable = registry.entryOperable(params["project"]) && OPERATIONS_SERVED;
        const capabilities: ApiCapabilities = {
          can_start: actor.can("start") && resolvable && OPERATIONS_SERVED,
          can_review: actor.can("review") && operable,
          can_cancel: actor.can("cancel") && operable,
          can_refresh_project: actor.can("project.refresh") && OPERATIONS_SERVED,
          can_resolve: resolvable,
          // The enforcement-events feed is served now (#723 slice 2). It is RESOLUTION-BOUND (admission
          // verdicts come from the resolved report and the route 501s behind `requireResolvable`), so
          // the flag follows `resolvable` — honest, never advertising a route that would 501.
          enforcement_events: resolvable,
          // The github-provenance surface is served now (#727 slice 2). `resolvable AND a github
          // repo source is recorded` (Python `for_actor`): resolution-bound (the route 501s behind
          // `requireResolvable`) AND it needs recorded provenance to compare. The TS registry serves
          // local checkouts only, so `repoSource` is null and the flag is false — but it is COMPUTED
          // here, so a test injecting a repo source (or a future Git-source slice) lights it up.
          github_provenance: resolvable && parseGithubRepo(registry.repoSource(params["project"])?.url) !== null,
        };
        // The authenticated principal is surfaced ONLY behind a TRUSTED proxy (`--trust-proxy-auth`,
        // Python `_caller_identity`, #577): the sole mode where the actor id is an identity the operator
        // vouches for. In token mode `actor.id` is a grant NAME (config, not an identity) and in open mode
        // it is null; neither is echoed. `ProxyHeaderAuthorizer` already maps an EMPTY actor header to null
        // (its explicit length check), so an empty header reads as no identity — matching Python.
        const caller_identity = context.authorizer instanceof ProxyHeaderAuthorizer ? actor.id : null;
        return { status: 200, body: { ...cp.meta(), caller_identity, capabilities } };
      },
    },
    {
      path: dualMounted("/workflows"),
      handler: ({ params }) => ({ status: 200, body: controlPlaneFor(params["project"]).workflows() }),
    },
    {
      path: dualMounted("/environments"),
      handler: ({ params }) => ({ status: 200, body: controlPlaneFor(params["project"]).environments() }),
    },
    {
      path: dualMounted("/environments/{environment_id}"),
      handler: ({ params }) => ({
        status: 200,
        body: controlPlaneFor(params["project"]).environmentDetail(params["environment_id"]!),
      }),
    },
    {
      path: dualMounted("/policies"),
      handler: ({ params }) => ({ status: 200, body: controlPlaneFor(params["project"]).policies() }),
    },
    {
      path: dualMounted("/policies/{policy_id}"),
      handler: ({ params }) => ({
        status: 200,
        body: controlPlaneFor(params["project"]).policyDetail(params["policy_id"]!),
      }),
    },
    {
      path: dualMounted("/profiles"),
      handler: ({ params }) => ({ status: 200, body: controlPlaneFor(params["project"]).profiles() }),
    },
    {
      // The insight-acknowledgement annotations projection (#733; Python `annotations`): the in-repo
      // `.typeflux/annotations.yaml`. A pure-YAML, project-level read — NOT resolution-bound (served
      // even for a foreign-runtime project) and NO capability flag, exactly like /workflows.
      path: dualMounted("/annotations"),
      handler: ({ params }) => ({ status: 200, body: controlPlaneFor(params["project"]).annotations() }),
    },
    {
      path: dualMounted("/profiles/{kind}/{profile_id}"),
      handler: ({ params }) => ({
        status: 200,
        body: controlPlaneFor(params["project"]).profileDetail(params["kind"]!, params["profile_id"]!),
      }),
    },
    {
      path: dualMounted("/validate"),
      handler: ({ params, query }) => {
        // Resolution-gated: a project whose runtime this server can't resolve fails closed (501).
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        const environmentId = queryScalar(query, "environment_id");
        const workflowIds = queryList(query, "workflow_id");
        const policyIds = queryList(query, "policy_id");
        // An UNKNOWN selected environment/workflow id is a 404 (Python `validate` runs
        // `_require_environment`/`_require_workflow` before resolving) — not an in-band validation
        // issue. That includes the EMPTY STRING: Python gates on `is not None`, so a present-but-
        // empty `?environment_id=` 404s (the pinned ""-vs-absent parity rule).
        if (environmentId !== undefined && !cp.hasEnvironment(environmentId)) {
          throw new ProjectControlPlaneError(`unknown project environment: ${environmentId}`, 404);
        }
        for (const workflowId of workflowIds) {
          if (!cp.hasWorkflow(workflowId)) {
            throw new ProjectControlPlaneError(`unknown project workflow: ${workflowId}`, 404);
          }
        }
        const report = cp.validate({
          ...(environmentId !== undefined ? { environmentId } : {}),
          ...(workflowIds.length > 0 ? { workflowIds } : {}),
          ...(policyIds.length > 0 ? { policyIds } : {}),
        });
        return { status: 200, body: report };
      },
    },
    {
      path: dualMounted("/enforcement-events"),
      handler: async ({ params, query }) => {
        // The enforcement-events feed (#723; Python `enforcement_events`): normalized admission
        // verdicts from the read tier's validation surface + bounded runtime moderation-block verdicts
        // read from Langfuse through the injected transport seam. Resolution-dependent (admission is
        // derived from the resolved report), so a non-TS-runtime project 501s exactly like /validate.
        //
        // GUARD ORDER mirrors Python: FastAPI coerces the query surface FIRST (limit int, since/until
        // datetime → 422), then the body runs `_require_resolvable` (501), the REQUIRED scope (422),
        // the environment/workflow 404s, the verdict allow-set (422), and the cursor decode (422).
        // limit int coercion (STRICT digits, FastAPI-parity message) — before the resolvable 501.
        const limit = coerceIntQuery(query, "limit") ?? ENFORCEMENT_DEFAULT_LIMIT;
        // since/until datetime coercion — a NAIVE value is normalized to UTC (never the server's
        // local offset). An unparseable value is the FastAPI-parity datetime 422.
        const rawSince = parseWindowBoundQuery(query, "since");
        const rawUntil = parseWindowBoundQuery(query, "until");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        // SCOPE IS REQUIRED (#723): without an environment the report never resolves any workflow, so
        // the feed would answer `[] / not_configured` — indistinguishable from a healthy quiet project.
        // Demand the scope explicitly rather than answer a silently-empty, resolvable feed. A present-
        // but-EMPTY `?environment_id=` is NOT missing (the ""-vs-absent rule): it 404s below.
        const environmentId = queryScalar(query, "environment_id");
        if (environmentId === undefined) {
          throw new ProjectControlPlaneError(
            "enforcement-events requires environment_id: admission verdicts resolve per environment, " +
              "and an unscoped call cannot be resolved (it would answer an empty, not_configured feed " +
              "indistinguishable from a quiet one)",
            422,
          );
        }
        if (!cp.hasEnvironment(environmentId)) {
          throw new ProjectControlPlaneError(`unknown project environment: ${environmentId}`, 404);
        }
        const workflowIds = queryList(query, "workflow_id");
        for (const workflowId of workflowIds) {
          if (!cp.hasWorkflow(workflowId)) {
            throw new ProjectControlPlaneError(`unknown project workflow: ${workflowId}`, 404);
          }
        }
        const verdicts = queryList(query, "verdict");
        const invalid = verdicts.filter((value) => value !== "blocked" && value !== "rejected");
        if (invalid.length > 0) {
          throw new ProjectControlPlaneError(
            `unknown verdict filter(s): ${invalid.join(", ")}; allowed: blocked, rejected`,
            422,
          );
        }
        const policyIds = queryList(query, "policy_id");
        const { since, until } = resolveWindow(rawSince ?? undefined, rawUntil ?? undefined);
        // The cursor's offset is bound to a fingerprint of THIS filter set: a cursor reused after any
        // filter change is rejected (422). Fingerprint the caller's RAW window intent (undefined when
        // unpinned), not the resolved window — an unpinned `until` resolves to now() afresh each call.
        const fingerprint = filterFingerprint({
          workflowIds,
          environmentId,
          verdicts,
          policyIds,
          ...(rawSince !== null ? { since: rawSince } : {}),
          ...(rawUntil !== null ? { until: rawUntil } : {}),
        });
        let offset: number;
        try {
          offset = decodeCursor(queryScalar(query, "cursor"), fingerprint);
        } catch (error) {
          throw new ProjectControlPlaneError(error instanceof Error ? error.message : String(error), 422);
        }
        const boundedLimit = Math.max(1, Math.min(limit, ENFORCEMENT_MAX_LIMIT));
        // Size the runtime fetch to just what this page could need (offset + one page), capped at MAX.
        const readerLimit = Math.min(offset + boundedLimit, ENFORCEMENT_MAX_LIMIT);
        const body = await cp.enforcementEvents({
          environmentId,
          workflowIds,
          policyIds,
          verdicts,
          since,
          until,
          limit: boundedLimit,
          offset,
          cursorFingerprint: fingerprint,
          readerLimit,
        });
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/github-provenance"),
      handler: async ({ params }) => {
        // The github-provenance surface (#727; Python `github_provenance`): HEAD-vs-served drift +
        // plan→approving-PR, read at request time through the injected GitHub transport seam.
        // Resolution-bound like /enforcement-events (the `github_provenance` capability follows
        // `resolvable`), so a non-TS-runtime project 501s here.
        registry.requireResolvable(params["project"]);
        const entry = registry.entry(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        // The served side comes from the registry's recorded git provenance. On this edition it is
        // ALWAYS null (the TS registry serves local checkouts only — no Git `repo:` source, no clone),
        // so the surface reports `not_configured` with NO network call, exactly as a Python local
        // mount does. `servedProvenance` re-parses the host (cheap, network-free).
        const repo = registry.repoSource(entry.id);
        // Host-gate BEFORE resolving the clone HEAD (Python `_served_github_provenance` parity —
        // its own review round added the same ordering): a non-github source must not trigger
        // git work whose result is discarded (Bugbot). Inert on this edition (repoSource is
        // structurally null) but the ordering must hold for the future git-source slice.
        const served =
          repo === null || parseGithubRepo(repo.url) === null
            ? null
            : servedProvenance({ repoUrl: repo.url, repoRef: repo.ref, repoSha: registry.repoHeadSha(entry.id) });
        // Every VALID plan is listed (newest-first by generated_at); each plan's sha is its plan
        // FILE's last commit in the served clone — null on this edition (no clone/git), so `sha`/`pr`
        // are honest nulls, never fabricated (Python P0-1: never attribute a PR to the checkout sha).
        const allPlanRefs: PlanRef[] = cp.githubPlanFiles().map((file) => ({
          planId: file.planId,
          sha: registry.planFileSha(entry.id, file.path),
        }));
        // Inert-when-absent, exactly like {@link langfuseFor}: NO ambient env-token self-construction
        // (#727 F3a) — a CI host carrying GITHUB_TOKEN must not silently light this surface up. Absent
        // served provenance OR an absent injected transport ⇒ not_configured with NO network call. The
        // seam stays injection-only (tests) until a Git-source slice wires BOTH deliberately.
        const transport = context.githubFor?.(entry.id);
        let readResult: GithubReadResult;
        if (served === null || transport === undefined) {
          readResult = { status: "not_configured" };
        } else {
          // Bound the PR fan-out to the most-recent sha-bearing plans; the tail is still listed
          // (pr null), just not looked up.
          const lookup = planRefs(allPlanRefs, PLAN_PR_LOOKUP_CAP);
          readResult = await transport.read({
            repo: served.repo,
            branch: served.branch,
            servedSha: served.servedSha,
            planShas: lookup.flatMap((ref) => (ref.sha !== null ? [ref.sha] : [])),
          });
        }
        return { status: 200, body: buildGithubProvenance({ served, plans: allPlanRefs, readResult }) };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/bundle"),
      handler: ({ params, query }) => {
        // Required-query 422 BEFORE the resolvable 501 — FastAPI validates the request surface
        // at argument-resolution time, before the handler body's gates run.
        const environmentId = requireQuery(query, "environment_id");
        registry.requireResolvable(params["project"]);
        const policyIds = queryList(query, "policy_id");
        // `deployment_image` is real now (#687): a supplied image yields the secret-free
        // `deployment_preview`; its absence yields `deployment_preview_reference`.
        const deploymentImage = queryScalar(query, "deployment_image");
        const cp = controlPlaneFor(params["project"]);
        return { status: 200, body: cp.bundle(params["workflow_id"]!, environmentId, policyIds, deploymentImage) };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/catalog"),
      handler: ({ params, query }) => {
        // Same ordering as bundle: the missing-query 422 wins over the resolvable 501.
        const environmentId = requireQuery(query, "environment_id");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        return { status: 200, body: cp.activityCatalog(params["workflow_id"]!, environmentId) };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/executions"),
      handler: async ({ params, query }) => {
        // FastAPI validates the WHOLE query surface before the handler body: the required
        // environment_id AND the limit integer 422 before the resolvable 501 can fire.
        const environmentId = requireQuery(query, "environment_id");
        // Python `limit: int = 20` — FastAPI coerces the string and 422s when it isn't an integer;
        // clamping to [1, 100] happens in the projection (shared strict-digits coercion).
        const limit = coerceIntQuery(query, "limit");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        const body = await cp.executions(params["workflow_id"]!, environmentId, {
          ...(limit !== undefined ? { limit } : {}),
        });
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/versions"),
      handler: async ({ params, query }) => {
        // The cross-version drain view (#686; Python `versions`). Resolution-gated like
        // executions (the current version identity is derived from the resolved plan); the
        // missing-query 422 wins over the resolvable 501 (FastAPI validation order).
        const environmentId = requireQuery(query, "environment_id");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        const body = await cp.versions(params["workflow_id"]!, environmentId);
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/workers"),
      handler: async ({ params, query }) => {
        // Task-queue worker presence (#686; Python `workers`). `task_queue` is the optional
        // override; an unreachable cluster degrades IN-BAND (Python parity), only a hang 503s.
        const environmentId = requireQuery(query, "environment_id");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        const taskQueue = queryScalar(query, "task_queue");
        const body = await cp.workers(params["workflow_id"]!, environmentId, {
          ...(taskQueue !== undefined ? { taskQueue } : {}),
        });
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/correlation"),
      handler: async ({ params, query }) => {
        // Run-to-trace correlation (#686; Python `correlation`). BOTH queries are required,
        // checked in declaration order — the same one-at-a-time posture the status route
        // established for its dual required params.
        const environmentId = requireQuery(query, "environment_id");
        const executionId = requireQuery(query, "execution_id");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        const body = await cp.correlation(params["workflow_id"]!, environmentId, executionId);
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/status"),
      handler: async ({ params, query }, actor) => {
        // GET lifecycle status (Python `status`). GUARD ORDER mirrors Python exactly: required
        // `environment_id`/`execution_id` queries (422, FastAPI validation) → the OPERABLE gate
        // (a binding driver, not resolution — an operable-but-unresolvable project answers status)
        // → workflow/environment 404 → the `trace=true` 403 → the memo-verified bounded query. The
        // operable-501 wins over the trace-403 (Python runs `_require_operable()` first), so a
        // trace=true poll on a non-operable project is a 501, not a 403.
        const environmentId = requireQuery(query, "environment_id");
        const executionId = requireQuery(query, "execution_id");
        registry.requireOperable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        if (!cp.hasWorkflow(params["workflow_id"]!)) {
          throw new ProjectControlPlaneError(`unknown project workflow: ${params["workflow_id"]}`, 404);
        }
        if (!cp.hasEnvironment(environmentId)) {
          throw new ProjectControlPlaneError(`unknown project environment: ${environmentId}`, 404);
        }
        // trace records an auditable operation → it needs an operate-class grant; an
        // inspect-only caller polling trace=true is a 403 (after operable/404, per Python).
        // FastAPI bool coercion: 1/true/yes/on (case-insensitive) are TRUE, 0/false/no/off are
        // FALSE, anything else is a 422 — `trace=1` must gate exactly like `trace=true`.
        const trace = parseBooleanQuery(query, "trace");
        if (trace && !actor.canOperate()) {
          throw new ProjectControlPlaneError(
            "trace=true status records an auditable lifecycle operation and requires an " +
              "operate-class permission (start/review/cancel/project.refresh); use trace=false " +
              "for read-only status polling",
            403,
          );
        }
        const runId = queryScalar(query, "run_id");
        const policyIds = queryList(query, "policy_id");
        const expectedPolicyHash = queryScalar(query, "expected_policy_hash");
        const body = await cp.status(params["workflow_id"]!, environmentId, executionId, {
          ...(runId !== undefined ? { runId } : {}),
          ...(policyIds.length > 0 ? { policyIds } : {}),
          ...(expectedPolicyHash !== undefined ? { expectedPolicyHash } : {}),
        });
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/connections"),
      handler: async ({ params, query }) => {
        // Resolution-gated like Python's connections route; missing-query 422 wins over the
        // resolvable 501 (FastAPI validation order). The probe is async by design.
        const environmentId = requireQuery(query, "environment_id");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        const body = await cp.connections(params["workflow_id"]!, environmentId);
        return { status: 200, body };
      },
    },
    {
      path: dualMounted("/workflows/{workflow_id}/prompt-status"),
      handler: async ({ params, query }) => {
        // Resolution-gated like bundle/catalog: drift is computed from the resolved spec.
        // The missing-query 422 wins over the resolvable 501 (FastAPI validation order).
        const environmentId = requireQuery(query, "environment_id");
        registry.requireResolvable(params["project"]);
        const cp = controlPlaneFor(params["project"]);
        // Async now (#573): with a langfuse transport injected, drift is a live lookup.
        return { status: 200, body: await cp.promptStatus(params["workflow_id"]!, environmentId) };
      },
    },
    {
      path: dualMounted("/deployments"),
      // Real now (#687 D687-5): reads the project's `deployments/` dir and live-verifies each plan.
      // Resolution-gated like Python's deployments_list: a foreign-runtime project answers 501, and
      // a project with no plans answers [] (the plan dir simply does not exist). PER-PLAN DEGRADE
      // (both editions): a stale plan (removed workflow) or a malformed/tampered file yields a
      // failed-verification entry — one bad file never hides the valid plans or 422s the listing.
      handler: ({ params }) => {
        registry.requireResolvable(params["project"]); // 404s unknown ids first, 501s foreign runtimes
        return { status: 200, body: controlPlaneFor(params["project"]).deployments() };
      },
    },
    {
      path: dualMounted("/deployments/{plan_id}"),
      handler: ({ params }) => {
        registry.requireResolvable(params["project"]);
        return { status: 200, body: controlPlaneFor(params["project"]).deployment(params["plan_id"]!) };
      },
    },
  ];

  // Operation-route handlers (#563 operate tier): real lifecycle routes over the ts-plan-argument
  // driver. GUARD ORDER mirrors Python's dependency chain EXACTLY: the route-level permission gate
  // FIRST (an underprivileged caller 403s before any body/project work — `auth-token-reader-cannot-
  // start`), then the routed-project 404 / gate, then body validation, then the bounded Temporal
  // op. start is resolution-gated (the plan is derived); review/cancel/status are OPERABLE-gated (a
  // binding driver, not resolution). review/cancel answer 204 with no body (Python `Response(204)`).
  const startHandler: RouteHandler = async (request, actor) => {
    requirePermission(actor, "start");
    // FastAPI validates the request BODY in the pre-handler solve — before the handler's
    // project gates ever run. Body 422 wins over project 404/501 (parity finder).
    const body = validateBody(request.body(), {
      environment_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      execution_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      input: { required: true, check: (v) => (isRecord(v) ? { ok: true } : { ok: false, msg: "Input should be a valid object" }) },
      task_queue: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      policy_ids: { required: false, check: (v) => (isStringArray(v) ? { ok: true } : { ok: false, msg: "Input should be a valid array" }) },
      expected_policy_hash: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
    });
    registry.requireResolvable(request.params["project"]);
    const cp = controlPlaneFor(request.params["project"]);
    const receipt = await cp.start(
      request.params["workflow_id"]!,
      body["environment_id"] as string,
      body["execution_id"] as string,
      body["input"] as Record<string, unknown>,
      {
        ...(body["task_queue"] !== undefined ? { taskQueue: body["task_queue"] as string | null } : {}),
        ...(body["policy_ids"] !== undefined ? { policyIds: body["policy_ids"] as string[] } : {}),
        ...(body["expected_policy_hash"] !== undefined ? { expectedPolicyHash: body["expected_policy_hash"] as string | null } : {}),
      },
    );
    return { status: 200, body: receipt };
  };
  const reviewHandler: RouteHandler = async (request, actor) => {
    requirePermission(actor, "review");
    // Body 422 before the operable gate (FastAPI pre-handler solve — parity finder).
    const body = validateBody(request.body(), {
      environment_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      execution_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      command: { required: true, check: checkReviewCommand },
      run_id: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      policy_ids: { required: false, check: (v) => (isStringArray(v) ? { ok: true } : { ok: false, msg: "Input should be a valid array" }) },
      expected_policy_hash: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
    });
    registry.requireOperable(request.params["project"]);
    const cp = controlPlaneFor(request.params["project"]);
    await cp.submitReview(
      request.params["workflow_id"]!,
      body["environment_id"] as string,
      body["execution_id"] as string,
      body["command"] as { user_decision: string; reviewer?: string | null; notes?: string | null; gate?: string | null },
      {
        ...(body["run_id"] !== undefined ? { runId: body["run_id"] as string | null } : {}),
        ...(body["policy_ids"] !== undefined ? { policyIds: body["policy_ids"] as string[] } : {}),
        ...(body["expected_policy_hash"] !== undefined ? { expectedPolicyHash: body["expected_policy_hash"] as string | null } : {}),
      },
    );
    // 204 No Content, empty body (Python `Response(status_code=204)`).
    return { status: 204, body: undefined };
  };
  const cancelHandler: RouteHandler = async (request, actor) => {
    requirePermission(actor, "cancel");
    // Body 422 before the operable gate (FastAPI pre-handler solve — parity finder).
    const body = validateBody(request.body(), {
      environment_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      execution_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      reason: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      run_id: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      policy_ids: { required: false, check: (v) => (isStringArray(v) ? { ok: true } : { ok: false, msg: "Input should be a valid array" }) },
      expected_policy_hash: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
    });
    registry.requireOperable(request.params["project"]);
    const cp = controlPlaneFor(request.params["project"]);
    await cp.requestCancel(
      request.params["workflow_id"]!,
      body["environment_id"] as string,
      body["execution_id"] as string,
      (body["reason"] as string | null | undefined) ?? null,
      {
        ...(body["run_id"] !== undefined ? { runId: body["run_id"] as string | null } : {}),
        ...(body["policy_ids"] !== undefined ? { policyIds: body["policy_ids"] as string[] } : {}),
        ...(body["expected_policy_hash"] !== undefined ? { expectedPolicyHash: body["expected_policy_hash"] as string | null } : {}),
      },
    );
    return { status: 204, body: undefined };
  };
  // REPIN decision (#563): Python drops a pinned operations runtime so the next mutating call
  // re-resolves the YAML. The TS operate tier pins NOTHING — every operation builds a fresh
  // `WorkflowOperations` from the per-request-fresh bundle, so reads and writes already reflect
  // the YAML on disk. Repin is therefore a structural no-op that returns the HONEST DTO: nothing
  // was pinned, so `repinned: false, dropped: 0` — never fabricated as if a pin were dropped.
  const repinHandler: RouteHandler = (request, actor) => {
    requirePermission(actor, "project.refresh");
    // Body 422 before the project gates (FastAPI pre-handler solve — parity finder).
    const body = validateBody(request.body(), {
      environment_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
    });
    registry.requireResolvable(request.params["project"]);
    const cp = controlPlaneFor(request.params["project"]);
    const environmentId = body["environment_id"] as string;
    // Guard order parity with Python's repin: workflow 404 then environment 404, after which the
    // no-op result. `hasWorkflow`/`hasEnvironment` mirror `_require_workflow`/`_require_environment`.
    if (!cp.hasWorkflow(request.params["workflow_id"]!)) {
      throw new ProjectControlPlaneError(`unknown project workflow: ${request.params["workflow_id"]}`, 404);
    }
    if (!cp.hasEnvironment(environmentId)) {
      throw new ProjectControlPlaneError(`unknown project environment: ${environmentId}`, 404);
    }
    return { status: 200, body: { repinned: false, dropped: 0 } };
  };
  // MIGRATE (#204): terminate-and-resubmit across graph versions. Requires BOTH start AND cancel
  // (migrate = terminate + start), gated before any body/project work. Resolution-gated (the
  // current version's plan is derived, like start). Addressed exactly like cancel/review:
  // `execution_id` + OPTIONAL `run_id` in the body — omitted, the current run is targeted.
  const migrateHandler: RouteHandler = async (request, actor) => {
    requirePermission(actor, "start");
    requirePermission(actor, "cancel");
    const body = validateBody(request.body(), {
      environment_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      execution_id: { required: true, check: (v) => (isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      run_id: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      abandon_gates: { required: false, check: (v) => (typeof v === "boolean" ? { ok: true } : { ok: false, msg: "Input should be a valid boolean" }) },
      dry_run: { required: false, check: (v) => (typeof v === "boolean" ? { ok: true } : { ok: false, msg: "Input should be a valid boolean" }) },
      reason: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
      policy_ids: { required: false, check: (v) => (isStringArray(v) ? { ok: true } : { ok: false, msg: "Input should be a valid array" }) },
      expected_policy_hash: { required: false, check: (v) => (v === null || isString(v) ? { ok: true } : { ok: false, msg: "Input should be a valid string" }) },
    });
    registry.requireResolvable(request.params["project"]);
    const cp = controlPlaneFor(request.params["project"]);
    const receipt = await cp.migrate(
      request.params["workflow_id"]!,
      body["environment_id"] as string,
      body["execution_id"] as string,
      {
        ...(body["run_id"] !== undefined ? { runId: body["run_id"] as string | null } : {}),
        ...(body["abandon_gates"] !== undefined ? { abandonGates: body["abandon_gates"] as boolean } : {}),
        ...(body["dry_run"] !== undefined ? { dryRun: body["dry_run"] as boolean } : {}),
        ...(body["reason"] !== undefined ? { reason: body["reason"] as string | null } : {}),
        ...(body["policy_ids"] !== undefined ? { policyIds: body["policy_ids"] as string[] } : {}),
        ...(body["expected_policy_hash"] !== undefined ? { expectedPolicyHash: body["expected_policy_hash"] as string | null } : {}),
      },
    );
    return { status: 200, body: receipt };
  };
  const operationRoutes: Array<{ path: string; handler: RouteHandler }> = [
    { path: dualMounted("/workflows/{workflow_id}/start"), handler: startHandler },
    { path: dualMounted("/workflows/{workflow_id}/review"), handler: reviewHandler },
    { path: dualMounted("/workflows/{workflow_id}/cancel"), handler: cancelHandler },
    { path: dualMounted("/workflows/{workflow_id}/migrate"), handler: migrateHandler },
    { path: dualMounted("/workflows/{workflow_id}/repin"), handler: repinHandler },
  ];

  // The auth boundary wraps every route at dispatch (Python's router-level dependency):
  // resolve the actor once, gate `inspect` (any grant implies it), hand the actor down.
  const authorizer = context.authorizer ?? new OpenAuthorizer();
  const dispatch = (handler: RouteHandler) => {
    return (request: RouteRequest): RouteResponse | Promise<RouteResponse> => {
      const actor = authorizer.authorize(request.headers);
      requirePermission(actor, "inspect");
      return handler(request, actor);
    };
  };

  const routes: Route[] = [];
  for (const { path, handler } of readRoutes) {
    routes.push({ method: "GET", segments: splitPath(`/api/v1${path}`), handler: dispatch(handler) });
    routes.push({
      method: "GET",
      segments: splitPath(`/api/v1/projects/{project}${path}`),
      handler: dispatch(handler),
    });
  }
  for (const { path, handler } of operationRoutes) {
    routes.push({ method: "POST", segments: splitPath(`/api/v1${path}`), handler: dispatch(handler) });
    routes.push({
      method: "POST",
      segments: splitPath(`/api/v1/projects/{project}${path}`),
      handler: dispatch(handler),
    });
  }
  // The project listing mounts once (no default-vs-prefixed dual form); refresh targets a
  // specific project explicitly (Python registers both on the app, not the dual-mount router).
  routes.push({
    method: "GET",
    segments: splitPath("/api/v1/projects"),
    handler: dispatch(() => ({ status: 200, body: registry.summaries() })),
  });
  routes.push({
    method: "POST",
    segments: splitPath("/api/v1/projects/{project}/refresh"),
    handler: dispatch((request, actor) => {
      // Operator-triggered project refresh (Python `refresh_project` → `ProjectRefreshResult`). The
      // permission gate FIRST, then the registry re-fetch (local-only in this slice: a no-op that
      // returns the honest local-checkout DTO; an unknown project id 404s via `registry.refresh`).
      requirePermission(actor, "project.refresh");
      const result = registry.refresh(request.params["project"]!);
      return { status: 200, body: result };
    }),
  });
  return routes;
}
