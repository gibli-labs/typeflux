from __future__ import annotations

import asyncio
import base64
import builtins
import sys
import types
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import (
    ArtifactGroupPart,
    ArtifactPart,
    ArtifactRef,
    ResolvedArtifact,
    ResolvedArtifactGroup,
    TextPart,
)
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.prompts.context import langfuse_prompt_context
from typeflux.providers import OpenAIProvider
from typeflux.providers.base import ProviderUsage
from typeflux.providers.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderTransientError,
)


class Output(BaseModel):
    label: str


def _fake_usage() -> Any:
    return types.SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)


def _fake_completion(finish_reason: str, *, usage: Any | None = None) -> Any:
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(finish_reason=finish_reason)],
        usage=usage,
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.finish_reason = "stop"
        self.usage: Any | None = _fake_usage()

    def create(self, **kwargs: Any) -> Output:
        self.calls.append(kwargs)
        return Output(label="ok")

    def create_with_completion(self, **kwargs: Any) -> tuple[Output, Any]:
        return self.create(**kwargs), _fake_completion(self.finish_reason, usage=self.usage)


class _FakeInstructorClient:
    def __init__(self) -> None:
        completions = _FakeCompletions()
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


class _AsyncFakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.finish_reason = "stop"
        self.usage: Any | None = _fake_usage()

    async def create(self, **kwargs: Any) -> Output:
        self.calls.append(kwargs)
        return Output(label="ok")

    async def create_with_completion(self, **kwargs: Any) -> tuple[Output, Any]:
        return await self.create(**kwargs), _fake_completion(self.finish_reason, usage=self.usage)


class _AsyncFakeInstructorClient:
    def __init__(self) -> None:
        completions = _AsyncFakeCompletions()
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


def _contains_identity(value: Any, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(_contains_identity(item, target) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identity(item, target) for item in value)
    return False


def test_openai_provider_maps_typeflux_call_to_instructor() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(
        default_model="test-model",
        instructor_client=client,
        instructor_max_retries=1,
    )

    result = provider.structured_call(
        messages=[ChatMessage(role="user", content="hello", name="tester")],
        output_schema=Output,
        temperature=0,
        metadata={
            "typeflux.activity_name": "classify_ticket",
            "typeflux.activity_execution_manifest_hash": "exec-hash",
            "typeflux": {
                "level": "provider",
                "activity_name": "classify_ticket",
                "join": {"activity_execution_manifest_hash": "exec-hash"},
            },
            "custom": "value",
            "attempt": 2,
            "cached": False,
        },
    )

    call = client.completions.calls[0]
    assert result == Output(label="ok")
    assert call["model"] == "test-model"
    assert call["messages"] == [{"role": "user", "content": "hello", "name": "tester"}]
    assert call["response_model"] is Output
    assert call["max_retries"] == 1
    assert "metadata" not in call
    assert "name" not in call


def test_openai_provider_reports_max_tokens_truncation() -> None:
    client = _FakeInstructorClient()
    client.completions.finish_reason = "length"
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)

    with pytest.raises(ProviderConfigError, match="max_tokens was exhausted"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
        )


@pytest.mark.asyncio
async def test_openai_provider_reports_max_tokens_truncation_async() -> None:
    client = _AsyncFakeInstructorClient()
    client.completions.finish_reason = "length"
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)

    with pytest.raises(ProviderConfigError, match="max_tokens was exhausted"):
        await provider.async_structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
        )


def test_openai_provider_forwards_usage_to_sink() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    received: list[ProviderUsage] = []

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=received.append,
    )

    assert len(received) == 1
    assert received[0].input_tokens == 11
    assert received[0].output_tokens == 7
    assert received[0].total_tokens == 18
    assert received[0].model == "test-model"


@pytest.mark.asyncio
async def test_openai_provider_forwards_usage_to_sink_async() -> None:
    client = _AsyncFakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    received: list[ProviderUsage] = []

    await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=received.append,
    )

    assert [u.input_tokens for u in received] == [11]
    assert received[0].output_tokens == 7
    assert received[0].total_tokens == 18


