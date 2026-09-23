from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import (
    ArtifactGroupPart,
    ArtifactRef,
    ResolvedArtifact,
    ResolvedArtifactGroup,
)
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.providers import GeminiProvider, ProviderUsage
from typeflux.providers.errors import ProviderConfigError


def _resolved_artifact(
    *,
    group: str,
    source: dict[str, Any],
    kind: str,
    media_type: str,
    local_path: Path | None = None,
) -> ResolvedArtifact:
    return ResolvedArtifact(
        group=group,
        index=0,
        ref=ArtifactRef(source=source, kind=kind, media_type=media_type),
        source_kind=source["type"],
        kind=kind,
        media_type=media_type,
        role=group,
        sha256=None,
        size_bytes=None,
        local_path=local_path,
    )


class Output(BaseModel):
    label: str


def _response(
    value: BaseModel | None = None,
    *,
    text: str | None = None,
    finish_reason: str = "STOP",
    usage: tuple[int, int, int] | None = (6, 1, 23),
) -> SimpleNamespace:
    parsed = value
    body = SimpleNamespace(
        parsed=parsed,
        text=text if text is not None else (value.model_dump_json() if value else None),
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish_reason))],
        usage_metadata=(
            None
            if usage is None
            else SimpleNamespace(
                prompt_token_count=usage[0],
                candidates_token_count=usage[1],
                total_token_count=usage[2],
            )
        ),
    )
    return body


class _FakeModels:
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._error = error

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response if self._response is not None else _response(Output(label="ok"))


class _AsyncFakeModels(_FakeModels):
    async def generate_content(self, **kwargs: Any) -> Any:  # type: ignore[override]
        return super().generate_content(**kwargs)


class _FakeClient:
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        self.models = _FakeModels(response, error)


class _FakeAsyncClient:
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        self.aio = SimpleNamespace(models=_AsyncFakeModels(response, error))


def test_structured_call_returns_validated_output_and_request_shape() -> None:
    client = _FakeClient()
    provider = GeminiProvider(default_model="gemini-2.5-flash", genai_client=client)

    result = provider.structured_call(
        messages=[
            ChatMessage(role="system", content="Be precise."),
            ChatMessage(role="user", content="hello"),
        ],
        output_schema=Output,
        temperature=0.0,
    )

    assert isinstance(result, Output)
    call = client.models.calls[0]
    assert call["model"] == "gemini-2.5-flash"
    config = call["config"]
    assert config["response_schema"] is Output
    assert config["response_mime_type"] == "application/json"
    assert config["system_instruction"] == {"parts": [{"text": "Be precise."}]}
    assert config["temperature"] == 0.0
    # user role maps to "user"; system is lifted out of contents.
    assert call["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_assistant_role_maps_to_model() -> None:
    client = _FakeClient()
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="assistant", content="prior")],
        output_schema=Output,
    )
    assert client.models.calls[0]["contents"][0]["role"] == "model"


def test_per_call_model_override_does_not_mutate_default() -> None:
    client = _FakeClient()
    provider = GeminiProvider(default_model="gemini-2.5-flash", genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
        model="gemini-2.5-pro",
    )
    assert client.models.calls[0]["model"] == "gemini-2.5-pro"
    assert provider.default_model == "gemini-2.5-flash"


def test_usage_forwards_total_tokens_verbatim() -> None:
    # total_token_count (incl. thinking tokens) is forwarded, not derived.
    client = _FakeClient(_response(Output(label="ok"), usage=(20, 15, 94)))
    provider = GeminiProvider(genai_client=client)
    usages: list[ProviderUsage] = []
    provider.structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
        usage_sink=usages.append,
    )
    assert usages[0].usage_details() == {"input": 20, "output": 15, "total": 94}


def test_truncated_response_raises_config_error() -> None:
    client = _FakeClient(_response(Output(label="ok"), finish_reason="MAX_TOKENS"))
    provider = GeminiProvider(genai_client=client)
    with pytest.raises(ProviderConfigError, match="truncated"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="x")],
            output_schema=Output,
        )


