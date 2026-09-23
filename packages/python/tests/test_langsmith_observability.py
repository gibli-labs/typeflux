from __future__ import annotations

import json
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from typeflux.core.contracts import (
    ActivityDefinitionSource,
    AIActivity,
    ChatMessage,
    PromptRef,
)
from typeflux.execution.starter import workflow_invocation_metadata
from typeflux.manifests import (
    ActivityExecutionManifest,
    AIActivityManifest,
    AIInvocationContext,
    CodeProvenance,
    build_workflow_execution_manifest,
)
from typeflux.metadata import YamlWorkflowContributor
from typeflux.observability.inspect import inspect_trace
from typeflux.observability.langsmith import (
    LangSmithObservabilityBackend,
    LangSmithOtelProfile,
    LangSmithTraceReader,
)
from typeflux.observability.otel import (
    _PROMOTED_SCALARS,
    OtelTraceWriter,
    _promoted_scalars,
)
from typeflux.observability.redaction import NoOpRedactor
from typeflux.observability.semantic import semantic_metadata
from typeflux.providers import ProviderUsage


class ActivityInput(BaseModel):
    text: str


class ActivityOutput(BaseModel):
    text: str


def _workflow_metadata() -> dict[str, Any]:
    workflow = build_workflow_execution_manifest(
        workflow_name="SupportWorkflow",
        workflow_id="wf-1",
        temporal_run_id="run-1",
        task_queue="support",
        activities=[],
        code_provenance=CodeProvenance(available=False, source="test"),
        sdk_version="test",
    ).to_dict()
    return {"typeflux": {"level": "workflow", "execution_manifest": workflow}}


def _activity_manifest() -> AIActivityManifest:
    return AIActivityManifest(
        activity_name="classify_ticket",
        input_schema_name="ActivityInput",
        input_schema_hash="input-hash",
        output_schema_name="ActivityOutput",
        output_schema_hash="output-hash",
        prompt_ref=PromptRef("support/classify"),
        resolved_prompt_version="prompt-v1",
        provider_model="gpt-4o-mini",
        hook_name=None,
        manifest_hash="classify-manifest",
    )


def _activity_execution_manifest() -> ActivityExecutionManifest:
    return ActivityExecutionManifest(
        activity_name="classify_ticket",
        activity_manifest_hash="classify-activity",
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
        manifest_hash="classify-execution",
    )


def _invocation_context() -> AIInvocationContext:
    return AIInvocationContext(
        temporal_namespace="default",
        temporal_workflow_type="SupportWorkflow",
        temporal_workflow_id="wf-1",
        temporal_run_id="run-1",
        temporal_activity_type="classify_ticket",
        temporal_activity_id="wf-1-activity",
        temporal_activity_attempt=1,
        typeflux_activity_name="classify_ticket",
        typeflux_manifest_hash="manifest-hash",
    )