def test_openai_provider_truncation_forwards_no_usage() -> None:
    # Usage is forwarded only after the truncation guard, mirroring Anthropic:
    # a response cut off at max_tokens reports no usage.
    client = _FakeInstructorClient()
    client.completions.finish_reason = "length"
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    received: list[ProviderUsage] = []

    with pytest.raises(ProviderConfigError, match="max_tokens was exhausted"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
            usage_sink=received.append,
        )

    assert received == []


def test_openai_provider_missing_usage_does_not_call_sink() -> None:
    client = _FakeInstructorClient()
    client.completions.usage = None  # SDK omitted usage (e.g. a streaming edge)
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    received: list[ProviderUsage] = []

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=received.append,
    )

    assert received == []


def test_openai_provider_maps_provider_params() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        provider_params=ProviderParams(
            max_tokens=2048,
            top_p=0.9,
            stop=("DONE",),
            seed=42,
            timeout=30,
            frequency_penalty=0.2,
            presence_penalty=0.3,
        ),
    )

    call = client.completions.calls[0]
    assert call["max_tokens"] == 2048
    assert call["top_p"] == 0.9
    assert call["stop"] == ["DONE"]
    assert call["seed"] == 42
    assert call["timeout"] == 30
    assert call["frequency_penalty"] == 0.2
    assert call["presence_penalty"] == 0.3


def test_openai_provider_rejects_unsupported_provider_params() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)

    with pytest.raises(ProviderConfigError, match="top_k"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
            provider_params=ProviderParams(top_k=5),
        )


def test_openai_provider_passes_trace_name_only_when_langfuse_enabled() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(
        default_model="test-model",
        enable_langfuse=True,
        instructor_client=client,
    )

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        metadata={
            "typeflux.activity_name": "classify_ticket",
            "typeflux": {"join": {"activity_execution_manifest_hash": "exec-hash"}},
        },
    )

    call = client.completions.calls[0]
    assert call["metadata"]["typeflux"]["join"]["activity_execution_manifest_hash"] == "exec-hash"
    assert call["name"] == "classify_ticket.openai"


def test_openai_provider_trace_name_prefers_structured_activity_metadata() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(
        default_model="test-model",
        enable_langfuse=True,
        instructor_client=client,
    )

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        metadata={
            "typeflux": {
                "activity_name": "classify_ticket",
                "join": {"activity_manifest_hash": "manifest-hash"},
            },
        },
    )

    assert client.completions.calls[0]["name"] == "classify_ticket.openai"


def test_openai_provider_trace_name_falls_back_to_legacy_flat_key() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(
        default_model="test-model",
        enable_langfuse=True,
        instructor_client=client,
    )

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        metadata={"typeflux.activity_name": "legacy_activity"},
    )

    assert client.completions.calls[0]["name"] == "legacy_activity.openai"


def test_openai_rate_limit_captures_retry_after_header() -> None:
    from types import SimpleNamespace

    from typeflux.providers.errors import ProviderRateLimitError
    from typeflux.providers.openai import _classify_provider_error

    error = type("RateLimitError", (Exception,), {})("rate limited")
    error.response = SimpleNamespace(headers={"retry-after": "3.5"}, status_code=429)
    classified = _classify_provider_error(error, provider="openai")
    assert isinstance(classified, ProviderRateLimitError)
    assert classified.retry_after_seconds == 3.5

    no_header = type("RateLimitError", (Exception,), {})("rate limited")
    no_header.status_code = 429
    assert _classify_provider_error(no_header, provider="openai").retry_after_seconds is None


def test_openai_provider_passes_langfuse_prompt_only_when_enabled() -> None:
    prompt = object()

    disabled_client = _FakeInstructorClient()
    disabled_provider = OpenAIProvider(
        default_model="test-model",
        instructor_client=disabled_client,
    )
    disabled_provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        observation_context=langfuse_prompt_context(prompt),
    )

    enabled_client = _FakeInstructorClient()
    enabled_provider = OpenAIProvider(
        default_model="test-model",
        enable_langfuse=True,
        instructor_client=enabled_client,
    )
    enabled_provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        metadata={"typeflux.activity_name": "classify_ticket"},
        observation_context=langfuse_prompt_context(prompt),
    )

    assert "langfuse_prompt" not in disabled_client.completions.calls[0]
    enabled_call = enabled_client.completions.calls[0]
    assert enabled_call["langfuse_prompt"] is prompt
    assert _contains_identity(enabled_call["metadata"], prompt) is False