def test_validation_failure_propagates_unwrapped() -> None:
    client = _FakeClient(_response(text='{"not_label": "x"}'))
    provider = GeminiProvider(genai_client=client)
    with pytest.raises(ValidationError):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="x")],
            output_schema=Output,
        )


# Status-code / network-error classification and unknown-error wrapping are
# covered for all providers in test_provider_error_classification.py; the
# stale-cache classification below is Gemini-specific.


def test_image_artifact_becomes_inline_data_part(tmp_path: Path) -> None:
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    artifact = _resolved_artifact(
        group="pics",
        source={"type": "local_path", "path": str(image)},
        kind="image",
        media_type="image/png",
        local_path=image,
    )
    groups = (ResolvedArtifactGroup(name="pics", artifacts=(artifact,)),)
    client = _FakeClient()
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[
            ChatMessage(
                role="user",
                content=(ArtifactGroupPart(group="pics", text="Photo:"),),
            )
        ],
        output_schema=Output,
        artifacts=groups,
    )
    parts = client.models.calls[0]["contents"][0]["parts"]
    assert parts[0] == {"text": "Photo:"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["inline_data"]["data"] == b"\x89PNG\r\n\x1a\n"


class _FakeFiles:
    def __init__(self) -> None:
        self.uploads: list[Any] = []
        self._file = SimpleNamespace(
            name="files/abc", uri="https://generativelanguage/files/abc", state="ACTIVE"
        )

    def upload(self, *, file: Any, config: Any = None) -> Any:
        self.uploads.append({"file": file, "config": config})
        return self._file

    def get(self, *, name: str) -> Any:
        return self._file


def test_oversize_artifact_uploads_via_files_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #358: a local artifact over the inline cap is uploaded via the Files API and
    # referenced by file_uri instead of inlined.
    import typeflux.providers.gemini as gemini_module

    monkeypatch.setattr(gemini_module, "_GEMINI_INLINE_MAX_BYTES", 4)
    media = tmp_path / "clip.wav"
    media.write_bytes(b"RIFFlarge-audio-bytes")  # > 4 bytes → oversize
    artifact = _resolved_artifact(
        group="media",
        source={"type": "local_path", "path": str(media)},
        kind="audio",
        media_type="audio/wav",
        local_path=media,
    )
    groups = (ResolvedArtifactGroup(name="media", artifacts=(artifact,)),)
    client = _FakeClient()
    client.files = _FakeFiles()  # type: ignore[attr-defined]
    provider = GeminiProvider(genai_client=client)

    provider.structured_call(
        messages=[
            ChatMessage(role="user", content=(ArtifactGroupPart(group="media", text="Clip:"),))
        ],
        output_schema=Output,
        artifacts=groups,
    )

    assert len(client.files.uploads) == 1  # uploaded once
    parts = client.models.calls[0]["contents"][0]["parts"]
    assert parts[0] == {"text": "Clip:"}
    assert parts[1]["file_data"]["file_uri"] == "https://generativelanguage/files/abc"
    assert parts[1]["file_data"]["mime_type"] == "audio/wav"
    assert "inline_data" not in parts[1]


def test_oversize_artifact_referenced_twice_uploads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dedup within a request: an artifact referenced in two parts uploads once.
    import typeflux.providers.gemini as gemini_module

    monkeypatch.setattr(gemini_module, "_GEMINI_INLINE_MAX_BYTES", 4)
    media = tmp_path / "doc.pdf"
    media.write_bytes(b"%PDF-large-bytes")
    artifact = _resolved_artifact(
        group="docs",
        source={"type": "local_path", "path": str(media)},
        kind="document",
        media_type="application/pdf",
        local_path=media,
    )
    groups = (ResolvedArtifactGroup(name="docs", artifacts=(artifact,)),)
    client = _FakeClient()
    client.files = _FakeFiles()  # type: ignore[attr-defined]
    provider = GeminiProvider(genai_client=client)

    provider.structured_call(
        messages=[
            ChatMessage(
                role="user",
                content=(
                    ArtifactGroupPart(group="docs", text="A:"),
                    ArtifactGroupPart(group="docs", text="B:"),
                ),
            )
        ],
        output_schema=Output,
        artifacts=groups,
    )
    assert len(client.files.uploads) == 1


def test_unreferenced_oversize_artifact_is_not_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #358 review (codex P2): an oversize artifact resolved into the request but
    # not referenced by any message must NOT be uploaded (no Files-API call, no
    # charge for unsent data) — a pure-text call stays pure-text.
    import typeflux.providers.gemini as gemini_module

    monkeypatch.setattr(gemini_module, "_GEMINI_INLINE_MAX_BYTES", 4)
    media = tmp_path / "unused.bin"
    media.write_bytes(b"oversize-unreferenced")
    artifact = _resolved_artifact(
        group="unused",
        source={"type": "local_path", "path": str(media)},
        kind="document",
        media_type="application/pdf",
        local_path=media,
    )
    groups = (ResolvedArtifactGroup(name="unused", artifacts=(artifact,)),)
    client = _FakeClient()
    client.files = _FakeFiles()  # type: ignore[attr-defined]
    provider = GeminiProvider(genai_client=client)

    provider.structured_call(
        messages=[ChatMessage(role="user", content="just text, no artifact reference")],
        output_schema=Output,
        artifacts=groups,
    )
    assert client.files.uploads == []  # never touched the Files API


def test_small_artifact_stays_inline(tmp_path: Path) -> None:
    # Default cap (~20MB): a small artifact is inlined, never uploaded.
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    artifact = _resolved_artifact(
        group="pics",
        source={"type": "local_path", "path": str(image)},
        kind="image",
        media_type="image/png",
        local_path=image,
    )
    groups = (ResolvedArtifactGroup(name="pics", artifacts=(artifact,)),)
    client = _FakeClient()
    client.files = _FakeFiles()  # type: ignore[attr-defined]
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="pics"),))],
        output_schema=Output,
        artifacts=groups,
    )
    assert client.files.uploads == []  # stayed inline
    assert "inline_data" in client.models.calls[0]["contents"][0]["parts"][0]


