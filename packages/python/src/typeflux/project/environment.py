from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

from typeflux.core.errors import TypefluxError
from typeflux.project.loader import (
    _load_yaml_mapping,
    _required_workflow_path,
    discover_project_workflows,
)
from typeflux.project.profiles import (
    AppliedComponentProfile,
    profile_overrides_and_provenance,
    resolve_selected_profiles,
    validate_profile_selection,
)
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.overrides import (
    YamlOverrideProvenance,
    validate_yaml_overrides,
    yaml_override_paths,
)
from typeflux.yaml.secrets import secret_value_configured
from typeflux.yaml.spec import TypefluxYamlSpec

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_ENV_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_DOTENV_SENTINEL = ".typeflux-project-env-do-not-load"

EnvValue = str | bool | int | float


class ProjectEnvironmentError(TypefluxError, ValueError):
    """Raised when a project environment profile cannot be resolved."""


class ProjectEnvironmentEnvFileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    required: bool = True

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("environment env_files.path must be non-empty")
        return value


class ProjectEnvironmentWorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, Any] = Field(default_factory=dict)
    #: Per-kind component profile selection; replaces the workflow-level
    #: selection for that kind (whole-reference replacement).
    profiles: dict[str, str] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _validate_overrides(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_allowed_overrides(value, prefix="workflows.<workflow>.overrides")
        return value


class ProjectEnvironmentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: Literal["1"] = "1"
    name: str
    env_files: list[ProjectEnvironmentEnvFileSpec] = Field(default_factory=list)
    variables: dict[str, EnvValue] = Field(default_factory=dict)
    overrides: dict[str, Any] = Field(default_factory=dict)
    workflows: dict[str, ProjectEnvironmentWorkflowSpec] = Field(default_factory=dict)
    profile_path: Path = Field(default=Path("environment.yaml"), exclude=True)

    @property
    def profile_dir(self) -> Path:
        return self.profile_path.parent

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("environment name must be non-empty")
        return value

    @field_validator("profile_path", mode="before")
    @classmethod
    def _validate_profile_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @field_validator("variables")
    @classmethod
    def _validate_variables(cls, value: dict[str, EnvValue]) -> dict[str, EnvValue]:
        for key in value:
            _validate_env_key(key)
        return value

    @field_validator("overrides")
    @classmethod
    def _validate_overrides(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_allowed_overrides(value, prefix="overrides")
        return value

    @field_validator("workflows")
    @classmethod
    def _validate_workflow_names(
        cls,
        value: dict[str, ProjectEnvironmentWorkflowSpec],
    ) -> dict[str, ProjectEnvironmentWorkflowSpec]:
        for workflow_id in value:
            if not workflow_id or workflow_id.strip() != workflow_id:
                raise ValueError("environment workflow override ids must be non-empty")
        return value


class ProjectEnvironmentFileStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    required: bool
    loaded: bool


class ProjectEnvironmentApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_id: str
    environment_name: str
    profile_path: str
    env_files: tuple[ProjectEnvironmentFileStatus, ...] = ()
    profile_variable_names: tuple[str, ...] = ()
    variables: dict[str, str] = Field(default_factory=dict, exclude=True)

    def safe_summary(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"variables"})


class ProjectResolvedWorkflowSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str
    workflow_id: str
    workflow_path: str
    environment_id: str
    environment_name: str
    environment_profile_path: str
    yaml_project: str
    yaml_name: str
    workflow_name: str
    task_queue: str
    temporal: dict[str, Any]
    registry: dict[str, Any]
    provider: dict[str, Any]
    observability: dict[str, Any]
    environment: dict[str, Any]


@dataclass(frozen=True)
class ProjectResolvedWorkflow:
    project: TypefluxProjectSpec
    workflow_id: str
    workflow_path: Path
    environment_id: str
    environment: ProjectEnvironmentSpec
    application: ProjectEnvironmentApplication
    spec: TypefluxYamlSpec
    components: tuple[AppliedComponentProfile, ...] = ()
    # The interpolation base this workflow was resolved under (#760): `None` for the
    # default os.environ-backed path, the injected mapping for a hermetic resolution.
    # RETAINED on the artifact so every downstream closure walk that re-resolves
    # siblings from it (`resolve_subworkflows_for`, `_iter_subworkflow_closure`,
    # `create_workflow_with_subworkflows`) automatically resolves children under the
    # SAME base — a hermetically-resolved parent can never have its children silently
    # read the operator's shell.
    base_env: Mapping[str, str] | None = None

    def summary(self) -> ProjectResolvedWorkflowSummary:
        registry = self.spec.runtime.registry
        provider = self.spec.runtime.provider
        observability = self.spec.runtime.observability
        with project_environment_context(self.application):
            temporal_summary = _temporal_summary(self.spec)
            provider_api_key_configured = secret_value_configured(provider.api_key)
        return ProjectResolvedWorkflowSummary(
            project_name=self.project.name,
            workflow_id=self.workflow_id,
            workflow_path=str(self.workflow_path),
            environment_id=self.environment_id,
            environment_name=self.environment.name,
            environment_profile_path=str(self.environment.profile_path),
            yaml_project=self.spec.project,
            yaml_name=self.spec.name,
            workflow_name=self.spec.workflow.name,
            task_queue=self.spec.task_queue,
            temporal=temporal_summary,
            registry={
                "type": registry.type,
                "label": registry.label,
                "host": registry.host,
            },
            provider={
                "type": provider.type,
                "model": provider.model,
                "base_url_configured": bool(provider.base_url),
                "api_key_configured": provider_api_key_configured,
                # Vertex AI authenticates with ADC, so api_key_configured=false is
                # expected there — surface the mode so it doesn't read as a missing
                # credential (#332).
                "vertex": provider.vertex is not None,
            },
            observability={
                "type": observability.type,
                "execution_manifest": observability.execution_manifest,
                # Carried so `/connections` decomposes from resolve_bundle alone
                # for foreign-edition projects (#671; the resolver contract's
                # coverage_note).
                "redaction_enabled": observability.redaction.enabled,
            },
            environment=self.application.safe_summary(),
        )


def load_project_environment(
    project: TypefluxProjectSpec,
    environment_id: str,
) -> ProjectEnvironmentSpec:
    try:
        raw_path = project.environments[environment_id]
    except KeyError as exc:
        raise ProjectEnvironmentError(f"unknown project environment: {environment_id}") from exc

    profile_path = _resolve_project_path(project, raw_path)
    raw = _load_yaml_mapping(profile_path, kind="Typeflux project environment profile")
    environment = ProjectEnvironmentSpec.model_validate(raw).model_copy(
        update={"profile_path": profile_path}
    )
    _validate_environment_workflow_ids(project, environment)
    return environment


def build_project_environment_application(
    environment: ProjectEnvironmentSpec,
    *,
    environment_id: str | None = None,
) -> ProjectEnvironmentApplication:
    env_values: dict[str, str] = {}
    file_statuses: list[ProjectEnvironmentFileStatus] = []
    for env_file in environment.env_files:
        env_path = _resolve_environment_path(environment, env_file.path)
        if not env_path.exists():
            if env_file.required:
                raise ProjectEnvironmentError(
                    f"required environment env file does not exist: {env_path}"
                )
            file_statuses.append(
                ProjectEnvironmentFileStatus(
                    path=str(env_path),
                    required=env_file.required,
                    loaded=False,
                )
            )
            continue
        env_values.update(_read_env_file(env_path))
        file_statuses.append(
            ProjectEnvironmentFileStatus(
                path=str(env_path),
                required=env_file.required,
                loaded=True,
            )
        )

    for key, value in environment.variables.items():
        env_values[key] = _stringify_env_value(value)

    return ProjectEnvironmentApplication(
        environment_id=environment_id or environment.name,
        environment_name=environment.name,
        profile_path=str(environment.profile_path),
        env_files=tuple(file_statuses),
        profile_variable_names=tuple(sorted(environment.variables)),
        variables=env_values,
    )


# Applying an environment profile mutates os.environ process-wide (below), so
# concurrent resolution of two projects/environments can leak each other's
# variables. A single process-wide lock serializes every env-context block;
# `_env_lock_held` (a ContextVar, so reentrancy is per-coroutine, not
# per-thread) makes a nested same-flow re-entry a no-op so the non-reentrant
# Lock never self-deadlocks. Throughput cost is acceptable for the human-paced
# control plane (#256); process-per-project is the scale-out path.
#
# Several async control-plane reads (drain, executions, workers, the
# operations build) hold this lock across an await to Temporal. A sync
# `acquire()` on the event-loop thread would block the loop and deadlock a
# second concurrent request, so async callers must take the lock via
# `async_project_environment_context` — it acquires in a worker thread
# (asyncio.to_thread), leaving the loop free, then enters the sync block whose
# acquire is a no-op because the ContextVar already marks the lock held.
#
# Blocking work INSIDE the held-lock region must use
# `run_while_env_lock_held`, never asyncio.to_thread — the default executor
# can be full of threads blocked acquiring this very lock (see the helper).
_env_resolution_lock = threading.Lock()
_env_lock_held: ContextVar[bool] = ContextVar("typeflux_env_lock_held", default=False)


@contextmanager
def project_environment_context(
    application: ProjectEnvironmentApplication,
) -> Iterator[None]:
    env_values = dict(application.variables)
    env_values.setdefault(
        "TYPEFLUX_ENV_FILE",
        str(Path(application.profile_path).parent / _DOTENV_SENTINEL),
    )
    already_held = _env_lock_held.get()
    token = None
    if not already_held:
        # Sync callers run in the threadpool (or the CLI/library), where a
        # blocking acquire is fine — it never blocks the asyncio event loop.
        _env_resolution_lock.acquire()
        token = _env_lock_held.set(True)
    original = {key: os.environ.get(key) for key in env_values}
    try:
        os.environ.update(env_values)
        # No provenance-cache clear needed here (#829): the cache holds cwd-keyed
        # CODE identity only — env-sourced identity bypasses it and deployment
        # identity is overlaid fresh on every read, so a swapped environment can
        # never be served stale from it.
        yield
    finally:
        for key, old_value in original.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value
        if token is not None:
            _env_lock_held.reset(token)
            _env_resolution_lock.release()


class _AcquireHandoff:
    """The race-free handoff between an env-lock acquire thread and its awaiter.

    The thread, having acquired, either hands the acquisition to the coroutine
    (``acquired``) or — when the coroutine already abandoned it — releases the
    lock immediately IN THE THREAD, immune to event-loop state. The abandoning
    coroutine, symmetrically, either marks ``abandoned`` (the thread will
    self-release) or releases directly (the thread had already completed). The
    mutex makes exactly one side responsible, always.

    Keyed to the THREAD's acquisition, never the asyncio wrapper's state: at
    interpreter/loop teardown, ``asyncio.run`` cancels pending tasks, which
    marks a ``to_thread`` wrapper cancelled while its thread still completes
    the acquire — a task-done-callback keyed on ``task.cancelled()`` skips the
    release there, leaking the process-wide lock and deadlocking the default
    executor's shutdown join (#597, faulthandler-diagnosed in CI).
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._abandoned = False
        self._acquired = False

    def acquire_in_thread(self) -> bool:
        _env_resolution_lock.acquire()
        with self._mutex:
            if self._abandoned:
                _env_resolution_lock.release()
                return False
            self._acquired = True
            return True

    def abandon(self) -> None:
        with self._mutex:
            if self._acquired:
                # The thread finished but the awaiter is unwinding — the
                # acquisition will never be used; release on its behalf.
                _env_resolution_lock.release()
                self._acquired = False
                return
            self._abandoned = True


@asynccontextmanager
async def async_project_environment_context(
    application: ProjectEnvironmentApplication,
) -> AsyncIterator[None]:
    """Hold the env-context lock across awaits without blocking the event loop.

    Async callers that mutate os.environ and then await Temporal must use this
    instead of the bare ``project_environment_context``: it acquires the
    process-wide resolution lock in a worker thread (so a second concurrent
    request never blocks the loop on ``acquire()``), then delegates to the sync
    context manager whose own acquire is skipped via the ContextVar guard.

    Acquisition is cancellation-safe against BOTH a cancelled caller (e.g. a
    control-plane timeout, #581) and loop teardown (#597): the unstoppable
    acquire thread and the unwinding coroutine settle ownership through a
    mutex-guarded handoff, so an orphaned acquisition is always released — by
    whichever side loses the race — regardless of task/future state.
    """
    already_held = _env_lock_held.get()
    token = None
    if not already_held:
        handoff = _AcquireHandoff()
        acquire_task = asyncio.ensure_future(asyncio.to_thread(handoff.acquire_in_thread))
        try:
            await asyncio.shield(acquire_task)
        except BaseException:
            # Whatever unwound us — timeout cancellation, loop teardown,
            # KeyboardInterrupt, GeneratorExit — settle ownership through the
            # handoff; the thread cannot be stopped but can no longer leak.
            handoff.abandon()
            raise
        token = _env_lock_held.set(True)
    try:
        with project_environment_context(application):
            yield
    finally:
        if token is not None:
            _env_lock_held.reset(token)
            _env_resolution_lock.release()


# Blocking work performed *while holding* the env lock must never queue to
# the loop's default executor: under a request burst that pool fills with
# threads blocked in `_env_resolution_lock.acquire` (the
# `async_project_environment_context` acquire path), so the holder's work
# would wait behind waiters that only unblock when the holder finishes —
# a threadpool-starvation deadlock. One dedicated worker suffices and is
# starvation-proof: the process-wide lock has at most one holder at a time.
_env_lock_holder_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="typeflux-env-lock-holder"
)


async def run_while_env_lock_held(func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run blocking work off-loop from inside an env-lock-held async context.

    Like ``asyncio.to_thread`` (including the contextvars copy, which carries
    the ``_env_lock_held`` marker so the work's env reads stay inside the
    context), but on a dedicated worker so it cannot deadlock behind
    default-executor threads that are blocked acquiring this same lock.

    Cancellation-safe against env teardown: a worker thread cannot be
    stopped, so if the caller is cancelled (e.g. a control-plane tier
    timeout, #581) after the work has started, this holds the unwind until
    the work finishes — otherwise the caller's env context would restore
    ``os.environ`` and release the lock while the orphaned work is still
    reading the environment. The wait is bounded by the work itself; work
    still queued at cancellation is cancelled without waiting.

    Call only while holding the env lock (inside
    ``async_project_environment_context``): the lock is what makes the
    single worker starvation- and queueing-proof — a lock-free submitter
    could queue behind another holder's cancelled-but-running work. And
    ``func`` must not call back into this helper: nested submissions queue
    behind the running outer call on the single worker and never start.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    # Submit directly (not run_in_executor) to keep the concurrent.futures
    # future: cancelling the asyncio wrapper marks it cancelled even while
    # the executor work keeps running, so only the cf future can tell
    # "removed from the queue" apart from "running to completion anyway".
    cf_future = _env_lock_holder_executor.submit(lambda: ctx.run(func, *args, **kwargs))
    try:
        return await asyncio.wrap_future(cf_future)
    except asyncio.CancelledError:
        if not cf_future.cancelled():
            done = asyncio.Event()
            cf_future.add_done_callback(lambda _: loop.call_soon_threadsafe(done.set))
            while not cf_future.done():
                try:
                    await done.wait()
                except asyncio.CancelledError:
                    continue  # re-cancelled while waiting out the worker
            error = cf_future.exception()
            if error is not None:
                # Cancellation must win the unwind, but the worker's own
                # failure is the truer root cause — don't lose it.
                logger.warning(
                    "env-lock worker %r failed during cancellation wait", func, exc_info=error
                )
        raise


def resolve_project_workflow(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    base_env: Mapping[str, str] | None = None,
) -> ProjectResolvedWorkflow:
    """Resolve a workflow's spec under a project environment.

    ``base_env`` (#760; TS parity with ``resolveProjectWorkflow``'s ``env``) is the
    base environment ``${VAR}`` references interpolate against. It defaults to
    ``None`` — the process ``os.environ``, applied through the environment overlay
    exactly as before (zero behavior change). A caller emitting COMMITTED,
    machine-independent artifacts (deployment renders) passes a fixed mapping so the
    operator's shell never leaks into the resolved bytes: resolution then runs
    PURELY-FUNCTIONALLY against ``{**base_env, **environment.variables}`` (the
    environment's ``variables``/``.env`` overlay still wins over the injected base,
    matching the overlay-over-``os.environ`` precedence of the default path) and
    NEVER mutates ``os.environ`` — the hermetic replacement for the fragile
    global-mutation ``withHermeticResolutionEnv`` wrapper. A ``${VAR}`` absent from
    the injected mapping errors exactly as an unset shell variable would.

    The base is RETAINED on the returned artifact (``ProjectResolvedWorkflow.base_env``),
    so downstream closure walks that re-resolve siblings from it — sub-workflow
    resolution, closure-policy admission, the workflow-class build — resolve every
    child under the same base automatically (TS ``planResolverFor`` threads its ``env``
    the same way).
    """
    environment = load_project_environment(project, environment_id)
    workflow = _project_workflow(project, workflow_id)
    workflow_path = _required_workflow_path(workflow)
    profile_overrides, components = _resolved_profile_overrides(
        project,
        workflow=workflow,
        environment=environment,
        workflow_id=workflow_id,
    )
    environment_overrides = _merged_overrides(environment, workflow_id)
    # Precedence: workflow YAML < selected profiles < environment overrides.
    overrides = _deep_merge(profile_overrides, environment_overrides)
    application = build_project_environment_application(
        environment,
        environment_id=environment_id,
    )
    if base_env is not None:
        # HERMETIC path: interpolate against the injected base with the environment's
        # variables/`.env` overlay on top (overlay wins), WITHOUT touching os.environ.
        interpolation_env = {**base_env, **application.variables}
        spec = load_yaml_spec(
            workflow_path,
            overrides=overrides,
            runtime_defaults=project.defaults.runtime or None,
            load_dotenv=False,
            env=interpolation_env,
        )
    else:
        with project_environment_context(application):
            spec = load_yaml_spec(
                workflow_path,
                overrides=overrides,
                runtime_defaults=project.defaults.runtime or None,
                load_dotenv=False,
            )
    if components:
        spec._component_provenance = tuple(component.to_dict() for component in components)
    # Profile-applied paths live on the component provenance records;
    # yaml_overrides provenance covers only true environment overrides (the
    # loader's generic record is replaced either way).
    spec._override_provenance = (
        YamlOverrideProvenance(
            source="project_environment",
            project_name=project.name,
            environment_id=environment_id,
            environment_name=environment.name,
            workflow_id=workflow_id,
            override_paths=yaml_override_paths(environment_overrides),
        )
        if environment_overrides
        else None
    )
    return ProjectResolvedWorkflow(
        project=project,
        workflow_id=workflow_id,
        workflow_path=workflow_path,
        environment_id=environment_id,
        environment=environment,
        application=application,
        spec=spec,
        components=components,
        base_env=base_env,
    )


@dataclass(frozen=True)
class ProjectSubworkflowResolution:
    """The resolution of a workflow's sub-workflow references (#55 §3.4).

    ``records`` maps each of the PARENT's direct ``workflow:`` / ``map.workflow``
    references to its resolved child identity (for ``create_workflow``).
    ``workflow_classes`` / ``activities`` are the TRANSITIVE closure — every
    descendant workflow class and the union of their activities — so a worker can
    register the whole child tree on the same task queue the parent inherits.
    ``child_specs`` is the same transitive closure of resolved child SPECS (workflow
    id -> spec), so the worker build can MERGE each child's prompt registry into the
    ONE registry it serves (#748) instead of forcing every child prompt to be
    duplicated into the parent.
    """

    records: dict[str, Any]
    workflow_classes: tuple[type, ...]
    activities: dict[str, Any]
    child_specs: dict[str, Any] = field(default_factory=dict)


def resolve_subworkflows_for(
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
) -> ProjectSubworkflowResolution:
    """Resolve the sub-workflow references of an already-resolved parent workflow —
    THE resolution entry point (#55 §3.4). Returns an EMPTY resolution when the
    parent references no siblings (the V1 path).

    Each referenced sibling is resolved under the SAME environment and built into its
    generated workflow class (recursively, so a child's own references resolve too);
    the child's registered type + digest are read off the built class, so a
    child-graph edit moves the parent digest by construction. Guarantees:

    - ACYCLIC: a reference cycle (A->B->A) is rejected via the per-path ``visiting``
      chain — computing A's digest would need B's, which needs A's.
    - RESOLVE-ONCE: a diamond-shared child (A->B->D, A->C->D) resolves exactly once
      per call via a shared memo (safe across paths — resolution is a pure function
      of workflow id + environment).
    - ONE ACTIVITY NAMESPACE: children register on the parent's worker, so a name
      declared by two workflows in the tree must be the IDENTICAL definition
      (canonical-JSON equality; TS edition enforces the same rule) — a divergent
      collision rejects naming both declaring workflows.
    - ROOT SEARCH ATTRIBUTE: the parent deployment's configured
      ``workflow_search_attribute`` (or None) is inherited by every descendant's
      child-start options, overriding intermediate specs' own config (TS
      start-context parity, #55 §6).
    """
    from typeflux.yaml.imports import collect_activities
    from typeflux.yaml.workflow import collect_subworkflow_references

    out_classes: dict[str, type] = {}
    out_activities: dict[str, Any] = {}
    out_child_specs: dict[str, Any] = {}
    references = collect_subworkflow_references(resolved.spec)
    # Cross-workflow activity-collision registry, seeded with EVERYTHING the parent
    # contributes — YAML definitions AND module-imported activities — through the
    # same registration path the children use: a module-only parent activity must
    # collide with a divergent child definition just like a YAML one. Collection
    # needs the env context, so the reference-free (V1) path skips it entirely.
    activity_owners: dict[str, tuple[str, Any]] = {}
    if references:
        with project_environment_context(resolved.application):
            parent_activities = collect_activities(resolved.spec)
        _register_activity_owners(
            resolved.workflow_id, resolved.spec, parent_activities, activity_owners
        )
    records = _resolve_subworkflow_refs(
        project,
        references,
        referencing_spec=resolved.spec,
        referencing_workflow_id=resolved.workflow_id,
        environment_id=resolved.environment_id,
        visiting=(resolved.workflow_id,),
        memo={},
        out_classes=out_classes,
        out_activities=out_activities,
        out_child_specs=out_child_specs,
        activity_owners=activity_owners,
        inherited_search_attribute=resolved.spec.runtime.temporal.workflow_search_attribute,
        # Children resolve under the SAME interpolation base as the parent (#760):
        # a hermetically-resolved parent's children must never read os.environ.
        base_env=resolved.base_env,
    )
    return ProjectSubworkflowResolution(
        records=records,
        workflow_classes=tuple(out_classes.values()),
        activities=out_activities,
        child_specs=out_child_specs,
    )


def create_workflow_with_subworkflows(
    project: TypefluxProjectSpec,
    resolved: ProjectResolvedWorkflow,
    *,
    activities: dict[str, Any] | None = None,
) -> tuple[type, ProjectSubworkflowResolution, dict[str, Any]]:
    """Build a resolved workflow's generated class with its sub-workflow references
    resolved — the one helper shared by the projection call sites (bundle, catalog,
    drain, runs, validation) and the natural future caching point (#55 §3.4).

    Returns ``(workflow_class, subworkflows, activities)``. Pass ``activities`` when
    the caller already collected them (validation); otherwise they are collected here
    under the environment context. Sync (takes the env lock) — async callers run it
    via ``asyncio.to_thread``.
    """
    from typeflux.yaml.imports import collect_activities
    from typeflux.yaml.workflow import create_workflow

    subworkflows = resolve_subworkflows_for(project, resolved)
    with project_environment_context(resolved.application):
        if activities is None:
            activities = collect_activities(resolved.spec)
        workflow_class = create_workflow(
            resolved.spec, activities, subworkflows=subworkflows.records
        )
    return workflow_class, subworkflows, activities


def _yaml_definition_form(definition: Any) -> dict[str, Any]:
    """The canonical comparison form of a YAML activity definition (cross-edition
    collision rule: canonical-JSON equality of the definition; TS mirrors this)."""
    return {"kind": "yaml", "definition": definition.model_dump(mode="json")}


def _activity_definition_forms(
    spec: TypefluxYamlSpec, activities: dict[str, Any]
) -> dict[str, Any]:
    """name -> canonical definition form for every activity a spec contributes.
    YAML definitions compare by their canonical JSON dump; module-loaded (Python
    code) activities compare by their definition source (same module+attribute =
    the same code object = identical by construction)."""
    yaml_forms = {
        definition.name: _yaml_definition_form(definition)
        for definition in spec.activities.definitions
    }
    forms: dict[str, Any] = {}
    for name, activity in activities.items():
        forms[name] = yaml_forms.get(name) or {
            "kind": "python",
            "source": activity.definition_source.to_dict(),
        }
    return forms


def _register_activity_owners(
    workflow_id: str,
    spec: TypefluxYamlSpec,
    activities: dict[str, Any],
    activity_owners: dict[str, tuple[str, Any]],
) -> None:
    # One activity namespace per worker (#55 §3.4): the parent co-registers every
    # descendant's activities, so a name declared by two workflows must be the
    # IDENTICAL definition (then it is deduplicated); different definitions under one
    # name would silently execute whichever registered first.
    for name, form in _activity_definition_forms(spec, activities).items():
        existing = activity_owners.get(name)
        if existing is None:
            activity_owners[name] = (workflow_id, form)
        elif existing[1] != form:
            raise ProjectEnvironmentError(
                f"activity name collision across the sub-workflow tree: {name!r} is "
                f"declared by workflow {existing[0]!r} and workflow {workflow_id!r} with "
                "different definitions — children register on the parent's worker, "
                "sharing one activity namespace, so a colliding name must be an "
                "identical definition (#55)"
            )


def _subworkflow_reference_steps(spec: TypefluxYamlSpec) -> dict[str, str]:
    """ref -> the FIRST referencing step id, for error context (#55)."""
    from typeflux.yaml.spec import (
        WorkflowMapStepSpec,
        WorkflowParallelStepSpec,
        WorkflowSubworkflowStepSpec,
    )

    out: dict[str, str] = {}

    def walk(steps: Sequence[Any]) -> None:
        for step in steps:
            if isinstance(step, WorkflowSubworkflowStepSpec):
                out.setdefault(step.workflow, step.id)
            elif isinstance(step, WorkflowMapStepSpec) and step.map.workflow is not None:
                out.setdefault(step.map.workflow, step.id)
            elif isinstance(step, WorkflowParallelStepSpec):
                for branch in step.parallel.branches:
                    walk(branch.steps)

    walk(spec.workflow.steps)
    return out


def _resolve_subworkflow_refs(
    project: TypefluxProjectSpec,
    references: Sequence[str],
    *,
    referencing_spec: TypefluxYamlSpec,
    referencing_workflow_id: str,
    environment_id: str,
    visiting: tuple[str, ...],
    memo: dict[str, Any],
    out_classes: dict[str, type],
    out_activities: dict[str, Any],
    out_child_specs: dict[str, Any],
    activity_owners: dict[str, tuple[str, Any]],
    inherited_search_attribute: str | None,
    base_env: Mapping[str, str] | None,
) -> dict[str, Any]:
    from typeflux.yaml.imports import collect_activities
    from typeflux.yaml.workflow import (
        ResolvedSubworkflow,
        collect_subworkflow_references,
        create_workflow,
    )

    reference_steps = _subworkflow_reference_steps(referencing_spec)
    records: dict[str, Any] = {}
    for ref in references:
        if ref in records:
            continue
        if ref in visiting:
            chain = " -> ".join((*visiting, ref))
            raise ProjectEnvironmentError(
                f"sub-workflow reference cycle detected: {chain} — a workflow must not "
                "transitively invoke itself (#55)"
            )
        if ref in memo:
            # A diamond-shared child resolves once per resolve_subworkflows_for call:
            # resolution is a pure function of (workflow id, environment), so reuse
            # across reference paths is safe; only cycle detection stays per-path.
            records[ref] = memo[ref]
            continue
        step_id = reference_steps.get(ref)
        referencing_label = (
            f"step {step_id!r} of workflow {referencing_workflow_id!r}"
            if step_id is not None
            else f"workflow {referencing_workflow_id!r}"
        )
        try:
            child = resolve_project_workflow(
                project,
                workflow_id=ref,
                environment_id=environment_id,
                base_env=base_env,
            )
        except ProjectEnvironmentError as exc:
            if f"unknown project workflow: {ref}" in str(exc):
                # A typo'd / undeclared reference: name the referencing step and the
                # project-manifest requirement, not the resolver's bare message.
                raise ProjectEnvironmentError(
                    f"{referencing_label} references sibling workflow {ref!r}, which is "
                    "not declared in the project manifest — sub-workflow references "
                    "resolve through workflow ids declared in typeflux.project.yaml (#55)"
                ) from exc
            # A DECLARED sibling whose own resolution is broken (bad profile, missing
            # env var, ...): surface the sibling's real error, prefixed with who
            # references it.
            raise ProjectEnvironmentError(
                f"sibling workflow {ref!r} (referenced by {referencing_label}) failed "
                f"to resolve: {exc}"
            ) from exc
        except Exception as exc:
            # Non-ProjectEnvironmentError resolution failures (a sibling's YAML
            # ValidationError, interpolation errors, ...) get the same attribution —
            # and become 422-mapping ProjectEnvironmentErrors for CP reads.
            raise ProjectEnvironmentError(
                f"sibling workflow {ref!r} (referenced by {referencing_label}) failed "
                f"to resolve: {exc}"
            ) from exc
        try:
            with project_environment_context(child.application):
                child_activities = collect_activities(child.spec)
                _register_activity_owners(ref, child.spec, child_activities, activity_owners)
                child_records = _resolve_subworkflow_refs(
                    project,
                    collect_subworkflow_references(child.spec),
                    referencing_spec=child.spec,
                    referencing_workflow_id=ref,
                    environment_id=environment_id,
                    visiting=(*visiting, ref),
                    memo=memo,
                    out_classes=out_classes,
                    out_activities=out_activities,
                    out_child_specs=out_child_specs,
                    activity_owners=activity_owners,
                    inherited_search_attribute=inherited_search_attribute,
                    base_env=base_env,
                )
                child_cls = create_workflow(
                    child.spec,
                    child_activities,
                    subworkflows=child_records,
                    inherited_search_attribute=inherited_search_attribute,
                )
        except ProjectEnvironmentError:
            raise  # already contextualized (cycle / nested ref / collision)
        except Exception as exc:
            # A declared sibling whose GRAPH is broken (unknown activity, type
            # mismatch, bad schema ref): the sibling's real error, attributed —
            # and a ProjectEnvironmentError so control-plane reads answer 422,
            # not 500.
            raise ProjectEnvironmentError(
                f"sibling workflow {ref!r} (referenced by {referencing_label}) failed "
                f"to build: {exc}"
            ) from exc
        workflow_type = getattr(child_cls, "__typeflux_workflow_type__")
        out_classes.setdefault(workflow_type, child_cls)
        # The resolved child spec joins the transitive registry-merge closure (#748); keyed by
        # workflow id, resolve-once (a diamond-shared child is recorded by the first path).
        out_child_specs.setdefault(ref, child.spec)
        for name, activity in child_activities.items():
            out_activities.setdefault(name, activity)
        record = ResolvedSubworkflow(
            workflow_id=ref,
            workflow_type=workflow_type,
            workflow_name=getattr(child_cls, "__typeflux_workflow_name__"),
            project=getattr(child_cls, "__typeflux_project__"),
            spec_digest=getattr(child_cls, "__typeflux_spec_digest__"),
            input_type=getattr(child_cls, "__typeflux_input_type__"),
            output_type=getattr(child_cls, "__typeflux_output_type__"),
        )
        memo[ref] = record
        records[ref] = record
    return records


def _resolved_profile_overrides(
    project: TypefluxProjectSpec,
    *,
    workflow: Any,
    environment: ProjectEnvironmentSpec,
    workflow_id: str,
) -> tuple[dict[str, Any], tuple[AppliedComponentProfile, ...]]:
    workflow_selection = dict(getattr(workflow, "profiles", {}) or {})
    environment_workflow = environment.workflows.get(workflow_id)
    environment_selection = dict(environment_workflow.profiles) if environment_workflow else {}
    validate_profile_selection(
        workflow_selection, context=f"workflow {workflow_id!r} profile selection"
    )
    validate_profile_selection(
        environment_selection,
        context=f"environment {environment.name!r} profile selection for {workflow_id!r}",
    )
    if not workflow_selection and not environment_selection:
        return {}, ()
    selected = resolve_selected_profiles(
        project,
        workflow_selection=workflow_selection,
        environment_selection=environment_selection,
    )
    return profile_overrides_and_provenance(selected)


def load_resolved_project_yaml_spec(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
) -> TypefluxYamlSpec:
    return resolve_project_workflow(
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
    ).spec


def _project_workflow(project: TypefluxProjectSpec, workflow_id: str):
    for workflow in discover_project_workflows(project):
        if workflow.id == workflow_id:
            return workflow
    raise ProjectEnvironmentError(f"unknown project workflow: {workflow_id}")


def _validate_environment_workflow_ids(
    project: TypefluxProjectSpec,
    environment: ProjectEnvironmentSpec,
) -> None:
    workflow_ids = {workflow.id for workflow in project.workflows}
    for workflow_id in environment.workflows:
        if workflow_id not in workflow_ids:
            raise ProjectEnvironmentError(
                f"environment {environment.name!r} references unknown workflow: {workflow_id}"
            )


def _merged_overrides(
    environment: ProjectEnvironmentSpec,
    workflow_id: str,
) -> dict[str, Any]:
    overrides = _deep_merge({}, environment.overrides)
    workflow = environment.workflows.get(workflow_id)
    if workflow is not None:
        overrides = _deep_merge(overrides, workflow.overrides)
    return overrides


def _deep_merge(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(merged[key], value)
            continue
        merged[key] = value
    return merged


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in dotenv_values(path).items():
        if value is None:
            continue
        _validate_env_key(key)
        values[key] = value
    return values


def _validate_allowed_overrides(value: dict[str, Any], *, prefix: str) -> None:
    validate_yaml_overrides(value, prefix=prefix)


def _validate_env_key(value: str) -> None:
    import re

    if not re.match(_ENV_KEY_PATTERN, value):
        raise ValueError(f"environment variable name is invalid: {value!r}")


def _stringify_env_value(value: EnvValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _resolve_project_path(project: TypefluxProjectSpec, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (project.project_dir / path).resolve()


def _resolve_environment_path(environment: ProjectEnvironmentSpec, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (environment.profile_dir / path).resolve()


def _temporal_summary(spec: TypefluxYamlSpec) -> dict[str, Any]:
    temporal = spec.runtime.temporal
    tls = temporal.tls
    if isinstance(tls, bool):
        tls_enabled = tls
        tls_mode: Literal["disabled", "boolean", "custom"] = "boolean" if tls else "disabled"
    else:
        tls_enabled = True
        tls_mode = "custom"
    return {
        "address": temporal.address,
        "namespace": temporal.namespace,
        "tls_enabled": tls_enabled,
        "tls_mode": tls_mode,
        "api_key_configured": secret_value_configured(temporal.api_key),
    }


__all__ = [
    "ProjectEnvironmentApplication",
    "ProjectEnvironmentEnvFileSpec",
    "ProjectEnvironmentError",
    "ProjectEnvironmentFileStatus",
    "ProjectEnvironmentSpec",
    "ProjectEnvironmentWorkflowSpec",
    "ProjectResolvedWorkflow",
    "ProjectResolvedWorkflowSummary",
    "ProjectSubworkflowResolution",
    "build_project_environment_application",
    "create_workflow_with_subworkflows",
    "load_project_environment",
    "load_resolved_project_yaml_spec",
    "async_project_environment_context",
    "project_environment_context",
    "resolve_project_workflow",
    "resolve_subworkflows_for",
]