def test_openai_prefix_composition_sends_reference_before_per_item_turn(tmp_path: Path) -> None:
    # #362: OpenAI has no explicit breakpoint (implicit byte-prefix caching), so the
    # only thing that matters is ORDER. When the executor composes prefix-style, the
    # stable reference document must land ahead of the varying per-item query in the
    # serialized messages so it can enter the implicit cached prefix.
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    pdf = tmp_path / "contract.pdf"
    pdf.write_bytes(b"%PDF-1.4\ncontract")
    contract = ResolvedArtifact(
        group="contract",
        index=0,
        ref=ArtifactRef(
            source={"type": "local_path", "path": str(pdf)},
            kind="document",
            media_type="application/pdf",
        ),
        source_kind="local_path",
        kind="document",
        media_type="application/pdf",
        role="contract",
        sha256=None,
        size_bytes=pdf.stat().st_size,
        local_path=pdf,
    )
    handle = CachedSessionHandle(
        provider="openai",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
    )

    # The message list as the executor's prefix composition produces it:
    # [system, reference attach, per-item turn].
    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You extract citations."),
            ChatMessage(
                role="user",
                content=(TextPart("Contract:"), ArtifactGroupPart(group="contract")),
            ),
            ChatMessage(role="user", content="Find: renewal date"),
        ],
        artifacts=(ResolvedArtifactGroup(name="contract", artifacts=(contract,)),),
        output_schema=Output,
        cached_session=handle,
    )

    sent = client.completions.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "user"]
    # The reference document rides ahead of the varying query (implicit prefix).
    assert sent[1]["content"][0] == {"type": "text", "text": "Contract:"}
    assert sent[1]["content"][1]["type"] == "file"
    assert sent[2]["content"] == "Find: renewal date"


def test_openai_provider_maps_content_parts_and_provider_file_artifacts() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    artifact = ResolvedArtifact(
        group="claim_documents",
        index=0,
        ref=ArtifactRef(
            source={
                "type": "provider_file",
                "provider": "openai",
                "file_id": "file_123",
            },
            kind="provider_file",
            media_type="application/pdf",
        ),
        source_kind="provider_file",
        kind="provider_file",
        media_type="application/pdf",
        role="claim_documents",
        sha256=None,
        size_bytes=None,
    )

    provider.structured_call(
        messages=[
            ChatMessage(
                role="user",
                content=(
                    TextPart("Review the claim."),
                    ArtifactGroupPart(group="claim_documents"),
                ),
            )
        ],
        artifacts=(ResolvedArtifactGroup(name="claim_documents", artifacts=(artifact,)),),
        output_schema=Output,
    )

    assert client.completions.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Review the claim."},
                {"type": "file", "file": {"file_id": "file_123"}},
            ],
        }
    ]


def test_openai_provider_rejects_negative_artifact_index() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    artifacts = tuple(
        ResolvedArtifact(
            group="claim_documents",
            index=index,
            ref=ArtifactRef(
                source={
                    "type": "provider_file",
                    "provider": "openai",
                    "file_id": f"file_{index}",
                },
                kind="provider_file",
                media_type="application/pdf",
            ),
            source_kind="provider_file",
            kind="provider_file",
            media_type="application/pdf",
            role="claim_documents",
            sha256=None,
            size_bytes=None,
        )
        for index in range(2)
    )

    with pytest.raises(ProviderConfigError, match="invalid artifact reference"):
        provider.structured_call(
            messages=[
                ChatMessage(
                    role="user",
                    content=(ArtifactPart("claim_documents[-1]"),),
                )
            ],
            artifacts=(ResolvedArtifactGroup(name="claim_documents", artifacts=artifacts),),
            output_schema=Output,
        )

    assert client.completions.calls == []