def test_inline_path_refuses_oversize_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #358 review: defensive invariant — an oversize artifact that reaches the
    # inline builder (no upload pass / missing key) fails loud, not silently
    # inlined to a server-side rejection.
    import typeflux.providers.gemini as gemini_module
    from typeflux.providers.errors import ProviderConfigError

    monkeypatch.setattr(gemini_module, "_GEMINI_INLINE_MAX_BYTES", 4)
    media = tmp_path / "big.bin"
    media.write_bytes(b"oversize-bytes")
    artifact = _resolved_artifact(
        group="m",
        source={"type": "local_path", "path": str(media)},
        kind="document",
        media_type="application/pdf",
        local_path=media,
    )
    with pytest.raises(ProviderConfigError, match="inline size cap"):
        gemini_module._to_gemini_inline_artifact_part(artifact)


@pytest.mark.asyncio
async def test_async_structured_call_returns_output() -> None:
    client = _FakeAsyncClient(_response(Output(label="async-ok")))
    provider = GeminiProvider(async_genai_client=client)
    result = await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
    )
    assert isinstance(result, Output) and result.label == "async-ok"


def test_default_params_respect_provider_capability() -> None:
    provider = GeminiProvider(
        genai_client=_FakeClient(),
        default_provider_params=ProviderParams(temperature=0.2, top_p=0.9),
    )
    provider.structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
    )
    config = provider._client.models.calls[0]["config"]  # type: ignore[union-attr]
    assert config["temperature"] == 0.2
    assert config["top_p"] == 0.9