def _writer_with_memory_exporter() -> tuple[OtelTraceWriter, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    writer = OtelTraceWriter(
        profile=LangSmithOtelProfile(api_key="k", project="p"), redactor=NoOpRedactor()
    )
    # Inject the in-memory tracer instead of building a real LangSmith exporter.
    writer._tracer = provider.get_tracer("test")
    writer._tracer_provider = provider
    return writer, exporter


def _drive_one_workflow(writer: OtelTraceWriter) -> None:
    activity = AIActivity(
        name="classify_ticket",
        input_type=ActivityInput,
        output_type=ActivityOutput,
        prompt_ref=PromptRef("support/classify"),
    )
    with writer.observe_workflow_invocation(
        workflow_name="SupportWorkflow",
        input_value=ActivityInput(text="hello"),
        metadata=_workflow_metadata(),
        tags=["typeflux", "typeflux.workflow:SupportWorkflow"],
    ) as workflow:
        observer = writer.create_activity_observer()
        with observer.observe_activity(
            activity=activity,
            input_value=ActivityInput(text="hello"),
            manifest=_activity_manifest(),
            execution_manifest=_activity_execution_manifest(),
            invocation_context=_invocation_context(),
        ) as activity_observation:
            with activity_observation.observe_generation(
                messages=[ChatMessage(role="user", content="hello")],
                output_schema=ActivityOutput,
                metadata={},
                validation_attempt=1,
                model="gpt-4o-mini",
                temperature=0.0,
            ) as generation:
                generation.update_output(ActivityOutput(text="billing"))
                generation.update_usage(
                    ProviderUsage(model="gpt-4o-mini", input_tokens=11, output_tokens=7)
                )
            activity_observation.update_output(ActivityOutput(text="billing"))
        workflow.update_output({"category": "billing"})


def test_langsmith_span_processor_satisfies_sdk_interface() -> None:
    # opentelemetry-sdk >=1.40 calls _on_ending on every registered span
    # processor at span end; langsmith's OtelSpanProcessor predates that hook and
    # is not a SpanProcessor subclass, so without the compat mixin every traced
    # workflow crashes at span end (#349). Guard the contract: the built
    # processor must be a SpanProcessor and its _on_ending must be a safe no-op.
    import pytest

    pytest.importorskip("langsmith.integrations.otel")
    from opentelemetry.sdk.trace import SpanProcessor

    processor = LangSmithOtelProfile(api_key="k", project="p").build_span_processor()

    assert isinstance(processor, SpanProcessor)
    processor._on_ending(None)  # the regressed hook — must not raise


def test_writer_emits_langsmith_span_conventions() -> None:
    writer, exporter = _writer_with_memory_exporter()
    _drive_one_workflow(writer)

    spans = {span.name: _attrs(span) for span in exporter.get_finished_spans()}
    workflow = spans["TypefluxWorkflow:SupportWorkflow"]
    assert workflow["langsmith.span.kind"] == "chain"
    assert workflow["langsmith.trace.name"] == "TypefluxWorkflow:SupportWorkflow"
    assert "typeflux" in workflow["langsmith.span.tags"]
    typeflux = json.loads(workflow["langsmith.metadata.typeflux"])
    assert typeflux["level"] == "workflow"
    assert "execution_manifest" in typeflux

    # High-value scalars are promoted to first-class flat metadata keys (so they
    # are searchable / render as fields in the UI), alongside the full blob.
    assert workflow["langsmith.metadata.workflow_name"] == "SupportWorkflow"
    assert workflow["langsmith.metadata.workflow_id"] == "wf-1"

    activity = spans["classify_ticket"]
    assert activity["langsmith.span.kind"] == "chain"
    assert activity["langsmith.metadata.provider_model"] == "gpt-4o-mini"
    assert activity["langsmith.metadata.prompt_ref"] == "support/classify"
    assert activity["langsmith.metadata.resolved_prompt_version"] == "prompt-v1"
    assert activity["langsmith.metadata.activity_name"] == "classify_ticket"

    generation = spans["classify_ticket.generation"]
    assert generation["langsmith.span.kind"] == "llm"
    assert generation["langsmith.metadata.model"] == "gpt-4o-mini"
    messages = json.loads(generation["input.value"])
    assert messages[0]["content"] == "hello"
    assert generation["gen_ai.usage.input_tokens"] == 11
    assert generation["gen_ai.usage.output_tokens"] == 7
    assert generation["gen_ai.usage.total_tokens"] == 18


def test_reader_decodes_only_the_typeflux_metadata_blob() -> None:
    # The writer JSON-encodes only the nested `typeflux` blob; promoted scalars
    # are plain strings and a user metadata value that merely *looks* like JSON
    # must round-trip verbatim — not be parsed into a dict/list (#330 item 3).
    from typeflux.observability.langsmith import _run_metadata

    run = {
        "extra": {
            "metadata": {
                "typeflux": '{"level": "workflow", "execution_manifest": {"workflow_id": "wf-1"}}',
                "workflow_id": "wf-1",
                "user_note": '{"ticket": "ABC-123"}',
                "user_list": "[1, 2, 3]",
            }
        }
    }

    decoded = _run_metadata(run)

    assert decoded["typeflux"] == {
        "level": "workflow",
        "execution_manifest": {"workflow_id": "wf-1"},
    }
    assert decoded["workflow_id"] == "wf-1"
    # The user-supplied JSON-looking strings are preserved verbatim.
    assert decoded["user_note"] == '{"ticket": "ABC-123"}'
    assert decoded["user_list"] == "[1, 2, 3]"


def test_every_promoted_scalar_resolves_against_a_real_manifest() -> None:
    # Drift guard (#330 item 4): each _PROMOTED_SCALARS entry hardcodes dotted
    # manifest paths. If a manifest/contributor field is renamed, the path stops
    # resolving and the searchable UI field silently vanishes with no error.
    # Assert every promoted scalar resolves against a real workflow- OR
    # activity-level metadata payload (each scalar belongs to one level).
    workflow_manifest = build_workflow_execution_manifest(
        workflow_name="SupportWorkflow",
        workflow_id="wf-1",
        temporal_run_id="run-1",
        task_queue="support",
        activities=[],
        code_provenance=CodeProvenance(
            available=True, source="git", git_sha="abc123", git_ref="main"
        ),
        sdk_version="test",
    )
    workflow_metadata = workflow_invocation_metadata(
        workflow_name="SupportWorkflow",
        workflow_id="wf-1",
        task_queue="support",
        temporal_run_id="run-1",
        execution_manifest=workflow_manifest,
        metadata_contributors=[
            YamlWorkflowContributor(
                name="support",
                project="examples.support",
                spec_digest="spec-digest-xyz",
                spec_digest_algorithm="sha256",
            )
        ],
    )
    activity_metadata = semantic_metadata(
        manifest=_activity_manifest(),
        activity_execution_manifest=_activity_execution_manifest(),
        invocation_context=_invocation_context(),
        level="generation",
    )
    workflow_promoted = _promoted_scalars(workflow_metadata)
    activity_promoted = _promoted_scalars(activity_metadata)

    for name, paths in _PROMOTED_SCALARS:
        assert name in workflow_promoted or name in activity_promoted, (
            f"promoted scalar {name!r} resolved at neither the workflow nor the activity "
            f"level against a real manifest; its _PROMOTED_SCALARS paths {paths} are stale"
        )


def _attrs(span: Any) -> dict[str, Any]:
    return dict(span.attributes or {})


def _run_from_span(span: Any) -> dict[str, Any]:
    """Mimic LangSmith's server-side OTEL-span -> run mapping for round-trip tests."""
    attributes = _attrs(span)
    metadata: dict[str, Any] = {}
    for key, value in attributes.items():
        if key.startswith("langsmith.metadata."):
            metadata[key[len("langsmith.metadata.") :]] = value
    inputs = {"value": attributes["input.value"]} if "input.value" in attributes else None
    outputs = {"value": attributes["output.value"]} if "output.value" in attributes else None
    return {
        "id": format(span.context.span_id, "016x"),
        "trace_id": format(span.context.trace_id, "032x"),
        "parent_run_id": (format(span.parent.span_id, "016x") if span.parent else None),
        "run_type": attributes.get("langsmith.span.kind"),
        "name": attributes.get("langsmith.trace.name") or span.name,
        "inputs": inputs,
        "outputs": outputs,
        "extra": {"metadata": metadata},
        "error": None,
        "start_time": None,
        "end_time": None,
    }


class _FakeLangSmithClient:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self._runs = runs

    def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]:
        trace_id = kwargs.get("trace_id")
        is_root = kwargs.get("is_root")
        runs = self._runs
        if trace_id is not None:
            runs = [run for run in runs if run["trace_id"] == trace_id]
        if is_root:
            runs = [run for run in runs if run["parent_run_id"] is None]
        return list(runs)