def test_openai_provider_maps_local_pdf_artifacts(tmp_path: Path) -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    pdf = tmp_path / "evidence.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    artifact = ResolvedArtifact(
        group="claim_documents",
        index=0,
        ref=ArtifactRef(
            source={"type": "local_path", "path": str(pdf)},
            kind="document",
            media_type="application/pdf",
        ),
        source_kind="local_path",
        kind="document",
        media_type="application/pdf",
        role="claim_documents",
        sha256=None,
        size_bytes=pdf.stat().st_size,
        local_path=pdf,
    )

    provider.structured_call(
        messages=[
            ChatMessage(
                role="user",
                content=(ArtifactGroupPart(group="claim_documents"),),
            )
        ],
        artifacts=(ResolvedArtifactGroup(name="claim_documents", artifacts=(artifact,)),),
        output_schema=Output,
    )

    assert client.completions.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "file",
                    "file": {
                        "filename": "evidence.pdf",
                        "file_data": (
                            "data:application/pdf;base64,"
                            + base64.b64encode(b"%PDF-1.4\nfixture").decode("ascii")
                        ),
                    },
                }
            ],
        }
    ]


def test_openai_provider_maps_image_url_artifacts() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    artifact = ResolvedArtifact(
        group="screenshots",
        index=0,
        ref=ArtifactRef(
            source={"type": "url", "url": "https://example.com/cat.png"},
            kind="image",
            media_type="image/png",
        ),
        source_kind="url",
        kind="image",
        media_type="image/png",
        role="screenshots",
        sha256=None,
        size_bytes=None,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="user", content=(ArtifactGroupPart(group="screenshots"),)),
        ],
        artifacts=(ResolvedArtifactGroup(name="screenshots", artifacts=(artifact,)),),
        output_schema=Output,
    )

    assert client.completions.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/cat.png"},
                }
            ],
        }
    ]


def test_openai_provider_maps_local_image_artifacts(tmp_path: Path) -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)
    image = tmp_path / "diagram.png"
    image.write_bytes(b"\x89PNG\r\nfixture")
    artifact = ResolvedArtifact(
        group="screenshots",
        index=0,
        ref=ArtifactRef(
            source={"type": "local_path", "path": str(image)},
            kind="image",
            media_type="image/png",
        ),
        source_kind="local_path",
        kind="image",
        media_type="image/png",
        role="screenshots",
        sha256=None,
        size_bytes=image.stat().st_size,
        local_path=image,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="user", content=(ArtifactGroupPart(group="screenshots"),)),
        ],
        artifacts=(ResolvedArtifactGroup(name="screenshots", artifacts=(artifact,)),),
        output_schema=Output,
    )

    expected_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\nfixture").decode(
        "ascii"
    )
    assert client.completions.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": expected_url}}],
        }
    ]


@pytest.mark.asyncio
async def test_openai_provider_maps_async_typeflux_call_to_instructor() -> None:
    client = _AsyncFakeInstructorClient()
    provider = OpenAIProvider(
        default_model="test-model",
        instructor_client=client,
        instructor_max_retries=1,
    )

    result = await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="hello", name="tester")],
        output_schema=Output,
        temperature=0,
        metadata={
            "typeflux.activity_name": "classify_ticket",
            "typeflux.activity_execution_manifest_hash": "exec-hash",
            "typeflux": {
                "level": "provider",
                "activity_name": "classify_ticket",
                "join": {"activity_execution_manifest_hash": "exec-hash"},
            },
            "custom": "value",
            "attempt": 2,
            "cached": False,
        },
    )

    call = client.completions.calls[0]
    assert result == Output(label="ok")
    assert call["model"] == "test-model"
    assert call["messages"] == [{"role": "user", "content": "hello", "name": "tester"}]
    assert call["response_model"] is Output
    assert call["max_retries"] == 1
    assert "metadata" not in call
    assert "name" not in call


@pytest.mark.asyncio
async def test_openai_provider_async_passes_trace_name_only_when_langfuse_enabled() -> None:
    client = _AsyncFakeInstructorClient()
    provider = OpenAIProvider(
        default_model="test-model",
        enable_langfuse=True,
        instructor_client=client,
    )

    await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        metadata={
            "typeflux.activity_name": "classify_ticket",
            "typeflux": {"join": {"activity_execution_manifest_hash": "exec-hash"}},
        },
    )

    call = client.completions.calls[0]
    assert call["metadata"]["typeflux"]["join"]["activity_execution_manifest_hash"] == "exec-hash"
    assert call["name"] == "classify_ticket.openai"


