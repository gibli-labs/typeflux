"""Subject-identity plumbing unit tests (#715 slice 1).

Covers the edition-neutral pieces: declarative extraction semantics, the
explicit-override normalizer, the observer carrier (userId source + portable
tags), and the cache-record subjects carrier with its key-digest invariance.
The runtime search-attribute stamping + sub-workflow inheritance live in
test_yaml.py / test_yaml_subworkflows.py; the live namespace round-trip is in
test_live_subject_index.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import BaseModel

from typeflux.contracts.cache import CacheKey, cache_key_digest, cache_record
from typeflux.core.subjects import (
    SUBJECT_IDS_SEARCH_ATTRIBUTE,
    SubjectInput,
    normalize_subject_ids,
    resolve_subject_ids,
    subject_index_query,
    subject_trace_tags,
    subject_user_id,
)
from typeflux.execution.starter import execute_workflow


class _Input(BaseModel):
    patient_ref: str
    others: list[str] = []


def test_resolve_single_subject() -> None:
    ids = resolve_subject_ids(
        _Input(patient_ref="pt-1"), [SubjectInput(from_path="input.patient_ref")]
    )
    assert ids == ("pt-1",)


def test_resolve_multi_value_and_dedup_preserves_order() -> None:
    ids = resolve_subject_ids(
        _Input(patient_ref="pt-1", others=["pt-2", "pt-1", "pt-3"]),
        [SubjectInput(from_path="input.patient_ref"), SubjectInput(from_path="input.others")],
    )
    assert ids == ("pt-1", "pt-2", "pt-3")


def test_resolve_required_missing_raises() -> None:
    with pytest.raises(ValueError, match="resolved to no value"):
        resolve_subject_ids(_Input(patient_ref="pt-1"), [SubjectInput(from_path="input.absent")])


def test_resolve_required_empty_list_raises() -> None:
    # An empty list contributes ZERO ids — starting anyway would produce the
    # un-indexed execution `required` exists to prevent (#715 review, finding 2).
    with pytest.raises(ValueError, match="resolved to no value"):
        resolve_subject_ids(
            _Input(patient_ref="pt-1", others=[]),
            [SubjectInput(from_path="input.others")],
        )


def test_resolve_optional_empty_list_skips() -> None:
    ids = resolve_subject_ids(
        _Input(patient_ref="pt-1", others=[]),
        [SubjectInput(from_path="input.others", required=False)],
    )
    assert ids == ()


def test_resolve_required_whitespace_only_list_raises() -> None:
    with pytest.raises(ValueError, match="non-empty-string"):
        resolve_subject_ids(
            _Input(patient_ref="pt-1", others=["   "]),
            [SubjectInput(from_path="input.others")],
        )


def test_resolve_optional_missing_skips() -> None:
    ids = resolve_subject_ids(
        _Input(patient_ref="pt-1"),
        [SubjectInput(from_path="input.absent", required=False)],
    )
    assert ids == ()


def test_resolve_empty_string_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty-string"):
        resolve_subject_ids(
            _Input(patient_ref="   "), [SubjectInput(from_path="input.patient_ref")]
        )


def test_subject_input_rejects_non_input_path() -> None:
    with pytest.raises(ValueError, match="must start with 'input.'"):
        SubjectInput(from_path="context.value")


def test_normalize_explicit_override_validates_and_dedups() -> None:
    assert normalize_subject_ids(["a", "a", "b"]) == ("a", "b")
    with pytest.raises(ValueError, match="non-empty strings"):
        normalize_subject_ids(["a", ""])


def test_user_id_is_primary_subject() -> None:
    assert subject_user_id(["pt-1", "pt-2"]) == "pt-1"
    assert subject_user_id([]) is None


def test_trace_tags_one_per_subject() -> None:
    assert subject_trace_tags(["pt-1", "pt-2"]) == [
        "typeflux.subject:pt-1",
        "typeflux.subject:pt-2",
    ]


def test_index_query_escapes_and_matches_list_membership() -> None:
    assert subject_index_query("pt-1") == f"{SUBJECT_IDS_SEARCH_ATTRIBUTE} = 'pt-1'"
    # Single quotes escape by DOUBLING (Temporal visibility SQL convention).
    assert subject_index_query("o'brien") == f"{SUBJECT_IDS_SEARCH_ATTRIBUTE} = 'o''brien'"


# --- cache-record carrier -------------------------------------------------


def test_cache_record_carries_subjects_present_only() -> None:
    key = CacheKey(activity="a", input_hash="h", scope={"company_id": "co-1"})
    without = cache_record(key=key, output={"x": 1}, created_at="t", output_schema_hash="o")
    with_subjects = cache_record(
        key=key, output={"x": 1}, created_at="t", output_schema_hash="o", subjects=["pt-1", "pt-2"]
    )
    assert "subjects" not in without  # absent when empty
    assert with_subjects["subjects"] == ["pt-1", "pt-2"]


def test_cache_key_digest_unchanged_by_record_subjects() -> None:
    # The subjects live on the RECORD, not the key: adding them must NOT move the
    # cache-key digest (else it would over-partition and wreck the hit rate).
    key = CacheKey(activity="a", input_hash="h", scope={"company_id": "co-1"})
    baseline = cache_key_digest(key)
    cache_record(
        key=key, output={"x": 1}, created_at="t", output_schema_hash="o", subjects=["pt-1"]
    )
    assert cache_key_digest(key) == baseline


# --- observer carrier -----------------------------------------------------


class _Observation:
    def __init__(self) -> None:
        self.metadata: dict[str, Any] | None = None
        self.output: Any = None

    def update_output(self, output_value: Any) -> None:
        self.output = output_value

    def update_error(self, error: BaseException) -> None:  # pragma: no cover - not exercised
        pass

    def update_metadata(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata


class _CapturingWriter:
    """A construct-level TraceWriter: records the subject_ids + tags it receives
    from the workflow observation (no Langfuse, no network)."""

    enabled = True

    def __init__(self) -> None:
        self.observed: dict[str, Any] = {}
        self.observation = _Observation()

    @contextmanager
    def observe_workflow_invocation(
        self, *, workflow_name, input_value, metadata=None, tags=None, subject_ids=None
    ):
        self.observed = {
            "workflow_name": workflow_name,
            "tags": list(tags or []),
            "subject_ids": tuple(subject_ids or ()),
        }
        yield self.observation

    def flush(self) -> None:
        pass


class _Handle:
    def __init__(self, workflow_id: str) -> None:
        self.run_id = f"{workflow_id}-run"

    async def result(self) -> Any:
        return {"ok": True}


class _Client:
    def __init__(self) -> None:
        self.start_kwargs: dict[str, Any] | None = None

    async def start_workflow(self, workflow, input_value, **kwargs):
        self.start_kwargs = kwargs
        return _Handle(kwargs["id"])


@pytest.mark.asyncio
async def test_observer_receives_primary_user_id_and_subject_tags() -> None:
    writer = _CapturingWriter()
    client = _Client()
    await execute_workflow(
        client=client,
        workflow="DemoWorkflow.run",
        input_value={"value": "x"},
        id="wf-1",
        task_queue="tq",
        trace_writer=writer,
        workflow_name="DemoWorkflow",
        subject_ids=["pt-1", "pt-2"],
        include_execution_manifest=False,
    )
    # The observation received the full subject list; the primary (userId source)
    # is the first, and the portable tags carry every subject.
    assert writer.observed["subject_ids"] == ("pt-1", "pt-2")
    assert subject_user_id(writer.observed["subject_ids"]) == "pt-1"
    assert {"typeflux.subject:pt-1", "typeflux.subject:pt-2"} <= set(writer.observed["tags"])


# --- the worker-path cache carrier (#715 review, finding 1) ---------------


@pytest.mark.asyncio
async def test_worker_activity_path_writes_cache_record_with_subjects() -> None:
    """The REAL registered activity fn (the exact function the Temporal worker
    runs), driven inside an ActivityEnvironment with the interpreter's context
    envelope: boundary coercion -> _build_invocation_context -> executor ->
    build_cache_record must land the threaded subjects on the written record —
    the chain the review found dead."""

    from temporalio.testing import ActivityEnvironment

    from typeflux.core import AIActivity, PromptRef
    from typeflux.core.contracts import CacheConfig, MapActivityContext
    from typeflux.execution.cache import InMemoryCacheStore
    from typeflux.execution.worker import build_temporal_activity
    from typeflux.prompts import InlinePromptRegistry
    from typeflux.testing import FakeProvider

    class WriteInput(BaseModel):
        text: str

    class WriteOutput(BaseModel):
        text: str

    written: list[dict[str, Any]] = []

    class RecordingStore(InMemoryCacheStore):
        def set(self, key: Any, record: dict[str, Any]) -> None:
            written.append(record)
            super().set(key, record)

    activity = AIActivity(
        name="cached_step",
        input_type=WriteInput,
        output_type=WriteOutput,
        prompt_ref=PromptRef("p"),
        cache=CacheConfig(enabled=True),
    )
    fn = build_temporal_activity(
        activity,
        registry=InlinePromptRegistry({"p": "echo {{ text }}"}),
        provider=FakeProvider([WriteOutput(text="done"), WriteOutput(text="done")]),
        cache_store=RecordingStore(),
    )
    env = ActivityEnvironment()

    await env.run(fn, WriteInput(text="hello"), MapActivityContext(subject_ids=("pt-1", "pt-2")))
    assert len(written) == 1
    assert written[0]["subjects"] == ["pt-1", "pt-2"]

    # No envelope (a subject-free run) => the record stays subjects-less.
    written.clear()
    await env.run(fn, WriteInput(text="other"), None)
    assert len(written) == 1
    assert "subjects" not in written[0]
