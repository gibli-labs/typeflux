from __future__ import annotations

import gc
import json
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from typeflux.core.contracts import (
    ActivityDefinitionSource,
    AIActivity,
    ChatMessage,
    PromptRef,
)
from typeflux.manifests import (
    ActivityExecutionManifest,
    AIActivityManifest,
    AIInvocationContext,
    CodeProvenance,
    build_workflow_execution_manifest,
)
from typeflux.metadata import RuntimePlacementContributor
from typeflux.observability import (
    ActivityExecutionManifestView,
    InMemoryTraceStore,
    ObservationRecord,
    TraceListQuery,
    TracePage,
    TraceRecord,
    TraceSearchQuery,
    TraceSummaryView,
    WorkflowExecutionManifestView,
    diff_traces,
    export_execution_manifest,
    inspect_trace,
)
from typeflux.observability import __main__ as cli
from typeflux.observability import langfuse as langfuse_module
from typeflux.observability import semantic as semantic_module
from typeflux.observability.langfuse import (
    LangfuseObservabilityBackend,
    LangfuseTraceReader,
    LangfuseTraceWriter,
)
from typeflux.observability.redaction import NoOpRedactor
from typeflux.observability.semantic import (
    LangfuseAIActivityObserver,
    observe_workflow_invocation,
)
from typeflux.prompts.context import langfuse_prompt_context
from typeflux.providers import ProviderUsage


class PayloadModel(BaseModel):
    when: datetime
    text: str


class ActivityInput(BaseModel):
    text: str


class ActivityOutput(BaseModel):
    text: str


def test_activity_trace_rollup_sink_prefers_exact_run_id() -> None:
    sink = semantic_module._ActivityTraceRollupSink()
    fallback = _workflow_invocation("wf-shared")
    exact = _workflow_invocation("wf-shared", "run-exact")

    sink.refresh(exact)
    sink.refresh(fallback)

    assert sink.lookup("wf-shared", "run-exact") is exact
    assert sink.lookup("wf-shared", "run-other") is fallback
    assert sink.lookup("missing", "run-exact") is None


def test_byo_client_workflow_observation_redacts_payloads() -> None:
    client = _FakeObservationClient()

    with observe_workflow_invocation(
        client=client,
        workflow_name="SupportWorkflow",
        input_value={"contact": "user@example.com"},
        metadata={"note": "call 555-123-4567"},
    ) as handle:
        handle.update_output({"reply": "reach me at user@example.com"})
        handle.update_metadata({"note": "call 555-123-4567"})

    start = client.start_calls[0]
    assert "user@example.com" not in str(start["input"])
    assert "[REDACTED_EMAIL]" in str(start["input"])
    assert "555-123-4567" not in str(start["metadata"])
    assert "user@example.com" not in str(client.trace_io_calls)
    assert "user@example.com" not in str(client.observations[0].updates)
    assert "555-123-4567" not in str(client.observations[0].updates)


def test_byo_client_activity_generation_and_hook_payloads_are_redacted() -> None:
    client = _FakeObservationClient()
    observer = LangfuseAIActivityObserver(client=client)
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    with observer.observe_activity(
        activity=activity,
        input_value=ActivityInput(text="email user@example.com"),
        manifest=_activity_manifest("classify_ticket"),
        execution_manifest=_activity_execution_manifest("classify_ticket"),
        invocation_context=None,
    ) as activity_observation:
        with activity_observation.observe_generation(
            messages=[ChatMessage(role="user", content="classify user@example.com")],
            output_schema=ActivityOutput,
            metadata={},
            validation_attempt=0,
            model=None,
            temperature=None,
        ):
            pass
        with activity_observation.observe_hook(
            activity_input=ActivityInput(text="email user@example.com"),
            llm_output=ActivityOutput(text="billing"),
            metadata={},
        ):
            pass

    assert "user@example.com" not in str(client.start_calls[0]["input"])
    assert "[REDACTED_EMAIL]" in str(client.start_calls[0]["input"])
    child_calls = client.observations[0].child_start_calls
    assert len(child_calls) == 2
    assert all("user@example.com" not in str(call["input"]) for call in child_calls)


def test_typeflux_built_clients_skip_call_site_redaction() -> None:
    client = _FakeObservationClient()
    semantic_module._register_langfuse_redactor(client, NoOpRedactor())

    assert semantic_module._payload_redaction(client) is semantic_module._identity_payload


def test_safe_status_message_sanitizes_error_text() -> None:
    from typeflux.providers.errors import ProviderError

    assert semantic_module._safe_status_message(RuntimeError("call 555-123-4567")) == "RuntimeError"
    provider_error = ProviderError(reason="anthropic structured call failed", provider="anthropic")
    assert (
        semantic_module._safe_status_message(provider_error) == "anthropic structured call failed"
    )
    try:
        ActivityOutput.model_validate({"text": {"nested": "secret-value"}})
    except Exception as exc:
        message = semantic_module._safe_status_message(exc)
    assert "validation error(s) for ActivityOutput" in message
    assert "secret-value" not in message


def test_observation_error_status_messages_are_sanitized() -> None:
    client = _FakeObservationClient()

    with pytest.raises(RuntimeError):
        with observe_workflow_invocation(
            client=client,
            workflow_name="SupportWorkflow",
            input_value={"ticket": "a"},
        ):
            raise RuntimeError("user@example.com asked for a refund")

    error_updates = [
        update for update in client.observations[0].updates if update.get("level") == "ERROR"
    ]
    assert error_updates == [{"level": "ERROR", "status_message": "RuntimeError"}]


def test_workflow_observation_update_metadata_refreshes_sink_keys() -> None:
    sink = semantic_module._ActivityTraceRollupSink()
    observation = _workflow_invocation("wf-refresh", "run-old", rollup_sink=sink)
    observation.update_metadata(_workflow_metadata("wf-refresh", "run-old"))

    assert sink.lookup("wf-refresh", "run-old") is observation

    observation.update_metadata(_workflow_metadata("wf-refresh", "run-new"))

    assert sink.lookup("wf-refresh", "run-new") is observation
    assert sink._observations.get(("wf-refresh", "run-old")) is None
    assert sink._observation_keys[observation] == {
        ("wf-refresh", None),
        ("wf-refresh", "run-new"),
    }


def test_workflow_observation_metadata_refresh_preserves_executed_activities() -> None:
    observation = semantic_module._LangfuseWorkflowInvocation(
        client=_FakeObservationClient(),
        observation=_FakeObservation(),
    )
    planned_entry = {
        "activity_name": "classify_ticket",
        "activity_manifest_hash": "classify_ticket-activity",
    }
    observation.update_metadata(
        {"typeflux": {"execution_manifest": {"activities": [dict(planned_entry)]}}}
    )
    observation.merge_activity_manifest(_activity_execution_manifest("classify_ticket"))

    # Run-id refresh rebuilds metadata from the planned rollup; the executed
    # activity manifest merged above must survive.
    observation.update_metadata(
        {
            "typeflux": {
                "execution_manifest": {
                    "temporal_run_id": "run-refreshed",
                    "activities": [dict(planned_entry)],
                }
            }
        }
    )

    manifest = observation._metadata["typeflux"]["execution_manifest"]
    assert manifest["temporal_run_id"] == "run-refreshed"
    assert manifest["activities"][0]["manifest_hash"] == "classify_ticket-execution"
    assert manifest["activities"][0]["rendered_messages_hash"] == "rendered-hash"


def test_workflow_observation_metadata_refresh_accepts_newer_executed_entries() -> None:
    observation = semantic_module._LangfuseWorkflowInvocation(
        client=_FakeObservationClient(),
        observation=_FakeObservation(),
    )
    observation.update_metadata({"typeflux": {"execution_manifest": {"activities": []}}})
    observation.merge_activity_manifest(_activity_execution_manifest("classify_ticket"))
    refreshed_executed = dict(
        observation._metadata["typeflux"]["execution_manifest"]["activities"][0]
    )
    refreshed_executed["manifest_hash"] = "classify_ticket-execution-v2"

    observation.update_metadata(
        {"typeflux": {"execution_manifest": {"activities": [refreshed_executed]}}}
    )

    manifest = observation._metadata["typeflux"]["execution_manifest"]
    assert manifest["activities"][0]["manifest_hash"] == "classify_ticket-execution-v2"


def test_invocation_metadata_emits_structured_join_without_flat_keys() -> None:
    metadata = semantic_module.invocation_metadata(
        manifest=_activity_manifest("classify_ticket"),
        invocation_context=None,
        validation_attempt=0,
    )

    typeflux = metadata["typeflux"]
    assert typeflux["activity_name"] == "classify_ticket"
    assert typeflux["join"]["activity_manifest_hash"] == "classify_ticket-manifest"
    assert all(not key.startswith("typeflux.") for key in metadata)


def test_activity_trace_rollup_sink_unregister_keeps_newer_owner() -> None:
    sink = semantic_module._ActivityTraceRollupSink()
    old = _workflow_invocation("wf-reused", "run-reused")
    new = _workflow_invocation("wf-reused", "run-reused")

    sink.refresh(old)
    sink.refresh(new)
    sink.unregister(old)

    assert sink.lookup("wf-reused", "run-reused") is new


def test_activity_trace_rollup_sink_values_are_weak() -> None:
    sink = semantic_module._ActivityTraceRollupSink()
    observation = _workflow_invocation("wf-weak", "run-weak")
    sink.refresh(observation)
    observation_ref = weakref.ref(observation)

    del observation
    gc.collect()

    assert observation_ref() is None
    assert sink.lookup("wf-weak", "run-weak") is None


def test_workflow_observation_context_exit_unregisters_rollup_sink() -> None:
    client = _FakeObservationClient()
    sink = semantic_module._ActivityTraceRollupSink()

    with observe_workflow_invocation(
        client=client,
        workflow_name="SupportWorkflow",
        input_value={"ticket": "a"},
        metadata=_workflow_metadata("wf-exit", "run-exit"),
        rollup_sink=sink,
    ) as workflow:
        assert sink.lookup("wf-exit", "run-exit") is workflow

    assert sink.lookup("wf-exit", "run-exit") is None


def test_concurrent_activity_observations_merge_into_matching_workflow() -> None:
    client = _FakeObservationClient()
    sink = semantic_module._ActivityTraceRollupSink()
    observer = LangfuseAIActivityObserver(
        client=client,
        redactor=NoOpRedactor(),
        rollup_sink=sink,
    )
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    with observe_workflow_invocation(
        client=client,
        workflow_name="SupportWorkflow",
        input_value={"ticket": "a"},
        metadata=_workflow_metadata("wf-a", "run-a"),
        rollup_sink=sink,
    ) as workflow_a:
        with observe_workflow_invocation(
            client=client,
            workflow_name="SupportWorkflow",
            input_value={"ticket": "b"},
            metadata=_workflow_metadata("wf-b", "run-b"),
            rollup_sink=sink,
        ) as workflow_b:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        _observe_activity_from_thread,
                        observer,
                        activity,
                        "wf-a",
                        "run-a",
                        "activity-a",
                    ),
                    executor.submit(
                        _observe_activity_from_thread,
                        observer,
                        activity,
                        "wf-b",
                        "run-b",
                        "activity-b",
                    ),
                ]
                for future in futures:
                    future.result()

            assert _workflow_activity_names(workflow_a) == ["activity-a"]
            assert _workflow_activity_names(workflow_b) == ["activity-b"]


