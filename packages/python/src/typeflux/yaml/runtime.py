from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol, cast

from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactPolicy
from typeflux.core.contracts import (
    AIActivity,
    ChatMessage,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
    ReviewCommand,
    WorkflowLifecycleStatus,
    YamlWorkflowActivity,
)
from typeflux.core.errors import LifecycleBindingError
from typeflux.core.subjects import (
    SUBJECT_IDS_SEARCH_ATTRIBUTE,
    normalize_subject_ids,
    resolve_subject_ids,
)
from typeflux.env import load_env
from typeflux.execution.controls import (
    ProviderCallLimits,
    ProviderRateLimitPolicy,
    ProviderRateLimitProviderPolicy,
    ProviderRetryPolicy,
)
from typeflux.execution.observer import AIActivityObserver
from typeflux.execution.preflight import PreflightReport, preflight_ai_activities
from typeflux.execution.starter import execute_workflow as execute_typeflux_workflow
from typeflux.execution.worker import TypefluxWorker, build_activity_rollup
from typeflux.manifests._common import canonical_json
from typeflux.metadata import (
    ActivityContextContributor,
    AdmissionContributor,
    CompensationContributor,
    ComponentProvenanceContributor,
    LifecycleOperationContributor,
    LifecycleOperationMetadataContext,
    MetadataContributor,
    PolicyContributor,
    RiskTierContributor,
    RuntimePlacementContributor,
    SecretReferenceContributor,
    TemporalConnectionContributor,
    YamlLifecycleContributor,
    YamlOverrideContributor,
    YamlWorkflowContributor,
    lifecycle_operation_contribution,
)
from typeflux.metadata import (
    redaction_exclusions as metadata_redaction_exclusions,
)
from typeflux.observability import (
    LangfuseObservabilityBackend,
    NoOpObservabilityBackend,
    ObservabilityBackend,
    TraceListQuery,
    TraceSearchQuery,
    diff_traces,
    inspect_trace,
)
from typeflux.observability.redaction import (
    NoOpRedactor,
    Redactor,
    RegexPIIRedactor,
    RegexRedactionRule,
)
from typeflux.prompts import (
    InlinePromptRegistry,
    LangfusePromptRegistry,
    LangSmithPromptRegistry,
    PromptRegistry,
)
from typeflux.providers import (
    AnthropicProvider,
    GeminiProvider,
    ModelProvider,
    OpenAIProvider,
    provider_default_params,
    validate_provider_params_supported,
)
from typeflux.testing import FakeProvider
from typeflux.yaml.identity import GENERATOR_VERSION, SPEC_DIGEST_ALGORITHM
from typeflux.yaml.imports import (
    collect_activities,
    import_object,
    validate_extension_class_import,
)
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.payload_codec import (
    PayloadCodecError,
    PayloadCodecSpec,
    build_payload_codec,
)
from typeflux.yaml.secrets import (
    resolve_custom_extension_config,
    resolve_optional_secret_text,
    secret_reference_records,
    secret_value_configured,
)
from typeflux.yaml.spec import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    InlinePromptSpec,
    ProviderLimitSpec,
    TypefluxYamlSpec,
)
from typeflux.yaml.tls import build_temporal_tls_config as _build_temporal_tls_config
from typeflux.yaml.workflow import (
    MapCallSpec,
    collect_subworkflow_references,
    create_workflow,
    create_yaml_workflow_runner,
    flatten_call_specs,
)

logger = logging.getLogger(__name__)

InlinePromptValue = str | ChatMessage | ResolvedPrompt | tuple[ChatMessage, ...]


class _GeneratedWorkflowClass(Protocol):
    run: Any