def test_openai_provider_disables_instructor_retries_by_default() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)

    provider.structured_call(messages=[], output_schema=Output)

    assert client.completions.calls[0]["max_retries"] == 0
    assert provider.supports_async_structured_call is False


@pytest.mark.asyncio
async def test_openai_provider_async_disables_instructor_retries_by_default() -> None:
    client = _AsyncFakeInstructorClient()
    provider = OpenAIProvider(default_model="test-model", instructor_client=client)

    await provider.async_structured_call(messages=[], output_schema=Output)

    assert client.completions.calls[0]["max_retries"] == 0
    assert provider.supports_async_structured_call is True


def test_openai_provider_uses_plain_openai_by_default(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    fake_instructor = types.SimpleNamespace(
        Mode=types.SimpleNamespace(JSON_SCHEMA="json-schema-mode"),
        from_openai=lambda client, **kwargs: (
            calls.append({"client": client, **kwargs}) or _FakeInstructorClient()
        ),
    )
    fake_openai_module = types.SimpleNamespace(
        OpenAI=lambda **kwargs: {"plain_openai_kwargs": kwargs},
    )
    fake_langfuse_openai_module = types.SimpleNamespace(
        OpenAI=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("default provider construction should not import Langfuse OpenAI")
        ),
    )
    fake_langfuse_module = types.SimpleNamespace(openai=fake_langfuse_openai_module)

    monkeypatch.setitem(sys.modules, "instructor", fake_instructor)
    monkeypatch.setitem(sys.modules, "openai", fake_openai_module)
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse_module)
    monkeypatch.setitem(sys.modules, "langfuse.openai", fake_langfuse_openai_module)

    OpenAIProvider(api_key="test-key")

    assert calls == [
        {
            "client": {"plain_openai_kwargs": {"api_key": "test-key"}},
            "mode": "json-schema-mode",
        }
    ]


@pytest.mark.asyncio
async def test_openai_provider_builds_plain_async_openai_lazily(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.kind = "sync"

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.kind = "async"

    def from_openai(
        client: Any, **kwargs: Any
    ) -> _FakeInstructorClient | _AsyncFakeInstructorClient:
        calls.append({"client": client, **kwargs})
        if client.kind == "async":
            return _AsyncFakeInstructorClient()
        return _FakeInstructorClient()

    fake_instructor = types.SimpleNamespace(
        Mode=types.SimpleNamespace(JSON_SCHEMA="json-schema-mode"),
        from_openai=from_openai,
    )
    fake_openai_module = types.SimpleNamespace(OpenAI=FakeOpenAI, AsyncOpenAI=FakeAsyncOpenAI)
    fake_langfuse_openai_module = types.SimpleNamespace(
        OpenAI=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("default provider construction should not import Langfuse OpenAI")
        ),
        AsyncOpenAI=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("default async provider path should not import Langfuse AsyncOpenAI")
        ),
    )

    monkeypatch.setitem(sys.modules, "instructor", fake_instructor)
    monkeypatch.setitem(sys.modules, "openai", fake_openai_module)
    monkeypatch.setitem(sys.modules, "langfuse.openai", fake_langfuse_openai_module)

    provider = OpenAIProvider(api_key="test-key", base_url="https://example.test")
    await provider.async_structured_call(messages=[], output_schema=Output)

    assert provider.supports_async_structured_call is True
    assert [call["client"].kind for call in calls] == ["sync", "async"]
    assert calls[1]["client"].kwargs == {
        "api_key": "test-key",
        "base_url": "https://example.test",
    }
    assert calls[1]["mode"] == "json-schema-mode"


