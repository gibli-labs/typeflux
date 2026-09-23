from __future__ import annotations

from types import SimpleNamespace

import pytest

from typeflux.core.artifacts import (
    ArtifactRef,
    ResolvedArtifact,
    ResolvedArtifactGroup,
)
from typeflux.providers import _shared
from typeflux.providers.errors import ProviderConfigError


class RateLimitError(Exception):
    pass


def test_status_code_reads_attrs_then_response() -> None:
    assert _shared.status_code(SimpleNamespace(status_code=429)) == 429
    assert _shared.status_code(SimpleNamespace(status=503)) == 503
    assert _shared.status_code(SimpleNamespace(code=400)) == 400
    nested = SimpleNamespace(response=SimpleNamespace(status_code=404))
    assert _shared.status_code(nested) == 404
    assert _shared.status_code(SimpleNamespace()) is None


def test_retry_after_seconds_from_attr_and_header() -> None:
    assert _shared.retry_after_seconds(SimpleNamespace(retry_after=2)) == 2.0
    headered = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "5"}))
    assert _shared.retry_after_seconds(headered) == 5.0
    assert _shared.retry_after_seconds(SimpleNamespace()) is None
    assert _shared.retry_after_seconds(SimpleNamespace(retry_after=-1)) is None


def test_matches_error_name_walks_mro() -> None:
    assert _shared.matches_error_name(RateLimitError(), "RateLimitError") is True
    assert _shared.matches_error_name(RateLimitError(), "OtherError") is False


def test_is_network_or_timeout_error() -> None:
    assert _shared.is_network_or_timeout_error(TimeoutError()) is True
    assert _shared.is_network_or_timeout_error(OSError()) is True

    class _ConnectionResetError(Exception):
        pass

    assert _shared.is_network_or_timeout_error(_ConnectionResetError()) is True
    assert _shared.is_network_or_timeout_error(ValueError()) is False


def test_matches_provider_error_falls_back_to_name_when_module_absent() -> None:
    # No SDK named "definitely-not-a-module"; only the name match path applies.
    assert _shared.matches_provider_error(
        RateLimitError(), "definitely_not_a_module", "RateLimitError"
    )
    assert not _shared.matches_provider_error(
        ValueError(), "definitely_not_a_module", "RateLimitError"
    )


def _artifact(group: str, index: int, marker: str) -> ResolvedArtifact:
    return ResolvedArtifact(
        group=group,
        index=index,
        ref=ArtifactRef(
            source={"type": "url", "url": f"https://example.test/{marker}"},
            kind="image",
            media_type="image/png",
        ),
        source_kind="url",
        kind="image",
        media_type="image/png",
        role=marker,
        sha256=None,
        size_bytes=None,
    )


def test_artifact_lookup_uses_provider_name_in_errors() -> None:
    groups = (
        ResolvedArtifactGroup(
            name="docs",
            artifacts=(_artifact("docs", 0, "a"), _artifact("docs", 1, "b")),
        ),
    )

    with pytest.raises(ProviderConfigError, match="unknown artifact group") as exc:
        _shared.artifacts_for_group(groups, "missing", provider="openai")
    assert exc.value.provider == "openai"

    assert _shared.artifact_for_name(groups, "docs[1]", provider="anthropic").role == "b"

    with pytest.raises(ProviderConfigError, match="resolves to 2 artifacts") as exc2:
        _shared.artifact_for_name(groups, "docs", provider="anthropic")
    assert exc2.value.provider == "anthropic"

    with pytest.raises(ProviderConfigError, match="invalid artifact reference"):
        _shared.artifact_for_name(groups, "docs[x]", provider="openai")