def test_writer_to_reader_round_trip_reconstructs_trace() -> None:
    writer, exporter = _writer_with_memory_exporter()
    _drive_one_workflow(writer)
    runs = [_run_from_span(span) for span in exporter.get_finished_spans()]

    reader = LangSmithTraceReader(client=_FakeLangSmithClient(runs), project="p")
    trace_id = runs[0]["trace_id"]
    trace = reader.get_trace(trace_id)

    # Root identified by the workflow-level marker; nested manifest survives the
    # JSON-attribute round trip the trace CLI relies on.
    assert trace.metadata["typeflux"]["level"] == "workflow"
    assert "execution_manifest" in trace.metadata["typeflux"]
    assert trace.retrieval is not None and trace.retrieval.backend == "langsmith"

    generation = next(obs for obs in trace.observations if obs.name == "classify_ticket.generation")
    assert generation.type == "llm"
    assert isinstance(generation.input, list)
    assert generation.input[0]["content"] == "hello"

    # The reconstructed trace drives the same inspect view the CLI prints.
    inspection = inspect_trace(reader, trace_id)
    assert inspection.trace.trace_id == trace_id
    assert inspection.workflow_manifest is not None
    assert inspection.workflow_manifest.workflow_name == "SupportWorkflow"


def test_reader_lists_root_traces() -> None:
    writer, exporter = _writer_with_memory_exporter()
    _drive_one_workflow(writer)
    runs = [_run_from_span(span) for span in exporter.get_finished_spans()]

    reader = LangSmithTraceReader(client=_FakeLangSmithClient(runs), project="p")
    from typeflux.observability.inspect import TraceListQuery

    page = reader.list_traces(TraceListQuery(limit=10))
    assert len(page.traces) == 1
    assert page.traces[0].metadata["typeflux"]["level"] == "workflow"


