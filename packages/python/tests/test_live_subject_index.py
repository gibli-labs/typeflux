"""LIVE proof (#715 slice 1): the TypefluxSubjectIds keyword-list search attribute
end-to-end on a real Temporal dev server.

- registers the custom ``TypefluxSubjectIds`` KeywordList attribute on the namespace
  (the deploy-time step the runbook documents), idempotently;
- runs a workflow whose ``subjects: [{ from: input.value }]`` block extracts the
  subject id at start;
- describes the execution and asserts ``TypefluxSubjectIds`` carries the subject;
- proves the enumeration seam: ``list_executions_for_subject`` finds the execution
  by subject via the same list_workflows visibility path.

Requires (gated by the ``live`` marker + ``TYPEFLUX_LIVE_TEMPORAL=1``):
- a local Temporal dev server on ``localhost:7233`` (``temporal server start-dev``).

Run: TYPEFLUX_LIVE_TEMPORAL=1 uv run --all-extras pytest -m live -k live_subject_index
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.live

LIVE = os.environ.get("TYPEFLUX_LIVE_TEMPORAL") == "1"
FIXTURES_DIR = Path(__file__).resolve().parent / "replay_fixtures"


async def _ensure_subject_attribute_registered(client) -> None:
    """Register TypefluxSubjectIds as a KeywordList on the namespace (idempotent)."""

    from temporalio.api.enums.v1 import IndexedValueType
    from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest

    from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE

    namespace = client.namespace
    request = AddSearchAttributesRequest(
        namespace=namespace,
        search_attributes={
            SUBJECT_IDS_SEARCH_ATTRIBUTE: IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD_LIST
        },
    )
    try:
        await client.operator_service.add_search_attributes(request)
    except Exception as exc:  # noqa: BLE001 - already-exists is the happy path on reruns
        if "already" not in str(exc).lower():
            raise


@pytest.mark.asyncio
async def test_live_subject_index_stamps_and_enumerates(monkeypatch: pytest.MonkeyPatch) -> None:
    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel

    from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE
    from typeflux.project.runs import list_executions_for_subject
    from typeflux.yaml import build_runtime, load_yaml_spec

    subject_id = f"subject-{uuid4().hex[:8]}"
    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "subjects.yaml"))
    await _ensure_subject_attribute_registered(runtime.client)

    workflow_id = f"live-subject-{uuid4().hex[:8]}"
    async with runtime.worker.build_worker():
        result = await runtime.execute_workflow(
            InputModel(value=subject_id), id=workflow_id, result_type=OutputModel
        )
        assert result == OutputModel(value="replay-fixture")

        # 1) STAMPED: the execution's TypefluxSubjectIds carries the extracted subject.
        from temporalio.common import SearchAttributeKey

        description = await runtime.client.get_workflow_handle(workflow_id).describe()
        stamped = description.typed_search_attributes.get(
            SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE)
        )
        assert stamped == [subject_id]

    # 2) ENUMERATED: the seam finds the execution by subject (list_workflows path).
    #    Visibility is eventually consistent — poll briefly.
    import asyncio

    found: tuple = ()
    for _ in range(20):
        found = (await list_executions_for_subject(runtime.client, subject_id)).executions
        if found:
            break
        await asyncio.sleep(0.5)
    assert [ref.execution_id for ref in found] == [workflow_id]
    # Slice 4: the enumeration also carries the row's own subject set + enum-derived
    # status flags (the delete driver's classification inputs).
    assert found[0].subject_ids is not None and subject_id in found[0].subject_ids


@pytest.mark.asyncio
async def test_live_subjects_reach_cache_records_through_real_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end cache-carrier proof (#715 review, finding 1): a
    subjects-declared workflow run through the REAL Temporal worker writes
    cross-run cache records whose ``subjects`` field carries the run's subject
    ids — interpreter (own search attribute) -> per-call context envelope ->
    invocation context -> ``build_cache_record``."""

    if not LIVE:
        pytest.skip("live Temporal dev-server test; set TYPEFLUX_LIVE_TEMPORAL=1 and run -m live")
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    for name in tuple(sys.modules):
        if name == "replay_demo_project" or name.startswith("replay_demo_project."):
            del sys.modules[name]

    from replay_demo_project.schemas import InputModel, OutputModel

    from typeflux.execution.cache import InMemoryCacheStore
    from typeflux.execution.worker import TypefluxWorker
    from typeflux.yaml import build_runtime, load_yaml_spec
    from typeflux.yaml.workflow import create_yaml_workflow_runner

    subject_id = f"subject-cache-{uuid4().hex[:8]}"
    runtime = await build_runtime(load_yaml_spec(FIXTURES_DIR / "subjects_cached.yaml"))
    await _ensure_subject_attribute_registered(runtime.client)

    written: list[dict] = []

    class RecordingStore(InMemoryCacheStore):
        def set(self, key, record) -> None:  # noqa: ANN001 - test double
            written.append(record)
            super().set(key, record)

    # The YAML runtime does not surface a cache-store knob (Python cross-run cache
    # is code-level, AIActivity.cache) — mirror runtime.worker's construction with
    # the store threaded in, so the workflow runs through the REAL worker path.
    worker = TypefluxWorker(
        client=runtime.client,
        task_queue=runtime.spec.task_queue,
        activities=tuple(runtime.activities.values()),
        registry=runtime.registry,
        provider=runtime.provider,
        artifact_policy=runtime.artifact_policy,
        observability=runtime.observability,
        workflows=[runtime.workflow_class],
        cache_store=RecordingStore(),
        workflow_runner=create_yaml_workflow_runner(),
    )
    workflow_id = f"live-subject-cache-{uuid4().hex[:8]}"
    async with worker.build_worker():
        result = await runtime.execute_workflow(
            InputModel(value=subject_id), id=workflow_id, result_type=OutputModel
        )
        assert result == OutputModel(value="replay-fixture")

    assert len(written) == 1
    assert written[0]["subjects"] == [subject_id]
