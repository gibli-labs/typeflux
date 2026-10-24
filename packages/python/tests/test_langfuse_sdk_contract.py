"""Langfuse 5 wire contracts through the installed SDK, with no live services."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from langfuse import Langfuse

from typeflux.observability import TraceListQuery
from typeflux.observability.langfuse import LangfuseTraceReader


def observation(trace_id: str, *, subject: str = "s1", **extra: object) -> dict[str, object]:
    return {
        "id": "observation-1",
        "projectId": "project-test",
        "traceId": trace_id,
        "name": "TypefluxWorkflow:review",
        "type": "SPAN",
        "startTime": "2026-10-22T10:00:00Z",
        "tags": ["typeflux.subject:s1"],
        "userId": subject,
        "metadata": {"typeflux": {"level": "workflow"}},
        **extra,
    }


def test_sdk_v5_preserves_filters_cursors_and_json_io() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/public/v2/observations"
        query = request.url.params
        assert "typeflux" in query["expandMetadata"].split(",")
        if "traceId" in query:
            assert query["traceId"] == "trace-1"
            assert "filter" not in query
            assert "fromStartTime" not in query
            if "cursor" not in query:
                return httpx.Response(200, json={"data": [], "meta": {"cursor": "opaque+/="}})
            assert query["cursor"] == "opaque+/="
            return httpx.Response(
                200,
                json={
                    "data": [observation("trace-1", input='{"value":1}', output='{"ok":true}')],
                    "meta": {"cursor": None},
                },
            )
        filters = json.loads(query["filter"])
        assert any(f["column"] == "traceTags" for f in filters)
        assert sum(f["column"] == "startTime" for f in filters) == 2
        return httpx.Response(
            200, json={"data": [observation("trace-1")], "meta": {"cursor": None}}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Langfuse(
            public_key="pk-contract-read",
            secret_key="sk-test",
            base_url="http://localhost:9",
            httpx_client=http,
            tracing_enabled=False,
        )
        try:
            reader = LangfuseTraceReader(client=client)
            listed = reader._list_traces_page(
                TraceListQuery(since=datetime(2026, 10, 21, tzinfo=UTC)),
                server_tags=("typeflux.subject:s1",),
            )
            assert [trace.trace_id for trace in listed.traces] == ["trace-1"]
            trace = reader.get_trace("trace-1")
            assert trace.input == {"value": 1}
            assert trace.output == {"ok": True}
            assert trace.retrieval is not None and trace.retrieval.complete
            assert trace.retrieval.pages_read == 2
        finally:
            client.shutdown()
    assert len(requests) == 3


def test_sdk_v5_erasure_checks_unfiltered_native_user_ownership() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/public/v2/observations"
        query = request.url.params
        if "traceId" in query:
            assert "filter" not in query
            assert "fromStartTime" not in query
            rows = [observation("shared", subject="s2")]
        else:
            assert any(f["column"] in {"traceTags", "userId"} for f in json.loads(query["filter"]))
            rows = [observation("shared")]
        return httpx.Response(200, json={"data": rows, "meta": {"cursor": None}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Langfuse(
            public_key="pk-contract-erase",
            secret_key="sk-test",
            base_url="http://localhost:9",
            httpx_client=http,
            tracing_enabled=False,
        )
        try:
            report = LangfuseTraceReader(client=client).delete_traces_for_subject(
                "s1", dry_run=False
            )
            assert report.deleted_count == 0
            assert report.conflicted[0].other_subject_count == 1
        finally:
            client.shutdown()


def test_sdk_v5_deletes_only_after_complete_ownership_hydration() -> None:
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            assert request.url.path == "/api/public/traces"
            assert json.loads(request.content) == {"traceIds": ["owned"]}
            return httpx.Response(200, json={"message": "deleted"})
        assert request.url.path == "/api/public/v2/observations"
        return httpx.Response(200, json={"data": [observation("owned")], "meta": {"cursor": None}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Langfuse(
            public_key="pk-contract-delete",
            secret_key="sk-test",
            base_url="http://localhost:9",
            httpx_client=http,
            tracing_enabled=False,
        )
        try:
            report = LangfuseTraceReader(client=client).delete_traces_for_subject(
                "s1", dry_run=False
            )
            assert report.deleted_count == 1
        finally:
            client.shutdown()
    assert methods == ["GET", "GET", "GET", "DELETE"]


def test_sdk_v5_exports_redacted_root_observation_io() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from typeflux.observability.langfuse import LangfuseTraceWriter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    client = Langfuse(
        public_key="pk-contract-writer",
        secret_key="sk-test",
        base_url="http://localhost:9",
        tracer_provider=provider,
        span_exporter=exporter,
    )
    try:
        writer = LangfuseTraceWriter(client=client)
        with writer.observe_workflow_invocation(
            workflow_name="review", input_value={"email": "person@example.com"}
        ) as observation_handle:
            observation_handle.update_output({"email": "person@example.com", "ok": True})
        client.flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = dict(spans[0].attributes or {})
        assert spans[0].name == "TypefluxWorkflow:review"
        assert "person@example.com" not in json.dumps(attributes)
        assert "langfuse.observation.input" in attributes
        assert "langfuse.observation.output" in attributes
        assert json.loads(attributes["langfuse.observation.output"])["ok"] is True
    finally:
        client.shutdown()
        provider.shutdown()


def test_repeated_detail_cursor_never_looks_like_complete_ownership() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [observation("shared")], "meta": {"cursor": "same"}}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Langfuse(
            public_key="pk-contract-cursor",
            secret_key="sk-test",
            base_url="http://localhost:9",
            httpx_client=http,
            tracing_enabled=False,
        )
        try:
            with pytest.raises(RuntimeError, match="cursor repeated"):
                LangfuseTraceReader(client=client).get_trace("shared")
        finally:
            client.shutdown()


@pytest.mark.parametrize("dry_run", [True, False])
def test_missing_candidate_is_excluded_without_aborting_other_owned_traces(dry_run: bool) -> None:
    deleted: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            assert request.url.path == "/api/public/traces"
            deleted.extend(json.loads(request.content)["traceIds"])
            return httpx.Response(200, json={"message": "deleted"})
        trace_id = request.url.params.get("traceId")
        rows = (
            []
            if trace_id == "gone"
            else [observation("owned")]
            if trace_id == "owned"
            else [observation("gone"), observation("owned")]
        )
        return httpx.Response(200, json={"data": rows, "meta": {"cursor": None}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Langfuse(
            public_key=f"pk-contract-missing-{dry_run}",
            secret_key="sk-test",
            base_url="http://localhost:9",
            httpx_client=http,
            tracing_enabled=False,
        )
        try:
            report = LangfuseTraceReader(client=client).delete_traces_for_subject(
                "s1", dry_run=dry_run
            )
            assert report.trace_ids == ("owned",)
            assert [(item.trace_id, item.other_subject_count) for item in report.conflicted] == [
                ("gone", None)
            ]
            assert report.deleted_count == (0 if dry_run else 1)
            assert any("ownership unavailable" in warning for warning in report.warnings)
        finally:
            client.shutdown()
    assert deleted == ([] if dry_run else ["owned"])
