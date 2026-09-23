"""Control-plane HTTP API (#241): the read tier.

A thin, versioned FastAPI adapter over the existing
``typeflux.project`` functions — routing, serialization, and error
mapping only; it performs no project/YAML resolution of its own. The project
manifest is re-read per request so the console always reflects the YAML on
disk; the traffic is human-paced, so freshness beats caching.

Requires the ``api`` extra: ``pip install 'typeflux[api]'``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shlex
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from fastapi.routing import APIRoute
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ImportError as exc:  # pragma: no cover - exercised only without the extra.
    raise ImportError(
        "the control-plane HTTP API requires the 'api' extra: pip install 'typeflux[api]'"
    ) from exc

from pydantic import BaseModel, ConfigDict, ValidationError

from typeflux.controlplane.auth import (
    Actor,
    Authorizer,
    OpenAuthorizer,
    Permission,
    ProxyHeaderAuthorizer,
)
from typeflux.controlplane.git_source import scrub_git_url
from typeflux.controlplane.registry import (
    ProjectRefreshResult,
    ProjectRegistry,
    ProjectSummary,
    UnsupportedProjectRuntimeError,
    load_project_registry,
)
from typeflux.controlplane.resolver import (
    InProcessPythonResolver,
    Resolver,
    ResolverOperationError,
)
from typeflux.core.contracts import ReviewCommand
from typeflux.core.errors import LifecycleBindingError, TypefluxError
from typeflux.observability.inspect import _as_utc_aware
from typeflux.project import (
    BINDING_PROFILE_FOR_RUNTIME,
    CATALOG_VERSION,
    ActivityCatalog,
    DeploymentPlan,
    EnforcementEventList,
    EnforcementReadResult,
    EnvironmentDefinition,
    GithubProvenance,
    GithubReadResult,
    PlanVerification,
    PolicyDefinition,
    PolicySummary,
    ProfileDefinition,
    ProfileSummary,
    ProjectAnnotations,
    ProjectValidationReport,
    ResolvedWorkflowBundle,
    RuntimePinInfo,
    TypefluxProjectSpec,
    WorkflowConnections,
    WorkflowDrainStatus,
    WorkflowExecutionList,
    WorkflowMigrateResult,
    WorkflowOperations,
    WorkflowOperationStatus,
    WorkflowPromptStatus,
    WorkflowRunCorrelation,
    WorkflowStartReceipt,
    WorkflowTaskQueueWorkers,
    environment_definition,
    load_project_annotations,
    policy_definition,
    policy_definitions,
    profile_definition,
    profile_definitions,
    verify_deployment_plan,
    workflow_connections,
    workflow_drain_status,
    workflow_executions,
    workflow_run_correlation,
    workflow_task_queue_workers,
)
from typeflux.project.bundle import BUNDLE_VERSION
from typeflux.project.deployments import (
    PlanDeployment,
    PlanIdentity,
    PlanMismatch,
    PlanPolicy,
    PlanPreflight,
    read_deployment_plan_dir,
)
from typeflux.project.enforcement import (
    DEFAULT_LIMIT as ENFORCEMENT_DEFAULT_LIMIT,
)
from typeflux.project.enforcement import (
    MAX_LIMIT as ENFORCEMENT_MAX_LIMIT,
)
from typeflux.project.enforcement import (
    build_enforcement_feed,
    decode_cursor,
    default_langfuse_enforcement_reader,
    filter_fingerprint,
    observer_from_report,
    resolve_window,
)
from typeflux.project.github_provenance import (
    PLAN_PR_LOOKUP_CAP,
    PlanRef,
    ServedProvenance,
    build_github_provenance,
    default_github_reader,
    parse_github_repo,
    plan_refs,
    served_provenance,
)

try:
    from temporalio.service import RPCError, RPCStatusCode
except ModuleNotFoundError:  # pragma: no cover - temporalio is a core dependency.
    RPCError = None  # type: ignore[assignment, misc]
    RPCStatusCode = None  # type: ignore[assignment, misc]

API_VERSION: Literal["1"] = "1"
API_TITLE = "Typeflux Control Plane API"

#: The enforcement-events runtime transport seam (#723): given the resolved
#: observer and a bounded window, it returns an ``EnforcementReadResult``
#: (reachability status + traces). The default reads Langfuse from the process
#: environment; tests inject a fixture. Keyword-only, matching the seam's call.
EnforcementReader = Callable[..., EnforcementReadResult]

#: The github-provenance transport seam (#727): given the served repo/branch/sha and the
#: capped set of plan commit shas, it returns a ``GithubReadResult`` (reachability status
#: + remote HEAD + behind-count + resolved PRs). The default reads GitHub from the process
#: token; tests inject a fixture. Keyword-only, matching the seam's call.
GithubReader = Callable[..., GithubReadResult]


class ApiError(BaseModel):
    """Error body for every non-2xx response.

    `error` is the normative discriminant (#617): `NotFound` (404),
    `InvalidRequest` (422, malformed request surface),
    `RequestValidationError` (422, missing/invalid parameters),
    `Forbidden` (403), `TemporalUnavailable` (503, bounded Temporal-tier
    call failed or timed out), `LifecycleBindingError` (409, execution is
    not the workflow/project this route binds to), `UnsupportedRuntime`
    (501, the project's declared runtime has no resolver on this server —
    resolution-dependent operations only; pure-YAML reads stay available),
    `HTTPError` (other HTTP failures). Configuration and runtime failures
    carry the raising
    exception's class name (422 for config/validation errors, 500
    otherwise), e.g. `ProjectProfileError`. `message` is informative prose
    and must not embed server-implementation source locations.
    Unauthenticated and unauthorized callers both receive 403 `Forbidden` —
    there is no 401, and token validity is never disclosed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: str
    message: str


class ApiCapabilities(BaseModel):
    """What the caller can actually do against the routed project (#619):
    the actor's grants intersected with the server's abilities. Starting
    needs resolution (the plan/runtime is built from project code);
    review/cancel need only a binding driver for the project's profile
    (#618: the ts-plan-argument driver operates TS executions plan-lessly),
    so an unresolvable-but-operable project reviews and cancels honestly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    can_start: bool
    can_review: bool
    can_cancel: bool
    can_refresh_project: bool
    can_resolve: bool
    #: Whether the enforcement-events feed (#723) actually WORKS for this project,
    #: so the console can feature-detect it. Both editions implement the endpoint
    #: (Python and TS #723 slice 2), but it is resolution-bound (admission verdicts
    #: are derived from the resolved validation report) and 501s behind
    #: `_require_resolvable` for an unresolvable project — so the flag follows
    #: `resolvable`, exactly like can_start/can_resolve, and never advertises a route
    #: that would 501.
    enforcement_events: bool
    #: Whether the github-provenance surface (#727) has something to serve for this
    #: project, so the console can feature-detect it. The Python edition implements the
    #: endpoint, but it is resolution-bound (it 501s behind `_require_resolvable`, like
    #: enforcement_events) AND needs recorded GitHub repo provenance to compare — the
    #: server-side TOKEN is runtime config, not surface, so it stays out of the capability
    #: and is reflected in `partial.github` (not_configured) instead, exactly as
    #: enforcement_events reflects Langfuse config in `partial.langfuse`. So the flag is
    #: `resolvable AND a github repo source is recorded`; the TS edition (#727 slice 2)
    #: reports False everywhere until it lands.
    github_provenance: bool

    @classmethod
    def for_actor(
        cls,
        actor: Actor,
        *,
        resolvable: bool = True,
        operable: bool = True,
        github_repo_present: bool = False,
    ) -> ApiCapabilities:
        return cls(
            can_start=actor.can(Permission.START) and resolvable,
            can_review=actor.can(Permission.REVIEW) and operable,
            can_cancel=actor.can(Permission.CANCEL) and operable,
            can_refresh_project=actor.can(Permission.PROJECT_REFRESH),
            can_resolve=resolvable,
            enforcement_events=resolvable,
            github_provenance=resolvable and github_repo_present,
        )


class ApiMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["1"]
    bundle_version: Literal["1"]
    catalog_version: Literal["1"]
    project: str
    manifest_path: str
    #: The routed project's declared language runtime (#619).
    runtime: Literal["python", "typescript"]
    #: The authenticated caller's principal, so the console can attribute reviews
    #: to the proxy-auth identity instead of free text (#577). Sourced ONLY from
    #: the trusted proxy-auth path: it is the ``--trust-proxy-auth`` actor header
    #: when that mode is active, and ``None`` in every other mode (token / open) —
    #: a token grant *name* is config, not an identity, and an untrusted actor
    #: header is spoofable, so neither is ever echoed here.
    caller_identity: str | None = None
    capabilities: ApiCapabilities


class ApiWorkflowSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    path: str | None = None
    directory: str | None = None
    profiles: dict[str, str]


class ApiWorkflowList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflows: tuple[ApiWorkflowSummary, ...] = ()


class ApiEnvironmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    path: str


class ApiEnvironmentList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environments: tuple[ApiEnvironmentSummary, ...] = ()


class ApiStartRequest(BaseModel):
    """Start one execution of a resolved workflow version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_id: str
    #: Temporal workflow id for the new execution (caller-chosen, e.g. a
    #: case or ticket identifier).
    execution_id: str
    #: Workflow input payload; validated against the workflow's input model.
    input: dict[str, Any]
    task_queue: str | None = None
    policy_ids: tuple[str, ...] = ()
    expected_policy_hash: str | None = None


class ApiReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_id: str
    execution_id: str
    run_id: str | None = None
    #: Reviewer identity and notes are kept out of ``typeflux.*`` metadata and
    #: execution manifests, but they are NOT client-side: the whole
    #: ``ReviewCommand`` is sent as the Temporal review signal payload and so
    #: persists in workflow history (visible to anyone who can read history).
    #: Send only data appropriate to retain there; see #325.
    command: ReviewCommand
    policy_ids: tuple[str, ...] = ()
    expected_policy_hash: str | None = None


class ApiCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_id: str
    execution_id: str
    run_id: str | None = None
    #: Sent as the Temporal cancel signal payload (persists in workflow history)
    #: and round-tripped into ``WorkflowLifecycleStatus.cancellation_reason``,
    #: which any ``inspect`` caller can read. Not client-side; avoid sensitive
    #: free text here (#325).
    reason: str | None = None
    policy_ids: tuple[str, ...] = ()
    expected_policy_hash: str | None = None


class ApiMigrateRequest(BaseModel):
    """Terminate a running execution and resubmit it against the current version.

    The long-drain primitive (#204): terminate-and-resubmit with input
    carry-over. Addressing follows the cancel/review convention exactly:
    ``execution_id`` (the Temporal workflow id) plus an OPTIONAL ``run_id`` —
    omitted, the operation targets the execution's current run. The new run
    reuses the same workflow id. Requires START and CANCEL — migration is a
    terminate followed by a start.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_id: str
    #: The Temporal workflow id of the execution to migrate; the new run reuses it.
    execution_id: str
    #: The specific run to migrate; omitted targets the current run (the
    #: cancel/review addressing convention).
    run_id: str | None = None
    #: Acknowledge dropping a pending review gate. Without it, an execution parked
    #: at a gate is refused (422) so the human decision is never silently lost.
    abandon_gates: bool = False
    #: Operator note appended to the canonical termination reason (persists in
    #: workflow history; keep it non-sensitive, #325).
    reason: str | None = None
    policy_ids: tuple[str, ...] = ()
    expected_policy_hash: str | None = None
    #: Preview (#791): run every preflight and return the would-migrate identity
    #: (``dry_run: true``, ``new_run_id: null``) without terminating anything.
    dry_run: bool = False


class ApiRepinRequest(BaseModel):
    """Drop the pinned operations runtime for a workflow so it re-pins (#324)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment_id: str


class ApiRepinResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: True when at least one pinned runtime was dropped (it will re-resolve on
    #: the next mutating call); False when nothing was pinned for this selection.
    repinned: bool
    #: How many pinned entries were dropped (one per distinct policy selection).
    dropped: int


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ApiError, "description": "Unknown workflow or environment id."},
    422: {"model": ApiError, "description": "Configuration or validation error."},
    500: {"model": ApiError, "description": "Unexpected runtime failure."},
}

# Temporal-tier endpoints are bounded (#581): one call against an unreachable
# cluster must never hold the process-wide env-resolution lock (and with it
# the whole read tier) for the duration of a connect retry loop.
_TEMPORAL_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_ERROR_RESPONSES,
    503: {
        "model": ApiError,
        "description": "Temporal-tier call timed out; the cluster is unreachable or slow.",
    },
}

# Lifecycle operations (status/review/cancel) additionally bind to the routed
# workflow+project before dispatch (#320); a mismatch is a 409 conflict.
_LIFECYCLE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_TEMPORAL_ERROR_RESPONSES,
    403: {
        "model": ApiError,
        "description": "Caller lacks the permission this operation requires.",
    },
    409: {
        "model": ApiError,
        "description": "Execution is not the bound workflow/project for this route.",
    },
}

# Resolution-dependent routes (#619): the project's declared runtime may have
# no resolver on this server — those routes additionally answer 501.
_UNSUPPORTED_RUNTIME_RESPONSE: dict[int | str, dict[str, Any]] = {
    501: {
        "model": ApiError,
        "description": "The project's declared runtime has no resolver on this server.",
    },
}

#: Ceiling for one Temporal-tier call (operations build/pin, start, status,
#: review, cancel, executions, workers, drain). On timeout the call is
#: cancelled — unwinding the env-resolution lock — and the endpoint answers
#: 503 (#581). Overridable per deployment via the environment.
TEMPORAL_TIER_TIMEOUT_ENV = "TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS"
_TEMPORAL_TIER_TIMEOUT_DEFAULT = 10.0

_T = TypeVar("_T")


@dataclass
class _PinnedOperations:
    """A pinned ``WorkflowOperations`` plus the version it was pinned at (#324).

    Recording the resolved spec digest and pin time at first use lets the status
    endpoint report which runtime version mutating ops are bound to, and the
    repin endpoint drop it on demand — without re-resolving YAML on every poll.
    """

    operations: WorkflowOperations
    spec_digest: str | None
    pinned_at: str


@asynccontextmanager
async def _operations_lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    cache = app.state.operations_cache
    for pinned in cache.values():
        pinned.operations.shutdown()
    cache.clear()
    # Close closeable resolvers (#642): a SubprocessResolver owns a long-lived
    # child process (its own process group) — app shutdown must not orphan it.
    for candidate in getattr(app.state, "resolvers", ()):
        close = getattr(candidate, "close", None)
        if callable(close):
            close()


# Set per request by the project-scoped route class to the matched
# ``{project}`` path segment (None for the default-alias routes), then read by
# ``_project()``. A ContextVar (not request state) so it propagates into both
# async endpoints and sync endpoints dispatched to the threadpool.
_current_project_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "typeflux_current_project_id", default=None
)


def _project_scoped_route_class() -> type[APIRoute]:
    class _ProjectScopedRoute(APIRoute):
        """Bind the matched ``{project}`` path segment for the handler's scope.

        The bind happens in the same execution context that runs the endpoint,
        so ``_current_project_id`` is visible to both async handlers and sync
        handlers (Starlette copies the context into the threadpool).
        """

        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            original = super().get_route_handler()

            async def scoped(request: Request) -> Response:
                token = _current_project_id.set(request.path_params.get("project"))
                try:
                    return await original(request)
                finally:
                    _current_project_id.reset(token)

            return scoped

    return _ProjectScopedRoute


def create_app(
    manifest_path: str | Path,
    *,
    cors_origins: tuple[str, ...] = (),
    authorizer: Authorizer | None = None,
    resolvers: Sequence[Resolver] | None = None,
    temporal_tier_timeout_seconds: float | None = None,
    enforcement_reader: EnforcementReader | None = None,
    github_reader: GithubReader | None = None,
) -> FastAPI:
    """Build the control-plane API bound to one project manifest.

    The single-manifest server is the degenerate case of the project registry
    (#256): a registry of one whose sole entry is the default alias, served at
    the unprefixed ``/api/v1/...`` routes. ``cors_origins`` is opt-in for a
    console served from another origin; the dev console proxies ``/api``.
    ``authorizer`` gates operations (#292); the default trusts the caller.
    ``enforcement_reader`` is the injectable Langfuse transport seam for the
    enforcement-events feed (#723); tests replace it with a fixture.
    ``github_reader`` is the injectable GitHub transport seam for the
    github-provenance surface (#727); tests replace it with a fixture.
    """
    return _build_app(
        ProjectRegistry.single(manifest_path),
        cors_origins=cors_origins,
        authorizer=authorizer,
        resolvers=resolvers,
        temporal_tier_timeout_seconds=temporal_tier_timeout_seconds,
        enforcement_reader=enforcement_reader,
        github_reader=github_reader,
    )


def create_app_from_registry(
    registry_path: str | Path,
    *,
    cors_origins: tuple[str, ...] = (),
    clone_cache: str | Path | None = None,
    authorizer: Authorizer | None = None,
    resolvers: Sequence[Resolver] | None = None,
    temporal_tier_timeout_seconds: float | None = None,
    enforcement_reader: EnforcementReader | None = None,
    github_reader: GithubReader | None = None,
) -> FastAPI:
    """Build the control-plane API serving every project in a registry file.

    The default project is also served at the unprefixed ``/api/v1/...``
    routes; every project (including the default) is served at
    ``/api/v1/projects/{project}/...``. ``clone_cache`` overrides where
    Git-sourced projects are cloned. ``authorizer`` gates operations (#292).
    ``enforcement_reader`` is the injectable Langfuse transport seam for the
    enforcement-events feed (#723). ``github_reader`` is the injectable GitHub
    transport seam for the github-provenance surface (#727).
    """
    registry = load_project_registry(registry_path, cache_dir=clone_cache)
    return _build_app(
        registry,
        cors_origins=cors_origins,
        authorizer=authorizer,
        resolvers=resolvers,
        temporal_tier_timeout_seconds=temporal_tier_timeout_seconds,
        enforcement_reader=enforcement_reader,
        github_reader=github_reader,
    )