class _PagingFakeClient:
    """Fake LangSmith client that honors is_root / start_time / the trace-id
    `in(...)` filter and records every list_runs call, so pagination and the
    single-call batched hydration (#330 items 1-2) can be asserted."""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self._runs = runs
        self.calls: list[dict[str, Any]] = []

    def list_runs(self, **kwargs: Any):
        self.calls.append(kwargs)
        runs = self._runs
        if kwargs.get("is_root"):
            runs = [run for run in runs if run["parent_run_id"] is None]
        filter_expr = kwargs.get("filter")
        if filter_expr:
            import re

            ids = set(re.findall(r'"([^"]+)"', filter_expr))
            runs = [run for run in runs if run["trace_id"] in ids]
        # The real SDK yields lazily; mirror that so islice short-circuits.
        return iter(list(runs))

    @property
    def filter_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("filter")]


def _root_run(
    i: int, *, error: bool = False, workflow_name: str = "SupportWorkflow"
) -> dict[str, Any]:
    return {
        "id": f"r{i}",
        "trace_id": f"t{i}",
        "parent_run_id": None,
        "run_type": "chain",
        "name": f"TypefluxWorkflow:{workflow_name}",
        "inputs": {"value": f"in-{i}"},
        "outputs": None if error else {"ok": True},
        "error": "boom" if error else None,
        "extra": {
            "metadata": {
                "typeflux": json.dumps(
                    {
                        "level": "workflow",
                        "execution_manifest": {
                            "workflow_id": f"wf-{i}",
                            "workflow_name": workflow_name,
                        },
                    }
                ),
                "workflow_id": f"wf-{i}",
            }
        },
    }