def test_thinking_budget_forwarded_including_zero() -> None:
    # 0 disables thinking and must be forwarded (not dropped as falsy).
    client = _FakeClient()
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
        provider_params=ProviderParams(thinking_budget=0),
    )
    assert client.models.calls[0]["config"]["thinking_config"] == {"thinking_budget": 0}


def test_thinking_budget_positive_caps_budget() -> None:
    client = _FakeClient()
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
        provider_params=ProviderParams(thinking_budget=512),
    )
    assert client.models.calls[0]["config"]["thinking_config"] == {"thinking_budget": 512}


def test_thinking_budget_absent_sets_no_thinking_config() -> None:
    client = _FakeClient()
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="user", content="x")],
        output_schema=Output,
    )
    assert "thinking_config" not in client.models.calls[0]["config"]


def test_thinking_budget_is_gemini_only() -> None:
    # Portability guard: thinking_budget is a Gemini param; setting it on a
    # provider that doesn't support it fails loud at validation.
    from typeflux.providers import OpenAIProvider
    from typeflux.providers.base import validate_provider_params_supported

    with pytest.raises(ProviderConfigError):
        validate_provider_params_supported(OpenAIProvider, ProviderParams(thinking_budget=0))
    assert "thinking_budget" in GeminiProvider.supported_provider_params


def test_system_only_prompt_raises_config_error() -> None:
    provider = GeminiProvider(genai_client=_FakeClient())
    with pytest.raises(ProviderConfigError, match="at least one user or assistant"):
        provider.structured_call(
            messages=[ChatMessage(role="system", content="only system")],
            output_schema=Output,
        )


def test_supported_artifact_kinds_only_claims_what_is_attached() -> None:
    # Preflight must reject kinds the messages path can't attach (no
    # provider_file/archive), not pass then fail mid-run. Audio/video are
    # attached inline like images/PDFs (#336); large media via the Files API
    # is tracked separately.
    assert GeminiProvider.supported_artifact_kinds == frozenset(
        {"image", "document", "audio", "video"}
    )


def test_audio_and_video_artifacts_become_inline_data_parts(tmp_path: Path) -> None:
    clip = tmp_path / "note.mp3"
    clip.write_bytes(b"ID3\x04audio")
    reel = tmp_path / "demo.mp4"
    reel.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    audio = _resolved_artifact(
        group="media",
        source={"type": "local_path", "path": str(clip)},
        kind="audio",
        media_type="audio/mpeg",
        local_path=clip,
    )
    video = _resolved_artifact(
        group="media",
        source={"type": "local_path", "path": str(reel)},
        kind="video",
        media_type="video/mp4",
        local_path=reel,
    )
    groups = (ResolvedArtifactGroup(name="media", artifacts=(audio, video)),)
    client = _FakeClient()
    provider = GeminiProvider(genai_client=client)
    provider.structured_call(
        messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="media"),))],
        output_schema=Output,
        artifacts=groups,
    )
    parts = client.models.calls[0]["contents"][0]["parts"]
    inline = [p["inline_data"] for p in parts if "inline_data" in p]
    assert {"mime_type": "audio/mpeg", "data": b"ID3\x04audio"} in inline
    assert {"mime_type": "video/mp4", "data": b"\x00\x00\x00\x18ftypmp42"} in inline


def test_url_audio_video_fails_loud_until_files_api() -> None:
    # audio/video are admitted at preflight (by kind) but attach only as local
    # inline data; a URL-sourced one must fail loud, not silently take the
    # image/document file_data path (Files API is #358).
    artifact = _resolved_artifact(
        group="media",
        source={"type": "url", "url": "https://example.com/clip.mp3"},
        kind="audio",
        media_type="audio/mpeg",
    )
    provider = GeminiProvider(genai_client=_FakeClient())
    with pytest.raises(ProviderConfigError, match="audio/video only as local inline"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="media"),))],
            output_schema=Output,
            artifacts=(ResolvedArtifactGroup(name="media", artifacts=(artifact,)),),
        )