@pytest.mark.asyncio
async def test_openai_provider_async_lazy_client_init_does_not_block_event_loop(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.kind = "sync"

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            sleep(0.2)
            self.kwargs = kwargs
            self.kind = "async"

    def from_openai(
        client: Any, **kwargs: Any
    ) -> _FakeInstructorClient | _AsyncFakeInstructorClient:
        calls.append({"client": client, **kwargs})
        if client.kind == "async":
            sleep(0.2)
            return _AsyncFakeInstructorClient()
        return _FakeInstructorClient()

    fake_instructor = types.SimpleNamespace(
        Mode=types.SimpleNamespace(JSON_SCHEMA="json-schema-mode"),
        from_openai=from_openai,
    )
    fake_openai_module = types.SimpleNamespace(OpenAI=FakeOpenAI, AsyncOpenAI=FakeAsyncOpenAI)
    monkeypatch.setitem(sys.modules, "instructor", fake_instructor)
    monkeypatch.setitem(sys.modules, "openai", fake_openai_module)

    provider = OpenAIProvider(api_key="test-key")
    start = monotonic()
    first_call = asyncio.create_task(
        provider.async_structured_call(messages=[], output_schema=Output)
    )
    second_call = asyncio.create_task(
        provider.async_structured_call(messages=[], output_schema=Output)
    )
    await asyncio.sleep(0.01)

    assert monotonic() - start < 0.2
    assert await first_call == Output(label="ok")
    assert await second_call == Output(label="ok")
    assert [call["client"].kind for call in calls] == ["sync", "async"]


def test_openai_provider_uses_langfuse_openai_when_enabled(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    fake_instructor = types.SimpleNamespace(
        Mode=types.SimpleNamespace(JSON_SCHEMA="json-schema-mode"),
        from_openai=lambda client, **kwargs: (
            calls.append({"client": client, **kwargs}) or _FakeInstructorClient()
        ),
    )
    fake_openai_module = types.SimpleNamespace(
        OpenAI=lambda **kwargs: {"openai_kwargs": kwargs},
    )
    fake_langfuse_module = types.SimpleNamespace(openai=fake_openai_module)

    monkeypatch.setitem(sys.modules, "instructor", fake_instructor)
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse_module)
    monkeypatch.setitem(sys.modules, "langfuse.openai", fake_openai_module)

    OpenAIProvider(api_key="test-key", enable_langfuse=True)

    assert calls == [
        {
            "client": {"openai_kwargs": {"api_key": "test-key"}},
            "mode": "json-schema-mode",
        }
    ]


def test_openai_provider_requires_langfuse_only_when_enabled(monkeypatch) -> None:
    fake_instructor = types.SimpleNamespace(
        Mode=types.SimpleNamespace(JSON_SCHEMA="json-schema-mode"),
        from_openai=lambda client, **kwargs: _FakeInstructorClient(),
    )
    fake_openai_module = types.SimpleNamespace(OpenAI=lambda **kwargs: {"openai_kwargs": kwargs})
    monkeypatch.setitem(sys.modules, "instructor", fake_instructor)
    monkeypatch.setitem(sys.modules, "openai", fake_openai_module)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"langfuse", "langfuse.openai"}:
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    OpenAIProvider(api_key="test-key")

    with pytest.raises(RuntimeError, match="langfuse is required when enable_langfuse=True"):
        OpenAIProvider(enable_langfuse=True)


@pytest.mark.asyncio
async def test_openai_provider_async_requires_async_injected_client() -> None:
    provider = OpenAIProvider(instructor_client=_FakeInstructorClient())

    with pytest.raises(ProviderConfigError, match="does not support async structured calls"):
        await provider.async_structured_call(messages=[], output_schema=Output)