def _child_run(i: int, *, error: bool = False) -> dict[str, Any]:
    return {
        "id": f"c{i}",
        "trace_id": f"t{i}",
        "parent_run_id": f"r{i}",
        "run_type": "llm",
        "name": "classify.generation",
        "inputs": None,
        "outputs": None,
        "error": "child-boom" if error else None,
        "extra": {"metadata": {"typeflux": '{"level": "generation", "activity_name": "classify"}'}},
    }


def test_list_traces_paginates_with_offset_cursor() -> None:
    from typeflux.observability.inspect import TraceListQuery

    runs = [_root_run(i) for i in range(5)]
    reader = LangSmithTraceReader(client=_PagingFakeClient(runs), project="p")

    page1 = reader.list_traces(TraceListQuery(limit=2))
    assert [t.trace_id for t in page1.traces] == ["t0", "t1"]
    assert page1.next_cursor == "2"

    page2 = reader.list_traces(TraceListQuery(limit=2, cursor=page1.next_cursor))
    assert [t.trace_id for t in page2.traces] == ["t2", "t3"]
    assert page2.next_cursor == "4"

    page3 = reader.list_traces(TraceListQuery(limit=2, cursor=page2.next_cursor))
    assert [t.trace_id for t in page3.traces] == ["t4"]
    assert page3.next_cursor is None  # last page, no more roots


def test_unfiltered_list_never_hydrates_children() -> None:
    from typeflux.observability.inspect import TraceListQuery

    runs = [_root_run(i) for i in range(3)] + [_child_run(i) for i in range(3)]
    client = _PagingFakeClient(runs)
    reader = LangSmithTraceReader(client=client, project="p")

    reader.list_traces(TraceListQuery(limit=10))

    # No filter needs child detail → root-only, zero per-trace hydration calls.
    assert client.filter_calls == []


def test_workflow_id_filter_hydrates_once_for_child_metadata_match() -> None:
    from typeflux.observability.inspect import TraceListQuery

    # workflow_id is matched with include_observation_metadata=True, so a
    # workflow_id present only on a child must still be found — which requires
    # hydration, done in ONE batched call for the page (not per-root N+1).
    runs = [_root_run(i) for i in range(3)] + [_child_run(i) for i in range(3)]
    client = _PagingFakeClient(runs)
    reader = LangSmithTraceReader(client=client, project="p")

    page = reader.list_traces(TraceListQuery(limit=10, workflow_id="wf-1"))

    assert [t.trace_id for t in page.traces] == ["t1"]
    assert len(client.filter_calls) == 1


def test_status_filter_hydrates_all_traces_in_a_single_call() -> None:
    from typeflux.observability.inspect import TraceListQuery

    # Root spans look OK; the error lives on a child — so status='error' must
    # hydrate, and it must do so for the whole page in ONE call (not N).
    runs: list[dict[str, Any]] = []
    for i in range(4):
        runs.append(_root_run(i))
        runs.append(_child_run(i, error=(i == 2)))
    client = _PagingFakeClient(runs)
    reader = LangSmithTraceReader(client=client, project="p")

    page = reader.list_traces(TraceListQuery(limit=10, status="error"))

    assert [t.trace_id for t in page.traces] == ["t2"]
    assert len(client.filter_calls) == 1  # batched hydration, not 4 separate calls


def test_decode_cursor_tolerates_garbage() -> None:
    from typeflux.observability.inspect import TraceListQuery

    runs = [_root_run(i) for i in range(3)]
    reader = LangSmithTraceReader(client=_PagingFakeClient(runs), project="p")

    # A malformed cursor falls back to offset 0 rather than crashing.
    page = reader.list_traces(TraceListQuery(limit=2, cursor="not-an-int"))
    assert [t.trace_id for t in page.traces] == ["t0", "t1"]


