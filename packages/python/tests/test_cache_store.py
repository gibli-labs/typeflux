"""Tests for the cross-run CacheStore + cache policy (#398, #504)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from typeflux.contracts.cache import CacheKey
from typeflux.core import (
    AIActivity,
    CacheConfig,
    ChatMessage,
    PromptRef,
    ResolvedPrompt,
)
from typeflux.core.artifacts import (
    ArtifactAttachment,
    ArtifactGroupPart,
    ArtifactInput,
    ArtifactPolicy,
    TextPart,
)
from typeflux.execution.cache import (
    CACHE_ERASURE_COVERAGE_CAVEAT,
    InMemoryCacheStore,
    SubjectCacheErasureReport,
    SubjectErasableCacheStore,
    erase_subject_from_cache,
)
from typeflux.execution.executor import execute_ai_activity
from typeflux.execution.lifecycle import ActivityCancelled, ActivityLifecycle
from typeflux.prompts import InlinePromptRegistry
from typeflux.testing import FakeProvider


class _In(BaseModel):
    company_id: str
    subject: str


class _ClaimIn(BaseModel):
    claim_id: str
    documents: list[str]


class _Out(BaseModel):
    label: str


def _registry(model: str = "fake-model") -> InlinePromptRegistry:
    return InlinePromptRegistry(
        {
            "p/classify": ResolvedPrompt(
                ref=PromptRef("p/classify"),
                messages=(ChatMessage(role="user", content="Classify {{ subject }}"),),
                resolved_version="v1",
                model=model,
            )
        }
    )


def _activity(cache: CacheConfig | None = None) -> AIActivity:
    return AIActivity(
        name="classify",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("p/classify"),
        cache=cache if cache is not None else CacheConfig(),
    )


def _run(activity: AIActivity, provider: FakeProvider, store: object, tenant_resolver=None) -> _Out:
    return execute_ai_activity(
        activity=activity,
        input_value=_In(company_id="co-1", subject="Invoice"),
        registry=_registry(),
        provider=provider,
        cache_store=store,
        tenant_resolver=tenant_resolver,
    )


# --- in-memory store (unit) ---


def test_in_memory_store_roundtrip_and_idempotent_set() -> None:
    store = InMemoryCacheStore()
    key = CacheKey(activity="a", input_hash="h", scope={"company_id": "c"})
    assert store.get(key) is None

    store.set(key, {"key": key.to_dict(), "output": {"label": "v1"}})
    got = store.get(key)
    assert got is not None and got["output"] == {"label": "v1"}

    # idempotent upsert: same key overwrites in place
    store.set(key, {"key": key.to_dict(), "output": {"label": "v2"}})
    got = store.get(key)
    assert got is not None and got["output"] == {"label": "v2"}

    # different scope is a different slot
    assert store.get(CacheKey(activity="a", input_hash="h", scope={"company_id": "d"})) is None


# --- executor integration ---


def test_cache_miss_then_hit_skips_provider() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="billing")])  # a single response
    activity = _activity()

    first = _run(activity, provider, store)
    assert first.label == "billing"
    assert len(provider.calls) == 1

    # Second run is a cache hit: the provider is not called again (it has no
    # second response — a miss would raise StopIteration / exhaust it).
    second = _run(activity, provider, store)
    assert second.label == "billing"
    assert len(provider.calls) == 1


def test_artifact_bytes_swap_busts_cache(tmp_path: Path) -> None:
    """#504: replacing an artifact's bytes under the same path/group/prompt must
    MISS — the file's content is invisible to the rendered messages, so only the
    folded artifact identity (sha256) can distinguish the runs."""

    document = tmp_path / "claim-note.txt"
    document.write_text("original claim text", encoding="utf-8")
    activity = AIActivity(
        name="review_claim",
        input_type=_ClaimIn,
        output_type=_Out,
        prompt_ref=PromptRef("p/review"),
        cache=CacheConfig(),
        artifact_inputs=(
            ArtifactInput(
                name="claim_documents",
                from_path="input.documents",
                media_types=("text/plain",),
                attach=ArtifactAttachment(text="Use the attached claim documents."),
            ),
        ),
    )
    registry = InlinePromptRegistry(
        {
            "p/review": ResolvedPrompt(
                ref=PromptRef("p/review"),
                messages=(
                    ChatMessage(
                        role="user",
                        content=(
                            TextPart("Review claim {{ claim_id }}."),
                            ArtifactGroupPart(group="claim_documents", text="Evidence:"),
                        ),
                    ),
                ),
                resolved_version="v1",
                model="fake-model",
            )
        }
    )
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="first"), _Out(label="regenerated")])

    def run() -> _Out:
        return execute_ai_activity(
            activity=activity,
            input_value=_ClaimIn(claim_id="C-1", documents=[str(document)]),
            registry=registry,
            provider=provider,
            cache_store=store,
            artifact_policy=ArtifactPolicy(local_roots=(tmp_path,)),
        )

    assert run().label == "first"
    assert len(provider.calls) == 1
    # Same bytes → hit (the provider has no third response, so a miss would fail).
    assert run().label == "first"
    assert len(provider.calls) == 1

    document.write_text("REPLACED claim text", encoding="utf-8")
    assert run().label == "regenerated"
    assert len(provider.calls) == 2


def test_no_cache_policy_leaves_store_untouched() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="a"), _Out(label="b")])
    activity = AIActivity(
        name="classify",
        input_type=_In,
        output_type=_Out,
        prompt_ref=PromptRef("p/classify"),
    )

    _run(activity, provider, store)
    _run(activity, provider, store)
    assert len(provider.calls) == 2
    assert store._records == {}


def test_cache_disabled_skips_caching() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="a"), _Out(label="b")])
    activity = _activity(CacheConfig(enabled=False))

    _run(activity, provider, store)
    _run(activity, provider, store)
    assert len(provider.calls) == 2
    assert store._records == {}


def test_tenant_scope_partitions_cache() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="t1"), _Out(label="t2")])
    activity = _activity()

    r1 = _run(activity, provider, store, tenant_resolver=lambda i: {"company_id": "t1"})
    r2 = _run(activity, provider, store, tenant_resolver=lambda i: {"company_id": "t2"})
    assert (r1.label, r2.label) == ("t1", "t2")
    assert len(provider.calls) == 2  # distinct scope → both miss

    # Re-running tenant t1 hits its own partition, no new provider call.
    r3 = _run(activity, provider, store, tenant_resolver=lambda i: {"company_id": "t1"})
    assert r3.label == "t1"
    assert len(provider.calls) == 2


def test_read_bypass_env_skips_read_but_still_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="x"), _Out(label="y")])
    activity = _activity(CacheConfig(bypass_reads_env="TF_NO_CACHE"))

    first = _run(activity, provider, store)  # populates the cache with "x"
    assert (first.label, len(provider.calls)) == ("x", 1)

    # Bypass set: read is skipped (provider re-called → "y"), write still happens.
    monkeypatch.setenv("TF_NO_CACHE", "1")
    second = _run(activity, provider, store)
    assert (second.label, len(provider.calls)) == ("y", 2)

    # Bypass cleared: read works again and returns the freshly written "y".
    monkeypatch.delenv("TF_NO_CACHE")
    third = _run(activity, provider, store)
    assert (third.label, len(provider.calls)) == ("y", 2)


def test_stale_output_schema_hash_treated_as_miss() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="v1"), _Out(label="v2")])
    activity = _activity()

    first = _run(activity, provider, store)
    assert (first.label, len(provider.calls)) == ("v1", 1)

    # Simulate the activity's output schema evolving since the entry was written.
    for record in store._records.values():
        record["output_schema_hash"] = "stale"

    second = _run(activity, provider, store)
    assert (second.label, len(provider.calls)) == ("v2", 2)  # miss → regenerated


def test_model_change_busts_cache() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="m1"), _Out(label="m2")])
    activity = _activity()
    inp = _In(company_id="co-1", subject="Invoice")

    r1 = execute_ai_activity(
        activity=activity,
        input_value=inp,
        registry=_registry("fake-model"),
        provider=provider,
        cache_store=store,
    )
    # Same input + prompt, different resolved model → distinct key → miss.
    r2 = execute_ai_activity(
        activity=activity,
        input_value=inp,
        registry=_registry("fake-model-2"),
        provider=provider,
        cache_store=store,
    )
    assert (r1.label, r2.label) == ("m1", "m2")
    assert len(provider.calls) == 2


def test_cache_hit_honors_cancellation() -> None:
    store = InMemoryCacheStore()
    provider = FakeProvider([_Out(label="x")])
    activity = _activity()
    inp = _In(company_id="co-1", subject="Invoice")
    _run(activity, provider, store)  # populate the cache
    assert len(provider.calls) == 1

    class _CancelledLifecycle(ActivityLifecycle):
        heartbeat_interval_seconds = None

        def heartbeat(self) -> None: ...

        def raise_if_cancelled(self) -> None:
            raise ActivityCancelled("cancelled")

    # A cache hit must still observe cancellation before running hooks/moderation.
    with pytest.raises(ActivityCancelled):
        execute_ai_activity(
            activity=activity,
            input_value=inp,
            registry=_registry(),
            provider=provider,
            cache_store=store,
            lifecycle=_CancelledLifecycle(),
        )
    assert len(provider.calls) == 1  # raised on the hit path, no new provider call


# --- subject-scoped erasure (#715 slice 3) ---


def _record(key: CacheKey, label: str, subjects: list[str] | None = None) -> dict[str, object]:
    """A minimal cache record dict shaped like what ``build_cache_record`` writes."""
    record: dict[str, object] = {
        "key": key.to_dict(),
        "output": {"label": label},
        "created_at": "2026-01-01T00:00:00Z",
        "output_schema_hash": "h",
    }
    if subjects:
        record["subjects"] = list(subjects)
    return record


def test_in_memory_store_advertises_erasable_capability() -> None:
    store = InMemoryCacheStore()
    # runtime_checkable isinstance is the capability probe erase tooling uses.
    assert isinstance(store, SubjectErasableCacheStore)


def test_plain_store_not_supported_names_class_and_fallback() -> None:
    class _PlainStore:
        def __init__(self) -> None:
            self._records: dict[str, object] = {}

        def get(self, key: CacheKey) -> object | None:
            return self._records.get(key.digest())

        def set(self, key: CacheKey, record: dict[str, object]) -> None:
            self._records[key.digest()] = record

    store = _PlainStore()
    assert not isinstance(store, SubjectErasableCacheStore)

    report = erase_subject_from_cache(store, "subject-a", dry_run=True)
    assert report.supported is False
    assert "_PlainStore" in report.store_class
    assert report.keys_found == 0 and report.keys_deleted == 0
    assert report.full_flush_fallback is not None
    assert "_PlainStore" in report.full_flush_fallback
    assert "flush" in report.full_flush_fallback.lower()
    # The honest coverage caveat rides even the not-supported report.
    assert CACHE_ERASURE_COVERAGE_CAVEAT in report.warnings
    # to_dict is JSON-shaped and carries the fallback text.
    payload = report.to_dict()
    assert payload["supported"] is False
    assert payload["full_flush_fallback"] == report.full_flush_fallback


def test_index_maintained_on_set_and_erase_via_helper() -> None:
    store = InMemoryCacheStore()
    key = CacheKey(activity="a", input_hash="h1", scope={})
    store.set(key, _record(key, "v1", subjects=["subject-a"]))

    # The helper capability-detects the InMemory store and drives its erase.
    dry = erase_subject_from_cache(store, "subject-a", dry_run=True)
    assert dry.supported is True and dry.dry_run is True
    assert dry.keys_found == 1 and dry.keys_deleted == 0
    assert dry.key_digests == (key.digest(),)
    # Dry run mutated nothing.
    assert store.get(key) is not None

    done = erase_subject_from_cache(store, "subject-a", dry_run=False)
    assert done.keys_found == 1 and done.keys_deleted == 1
    assert store.get(key) is None
    # Index emptied — a repeat erase finds nothing.
    assert erase_subject_from_cache(store, "subject-a", dry_run=False).keys_found == 0


def test_subject_free_records_never_indexed_and_untouched() -> None:
    store = InMemoryCacheStore()
    subject_key = CacheKey(activity="a", input_hash="s", scope={})
    free_key = CacheKey(activity="a", input_hash="f", scope={})
    store.set(subject_key, _record(subject_key, "s", subjects=["subject-a"]))
    store.set(free_key, _record(free_key, "f"))  # no subjects → never indexed

    report = store.erase_subject("subject-a", dry_run=False)
    assert report.keys_deleted == 1
    assert store.get(subject_key) is None
    # The subject-free record carries no subject data → erasure leaves it intact.
    assert store.get(free_key) is not None


def test_overwrite_reindexes_and_drops_stale_subject() -> None:
    store = InMemoryCacheStore()
    key = CacheKey(activity="a", input_hash="h", scope={})
    store.set(key, _record(key, "v1", subjects=["subject-a"]))
    # Overwrite the SAME key under a different subject set.
    store.set(key, _record(key, "v2", subjects=["subject-b"]))

    # The stale subject-a index entry must be gone — erasing A finds nothing.
    assert store.erase_subject("subject-a", dry_run=True).keys_found == 0
    # subject-b now owns the key.
    b = store.erase_subject("subject-b", dry_run=False)
    assert b.keys_deleted == 1
    assert store.get(key) is None


def test_multi_subject_record_erasable_under_either_subject() -> None:
    store = InMemoryCacheStore()
    key = CacheKey(activity="a", input_hash="h", scope={})
    store.set(key, _record(key, "v", subjects=["subject-a", "subject-b"]))

    # Both subjects see the shared record in a dry run.
    assert store.erase_subject("subject-a", dry_run=True).keys_found == 1
    assert store.erase_subject("subject-b", dry_run=True).keys_found == 1

    # Erasing under EITHER deletes the shared entry...
    report = store.erase_subject("subject-a", dry_run=False)
    assert report.keys_deleted == 1
    assert store.get(key) is None
    # ...and the co-subject's index is pruned too (no dangling pointer).
    assert store.erase_subject("subject-b", dry_run=True).keys_found == 0


def test_erase_deletes_only_the_named_subjects_keys() -> None:
    store = InMemoryCacheStore()
    key_a = CacheKey(activity="a", input_hash="a", scope={})
    key_b = CacheKey(activity="a", input_hash="b", scope={})
    store.set(key_a, _record(key_a, "a", subjects=["subject-a"]))
    store.set(key_b, _record(key_b, "b", subjects=["subject-b"]))

    store.erase_subject("subject-a", dry_run=False)
    assert store.get(key_a) is None
    assert store.get(key_b) is not None  # other subject untouched


def test_coverage_caveat_always_present_on_supported_erase() -> None:
    store = InMemoryCacheStore()
    key = CacheKey(activity="a", input_hash="h", scope={})
    store.set(key, _record(key, "v", subjects=["subject-a"]))
    for dry_run in (True, False):
        report = store.erase_subject("subject-a", dry_run=dry_run)
        assert CACHE_ERASURE_COVERAGE_CAVEAT in report.warnings


def test_erase_atomic_wrt_concurrent_overwrite_of_same_key() -> None:
    """Regression (#715 slice 3 review): an ``erase_subject`` interleaved with a
    concurrent ``set`` overwriting one of ITS OWN digests must never pop the fresh
    record written for a DIFFERENT subject. The store's lock makes each operation
    atomic: the overwrite either lands before the erase (erase sees post-overwrite
    state) or after it (the erase completes first) — either way subject-new's record
    survives and the report reflects exactly what was deleted.

    The interleaving is forced deterministically: the erase thread is paused INSIDE
    its critical section (first ``_unindex`` call of the delete loop, after popping
    the first of subject-old's two records) while a writer thread attempts to
    overwrite the second key under subject-new. Without the lock, the writer would
    complete inside that window and the resumed erase would destroy subject-new's
    fresh record while reporting a clean two-key erase of subject-old.
    """

    import threading

    store = InMemoryCacheStore()
    key_1 = CacheKey(activity="a", input_hash="one", scope={})
    key_2 = CacheKey(activity="a", input_hash="two", scope={})
    store.set(key_1, _record(key_1, "old-1", subjects=["subject-old"]))
    store.set(key_2, _record(key_2, "old-2", subjects=["subject-old"]))

    # The erase loop visits digests in sorted order; the writer targets whichever
    # key sorts SECOND so its digest is still pending when the erase pauses.
    first_digest, second_digest = sorted([key_1.digest(), key_2.digest()])
    overwrite_key = key_2 if key_2.digest() == second_digest else key_1

    entered_erase = threading.Event()
    proceed = threading.Event()
    original_unindex = store._unindex

    def gated_unindex(digest: str, subjects: object) -> None:
        # One-shot gate on the erase loop's FIRST deletion (the writer's set()
        # never reaches _unindex for these digests while blocked on the lock).
        if digest == first_digest and not entered_erase.is_set():
            entered_erase.set()
            assert proceed.wait(timeout=5.0), "test gate timed out"
        original_unindex(digest, subjects)

    store._unindex = gated_unindex  # type: ignore[method-assign]

    report_box: list[object] = []

    def run_erase() -> None:
        report_box.append(store.erase_subject("subject-old", dry_run=False))

    def run_overwrite() -> None:
        store.set(overwrite_key, _record(overwrite_key, "fresh", subjects=["subject-new"]))

    eraser = threading.Thread(target=run_erase)
    eraser.start()
    assert entered_erase.wait(timeout=5.0), "erase never entered its critical section"

    writer = threading.Thread(target=run_overwrite)
    writer.start()
    # The erase holds the store lock while paused, so the writer MUST be blocked —
    # deterministic: it cannot acquire the lock until the erase releases it.
    writer.join(timeout=0.2)
    assert writer.is_alive(), "writer completed inside the erase's critical section"

    proceed.set()
    eraser.join(timeout=5.0)
    writer.join(timeout=5.0)
    assert not eraser.is_alive() and not writer.is_alive()

    # Invariant 1: subject-new's fresh record SURVIVES the erase of subject-old.
    survivor = store.get(overwrite_key)
    assert survivor is not None and survivor["output"] == {"label": "fresh"}
    assert store.erase_subject("subject-new", dry_run=True).keys_found == 1

    # Invariant 2: the report reflects reality — it deleted exactly subject-old's
    # two records (both were subject-old's at deletion time), nothing else.
    [report] = report_box
    assert isinstance(report, SubjectCacheErasureReport)
    assert report.keys_found == 2 and report.keys_deleted == 2
    assert set(report.key_digests) == {first_digest, second_digest}
    # subject-old is fully gone.
    assert store.erase_subject("subject-old", dry_run=True).keys_found == 0


def test_adopter_shaped_scenario_other_subjects_cache_survives_erase() -> None:
    """Adopter shape: records keyed on claim+evidence content, tagged by subject.
    Erasing one subject leaves every other subject's cache entries — and their
    hits — intact (a store-level integration of the write-index + erase primitive)."""

    store = InMemoryCacheStore()
    scope = {"company_id": "co-1", "product_id": "prod-1"}

    def claim_key(claim_id: str) -> CacheKey:
        # input_hash stands in for the digest over rendered claim + evidence content.
        return CacheKey(activity="review_claim", input_hash=f"digest::{claim_id}", scope=scope)

    key_a = claim_key("claim-A")
    key_b1 = claim_key("claim-B1")
    key_b2 = claim_key("claim-B2")
    store.set(key_a, _record(key_a, "verdict-A", subjects=["subject-a"]))
    store.set(key_b1, _record(key_b1, "verdict-B1", subjects=["subject-b"]))
    store.set(key_b2, _record(key_b2, "verdict-B2", subjects=["subject-b"]))

    report = store.erase_subject("subject-a", dry_run=False)
    assert report.keys_found == 1 and report.keys_deleted == 1

    # subject-a's cache entry is gone (a re-review would MISS)...
    assert store.get(key_a) is None
    # ...but subject-b's two entries still HIT.
    assert store.get(key_b1) is not None
    assert store.get(key_b2) is not None
    assert erase_subject_from_cache(store, "subject-b", dry_run=True).keys_found == 2


def test_erase_subject_end_to_end_preserves_other_subject_hits() -> None:
    """Full executor path: two subjects populate the cache through real runs; erasing
    one makes its input MISS while the other still HITS (provider not re-called)."""

    from typeflux.manifests.activity import AIInvocationContext

    def _ctx(subject: str) -> AIInvocationContext:
        return AIInvocationContext(
            temporal_namespace=None,
            temporal_workflow_type=None,
            temporal_workflow_id=None,
            temporal_run_id=None,
            temporal_activity_type=None,
            temporal_activity_id=None,
            temporal_activity_attempt=None,
            typeflux_activity_name="classify",
            typeflux_manifest_hash="m",
            subject_ids=(subject,),
        )

    store = InMemoryCacheStore()
    activity = _activity()
    # Distinct inputs → distinct cache keys; one response each (a miss would exhaust).
    provider = FakeProvider([_Out(label="A"), _Out(label="B")])

    def run(subject: str, subject_name: str) -> _Out:
        return execute_ai_activity(
            activity=activity,
            input_value=_In(company_id="co-1", subject=subject_name),
            registry=_registry(),
            provider=provider,
            cache_store=store,
            invocation_context=_ctx(subject),
        )

    run("subject-a", "InvoiceA")
    run("subject-b", "InvoiceB")
    assert len(provider.calls) == 2

    erased = store.erase_subject("subject-a", dry_run=False)
    assert erased.keys_deleted == 1

    # subject-b still hits (no new provider call); subject-a would miss (provider
    # exhausted, so a call would raise) — we assert the surviving hit only.
    again_b = run("subject-b", "InvoiceB")
    assert again_b.label == "B"
    assert len(provider.calls) == 2  # hit, not a re-call