def test_url_artifact_without_media_type_raises() -> None:
    artifact = _resolved_artifact(
        group="links",
        source={"type": "url", "url": "https://example.com/doc"},
        kind="document",
        media_type="application/pdf",
    )
    # Force media_type None on the resolved artifact to hit the URL guard.
    object.__setattr__(artifact, "media_type", None)
    provider = GeminiProvider(genai_client=_FakeClient())
    with pytest.raises(ProviderConfigError, match="media type to attach a URL"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="links"),))],
            output_schema=Output,
            artifacts=(ResolvedArtifactGroup(name="links", artifacts=(artifact,)),),
        )


# --- Provider session caching (#60, phase 3: reference style) ---------------


class _FakeCaches:
    def __init__(self, name: str = "cachedContents/test") -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self._name = name

    def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return SimpleNamespace(name=self._name)

    def delete(self, **kwargs: Any) -> None:
        self.deleted.append(kwargs)


class _CachingFakeClient(_FakeClient):
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        super().__init__(response, error)
        self.caches = _FakeCaches()


def test_gemini_declares_reference_session_cache() -> None:
    assert GeminiProvider.supports_session_cache is True
    assert GeminiProvider.session_cache_style == "reference"


def test_prepare_cached_session_creates_cachedcontent() -> None:
    client = _CachingFakeClient()
    provider = GeminiProvider(default_model="gemini-2.5-flash", genai_client=client)

    handle = provider.prepare_cached_session(
        messages=[
            ChatMessage(role="system", content="Stable instructions."),
            ChatMessage(role="user", content="reference files"),
        ],
        model="gemini-2.5-flash",
        identity_hash="idhash",
        ttl_seconds=300,
    )

    assert handle.provider == "gemini"
    assert handle.supported is True
    assert handle.cache_id == "cachedContents/test"
    assert handle.identity_hash == "idhash"
    assert handle.ttl_seconds == 300
    create = client.caches.created[0]
    assert create["model"] == "gemini-2.5-flash"
    assert create["config"]["system_instruction"] == {"parts": [{"text": "Stable instructions."}]}
    assert create["config"]["contents"][0]["parts"] == [{"text": "reference files"}]
    assert create["config"]["ttl"] == "300s"


def test_structured_call_references_cache_and_omits_system() -> None:
    client = _CachingFakeClient()
    provider = GeminiProvider(default_model="gemini-2.5-flash", genai_client=client)
    handle = CachedSessionHandle(
        provider="gemini", identity_hash="h", supported=True, cache_id="cachedContents/x"
    )

    # Reference style: the runtime passes only the per-item input.
    provider.structured_call(
        messages=[ChatMessage(role="user", content="the per-item question")],
        output_schema=Output,
        cached_session=handle,
    )

    config = client.models.calls[0]["config"]
    assert config["cached_content"] == "cachedContents/x"
    # The prefix (incl. system instruction) is in the cache, not re-sent.
    assert "system_instruction" not in config


def test_structured_call_without_session_keeps_system_inline() -> None:
    client = _CachingFakeClient()
    provider = GeminiProvider(default_model="gemini-2.5-flash", genai_client=client)

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="Be precise."),
            ChatMessage(role="user", content="hi"),
        ],
        output_schema=Output,
    )

    config = client.models.calls[0]["config"]
    assert "cached_content" not in config
    assert config["system_instruction"] == {"parts": [{"text": "Be precise."}]}


def test_release_cached_session_deletes_cache() -> None:
    client = _CachingFakeClient()
    provider = GeminiProvider(genai_client=client)
    handle = CachedSessionHandle(
        provider="gemini", identity_hash="h", supported=True, cache_id="cachedContents/x"
    )

    provider.release_cached_session(handle)
    assert client.caches.deleted == [{"name": "cachedContents/x"}]

    # A fail-soft handle (no cache id) releases nothing.
    provider.release_cached_session(CachedSessionHandle(provider="gemini", identity_hash="h"))
    assert len(client.caches.deleted) == 1