@dataclass(frozen=True)
class TypefluxYamlRuntime:
    spec: TypefluxYamlSpec
    client: Any
    worker: TypefluxWorker
    workflow_class: type
    activities: dict[str, YamlWorkflowActivity]
    registry: PromptRegistry
    provider: ModelProvider
    artifact_policy: ArtifactPolicy
    observer: AIActivityObserver | None
    observability: ObservabilityBackend
    policy_guard: Any | None = None
    langfuse_client: Any | None = None

    @classmethod
    async def from_file(cls, path: str) -> TypefluxYamlRuntime:
        return await build_runtime(load_yaml_spec(path))

    async def execute_workflow(
        self,
        input_value: Any,
        *,
        id: str,
        task_queue: str | None = None,
        result_type: type | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        subject_ids: Sequence[str] | None = None,
        **execute_kwargs: Any,
    ) -> Any:
        workflow_task_queue = task_queue or self.spec.task_queue
        workflow_class = cast(_GeneratedWorkflowClass, self.workflow_class)
        resolved_subject_ids = _resolve_workflow_subject_ids(self.spec, input_value, subject_ids)
        metadata_contributors = _yaml_metadata_contributors(
            self.spec,
            self.workflow_class,
            input_value,
            policy_guard=self.policy_guard,
        )
        execute_kwargs["memo"] = _workflow_start_memo(
            execute_kwargs.get("memo"),
            self.workflow_class,
        )
        search_attributes = _workflow_start_search_attributes(
            execute_kwargs.get("search_attributes"),
            spec=self.spec,
            subject_ids=resolved_subject_ids,
        )
        if search_attributes is not None:
            execute_kwargs["search_attributes"] = search_attributes
        # #715 slice 4: pin the execution's subject set for the subject-scoped codec
        # BEFORE the start encodes the input (the execution does not exist in
        # visibility yet, so the registry is the only resolvable channel). Registers
        # the SAME ids stamped into TypefluxSubjectIds; the empty set pins "no
        # subjects". No-op without payload_codec.subject_scope.
        _register_subject_scope_binding(self.client, id, resolved_subject_ids)
        return await execute_typeflux_workflow(
            client=self.client,
            workflow=workflow_class.run,
            input_value=input_value,
            id=id,
            task_queue=workflow_task_queue,
            result_type=result_type,
            trace_writer=self.observability.writer,
            workflow_name=self.spec.workflow.name,
            tags=tags,
            metadata=metadata,
            subject_ids=resolved_subject_ids,
            activities=tuple(self.activities.values()),
            activity_rollup=(
                build_activity_rollup(
                    tuple(self.activities.values()),
                    self.registry,
                    self.provider,
                )
                if self.observability.writer.enabled
                else None
            ),
            metadata_contributors=metadata_contributors,
            include_execution_manifest=self.spec.runtime.observability.execution_manifest,
            **execute_kwargs,
        )

    async def start_workflow(
        self,
        input_value: Any,
        *,
        id: str,
        task_queue: str | None = None,
        result_type: type | None = None,
        subject_ids: Sequence[str] | None = None,
        **start_kwargs: Any,
    ) -> Any:
        """Start the workflow without awaiting its result; returns the handle.

        Applies the same identity memo (spec digest + logical workflow name)
        and opt-in logical-name search attributes as ``execute_workflow``.
        No root workflow observation is opened — a root trace started by a
        non-blocking call could never be closed correctly. Root traces come
        from ``execute_workflow``/``project submit``; worker-side activity and
        generation observations still correlate by workflow id.
        """
        workflow_class = cast(_GeneratedWorkflowClass, self.workflow_class)
        resolved_subject_ids = _resolve_workflow_subject_ids(self.spec, input_value, subject_ids)
        start_kwargs["memo"] = _workflow_start_memo(
            start_kwargs.get("memo"),
            self.workflow_class,
        )
        search_attributes = _workflow_start_search_attributes(
            start_kwargs.get("search_attributes"),
            spec=self.spec,
            subject_ids=resolved_subject_ids,
        )
        if search_attributes is not None:
            start_kwargs["search_attributes"] = search_attributes
        if result_type is not None:
            start_kwargs["result_type"] = result_type
        # #715 slice 4: same pre-start subject-binding pin as execute_workflow.
        _register_subject_scope_binding(self.client, id, resolved_subject_ids)
        return await self.client.start_workflow(
            workflow_class.run,
            input_value,
            id=id,
            task_queue=task_queue or self.spec.task_queue,
            **start_kwargs,
        )

    def inspect_trace(self, trace_id: str):
        return inspect_trace(self.observability.reader, trace_id)

    def list_traces(self, query: TraceListQuery | None = None, **kwargs: Any):
        return self.observability.reader.list_traces(query or TraceListQuery(**kwargs))

    def search_traces(self, query: TraceSearchQuery | None = None, **kwargs: Any):
        return self.observability.reader.search_traces(query or TraceSearchQuery(**kwargs))

    def diff_traces(self, left_trace_id: str, right_trace_id: str):
        return diff_traces(self.observability.reader, left_trace_id, right_trace_id)

    async def _verify_execution_binding(self, handle: Any, workflow_id: str) -> str | None:
        """Confirm the execution at this id is the workflow+project we route.

        A lifecycle op addresses an execution by id; an id collision (reuse, a
        typo, or a cross-project hijack) would otherwise let a query/signal land
        on a foreign execution. We ``describe`` the execution once and require
        both:

        - its registered ``workflow_type`` equals this runtime's bound type
          (workflow identity *and* graph version — Temporal-enforced, hard to
          spoof), and
        - its ``typeflux_project`` memo equals this runtime's project.

        Both are required and fail closed (:class:`LifecycleBindingError`).
        There is no legacy fallback: an execution started outside the Typeflux
        runtime carries no identity memo, so we cannot vouch for it and refusing
        the op is the correct outcome (pre-adoption, no back-compat; #320).
        """
        expected_type = getattr(self.workflow_class, "__typeflux_workflow_type__", None)
        expected_project = getattr(self.workflow_class, "__typeflux_project__", None)
        description = await handle.describe()
        actual_type = getattr(description, "workflow_type", None)
        if expected_type is None or actual_type != expected_type:
            raise LifecycleBindingError(
                f"lifecycle operation refused: execution {workflow_id!r} has workflow type "
                f"{actual_type!r}, not the bound type {expected_type!r}"
            )
        memo = await _describe_memo(description)
        actual_project = memo.get("typeflux_project")
        if actual_project != expected_project:
            raise LifecycleBindingError(
                f"lifecycle operation refused: execution {workflow_id!r} belongs to project "
                f"{actual_project!r}, not the bound project {expected_project!r}"
            )
        described_run_id = getattr(description, "run_id", None)
        return described_run_id if isinstance(described_run_id, str) else None

    async def _bound_handle(self, workflow_id: str, *, run_id: str | None = None) -> Any:
        """Get the Temporal handle for a lifecycle op, verified-bound (#320).

        Every lifecycle entry point acquires its handle here, so the binding
        check is inseparable from getting the handle — a future operation
        cannot reach an execution without first verifying it is the one we
        route.
        """
        handle = self.client.get_workflow_handle(workflow_id, run_id=run_id)
        bound_run_id = await self._verify_execution_binding(handle, workflow_id)
        if run_id is None and bound_run_id is not None:
            # Temporal query/signal calls on an id-only handle target the latest
            # run. Pin to the run that describe() verified so workflow-id reuse
            # cannot swap the target between verification and dispatch.
            handle = self.client.get_workflow_handle(workflow_id, run_id=bound_run_id)
        return handle

    async def _lifecycle_status_query(self, handle: Any) -> WorkflowLifecycleStatus:
        value = await handle.query(
            "typeflux_lifecycle_status",
            result_type=WorkflowLifecycleStatus,
        )
        return _coerce_lifecycle_status(value)

    async def query_lifecycle_status(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
        trace: bool = True,
    ) -> WorkflowLifecycleStatus:
        handle = await self._bound_handle(workflow_id, run_id=run_id)
        if not trace:
            # Internal/polling callers opt out so repeated status checks do not
            # each become a first-class lifecycle operation in the audit trail.
            return await self._lifecycle_status_query(handle)
        operation = _lifecycle_operation_payload(
            operation_type="query",
            operation_name="typeflux_lifecycle_status",
            workflow_name=self.spec.workflow.name,
            workflow_id=workflow_id,
            run_id=_handle_run_id(handle, run_id),
        )
        with self.observability.writer.observe_lifecycle_operation(
            operation_type="query",
            operation_name="typeflux_lifecycle_status",
            workflow_name=self.spec.workflow.name,
            workflow_id=workflow_id,
            run_id=_handle_run_id(handle, run_id),
            metadata=operation.operation_metadata,
            tags=list(operation.search_tags),
        ) as observation:
            status = await self._lifecycle_status_query(handle)
            completed_operation = _lifecycle_operation_payload(
                operation_type="query",
                operation_name="typeflux_lifecycle_status",
                workflow_name=self.spec.workflow.name,
                workflow_id=workflow_id,
                run_id=_handle_run_id(handle, run_id),
                status=status,
            )
            observation.update_metadata(dict(completed_operation.operation_metadata))
            return status

    async def wait_for_lifecycle_state(
        self,
        workflow_id: str,
        state: str,
        *,
        run_id: str | None = None,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 1.0,
    ) -> WorkflowLifecycleStatus:
        """Poll untraced until the workflow reports the lifecycle state.

        Polling is deliberately not recorded as curated lifecycle operations:
        a wait loop is plumbing, not an audit-trail event. Callers that want
        an auditable status check should call ``query_lifecycle_status`` once
        the wait returns. Transient query errors are tolerated until the
        deadline because the worker may not be polling yet.
        """
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        # Bind once: the (workflow_id, run_id) identity is fixed for the wait, so
        # re-verifying every poll tick would only add describe RPCs. A binding
        # failure is not transient — fail fast, before the tolerant poll loop.
        handle = await self._bound_handle(workflow_id, run_id=run_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_error: BaseException | None = None
        while True:
            try:
                current = await self._lifecycle_status_query(handle)
            except Exception as exc:
                last_error = exc
            else:
                if current.state == state:
                    return current
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"workflow {workflow_id!r} did not reach lifecycle state {state!r} "
                    f"within {timeout_seconds}s"
                ) from last_error
            await asyncio.sleep(poll_interval_seconds)

    async def request_lifecycle_cancel(
        self,
        workflow_id: str,
        reason: str | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        handle = await self._bound_handle(workflow_id, run_id=run_id)
        operation = _lifecycle_operation_payload(
            operation_type="signal",
            operation_name="typeflux_request_cancel",
            workflow_name=self.spec.workflow.name,
            workflow_id=workflow_id,
            run_id=_handle_run_id(handle, run_id),
            cancellation_requested=True,
        )
        with self.observability.writer.observe_lifecycle_operation(
            operation_type="signal",
            operation_name="typeflux_request_cancel",
            workflow_name=self.spec.workflow.name,
            workflow_id=workflow_id,
            run_id=_handle_run_id(handle, run_id),
            metadata=operation.operation_metadata,
            tags=list(operation.search_tags),
        ):
            await handle.signal("typeflux_request_cancel", reason)

    async def submit_lifecycle_review(
        self,
        workflow_id: str,
        command: ReviewCommand | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None:
        review = (
            command if isinstance(command, ReviewCommand) else ReviewCommand.model_validate(command)
        )
        review_gate, review_route_target = _review_gate_route(self.spec, review)
        handle = await self._bound_handle(workflow_id, run_id=run_id)
        operation = _lifecycle_operation_payload(
            operation_type="signal",
            operation_name="typeflux_submit_review",
            workflow_name=self.spec.workflow.name,
            workflow_id=workflow_id,
            run_id=_handle_run_id(handle, run_id),
            review_user_decision=review.user_decision if review_route_target is not None else None,
            review_route_target=review_route_target,
            review_gate=review_gate,
        )
        with self.observability.writer.observe_lifecycle_operation(
            operation_type="signal",
            operation_name="typeflux_submit_review",
            workflow_name=self.spec.workflow.name,
            workflow_id=workflow_id,
            run_id=_handle_run_id(handle, run_id),
            metadata=operation.operation_metadata,
            tags=list(operation.search_tags),
        ):
            await handle.signal("typeflux_submit_review", review)

    def preflight(self) -> PreflightReport:
        return preflight_ai_activities(
            activities=tuple(self.activities.values()),
            registry=self.registry,
            provider=self.provider,
            provider_default_params=provider_default_params(self.provider),
        )


@dataclass(frozen=True)
class PreparedRuntimeBuild:
    """The synchronous portion of a runtime build (#585).

    Everything before the Temporal connect — activity-module imports,
    workflow-class creation, observability/provider client construction — is
    blocking work. Callers on an event loop (the control-plane operations
    pin) run ``prepare_runtime_build`` in a worker thread and hand the result
    to ``build_runtime``; every other caller lets ``build_runtime`` prepare
    inline, exactly as before.
    """

    activities: dict[str, YamlWorkflowActivity]
    workflow_class: type
    observability: ObservabilityBackend
    plugin: Any
    observer: Any
    langfuse_client: Any
    registry: PromptRegistry
    provider: ModelProvider
    artifact_policy: ArtifactPolicy
    # The transitive sub-workflow CHILD classes (#55 §6): a child runs on THIS worker
    # (its own registered type), so it must be registered alongside the parent. Empty for
    # V1 / sub-workflow-free specs. The child ACTIVITIES are already merged into ``activities``.
    child_workflow_classes: tuple[type, ...] = ()


def prepare_runtime_build(
    spec: TypefluxYamlSpec,
    *,
    policy_guard: Any | None = None,
    subworkflow_records: dict[str, Any] | None = None,
    child_workflow_classes: tuple[type, ...] = (),
    child_activities: dict[str, YamlWorkflowActivity] | None = None,
    child_registry_specs: Sequence[tuple[str, TypefluxYamlSpec]] = (),
) -> PreparedRuntimeBuild:
    """Run the sync prelude of a runtime build; see ``PreparedRuntimeBuild``.

    Must run inside the project environment context (the env-var reads for
    secrets/config happen here); safe in a worker thread — module imports,
    client constructors, and os.environ reads under the held env lock.

    Sub-workflows (#55 §3.4): a caller with project context resolves the parent's
    references (``resolve_subworkflows_for``) and passes ``subworkflow_records`` (for the
    parent's ``create_workflow``), ``child_workflow_classes`` (the transitive child classes
    to co-register), and ``child_activities`` (the child activity union, merged under the
    parent's). Absent for V1 / single-spec builds — a sub-workflow parent loaded standalone
    then rejects at ``create_workflow``, naming the project-manifest requirement.
    """
    if policy_guard is None and spec.workflow.risk_tier in (
        "policy_gated",
        "human_gated",
        "prohibited",
    ):
        # #788 (audit B1): the single-spec path has no policy machinery by design, so an
        # elevated declared tier is INERT here. Warn loudly instead of failing (the
        # local-dev story stays intact; TYPEFLUX_EXPECTED_POLICY_HASH remains this
        # path's fail-close) and point at the surface that enforces it.
        logger.warning(
            "workflow declares risk_tier %r but no policy guard is attached — the declared "
            "tier is UNENFORCED on this single-spec runtime. Use the project edition with a "
            "policy binding (validation.targets + a risk_tiers dimension) to enforce it.",
            spec.workflow.risk_tier,
        )

    load_env()
    _enforce_runtime_policy_guard(spec, policy_guard)
    activities = collect_activities(spec)
    # Child activities register on the SAME worker (parent wins a name collision — within one
    # project an activity of a given name is one activity). They resolve their prompts against
    # this worker's registry, so a project that uses sub-workflows should share its prompt
    # registry/provider across parent and children (or supply the children's prompts too).
    if child_activities:
        activities = {**child_activities, **activities}
    workflow_class = create_workflow(spec, activities, subworkflows=subworkflow_records)
    observability = _build_observability(
        spec, policy_guard=policy_guard, child_specs=child_registry_specs
    )
    plugin = observability.writer.configure_temporal_plugin()
    metadata_contributors = _yaml_metadata_contributors(spec, policy_guard=policy_guard)
    observer = observability.writer.create_activity_observer(
        metadata_contributors=metadata_contributors
    )
    langfuse_client = getattr(observability.writer, "client", None)

    # Registry composition (#748): the ONE registry served by this (possibly composed) worker
    # is the MERGE of the parent's registry and every referenced child's (byte-identical dedupe,
    # loud conflicts). With no sub-workflows `child_registry_specs` is empty ⇒ the parent's
    # registry, unchanged.
    registry = _build_registry(spec, child_registry_specs)
    # Langfuse-instrumented OpenAI client only when the backend is Langfuse; the
    # LangSmith/OTEL backend captures the generation through its own span.
    for child_id, child_spec in child_registry_specs:
        _assert_consistent_custom_provider_config(spec, child_id, child_spec)
    provider = _build_provider(spec, enable_langfuse=_uses_langfuse(spec))
    _validate_activity_provider_params(provider, tuple(activities.values()))
    artifact_policy = _build_artifact_policy(spec)
    return PreparedRuntimeBuild(
        activities=activities,
        workflow_class=workflow_class,
        observability=observability,
        plugin=plugin,
        observer=observer,
        langfuse_client=langfuse_client,
        registry=registry,
        provider=provider,
        artifact_policy=artifact_policy,
        child_workflow_classes=child_workflow_classes,
    )


async def build_runtime(
    spec: TypefluxYamlSpec,
    *,
    policy_guard: Any | None = None,
    prepared: PreparedRuntimeBuild | None = None,
    subworkflow_records: dict[str, Any] | None = None,
    child_workflow_classes: tuple[type, ...] = (),
    child_activities: dict[str, YamlWorkflowActivity] | None = None,
    child_registry_specs: Sequence[tuple[str, TypefluxYamlSpec]] = (),
    subject_keystore: Any | None = None,
) -> TypefluxYamlRuntime:
    """Connect and assemble the runtime, preparing inline unless given.

    A caller passing ``prepared`` must have built it via
    ``prepare_runtime_build(spec, policy_guard=policy_guard)`` with these
    same arguments: the prelude's policy enforcement ran against *that*
    spec/guard pair, so a mismatched ``prepared`` would silently skip
    enforcement for the pair passed here.

    Sub-workflows (#55 §3.4): pass ``subworkflow_records`` / ``child_workflow_classes`` /
    ``child_activities`` (from ``resolve_subworkflows_for``) so the parent's children register
    on this worker; ignored when ``prepared`` is supplied (it already carries them).

    ``subject_keystore`` (#715 slice 4) injects the crypto-shred keystore backend for a
    spec declaring ``payload_codec.subject_scope``; ``None`` builds the process-local
    in-memory reference backend (test/dev only — see docs/privacy.md).
    """
    if prepared is None:
        prepared = prepare_runtime_build(
            spec,
            policy_guard=policy_guard,
            subworkflow_records=subworkflow_records,
            child_workflow_classes=child_workflow_classes,
            child_activities=child_activities,
            child_registry_specs=child_registry_specs,
        )
    codec_spec = spec.runtime.temporal.payload_codec
    if codec_spec is not None and codec_spec.subject_scope is not None:
        # #715 slice 4 boundary: a child's start payloads are encoded (with the CHILD's
        # serialization context) before the child exists, so its subject binding cannot
        # be resolved. Reject the combination loudly rather than fail mid-workflow.
        # BOTH signals are checked (Bugbot): the SPEC GRAPH (what the YAML declares —
        # `workflow:` steps and `map.workflow` fan-outs, recursively; TS
        # specReferencesSubworkflows parity, and not bypassable by omitting resolved
        # child classes) and the resolved child classes (covers programmatically
        # injected children the spec walk cannot see).
        if prepared.child_workflow_classes or collect_subworkflow_references(spec):
            raise ValueError(
                "runtime.temporal.payload_codec.subject_scope does not yet support "
                "sub-workflow composition (#715 slice 4): a child's input is encoded before "
                "the child execution exists, so its subject binding cannot be resolved. "
                "Remove subject_scope or the sub-workflow references."
            )
        if subject_keystore is None:
            # SOLE-OWNER justification for the in-memory default (#715 Bugbot): this
            # runtime constructs BOTH the starting client and the worker in this ONE
            # process, sharing one data converter — every encode and decode goes
            # through the same codec + keystore instance, so a process-local reference
            # backend is coherent HERE (and only here; the live suite proves the
            # round-trip). A split starter/worker deployment must inject a shared
            # backend — see docs/privacy.md 'Keystore backends'.
            from typeflux.yaml.subject_keystore import InMemorySubjectKeystore

            subject_keystore = InMemorySubjectKeystore()
    # The keystore kwarg is passed ONLY when a caller injected one: an injected keystore
    # must reach the codec or fail LOUDLY (a connect seam without the parameter raises
    # TypeError — never silently dropped), while the spec-driven subject_scope wrap
    # itself lives inside _connect_client and needs no kwarg for the default backend.
    if subject_keystore is not None:
        client = await _connect_client(
            spec, plugin=prepared.plugin, subject_keystore=subject_keystore
        )
    else:
        client = await _connect_client(spec, plugin=prepared.plugin)
    if spec.workflow.version is not None:
        await _enforce_frozen_version_label(client, prepared.workflow_class)
    worker = TypefluxWorker(
        client=client,
        task_queue=spec.task_queue,
        activities=tuple(prepared.activities.values()),
        registry=prepared.registry,
        provider=prepared.provider,
        artifact_policy=prepared.artifact_policy,
        observer=prepared.observer,
        observability=prepared.observability,
        # Co-register the transitive sub-workflow child classes (#55 §6): a child runs on
        # this worker under its own registered type, so it must be registered too.
        workflows=[prepared.workflow_class, *prepared.child_workflow_classes],
        provider_rate_limit_policy=_build_provider_rate_limit_policy(spec),
        provider_retry_policy=_build_provider_retry_policy(spec),
        provider_model_policy_guard=policy_guard,
        workflow_runner=create_yaml_workflow_runner(),
    )
    return TypefluxYamlRuntime(
        spec=spec,
        client=client,
        worker=worker,
        workflow_class=prepared.workflow_class,
        activities=prepared.activities,
        registry=prepared.registry,
        provider=prepared.provider,
        artifact_policy=prepared.artifact_policy,
        observer=prepared.observer,
        observability=prepared.observability,
        policy_guard=policy_guard,
        langfuse_client=prepared.langfuse_client,
    )


def _resolve_workflow_subject_ids(
    spec: TypefluxYamlSpec,
    input_value: Any,
    explicit: Sequence[str] | None,
) -> tuple[str, ...]:
    """Resolve the subject ids for a start (#715 slice 1).

    A NON-EMPTY explicit ``subject_ids`` override wins over the declarative
    ``subjects:`` extraction (the runtime API is authoritative when a caller
    hand-wires them); otherwise the declarative selectors pull id(s) off the
    validated input. An EMPTY override is treated as "no override provided" and
    falls through to extraction: subjects are erasure-critical, so there is no
    legitimate "explicitly no subjects" opt-out when the spec declares required
    subjects — ``subject_ids=[]`` must never silently bypass them (#715 review
    round 2). Both paths validate non-empty strings and de-dup; a required
    selector that resolves to nothing raises here, at start.
    """

    if explicit:
        return normalize_subject_ids(explicit)
    subject_inputs = tuple(item.to_subject_input() for item in spec.workflow.subjects)
    return resolve_subject_ids(input_value, subject_inputs)


def _workflow_start_search_attributes(
    existing: Any,
    *,
    spec: TypefluxYamlSpec,
    subject_ids: Sequence[str] = (),
) -> Any:
    # The logical-name keyword attribute gives one visibility query across
    # versioned workflow types. Caller-supplied pairs are preserved; the
    # configured Typeflux keys are authoritative on conflict.
    #
    # `TypefluxSubjectIds` (#715 slice 1) is stamped INDEPENDENTLY of the opt-in
    # `workflow_search_attribute`: it is the fixed erasure index, so any execution
    # WITH subjects gets it even when no logical-name attribute is configured.
    attribute_name = spec.runtime.temporal.workflow_search_attribute
    if attribute_name is None and not subject_ids:
        return existing

    from temporalio.common import (
        SearchAttributeKey,
        SearchAttributePair,
        TypedSearchAttributes,
    )

    typeflux_names: set[str] = set()
    pairs: list[Any] = []
    if attribute_name is not None:
        typeflux_names.add(attribute_name)
        pairs.append(
            SearchAttributePair(SearchAttributeKey.for_keyword(attribute_name), spec.workflow.name)
        )
    if subject_ids:
        typeflux_names.add(SUBJECT_IDS_SEARCH_ATTRIBUTE)
        subject_key = SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE)
        pairs.append(SearchAttributePair(subject_key, list(subject_ids)))
    if existing is not None:
        if not isinstance(existing, TypedSearchAttributes):
            raise TypeError("workflow search_attributes must be TypedSearchAttributes")
        pairs.extend(pair for pair in existing if pair.key.name not in typeflux_names)
    return TypedSearchAttributes(pairs)


def _workflow_identity_memo(workflow_class: type | None) -> dict[str, str]:
    # DRIFT GUARD: these literal keys are mirrored by the CHILD identity memo
    # (yaml/workflow.py `_child_identity_memo` = this set ∪
    # {"typeflux_parent_workflow_id"}); test_yaml_subworkflows pins the sets equal.
    # Renaming a key here without the counterpart silently breaks parent->child
    # correlation (the CP `children` listing joins on these exact keys). #55 §9
    memo: dict[str, str] = {}
    spec_digest = getattr(workflow_class, "__typeflux_spec_digest__", None)
    if isinstance(spec_digest, str):
        memo["typeflux_spec_digest"] = spec_digest
    workflow_name = getattr(workflow_class, "__typeflux_workflow_name__", None)
    if isinstance(workflow_name, str):
        memo["typeflux_workflow"] = workflow_name
    # The project boundary: nothing else on an execution identifies its project,
    # so the lifecycle binding check (#320) reads it from here.
    project = getattr(workflow_class, "__typeflux_project__", None)
    if isinstance(project, str):
        memo["typeflux_project"] = project
    return memo


def _workflow_start_memo(existing: Any, workflow_class: type | None) -> dict[str, Any]:
    identity_memo = _workflow_identity_memo(workflow_class)
    if existing is None:
        return identity_memo
    if not isinstance(existing, Mapping):
        raise TypeError("workflow memo must be a mapping")
    return {**dict(existing), **identity_memo}


async def _enforce_frozen_version_label(client: Any, workflow_class: type) -> None:
    # A workflow.version label is a frozen pointer to one graph. Compare the
    # loaded spec digest against the most recent execution started under the
    # same versioned type; executions without the identity memo (or backends
    # without visibility support) are skipped, so the check is best-effort.
    workflow_type = getattr(workflow_class, "__typeflux_workflow_type__", None)
    spec_digest = getattr(workflow_class, "__typeflux_spec_digest__", None)
    label = getattr(workflow_class, "__typeflux_workflow_version_label__", None)
    if not isinstance(workflow_type, str) or not isinstance(spec_digest, str):
        return
    list_workflows = getattr(client, "list_workflows", None)
    if list_workflows is None:
        return
    recorded: str | None = None
    try:
        async for execution in list_workflows(f"WorkflowType = '{workflow_type}'", limit=1):
            recorded = await _execution_memo_spec_digest(execution)
            break
    except Exception as exc:
        logger.warning(
            "skipping frozen workflow.version check for %s: visibility query failed (%s)",
            workflow_type,
            type(exc).__name__,
        )
        return
    if recorded is not None and recorded != spec_digest:
        raise ValueError(
            f"workflow.version {label!r} is frozen to spec digest {recorded!r}, but the "
            f"loaded YAML graph has digest {spec_digest!r}; assign a new workflow.version "
            "for graph changes instead of reusing a version label"
        )


async def _describe_memo(description: Any) -> Mapping[str, Any]:
    """Decode a workflow description's memo.

    ``WorkflowExecutionDescription.memo`` is an async method on real Temporal
    handles; tolerate a plain mapping or a sync callable too so the helper
    works against test doubles. Returns an empty mapping when no memo is set.
    """
    memo = getattr(description, "memo", None)
    if memo is None:
        return {}
    value = memo() if callable(memo) else memo
    if isawaitable(value):
        value = await value
    return value if isinstance(value, Mapping) else {}


async def _execution_memo_spec_digest(execution: Any) -> str | None:
    value = (await _describe_memo(execution)).get("typeflux_spec_digest")
    return value if isinstance(value, str) else None


def _enforce_runtime_policy_guard(spec: TypefluxYamlSpec, policy_guard: Any | None) -> None:
    if policy_guard is None:
        return
    provider = spec.runtime.provider
    # provider.model is materialized at load (see ProviderSpec), so admission and
    # runtime enforce against the same concrete value.
    policy_guard.enforce_provider_model(
        provider_name=provider.type,
        provider_model=provider.model,
    )


class RegistryCompositionError(Exception):
    """The specs in a composition closure declare registries that cannot be merged (#748)."""


def _build_registry(
    spec: TypefluxYamlSpec,
    child_specs: Sequence[tuple[str, TypefluxYamlSpec]] = (),
) -> PromptRegistry:
    registry_spec = spec.runtime.registry
    # Registry composition (#748): a composed worker serves ONE registry, so every
    # (transitively) referenced sub-workflow must declare a registry compatible with the
    # parent's — same type, and for an external backend the same config. This runs for every
    # registry type, so a child declaring a registry the parent's worker could never serve
    # fails loudly at load rather than being silently dropped.
    for child_id, child_spec in child_specs:
        _assert_compatible_registry_config(spec, child_id, child_spec)
    if registry_spec.type == "inline":
        # Merge the parent's and every child's inline prompt map into the ONE served
        # registry (byte-identical dedupe, loud conflicts) — children no longer have to be
        # duplicated into the parent spec.
        prompts = cast(
            Mapping[str | PromptRef, InlinePromptValue],
            _merge_inline_prompt_values(spec, child_specs),
        )
        return InlinePromptRegistry(prompts)
    if registry_spec.type == "custom":
        validate_extension_class_import(
            project=spec.project,
            class_path=registry_spec.registry_class,
            policy=spec.runtime.imports,
            allow_flag="allow_registry_class",
            field="runtime.registry.class",
        )
        registry_cls = import_object(cast(str, registry_spec.registry_class))
        return cast(
            PromptRegistry,
            _instantiate_custom_extension(
                registry_cls, config=registry_spec.config, kind="registry"
            ),
        )
    if registry_spec.type == "langsmith":
        langsmith_registry: PromptRegistry = LangSmithPromptRegistry(
            api_url=_langsmith_host(registry_spec.host),
        )
        if registry_spec.label:
            return _LabelOverrideRegistry(registry=langsmith_registry, label=registry_spec.label)
        return langsmith_registry
    registry = LangfusePromptRegistry(
        host=_langfuse_host(registry_spec.host),
        allow_prompt_model_override=spec.runtime.provider.allow_prompt_model_override,
    )
    if registry_spec.label:
        return _LabelOverrideRegistry(registry=registry, label=registry_spec.label)
    return registry


def _inline_prompt_value(name: str, value: str | InlinePromptSpec) -> InlinePromptValue:
    """Convert one spec registry prompt entry (a bare string or an inline-prompt spec) to the
    core ``InlinePromptValue``. Shared by the single-spec registry build and the sub-workflow
    registry composition (#748)."""
    if isinstance(value, InlinePromptSpec):
        return ResolvedPrompt(
            ref=PromptRef(name),
            messages=tuple(message.to_chat_message() for message in value.messages),
            resolved_version="inline",
            model=value.model,
            temperature=value.temperature,
            provider_params=(
                value.provider_params.to_provider_params(
                    legacy_model=value.model,
                    legacy_temperature=value.temperature,
                )
                if value.provider_params is not None
                else ProviderParams()
            ),
        )
    return value


def _inline_prompt_values(spec: TypefluxYamlSpec) -> dict[str | PromptRef, InlinePromptValue]:
    return {
        name: _inline_prompt_value(name, value)
        for name, value in spec.runtime.registry.prompts.items()
    }


def _prompt_canonical(value: str | InlinePromptSpec) -> str:
    """The byte-identity form of a registry prompt entry (canonical JSON of its payload)."""
    payload = value if isinstance(value, str) else value.model_dump(mode="json")
    return canonical_json(payload)


def _assert_compatible_registry_config(
    spec: TypefluxYamlSpec, child_id: str, child_spec: TypefluxYamlSpec
) -> None:
    """A composed worker serves ONE registry, so a child's registry must be of the SAME type
    as the parent's, and for a non-inline (external) backend the SAME config
    (label/host/class, and for a custom class its declared config block, #792).
    A divergence is a load-time error — never a silent "use the parent's" (#748)."""
    parent_registry = spec.runtime.registry
    child_registry = child_spec.runtime.registry
    if parent_registry.type != child_registry.type:
        raise RegistryCompositionError(
            f"sub-workflow registry composition: workflow {child_id!r} declares a "
            f"{child_registry.type!r} registry but the composed worker serves the parent "
            f"{spec.name!r}'s {parent_registry.type!r} registry — a composed worker serves ONE "
            "registry, so every composed workflow must declare the same registry type (#748)"
        )
    if parent_registry.type == "inline":
        return  # Inline configs never conflict at the config level — only per-prompt (below).
    parent_config = canonical_json(
        {
            "label": parent_registry.label,
            "host": parent_registry.host,
            "class": parent_registry.registry_class,
            # #792: a custom registry's declared config is part of its identity — the same
            # class with different config is a different backend, not a compatible one.
            "config": _custom_config_identity(parent_registry.config),
        }
    )
    child_config = canonical_json(
        {
            "label": child_registry.label,
            "host": child_registry.host,
            "class": child_registry.registry_class,
            "config": _custom_config_identity(child_registry.config),
        }
    )
    if parent_config != child_config:
        raise RegistryCompositionError(
            f"sub-workflow registry composition: workflow {child_id!r} declares a "
            f"{child_registry.type!r} registry with a DIFFERENT backend config "
            "(label/host/class/config) "
            f"than the parent {spec.name!r} — a composed worker serves ONE registry, so conflicting "
            "external registry configs must be reconciled, not silently resolved to the parent's (#748)"
        )


def _merge_inline_prompt_values(
    spec: TypefluxYamlSpec, child_specs: Sequence[tuple[str, TypefluxYamlSpec]]
) -> dict[str | PromptRef, InlinePromptValue]:
    """Merge the parent's and every child's inline prompt map into one. First declaration of a
    name wins the built value; a later declaration of the SAME name is tolerated only when it is
    byte-identical (canonical JSON) — a conflict is a loud error naming both workflows and the
    first differing field (#748)."""
    merged: dict[str | PromptRef, InlinePromptValue] = {}
    provenance: dict[str, tuple[str, str, str | InlinePromptSpec]] = {}
    sources: list[tuple[str, TypefluxYamlSpec]] = [(spec.name, spec), *child_specs]
    for source_id, source_spec in sources:
        for name, value in source_spec.runtime.registry.prompts.items():
            canonical = _prompt_canonical(value)
            prior = provenance.get(name)
            if prior is None:
                provenance[name] = (source_id, canonical, value)
                merged[name] = _inline_prompt_value(name, value)
                continue
            if prior[1] == canonical:
                continue  # Byte-identical duplicate — already merged once.
            raise RegistryCompositionError(
                f"sub-workflow registry composition: prompt {name!r} is declared with DIFFERENT "
                f"definitions by workflows {prior[0]!r} and {source_id!r} (first differing field: "
                f"{_first_differing_field(prior[2], value, prior[0], source_id)}) — a composed "
                "worker serves ONE registry, so a shared prompt name must be byte-identical "
                "across composed workflows or use a distinct name (#748)"
            )
    return merged


def _prompt_payload(value: str | InlinePromptSpec) -> Any:
    return value if isinstance(value, str) else value.model_dump(mode="json")


# ABSENT sentinel for the union-of-keys/indexes diff walk (#748 review): an optional field
# set in only one spec must diff as "absent", not crash or conflate. Pydantic dumps an UNSET
# optional as ``None``, so ``None`` is the same "absent" state here (TS parity: zod omits
# the key entirely — both editions report `path (set in A, absent in B)` for the same YAML).
_ABSENT: Any = object()


def _canonical_or_absent(value: Any) -> str:
    if value is _ABSENT or value is None:
        return "\x00absent"  # NUL-prefixed: can never collide with real canonical JSON.
    return canonical_json(value)


def _first_differing_field(
    a: str | InlinePromptSpec, b: str | InlinePromptSpec, a_id: str, b_id: str
) -> str:
    """The path (dotted keys / ``[i]`` indices) of the first place two prompt values diverge,
    for the conflict message. Deterministic (sorted keys). A field present/set in only ONE
    source is a diff in its own right, reported as ``path (set in 'A', absent in 'B')``."""
    return _first_differing_path(_prompt_payload(a), _prompt_payload(b), "", a_id, b_id)


def _first_differing_path(a: Any, b: Any, path: str, a_id: str, b_id: str) -> str:
    here = path or "(value)"
    if _canonical_or_absent(a) == _canonical_or_absent(b):
        return here  # No divergence at or below this node — report the node itself.
    if a is _ABSENT or a is None or b is _ABSENT or b is None:
        # Exactly one side absent (both-absent compared equal above): this IS the diff.
        set_in, absent_in = (b_id, a_id) if (a is _ABSENT or a is None) else (a_id, b_id)
        return f"{here} (set in {set_in!r}, absent in {absent_in!r})"
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            child_path = key if path == "" else f"{path}.{key}"
            av = a.get(key, _ABSENT)
            bv = b.get(key, _ABSENT)
            if _canonical_or_absent(av) != _canonical_or_absent(bv):
                return _first_differing_path(av, bv, child_path, a_id, b_id)
        return here
    if isinstance(a, list) and isinstance(b, list):
        for index in range(max(len(a), len(b))):
            av = a[index] if index < len(a) else _ABSENT
            bv = b[index] if index < len(b) else _ABSENT
            if _canonical_or_absent(av) != _canonical_or_absent(bv):
                return _first_differing_path(av, bv, f"{path}[{index}]", a_id, b_id)
        return here
    return here


def _custom_config_identity(
    config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """A JSON-comparable identity for a custom-extension ``config`` block: literals as-is,
    ``value_from`` references as their dumped source shape — what composition compares, so
    two workflows naming the same class with different config cannot silently compose."""
    if config is None:
        return None
    return {
        key: value if isinstance(value, str) else value.model_dump(mode="json")
        for key, value in config.items()
    }


def _instantiate_custom_extension(cls: Any, *, config: Any, kind: str) -> Any:
    """Construct a custom extension class (#792): a declared ``config`` block resolves
    (literals + value_from, fail-closed on required sources) and is passed as
    ``cls(config=resolved)`` — the class must accept it, a declared-but-ignored config would
    read like working configuration that isn't. Undeclared keeps the zero-arg contract."""
    resolved = resolve_custom_extension_config(config, kind=kind)
    if resolved is None:
        return cls()
    try:
        return cls(config=resolved)
    except TypeError as exc:
        # Only a call-boundary mismatch earns the pointer message; a TypeError raised
        # INSIDE a config-accepting __init__ is the class's own bug and must surface
        # unchanged (a wrong "define __init__(...)" hint sends the author to fix a
        # signature that is already correct).
        try:
            inspect.signature(cls).bind(config=resolved)
        except TypeError:
            raise TypeError(
                f"runtime.{kind}.config is declared but {cls.__module__}.{cls.__qualname__} "
                f"does not accept it: define __init__(self, *, config: dict[str, str]) "
                f"(or drop the config block)"
            ) from exc
        except ValueError:
            pass  # No introspectable signature (C types): fall through to the original.
        raise


class ProviderCompositionError(Exception):
    """A composed sub-workflow's custom provider config diverges from the parent's (#792)."""


def _assert_consistent_custom_provider_config(
    spec: TypefluxYamlSpec, child_id: str, child_spec: TypefluxYamlSpec
) -> None:
    """The narrow #792 slice of provider composition: a composed worker builds ONE provider
    — the parent's — and a child naming the SAME custom class with a DIFFERENT config block
    would silently run on the parent's credentials/endpoints. That divergence is a loud
    error, mirroring the registry/observability config-identity rules. The broader
    provider-agreement question (a child declaring a different type/model/class at all is
    silently ignored today) predates #792 and is tracked separately — this check only
    refuses to let the new config surface widen that gap."""
    parent_provider = spec.runtime.provider
    child_provider = child_spec.runtime.provider
    if parent_provider.type != "custom" or child_provider.type != "custom":
        return
    if parent_provider.provider_class != child_provider.provider_class:
        return  # Pre-existing silent-ignore surface; config identity is meaningless across classes.
    if _custom_config_identity(child_provider.config) != _custom_config_identity(
        parent_provider.config
    ):
        raise ProviderCompositionError(
            f"sub-workflow provider composition: workflow {child_id!r} declares the custom "
            f"provider class {child_provider.provider_class!r} with a DIFFERENT config block "
            f"than the parent {spec.name!r} — the composed worker builds ONE provider from "
            "the parent's config, so a divergent child config would be silently dead (#792)"
        )


def _build_provider(spec: TypefluxYamlSpec, *, enable_langfuse: bool) -> ModelProvider:
    provider_spec = spec.runtime.provider
    runtime_provider_params = provider_spec.provider_params()
    if provider_spec.type == "custom":
        validate_extension_class_import(
            project=spec.project,
            class_path=provider_spec.provider_class,
            policy=spec.runtime.imports,
            allow_flag="allow_provider_class",
            field="runtime.provider.class",
        )
        provider_cls = import_object(cast(str, provider_spec.provider_class))
        provider = _instantiate_custom_extension(
            provider_cls, config=provider_spec.config, kind="provider"
        )
        # The instantiated class's provider_name flows to runtime metadata,
        # observability, and per-provider rate limiting via provider_identifier().
        # The static policy/admission layer identifies a custom provider as
        # "custom" (its YAML type) — so it can never impersonate a built-in
        # provider's identity, which the old `type: fake` + class back door
        # allowed (a class named FakeProvider collapsed to "fake").
        setattr(provider, "default_provider_params", runtime_provider_params)
        validate_provider_params_supported(provider, runtime_provider_params)
        return provider
    if provider_spec.type == "fake":
        provider = FakeProvider([])
        provider.default_provider_params = runtime_provider_params
        validate_provider_params_supported(provider, runtime_provider_params)
        return provider
    api_key = resolve_optional_secret_text(
        provider_spec.api_key,
        runtime_path="runtime.provider.api_key",
    )
    if provider_spec.type == "openai":
        return OpenAIProvider(
            default_model=provider_spec.model or DEFAULT_OPENAI_MODEL,
            api_key=api_key,
            base_url=provider_spec.base_url,
            enable_langfuse=enable_langfuse,
            default_provider_params=runtime_provider_params,
        )
    if provider_spec.type == "anthropic":
        return AnthropicProvider(
            default_model=provider_spec.model or DEFAULT_ANTHROPIC_MODEL,
            api_key=api_key,
            base_url=provider_spec.base_url,
            default_provider_params=runtime_provider_params,
        )
    if provider_spec.type == "gemini":
        if provider_spec.vertex is not None:
            # Vertex AI path: authenticates with Application Default Credentials,
            # not an api_key (#332). project/location are optional — the SDK reads
            # GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION when omitted.
            return GeminiProvider(
                default_model=provider_spec.model or DEFAULT_GEMINI_MODEL,
                use_vertex=True,
                project=provider_spec.vertex.project,
                location=provider_spec.vertex.location,
                default_provider_params=runtime_provider_params,
            )
        # Developer-API path (api_key).
        return GeminiProvider(
            default_model=provider_spec.model or DEFAULT_GEMINI_MODEL,
            api_key=api_key,
            default_provider_params=runtime_provider_params,
        )
    raise ValueError(f"unsupported provider type: {provider_spec.type}")


def _validate_activity_provider_params(
    provider: ModelProvider,
    activities: Sequence[YamlWorkflowActivity],
) -> None:
    default_params = provider_default_params(provider)
    for activity in activities:
        if not isinstance(activity, AIActivity):
            continue
        validate_provider_params_supported(
            provider,
            default_params.merge(activity.provider_params),
            activity_name=activity.name,
            prompt_name=activity.prompt_ref.name,
        )


def _build_provider_rate_limit_policy(spec: TypefluxYamlSpec) -> ProviderRateLimitPolicy | None:
    provider_limits = spec.runtime.provider_limits
    if provider_limits is None:
        return None
    return ProviderRateLimitPolicy(
        default=_provider_call_limits(provider_limits.default),
        providers={
            provider_name: ProviderRateLimitProviderPolicy(
                limits=_provider_call_limits(provider_spec),
                models={
                    model_name: model_limits
                    for model_name, model_limits_spec in provider_spec.models.items()
                    if (model_limits := _provider_call_limits(model_limits_spec)) is not None
                },
            )
            for provider_name, provider_spec in provider_limits.providers.items()
        },
    )


def _build_provider_retry_policy(spec: TypefluxYamlSpec) -> ProviderRetryPolicy | None:
    provider_retry = spec.runtime.provider_retry
    if provider_retry is None:
        return None
    return ProviderRetryPolicy(
        max_attempts=provider_retry.max_attempts,
        initial_backoff_seconds=provider_retry.initial_backoff_seconds,
        max_backoff_seconds=provider_retry.max_backoff_seconds,
        backoff_multiplier=provider_retry.backoff_multiplier,
        jitter_ratio=provider_retry.jitter_ratio,
        retry_rate_limits=provider_retry.retry_rate_limits,
        retry_transient_errors=provider_retry.retry_transient_errors,
    )


def _build_artifact_policy(spec: TypefluxYamlSpec) -> ArtifactPolicy:
    artifact_spec = spec.runtime.artifacts
    source_path = getattr(spec, "_source_path", None)
    base_dir = source_path.parent if source_path is not None else Path.cwd()
    return ArtifactPolicy(
        local_roots=tuple(
            _resolve_artifact_root(root, base_dir=base_dir) for root in artifact_spec.local_roots
        ),
        allowed_source_kinds=tuple(artifact_spec.allowed_sources),
        allowed_media_types=tuple(artifact_spec.allowed_media_types),
        max_bytes=artifact_spec.max_bytes,
    )


def _resolve_artifact_root(root: str, *, base_dir: Path) -> Path:
    path = Path(root).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _yaml_map_steps(workflow_class: type, input_value: Any) -> list[dict[str, Any]]:
    # Leaf calls, depth-first (#55): maps nested in parallel branches surface too.
    calls = flatten_call_specs(getattr(workflow_class, "__typeflux_activity_calls__", ()))
    payload = []
    for call in calls:
        if not isinstance(call, MapCallSpec):
            continue
        item_count = _map_step_size(input_value, call.over)
        payload.append(
            {
                "map_step_id": call.step_id,
                "activity_name": call.activity_name,
                "over": call.over,
                "map_size": item_count,
                "map_concurrency": call.concurrency,
                "collect_output": call.collect.output_type.__name__,
                "collect_field": call.collect.field,
            }
        )
    return payload


def _map_step_size(input_value: Any, path: str) -> int | None:
    parts = path.split(".")
    if not parts or parts[0] != "input":
        return None
    current = input_value
    for field_name in parts[1:]:
        if isinstance(current, dict):
            current = current.get(field_name)
        elif isinstance(current, BaseModel):
            current = getattr(current, field_name, None)
        else:
            current = getattr(current, field_name, None)
        if current is None:
            return None
    return len(current) if isinstance(current, Sequence) and not isinstance(current, str) else None


def _yaml_lifecycle_metadata(
    spec: TypefluxYamlSpec,
    workflow_class: type,
    input_value: Any,
) -> dict[str, Any]:
    lifecycle = spec.workflow.lifecycle
    if lifecycle is None or not lifecycle.enabled:
        return {}
    total_units = 0
    if lifecycle.progress:
        # Leaf calls, depth-first (#55): a parallel block contributes its branches'
        # units, never a unit of its own (parity with _lifecycle_total_units).
        calls = flatten_call_specs(getattr(workflow_class, "__typeflux_activity_calls__", ()))
        for call in calls:
            if hasattr(call, "over"):
                total_units += _map_step_size(input_value, call.over) or 0
            else:
                total_units += 1
    payload: dict[str, Any] = {
        "state": "pending",
        "current_step": None,
        "completed_units": 0,
        "total_units": total_units,
        "cancellation_requested": False,
        "waiting_checkpoint": None,
        "terminal_status": None,
        "status_event_limit": lifecycle.history.status_event_limit,
    }
    if lifecycle.review is not None:
        payload["review_after_step"] = lifecycle.review.after_step
    # Multi-gate workflows (#55 slice 4): the gate ids + checkpoints join the start-time
    # trace/manifest metadata additively (single-`review` payloads are unchanged).
    if lifecycle.gates is not None:
        payload["gates"] = [
            {"id": gate.id, "after_step": gate.after_step} for gate in lifecycle.gates
        ]
    return payload


def _provider_call_limits(spec: ProviderLimitSpec | None) -> ProviderCallLimits | None:
    if spec is None:
        return None
    if spec.max_concurrent is None and spec.min_interval_seconds is None:
        return None
    return ProviderCallLimits(
        max_concurrent=spec.max_concurrent,
        min_interval_seconds=spec.min_interval_seconds,
    )


def _codec_data_converter(
    base: Any,
    codec_spec: PayloadCodecSpec | None,
    *,
    subject_keystore: Any | None = None,
) -> Any:
    """Return the base data converter, or a copy carrying the AES-256-GCM payload codec.

    The single ``payload_codec`` slot on a ``DataConverter`` wraps ONE composite codec —
    cross-edition interop rests only on the pinned wire format, not on chain semantics.
    Absent codec spec ⇒ base unchanged (plaintext; digests invariant).

    When the spec declares ``payload_codec.subject_scope`` (#715 slice 4), the shared
    codec is wrapped in the :class:`SubjectScopedPayloadCodec` so subject-scoped
    executions seal under per-subject keystore records (crypto-shred).
    ``subject_keystore`` is REQUIRED then — FAIL-CLOSED (#715 Bugbot): silently minting
    a process-local keystore here would let a CP/reader process encode subject payloads
    under keys no worker holds (and mint keys for subjects whose real records live
    elsewhere). Only a caller that OWNS both encode and decode for the deployment may
    supply the in-memory reference backend (``build_runtime`` does, with its
    sole-owner justification).
    """
    import dataclasses

    codec: Any = build_payload_codec(codec_spec)
    if codec is None:
        return base
    if codec_spec is not None and codec_spec.subject_scope is not None:
        from typeflux.yaml.subject_keystore import SubjectScopedPayloadCodec

        if subject_keystore is None:
            raise PayloadCodecError(
                "runtime.temporal.payload_codec.subject_scope requires an injected "
                "SubjectKeystore backend on this path (#715 slice 4): this process is not "
                "the sole owner of subject key records, and a silently-minted process-local "
                "keystore would seal subject payloads under keys no worker holds. Inject a "
                "SHARED keystore backend (build_runtime(subject_keystore=...) for the "
                "runtime-owned worker path; see docs/privacy.md 'Keystore backends')."
            )
        codec = SubjectScopedPayloadCodec(codec, subject_keystore)
    return dataclasses.replace(base, payload_codec=codec)


def _bind_subject_scope_client(data_converter: Any, client: Any) -> None:
    """Late-bind the connected client into the subject-scope bindings (#715 slice 4).

    The codec's visibility fallback resolves ``workflow_id -> TypefluxSubjectIds`` via
    this client, so worker/CP processes that did not start an execution can still honor
    its subject scoping. No-op for a plain (or absent) codec.
    """
    from typeflux.yaml.subject_keystore import SubjectScopedPayloadCodec

    codec = getattr(data_converter, "payload_codec", None)
    if isinstance(codec, SubjectScopedPayloadCodec):
        codec.bindings.bind_client(client)


def _register_subject_scope_binding(
    client: Any, workflow_id: str, subject_ids: Sequence[str]
) -> None:
    """Register a start's subject set with the subject-scoped codec (#715 slice 4).

    Called by the runtime start path with the SAME resolved ids it stamps into
    ``TypefluxSubjectIds`` — including the EMPTY set (pinning "no subjects" avoids a
    visibility describe for the start's own input encode, which happens before the
    execution exists). No-op when the client's codec is not subject-scoped.
    """
    from typeflux.yaml.subject_keystore import SubjectScopedPayloadCodec

    codec = getattr(getattr(client, "data_converter", None), "payload_codec", None)
    if isinstance(codec, SubjectScopedPayloadCodec):
        codec.bindings.register(workflow_id, subject_ids)


async def _connect_client(
    spec: TypefluxYamlSpec, *, plugin: Any | None, subject_keystore: Any | None = None
) -> Any:
    try:
        from temporalio.client import Client
        from temporalio.contrib.pydantic import pydantic_data_converter
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard.
        raise RuntimeError("temporalio is required to build a Typeflux YAML runtime") from exc

    api_key = resolve_optional_secret_text(
        spec.runtime.temporal.api_key,
        runtime_path="runtime.temporal.api_key",
    )
    if api_key and spec.runtime.temporal.tls is False:
        raise ValueError("runtime.temporal.api_key requires runtime.temporal.tls to be enabled")
    # Build the AES-256-GCM codec ONCE from the resolved spec (fail-closed on a missing/
    # wrong-length key). Absent payload_codec ⇒ no codec ⇒ plaintext, digests unchanged.
    data_converter = _codec_data_converter(
        pydantic_data_converter,
        spec.runtime.temporal.payload_codec,
        subject_keystore=subject_keystore,
    )
    kwargs: dict[str, Any] = {
        "namespace": spec.runtime.temporal.namespace,
        "tls": _build_temporal_tls_config(spec.runtime.temporal.tls),
        "api_key": api_key,
        "data_converter": data_converter,
    }
    if plugin is not None:
        kwargs["plugins"] = [plugin]
    client = await Client.connect(spec.runtime.temporal.address, **kwargs)
    # #715 slice 4: give the subject-scope bindings their visibility fallback — this is
    # how a worker/CP process resolves the TypefluxSubjectIds of executions it did not
    # start. No-op without subject_scope.
    _bind_subject_scope_client(data_converter, client)
    return client


def _uses_langfuse(spec: TypefluxYamlSpec) -> bool:
    return spec.runtime.observability.type == "langfuse"


def _uses_langsmith(spec: TypefluxYamlSpec) -> bool:
    return spec.runtime.observability.type == "langsmith"


def _observability_required_by_policy(policy_guard: Any | None) -> bool:
    """Whether the effective composed policy sets ``observability.required: True`` (#756).

    Reads the SAME ``policy.payload.observability`` mapping admission's
    ``_validate_observability`` evaluates, so the runtime fail-closed gate and the
    admission shape-check agree on what "required" means. Absent guard / payload /
    non-True value ⇒ not required (back-compat: no gate for an ungoverned or
    observability-silent policy).
    """
    if policy_guard is None:
        return False
    policy = getattr(policy_guard, "policy", None)
    payload = getattr(policy, "payload", None)
    if not isinstance(payload, Mapping):
        return False
    observability = payload.get("observability")
    if not isinstance(observability, Mapping):
        return False
    return observability.get("required") is True


class ObservabilityCompositionError(Exception):
    """The specs in a composition closure declare observability that cannot compose (#756)."""


def _effective_observability_backend(spec: TypefluxYamlSpec) -> str:
    """The spec's effective declared backend — admission's ``type or "none"`` collapse."""
    return spec.runtime.observability.type or "none"


def _assert_consistent_observability_config(
    spec: TypefluxYamlSpec, child_id: str, child_spec: TypefluxYamlSpec
) -> None:
    """A composed worker builds ONE observability backend — the PARENT's — and threads its
    observer into every registered activity (#756, the #748 registry-singleton rule). A child
    declaring a DIFFERENT backend would be silently ignored: admission passes it (its backend
    is not "none"), but the runtime never resolves or credential-checks it. Require the closure
    to agree: a child declares the parent's effective backend, or none/absent — which INHERITS
    the parent's observer (admission's ``_validate_observability`` collapses absent and explicit
    ``none`` identically, and under a required-observability policy its closure validation
    already rejects such a child, so a governed closure never reaches the inherit path
    silently). Divergence is a load-time error — never a silent "use the parent's"."""
    child_backend = _effective_observability_backend(child_spec)
    if child_backend == "none":
        return  # Absent/none inherits the parent's observer.
    parent_backend = _effective_observability_backend(spec)
    if child_backend == parent_backend:
        if child_backend != "custom":
            return
        # Custom backends compose only when they are the SAME class AND the same declared
        # config (mirroring the registry config-identity rule): two different classes — or
        # one class with divergent config (#792) — cannot both be the ONE built backend.
        if (
            child_spec.runtime.observability.backend_class
            != spec.runtime.observability.backend_class
        ):
            raise ObservabilityCompositionError(
                f"sub-workflow observability composition: workflow {child_id!r} declares a custom "
                f"observability class {child_spec.runtime.observability.backend_class!r} but the composed "
                f"worker builds the parent {spec.name!r}'s {spec.runtime.observability.backend_class!r} — "
                "a composed worker builds ONE observer, so composed custom backends must share the "
                "same class (#756)"
            )
        if _custom_config_identity(
            child_spec.runtime.observability.config
        ) != _custom_config_identity(spec.runtime.observability.config):
            raise ObservabilityCompositionError(
                f"sub-workflow observability composition: workflow {child_id!r} declares the "
                f"custom class {child_spec.runtime.observability.backend_class!r} with a DIFFERENT "
                f"config block than the parent {spec.name!r} — the composed worker builds ONE "
                "observer from the parent's config, so a divergent child config would be "
                "silently dead (#792)"
            )
        return
    raise ObservabilityCompositionError(
        f"sub-workflow observability composition: workflow {child_id!r} declares "
        f"{child_backend!r} observability but the composed worker builds the parent "
        f"{spec.name!r}'s {parent_backend!r} observer — a composed worker builds ONE observer, "
        "so every composed workflow must declare the parent's observability backend (or none, "
        "which inherits it) (#756)"
    )


class ObservabilityCredentials(NamedTuple):
    """The resolved spec-declared observability credentials (#793), resolved ONCE per
    worker build and threaded to BOTH the required-observability gate and backend
    construction — check-vs-use must never diverge. Fields are None when unset (env
    fallback applies downstream). host/endpoint/project are plain spec strings (not
    secret slots); the three key fields resolve like every sibling secret."""

    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None
    langsmith_api_key: str | None
    langsmith_endpoint: str | None
    langsmith_project: str | None


def resolved_observability_credentials(spec: TypefluxYamlSpec) -> ObservabilityCredentials:
    observability = spec.runtime.observability
    langfuse = observability.langfuse
    langsmith = observability.langsmith
    return ObservabilityCredentials(
        langfuse_public_key=resolve_optional_secret_text(
            langfuse.public_key if langfuse else None,
            runtime_path="runtime.observability.langfuse.public_key",
        ),
        langfuse_secret_key=resolve_optional_secret_text(
            langfuse.secret_key if langfuse else None,
            runtime_path="runtime.observability.langfuse.secret_key",
        ),
        langfuse_host=langfuse.host if langfuse else None,
        langsmith_api_key=resolve_optional_secret_text(
            langsmith.api_key if langsmith else None,
            runtime_path="runtime.observability.langsmith.api_key",
        ),
        langsmith_endpoint=langsmith.endpoint if langsmith else None,
        langsmith_project=langsmith.project if langsmith else None,
    )


def _enforce_required_observability_resolves(
    spec: TypefluxYamlSpec,
    policy_guard: Any | None,
    credentials: ObservabilityCredentials,
) -> None:
    """Fail CLOSED when a required-observability policy's declared backend cannot resolve (#756).

    Parity with the TS ``langfuse``/``langsmith`` observer helpers: when the governing
    composed policy sets ``observability.required`` and the spec declares langfuse/langsmith
    but the credential env vars are absent, refuse to build the worker with a pointed error
    naming the exact vars — instead of degrading to an untraced run behind the SDK's silent
    disabled mode (a regulated-tier adopter compliance gap). Admission
    (``_validate_observability``) already refused a ``type: none`` spec under this policy, so a
    ``required`` backend here is langfuse/langsmith; absent credentials are the only remaining
    silent path. No policy / required absent ⇒ no gate.
    """
    if not _observability_required_by_policy(policy_guard):
        return
    if _uses_langfuse(spec) and not (
        (credentials.langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY"))
        and (credentials.langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY"))
    ):
        raise RuntimeError(
            "runtime.observability.type is langfuse and the governing project policy sets "
            "observability.required, but no credentials resolve — "
            "a required-observability workflow must not run untraced. Declare "
            "runtime.observability.langfuse.public_key/secret_key (value_from) or export "
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (plus LANGFUSE_HOST for self-hosted) "
            "before starting the worker."
        )
    if _uses_langsmith(spec) and not (
        credentials.langsmith_api_key or os.getenv("LANGSMITH_API_KEY")
    ):
        raise RuntimeError(
            "runtime.observability.type is langsmith and the governing project policy sets "
            "observability.required, but no credentials resolve — a required-observability "
            "workflow must not run untraced. Declare runtime.observability.langsmith.api_key "
            "(value_from) or export LANGSMITH_API_KEY (plus LANGSMITH_ENDPOINT for "
            "self-hosted and LANGSMITH_PROJECT to pick the project) before starting the worker."
        )


def _build_observability(
    spec: TypefluxYamlSpec,
    *,
    policy_guard: Any | None = None,
    child_specs: Sequence[tuple[str, TypefluxYamlSpec]] = (),
) -> ObservabilityBackend:
    # Observability composition (#756, mirroring _build_registry's #748 rule): the ONE backend
    # this (possibly composed) worker builds is the parent's, so every referenced child must
    # declare a compatible backend (absent/none inherits) before anything is constructed.
    for child_id, child_spec in child_specs:
        _assert_consistent_observability_config(spec, child_id, child_spec)
    # With consistency guaranteed, the parent's backend IS the closure's single effective
    # backend — exactly what the required-credential gate evaluates.
    credentials = resolved_observability_credentials(spec)
    _enforce_required_observability_resolves(spec, policy_guard, credentials)
    observability_spec = spec.runtime.observability
    if observability_spec.type == "custom":
        validate_extension_class_import(
            project=spec.project,
            class_path=observability_spec.backend_class,
            policy=spec.runtime.imports,
            allow_flag="allow_observability_class",
            field="runtime.observability.class",
        )
        backend_cls = import_object(cast(str, observability_spec.backend_class))
        # A custom backend owns its redaction; its declared config (or its own env reads)
        # flows to the worker through the generic observability= seam.
        return cast(
            ObservabilityBackend,
            _instantiate_custom_extension(
                backend_cls, config=observability_spec.config, kind="observability"
            ),
        )
    if _uses_langsmith(spec):
        from typeflux.observability.langsmith import LangSmithObservabilityBackend

        return LangSmithObservabilityBackend.from_env(
            redactor=_build_redactor(spec, policy_guard=policy_guard),
            api_key=credentials.langsmith_api_key,
            endpoint=credentials.langsmith_endpoint,
            project=credentials.langsmith_project,
        )
    if not _uses_langfuse(spec):
        return NoOpObservabilityBackend()
    return LangfuseObservabilityBackend.from_env(
        redactor=_build_redactor(spec, policy_guard=policy_guard),
        # A spec-declared host wins; the registry host stays the shared fallback.
        host=credentials.langfuse_host or _langfuse_host(spec.runtime.registry.host),
        public_key=credentials.langfuse_public_key,
        secret_key=credentials.langfuse_secret_key,
    )


def _build_redactor(
    spec: TypefluxYamlSpec,
    *,
    policy_guard: Any | None = None,
) -> Redactor:
    redaction = spec.runtime.observability.redaction
    if not redaction.enabled:
        return NoOpRedactor()
    contributor_exclusions = (
        metadata_redaction_exclusions(
            (
                *_yaml_metadata_contributors(spec),
                *_policy_metadata_contributors(policy_guard),
                ActivityContextContributor(),
                LifecycleOperationContributor(),
            )
        )
        if redaction.preserve_typeflux_metadata
        else ()
    )
    custom_rules = tuple(
        RegexRedactionRule(
            name=rule.name,
            pattern=re.compile(rule.pattern),
            replacement=rule.replacement,
        )
        for rule in redaction.custom_rules
    )
    return RegexPIIRedactor.default(
        emails=redaction.emails,
        phones=redaction.phones,
        ssn=redaction.ssn,
        credit_cards=redaction.credit_cards,
        preserve_typeflux_metadata=redaction.preserve_typeflux_metadata,
        exclude_paths=(*redaction.exclude_paths, *contributor_exclusions),
        custom_rules=custom_rules,
    )


def _yaml_metadata_contributors(
    spec: TypefluxYamlSpec,
    workflow_class: type | None = None,
    input_value: Any | None = None,
    policy_guard: Any | None = None,
) -> tuple[MetadataContributor, ...]:
    map_steps = (
        _yaml_map_steps(workflow_class, input_value)
        if workflow_class is not None and input_value is not None
        else ()
    )
    lifecycle_metadata = (
        _yaml_lifecycle_metadata(spec, workflow_class, input_value)
        if workflow_class is not None and input_value is not None
        else {}
    )
    return (
        YamlWorkflowContributor(
            project=spec.project,
            name=spec.name,
            map_steps=map_steps,
            spec_digest=getattr(workflow_class, "__typeflux_spec_digest__", None),
            spec_digest_algorithm=(SPEC_DIGEST_ALGORITHM if workflow_class is not None else None),
            generator_version=(GENERATOR_VERSION if workflow_class is not None else None),
            workflow_type=getattr(workflow_class, "__typeflux_workflow_type__", None),
            workflow_version_label=getattr(
                workflow_class, "__typeflux_workflow_version_label__", None
            ),
        ),
        YamlOverrideContributor(getattr(spec, "_override_provenance", None)),
        ComponentProvenanceContributor(getattr(spec, "_component_provenance", ())),
        SecretReferenceContributor(secret_reference_records(spec)),
        _temporal_connection_contributor(spec),
        RuntimePlacementContributor.from_env(),
        *_policy_metadata_contributors(policy_guard),
        *_risk_tier_metadata_contributors(spec, policy_guard),
        *_compensation_metadata_contributors(spec),
        *_admission_metadata_contributors(spec),
        YamlLifecycleContributor(lifecycle_metadata),
    )


def _policy_metadata_contributors(policy_guard: Any | None) -> tuple[MetadataContributor, ...]:
    if policy_guard is None:
        return ()
    return (PolicyContributor.from_guard(policy_guard),)


def _risk_tier_metadata_contributors(
    spec: TypefluxYamlSpec,
    policy_guard: Any | None,
) -> tuple[MetadataContributor, ...]:
    # Risk-tier evidence is stamped ONLY when the composed policy carried a
    # ``risk_tiers`` dimension (#300 D300-5), parity with the admission/policy
    # only-when-ran rule: no tier machinery in play ⇒ no metadata, so an ungoverned
    # (or risk-tier-free) workflow's manifest is byte-unchanged. Reuses the ONE
    # admission evaluation (``evaluate_risk_tier``) so evidence and enforcement agree.
    if policy_guard is None:
        return ()
    policy = getattr(policy_guard, "policy", None)
    payload = getattr(policy, "payload", None)
    if not isinstance(payload, Mapping):
        return ()
    # Local import: `project` modules import `yaml.runtime` at module load, so a
    # module-level import here would close an import cycle.
    from typeflux.project.policy_enforcement import evaluate_risk_tier

    evaluation = evaluate_risk_tier(spec, payload)
    if evaluation is None:
        return ()
    return (
        RiskTierContributor(
            declared=evaluation.declared,
            effective=evaluation.effective,
            floor_source=evaluation.floor_source,
            satisfied_controls=evaluation.satisfied_controls(),
        ),
    )


def _compensation_metadata_contributors(
    spec: TypefluxYamlSpec,
) -> tuple[MetadataContributor, ...]:
    # Saga-compensation provenance is stamped ONLY when the workflow declares a
    # ``compensate:`` step (#299 D299-5), parity with the risk-tier only-when-in-play
    # rule: no compensation planned ⇒ no metadata, so a non-saga workflow's manifest is
    # byte-unchanged. ``from_spec`` returns an inert contributor (no data) when nothing
    # compensates, which contributes nothing.
    contributor = CompensationContributor.from_spec(spec)
    if not contributor.payload:
        return ()
    return (contributor,)


def _admission_metadata_contributors(spec: Any) -> tuple[MetadataContributor, ...]:
    # Provenance is stamped ONLY when a spec entered via ``admit_spec`` (#298 D298-4):
    # that entry point attaches an ``_admission_provenance`` report to the spec. An
    # operator filesystem flow never sets it, so ``getattr`` yields None and the
    # manifest is byte-unchanged — no admission metadata unless admission actually ran.
    provenance = getattr(spec, "_admission_provenance", None)
    if provenance is None:
        return ()
    return (AdmissionContributor.from_report(provenance),)


def _temporal_connection_contributor(spec: TypefluxYamlSpec) -> TemporalConnectionContributor:
    temporal = spec.runtime.temporal
    tls = temporal.tls
    if isinstance(tls, bool):
        tls_enabled = tls
        tls_mode: Literal["disabled", "boolean", "custom"] = "boolean" if tls else "disabled"
    else:
        tls_enabled = True
        tls_mode = "custom"
    return TemporalConnectionContributor.from_env(
        address=temporal.address,
        namespace=temporal.namespace,
        tls_enabled=tls_enabled,
        tls_mode=tls_mode,
        api_key_configured=secret_value_configured(temporal.api_key),
    )


def _lifecycle_operation_payload(
    *,
    operation_type: Literal["query", "signal"],
    operation_name: str,
    workflow_name: str | None,
    workflow_id: str,
    run_id: str | None = None,
    status: WorkflowLifecycleStatus | None = None,
    review_user_decision: str | None = None,
    review_route_target: str | None = None,
    review_gate: str | None = None,
    cancellation_requested: bool = False,
):
    return lifecycle_operation_contribution(
        (LifecycleOperationContributor(),),
        LifecycleOperationMetadataContext(
            operation_type=operation_type,
            operation_name=operation_name,
            workflow_name=workflow_name,
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            review_user_decision=review_user_decision,
            review_route_target=review_route_target,
            review_gate=review_gate,
            cancellation_requested=cancellation_requested,
        ),
    )


def _review_gate_route(
    spec: TypefluxYamlSpec, command: ReviewCommand
) -> tuple[str | None, str | None]:
    """Resolve ``(gate_id, route_target)`` for a review command's audit metadata (#55 slice 4).

    Mirrors the runtime's DS4-2 targeting on the RESOLVED spec: an explicit ``command.gate``
    selects that gate; absent + a single gate (incl. the V1 ``review`` sugar) selects it;
    absent + several gates is ambiguous — the running workflow decides, so the metadata
    records no route rather than guessing. ``gate_id`` is None for single-``review`` specs
    (their operation metadata stays byte-identical; the gate name only exists for ``gates``).
    """
    lifecycle = spec.workflow.lifecycle
    if lifecycle is None:
        return None, None
    gates = lifecycle.resolved_gates()
    if not gates:
        return None, None
    if command.gate is not None:
        gate = next((candidate for candidate in gates if candidate.id == command.gate), None)
    elif len(gates) == 1:
        gate = gates[0]
    else:
        gate = None
    if gate is None:
        return None, None
    route = gate.user_decisions.get(command.user_decision)
    if route is None:
        return None, None
    gate_id = gate.id if lifecycle.gates is not None else None
    return gate_id, route.route


def _coerce_lifecycle_status(value: Any) -> WorkflowLifecycleStatus:
    return (
        value
        if isinstance(value, WorkflowLifecycleStatus)
        else WorkflowLifecycleStatus.model_validate(value)
    )


def _handle_run_id(handle: Any, fallback: str | None) -> str | None:
    run_id = getattr(handle, "run_id", None)
    return run_id if isinstance(run_id, str) else fallback


def _langfuse_host(configured_host: str | None) -> str | None:
    return configured_host or os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL")


def _langsmith_host(configured_host: str | None) -> str | None:
    return configured_host or os.getenv("LANGSMITH_HOST") or os.getenv("LANGCHAIN_ENDPOINT")


class _LabelOverrideRegistry:
    """Applies the registry default label to refs with no explicit selector.

    An explicit per-ref ``version`` (immutable pin) or ``label`` always wins;
    only refs that specify neither inherit ``runtime.registry.label``.
    """

    def __init__(self, *, registry: PromptRegistry, label: str) -> None:
        self._registry = registry
        self._label = label

    def resolve(self, ref: PromptRef):
        if ref.version is not None or ref.label is not None:
            return self._registry.resolve(ref)
        return self._registry.resolve(
            PromptRef(
                name=ref.name,
                label=self._label,
                prompt_type=ref.prompt_type,
            )
        )


__all__ = [
    "PreparedRuntimeBuild",
    "TypefluxYamlRuntime",
    "build_runtime",
    "prepare_runtime_build",
]
