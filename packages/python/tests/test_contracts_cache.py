"""Golden-lock + determinism tests for the cache key + record contract (#391, #504)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from typeflux.contracts import (
    CacheKey,
    cache_input_hash,
    cache_key_digest,
    cache_record,
)
from typeflux.core.artifacts import (
    ArtifactRef,
    ResolvedArtifact,
    ResolvedArtifactGroup,
    artifact_groups_cache_identity,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _norm(value: object) -> object:
    return json.loads(json.dumps(value))


def _artifact_groups() -> tuple[ResolvedArtifactGroup, ...]:
    """The golden artifact fixture (#504): a hashed local file plus a URL artifact
    with no sha256/size/role — pinning that absent fields DROP from the folded
    identity rather than serializing as null, and that an UNHASHED artifact folds
    its source (both SDKs must agree)."""

    pdf = ResolvedArtifact(
        group="contract",
        index=0,
        ref=ArtifactRef(source={"type": "local_path", "path": "/data/contract.pdf"}),
        source_kind="local_path",
        kind="document",
        media_type="application/pdf",
        role="user",
        sha256="e" * 64,
        size_bytes=2048,
    )
    diagram = ResolvedArtifact(
        group="contract",
        index=1,
        ref=ArtifactRef(source={"type": "url", "url": "https://example.com/diagram.png"}),
        source_kind="url",
        kind="image",
        media_type="image/png",
        role=None,
        sha256=None,
        size_bytes=None,
    )
    return (ResolvedArtifactGroup(name="contract", artifacts=(pdf, diagram)),)


def build_goldens() -> dict[str, object]:
    """Construct the cache key + record from the Python baseline. Shared by the
    golden-lock test and the regeneration step so they cannot drift."""

    input_hash = cache_input_hash(
        activity="classify_ticket",
        input_schema_hash="a" * 64,
        rendered_messages_hash="b" * 64,
        provider_params={"model": "fake-model", "temperature": 0.0},
    )
    key = CacheKey(
        activity="classify_ticket",
        input_hash=input_hash,
        scope={"company_id": "co-1", "product_id": "prod-1"},
    )
    record = cache_record(
        key=key,
        output={"label": "billing", "score": 0.97},
        created_at="2026-01-01T00:00:00Z",
        output_schema_hash="c" * 64,
        manifest_hash="d" * 64,
        tokens_saved=512,
    )
    # Same call-determining inputs plus resolved artifacts (#504): the folded
    # identity is the artifact_groups_cache_identity shape (safe summary +
    # source for unhashed artifacts), so replacing the bytes — or the source
    # location — under the same group name changes the key.
    artifacts_input_hash = cache_input_hash(
        activity="classify_ticket",
        input_schema_hash="a" * 64,
        rendered_messages_hash="b" * 64,
        provider_params={"model": "fake-model", "temperature": 0.0},
        artifacts=artifact_groups_cache_identity(_artifact_groups()),
    )
    artifacts_key = CacheKey(
        activity="classify_ticket",
        input_hash=artifacts_input_hash,
        scope={"company_id": "co-1", "product_id": "prod-1"},
    )
    # Subject-scoped record (#715 slice 1): subjects ride on the RECORD, and the
    # KEY is byte-identical to the subject-free record above — proving that adding
    # subjects never perturbs the cache key/digest (over-partitioning guard). Both
    # editions must reproduce this golden.
    subjects_record = cache_record(
        key=key,
        output={"label": "billing", "score": 0.97},
        created_at="2026-01-01T00:00:00Z",
        output_schema_hash="c" * 64,
        manifest_hash="d" * 64,
        tokens_saved=512,
        subjects=["subject-a", "subject-b"],
    )
    return {
        "cache-key/golden/cache_key.json": _norm(key.to_dict()),
        "cache-key/golden/cache_record.json": _norm(record),
        "cache-key/golden/cache_record_subjects.json": _norm(subjects_record),
        "cache-key/golden/cache_key_artifacts.json": _norm(artifacts_key.to_dict()),
    }


_GOLDENS = build_goldens()


def test_input_hash_is_deterministic_and_param_order_independent() -> None:
    a = cache_input_hash(
        activity="x",
        input_schema_hash="s",
        rendered_messages_hash="r",
        provider_params={"model": "m", "temperature": 0.0},
    )
    b = cache_input_hash(
        activity="x",
        input_schema_hash="s",
        rendered_messages_hash="r",
        provider_params={"temperature": 0.0, "model": "m"},
    )
    assert a == b
    assert len(a) == 64


def test_input_hash_changes_with_call_determining_inputs() -> None:
    base = dict(activity="x", input_schema_hash="s", rendered_messages_hash="r")
    h0 = cache_input_hash(**base, provider_params={"model": "m"})
    assert cache_input_hash(**base, provider_params={"model": "other"}) != h0
    assert (
        cache_input_hash(**{**base, "rendered_messages_hash": "r2"}, provider_params={"model": "m"})
        != h0
    )


def test_artifact_identity_changes_input_hash() -> None:
    """Swapping an artifact's bytes (sha256) under the same group name + prompt
    must change the key (#504) — before the fold this served a stale output."""

    groups = _artifact_groups()
    base = dict(
        activity="x",
        input_schema_hash="s",
        rendered_messages_hash="r",
        provider_params={"model": "m"},
    )
    h_original = cache_input_hash(**base, artifacts=artifact_groups_cache_identity(groups))
    swapped = dataclasses.replace(groups[0].artifacts[0], sha256="f" * 64)
    swapped_groups = (
        ResolvedArtifactGroup(name="contract", artifacts=(swapped, groups[0].artifacts[1])),
    )
    h_swapped = cache_input_hash(**base, artifacts=artifact_groups_cache_identity(swapped_groups))
    assert h_swapped != h_original
    assert h_original != cache_input_hash(**base)


def test_unhashed_artifact_source_swap_changes_input_hash() -> None:
    """An UNHASHED artifact (no sha256) folds its source: swapping the URL under
    the same group/index/media type must change the key (#504 codex P1)."""

    def url_group(url: str) -> tuple[ResolvedArtifactGroup, ...]:
        artifact = ResolvedArtifact(
            group="docs",
            index=0,
            ref=ArtifactRef(source={"type": "url", "url": url}),
            source_kind="url",
            kind="image",
            media_type="image/png",
            role=None,
            sha256=None,
            size_bytes=None,
        )
        return (ResolvedArtifactGroup(name="docs", artifacts=(artifact,)),)

    identity = artifact_groups_cache_identity(url_group("https://example.com/a.png"))
    assert identity[0]["artifacts"][0]["source"] == {
        "type": "url",
        "url": "https://example.com/a.png",
    }
    base = dict(
        activity="x",
        input_schema_hash="s",
        rendered_messages_hash="r",
        provider_params={"model": "m"},
    )
    h_a = cache_input_hash(
        **base, artifacts=artifact_groups_cache_identity(url_group("https://example.com/a.png"))
    )
    h_b = cache_input_hash(
        **base, artifacts=artifact_groups_cache_identity(url_group("https://example.com/b.png"))
    )
    assert h_a != h_b


def test_hashed_artifact_folds_no_source() -> None:
    """A sha256-pinned artifact keys on its CONTENT: the source stays out of the
    fold, so the same bytes served from a new location still hit."""

    groups = _artifact_groups()
    identity = artifact_groups_cache_identity(groups)
    hashed, unhashed = identity[0]["artifacts"]
    assert "source" not in hashed
    assert "source" in unhashed


def test_empty_groups_drop_from_cache_identity() -> None:
    """An optional artifact input that resolved to nothing must not change the
    key (#504 codex P2): empty groups filter out, and an all-empty fold hashes
    identically to no artifacts at all."""

    empty = (ResolvedArtifactGroup(name="docs", artifacts=()),)
    assert artifact_groups_cache_identity(empty) == []
    base = dict(
        activity="x",
        input_schema_hash="s",
        rendered_messages_hash="r",
        provider_params={"model": "m"},
    )
    h0 = cache_input_hash(**base)
    assert cache_input_hash(**base, artifacts=artifact_groups_cache_identity(empty)) == h0


def test_absent_or_empty_artifacts_leave_input_hash_unchanged() -> None:
    """No artifacts → the payload is byte-identical to the pre-#504 recipe, so
    keys for artifact-free activities (and stored entries) stay valid."""

    base = dict(
        activity="x",
        input_schema_hash="s",
        rendered_messages_hash="r",
        provider_params={"model": "m"},
    )
    h0 = cache_input_hash(**base)
    assert cache_input_hash(**base, artifacts=None) == h0
    assert cache_input_hash(**base, artifacts=[]) == h0


def test_scope_order_does_not_change_digest() -> None:
    k1 = CacheKey("a", "h", {"company_id": "c", "product_id": "p"})
    k2 = CacheKey("a", "h", {"product_id": "p", "company_id": "c"})
    assert cache_key_digest(k1) == cache_key_digest(k2)
    assert len(cache_key_digest(k1)) == 64


@pytest.mark.parametrize("rel", sorted(_GOLDENS))
def test_shape_matches_golden(rel: str) -> None:
    expected = json.loads((CONTRACTS / rel).read_text(encoding="utf-8"))
    assert _GOLDENS[rel] == expected