def test_direct_observer_without_writer_sink_does_not_cross_thread_rollup() -> None:
    client = _FakeObservationClient()
    observer = LangfuseAIActivityObserver(client=client, redactor=NoOpRedactor())
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    with observe_workflow_invocation(
        client=client,
        workflow_name="SupportWorkflow",
        input_value={"ticket": "a"},
        metadata=_workflow_metadata("wf-direct", "run-direct"),
    ) as workflow:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                _observe_activity_from_thread,
                observer,
                activity,
                "wf-direct",
                "run-direct",
                "direct-cross-thread",
            ).result()

    assert _workflow_activity_names(workflow) == []
    assert client.start_calls[-1]["metadata"]["typeflux"]["activity_name"] == (
        "direct-cross-thread"
    )


def test_contextvar_workflow_observation_wins_over_explicit_sink() -> None:
    client = _FakeObservationClient()
    sink = semantic_module._ActivityTraceRollupSink()
    sink_observation = _workflow_invocation("wf-context", "run-context")
    sink.refresh(sink_observation)
    observer = LangfuseAIActivityObserver(
        client=client,
        redactor=NoOpRedactor(),
        rollup_sink=sink,
    )
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    with observe_workflow_invocation(
        client=client,
        workflow_name="SupportWorkflow",
        input_value={"ticket": "a"},
        metadata=_workflow_metadata("wf-current", "run-current"),
    ) as current:
        with observer.observe_activity(
            activity=activity,
            input_value=ActivityInput(text="context-activity"),
            manifest=_activity_manifest("context-activity"),
            execution_manifest=_activity_execution_manifest("context-activity"),
            invocation_context=_invocation_context("wf-context", "run-context"),
        ):
            pass

    assert _workflow_activity_names(current) == ["context-activity"]
    assert _workflow_activity_names(sink_observation) == []


def test_langfuse_generation_observation_attaches_prompt_handle_without_metadata_leak() -> None:
    client = _FakeObservationClient()
    observer = LangfuseAIActivityObserver(client=client, redactor=NoOpRedactor())
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )
    prompt = object()
    execution_manifest = _activity_execution_manifest("classify_ticket")

    with observer.observe_activity(
        activity=activity,
        input_value=ActivityInput(text="hello"),
        manifest=_activity_manifest("classify_ticket"),
        execution_manifest=execution_manifest,
        invocation_context=_invocation_context("wf-prompt", "run-prompt"),
    ) as observation:
        with observation.observe_generation(
            messages=[],
            output_schema=ActivityOutput,
            metadata={"typeflux": {"activity_execution_manifest": execution_manifest.to_dict()}},
            validation_attempt=0,
            model="gpt-4o-mini",
            temperature=0,
            observation_context=langfuse_prompt_context(prompt),
        ):
            pass

    generation_start = client.observations[0].child_start_calls[0]
    assert generation_start["prompt"] is prompt
    assert _contains_identity(generation_start["metadata"], prompt) is False
    assert "prompt" not in generation_start["metadata"]["typeflux"]


def test_generation_observation_update_usage_sets_usage_details() -> None:
    client = _FakeObservationClient()
    observer = LangfuseAIActivityObserver(client=client)
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    with observer.observe_activity(
        activity=activity,
        input_value=ActivityInput(text="hello"),
        manifest=_activity_manifest("classify_ticket"),
        execution_manifest=_activity_execution_manifest("classify_ticket"),
        invocation_context=None,
    ) as observation:
        with observation.observe_generation(
            messages=[],
            output_schema=ActivityOutput,
            metadata={},
            validation_attempt=0,
            model="claude-test",
            temperature=None,
        ) as generation:
            generation.update_usage(
                ProviderUsage(input_tokens=120, output_tokens=45, model="claude-test")
            )

    generation_observation = client.observations[0].children[0]
    assert {
        "usage_details": {"input": 120, "output": 45, "total": 165},
        "model": "claude-test",
    } in generation_observation.updates


def test_activity_observation_without_rollup_match_does_not_crash() -> None:
    client = _FakeObservationClient()
    observer = LangfuseAIActivityObserver(
        client=client,
        redactor=NoOpRedactor(),
        rollup_sink=semantic_module._ActivityTraceRollupSink(),
    )
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    _observe_activity_from_thread(
        observer,
        activity,
        "wf-missing",
        "run-missing",
        "unmatched-activity",
    )

    assert client.start_calls[-1]["metadata"]["typeflux"]["activity_name"] == "unmatched-activity"


def test_langfuse_trace_writer_observer_uses_explicit_rollup_sink() -> None:
    client = _FakeObservationClient()
    writer = LangfuseTraceWriter(client=client, redactor=NoOpRedactor())
    observer = writer.create_activity_observer()
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )

    with writer.observe_workflow_invocation(
        workflow_name="SupportWorkflow",
        input_value={"ticket": "a"},
        metadata=_workflow_metadata("wf-writer", "run-writer"),
    ) as workflow:
        token = semantic_module._CURRENT_WORKFLOW_OBSERVATION.set(None)
        try:
            _observe_activity_from_thread(
                observer,
                activity,
                "wf-writer",
                "run-writer",
                "writer-activity",
            )
        finally:
            semantic_module._CURRENT_WORKFLOW_OBSERVATION.reset(token)

    assert _workflow_activity_names(workflow) == ["writer-activity"]


def test_writer_created_activity_observer_applies_metadata_contributors() -> None:
    client = _FakeObservationClient()
    writer = LangfuseTraceWriter(client=client, redactor=NoOpRedactor())
    observer = writer.create_activity_observer(
        metadata_contributors=(
            RuntimePlacementContributor(
                platform="kubernetes",
                k8s_namespace="typeflux-smoke",
                k8s_pod_name="worker-abc123",
                k8s_pod_uid="pod-uid",
                k8s_node_name="minikube",
                k8s_service_account="default",
                k8s_deployment_name="typeflux-worker",
                k8s_worker_name="typeflux-worker",
                container_image="typeflux-worker:k8s-smoke",
            ),
        )
    )
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )
    execution_manifest = _activity_execution_manifest("classify_ticket")

    with observer.observe_activity(
        activity=activity,
        input_value=ActivityInput(text="hello"),
        manifest=_activity_manifest("classify_ticket"),
        execution_manifest=execution_manifest,
        invocation_context=_invocation_context("wf-placement", "run-placement"),
    ) as observation:
        with observation.observe_generation(
            messages=[],
            output_schema=ActivityOutput,
            metadata={"typeflux": {"activity_execution_manifest": execution_manifest.to_dict()}},
            validation_attempt=0,
            model="gpt-4o-mini",
            temperature=0,
        ):
            pass

    expected = {
        "platform": "kubernetes",
        "kubernetes": {
            "namespace": "typeflux-smoke",
            "pod_name": "worker-abc123",
            "pod_uid": "pod-uid",
            "node_name": "minikube",
            "service_account": "default",
            "deployment_name": "typeflux-worker",
            "worker_name": "typeflux-worker",
        },
        "container_image": "typeflux-worker:k8s-smoke",
    }
    activity_metadata = client.start_calls[-1]["metadata"]
    generation_metadata = client.observations[-1].child_start_calls[-1]["metadata"]

    assert activity_metadata["typeflux"]["runtime_placement"] == expected
    assert generation_metadata["typeflux"]["runtime_placement"] == expected
    assert "runtime_placement" not in activity_metadata["typeflux"].get(
        "activity_execution_manifest", {}
    )


def test_in_memory_trace_inspect_reconstructs_manifest() -> None:
    workflow = build_workflow_execution_manifest(
        workflow_name="SupportWorkflow",
        workflow_id="wf-1",
        task_queue="support",
        activities=[
            {
                "activity_name": "classify_ticket",
                "manifest_hash": "activity-exec-hash",
                "activity_manifest_hash": "activity-hash",
                "input_schema": {"name": "TicketInput", "hash": "input-hash"},
                "output_schema": {"name": "Classification", "hash": "output-hash"},
                "prompt_ref": {"name": "support/classify"},
                "resolved_prompt_version": "prompt-v1",
                "provider_model": "gpt-4o-mini",
            }
        ],
    ).to_dict()
    trace = TraceRecord(
        trace_id="trace-1",
        metadata={"typeflux": {"execution_manifest": workflow}},
        observations=(
            ObservationRecord(
                name="classify_ticket.generation",
                type="generation",
                metadata={"typeflux": {"level": "generation"}},
            ),
            ObservationRecord(
                name="RunActivity",
                metadata={"temporal.workflow_id": "wf-1"},
            ),
        ),
    )

    inspection = inspect_trace(InMemoryTraceStore([trace]), "trace-1")

    assert inspection.workflow == workflow
    assert inspection.activities[0]["activity_name"] == "classify_ticket"
    assert inspection.generation_spans[0].name == "classify_ticket.generation"
    assert inspection.temporal_spans[0].name == "RunActivity"
    assert inspection.warnings == ()
    assert inspection.workflow_manifest is not None
    assert inspection.workflow_manifest.workflow_name == "SupportWorkflow"
    assert inspection.activity_manifests[0].activity_name == "classify_ticket"


