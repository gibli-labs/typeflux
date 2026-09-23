"""Live, opt-in round-trip coverage for every observability backend (#339).

For each backend (Langfuse, LangSmith) this emits a workflow trace through the
*writer*, then polls the *reader* until the trace reconstructs — proving both the
export path and the CLI read path against the real backend, symmetrically.

Run with the relevant keys set::

    TYPEFLUX_RUN_LIVE=1 uv run pytest -m live -k live_observer -s
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from typeflux.env import load_env
from typeflux.manifests import CodeProvenance, build_workflow_execution_manifest
from typeflux.observability.backend import ObservabilityBackend
from typeflux.observability.inspect import TraceListQuery, TraceSummaryView

_POLL_TIMEOUT_SECONDS = 90
_POLL_INTERVAL_SECONDS = 3
_MAX_SCAN_PAGES = 10


def _find_probe(reader: Any, workflow_id: str) -> TraceSummaryView | None:
    # Query by workflow_id and follow next_cursor across pages so the probe is
    # found even when it isn't on the first page. Langfuse paginates; the
    # LangSmith reader ignores the cursor today (#330), so for LangSmith this is
    # effectively first-page-only and relies on the dedicated, quiet project set
    # below keeping the probe in the recent window.
    cursor: str | None = None
    for _ in range(_MAX_SCAN_PAGES):
        page = reader.list_traces(TraceListQuery(workflow_id=workflow_id, limit=25, cursor=cursor))
        for trace in page.traces:
            candidate = TraceSummaryView.from_trace(trace)
            if candidate.workflow_id == workflow_id:
                return candidate
        if not page.next_cursor:
            return None
        cursor = page.next_cursor
    return None


def _langfuse_backend() -> ObservabilityBackend:
    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    return LangfuseObservabilityBackend.from_env()


def _langsmith_backend() -> ObservabilityBackend:
    from typeflux.observability.langsmith import LangSmithObservabilityBackend

    return LangSmithObservabilityBackend.from_env()


# (backend id, required env keys, builder)
_BACKENDS: list[tuple[str, tuple[str, ...], Callable[[], ObservabilityBackend]]] = [
    ("langfuse", ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"), _langfuse_backend),
    ("langsmith", ("LANGSMITH_API_KEY",), _langsmith_backend),
]


def _workflow_metadata(workflow_id: str) -> dict[str, Any]:
    manifest = build_workflow_execution_manifest(
        workflow_name="LiveObserverProbe",
        workflow_id=workflow_id,
        temporal_run_id=None,
        task_queue="live-observer-probe",
        activities=[],
        code_provenance=CodeProvenance(available=False, source="test"),
        sdk_version="test",
    ).to_dict()
    return {"typeflux": {"level": "workflow", "execution_manifest": manifest}}


@pytest.mark.live
@pytest.mark.parametrize("backend_id, key_envs, build", _BACKENDS, ids=[b[0] for b in _BACKENDS])
def test_live_observer_round_trip(
    backend_id: str,
    key_envs: tuple[str, ...],
    build: Callable[[], ObservabilityBackend],
    request: pytest.FixtureRequest,
) -> None:
    load_env()
    if request.config.option.markexpr != "live":
        pytest.skip("live tests run only when explicitly selected with -m live")
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1":
        pytest.skip("TYPEFLUX_RUN_LIVE must be 1")
    missing = [name for name in key_envs if not os.getenv(name)]
    if missing:
        pytest.skip(f"missing env: {', '.join(missing)}")
    # Pin a known project for the LangSmith reader/writer round trip.
    if backend_id == "langsmith":
        os.environ.setdefault("LANGSMITH_PROJECT", "typeflux-live-observer")

    backend = build()
    workflow_id = f"live-observer-{backend_id}-{uuid4().hex[:12]}"

    # Emit a workflow trace through the writer.
    with backend.writer.observe_workflow_invocation(
        workflow_name="LiveObserverProbe",
        input_value={"probe": workflow_id},
        metadata=_workflow_metadata(workflow_id),
        tags=["typeflux", "live-observer-probe"],
    ) as observation:
        observation.update_output({"ok": True, "probe": workflow_id})
    backend.writer.flush()
    backend.writer.shutdown()

    # Poll the reader until the backend has ingested and we can reconstruct it.
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    summary: TraceSummaryView | None = None
    while time.monotonic() < deadline:
        summary = _find_probe(backend.reader, workflow_id)
        if summary is not None:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    assert summary is not None, (
        f"{backend_id}: trace for {workflow_id} not found within {_POLL_TIMEOUT_SECONDS}s"
    )
    assert summary.workflow_name == "LiveObserverProbe"
    print(f"\n[{backend_id}] reconstructed trace {summary.trace_id} workflow_id={workflow_id}")