def _build_app(
    registry: ProjectRegistry,
    *,
    cors_origins: tuple[str, ...] = (),
    authorizer: Authorizer | None = None,
    resolvers: Sequence[Resolver] | None = None,
    temporal_tier_timeout_seconds: float | None = None,
    enforcement_reader: EnforcementReader | None = None,
    github_reader: GithubReader | None = None,
) -> FastAPI:
    # The resolution seam (#619), per-runtime multiplexed (#642): the
    # language-bound resolution operations go through one contract-shaped
    # Resolver PER RUNTIME; the default is the in-process Python resolver
    # alone. The supported-runtime gate derives from the serving resolvers —
    # an injected resolver's projects are honestly resolvable, everything
    # else fails closed (501 UnsupportedRuntime).
    resolver_seq = tuple(resolvers) if resolvers else (InProcessPythonResolver(),)
    resolver_map: dict[str, Resolver] = {}
    for candidate in resolver_seq:
        if candidate.runtime in resolver_map:
            raise ValueError(
                f"duplicate resolver for runtime {candidate.runtime!r}: a control "
                f"plane holds exactly one resolver per runtime (#642)"
            )
        resolver_map[candidate.runtime] = candidate
    supported_runtimes = frozenset(resolver_map)

    def _resolution() -> Resolver:
        # Dispatch by the ROUTED project's declared runtime. Only reachable
        # behind _require_resolvable, which guarantees the runtime is served.
        entry = registry.entry(_current_project_id.get())
        return resolver_map[entry.runtime]

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=(
            "Control-plane view of one Typeflux project: validation, resolved "
            "workflow bundles (including the nodes+edges topology projection), "
            "the activity catalog, the cross-version drain view, and "
            "start/status/review/cancel operations. Responses carry the same "
            "secret-free contract JSON as the project CLI."
        ),
        lifespan=_operations_lifespan,
    )
    app.state.registry = registry
    app.state.authorizer = authorizer if authorizer is not None else OpenAuthorizer()
    # The enforcement-events transport seam (#723): the default queries Langfuse
    # from the process environment; tests inject a fixture reader. The default
    # caches its Langfuse backend once per app (via `enforcement_backend`, like
    # operations_cache) instead of constructing a fresh client per request — the
    # backend depends only on the process env, so once-per-app is enough (an env
    # change mid-process is out of scope; it is fixed at serve time).
    app.state.enforcement_backend = {}
    if enforcement_reader is not None:
        app.state.enforcement_reader = enforcement_reader
    else:

        def _cached_enforcement_reader(**kwargs: Any) -> EnforcementReadResult:
            return default_langfuse_enforcement_reader(
                **kwargs, backend_cache=app.state.enforcement_backend
            )

        app.state.enforcement_reader = _cached_enforcement_reader
    # The github-provenance transport seam (#727): the default reads GitHub from the
    # process token; tests inject a fixture reader. No per-app backend is cached — the
    # default reader (`_GithubHttp`) is a stateless token holder built per request, so there
    # is nothing to amortize and no stale-state trap to keep alive.
    app.state.github_reader = github_reader if github_reader is not None else default_github_reader
    app.state.operations_cache = {}
    # The lifespan closes closeable resolvers on shutdown (#642) — a
    # SubprocessResolver's child process must not outlive the app.
    app.state.resolvers = tuple(resolver_map.values())
    # Serializes operations *builds* so a pinned runtime is created once per
    # cache key. The env-context lock itself is held deadlock-free across the
    # build's await by async_project_environment_context (it acquires in a
    # worker thread), independent of this lock.
    app.state.operations_lock = asyncio.Lock()
    app.state.temporal_tier_timeout = (
        temporal_tier_timeout_seconds
        if temporal_tier_timeout_seconds is not None
        else float(os.environ.get(TEMPORAL_TIER_TIMEOUT_ENV, _TEMPORAL_TIER_TIMEOUT_DEFAULT))
    )

    async def _temporal_bounded(awaitable: Coroutine[Any, Any, _T], *, what: str) -> _T:
        # Every Temporal-tier await is bounded (#581): cancellation on timeout
        # unwinds async_project_environment_context, so the process-wide
        # env-resolution lock is freed at the bound instead of at the mercy of
        # a connect retry loop — reads never starve behind a dead cluster.
        timeout = app.state.temporal_tier_timeout
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{what} did not complete within {timeout:g}s; the call was cancelled "
                    "so it cannot stall other requests. Check that the Temporal cluster in "
                    "this environment's runtime config is reachable."
                ),
            ) from None
        except RuntimeError as exc:
            # temporalio surfaces a refused/failed connect as a bare
            # RuntimeError("Failed client connect: ..."); that is cluster
            # unavailability, not a server bug — answer 503, not 500.
            if "client connect" not in str(exc):
                raise
            raise HTTPException(
                status_code=503,
                detail=f"{what} failed: {exc}",
            ) from exc
        except Exception as exc:
            # An already-pinned client surfaces an outage as a gRPC RPCError
            # (UNAVAILABLE / DEADLINE_EXCEEDED), not a connect RuntimeError —
            # the same cluster unavailability, the same 503. Every other RPC
            # status keeps its existing mapping.
            if (
                RPCError is not None
                and isinstance(exc, RPCError)
                and exc.status in (RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED)
            ):
                raise HTTPException(
                    status_code=503,
                    detail=f"{what} failed: Temporal RPC {exc.status.name}: {exc.message}",
                ) from exc
            raise

    def _authorize(request: Request) -> Actor:
        # The actor drives authorization and the console capability report only;
        # it never enters typeflux.* execution metadata (#292).
        return request.app.state.authorizer.authorize(request)

    def require(permission: Permission) -> Callable[..., Actor]:
        # Depends lives in the default (a runtime value), not the annotation:
        # `from __future__ import annotations` stringifies annotations, and these
        # dependency callables are closure-locals get_type_hints can't resolve.
        def dependency(actor: Actor = Depends(_authorize)) -> Actor:
            if not actor.can(permission):
                raise HTTPException(
                    status_code=403,
                    detail=f"operation requires the {permission.value!r} permission",
                )
            return actor

        return dependency

    def require_all(*permissions: Permission) -> Callable[..., Actor]:
        # An operation that is more than one primitive requires EVERY listed
        # permission (migrate = terminate + start ⇒ CANCEL and START). Fails
        # closed on the first missing grant with the same 403 shape as `require`.
        def dependency(actor: Actor = Depends(_authorize)) -> Actor:
            for permission in permissions:
                if not actor.can(permission):
                    raise HTTPException(
                        status_code=403,
                        detail=f"operation requires the {permission.value!r} permission",
                    )
            return actor

        return dependency

    # Every route needs at least inspect; mutating routes additionally declare
    # their specific permission below. _authorize is shared, so it resolves the
    # actor once per request.
    require_inspect = require(Permission.INSPECT)

    # Routes are defined once on this router and mounted twice: unprefixed for
    # the default project, and under /projects/{project} for the full set.
    router = APIRouter(
        route_class=_project_scoped_route_class(),
        dependencies=[Depends(require_inspect)],
    )
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.exception_handler(LifecycleBindingError)
    async def _binding_error(request: Request, exc: LifecycleBindingError) -> JSONResponse:
        # A lifecycle op addressed an execution that is not the workflow/project
        # this route binds to (#320): a routing/identity conflict, not a bad
        # request. Fail closed with 409 — the op never reached the execution.
        return JSONResponse(
            status_code=409,
            content=ApiError(error=type(exc).__name__, message=str(exc)).model_dump(),
        )

    @app.exception_handler(UnsupportedProjectRuntimeError)
    async def _unsupported_runtime(
        request: Request, exc: UnsupportedProjectRuntimeError
    ) -> JSONResponse:
        # The server understands the request and cannot serve it by
        # construction (#619): resolution is language-bound and this server
        # has no resolver for the project's declared runtime. 501, fail
        # closed — never garbage from a wrong-runtime import attempt.
        return JSONResponse(
            status_code=501,
            content=ApiError(error="UnsupportedRuntime", message=str(exc)).model_dump(),
        )

    @app.exception_handler(ResolverOperationError)
    async def _resolver_operation_error(
        request: Request, exc: ResolverOperationError
    ) -> JSONResponse:
        # A structured subprocess-resolver failure IS the wire object (and
        # status) the resolver's own edition would have served — forward it
        # verbatim, never translate (resolver.v1.json error_shape, #642).
        return JSONResponse(
            status_code=exc.status,
            content=ApiError(error=exc.error, message=exc.message).model_dump(),
        )

    @app.exception_handler(TypefluxError)
    async def _typeflux_error(request: Request, exc: TypefluxError) -> JSONResponse:
        # Config/validation errors derive from ValueError (see tests/test_errors.py);
        # anything else Typeflux-rooted is an unexpected runtime failure.
        status_code = 422 if isinstance(exc, ValueError) else 500
        return JSONResponse(
            status_code=status_code,
            content=ApiError(error=type(exc).__name__, message=str(exc)).model_dump(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error = {
            403: "Forbidden",
            404: "NotFound",
            422: "InvalidRequest",
            503: "TemporalUnavailable",
        }.get(exc.status_code, "HTTPError")
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiError(error=error, message=str(exc.detail)).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Language-neutral message (#617): FastAPI's str(exc) appends the
        # endpoint's source location (File "…/api.py", line N) — noise no
        # other server edition can reproduce and the contract forbids.
        errors = exc.errors()
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in errors
        )
        plural = "s" if len(errors) != 1 else ""
        return JSONResponse(
            status_code=422,
            content=ApiError(
                error="RequestValidationError",
                message=f"{len(errors)} validation error{plural}: {detail}",
            ).model_dump(),
        )

    def _project() -> TypefluxProjectSpec:
        # The project-scoped route class bound the matched {project} segment
        # (None ⇒ default). Re-read the manifest per request so the console
        # reflects the YAML on disk.
        project_id = _current_project_id.get()
        try:
            return registry.resolve(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown project: {project_id}") from None

    def _entry_operable(runtime: str) -> bool:
        # A project is OPERABLE when its binding driver can run here (#618):
        # the ts-plan-argument driver is plan-less (needs no resolution), but
        # the python-versioned-type driver builds an in-process runtime —
        # module imports — so it additionally requires this app's resolver to
        # cover the runtime (gate-follows-resolver, #619).
        if runtime == "typescript":
            return True
        return runtime in supported_runtimes and runtime in BINDING_PROFILE_FOR_RUNTIME

    def _require_operable() -> None:
        # Lifecycle operations (status/review/cancel) need a binding DRIVER,
        # not resolution (#618 slice 4). Unknown projects still 404 here; a
        # driver this app cannot run fails closed with the declared 501.
        project_id = _current_project_id.get()
        try:
            entry = registry.entry(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown project: {project_id}") from None
        if not _entry_operable(entry.runtime):
            raise UnsupportedProjectRuntimeError(
                f"project {entry.id!r} declares runtime {entry.runtime!r}, whose "
                "binding driver requires resolution this server does not provide "
                f"(supported: {', '.join(sorted(supported_runtimes))})"
            )

    def _require_resolvable() -> None:
        # Gate for the language-bound routes (the resolver-contract closure:
        # the four resolution operations, identity-deriving executions/
        # versions/workers/connections/correlation, deployment verification,
        # and the operate tier). Pure-YAML reads stay available — an
        # unresolvable project is inspectable. Runs before _project(), so an
        # unknown project id must map to the same 404 here.
        project_id = _current_project_id.get()
        try:
            registry.require_resolvable(project_id, supported=supported_runtimes)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown project: {project_id}") from None

    def _entry_runtime() -> str:
        return registry.entry(_current_project_id.get()).runtime

    def _require_workflow(project: TypefluxProjectSpec, workflow_id: str) -> None:
        if workflow_id not in {workflow.id for workflow in project.workflows}:
            raise HTTPException(
                status_code=404,
                detail=f"unknown project workflow: {workflow_id}",
            )

    def _require_environment(project: TypefluxProjectSpec, environment_id: str) -> None:
        if environment_id not in project.environments:
            raise HTTPException(
                status_code=404,
                detail=f"unknown project environment: {environment_id}",
            )

    def _served_github_provenance() -> ServedProvenance | None:
        # The served side of the github-provenance drift comparison (#727), built from
        # the registry's recorded git-source provenance: the declared repo url/ref plus
        # the current clone HEAD sha (None for a local mount — which records no source, so
        # this is None and the surface reports not_configured with no network call).
        project_id = _current_project_id.get()
        repo = registry.repo_source(project_id)
        if repo is None:
            return None
        # Gate on the GitHub host FIRST: a non-github (local/GHE/ssh) source is not served
        # here, and resolving `repo_head_sha` forks `git rev-parse` — so the host check has
        # to short-circuit BEFORE that subprocess, or every request on a non-github project
        # would fork git for nothing. `served_provenance` re-parses (cheap, network-free).
        scrubbed = scrub_git_url(repo.url)
        if parse_github_repo(scrubbed) is None:
            return None
        return served_provenance(
            repo_url=scrubbed,
            repo_ref=repo.ref,
            repo_sha=registry.repo_head_sha(project_id),
        )

    def _github_repo_present() -> bool:
        # Whether a GitHub repo source is recorded for the project — the surface factor of
        # the `github_provenance` capability (the other is `resolvable`, ANDed in
        # for_actor). Cheap and network-free: it only parses the declared repo url's host,
        # never reads the clone or GitHub. A local mount / non-github (GHE) host is false.
        repo = registry.repo_source(_current_project_id.get())
        return repo is not None and parse_github_repo(scrub_git_url(repo.url)) is not None

    def _caller_identity(request: Request, actor: Actor) -> str | None:
        # The authenticated principal is surfaced ONLY behind a TRUSTED proxy
        # (--trust-proxy-auth) — the sole mode where the actor id is an identity
        # the operator vouches for (#577). In token mode the actor id is a grant
        # *name* (config, not an identity) and in open mode it is None; neither is
        # echoed. The proxy authorizer already maps an EMPTY actor header to None,
        # so an empty header reads as no identity in both editions.
        authorizer = request.app.state.authorizer
        if not isinstance(authorizer, ProxyHeaderAuthorizer):
            return None
        return actor.id

    @router.get("/meta", response_model=ApiMeta)
    def meta(request: Request, actor: Actor = Depends(require_inspect)) -> ApiMeta:
        project = _project()
        entry = registry.entry(_current_project_id.get())
        return ApiMeta(
            api_version=API_VERSION,
            bundle_version=BUNDLE_VERSION,
            catalog_version=CATALOG_VERSION,
            project=project.name,
            manifest_path=str(project.manifest_path),
            runtime=entry.runtime,
            caller_identity=_caller_identity(request, actor),
            capabilities=ApiCapabilities.for_actor(
                actor,
                resolvable=registry.entry_runtime_resolvable(
                    _current_project_id.get(), supported=supported_runtimes
                ),
                operable=_entry_operable(entry.runtime),
                github_repo_present=_github_repo_present(),
            ),
        )

    @router.get("/workflows", response_model=ApiWorkflowList)
    def workflows() -> ApiWorkflowList:
        project = _project()
        return ApiWorkflowList(
            workflows=tuple(
                ApiWorkflowSummary(
                    id=workflow.id,
                    path=workflow.path,
                    directory=workflow.directory,
                    profiles=dict(workflow.profiles),
                )
                for workflow in project.workflows
            )
        )

    @router.get("/environments", response_model=ApiEnvironmentList)
    def environments() -> ApiEnvironmentList:
        project = _project()
        return ApiEnvironmentList(
            environments=tuple(
                ApiEnvironmentSummary(id=environment_id, path=path)
                for environment_id, path in sorted(project.environments.items())
            )
        )

    @router.get(
        "/environments/{environment_id}",
        response_model=EnvironmentDefinition,
        responses=_ERROR_RESPONSES,
    )
    def environment_detail(environment_id: str) -> EnvironmentDefinition:
        project = _project()
        _require_environment(project, environment_id)
        return environment_definition(project, environment_id)

    @router.get("/policies", response_model=tuple[PolicySummary, ...])
    def policies() -> tuple[PolicySummary, ...]:
        return policy_definitions(_project())

    @router.get(
        "/policies/{policy_id}",
        response_model=PolicyDefinition,
        response_model_exclude_none=True,
        responses=_ERROR_RESPONSES,
    )
    def policy_detail(policy_id: str) -> PolicyDefinition:
        project = _project()
        if policy_id not in project.policies:
            raise HTTPException(status_code=404, detail=f"unknown project policy: {policy_id}")
        return policy_definition(project, policy_id)

    @router.get("/profiles", response_model=tuple[ProfileSummary, ...])
    def profiles() -> tuple[ProfileSummary, ...]:
        return profile_definitions(_project())

    @router.get(
        "/profiles/{kind}/{profile_id}",
        response_model=ProfileDefinition,
        responses=_ERROR_RESPONSES,
    )
    def profile_detail(kind: str, profile_id: str) -> ProfileDefinition:
        project = _project()
        declared = getattr(project.profiles, kind, {}) if project.profiles is not None else {}
        if kind not in ("provider", "registry", "runtime") or profile_id not in declared:
            raise HTTPException(
                status_code=404, detail=f"unknown project profile: {kind}/{profile_id}"
            )
        return profile_definition(project, kind=kind, profile_id=profile_id)

    @router.get(
        "/annotations",
        response_model=ProjectAnnotations,
        response_model_exclude_none=True,
    )
    def annotations() -> ProjectAnnotations:
        # The insight-acknowledgement annotations projection (#733): the parsed
        # `.typeflux/annotations.yaml` beside the manifest. A PURE-YAML, project-level
        # read — NOT resolution-bound (an unresolvable TS project still has annotations,
        # and there is no seam), so it never 501s and needs no capability flag (it is
        # always servable, exactly like /workflows and /policies). Parsed fail-closed:
        # an absent file is the common case (empty projection), and a malformed file
        # degrades to an EMPTY projection here while surfacing as a validation issue on
        # /validate — never a partial parse. Re-read per request, like every read.
        return load_project_annotations(_project())

    @router.get(
        "/validate",
        response_model=ProjectValidationReport,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def validate(
        environment_id: str | None = None,
        workflow_id: Annotated[list[str] | None, Query()] = None,
        policy_id: Annotated[list[str] | None, Query()] = None,
    ) -> ProjectValidationReport:
        _require_resolvable()
        project = _project()
        if environment_id is not None:
            _require_environment(project, environment_id)
        for requested in workflow_id or ():
            _require_workflow(project, requested)
        return _resolution().validate_project(
            str(project.manifest_path),
            environment_id=environment_id,
            workflow_ids=tuple(workflow_id or ()),
            policy_ids=tuple(policy_id or ()),
        )

    @router.get(
        "/workflows/{workflow_id}/bundle",
        response_model=ResolvedWorkflowBundle,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def bundle(
        workflow_id: str,
        environment_id: str,
        policy_id: Annotated[list[str] | None, Query()] = None,
        deployment_image: str | None = None,
    ) -> ResolvedWorkflowBundle:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        return _resolution().resolve_bundle(
            str(project.manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=tuple(policy_id or ()),
            deployment_image=deployment_image,
        )

    @router.get(
        "/workflows/{workflow_id}/catalog",
        response_model=ActivityCatalog,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def catalog(workflow_id: str, environment_id: str) -> ActivityCatalog:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        return _resolution().resolve_catalog(
            str(project.manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
        )

    async def _operations(
        project: TypefluxProjectSpec,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...],
        expected_policy_hash: str | None,
    ) -> _PinnedOperations:
        # Operations pin the resolved runtime (and its verified policy hash)
        # at first use and reuse the Temporal connection across requests —
        # status polling at the recommended cadence must not re-resolve YAML
        # and reconnect per tick. Restart `serve` (or POST .../repin) to re-pin
        # after YAML edits; the read endpoints stay per-request fresh.
        # The cache key carries the project dimension so a workflow id that
        # exists in two projects never shares a pinned Temporal connection.
        project_id = _current_project_id.get() or registry.default_id
        key = (project_id, workflow_id, environment_id, policy_ids, expected_policy_hash)
        cache = app.state.operations_cache
        if key in cache:
            return cache[key]
        async with app.state.operations_lock:
            if key in cache:
                return cache[key]
            entry_runtime = registry.entry(_current_project_id.get()).runtime
            operations = await WorkflowOperations.for_project_workflow(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                policy_ids=policy_ids,
                expected_policy_hash=expected_policy_hash,
                # The registry runtime selects the binding driver (#618/#619);
                # the runtime gate has already 501'd unresolvable projects,
                # so this is defense-in-depth until the ts driver lands.
                binding_profile=BINDING_PROFILE_FOR_RUNTIME[entry_runtime],
                # The runtime's resolver, when this server holds one (#642):
                # the ts-plan-argument driver starts (resolve_plan) and honors
                # policy selections (resolve_bundle/validate_project) with it;
                # without one, both fail closed exactly as before.
                resolver=resolver_map.get(entry_runtime),
            )
            # A runtime-less driver (ts-plan-argument) has no in-process spec
            # digest — identity lives in the execution memo. Falls back to the
            # facade itself for test fakes without a driver attribute.
            runtime_obj = getattr(getattr(operations, "driver", operations), "runtime", None)
            pinned = _PinnedOperations(
                operations=operations,
                spec_digest=getattr(
                    getattr(runtime_obj, "workflow_class", None),
                    "__typeflux_spec_digest__",
                    None,
                ),
                pinned_at=datetime.now(UTC).isoformat(),
            )
            cache[key] = pinned
            return pinned

    async def _bind_operations(
        project: TypefluxProjectSpec,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...],
        expected_policy_hash: str | None,
    ) -> _PinnedOperations:
        # The one way to obtain a pinned runtime from a route: pinning IS a
        # Temporal-tier call, so the bound is part of the operation — a new
        # endpoint cannot forget it.
        return await _temporal_bounded(
            _operations(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                policy_ids=policy_ids,
                expected_policy_hash=expected_policy_hash,
            ),
            what="pinning the operations runtime",
        )

    async def _drop_pinned_operations(predicate: Callable[[tuple[Any, ...]], bool]) -> int:
        # Drop (and shut down) every pinned runtime whose cache key matches, so
        # the next call re-pins. Cache keys are
        # (project_id, workflow_id, environment_id, policy_ids, expected_policy_hash).
        # Shared by repin (#324) and project refresh so the shutdown/locking
        # semantics stay in one place.
        cache = app.state.operations_cache
        async with app.state.operations_lock:
            keys = [key for key in cache if predicate(key)]
            for key in keys:
                cache.pop(key).operations.shutdown()
        return len(keys)

    def _coerce_input(operations: WorkflowOperations, payload: dict[str, Any]) -> Any:
        # Falls back to the facade itself for test fakes without a driver
        # attribute (the `_operations` pin uses the same idiom).
        driver = getattr(operations, "driver", operations)
        if getattr(driver, "runtime", None) is None:
            # A foreign-edition driver (ts-plan-argument, #673): validate the
            # payload against the workflow's input JSON SCHEMA pinned with the
            # plan (its input model is project CODE in the other language — the
            # schema is the wire-portable contract). A missing schema fails
            # CLOSED, exactly the TS edition's own "starting requires an
            # injected schema" posture — never dispatch unvalidated silently.
            json_schema = getattr(driver, "_input_json_schema", None)
            if not isinstance(json_schema, dict):
                reason = getattr(driver, "_input_schema_error", None)
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "starting requires the workflow's input schema from the "
                        "typescript resolver (give it the project's schemas, e.g. "
                        "--schemas/--conformance-schemas)" + (f": {reason}" if reason else "")
                    ),
                )
            try:
                import jsonschema
            except ImportError as exc:  # pragma: no cover - stale env only
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "this server is missing the jsonschema dependency required "
                        "for foreign-edition input validation — reinstall the api "
                        "extra (pip install 'typeflux[api]')"
                    ),
                ) from exc

            schema_name = getattr(driver, "_input_schema_name", None) or "input"
            try:
                # PINNED to draft 2020-12 (what Zod's z.toJSONSchema targets; the
                # TS emission strips $schema for hash stability, so the draft
                # must not float with jsonschema's default) + a FormatChecker so
                # format-only constraints (z.string().url() → format: uri)
                # actually enforce instead of annotating.
                jsonschema.validate(
                    payload,
                    json_schema,
                    cls=jsonschema.Draft202012Validator,
                    format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
                )
            except jsonschema.ValidationError as exc:
                # The parity prefix both editions use; jsonschema's prose
                # differs from Zod's/pydantic's (behavioral, not byte-identical).
                raise HTTPException(
                    status_code=422,
                    detail=f"invalid workflow input for {schema_name}: {exc.message}",
                ) from exc
            except jsonschema.SchemaError as exc:
                # The pinned schema ITSELF is invalid (a resolver-emission bug):
                # a config-class 422 with the reason — never an opaque 500 that
                # breaks the ApiError contract.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"the workflow's input JSON Schema for {schema_name} is "
                        f"invalid (resolver emission): {exc.message}"
                    ),
                ) from exc
            return payload
        run = getattr(operations.runtime.workflow_class, "run")
        input_model = run.__annotations__["input_value"]
        try:
            return input_model.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"invalid workflow input for {input_model.__name__}: {exc}",
            ) from exc

    @router.post(
        "/workflows/{workflow_id}/start",
        response_model=WorkflowStartReceipt,
        responses={**_TEMPORAL_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def start(
        workflow_id: str,
        request: ApiStartRequest,
        _actor: Actor = Depends(require(Permission.START)),
    ) -> WorkflowStartReceipt:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, request.environment_id)
        pinned = await _bind_operations(
            project,
            workflow_id=workflow_id,
            environment_id=request.environment_id,
            policy_ids=request.policy_ids,
            expected_policy_hash=request.expected_policy_hash,
        )
        input_value = _coerce_input(pinned.operations, request.input)
        return await _temporal_bounded(
            pinned.operations.start(
                input_value,
                workflow_id=request.execution_id,
                task_queue=request.task_queue,
            ),
            what="starting the execution",
        )

    @router.get(
        "/workflows/{workflow_id}/status",
        response_model=WorkflowOperationStatus,
        responses={**_LIFECYCLE_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def status(
        workflow_id: str,
        environment_id: str,
        execution_id: str,
        run_id: str | None = None,
        trace: bool = False,
        policy_id: Annotated[list[str] | None, Query()] = None,
        expected_policy_hash: str | None = None,
        actor: Actor = Depends(require_inspect),
    ) -> WorkflowOperationStatus:
        _require_operable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        # trace=true records a first-class, auditable lifecycle operation in the
        # trace — a write. Read-only means read-only: an inspect-only caller may
        # poll (trace=false) but may not author audit events. Fail loud, never
        # silently downgrade to trace=false (#321).
        if trace and not actor.can_operate():
            raise HTTPException(
                status_code=403,
                detail=(
                    "trace=true status records an auditable lifecycle operation and requires "
                    "an operate-class permission (start/review/cancel/project.refresh); use "
                    "trace=false for read-only status polling"
                ),
            )
        pinned = await _bind_operations(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=tuple(policy_id or ()),
            expected_policy_hash=expected_policy_hash,
        )
        result = await _temporal_bounded(
            pinned.operations.status(execution_id, run_id=run_id, trace=trace),
            what="querying execution status",
        )
        # Surface which pinned runtime version mutating ops are bound to, for
        # visibility (#324). No freshness verdict — see RuntimePinInfo; to refresh
        # a stale pin the operator calls repin rather than inferring from a digest.
        runtime_pin = RuntimePinInfo(spec_digest=pinned.spec_digest, pinned_at=pinned.pinned_at)
        return result.model_copy(update={"runtime_pin": runtime_pin})

    @router.post(
        "/workflows/{workflow_id}/review",
        status_code=204,
        response_class=Response,
        responses={**_LIFECYCLE_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def review(
        workflow_id: str,
        request: ApiReviewRequest,
        _actor: Actor = Depends(require(Permission.REVIEW)),
    ) -> Response:
        _require_operable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, request.environment_id)
        pinned = await _bind_operations(
            project,
            workflow_id=workflow_id,
            environment_id=request.environment_id,
            policy_ids=request.policy_ids,
            expected_policy_hash=request.expected_policy_hash,
        )
        await _temporal_bounded(
            pinned.operations.submit_review(
                request.execution_id,
                request.command,
                run_id=request.run_id,
            ),
            what="submitting the review decision",
        )
        return Response(status_code=204)

    @router.post(
        "/workflows/{workflow_id}/cancel",
        status_code=204,
        response_class=Response,
        responses={**_LIFECYCLE_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def cancel(
        workflow_id: str,
        request: ApiCancelRequest,
        _actor: Actor = Depends(require(Permission.CANCEL)),
    ) -> Response:
        _require_operable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, request.environment_id)
        pinned = await _bind_operations(
            project,
            workflow_id=workflow_id,
            environment_id=request.environment_id,
            policy_ids=request.policy_ids,
            expected_policy_hash=request.expected_policy_hash,
        )
        await _temporal_bounded(
            pinned.operations.request_cancel(
                request.execution_id,
                request.reason,
                run_id=request.run_id,
            ),
            what="requesting cancellation",
        )
        return Response(status_code=204)

    @router.post(
        "/workflows/{workflow_id}/migrate",
        response_model=WorkflowMigrateResult,
        responses={**_LIFECYCLE_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def migrate(
        workflow_id: str,
        request: ApiMigrateRequest,
        _actor: Actor = Depends(require_all(Permission.START, Permission.CANCEL)),
    ) -> WorkflowMigrateResult:
        # Long-drain migration (#204): terminate-and-resubmit across graph
        # versions, addressed exactly like cancel/review (execution_id +
        # optional run_id in the body; omitted run_id targets the current run).
        # Resolution-dependent (it resolves the CURRENT version to migrate
        # onto) and lifecycle-bound (the old execution must be this project's
        # logical workflow). Frozen-version enforcement runs when the
        # operations runtime is pinned, so the start leg reuses it.
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, request.environment_id)
        pinned = await _bind_operations(
            project,
            workflow_id=workflow_id,
            environment_id=request.environment_id,
            policy_ids=request.policy_ids,
            expected_policy_hash=request.expected_policy_hash,
        )
        return await _temporal_bounded(
            pinned.operations.migrate(
                request.execution_id,
                run_id=request.run_id,
                abandon_gates=request.abandon_gates,
                reason=request.reason,
                dry_run=request.dry_run,
            ),
            what="migrating the execution",
        )

    @router.post(
        "/workflows/{workflow_id}/repin",
        response_model=ApiRepinResult,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def repin(
        workflow_id: str,
        request: ApiRepinRequest,
        _actor: Actor = Depends(require(Permission.PROJECT_REFRESH)),
    ) -> ApiRepinResult:
        _require_resolvable()
        # Operator-triggered: drop the pinned operations runtime(s) for this
        # workflow+environment so the next mutating call re-resolves the current
        # YAML — refreshing the pin without a full `serve` restart (#324). Reads
        # are already per-request fresh. Gated like project refresh (operator
        # runtime maintenance). Drops every policy selection for the workflow.
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, request.environment_id)
        project_id = _current_project_id.get() or registry.default_id
        dropped = await _drop_pinned_operations(
            lambda key: (
                key[0] == project_id and key[1] == workflow_id and key[2] == request.environment_id
            )
        )
        return ApiRepinResult(repinned=dropped > 0, dropped=dropped)

    async def _foreign_visibility_call(
        project: TypefluxProjectSpec,
        *,
        workflow_id: str,
        environment_id: str,
        method: str,
        capability: str,
        what: str,
        **call_kwargs: Any,
    ) -> Any:
        # Decomposed visibility (#671/#686): the binding DRIVER owns the
        # projection — the ts-plan-argument driver works by MEMO identity over
        # the resolved plan's connection (composed profiles, #672) and digest.
        # Built FRESH per request, NOT through the mutating-ops pin cache:
        # read endpoints stay per-request fresh (the `_operations` contract),
        # so version comparisons always run against the CURRENT resolution
        # after YAML edits (codex P2). Construction still runs the same policy
        # admission as the pinned path.
        entry_runtime = _entry_runtime()
        operations = await _temporal_bounded(
            WorkflowOperations.for_project_workflow(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                policy_ids=(),
                expected_policy_hash=None,
                binding_profile=BINDING_PROFILE_FOR_RUNTIME[entry_runtime],
                resolver=resolver_map.get(entry_runtime),
            ),
            what=f"resolving {capability}",
        )
        try:
            driver = getattr(operations, "driver", operations)
            bound = getattr(driver, method, None)
            if bound is None:
                raise UnsupportedProjectRuntimeError(
                    f"the {entry_runtime!r} binding driver does not implement {capability}"
                )
            return await _temporal_bounded(bound(**call_kwargs), what=what)
        finally:
            operations.shutdown()

    @router.get(
        "/workflows/{workflow_id}/executions",
        response_model=WorkflowExecutionList,
        responses={**_TEMPORAL_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def executions(
        workflow_id: str,
        environment_id: str,
        limit: int = 20,
    ) -> WorkflowExecutionList:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        if _entry_runtime() != "python":
            result: WorkflowExecutionList = await _foreign_visibility_call(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                method="list_executions",
                capability="the executions listing",
                what="listing executions",
                limit=max(1, min(limit, 100)),
            )
            return result
        return await _temporal_bounded(
            workflow_executions(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                limit=max(1, min(limit, 100)),
            ),
            what="listing executions",
        )

    @router.get(
        "/workflows/{workflow_id}/correlation",
        response_model=WorkflowRunCorrelation,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def correlation(
        workflow_id: str,
        environment_id: str,
        execution_id: str,
    ) -> WorkflowRunCorrelation:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        if _entry_runtime() != "python":
            # The foreign path is blocking (subprocess resolver + langfuse reader);
            # off-loop like any sync route body.
            return await asyncio.to_thread(
                _foreign_correlation,
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                execution_id=execution_id,
            )
        # Async since #55 §9: the children tier awaits Temporal. It bounds its own
        # Temporal await internally (degrading to children=None), so no
        # _temporal_bounded wrapper — the card must answer, not 503.
        return await workflow_run_correlation(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
            execution_id=execution_id,
        )

    def _foreign_correlation(
        project: TypefluxProjectSpec,
        *,
        workflow_id: str,
        environment_id: str,
        execution_id: str,
    ) -> WorkflowRunCorrelation:
        # Decomposed correlation (#686): observer-driven, no Temporal tier.
        # The OBSERVER type comes from the resolver bundle's secret-free
        # runtime summary (the composed-profile view, like _foreign_connections);
        # a non-langfuse observer answers the trivial reachable shape, and a
        # langfuse one reuses Python's own reader by the shared execution-id
        # join key (both runtimes stamp it into trace metadata). Credentials
        # come from the PROCESS environment (the serve deployment's), matching
        # the connections probe's posture — the project environment file is the
        # other edition's resolution input, not this server's.
        from typeflux.project.runs import _langfuse_trace_summary

        bundle = _resolution().resolve_bundle(
            str(project.manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
        bundle_runtime = getattr(bundle, "runtime", None)
        runtime_summary = bundle_runtime if isinstance(bundle_runtime, dict) else {}
        observability = runtime_summary.get("observability")
        observability = observability if isinstance(observability, dict) else {}
        observer = str(observability.get("type") or "none")
        if observer != "langfuse":
            return WorkflowRunCorrelation(
                execution_id=execution_id,
                observer=observer,
                reachable=True,
                trace=None,
            )
        try:
            trace = _langfuse_trace_summary(execution_id)
        except Exception as exc:  # noqa: BLE001 - degrade, never 500 the card.
            return WorkflowRunCorrelation(
                execution_id=execution_id,
                observer=observer,
                reachable=False,
                trace=None,
                warning=f"observability backend unreachable: {exc}",
            )
        return WorkflowRunCorrelation(
            execution_id=execution_id,
            observer=observer,
            reachable=True,
            trace=trace,
        )

    @router.get(
        "/workflows/{workflow_id}/connections",
        response_model=WorkflowConnections,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def connections(workflow_id: str, environment_id: str) -> WorkflowConnections:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        if _entry_runtime() != "python":
            return _foreign_connections(
                project, workflow_id=workflow_id, environment_id=environment_id
            )
        return workflow_connections(
            project,
            workflow_id=workflow_id,
            environment_id=environment_id,
        )

    def _foreign_connections(
        project: TypefluxProjectSpec, *, workflow_id: str, environment_id: str
    ) -> WorkflowConnections:
        # Decomposed connections (#671): the resolver's bundle carries the
        # RESOLVED runtime summary — registry type/host and observability
        # type + execution_manifest + redaction_enabled, the composed-profile
        # view — and the probe itself is runtime-neutral. Secret-safe by
        # construction: the bundle contract is "types + hosts only, never
        # credentials". The langfuse probe reads the PROCESS environment for
        # credentials (the serve deployment's), like the TS edition's default
        # probe — the project environment file is the other edition's
        # resolution input, not this server's.
        from typeflux.project.connections import ObserverStatus, _probe

        bundle = _resolution().resolve_bundle(
            str(project.manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
        )
        bundle_runtime = getattr(bundle, "runtime", None)
        runtime_summary = bundle_runtime if isinstance(bundle_runtime, dict) else {}
        registry_summary = runtime_summary.get("registry")
        registry_summary = registry_summary if isinstance(registry_summary, dict) else {}
        observability = runtime_summary.get("observability")
        observability = observability if isinstance(observability, dict) else {}
        registry_type = registry_summary.get("type")
        if not isinstance(registry_type, str) or not registry_type:
            # A bundle without the registry summary is a resolver-emission bug —
            # fail closed, never probe a guessed backend.
            raise HTTPException(
                status_code=422,
                detail=(
                    "the resolver's bundle carries no runtime registry summary; "
                    "connections cannot be reported for this project"
                ),
            )
        host = registry_summary.get("host")
        observer_type = str(observability.get("type") or "none")
        observer = _probe(kind=observer_type, configured_host=None)
        return WorkflowConnections(
            workflow_id=workflow_id,
            environment_id=environment_id,
            registry=_probe(
                kind=registry_type,
                configured_host=host if isinstance(host, str) and host else None,
            ),
            observability=ObserverStatus(
                **observer.model_dump(),
                # Absent flags read TRUE: both editions' specs default
                # execution_manifest/redaction.enabled on — an absent
                # observability block is NOT "off".
                execution_manifest=bool(observability.get("execution_manifest", True)),
                redaction_enabled=bool(observability.get("redaction_enabled", True)),
            ),
        )

    @router.get(
        "/workflows/{workflow_id}/prompt-status",
        response_model=WorkflowPromptStatus,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def prompt_status(workflow_id: str, environment_id: str) -> WorkflowPromptStatus:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        return _resolution().prompt_status(
            str(project.manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
        )

    @router.get(
        "/enforcement-events",
        response_model=EnforcementEventList,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def enforcement_events(
        environment_id: str | None = None,
        workflow_id: Annotated[list[str] | None, Query()] = None,
        policy_id: Annotated[list[str] | None, Query()] = None,
        verdict: Annotated[list[str] | None, Query()] = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = ENFORCEMENT_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> EnforcementEventList:
        # The enforcement-events feed (#723): normalized admission verdicts from
        # the read tier's validation surface + bounded runtime verdicts read
        # from Langfuse through the injected transport seam. Resolution-dependent
        # (admission is derived from the resolved validation report), so a
        # non-python-runtime project 501s exactly like /validate.
        #
        # A SYNC `def` route on purpose (like `connections`): the injected reader
        # makes BLOCKING Langfuse HTTP calls, and Starlette runs a sync route body
        # in its threadpool — so the blocking read never sits on the event loop
        # (the #585 class). An `async def` here would need an explicit to_thread
        # like `correlation`'s foreign path; a plain def is the simpler equivalent.
        _require_resolvable()
        project = _project()
        # SCOPE IS REQUIRED (#723): without an environment the validation report
        # never resolves any workflow (validate_project_bundle returns the bare
        # reference report), so the feed would answer `[] / not_configured` —
        # indistinguishable from a healthy quiet project. Demand the scope
        # explicitly rather than answer a silently-empty, resolvable feed.
        if environment_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "enforcement-events requires environment_id: admission verdicts resolve "
                    "per environment, and an unscoped call cannot be resolved (it would "
                    "answer an empty, not_configured feed indistinguishable from a quiet one)"
                ),
            )
        _require_environment(project, environment_id)
        for requested in workflow_id or ():
            _require_workflow(project, requested)
        verdicts = tuple(verdict or ())
        invalid = [value for value in verdicts if value not in ("blocked", "rejected")]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown verdict filter(s): {', '.join(invalid)}; allowed: blocked, rejected"
                ),
            )
        policy_ids = tuple(policy_id or ())
        workflow_ids = tuple(workflow_id or ())
        raw_since, raw_until = _as_utc_aware(since), _as_utc_aware(until)
        window_since, window_until = resolve_window(raw_since, raw_until)
        # The cursor's offset is bound to a fingerprint of THIS filter set: a cursor
        # reused after any filter change is rejected (422), never silently skips/dupes.
        # Fingerprint the caller's RAW window intent (None when unpinned), not the
        # resolved window — an unpinned `until` resolves to now() afresh each call, so
        # the resolved value would reject every paginated request.
        fingerprint = filter_fingerprint(
            workflow_ids=workflow_ids,
            environment_id=environment_id,
            verdicts=verdicts,
            policy_ids=policy_ids,
            since=raw_since,
            until=raw_until,
        )
        try:
            offset = decode_cursor(cursor, fingerprint=fingerprint)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        bounded_limit = max(1, min(limit, ENFORCEMENT_MAX_LIMIT))
        # Size the runtime fetch to just what this page could need (offset + one
        # page), capped at MAX — a limit=1 request no longer pages the full 200
        # rows (up to 5 sequential Langfuse pages). A trace may yield 0..N events,
        # so this is a bound, not an exact count; admission events (cheap, from the
        # report) always serve in full and sort ahead of dated runtime events.
        reader_limit = min(offset + bounded_limit, ENFORCEMENT_MAX_LIMIT)
        # Admission events: normalize from the read tier's own validation report
        # (never re-derived) — same computation /validate serves. TRADEOFF: each
        # cursor page re-runs validate_project (no cross-request cache) — the
        # accepted stateless-re-read cost of #577; the report is cheap relative to
        # the bounded Langfuse read and keeps the feed free of server-side state.
        # policy_id is a FILTER over each event's RECORDED applied policies (see
        # build_enforcement_feed) — it must NOT be forwarded into validate_project,
        # which would re-scope how admission verdicts are COMPUTED (validating under
        # only the requested policies instead of the project's real selection) and
        # so fabricate a different admission outcome than the project actually has
        # (Bugbot). Validation always runs under the real policy selection.
        report = _resolution().validate_project(
            str(project.manifest_path),
            environment_id=environment_id,
            workflow_ids=workflow_ids,
        )
        # Runtime events: the observer type comes from the resolved report; a
        # non-langfuse observer degrades to "not_configured" (admission still
        # serves), a transport failure to "unreachable" — never a silent empty.
        read_result = app.state.enforcement_reader(
            observer=observer_from_report(report),
            environment_id=environment_id,
            since=window_since,
            until=window_until,
            limit=reader_limit,
        )
        return build_enforcement_feed(
            report=report,
            read_result=read_result,
            environment_id=environment_id,
            policy_ids=policy_ids,
            workflow_ids=workflow_ids,
            verdicts=verdicts,
            since=window_since,
            until=window_until,
            limit=bounded_limit,
            offset=offset,
            cursor_fingerprint=fingerprint,
        )

    @router.get(
        "/github-provenance",
        response_model=GithubProvenance,
        response_model_exclude_none=True,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def github_provenance() -> GithubProvenance:
        # The github-provenance surface (#727): HEAD-vs-served drift + plan→approving-PR,
        # read at request time through the injected GitHub transport seam. Resolution-bound
        # like /enforcement-events (the `github_provenance` capability follows `resolvable`),
        # so a non-python-runtime project 501s here and reports the capability false.
        #
        # A SYNC `def` route on purpose (like `connections`/`enforcement_events`): the
        # injected reader makes BLOCKING GitHub HTTP calls, and Starlette runs a sync body
        # in its threadpool, so the blocking read never sits on the event loop (#585).
        _require_resolvable()
        project = _project()
        project_id = _current_project_id.get()
        # Every approved plan is listed (newest-first by generated_at); only its PR needs
        # the network. `read_deployment_plan_dir` keeps malformed files visible as error
        # entries; those carry no plan, so they are skipped here. Each plan's sha is its plan
        # FILE's last commit in the served clone (the commit whose PR merged the file), NOT
        # the checkout `code.sha` the plan was resolved from — that predates the file's merge
        # commit, so attributing the PR to it finds unrelated PRs or none (#727 P0-1). An
        # untracked/uncommitted plan file resolves to sha=None → pr=null (never fabricated).
        loaded = sorted(
            (
                (entry.file, entry.plan)
                for entry in read_deployment_plan_dir(project)
                if entry.plan is not None
            ),
            key=lambda item: item[1].generated_at,
            reverse=True,
        )
        all_plan_refs = [
            PlanRef(
                plan_id=plan.plan_id,
                sha=registry.plan_file_sha(project_id, f"deployments/{file}"),
            )
            for file, plan in loaded
        ]
        # The served side comes from the registry's recorded git-source provenance. None for
        # a local mount or a non-github (GHE/ssh) source → not_configured with NO network
        # call (the token/repo-absent posture), and the capability is false for it.
        served = _served_github_provenance()
        if served is None:
            read_result = GithubReadResult("not_configured")
        else:
            # Bound the PR fan-out to the most-recent PLAN_PR_LOOKUP_CAP sha-bearing plans;
            # the tail is still listed (pr=null), just not looked up.
            lookup = plan_refs(all_plan_refs, cap=PLAN_PR_LOOKUP_CAP)
            read_result = app.state.github_reader(
                repo=served.repo,
                branch=served.branch,
                served_sha=served.served_sha,
                plan_shas=tuple(ref.sha for ref in lookup if ref.sha is not None),
            )
        return build_github_provenance(served=served, plans=all_plan_refs, read_result=read_result)

    class _DeploymentEntry(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        plan_id: str
        plan: DeploymentPlan
        verification: PlanVerification
        #: The plan file's manifest-relative path — the console builds source links from it
        #: (#610); the same location promote_command encodes.
        path: str
        #: The exact CLI command an operator runs to promote this plan. The
        #: plan is authoritative — env, workflow, and image come from the file —
        #: so the command needs only the manifest and the plan path.
        promote_command: str

    def _promote_command(project: TypefluxProjectSpec, plan_path: str) -> str:
        # SHELL-QUOTED (defense in depth on top of the loader's id-charset validation): the
        # console copy-pastes this, so a tampered plan/filename must never become an injection.
        return (
            "uv run typeflux-project deploy "
            f"{shlex.quote(str(project.manifest_path))} --apply {shlex.quote(plan_path)}"
        )

    def _deployment_entry(project: TypefluxProjectSpec, plan: DeploymentPlan) -> _DeploymentEntry:
        # PER-PLAN DEGRADE (both editions): a stale plan whose workflow/environment no longer
        # resolves is DRIFT, not a server fault — record a failed verification instead of
        # 422-ing the whole listing (which would hide every valid plan behind one stale file).
        try:
            verification = verify_deployment_plan(project, plan)
        except Exception as exc:  # noqa: BLE001 - availability: one bad plan never hides the rest.
            verification = PlanVerification(
                ok=False,
                mismatches=(
                    PlanMismatch(
                        path="resolution", plan_value=plan.plan_id, current_value=str(exc)
                    ),
                ),
            )
        plan_path = f"deployments/{plan.plan_id}.yaml"
        return _DeploymentEntry(
            plan_id=plan.plan_id,
            plan=plan,
            verification=verification,
            path=plan_path,
            promote_command=_promote_command(project, plan_path),
        )

    def _error_deployment_entry(
        project: TypefluxProjectSpec, file: str, error: str
    ) -> _DeploymentEntry:
        # A malformed/tampered plan file surfaces as an ERROR ENTRY (filename + load error,
        # zeroed placeholder plan — the contract requires one) rather than silently vanishing:
        # an operator must see a corrupt plan file, not a shorter listing.
        placeholder = DeploymentPlan(
            plan_hash="",
            generated_at="",
            identity=PlanIdentity(
                workflow_id="",
                workflow_name="",
                workflow_type="",
                spec_digest="",
                spec_digest_algorithm="",
                environment_id="",
            ),
            policy=PlanPolicy(policy_hash=""),
            deployment=PlanDeployment(
                image="", image_digest_pinned=False, preflight=PlanPreflight(ok=False)
            ),
        )
        plan_path = f"deployments/{file}"
        return _DeploymentEntry(
            plan_id=file.removesuffix(".yaml"),
            plan=placeholder,
            verification=PlanVerification(
                ok=False,
                mismatches=(PlanMismatch(path="parse", plan_value=file, current_value=error),),
            ),
            path=plan_path,
            promote_command=_promote_command(project, plan_path),
        )

    @router.get(
        "/deployments",
        response_model=tuple[_DeploymentEntry, ...],
        responses=_UNSUPPORTED_RUNTIME_RESPONSE,
    )
    def deployments_list() -> tuple[_DeploymentEntry, ...]:
        # PER-PLAN DEGRADE: each valid plan is live-verified (a stale one records a failed
        # verification); a malformed/tampered file becomes an error entry. One bad file never
        # hides the valid plans or fails the listing (mirrored by the TS edition, #687).
        _require_resolvable()
        if _entry_runtime() != "python":
            # The deployments surface is per-loader; a foreign-runtime project's plans are the
            # OTHER server's to list (its own deployments routes are real now, #687).
            return ()
        project = _project()
        return tuple(
            _deployment_entry(project, entry.plan)
            if entry.plan is not None
            else _error_deployment_entry(project, entry.file, entry.error or "unreadable file")
            for entry in read_deployment_plan_dir(project)
        )

    @router.get(
        "/deployments/{plan_id}",
        response_model=_DeploymentEntry,
        responses={**_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    def deployments_detail(plan_id: str) -> _DeploymentEntry:
        _require_resolvable()
        if _entry_runtime() != "python":
            # The deployments surface is per-loader (see deployments_list).
            raise HTTPException(status_code=404, detail=f"unknown deployment plan: {plan_id}")
        project = _project()
        for entry in read_deployment_plan_dir(project):
            if entry.plan is not None:
                if entry.plan.plan_id == plan_id:
                    return _deployment_entry(project, entry.plan)
            elif entry.file.removesuffix(".yaml") == plan_id:
                return _error_deployment_entry(
                    project, entry.file, entry.error or "unreadable file"
                )
        raise HTTPException(status_code=404, detail=f"unknown deployment plan: {plan_id}")

    @router.get(
        "/workflows/{workflow_id}/workers",
        response_model=WorkflowTaskQueueWorkers,
        response_model_exclude_none=True,
        responses={**_TEMPORAL_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def workers(
        workflow_id: str, environment_id: str, task_queue: str | None = None
    ) -> WorkflowTaskQueueWorkers:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        if _entry_runtime() != "python":
            # The describe is runtime-neutral, but the canonical implementation
            # resolves the queue via the Python loader — the ts driver routes
            # it through its own composed connection + pinned plan (#686).
            result: WorkflowTaskQueueWorkers = await _foreign_visibility_call(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                method="task_queue_workers",
                capability="the task-queue workers view",
                what="describing task-queue workers",
                task_queue=task_queue,
            )
            return result
        return await _temporal_bounded(
            workflow_task_queue_workers(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                task_queue=task_queue,
            ),
            what="describing task-queue workers",
        )

    @router.get(
        "/workflows/{workflow_id}/versions",
        response_model=WorkflowDrainStatus,
        responses={**_TEMPORAL_ERROR_RESPONSES, **_UNSUPPORTED_RUNTIME_RESPONSE},
    )
    async def versions(workflow_id: str, environment_id: str) -> WorkflowDrainStatus:
        _require_resolvable()
        project = _project()
        _require_workflow(project, workflow_id)
        _require_environment(project, environment_id)
        if _entry_runtime() != "python":
            # The ts profile's drain identity lives in MEMOS, not type names —
            # the driver scans the generic type and groups by the memo's
            # version identity (#686; binding_ts.drain_status).
            result: WorkflowDrainStatus = await _foreign_visibility_call(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
                method="drain_status",
                capability="the drain view",
                what="computing the drain view",
            )
            return result
        return await _temporal_bounded(
            workflow_drain_status(
                project,
                workflow_id=workflow_id,
                environment_id=environment_id,
            ),
            what="computing the drain view",
        )

    # The registry surface itself is project-agnostic — it lists the projects
    # the console can switch between — so it lives on the app, not the router.
    @app.get("/api/v1/projects", response_model=tuple[ProjectSummary, ...])
    def projects(_actor: Actor = Depends(require_inspect)) -> tuple[ProjectSummary, ...]:
        return registry.summaries(supported=supported_runtimes)

    @app.post(
        "/api/v1/projects/{project}/refresh",
        response_model=ProjectRefreshResult,
        responses=_ERROR_RESPONSES,
    )
    async def refresh_project(
        project: str,
        _actor: Actor = Depends(require(Permission.PROJECT_REFRESH)),
    ) -> ProjectRefreshResult:
        # Operator-triggered: re-fetch a Git-sourced project's clone (a no-op
        # for local checkouts), then drop its pinned operations so the next
        # call re-resolves the new sha. Read endpoints are already per-request
        # fresh, so they reflect the new checkout immediately.
        try:
            # registry.refresh runs a blocking git subprocess; offload it so a
            # slow/hung fetch never freezes the event loop for other requests.
            result = await asyncio.to_thread(registry.refresh, project)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown project: {project}") from None
        await _drop_pinned_operations(lambda key: key[0] == project)
        return result

    # Mount the one handler set twice: unprefixed for the default project, and
    # under /projects/{project} for the full set (default included).
    app.include_router(router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1/projects/{project}")
    _install_project_scoped_openapi(app)
    return app


_OPENAPI_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def _install_project_scoped_openapi(app: FastAPI) -> None:
    """Declare the ``project`` path parameter on project-scoped operations (#354).

    The router is mounted twice (unprefixed + under ``/projects/{project}``) and
    handlers read the project from a ContextVar, not a signature parameter — so
    FastAPI emits the ``/projects/{project}/...`` operations *without* a
    ``project`` parameter, and a generated client has nothing to fill the path
    segment with. Declaring ``project`` on the shared handler would instead break
    the unprefixed mount (no ``{project}`` in that path), so patch the generated
    schema: inject the param into every operation whose path templates
    ``{project}`` and doesn't already declare it (``refresh`` does). Idempotent,
    so the cached schema can be regenerated safely.
    """
    base_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = base_openapi()
        for path, path_item in schema.get("paths", {}).items():
            if "{project}" not in path or not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method not in _OPENAPI_HTTP_METHODS or not isinstance(operation, dict):
                    continue
                params = operation.setdefault("parameters", [])
                if any(p.get("name") == "project" and p.get("in") == "path" for p in params):
                    continue
                params.insert(
                    0,
                    {
                        "in": "path",
                        "name": "project",
                        "required": True,
                        "schema": {"title": "Project", "type": "string"},
                    },
                )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def openapi_spec() -> dict[str, Any]:
    """The OpenAPI document for the control-plane API.

    The schema depends only on the route/model definitions, never on a
    project manifest, so a placeholder path is sufficient.
    """
    return create_app("typeflux.project.yaml").openapi()


def render_openapi_document(document: dict[str, Any]) -> str:
    """The canonical rendering shared by the emitted spec and the contract file.

    Single source of formatting truth (#616): the conformance check re-renders
    the contract with this exact function, so the two can't drift.
    """
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_openapi_spec() -> str:
    """Deterministic JSON rendering, gated against the normative contract.

    The contract (contracts/controlplane/openapi.v1.json, #616) is the
    interface; this emission must match it byte-for-byte.
    """
    return render_openapi_document(openapi_spec())


__all__ = [
    "API_TITLE",
    "API_VERSION",
    "create_app",
    "create_app_from_registry",
    "openapi_spec",
    "render_openapi_document",
    "render_openapi_spec",
]