def test_gemini_usage_parses_cached_content_tokens() -> None:
    response = _response(Output(label="ok"))
    response.usage_metadata = SimpleNamespace(
        prompt_token_count=10,
        candidates_token_count=4,
        total_token_count=14,
        cached_content_token_count=950,
    )
    provider = GeminiProvider(genai_client=_FakeClient(response=response))
    captured: list[Any] = []

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hi")],
        output_schema=Output,
        usage_sink=captured.append,
    )

    assert captured[0].cache_read_tokens == 950
    assert captured[0].cache_hit is True
    assert captured[0].usage_details()["cache_read"] == 950


def test_reference_cache_fails_loud_on_inconsistent_handle() -> None:
    provider = GeminiProvider(default_model="gemini-2.5-flash", genai_client=_CachingFakeClient())
    msgs = [ChatMessage(role="user", content="x")]

    # supported reference handle with no cache_id → must fail, not silently drop prefix
    with pytest.raises(ProviderConfigError, match="no cache_id"):
        provider.structured_call(
            messages=msgs,
            output_schema=Output,
            cached_session=CachedSessionHandle(
                provider="gemini", identity_hash="h", supported=True
            ),
        )
    # prepared by a different provider
    with pytest.raises(ProviderConfigError, match="not gemini"):
        provider.structured_call(
            messages=msgs,
            output_schema=Output,
            cached_session=CachedSessionHandle(
                provider="openai", identity_hash="h", supported=True, cache_id="cc/x"
            ),
        )
    # model-bound mismatch
    with pytest.raises(ProviderConfigError, match="model-bound"):
        provider.structured_call(
            messages=msgs,
            output_schema=Output,
            cached_session=CachedSessionHandle(
                provider="gemini", identity_hash="h", supported=True, cache_id="cc/x", model="other"
            ),
        )


def test_release_swallows_delete_errors_best_effort() -> None:
    client = _CachingFakeClient()

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("already gone")

    client.caches.delete = _boom  # type: ignore[method-assign]
    provider = GeminiProvider(genai_client=client)
    # Must not raise — cleanup is best-effort (TTL reaps it).
    provider.release_cached_session(
        CachedSessionHandle(provider="gemini", identity_hash="h", supported=True, cache_id="cc/x")
    )


class _GeminiClientError(Exception):
    """Mimics google-genai ClientError enough for status_code()."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_stale_cache_error_classification() -> None:
    # #368: a referenced cache that expired/was deleted comes back as a 403 whose
    # message names CachedContent. It maps to ProviderCacheUnavailableError (so
    # the runtime can recover uncached) ONLY when the call used cached_content.
    from typeflux.providers.errors import (
        ProviderAuthError,
        ProviderCacheUnavailableError,
    )
    from typeflux.providers.gemini import _raise_gemini_provider_error

    stale = _GeminiClientError(403, "CachedContent not found (or permission denied)")
    # With cached_content set → recoverable, typed cache-unavailable error.
    with pytest.raises(ProviderCacheUnavailableError):
        _raise_gemini_provider_error(stale, cached_content="cachedContents/x")
    # Same error without a cache in play → ordinary auth error (not misread).
    with pytest.raises(ProviderAuthError):
        _raise_gemini_provider_error(stale, cached_content=None)
    # Robust to message drift: ANY 403/404 on a cached call is treated as a stale
    # cache (recovers uncached); a false positive costs only one extra call.
    reworded = _GeminiClientError(404, "NOT_FOUND: cache resource was not found")
    with pytest.raises(ProviderCacheUnavailableError):
        _raise_gemini_provider_error(reworded, cached_content="cachedContents/x")
    # But a non-cache status (e.g. 429) on a cached call still classifies normally.
    from typeflux.providers.errors import ProviderRateLimitError

    rate = _GeminiClientError(429, "rate limited")
    with pytest.raises(ProviderRateLimitError):
        _raise_gemini_provider_error(rate, cached_content="cachedContents/x")