def test_search_traces_paginates_with_cursor() -> None:
    from typeflux.observability.inspect import TraceSearchQuery

    runs = [_root_run(i) for i in range(5)]
    reader = LangSmithTraceReader(client=_PagingFakeClient(runs), project="p")

    p1 = reader.search_traces(TraceSearchQuery(limit=2, scan_pages=1))
    assert [t.trace_id for t in p1.traces] == ["t0", "t1"]
    assert p1.next_cursor == "2"

    p2 = reader.search_traces(TraceSearchQuery(limit=2, scan_pages=1, cursor=p1.next_cursor))
    assert [t.trace_id for t in p2.traces] == ["t2", "t3"]
    assert p2.next_cursor == "4"

    p3 = reader.search_traces(TraceSearchQuery(limit=2, scan_pages=1, cursor=p2.next_cursor))
    assert [t.trace_id for t in p3.traces] == ["t4"]
    assert p3.next_cursor is None


def test_search_traces_walks_root_stream_once_across_scan_pages() -> None:
    from typeflux.observability.inspect import TraceSearchQuery

    runs = [_root_run(i) for i in range(5)]
    client = _PagingFakeClient(runs)
    reader = LangSmithTraceReader(client=client, project="p")

    # A workflow-level filter (no child hydration) scanning several pages must
    # open the root stream exactly once — not re-fetch it per scan page.
    page = reader.search_traces(TraceSearchQuery(limit=2, scan_pages=3, workflow_id="wf-4"))

    assert [t.trace_id for t in page.traces] == ["t4"]
    root_calls = [c for c in client.calls if c.get("is_root")]
    assert len(root_calls) == 1


def test_search_cursor_resumes_at_unprocessed_root_when_limit_fills_mid_page() -> None:
    from typeflux.observability.inspect import TraceSearchQuery

    runs = [
        _root_run(0, workflow_name="MatchWorkflow"),
        _root_run(1, workflow_name="OtherWorkflow"),
        _root_run(2, workflow_name="MatchWorkflow"),
        _root_run(3, workflow_name="MatchWorkflow"),
        _root_run(4, workflow_name="MatchWorkflow"),
    ]
    reader = LangSmithTraceReader(client=_PagingFakeClient(runs), project="p")

    page1 = reader.search_traces(
        TraceSearchQuery(limit=2, scan_pages=2, workflow_name="MatchWorkflow")
    )
    assert [t.trace_id for t in page1.traces] == ["t0", "t2"]
    assert page1.next_cursor == "3"

    page2 = reader.search_traces(
        TraceSearchQuery(
            limit=2,
            scan_pages=2,
            workflow_name="MatchWorkflow",
            cursor=page1.next_cursor,
        )
    )
    assert [t.trace_id for t in page2.traces] == ["t3", "t4"]
    assert page2.next_cursor is None


def test_root_falls_back_to_workflow_name_when_metadata_undecoded() -> None:
    # If the typeflux metadata blob fails to decode, the reader must still pick
    # the workflow span (by name) as the root, not an arbitrary child run.
    from typeflux.observability.langsmith import _trace_from_runs

    child = {
        "id": "c",
        "trace_id": "t",
        "parent_run_id": "r",
        "run_type": "llm",
        "name": "classify_ticket.generation",
        "inputs": None,
        "outputs": None,
        "extra": {"metadata": {}},
    }
    root = {
        "id": "r",
        "trace_id": "t",
        "parent_run_id": None,
        "run_type": "chain",
        "name": "TypefluxWorkflow:SupportWorkflow",
        "inputs": {"value": '{"text": "hi"}'},
        "outputs": None,
        "extra": {"metadata": {}},  # no decodable typeflux.level
    }

    trace = _trace_from_runs("t", [child, root])
    assert trace.name == "TypefluxWorkflow:SupportWorkflow"
    assert trace.input == {"text": "hi"}


def test_from_env_builds_otel_writer_and_reader() -> None:
    backend = LangSmithObservabilityBackend.from_env(redactor=NoOpRedactor())
    assert isinstance(backend.writer, OtelTraceWriter)
    assert isinstance(backend.reader, LangSmithTraceReader)
    assert backend.writer.enabled is True