# Status-code classification (429/401/400/5xx/408/409), unknown-error wrapping,
# message-text immunity, and sync ValidationError passthrough are covered for
# all providers in test_provider_error_classification.py; the SDK error-type
# name mappings below are OpenAI-specific.
def test_openai_provider_classifies_rate_limit_by_sdk_error_type() -> None:
    provider = OpenAIProvider(
        instructor_client=_FailingInstructorClient(_named_error("RateLimitError"))
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        provider.structured_call(messages=[], output_schema=Output)

    assert exc_info.value.retryable is True


@pytest.mark.parametrize("error_name", ["AuthenticationError", "PermissionDeniedError"])
def test_openai_provider_classifies_auth_error_by_sdk_error_type(error_name: str) -> None:
    provider = OpenAIProvider(instructor_client=_FailingInstructorClient(_named_error(error_name)))

    with pytest.raises(ProviderAuthError) as exc_info:
        provider.structured_call(messages=[], output_schema=Output)

    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "error_name", ["BadRequestError", "NotFoundError", "UnprocessableEntityError"]
)
def test_openai_provider_classifies_config_error_by_sdk_error_type(error_name: str) -> None:
    provider = OpenAIProvider(instructor_client=_FailingInstructorClient(_named_error(error_name)))

    with pytest.raises(ProviderConfigError) as exc_info:
        provider.structured_call(messages=[], output_schema=Output)

    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "error_name", ["APIConnectionError", "APITimeoutError", "InternalServerError"]
)
def test_openai_provider_classifies_transient_error_by_sdk_error_type(error_name: str) -> None:
    provider = OpenAIProvider(instructor_client=_FailingInstructorClient(_named_error(error_name)))

    with pytest.raises(ProviderTransientError) as exc_info:
        provider.structured_call(messages=[], output_schema=Output)

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_openai_provider_async_classifies_rate_limit() -> None:
    provider = OpenAIProvider(
        instructor_client=_AsyncFailingInstructorClient(_SdkError("rate limit", 429))
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        await provider.async_structured_call(messages=[], output_schema=Output)

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_openai_provider_async_preserves_validation_error() -> None:
    try:
        Output.model_validate({})
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - defensive assertion.
        raise AssertionError("expected validation error")
    provider = OpenAIProvider(instructor_client=_AsyncFailingInstructorClient(validation_error))

    with pytest.raises(ValidationError) as exc_info:
        await provider.async_structured_call(messages=[], output_schema=Output)

    assert exc_info.value is validation_error


def _named_error(name: str) -> Exception:
    return type(name, (Exception,), {})(name)


class _SdkError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _FailingCompletions:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, **kwargs: Any) -> Output:
        raise self.error

    def create_with_completion(self, **kwargs: Any) -> tuple[Output, Any]:
        raise self.error


class _FailingInstructorClient:
    def __init__(self, error: Exception) -> None:
        completions = _FailingCompletions(error)
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


class _AsyncFailingCompletions:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def create(self, **kwargs: Any) -> Output:
        raise self.error

    async def create_with_completion(self, **kwargs: Any) -> tuple[Output, Any]:
        raise self.error


class _AsyncFailingInstructorClient:
    def __init__(self, error: Exception) -> None:
        completions = _AsyncFailingCompletions(error)
        self.completions = completions
        self.chat = type("Chat", (), {"completions": completions})()


# --- Provider session caching (#60, phase 4: OpenAI implicit prefix) --------


def test_openai_declares_prefix_session_cache() -> None:
    assert OpenAIProvider.supports_session_cache is True
    assert OpenAIProvider.session_cache_style == "prefix"


def test_prepare_cached_session_is_a_logical_marker_no_api_call() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="gpt-4o-mini", instructor_client=client)

    handle = provider.prepare_cached_session(
        messages=[ChatMessage(role="system", content="stable")],
        model="gpt-4o-mini",
        identity_hash="idhash",
        ttl_seconds=300,
    )

    assert handle.supported is True
    assert handle.provider == "openai"
    assert handle.cache_id is None  # implicit cache, no object
    assert handle.identity_hash == "idhash"
    assert handle.model == "gpt-4o-mini"
    # No warm-up call was made.
    assert client.completions.calls == []
    provider.release_cached_session(handle)  # no-op, must not raise


def test_structured_call_accepts_cached_session_without_changing_request() -> None:
    client = _FakeInstructorClient()
    provider = OpenAIProvider(default_model="gpt-4o-mini", instructor_client=client)
    handle = CachedSessionHandle(provider="openai", identity_hash="h", supported=True)

    result = provider.structured_call(
        messages=[ChatMessage(role="user", content="hi")],
        output_schema=Output,
        cached_session=handle,
    )

    assert isinstance(result, Output)
    # Implicit caching: the request is unchanged by the handle.
    assert "cached_content" not in client.completions.calls[0]


def test_openai_usage_parses_cached_prompt_tokens() -> None:
    client = _FakeInstructorClient()
    client.completions.usage = types.SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=20,
        total_tokens=1020,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=768),
    )
    provider = OpenAIProvider(default_model="gpt-4o-mini", instructor_client=client)
    captured: list[ProviderUsage] = []

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hi")],
        output_schema=Output,
        usage_sink=captured.append,
    )

    assert captured[0].cache_read_tokens == 768
    assert captured[0].cache_hit is True
    assert captured[0].usage_details()["cache_read"] == 768