def test_trace_search_filters_manifest_fields() -> None:
    matching = _trace(
        "trace-match",
        workflow_name="SupportWorkflow",
        git_sha="abc123",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        input_hash="input-hash",
        output_hash="output-hash",
        model="gpt-4o-mini",
    )
    other = _trace(
        "trace-other",
        workflow_name="SupportWorkflow",
        git_sha="abc123",
        activity_name="draft_response",
        prompt_ref="support/draft",
        input_hash="different-input",
        output_hash="different-output",
        model="gpt-4o",
    )
    store = InMemoryTraceStore([matching, other])
    workflow_contract_hash = matching.metadata["typeflux"]["execution_manifest"][
        "workflow_contract_hash"
    ]

    page = store.search_traces(
        TraceSearchQuery(
            prompt_ref="support/classify",
            activity_name="classify_ticket",
            input_schema_hash="input-hash",
            output_schema_hash="output-hash",
            provider_model="gpt-4o-mini",
            workflow_contract_hash=workflow_contract_hash,
            git_sha="abc123",
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-match"]


def test_trace_search_filters_temporal_connection_metadata() -> None:
    matching = _trace(
        "trace-match",
        workflow_name="SupportWorkflow",
        temporal_connection={
            "address": "namespace.tmprl.cloud:7233",
            "namespace": "namespace",
            "region": "us-east",
            "tls_enabled": True,
            "tls_mode": "boolean",
            "api_key_configured": True,
        },
    )
    other = _trace(
        "trace-other",
        workflow_name="SupportWorkflow",
        temporal_connection={
            "address": "other.tmprl.cloud:7233",
            "namespace": "other",
            "region": "eu-west",
            "tls_enabled": True,
            "tls_mode": "boolean",
            "api_key_configured": True,
        },
    )
    store = InMemoryTraceStore([matching, other])

    page = store.search_traces(
        TraceSearchQuery(
            temporal_address="namespace.tmprl.cloud:7233",
            temporal_namespace="namespace",
            temporal_region="us-east",
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-match"]


def test_trace_search_filters_runtime_placement_metadata() -> None:
    matching = _trace(
        "trace-match",
        workflow_name="SupportWorkflow",
        runtime_placement={
            "platform": "kubernetes",
            "kubernetes": {
                "namespace": "typeflux-smoke",
                "pod_name": "worker-abc123",
                "pod_uid": "pod-uid",
                "node_name": "minikube",
                "service_account": "default",
                "deployment_name": "typeflux-worker",
                "worker_name": "typeflux-worker",
            },
            "container_image": "typeflux-worker:k8s-smoke",
        },
    )
    other = _trace(
        "trace-other",
        workflow_name="SupportWorkflow",
        runtime_placement={
            "platform": "kubernetes",
            "kubernetes": {
                "namespace": "other",
                "pod_name": "worker-other",
                "deployment_name": "other-worker",
            },
            "container_image": "typeflux-worker:other",
        },
    )
    store = InMemoryTraceStore([matching, other])

    page = store.search_traces(
        TraceSearchQuery(
            runtime_platform="kubernetes",
            k8s_namespace="typeflux-smoke",
            k8s_deployment_name="typeflux-worker",
            k8s_pod_name="worker-abc123",
            container_image="typeflux-worker:k8s-smoke",
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-match"]


def test_trace_search_reads_runtime_placement_from_observation_metadata() -> None:
    placement = {
        "platform": "kubernetes",
        "kubernetes": {
            "namespace": "typeflux-smoke",
            "pod_name": "worker-abc123",
            "deployment_name": "typeflux-worker",
        },
        "container_image": "typeflux-worker:k8s-smoke",
    }
    workflow = build_workflow_execution_manifest(
        workflow_name="SupportWorkflow",
        workflow_id="support-workflow",
        task_queue="support",
        activities=[],
    ).to_dict()
    trace = TraceRecord(
        trace_id="trace-observation-placement",
        metadata={"typeflux": {"execution_manifest": workflow}},
        observations=(
            ObservationRecord(
                observation_id="activity-observation",
                metadata={"typeflux": {"runtime_placement": placement}},
            ),
        ),
    )
    store = InMemoryTraceStore([trace])

    page = store.search_traces(
        TraceSearchQuery(
            runtime_platform="kubernetes",
            k8s_namespace="typeflux-smoke",
            k8s_pod_name="worker-abc123",
            container_image="typeflux-worker:k8s-smoke",
        )
    )
    summary = TraceSummaryView.from_trace(trace).to_public_dict()

    assert [trace.trace_id for trace in page.traces] == ["trace-observation-placement"]
    assert summary["runtime_placement"] == placement


def test_trace_search_filters_policy_metadata() -> None:
    matching = _trace(
        "trace-match",
        workflow_name="SupportWorkflow",
        policy={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
            "policy_hash": "policy-hash",
            "enforcement_mode": "runtime",
            "admission_status": "passed",
        },
    )
    other = _trace(
        "trace-other",
        workflow_name="SupportWorkflow",
        policy={
            "version": "1",
            "selected_policy_ids": ["local"],
            "applied_policy_ids": ["local"],
            "policy_names": ["local"],
            "policy_hash": "other-policy-hash",
            "enforcement_mode": "runtime",
            "admission_status": "passed",
        },
    )
    store = InMemoryTraceStore([matching, other])

    page = store.search_traces(
        TraceSearchQuery(
            policy_id="regulated",
            policy_name="regulated",
            policy_hash="policy-hash",
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-match"]

    # policy_id matches the union of selected AND applied ids: "base" is applied
    # (pulled in via extends) on the matching trace but not selected, and absent
    # from the other trace entirely.
    applied_only = store.search_traces(TraceSearchQuery(policy_id="base"))
    assert [trace.trace_id for trace in applied_only.traces] == ["trace-match"]


def test_trace_summary_merges_direct_policy_metadata() -> None:
    trace = _trace(
        "trace-policy-direct",
        workflow_name="SupportWorkflow",
        policy={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
            "policy_hash": "policy-hash",
            "admission_status": "passed",
        },
        trace_policy={
            "enforcement_mode": "project_submit",
        },
    )
    store = InMemoryTraceStore([trace])

    summary = TraceSummaryView.from_trace(trace)
    page = store.search_traces(TraceSearchQuery(policy_hash="policy-hash"))
    manifest = export_execution_manifest(store, "trace-policy-direct")

    assert summary.policy is not None
    assert summary.policy.policy_hash == "policy-hash"
    assert summary.policy.enforcement_mode == "project_submit"
    assert [item.trace_id for item in page.traces] == ["trace-policy-direct"]
    assert "enforcement_mode" not in manifest["workflow"]["policy"]


def test_trace_summary_omits_missing_temporal_connection_metadata() -> None:
    summary = TraceSummaryView.from_trace(
        _trace("trace-no-temporal", workflow_name="SupportWorkflow")
    ).to_public_dict()

    assert "temporal_connection" not in summary


def test_trace_summary_omits_missing_runtime_placement_metadata() -> None:
    summary = TraceSummaryView.from_trace(
        _trace("trace-no-placement", workflow_name="SupportWorkflow")
    ).to_public_dict()

    assert "runtime_placement" not in summary


def test_trace_summary_omits_missing_policy_metadata() -> None:
    summary = TraceSummaryView.from_trace(
        _trace("trace-no-policy", workflow_name="SupportWorkflow")
    ).to_public_dict()

    assert "policy" not in summary


def test_trace_list_matches_lifecycle_operation_workflow_id_without_root_observation() -> None:
    matching = TraceRecord(
        trace_id="trace-control-plane",
        observations=(
            ObservationRecord(
                name="TypefluxLifecycleQuery:typeflux_lifecycle_status",
                metadata={
                    "typeflux": {
                        "level": "lifecycle_operation",
                        "lifecycle_operation": {
                            "operation_type": "query",
                            "operation_name": "typeflux_lifecycle_status",
                            "workflow_id": "wf-ops-1",
                        },
                    }
                },
            ),
        ),
    )
    other = TraceRecord(
        trace_id="trace-other-control-plane",
        observations=(
            ObservationRecord(
                name="TypefluxLifecycleQuery:typeflux_lifecycle_status",
                metadata={
                    "typeflux": {
                        "level": "lifecycle_operation",
                        "lifecycle_operation": {
                            "operation_type": "query",
                            "operation_name": "typeflux_lifecycle_status",
                            "workflow_id": "wf-other",
                        },
                    }
                },
            ),
        ),
    )
    store = InMemoryTraceStore([matching, other])

    page = store.list_traces(TraceListQuery(workflow_id="wf-ops-1", limit=1))

    assert [trace.trace_id for trace in page.traces] == ["trace-control-plane"]


def test_trace_list_matches_ts_runtime_workflow_id_forms() -> None:
    # The TS runtime's two identity surfaces (#686): the grouped parent trace's
    # BARE `workflow_id` metadata (caller-side, #681) and a worker-only run's
    # per-activity `typeflux.activity_execution_manifest.workflow_id` (#682).
    parent_metadata = TraceRecord(
        trace_id="ts-parent",
        metadata={"typeflux_workflow": "W", "workflow_id": "wf-ts-1"},
    )
    worker_only = TraceRecord(
        trace_id="ts-worker-only",
        observations=(
            ObservationRecord(
                name="assess",
                metadata={
                    "typeflux": {
                        "activity_execution_manifest": {
                            "activity_name": "assess",
                            "workflow_id": "wf-ts-2",
                        }
                    }
                },
            ),
        ),
    )
    other = TraceRecord(trace_id="ts-other", metadata={"workflow_id": "wf-ts-other"})
    store = InMemoryTraceStore([parent_metadata, worker_only, other])

    by_parent = store.list_traces(TraceListQuery(workflow_id="wf-ts-1", limit=1))
    assert [trace.trace_id for trace in by_parent.traces] == ["ts-parent"]

    by_activity_manifest = store.list_traces(TraceListQuery(workflow_id="wf-ts-2", limit=1))
    assert [trace.trace_id for trace in by_activity_manifest.traces] == ["ts-worker-only"]


def test_trace_list_matches_typeflux_workflow_id_without_root_manifest() -> None:
    matching = _provider_trace(
        "provider-match",
        workflow_name="SupportWorkflow",
        workflow_id="wf-ops-1",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        prompt_version="prompt-v1",
        model="gpt-4o-mini",
        activity_hash="activity-hash",
        input_hash="input-hash",
        output_hash="output-hash",
    )
    other = _provider_trace(
        "provider-other",
        workflow_name="SupportWorkflow",
        workflow_id="wf-other",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        prompt_version="prompt-v1",
        model="gpt-4o-mini",
        activity_hash="activity-hash",
        input_hash="input-hash",
        output_hash="output-hash",
    )
    store = InMemoryTraceStore([matching, other])
    trace_query_hint = {"workflow_id": "wf-ops-1", "limit": 1}

    page = store.list_traces(TraceListQuery(**trace_query_hint))

    assert [trace.trace_id for trace in page.traces] == ["provider-match"]


def test_trace_search_matches_provider_only_observation_metadata() -> None:
    matching = _provider_trace(
        "provider-match",
        workflow_name="SupportWorkflow",
        workflow_id="workflow-1",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        prompt_version="prompt-v1",
        model="gpt-4o-mini",
        activity_hash="activity-hash",
        input_hash="input-hash",
        output_hash="output-hash",
    )
    other = _provider_trace(
        "provider-other",
        workflow_name="SupportWorkflow",
        workflow_id="workflow-1",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        prompt_version="prompt-v1",
        model="gpt-4o",
        activity_hash="activity-hash",
        input_hash="input-hash",
        output_hash="output-hash",
    )
    store = InMemoryTraceStore([matching, other])

    page = store.search_traces(
        TraceSearchQuery(
            workflow_name="SupportWorkflow",
            workflow_id="workflow-1",
            activity_name="classify_ticket",
            prompt_ref="support/classify",
            resolved_prompt_version="prompt-v1",
            input_schema_hash="input-hash",
            output_schema_hash="output-hash",
            provider_model="gpt-4o-mini",
            activity_manifest_hash="activity-hash",
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["provider-match"]


def test_trace_search_can_disable_provider_only_observation_fallback() -> None:
    matching = _provider_trace(
        "provider-match",
        workflow_name="SupportWorkflow",
        workflow_id="workflow-1",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        prompt_version="prompt-v1",
        model="gpt-4o-mini",
        activity_hash="activity-hash",
        input_hash="input-hash",
        output_hash="output-hash",
    )
    store = InMemoryTraceStore([matching])

    page = store.search_traces(
        TraceSearchQuery(
            workflow_name="SupportWorkflow",
            workflow_id="workflow-1",
            activity_name="classify_ticket",
            prompt_ref="support/classify",
            resolved_prompt_version="prompt-v1",
            input_schema_hash="input-hash",
            output_schema_hash="output-hash",
            provider_model="gpt-4o-mini",
            activity_manifest_hash="activity-hash",
            include_untagged_fallback=False,
        )
    )

    assert page.traces == ()


def test_langfuse_search_uses_server_tags_and_post_filters_manifest_fields() -> None:
    matching = _trace(
        "trace-match",
        workflow_name="SupportWorkflow",
        git_sha="abc123",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        model="gpt-4o-mini",
    )
    false_positive = _trace(
        "trace-false-positive",
        workflow_name="SupportWorkflow",
        git_sha="different",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        model="gpt-4o-mini",
    )
    client = _FakeLangfuseClient([matching.to_public_dict(), false_positive.to_public_dict()])
    reader = LangfuseTraceReader(client=client)

    page = reader.search_traces(
        TraceSearchQuery(
            workflow_name="SupportWorkflow",
            activity_name="classify_ticket",
            prompt_ref="support/classify",
            provider_model="gpt-4o-mini",
            git_sha="abc123",
            scan_pages=1,
            include_untagged_fallback=False,
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-match"]
    assert {"metadata", "model", "prompt", "trace_context"} <= set(
        client.api.observations.get_many_calls[0]["fields"].split(",")
    )
    assert client.api.observations.get_many_calls[0]["tags"] == [
        "typeflux.workflow:SupportWorkflow",
        "typeflux.activity:classify_ticket",
        "typeflux.prompt:support/classify",
        "typeflux.model:gpt-4o-mini",
    ]
    assert page.complete is True
    assert page.warnings == ()
    assert client.api.trace.list_calls == []


def test_langfuse_search_warns_when_scan_page_bound_truncates() -> None:
    payloads = [
        _trace(f"trace-{index}", workflow_name="SupportWorkflow").to_public_dict()
        for index in range(60)
    ]
    client = _FakeLangfuseClient(payloads)
    reader = LangfuseTraceReader(client=client)

    page = reader.search_traces(
        TraceSearchQuery(
            limit=1,
            scan_pages=1,
            prompt_ref="support/never-matches",
            include_untagged_fallback=False,
        )
    )

    assert page.traces == ()
    assert page.complete is False
    assert page.next_cursor is not None
    assert "Langfuse trace search incomplete" in page.warnings[0]
    assert "warnings" in page.to_summary_dict()


def test_langfuse_search_uses_v2_observations_with_untagged_fallback() -> None:
    old_trace = _trace("old-trace", workflow_name="SupportWorkflow", prompt_ref="support/classify")
    client = _FakeLangfuseClient([old_trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)

    page = reader.search_traces(
        TraceSearchQuery(
            prompt_ref="support/classify", scan_pages=1, include_untagged_fallback=True
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["old-trace"]
    assert len(client.api.observations.get_many_calls) == 1
    assert client.api.trace.list_calls == []


def test_langfuse_search_respects_disabled_untagged_fallback() -> None:
    old_trace = _provider_trace(
        "provider-only",
        workflow_name="SupportWorkflow",
        workflow_id="workflow-1",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        prompt_version="prompt-v1",
        model="gpt-4o-mini",
        activity_hash="activity-hash",
        input_hash="input-hash",
        output_hash="output-hash",
    )
    client = _FakeLangfuseClient([old_trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)

    page = reader.search_traces(
        TraceSearchQuery(
            workflow_name="SupportWorkflow",
            activity_name="classify_ticket",
            prompt_ref="support/classify",
            scan_pages=1,
            include_untagged_fallback=False,
        )
    )

    assert page.traces == ()
    assert len(client.api.observations.get_many_calls) == 1
    assert client.api.trace.list_calls == []


def test_langfuse_search_passes_backend_filter_string_and_post_filters() -> None:
    matching = _trace("trace-match", workflow_name="SupportWorkflow", prompt_ref="support/classify")
    false_positive = _trace(
        "trace-false-positive", workflow_name="SupportWorkflow", prompt_ref="support/other"
    )
    client = _FakeLangfuseClient([matching.to_public_dict(), false_positive.to_public_dict()])
    reader = LangfuseTraceReader(client=client)
    backend_filter = (
        '[{"type":"arrayOptions","column":"tags","operator":"all of","value":["typeflux"]}]'
    )

    page = reader.search_traces(
        TraceSearchQuery(prompt_ref="support/classify", backend_filter=backend_filter)
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-match"]
    assert client.api.observations.get_many_calls[0]["filter"] == backend_filter
    assert client.api.trace.list_calls == []


def test_langfuse_search_serializes_backend_filter_payload() -> None:
    trace = _trace("trace-filter", workflow_name="SupportWorkflow")
    client = _FakeLangfuseClient([trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)
    backend_filter = [
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["typeflux"],
        }
    ]

    page = reader.search_traces(TraceSearchQuery(backend_filter=backend_filter))

    assert [trace.trace_id for trace in page.traces] == ["trace-filter"]
    serialized_filter = client.api.observations.get_many_calls[0]["filter"]
    assert isinstance(serialized_filter, str)
    assert json.loads(serialized_filter) == backend_filter


def test_langfuse_list_uses_default_bounded_v2_window(monkeypatch) -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    trace = _trace("trace-window", workflow_name="SupportWorkflow")
    client = _FakeLangfuseClient([trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)
    monkeypatch.setattr(langfuse_module, "_utc_now", lambda: now)

    page = reader.list_traces(TraceListQuery(limit=5))

    call = client.api.observations.get_many_calls[0]
    assert [trace.trace_id for trace in page.traces] == ["trace-window"]
    assert call["from_start_time"] == now - timedelta(hours=24)
    assert call["to_start_time"] == now
    assert {"metadata", "model", "prompt", "trace_context"} <= set(call["fields"].split(","))
    assert "usage" in call["fields"].split(",")
    assert client.api.trace.list_calls == []


def test_langfuse_list_projection_preserves_usage_in_raw_observation() -> None:
    payload = {
        "id": "trace-usage",
        "observations": [
            {
                "id": "generation-1",
                "name": "assess_claim.generation",
                "type": "generation",
                "metadata": {"typeflux": {"level": "generation"}},
                "usage": {"input": 571, "output": 168, "total": 739},
            }
        ],
    }
    client = _FakeLangfuseClient([payload])
    reader = LangfuseTraceReader(client=client)

    page = reader.list_traces(TraceListQuery(limit=5))

    generation = next(
        observation
        for trace in page.traces
        for observation in trace.observations
        if observation.observation_id == "generation-1"
    )
    assert generation.raw["usage"] == {"input": 571, "output": 168, "total": 739}


def test_langfuse_list_hydrates_workflow_id_candidates_before_filtering() -> None:
    partial_payload = {
        "id": "trace-partial",
        "observations": [
            {
                "id": "generation-1",
                "name": "assess_claim.generation",
                "type": "generation",
                "metadata": {"typeflux": {"level": "generation"}},
            }
        ],
    }
    full_payload = {
        "id": "trace-partial",
        "observations": [
            *partial_payload["observations"],
            {
                "id": "lifecycle-1",
                "name": "TypefluxLifecycleQuery:typeflux_lifecycle_status",
                "type": "span",
                "metadata": {
                    "typeflux": {
                        "level": "lifecycle_operation",
                        "lifecycle_operation": {
                            "operation_type": "query",
                            "operation_name": "typeflux_lifecycle_status",
                            "workflow_id": "wf-ops-1",
                        },
                    }
                },
            },
        ],
    }
    partial_rows = _observation_rows_from_trace_payload(partial_payload)
    full_rows = _observation_rows_from_trace_payload(full_payload)

    class ObservationsApi:
        def __init__(self) -> None:
            self.get_many_calls: list[dict[str, Any]] = []

        def get_many(self, **kwargs: Any) -> dict[str, Any]:
            self.get_many_calls.append(kwargs)
            rows = full_rows if kwargs.get("trace_id") == "trace-partial" else partial_rows
            return {"data": rows, "meta": {"cursor": None}}

    class Api:
        def __init__(self) -> None:
            self.trace = _FakeTraceApi(full_payload)
            self.observations = ObservationsApi()

    class Client:
        def __init__(self) -> None:
            self.api = Api()

    client = Client()
    reader = LangfuseTraceReader(client=client)

    page = reader.list_traces(TraceListQuery(workflow_id="wf-ops-1", limit=5))

    assert [trace.trace_id for trace in page.traces] == ["trace-partial"]
    assert client.api.observations.get_many_calls[0].get("trace_id") is None
    assert client.api.observations.get_many_calls[1]["trace_id"] == "trace-partial"


def test_langfuse_list_normalizes_naive_query_datetimes_to_utc() -> None:
    trace = _trace("trace-window", workflow_name="SupportWorkflow")
    client = _FakeLangfuseClient([trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)

    page = reader.list_traces(
        TraceListQuery(
            limit=5,
            since=datetime(2026, 5, 27, 11, 0),
            until=datetime(2026, 5, 27, 13, 0),
        )
    )

    call = client.api.observations.get_many_calls[0]
    assert [trace.trace_id for trace in page.traces] == ["trace-window"]
    assert call["from_start_time"] == datetime(2026, 5, 27, 11, 0, tzinfo=UTC)
    assert call["to_start_time"] == datetime(2026, 5, 27, 13, 0, tzinfo=UTC)


def test_langfuse_list_filters_unsupported_observations_kwargs_from_signature() -> None:
    trace = _trace("trace-signature", workflow_name="SupportWorkflow")
    payload = trace.to_public_dict()

    class ObservationsApi:
        def __init__(self) -> None:
            self.get_many_calls: list[dict[str, Any]] = []

        def get_many(
            self,
            *,
            limit: int,
            from_start_time: datetime,
            to_start_time: datetime,
        ) -> dict[str, Any]:
            self.get_many_calls.append(
                {
                    "limit": limit,
                    "from_start_time": from_start_time,
                    "to_start_time": to_start_time,
                }
            )
            return {
                "data": _observation_rows_from_trace_payload(payload),
                "meta": {"cursor": None},
            }

    class Api:
        def __init__(self) -> None:
            self.trace = _FakeTraceApi(payload)
            self.observations = ObservationsApi()

    class Client:
        def __init__(self) -> None:
            self.api = Api()

    client = Client()
    reader = LangfuseTraceReader(client=client)

    page = reader.list_traces(TraceListQuery(limit=5))

    call = client.api.observations.get_many_calls[0]
    assert [trace.trace_id for trace in page.traces] == ["trace-signature"]
    assert set(call) == {"limit", "from_start_time", "to_start_time"}
    assert call["limit"] == 50
    assert isinstance(call["from_start_time"], datetime)
    assert isinstance(call["to_start_time"], datetime)


def test_langfuse_list_reraises_internal_observations_type_error() -> None:
    class ObservationsApi:
        def __init__(self) -> None:
            self.calls = 0

        def get_many(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise TypeError("internal SDK failure")

    class Api:
        def __init__(self) -> None:
            self.trace = _FakeTraceApi([])
            self.observations = ObservationsApi()

    class Client:
        def __init__(self) -> None:
            self.api = Api()

    client = Client()
    reader = LangfuseTraceReader(client=client)

    with pytest.raises(TypeError, match="internal SDK failure"):
        reader.list_traces(TraceListQuery(limit=5))

    assert client.api.observations.calls == 1


def test_langfuse_list_uses_v2_cursor_pagination() -> None:
    traces = [
        _trace(f"trace-{index}", workflow_name="SupportWorkflow").to_public_dict()
        for index in range(51)
    ]
    client = _FakeLangfuseClient(traces)
    reader = LangfuseTraceReader(client=client)

    first_page = reader.list_traces(TraceListQuery(limit=1))
    second_page = reader.list_traces(TraceListQuery(limit=1, cursor=first_page.next_cursor))

    assert [trace.trace_id for trace in first_page.traces] == ["trace-0"]
    assert first_page.next_cursor == "cursor-51"
    assert [trace.trace_id for trace in second_page.traces] == ["trace-50"]
    assert client.api.observations.get_many_calls[1]["cursor"] == "cursor-51"


def test_langfuse_search_starts_from_query_cursor() -> None:
    traces = [
        _trace(f"trace-{index}", workflow_name="SupportWorkflow").to_public_dict()
        for index in range(51)
    ]
    client = _FakeLangfuseClient(traces)
    reader = LangfuseTraceReader(client=client)

    page = reader.search_traces(
        TraceSearchQuery(limit=1, cursor="cursor-51", scan_pages=1, workflow_name="SupportWorkflow")
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-50"]
    assert client.api.observations.get_many_calls[0]["cursor"] == "cursor-51"


def test_langfuse_get_trace_uses_v2_detail_fields_and_trace_id_filter() -> None:
    trace = _trace("trace-detail", workflow_name="SupportWorkflow")
    client = _FakeLangfuseClient([trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)

    result = reader.get_trace(
        "trace-detail",
        since=datetime(2026, 5, 27, 11, 0, tzinfo=UTC),
        until=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
    )

    call = client.api.observations.get_many_calls[0]
    assert result.trace_id == "trace-detail"
    assert call["trace_id"] == "trace-detail"
    assert {"metadata", "model", "prompt", "trace_context", "io"} <= set(call["fields"].split(","))
    assert "parse_io_as_json" not in call
    assert call["expand_metadata"] == "true"
    assert call["from_start_time"] == datetime(2026, 5, 27, 11, 0, tzinfo=UTC)
    assert call["to_start_time"] == datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    assert client.api.trace.list_calls == []


def test_langfuse_get_trace_without_bounds_ignores_default_lookback() -> None:
    old_trace = _trace("trace-ancient", workflow_name="SupportWorkflow")
    client = _FakeLangfuseClient([old_trace.to_public_dict()])
    reader = LangfuseTraceReader(client=client)

    result = reader.get_trace("trace-ancient")

    call = client.api.observations.get_many_calls[0]
    assert result.trace_id == "trace-ancient"
    assert "from_start_time" not in call
    assert "to_start_time" not in call


def test_langfuse_get_trace_paginates_high_fanout_trace_to_completion() -> None:
    client = _FakeLangfuseClient(_high_fanout_trace_payload("trace-fanout", activity_count=7))
    reader = LangfuseTraceReader(client=client, detail_page_size=3)

    trace = reader.get_trace("trace-fanout")
    inspection = inspect_trace(InMemoryTraceStore([trace]), "trace-fanout")

    assert trace.retrieval is not None
    assert trace.retrieval.complete is True
    assert trace.retrieval.pages_read == 3
    assert trace.retrieval.observations_read == 8
    assert trace.retrieval.next_cursor is None
    assert trace.retrieval.warnings == ()
    assert [call.get("cursor") for call in client.api.observations.get_many_calls] == [
        None,
        "cursor-4",
        "cursor-7",
    ]
    assert [activity.activity_name for activity in inspection.activity_manifests] == [
        f"review_page_{index}" for index in range(7)
    ]
    assert inspection.warnings == ()


def test_langfuse_get_trace_marks_high_fanout_trace_incomplete_at_page_bound() -> None:
    client = _FakeLangfuseClient(_high_fanout_trace_payload("trace-truncated", activity_count=7))
    reader = LangfuseTraceReader(client=client, detail_page_size=3, max_detail_pages=2)

    trace = reader.get_trace("trace-truncated")
    inspection = inspect_trace(InMemoryTraceStore([trace]), "trace-truncated")
    manifest = inspection.to_manifest_dict()

    assert trace.retrieval is not None
    assert trace.retrieval.complete is False
    assert trace.retrieval.pages_read == 2
    assert trace.retrieval.observations_read == 6
    assert trace.retrieval.next_cursor == "cursor-7"
    assert "Langfuse trace retrieval incomplete" in trace.retrieval.warnings[0]
    assert len(inspection.activity_manifests) == 5
    assert inspection.warnings == trace.retrieval.warnings
    assert manifest["warnings"] == list(trace.retrieval.warnings)


def test_langfuse_get_trace_respects_per_call_max_detail_pages() -> None:
    client = _FakeLangfuseClient(_high_fanout_trace_payload("trace-bound", activity_count=4))
    reader = LangfuseTraceReader(client=client, detail_page_size=2, max_detail_pages=10)

    trace = reader.get_trace("trace-bound", max_detail_pages=1)

    assert trace.retrieval is not None
    assert trace.retrieval.complete is False
    assert trace.retrieval.pages_read == 1
    assert trace.retrieval.max_pages == 1
    assert trace.retrieval.next_cursor == "cursor-3"


def test_langfuse_reader_requires_observations_v2() -> None:
    class ClientWithoutV2:
        api = object()

    reader = LangfuseTraceReader(client=ClientWithoutV2())

    with pytest.raises(RuntimeError, match="Observations API v2 is required"):
        reader.list_traces(TraceListQuery())


def test_in_memory_search_rejects_backend_filter() -> None:
    store = InMemoryTraceStore([_trace("trace-local", workflow_name="SupportWorkflow")])

    with pytest.raises(RuntimeError, match="backend_filter is not supported"):
        store.search_traces(TraceSearchQuery(backend_filter=[]))


def test_trace_list_filters_workflow_and_status() -> None:
    ok = _trace("ok", workflow_name="SupportWorkflow")
    error = TraceRecord(
        trace_id="error",
        metadata=ok.metadata,
        observations=(ObservationRecord(level="ERROR"),),
    )
    store = InMemoryTraceStore([ok, error])

    page = store.list_traces(TraceListQuery(workflow_name="SupportWorkflow", status="error"))

    assert [trace.trace_id for trace in page.traces] == ["error"]


def test_trace_list_normalizes_mixed_naive_and_aware_datetimes() -> None:
    trace = TraceRecord(
        trace_id="trace-window",
        timestamp=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
    )
    store = InMemoryTraceStore([trace])

    page = store.list_traces(
        TraceListQuery(
            since=datetime(2026, 5, 27, 11, 0),
            until=datetime(2026, 5, 27, 13, 0),
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-window"]


def test_trace_search_normalizes_mixed_naive_and_aware_datetimes() -> None:
    trace = TraceRecord(
        trace_id="trace-window",
        timestamp=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
    )
    store = InMemoryTraceStore([trace])

    page = store.search_traces(
        TraceSearchQuery(
            since=datetime(2026, 5, 27, 11, 0),
            until=datetime(2026, 5, 27, 13, 0),
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-window"]


def test_trace_list_normalizes_naive_trace_datetime_against_aware_query() -> None:
    trace = TraceRecord(
        trace_id="trace-window",
        timestamp=datetime(2026, 5, 27, 12, 0),
    )
    store = InMemoryTraceStore([trace])

    page = store.list_traces(
        TraceListQuery(
            since=datetime(2026, 5, 27, 11, 0, tzinfo=UTC),
            until=datetime(2026, 5, 27, 13, 0, tzinfo=UTC),
        )
    )

    assert [trace.trace_id for trace in page.traces] == ["trace-window"]


def test_diff_traces_reports_manifest_changes() -> None:
    left = _trace(
        "left",
        workflow_name="SupportWorkflow",
        git_sha="abc123",
        prompt_ref="support/classify",
        prompt_version="v1",
        model="gpt-4o-mini",
    )
    right = _trace(
        "right",
        workflow_name="SupportWorkflow",
        git_sha="def456",
        prompt_ref="support/classify",
        prompt_version="v2",
        model="gpt-4o",
    )
    diff = diff_traces(InMemoryTraceStore([left, right]), "left", "right")

    assert {field.field for field in diff.workflow.changed_fields} == {
        "workflow_contract_hash",
        "manifest_hash",
        "code_provenance",
    }
    changed = [activity for activity in diff.activities if activity.status == "changed"]
    assert [activity.activity_name for activity in changed] == ["classify_ticket"]
    assert {field.field for field in changed[0].changed_fields} >= {
        "resolved_prompt_version",
        "provider_model",
    }


def test_diff_traces_reports_policy_changes() -> None:
    left = _trace(
        "left",
        workflow_name="SupportWorkflow",
        policy={
            "version": "1",
            "selected_policy_ids": ["local"],
            "applied_policy_ids": ["local"],
            "policy_names": ["local"],
            "policy_hash": "local-policy-hash",
            "enforcement_mode": "runtime",
            "admission_status": "passed",
        },
    )
    right = _trace(
        "right",
        workflow_name="SupportWorkflow",
        policy={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
            "policy_hash": "regulated-policy-hash",
            "enforcement_mode": "runtime",
            "admission_status": "passed",
        },
    )

    diff = diff_traces(InMemoryTraceStore([left, right]), "left", "right")

    changed_fields = {field.field for field in diff.workflow.changed_fields}
    # A policy-only change is reported once, under "policy" — not duplicated into
    # the catch-all "contributions" blob.
    assert "policy" in changed_fields
    assert "contributions" not in changed_fields
    policy_diff = next(field for field in diff.workflow.changed_fields if field.field == "policy")
    assert policy_diff.left["policy_hash"] == "local-policy-hash"
    assert policy_diff.right["policy_hash"] == "regulated-policy-hash"


def test_diff_traces_reports_definition_source_changes() -> None:
    left = _trace(
        "left",
        workflow_name="SupportWorkflow",
        definition_source={
            "kind": "yaml",
            "yaml_project": "support",
            "yaml_name": "support_yaml",
        },
    )
    right = _trace(
        "right",
        workflow_name="SupportWorkflow",
        definition_source={
            "kind": "python",
            "module": "support.activities",
            "export": "ALL_ACTIVITIES",
        },
    )

    diff = diff_traces(InMemoryTraceStore([left, right]), "left", "right")

    changed = [activity for activity in diff.activities if activity.status == "changed"]
    assert {field.field for field in changed[0].changed_fields} == {
        "activity_manifest_hash",
        "manifest_hash",
        "definition_source",
    }


def test_langfuse_reader_normalizes_trace_payloads() -> None:
    client = _FakeLangfuseClient(
        {
            "id": "trace-1",
            "name": "TypefluxWorkflow:SupportWorkflow",
            "metadata": {"typeflux": {"execution_manifest": {"workflow_name": "SupportWorkflow"}}},
            "observations": [
                {
                    "id": "obs-1",
                    "name": "classify_ticket",
                    "type": "span",
                    "metadata": {"typeflux": {"level": "activity"}},
                }
            ],
        }
    )
    reader = LangfuseTraceReader(client=client)

    trace = reader.get_trace("trace-1")

    assert trace.trace_id == "trace-1"
    activity = next(
        observation for observation in trace.observations if observation.observation_id == "obs-1"
    )
    assert activity.metadata["typeflux"]["level"] == "activity"


def test_langfuse_backend_from_client_builds_writer_and_reader() -> None:
    client = _FakeLangfuseClient({"id": "trace-1"})
    backend = LangfuseObservabilityBackend.from_client(client, redactor=NoOpRedactor())

    assert backend.writer.enabled is True
    assert backend.writer.configure_temporal_plugin() is None
    assert backend.reader.get_trace("trace-1").trace_id == "trace-1"


def test_langfuse_writer_observes_lifecycle_query_operation() -> None:
    client = _FakeObservationClient()
    writer = LangfuseTraceWriter(client=client, redactor=NoOpRedactor())
    metadata = {
        "typeflux": {
            "level": "lifecycle_operation",
            "lifecycle_operation": {
                "operation_type": "query",
                "operation_name": "typeflux_lifecycle_status",
                "workflow_name": "LifecycleReviewWorkflow",
                "workflow_id": "lifecycle-review-1",
            },
        }
    }

    with writer.observe_lifecycle_operation(
        operation_type="query",
        operation_name="typeflux_lifecycle_status",
        workflow_name="LifecycleReviewWorkflow",
        workflow_id="lifecycle-review-1",
        metadata=metadata,
    ) as observation:
        observation.update_metadata(
            {
                "typeflux": {
                    "level": "lifecycle_operation",
                    "lifecycle_operation": {
                        "operation_type": "query",
                        "operation_name": "typeflux_lifecycle_status",
                        "workflow_name": "LifecycleReviewWorkflow",
                        "workflow_id": "lifecycle-review-1",
                        "status": {"state": "waiting_for_review"},
                    },
                }
            }
        )

    assert client.start_calls[0]["name"] == "TypefluxLifecycleQuery:typeflux_lifecycle_status"
    assert client.start_calls[0]["as_type"] == "span"
    assert client.start_calls[0]["metadata"] == metadata
    assert client.observations[0].updates[-1]["metadata"]["typeflux"]["lifecycle_operation"][
        "status"
    ] == {"state": "waiting_for_review"}


def test_langfuse_writer_observes_lifecycle_signal_operation() -> None:
    client = _FakeObservationClient()
    writer = LangfuseTraceWriter(client=client, redactor=NoOpRedactor())

    with writer.observe_lifecycle_operation(
        operation_type="signal",
        operation_name="typeflux_submit_review",
        workflow_name="LifecycleReviewWorkflow",
        workflow_id="lifecycle-review-1",
        metadata={
            "typeflux": {
                "level": "lifecycle_operation",
                "lifecycle_operation": {
                    "operation_type": "signal",
                    "operation_name": "typeflux_submit_review",
                    "workflow_id": "lifecycle-review-1",
                    "review_user_decision": "send_email",
                    "review_route_target": "send_email",
                },
            }
        },
    ):
        pass

    assert client.start_calls[0]["name"] == "TypefluxLifecycleSignal:typeflux_submit_review"
    operation = client.start_calls[0]["metadata"]["typeflux"]["lifecycle_operation"]
    assert operation["review_user_decision"] == "send_email"
    assert operation["review_route_target"] == "send_email"
    assert "reviewer" not in operation
    assert "notes" not in operation


def test_langfuse_writer_flush_does_not_shutdown_tracer_provider() -> None:
    client = _FlushableClient()
    tracer_provider = _FakeTracerProvider()
    writer = LangfuseTraceWriter(client=client, redactor=NoOpRedactor())
    writer._tracer_provider = tracer_provider

    writer.flush()

    assert client.flush_count == 1
    assert tracer_provider.force_flush_count == 0
    assert tracer_provider.shutdown_count == 0


def test_langfuse_writer_shutdown_flushes_client_and_tracer_provider() -> None:
    client = _FlushableClient()
    tracer_provider = _FakeTracerProvider()
    writer = LangfuseTraceWriter(client=client, redactor=NoOpRedactor())
    writer._tracer_provider = tracer_provider

    writer.shutdown()

    assert client.flush_count == 1
    assert tracer_provider.force_flush_count == 1
    assert tracer_provider.shutdown_count == 1


def test_langfuse_writer_shutdown_without_resources_is_safe() -> None:
    writer = LangfuseTraceWriter(redactor=NoOpRedactor())

    writer.shutdown()


def test_manifest_views_preserve_manifest_identity_fields() -> None:
    trace = _trace(
        "trace-view",
        workflow_name="SupportWorkflow",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        input_hash="input-hash",
        output_hash="output-hash",
        model="gpt-4o-mini",
    )
    workflow = trace.metadata["typeflux"]["execution_manifest"]
    workflow["map_steps"] = [
        {
            "map_step_id": "review_evidence",
            "activity_name": "review_evidence_item",
            "over": "input.evidence",
            "map_size": 4,
            "map_concurrency": 3,
        }
    ]
    workflow["activities"][0]["input_schema"]["module_status"] = "unstable"
    workflow["activities"][0]["input_schema"]["module_warning"] = (
        "schema module omitted because it is not a stable import path"
    )

    workflow_view = WorkflowExecutionManifestView.from_payload(workflow)
    activity_view = workflow_view.activities[0]

    assert workflow_view.workflow_name == "SupportWorkflow"
    assert workflow_view.workflow_contract_hash == workflow["workflow_contract_hash"]
    assert activity_view.activity_name == "classify_ticket"
    assert activity_view.prompt_ref.name == "support/classify"
    assert activity_view.input_schema.hash == "input-hash"
    assert activity_view.input_schema.module_status == "unstable"
    assert activity_view.input_schema.module_warning == (
        "schema module omitted because it is not a stable import path"
    )
    assert activity_view.input_schema.to_public_dict()["module_status"] == "unstable"
    assert activity_view.output_schema.hash == "output-hash"
    assert activity_view.provider_model == "gpt-4o-mini"
    assert workflow_view.map_steps == (
        {
            "map_step_id": "review_evidence",
            "activity_name": "review_evidence_item",
            "over": "input.evidence",
            "map_size": 4,
            "map_concurrency": 3,
        },
    )
    assert workflow_view.to_public_dict()["map_steps"] == [
        {
            "map_step_id": "review_evidence",
            "activity_name": "review_evidence_item",
            "over": "input.evidence",
            "map_size": 4,
            "map_concurrency": 3,
        }
    ]


def test_manifest_view_rejects_incomplete_activity_payload() -> None:
    try:
        ActivityExecutionManifestView.from_payload({"activity_name": "missing"})
    except Exception as exc:
        assert "input_schema" in str(exc)
    else:  # pragma: no cover - defensive assertion.
        raise AssertionError("expected incomplete manifest payload to fail validation")


def test_trace_public_serialization_excludes_raw_and_serializes_payloads() -> None:
    raw = object()
    trace = TraceRecord(
        trace_id="trace-serialize",
        timestamp=datetime(2026, 5, 15, tzinfo=UTC),
        input=PayloadModel(when=datetime(2026, 5, 15, 12, 0, tzinfo=UTC), text="hello"),
        observations=(
            ObservationRecord(
                observation_id="obs",
                start_time=datetime(2026, 5, 15, 12, 1, tzinfo=UTC),
                raw=raw,
            ),
        ),
        raw=raw,
    )

    payload = trace.to_public_dict()

    assert payload["timestamp"] == "2026-05-15T00:00:00+00:00"
    assert payload["input"]["when"] == "2026-05-15T12:00:00+00:00"
    assert payload["observations"][0]["start_time"] == "2026-05-15T12:01:00+00:00"
    assert "raw" not in payload
    assert "raw" not in payload["observations"][0]


def test_trace_inspection_summary_and_json_are_manifest_centered() -> None:
    trace = _trace(
        "trace-summary",
        workflow_name="SupportWorkflow",
        activity_name="classify_ticket",
        prompt_ref="support/classify",
        temporal_connection={
            "address": "namespace.tmprl.cloud:7233",
            "namespace": "namespace",
            "region": "us-east",
            "tls_enabled": True,
            "tls_mode": "boolean",
            "api_key_configured": True,
        },
        runtime_placement={
            "platform": "kubernetes",
            "kubernetes": {
                "namespace": "typeflux-smoke",
                "pod_name": "worker-abc123",
                "pod_uid": "pod-uid",
                "node_name": "minikube",
                "service_account": "default",
                "deployment_name": "typeflux-worker",
                "worker_name": "typeflux-worker",
            },
            "container_image": "typeflux-worker:k8s-smoke",
        },
        policy={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
            "policy_hash": "policy-hash",
            "enforcement_mode": "runtime",
            "admission_status": "passed",
        },
    )
    inspection = inspect_trace(InMemoryTraceStore([trace]), "trace-summary")

    summary = inspection.to_summary_dict()
    full = inspection.to_json_dict()
    manifest = inspection.to_manifest_dict()

    assert summary["workflow"]["workflow_name"] == "SupportWorkflow"
    assert "workflow_contract_hash" in summary["workflow"]
    assert summary["workflow"]["temporal_connection"] == {
        "address": "namespace.tmprl.cloud:7233",
        "namespace": "namespace",
        "region": "us-east",
        "tls_enabled": True,
        "tls_mode": "boolean",
        "api_key_configured": True,
    }
    assert summary["workflow"]["runtime_placement"] == {
        "platform": "kubernetes",
        "kubernetes": {
            "namespace": "typeflux-smoke",
            "pod_name": "worker-abc123",
            "pod_uid": "pod-uid",
            "node_name": "minikube",
            "service_account": "default",
            "deployment_name": "typeflux-worker",
            "worker_name": "typeflux-worker",
        },
        "container_image": "typeflux-worker:k8s-smoke",
    }
    assert summary["workflow"]["policy"] == {
        "version": "1",
        "selected_policy_ids": ["regulated"],
        "applied_policy_ids": ["base", "regulated"],
        "policy_names": ["base", "regulated"],
        "policy_hash": "policy-hash",
        "enforcement_mode": "runtime",
        "admission_status": "passed",
    }
    assert summary["activities"][0]["prompt_ref"] == "support/classify"
    assert summary["activities"][0]["definition_source"] == {"kind": "unknown"}
    assert summary["spans"]["activities"] == 1
    assert full["workflow_manifest"]["workflow_name"] == "SupportWorkflow"
    assert "workflow_contract_hash" in full["workflow_manifest"]
    assert full["workflow_manifest"]["temporal_connection"]["region"] == "us-east"
    assert full["workflow_manifest"]["policy"]["policy_hash"] == "policy-hash"
    assert full["runtime_placement"]["kubernetes"]["pod_name"] == "worker-abc123"
    assert full["activity_manifests"][0]["activity_name"] == "classify_ticket"
    assert full["activity_manifests"][0]["definition_source"] == {"kind": "unknown"}
    assert manifest["workflow"]["workflow_name"] == "SupportWorkflow"
    assert "workflow_contract_hash" in manifest["workflow"]
    assert manifest["workflow"]["temporal_connection"]["namespace"] == "namespace"
    assert manifest["workflow"]["policy"]["policy_names"] == ["base", "regulated"]
    assert "runtime_placement" not in manifest["workflow"]
    assert "runtime_placement" not in manifest["workflow"].get("contributions", {})
    assert manifest["activities"][0]["activity_name"] == "classify_ticket"


def test_cli_inspect_defaults_to_compact_summary(monkeypatch, capsys) -> None:
    store = InMemoryTraceStore([_trace("trace-cli", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(["trace", "inspect", "trace-cli", "--backend", "langfuse"])

    captured = capsys.readouterr()
    assert result == 0
    assert '"trace_id": "trace-cli"' in captured.out
    assert '"workflow": {' in captured.out
    assert '"trace": {' not in captured.out


def test_cli_inspect_json_and_export(monkeypatch, capsys) -> None:
    store = InMemoryTraceStore([_trace("trace-cli-json", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    inspect_result = cli.main(
        # #813: --full selects the full public JSON; --json is accepted as a no-op
        # (this command always prints JSON).
        ["trace", "inspect", "trace-cli-json", "--full", "--json", "--backend", "langfuse"]
    )
    inspect_output = capsys.readouterr().out
    export_result = cli.main(["trace", "export", "trace-cli-json", "--backend", "langfuse"])
    export_output = capsys.readouterr().out

    assert inspect_result == 0
    assert export_result == 0
    assert '"trace": {' in inspect_output
    assert '"workflow_manifest": {' in inspect_output
    assert '"workflow": {' in export_output
    assert '"trace": {' not in export_output


def test_cli_inspect_passes_max_detail_pages(monkeypatch, capsys) -> None:
    store = _RecordingDetailStore([_trace("trace-cli-detail", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        [
            "trace",
            "inspect",
            "trace-cli-detail",
            "--backend",
            "langfuse",
            "--max-detail-pages",
            "3",
        ]
    )

    assert result == 0
    assert store.get_trace_max_detail_pages == [3]
    assert '"trace_id": "trace-cli-detail"' in capsys.readouterr().out


def test_cli_search_defaults_to_table_and_json_is_available(monkeypatch, capsys) -> None:
    store = InMemoryTraceStore([_trace("trace-cli-search", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    table_result = cli.main(
        ["trace", "search", "--workflow-name", "SupportWorkflow", "--backend", "langfuse"]
    )
    table_output = capsys.readouterr().out
    json_result = cli.main(
        ["trace", "search", "--workflow-name", "SupportWorkflow", "--json", "--backend", "langfuse"]
    )
    json_output = capsys.readouterr().out

    assert table_result == 0
    assert json_result == 0
    assert "trace_id" in table_output
    assert "contract" in table_output
    assert "trace-cli-search" in table_output
    assert '"traces": [' in json_output


def test_cli_trace_list_passes_cursor_and_prints_table_hint(monkeypatch, capsys) -> None:
    store = _RecordingStore(
        [_trace("trace-cli-list-cursor", workflow_name="SupportWorkflow")],
        next_cursor="cursor-next",
    )
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        [
            "trace",
            "list",
            "--backend",
            "langfuse",
            "--since",
            "7d",
            "--cursor",
            "cursor-current",
            "--limit",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert store.list_queries[0].cursor == "cursor-current"
    assert "trace-cli-list-cursor" in captured.out
    assert "More results available; rerun with --cursor cursor-next" in captured.err


def test_cli_trace_list_json_includes_next_cursor(monkeypatch, capsys) -> None:
    store = _RecordingStore(
        [_trace("trace-cli-list-json", workflow_name="SupportWorkflow")],
        next_cursor="cursor-next",
    )
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        [
            "trace",
            "list",
            "--backend",
            "langfuse",
            "--since",
            "7d",
            "--limit",
            "1",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"next_cursor": "cursor-next"' in captured.out
    assert "More results available" not in captured.err


def test_cli_trace_search_passes_cursor_and_prints_table_hint(monkeypatch, capsys) -> None:
    store = _RecordingStore(
        [_trace("trace-cli-search-cursor", workflow_name="SupportWorkflow")],
        next_cursor="cursor-next",
    )
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        [
            "trace",
            "search",
            "--backend",
            "langfuse",
            "--since",
            "7d",
            "--workflow-name",
            "SupportWorkflow",
            "--workflow-contract-hash",
            "contract-hash",
            "--policy-id",
            "regulated",
            "--policy-name",
            "regulated",
            "--policy-hash",
            "policy-hash",
            "--cursor",
            "cursor-current",
            "--limit",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert store.queries[0].cursor == "cursor-current"
    assert store.queries[0].workflow_contract_hash == "contract-hash"
    assert store.queries[0].policy_id == "regulated"
    assert store.queries[0].policy_name == "regulated"
    assert store.queries[0].policy_hash == "policy-hash"
    assert "trace-cli-search-cursor" in captured.out
    assert "More results available; rerun with --cursor cursor-next" in captured.err


def test_cli_trace_search_json_includes_next_cursor(monkeypatch, capsys) -> None:
    store = _RecordingStore(
        [_trace("trace-cli-search-json", workflow_name="SupportWorkflow")],
        next_cursor="cursor-next",
    )
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        [
            "trace",
            "search",
            "--backend",
            "langfuse",
            "--since",
            "7d",
            "--workflow-name",
            "SupportWorkflow",
            "--limit",
            "1",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert '"next_cursor": "cursor-next"' in captured.out
    assert "More results available" not in captured.err


def test_cli_parses_relative_time_windows() -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

    assert cli._parse_datetime("15m", now=now) == now - timedelta(minutes=15)
    assert cli._parse_datetime("24h", now=now) == now - timedelta(hours=24)
    assert cli._parse_datetime("7d", now=now) == now - timedelta(days=7)
    assert cli._parse_datetime("90m", now=now) == now - timedelta(minutes=90)
    assert cli._parse_datetime("48h", now=now) == now - timedelta(hours=48)
    assert cli._parse_datetime("2026-05-27T12:00:00Z") == now


def test_cli_parses_naive_iso_datetime_as_utc() -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

    assert cli._parse_datetime("2026-05-27T12:00:00") == now
    assert cli._parse_datetime("2026-05-27T08:00:00-04:00") == now


@pytest.mark.parametrize("value", ["15min", "0h", "-1h", "1w", "soon"])
def test_cli_rejects_invalid_relative_time_windows(value: str, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(InMemoryTraceStore()))
    since_arg = f"--since={value}" if value.startswith("-") else "--since"
    argv = ["trace", "search", "--backend", "langfuse", since_arg]
    if since_arg == "--since":
        argv.append(value)

    with pytest.raises(SystemExit):
        cli.main(argv)

    assert "invalid datetime" in capsys.readouterr().err


def test_cli_langfuse_search_defaults_to_24h_since(monkeypatch, capsys) -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    store = _RecordingStore([_trace("trace-cli-search", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_utc_now", lambda: now)
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        ["trace", "search", "--workflow-name", "SupportWorkflow", "--backend", "langfuse"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert store.queries[0].since == now - timedelta(hours=24)
    assert "Using default --since 24h" in captured.err


def test_cli_langfuse_search_respects_explicit_relative_since(monkeypatch, capsys) -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    store = _RecordingStore([_trace("trace-cli-search", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_utc_now", lambda: now)
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(
        [
            "trace",
            "search",
            "--workflow-name",
            "SupportWorkflow",
            "--since",
            "7d",
            "--backend",
            "langfuse",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert store.queries[0].since == now - timedelta(days=7)
    assert "Using default --since" not in captured.err


def test_cli_langfuse_trace_list_defaults_to_24h_since(monkeypatch, capsys) -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    store = _RecordingStore([_trace("trace-cli-list", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_utc_now", lambda: now)
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    result = cli.main(["trace", "list", "--backend", "langfuse"])

    assert result == 0
    assert store.list_queries[0].since == now - timedelta(hours=24)
    assert "Using default --since" not in capsys.readouterr().err


def test_cli_search_backend_filter_inline_and_file(monkeypatch, capsys, tmp_path: Path) -> None:
    store = _RecordingStore([_trace("trace-cli-filter", workflow_name="SupportWorkflow")])
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))
    filter_payload = (
        '[{"type":"arrayOptions","column":"tags","operator":"all of","value":["typeflux"]}]'
    )

    inline_result = cli.main(
        ["trace", "search", "--backend-filter", filter_payload, "--json", "--backend", "langfuse"]
    )
    capsys.readouterr()
    filter_file = tmp_path / "filter.json"
    filter_file.write_text(filter_payload, encoding="utf-8")
    file_result = cli.main(
        [
            "trace",
            "search",
            "--backend-filter-file",
            str(filter_file),
            "--json",
            "--backend",
            "langfuse",
        ]
    )

    assert inline_result == 0
    assert file_result == 0
    assert store.queries[0].backend_filter == [
        {"type": "arrayOptions", "column": "tags", "operator": "all of", "value": ["typeflux"]}
    ]
    assert store.queries[1].backend_filter == store.queries[0].backend_filter


def test_cli_search_invalid_backend_filter_exits(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(InMemoryTraceStore()))

    with pytest.raises(SystemExit):
        cli.main(["trace", "search", "--backend-filter", "{nope", "--backend", "langfuse"])

    assert "invalid backend filter JSON" in capsys.readouterr().err


def test_cli_diff_defaults_to_summary_and_json_is_available(monkeypatch, capsys) -> None:
    store = InMemoryTraceStore(
        [
            _trace("left-cli", workflow_name="SupportWorkflow", prompt_version="v1"),
            _trace("right-cli", workflow_name="SupportWorkflow", prompt_version="v2"),
        ]
    )
    monkeypatch.setattr(cli, "_backend", lambda name: _Backend(store))

    summary_result = cli.main(["trace", "diff", "left-cli", "right-cli", "--backend", "langfuse"])
    summary_output = capsys.readouterr().out
    json_result = cli.main(
        # #813: --full selects the full public JSON; --json is accepted as a no-op.
        ["trace", "diff", "left-cli", "right-cli", "--full", "--json", "--backend", "langfuse"]
    )
    json_output = capsys.readouterr().out

    assert summary_result == 0
    assert json_result == 0
    assert '"workflow_changes":' in summary_output
    assert '"left": {' in json_output


def _workflow_invocation(
    workflow_id: str,
    run_id: str | None = None,
    *,
    rollup_sink: Any | None = None,
):
    observation = semantic_module._LangfuseWorkflowInvocation(
        client=_FakeObservationClient(),
        observation=_FakeObservation(),
        rollup_sink=rollup_sink,
    )
    observation._metadata = _workflow_metadata(workflow_id, run_id)
    return observation


def _workflow_metadata(workflow_id: str, run_id: str | None = None) -> dict[str, Any]:
    workflow = build_workflow_execution_manifest(
        workflow_name="SupportWorkflow",
        workflow_id=workflow_id,
        temporal_run_id=run_id,
        task_queue="support",
        activities=[],
        code_provenance=CodeProvenance(available=False, source="test"),
        sdk_version="test",
    ).to_dict()
    return {"typeflux": {"execution_manifest": workflow}}


def _activity_manifest(activity_name: str) -> AIActivityManifest:
    return AIActivityManifest(
        activity_name=activity_name,
        input_schema_name="ActivityInput",
        input_schema_hash="input-hash",
        output_schema_name="ActivityOutput",
        output_schema_hash="output-hash",
        prompt_ref=PromptRef("support/classify"),
        resolved_prompt_version="prompt-v1",
        provider_model="gpt-4o-mini",
        hook_name=None,
        manifest_hash=f"{activity_name}-manifest",
    )


def _activity_execution_manifest(activity_name: str) -> ActivityExecutionManifest:
    return ActivityExecutionManifest(
        activity_name=activity_name,
        activity_manifest_hash=f"{activity_name}-activity",
        definition_source=ActivityDefinitionSource(),
        input_schema_module=__name__,
        input_schema_name="ActivityInput",
        input_schema_hash="input-hash",
        output_schema_module=__name__,
        output_schema_name="ActivityOutput",
        output_schema_hash="output-hash",
        prompt_ref=PromptRef("support/classify"),
        resolved_prompt_version="prompt-v1",
        prompt_messages_hash="prompt-hash",
        rendered_messages_hash="rendered-hash",
        provider_model="gpt-4o-mini",
        temperature=None,
        hook_name=None,
        validation_attempt=1,
        manifest_hash=f"{activity_name}-execution",
    )


def _invocation_context(workflow_id: str, run_id: str) -> AIInvocationContext:
    return AIInvocationContext(
        temporal_namespace="default",
        temporal_workflow_type="SupportWorkflow",
        temporal_workflow_id=workflow_id,
        temporal_run_id=run_id,
        temporal_activity_type="classify_ticket",
        temporal_activity_id=f"{workflow_id}-activity",
        temporal_activity_attempt=1,
        typeflux_activity_name="classify_ticket",
        typeflux_manifest_hash="manifest-hash",
    )


def _observe_activity_from_thread(
    observer: LangfuseAIActivityObserver,
    activity: AIActivity,
    workflow_id: str,
    run_id: str,
    activity_name: str,
) -> None:
    with observer.observe_activity(
        activity=activity,
        input_value=ActivityInput(text=activity_name),
        manifest=_activity_manifest(activity_name),
        execution_manifest=_activity_execution_manifest(activity_name),
        invocation_context=_invocation_context(workflow_id, run_id),
    ):
        pass


def _workflow_activity_names(observation) -> list[str]:
    metadata = observation._metadata
    assert isinstance(metadata, dict)
    execution_manifest = metadata["typeflux"]["execution_manifest"]
    return [item["activity_name"] for item in execution_manifest["activities"]]


def _trace(
    trace_id: str,
    *,
    workflow_name: str,
    git_sha: str | None = None,
    activity_name: str = "classify_ticket",
    prompt_ref: str = "support/classify",
    input_hash: str = "input-hash",
    output_hash: str = "output-hash",
    model: str = "gpt-4o-mini",
    prompt_version: str = "prompt-v1",
    definition_source: dict | None = None,
    temporal_connection: dict | None = None,
    runtime_placement: dict | None = None,
    policy: dict | None = None,
    trace_policy: dict | None = None,
) -> TraceRecord:
    contributions = {}
    if temporal_connection:
        contributions["temporal_connection"] = temporal_connection
    if policy:
        contributions["policy"] = policy
    workflow = build_workflow_execution_manifest(
        workflow_name=workflow_name,
        workflow_id=f"{trace_id}-workflow",
        task_queue="support",
        activities=[
            {
                "activity_name": activity_name,
                "manifest_hash": f"{trace_id}-activity-exec",
                "activity_manifest_hash": f"{trace_id}-activity",
                "definition_source": definition_source or {"kind": "unknown"},
                "input_schema": {"name": "Input", "hash": input_hash},
                "output_schema": {"name": "Output", "hash": output_hash},
                "prompt_ref": {"name": prompt_ref},
                "resolved_prompt_version": prompt_version,
                "provider_model": model,
            }
        ],
        contributions=contributions or None,
    ).to_dict()
    if git_sha:
        workflow["code_provenance"]["git_sha"] = git_sha
    typeflux_metadata = {"execution_manifest": workflow}
    if runtime_placement:
        typeflux_metadata["runtime_placement"] = runtime_placement
    if trace_policy:
        typeflux_metadata["policy"] = trace_policy
    return TraceRecord(
        trace_id=trace_id,
        metadata={"typeflux": typeflux_metadata},
    )


def _provider_trace(
    trace_id: str,
    *,
    workflow_name: str,
    workflow_id: str,
    activity_name: str,
    prompt_ref: str,
    prompt_version: str,
    model: str,
    activity_hash: str,
    input_hash: str,
    output_hash: str,
) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id,
        observations=(
            ObservationRecord(
                name=f"{activity_name}.generation",
                type="generation",
                metadata={
                    "typeflux": {
                        "level": "generation",
                        "activity_name": activity_name,
                        "join": {
                            "activity_manifest_hash": activity_hash,
                            "activity_execution_manifest_hash": f"{activity_hash}-execution",
                        },
                        "temporal": {
                            "workflow_type": workflow_name,
                            "workflow_id": workflow_id,
                            "run_id": "run-1",
                        },
                        "activity": {
                            "activity_name": activity_name,
                            "activity_manifest_hash": activity_hash,
                            "input_schema": {"name": "Input", "hash": input_hash},
                            "output_schema": {"name": "Output", "hash": output_hash},
                            "prompt_ref": {"name": prompt_ref},
                            "resolved_prompt_version": prompt_version,
                            "provider_model": model,
                        },
                    }
                },
            ),
        ),
    )


def _high_fanout_trace_payload(trace_id: str, *, activity_count: int) -> dict[str, Any]:
    workflow = build_workflow_execution_manifest(
        workflow_name="FanoutWorkflow",
        workflow_id=f"{trace_id}-workflow",
        task_queue="fanout",
        activities=[],
    ).to_dict()
    return {
        "id": trace_id,
        "name": "TypefluxWorkflow:FanoutWorkflow",
        "metadata": {"typeflux": {"execution_manifest": workflow}},
        "observations": [
            {
                "observation_id": f"{trace_id}-activity-{index}",
                "name": f"review_page_{index}",
                "type": "SPAN",
                "metadata": {
                    "typeflux": {
                        "level": "activity",
                        "activity_execution_manifest": _activity_execution_manifest(
                            f"review_page_{index}"
                        ).to_dict(),
                    }
                },
            }
            for index in range(activity_count)
        ],
    }


class _FakeLangfuseClient:
    def __init__(self, trace_payload):
        self.api = _FakeLangfuseApi(trace_payload)


class _FakeLangfuseApi:
    def __init__(self, trace_payload):
        self.trace = _FakeTraceApi(trace_payload)
        self.observations = _FakeObservationsApi(trace_payload)


class _FakeTraceApi:
    def __init__(self, trace_payload):
        self._trace_payloads = trace_payload if isinstance(trace_payload, list) else [trace_payload]
        self.list_calls = []
        self.return_empty_for_tagged_search = False

    def get(self, trace_id: str):
        for payload in self._trace_payloads:
            if payload.get("id") == trace_id or payload.get("trace_id") == trace_id:
                return payload
        assert trace_id == "trace-1"
        return self._trace_payloads[0]

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if kwargs.get("tags") and self.return_empty_for_tagged_search:
            return {"data": []}
        return {"data": self._trace_payloads}


class _FakeObservationsApi:
    def __init__(self, trace_payload):
        trace_payloads = trace_payload if isinstance(trace_payload, list) else [trace_payload]
        self._rows = [
            row
            for payload in trace_payloads
            for row in _observation_rows_from_trace_payload(payload)
        ]
        self.get_many_calls = []

    def get_many(self, **kwargs):
        self.get_many_calls.append(kwargs)
        rows = list(self._rows)
        trace_id = kwargs.get("trace_id")
        if trace_id:
            rows = [row for row in rows if row.get("traceId") == trace_id]
        cursor = kwargs.get("cursor")
        start_index = _cursor_start_index(cursor)
        rows = rows[start_index:]
        limit = kwargs.get("limit") or len(rows)
        page_rows = rows[:limit]
        next_cursor = f"cursor-{start_index + limit + 1}" if len(rows) > limit else None
        meta = {"cursor": next_cursor}
        return {"data": page_rows, "meta": meta}


def _cursor_start_index(cursor: Any) -> int:
    if not isinstance(cursor, str) or not cursor.startswith("cursor-"):
        return 0
    try:
        return max(0, int(cursor.removeprefix("cursor-")) - 1)
    except ValueError:
        return 0


def _observation_rows_from_trace_payload(payload):
    trace_id = payload.get("trace_id") or payload.get("traceId") or payload.get("id")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    typeflux = metadata.get("typeflux") if isinstance(metadata.get("typeflux"), dict) else {}
    execution_manifest = (
        typeflux.get("execution_manifest")
        if isinstance(typeflux.get("execution_manifest"), dict)
        else {}
    )
    workflow_name = execution_manifest.get("workflow_name") or payload.get("name")
    rows = [
        {
            "id": f"{trace_id}-root",
            "traceId": trace_id,
            "name": payload.get("name")
            or (f"TypefluxWorkflow:{workflow_name}" if workflow_name else None),
            "type": "SPAN",
            "level": "DEFAULT",
            "startTime": payload.get("timestamp") or "2026-05-27T12:00:00+00:00",
            "endTime": payload.get("timestamp") or "2026-05-27T12:00:01+00:00",
            "input": payload.get("input"),
            "output": payload.get("output"),
            "metadata": metadata,
            "tags": metadata.get("tags"),
            "traceName": payload.get("name")
            or (f"TypefluxWorkflow:{workflow_name}" if workflow_name else None),
        }
    ]
    for index, observation in enumerate(payload.get("observations") or ()):
        observation_metadata = (
            observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
        )
        rows.append(
            {
                "id": observation.get("observation_id")
                or observation.get("id")
                or f"{trace_id}-obs-{index}",
                "traceId": trace_id,
                "name": observation.get("name"),
                "type": observation.get("type") or "SPAN",
                "level": observation.get("level") or "DEFAULT",
                "startTime": observation.get("start_time") or "2026-05-27T12:00:00+00:00",
                "endTime": observation.get("end_time") or "2026-05-27T12:00:01+00:00",
                "input": observation.get("input"),
                "output": observation.get("output"),
                "usage": observation.get("usage"),
                "usageDetails": observation.get("usageDetails"),
                "metadata": observation_metadata,
                "tags": metadata.get("tags"),
                "traceName": payload.get("name")
                or (f"TypefluxWorkflow:{workflow_name}" if workflow_name else None),
            }
        )
    return rows


class _Backend:
    def __init__(self, reader):
        self.reader = reader


class _RecordingStore(InMemoryTraceStore):
    def __init__(self, traces=(), *, next_cursor: str | None = None) -> None:
        super().__init__(traces)
        self.next_cursor = next_cursor
        self.queries = []
        self.list_queries = []

    def list_traces(self, query: TraceListQuery):
        self.list_queries.append(query)
        page = super().list_traces(query)
        return page.model_copy(update={"next_cursor": self.next_cursor})

    def search_traces(self, query: TraceSearchQuery):
        self.queries.append(query)
        return TracePage(traces=self._traces[: query.limit], next_cursor=self.next_cursor)


class _RecordingDetailStore(InMemoryTraceStore):
    def __init__(self, traces=()) -> None:
        super().__init__(traces)
        self.get_trace_max_detail_pages = []

    def get_trace(
        self,
        trace_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        max_detail_pages: int | None = None,
    ):
        self.get_trace_max_detail_pages.append(max_detail_pages)
        return super().get_trace(
            trace_id,
            since=since,
            until=until,
            max_detail_pages=max_detail_pages,
        )


class _FakeObservationClient:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.trace_io_calls: list[dict[str, Any]] = []
        self.observations: list[_FakeObservation] = []

    def start_as_current_observation(self, **kwargs):
        self.start_calls.append(kwargs)
        observation = _FakeObservation()
        self.observations.append(observation)
        return _FakeObservationContext(observation)

    def set_current_trace_io(self, **kwargs):
        self.trace_io_calls.append(kwargs)

    def flush(self) -> None:
        return None


class _FakeObservationContext:
    def __init__(self, observation: _FakeObservation) -> None:
        self._observation = observation

    def __enter__(self):
        return self._observation

    def __exit__(self, exc_type, exc, traceback):
        return False


class _FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.child_start_calls: list[dict[str, Any]] = []
        self.children: list[_FakeObservation] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    def start_as_current_observation(self, **kwargs):
        self.child_start_calls.append(kwargs)
        observation = _FakeObservation()
        self.children.append(observation)
        return _FakeObservationContext(observation)


def _contains_identity(value: Any, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(_contains_identity(item, target) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identity(item, target) for item in value)
    return False


class _FlushableClient:
    def __init__(self) -> None:
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1


class _FakeTracerProvider:
    def __init__(self) -> None:
        self.force_flush_count = 0
        self.shutdown_count = 0

    def force_flush(self) -> None:
        self.force_flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1
