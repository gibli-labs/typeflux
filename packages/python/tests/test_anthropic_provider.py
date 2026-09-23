from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from typeflux.core.artifacts import (
    ArtifactGroupPart,
    ArtifactPart,
    ArtifactRef,
    ProviderExtensionPart,
    ResolvedArtifact,
    ResolvedArtifactGroup,
    TextPart,
)
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.providers import AnthropicProvider, ProviderUsage
from typeflux.providers.errors import (
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
)


class Output(BaseModel):
    label: str


def _parsed_message(value: BaseModel, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    # Mirror the real anthropic SDK ParsedMessage shape: parsed_output lives on the
    # parsed text content block (ParsedTextBlock), not on the top-level message.
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(
                type="text",
                text=value.model_dump_json(),
                parsed_output=value,
            )
        ],
    )


class _FakeMessages:
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response
        self.error = error

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response or _parsed_message(Output(label="ok"))


class _FakeCreateOnlyMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text='{"label":"ok"}')])


class _AsyncFakeMessages:
    def __init__(self, response: Any | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response
        self.error = error

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response or _parsed_message(Output(label="ok"))


def _client(messages: Any) -> Any:
    return SimpleNamespace(messages=messages)


def _named_error(name: str) -> Exception:
    return type(name, (Exception,), {})(name)


class _SdkError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_anthropic_provider_maps_typeflux_call_to_messages_parse() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))

    result = provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="hello", name="tester"),
        ],
        output_schema=Output,
        temperature=0,
        metadata={"custom": "value"},
    )

    call = messages.calls[0]
    assert result == Output(label="ok")
    assert call["model"] == "claude-test"
    assert call["max_tokens"] == 4096
    assert call["system"] == "You are precise."
    assert call["messages"] == [{"role": "user", "content": "hello"}]
    assert call["output_format"] is Output
    assert call["temperature"] == 0
    assert "metadata" not in call


def test_anthropic_provider_maps_provider_params() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        provider_params=ProviderParams(
            max_tokens=12000,
            top_p=0.8,
            top_k=40,
            stop=("DONE",),
            timeout=45,
        ),
    )

    call = messages.calls[0]
    assert call["max_tokens"] == 12000
    assert call["top_p"] == 0.8
    assert call["top_k"] == 40
    assert call["stop_sequences"] == ["DONE"]
    assert call["timeout"] == 45


def test_anthropic_provider_reports_max_tokens_truncation() -> None:
    messages = _FakeMessages(response=SimpleNamespace(stop_reason="max_tokens"))
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))

    with pytest.raises(ProviderConfigError, match="max_tokens was exhausted"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
        )


def test_anthropic_provider_uses_parsed_output_from_content_block() -> None:
    # The SDK exposes parsed_output on the parsed text content block, not the
    # top-level message. Use it directly instead of re-parsing the text — which
    # here is deliberately not valid JSON to prove the structured path is used.
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="text", text="not-json", parsed_output=Output(label="from-block"))
        ],
    )
    messages = _FakeMessages(response=response)
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))

    result = provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
    )

    assert result == Output(label="from-block")


def test_anthropic_provider_rejects_unsupported_provider_params() -> None:
    provider = AnthropicProvider(
        default_model="claude-test", anthropic_client=_client(_FakeMessages())
    )

    for params in (
        ProviderParams(seed=7),
        ProviderParams(frequency_penalty=0.5),
        ProviderParams(presence_penalty=0.5),
    ):
        with pytest.raises(ProviderConfigError, match="does not support"):
            provider.structured_call(
                messages=[ChatMessage(role="user", content="hello")],
                output_schema=Output,
                provider_params=params,
            )


@pytest.mark.asyncio
async def test_anthropic_provider_reports_max_tokens_truncation_async() -> None:
    messages = _AsyncFakeMessages(response=SimpleNamespace(stop_reason="max_tokens"))
    provider = AnthropicProvider(
        default_model="claude-test",
        anthropic_client=_client(_FakeMessages()),
        async_anthropic_client=_client(messages),
    )

    with pytest.raises(ProviderConfigError, match="max_tokens was exhausted"):
        await provider.async_structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
        )


def test_anthropic_provider_uses_create_output_config_when_parse_is_unavailable() -> None:
    messages = _FakeCreateOnlyMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))

    result = provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
    )

    call = messages.calls[0]
    assert result == Output(label="ok")
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"]["title"] == "Output"
    assert "output_format" not in call


def test_anthropic_provider_maps_content_parts_and_provider_file_artifacts() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    artifact = _resolved_artifact(
        group="claim_documents",
        source={"type": "provider_file", "provider": "anthropic", "file_id": "file_123"},
        kind="provider_file",
        media_type="application/pdf",
    )

    provider.structured_call(
        messages=[
            ChatMessage(
                role="user",
                content=(
                    TextPart("Review the claim."),
                    ArtifactGroupPart(group="claim_documents"),
                    ProviderExtensionPart(
                        provider="anthropic",
                        payload={"type": "text", "text": "native block"},
                    ),
                ),
            )
        ],
        artifacts=(ResolvedArtifactGroup(name="claim_documents", artifacts=(artifact,)),),
        output_schema=Output,
    )

    assert messages.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Review the claim."},
                {
                    "type": "document",
                    "source": {"type": "file", "file_id": "file_123"},
                },
                {"type": "text", "text": "native block"},
            ],
        }
    ]


def test_anthropic_provider_maps_image_url_artifacts() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    artifact = _resolved_artifact(
        group="screenshots",
        source={"type": "url", "url": "https://example.com/cat.png"},
        kind="image",
        media_type="image/png",
    )

    provider.structured_call(
        messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="screenshots"),))],
        artifacts=(ResolvedArtifactGroup(name="screenshots", artifacts=(artifact,)),),
        output_schema=Output,
    )

    assert messages.calls[0]["messages"][0]["content"] == [
        {
            "type": "image",
            "source": {"type": "url", "url": "https://example.com/cat.png"},
        }
    ]


def test_anthropic_provider_maps_local_image_artifacts(tmp_path: Path) -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    image = tmp_path / "diagram.png"
    image.write_bytes(b"\x89PNG\r\nfixture")
    artifact = _resolved_artifact(
        group="screenshots",
        source={"type": "local_path", "path": str(image)},
        kind="image",
        media_type="image/png",
        local_path=image,
        size_bytes=image.stat().st_size,
    )

    provider.structured_call(
        messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="screenshots"),))],
        artifacts=(ResolvedArtifactGroup(name="screenshots", artifacts=(artifact,)),),
        output_schema=Output,
    )

    assert messages.calls[0]["messages"][0]["content"] == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(b"\x89PNG\r\nfixture").decode("ascii"),
            },
        }
    ]


def test_anthropic_provider_maps_local_pdf_and_text_artifacts(tmp_path: Path) -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    pdf = tmp_path / "evidence.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    notes = tmp_path / "notes.txt"
    notes.write_text("plain text evidence", encoding="utf-8")
    artifacts = (
        _resolved_artifact(
            group="claim_documents",
            source={"type": "local_path", "path": str(pdf)},
            kind="document",
            media_type="application/pdf",
            local_path=pdf,
            size_bytes=pdf.stat().st_size,
        ),
        _resolved_artifact(
            group="claim_documents",
            index=1,
            source={"type": "local_path", "path": str(notes)},
            kind="document",
            media_type="text/plain",
            local_path=notes,
            size_bytes=notes.stat().st_size,
        ),
    )

    provider.structured_call(
        messages=[ChatMessage(role="user", content=(ArtifactGroupPart(group="claim_documents"),))],
        artifacts=(ResolvedArtifactGroup(name="claim_documents", artifacts=artifacts),),
        output_schema=Output,
    )

    assert messages.calls[0]["messages"][0]["content"] == [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(b"%PDF-1.4\nfixture").decode("ascii"),
            },
        },
        {"type": "text", "text": "plain text evidence"},
    ]


def test_anthropic_system_text_reference_rides_in_cached_system_block() -> None:
    # #362 pin: a role="system" reference with TEXT content is folded into the
    # cached system block (the cheapest always-stable thing to cache) — it works.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic", identity_hash="h", supported=True, style="prefix"
    )

    provider.structured_call(
        messages=[
            ChatMessage(
                role="system",
                content=(TextPart("You are precise."), TextPart("Reference: contract clause.")),
            ),
            ChatMessage(role="user", content="per-item"),
        ],
        output_schema=Output,
        cached_session=handle,
    )

    assert messages.calls[0]["system"] == [
        {
            "type": "text",
            "text": "You are precise.\n\nReference: contract clause.",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_anthropic_document_bearing_system_message_raises() -> None:
    # #362 pin (existing behavior): a DOCUMENT in a system message has no text to
    # fold and cannot ride the text-only system block — it raises. Document
    # references must attach as user turns to join the prefix.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    artifact = _resolved_artifact(
        group="contract",
        source={"type": "local_path", "path": "/data/contract.pdf"},
        kind="document",
        media_type="application/pdf",
    )

    with pytest.raises(ProviderConfigError, match="only supports text content in system messages"):
        provider.structured_call(
            messages=[
                ChatMessage(role="system", content=(ArtifactGroupPart(group="contract"),)),
                ChatMessage(role="user", content="q"),
            ],
            artifacts=(ResolvedArtifactGroup(name="contract", artifacts=(artifact,)),),
            output_schema=Output,
        )

    assert messages.calls == []


def test_anthropic_provider_rejects_provider_file_for_other_provider() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    artifact = _resolved_artifact(
        group="claim_documents",
        source={"type": "provider_file", "provider": "openai", "file_id": "file_123"},
        kind="provider_file",
        media_type="application/pdf",
    )

    with pytest.raises(ProviderConfigError, match="not for Anthropic"):
        provider.structured_call(
            messages=[
                ChatMessage(role="user", content=(ArtifactGroupPart(group="claim_documents"),))
            ],
            artifacts=(ResolvedArtifactGroup(name="claim_documents", artifacts=(artifact,)),),
            output_schema=Output,
        )

    assert messages.calls == []


def test_anthropic_provider_rejects_unsupported_artifacts() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    artifact = _resolved_artifact(
        group="recordings",
        source={"type": "url", "url": "https://example.com/audio.mp3"},
        kind="audio",
        media_type="audio/mpeg",
    )

    with pytest.raises(ProviderConfigError, match="cannot attach"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content=(ArtifactPart("recordings"),))],
            artifacts=(ResolvedArtifactGroup(name="recordings", artifacts=(artifact,)),),
            output_schema=Output,
        )

    assert messages.calls == []


@pytest.mark.asyncio
async def test_anthropic_provider_maps_async_typeflux_call() -> None:
    messages = _AsyncFakeMessages()
    provider = AnthropicProvider(
        default_model="claude-test",
        anthropic_client=_client(_FakeMessages()),
        async_anthropic_client=_client(messages),
    )

    result = await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
    )

    assert result == Output(label="ok")
    assert messages.calls[0]["model"] == "claude-test"


def test_anthropic_provider_async_only_injection_builds_no_sync_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Injecting only the async client must not import the SDK or build a real
    # sync client; blocking the import proves neither happens.
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)

    provider = AnthropicProvider(
        default_model="claude-test",
        async_anthropic_client=_client(_AsyncFakeMessages()),
    )

    assert provider.supports_async_structured_call is True
    with pytest.raises(ProviderConfigError, match="does not support sync structured calls"):
        provider.structured_call(
            messages=[ChatMessage(role="user", content="hello")],
            output_schema=Output,
        )


@pytest.mark.asyncio
async def test_anthropic_provider_async_only_injection_supports_async_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)
    messages = _AsyncFakeMessages()
    provider = AnthropicProvider(
        default_model="claude-test",
        async_anthropic_client=_client(messages),
    )

    result = await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
    )

    assert result == Output(label="ok")
    assert messages.calls[0]["model"] == "claude-test"


def test_anthropic_provider_yaml_shaped_max_tokens_overrides_constructor_default() -> None:
    # Mirrors what the YAML runtime passes: default_provider_params built from
    # runtime.provider.params must win over the constructor's 4096 default.
    provider = AnthropicProvider(
        default_model="claude-test",
        anthropic_client=_client(_FakeMessages()),
        default_provider_params=ProviderParams(model="claude-test", max_tokens=16000),
    )

    assert provider.max_tokens == 16000
    assert provider.default_provider_params.max_tokens == 16000

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
    )
    assert provider._client.messages.calls[0]["max_tokens"] == 16000


def test_anthropic_provider_reports_usage_to_sink() -> None:
    response = _parsed_message(Output(label="ok"))
    response.usage = SimpleNamespace(input_tokens=120, output_tokens=45)
    provider = AnthropicProvider(
        default_model="claude-test",
        anthropic_client=_client(_FakeMessages(response=response)),
    )
    captured: list[ProviderUsage] = []

    result = provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=captured.append,
    )

    assert result == Output(label="ok")
    assert captured == [ProviderUsage(input_tokens=120, output_tokens=45, model="claude-test")]
    assert captured[0].usage_details() == {"input": 120, "output": 45, "total": 165}


@pytest.mark.asyncio
async def test_anthropic_provider_reports_usage_to_sink_async() -> None:
    response = _parsed_message(Output(label="ok"))
    response.usage = SimpleNamespace(input_tokens=7, output_tokens=3)
    provider = AnthropicProvider(
        default_model="claude-test",
        async_anthropic_client=_client(_AsyncFakeMessages(response=response)),
    )
    captured: list[ProviderUsage] = []

    result = await provider.async_structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=captured.append,
    )

    assert result == Output(label="ok")
    assert captured == [ProviderUsage(input_tokens=7, output_tokens=3, model="claude-test")]


def test_anthropic_provider_skips_sink_when_result_has_no_usage() -> None:
    provider = AnthropicProvider(
        default_model="claude-test",
        anthropic_client=_client(_FakeMessages()),
    )
    captured: list[ProviderUsage] = []

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=captured.append,
    )

    assert captured == []


# --- Provider session caching (#60, phase 2: prefix style) ------------------


def test_anthropic_declares_prefix_session_cache() -> None:
    assert AnthropicProvider.supports_session_cache is True
    assert AnthropicProvider.session_cache_style == "prefix"


def test_prepare_cached_session_returns_supported_prefix_handle() -> None:
    provider = AnthropicProvider(
        default_model="claude-test", anthropic_client=_client(_FakeMessages())
    )

    handle = provider.prepare_cached_session(
        messages=[ChatMessage(role="system", content="stable")],
        model="claude-test",
        identity_hash="idhash",
        ttl_seconds=300,
    )

    assert handle.provider == "anthropic"
    assert handle.supported is True
    assert handle.style == "prefix"
    assert handle.identity_hash == "idhash"
    assert handle.cache_id is None  # prefix style: no server-side cache object
    assert handle.model == "claude-test"
    # ttl is provider-managed for prefix style; don't claim a TTL never sent (#60 review).
    assert handle.ttl_seconds is None
    assert handle.created_at is None  # runtime stamps it (replay-safe)
    provider.release_cached_session(handle)  # no-op, must not raise


def test_structured_call_marks_prefix_cache_control_when_session_active() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(provider="anthropic", identity_hash="h", supported=True)

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="reference files here"),  # stable prefix turn
            ChatMessage(role="user", content="the per-item question"),  # variable input
        ],
        output_schema=Output,
        cached_session=handle,
    )

    call = messages.calls[0]
    # System becomes a block list carrying the cache breakpoint.
    assert call["system"] == [
        {"type": "text", "text": "You are precise.", "cache_control": {"type": "ephemeral"}}
    ]
    # The breakpoint also lands on the last block of the prefix turn (2nd-to-last
    # message); the final per-item message is left unmarked (variable).
    prefix_turn, item_turn = call["messages"]
    assert prefix_turn["content"] == [
        {"type": "text", "text": "reference files here", "cache_control": {"type": "ephemeral"}}
    ]
    assert item_turn["content"] == "the per-item question"


def test_prefix_stable_messages_marks_reference_turn_not_the_query() -> None:
    # #362: when the handle carries prefix_stable_messages=k>0, the breakpoint lands
    # on conversation[k-1] (the last stable reference turn), NOT the default -2. Here
    # the reference doc leads the conversation, then the varying per-item query.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="Contract text (stable reference)."),  # k-1 == 0
            ChatMessage(role="user", content="the per-item question"),  # variable input
        ],
        output_schema=Output,
        cached_session=handle,
    )

    reference_turn, item_turn = messages.calls[0]["messages"]
    assert reference_turn["content"] == [
        {
            "type": "text",
            "text": "Contract text (stable reference).",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # The per-item turn is never marked (it varies across items).
    assert item_turn["content"] == "the per-item question"


def test_prefix_stable_messages_multi_turn_marks_only_the_reference_span() -> None:
    # #362 documented outcome: with a multi-turn prompt shape
    # [system, stable_user, per_item_user] plus a reference artifact, the executor
    # composes [system, reference attach, stable_user, per_item_user] and the handle
    # carries k=1 (one reference turn). The breakpoint therefore lands on
    # conversation[0] (the reference doc): the cached prefix is system + reference.
    # A stable rendered user turn that follows the reference is NOT covered by the
    # breakpoint (Anthropic caches up to and including the marked block only) — the
    # reference documents are the guaranteed stable span, not arbitrary later turns.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="Reference contract clause."),  # conversation[0]
            ChatMessage(role="user", content="Shared rubric turn."),  # conversation[1]
            ChatMessage(role="user", content="the per-item question"),  # conversation[2]
        ],
        output_schema=Output,
        cached_session=handle,
    )

    reference_turn, rubric_turn, item_turn = messages.calls[0]["messages"]
    assert reference_turn["content"] == [
        {
            "type": "text",
            "text": "Reference contract clause.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # The later stable turn and the per-item turn are left unmarked (plain strings).
    assert rubric_turn["content"] == "Shared rubric turn."
    assert item_turn["content"] == "the per-item question"


def test_prefix_stable_messages_none_falls_back_to_legacy_second_to_last() -> None:
    # #362: k None/0 keeps the proven conversation[-2] contract byte-for-byte.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=None,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="instructions turn"),  # -2 (legacy)
            ChatMessage(role="user", content="per-item"),
        ],
        output_schema=Output,
        cached_session=handle,
    )

    prefix_turn, item_turn = messages.calls[0]["messages"]
    assert prefix_turn["content"] == [
        {"type": "text", "text": "instructions turn", "cache_control": {"type": "ephemeral"}}
    ]
    assert item_turn["content"] == "per-item"


def test_prefix_stable_messages_clamps_and_never_marks_final_message() -> None:
    # #362: a count that would reach/exceed the final (variable) message clamps back
    # to conversation[-2] rather than marking the per-item turn. Here k=2 with only a
    # single stable turn + the per-item turn (len==2): k-1==1 is the final message,
    # so it clamps to index 0.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=2,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="stable"),
            ChatMessage(role="user", content="per-item"),
        ],
        output_schema=Output,
        cached_session=handle,
    )

    prefix_turn, item_turn = messages.calls[0]["messages"]
    assert prefix_turn["content"] == [
        {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}
    ]
    assert item_turn["content"] == "per-item"  # final message untouched


def test_per_item_artifact_shape_marks_only_system_not_the_varying_query() -> None:
    # #698: shape [system, per-item query, per-item artifact attach] with NO reference
    # inputs. The handle flags per_item_artifact_messages=True and carries no reference
    # count, so conversation[-2] (the varying query) must NOT be marked — that would key
    # the conversation cache on per-item content. The system block still caches.
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=None,
        per_item_artifact_messages=True,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="the per-item question"),  # varies per item
            ChatMessage(role="user", content="per-item artifact attach"),  # trailing attach
        ],
        output_schema=Output,
        cached_session=handle,
    )

    call = messages.calls[0]
    # System block is still cached (the shape's remaining benefit).
    assert call["system"] == [
        {"type": "text", "text": "You are precise.", "cache_control": {"type": "ephemeral"}}
    ]
    # Nothing in the conversation is marked — both trailing turns vary.
    query_turn, artifact_turn = call["messages"]
    assert query_turn["content"] == "the per-item question"
    assert artifact_turn["content"] == "per-item artifact attach"
    assert "cache_control" not in str(call["messages"])


def test_per_item_artifact_flag_ignored_when_reference_span_present() -> None:
    # #698: when a reference span IS present (prefix_stable_messages=k>0), it is the
    # authoritative breakpoint; the per_item_artifact_messages flag does not suppress it.
    # The breakpoint still lands on conversation[k-1] (#362 behavior, unchanged).
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
        prefix_stable_messages=1,
        per_item_artifact_messages=True,
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="Reference contract clause."),  # conversation[0]
            ChatMessage(role="user", content="the per-item question"),  # varies
            ChatMessage(role="user", content="per-item artifact attach"),  # trailing attach
        ],
        output_schema=Output,
        cached_session=handle,
    )

    reference_turn, query_turn, artifact_turn = messages.calls[0]["messages"]
    assert reference_turn["content"] == [
        {
            "type": "text",
            "text": "Reference contract clause.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert query_turn["content"] == "the per-item question"
    assert artifact_turn["content"] == "per-item artifact attach"


def test_per_item_artifact_flag_default_false_keeps_legacy_second_to_last() -> None:
    # #698: the proven stable-instructions shape [system, stable_user, per_item] with NO
    # per-item artifacts (flag defaults to False) still marks conversation[-2].
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic",
        identity_hash="h",
        supported=True,
        style="prefix",
    )
    assert handle.per_item_artifact_messages is False  # additive default

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="instructions turn"),  # -2 (legacy)
            ChatMessage(role="user", content="per-item"),
        ],
        output_schema=Output,
        cached_session=handle,
    )

    prefix_turn, item_turn = messages.calls[0]["messages"]
    assert prefix_turn["content"] == [
        {"type": "text", "text": "instructions turn", "cache_control": {"type": "ephemeral"}}
    ]
    assert item_turn["content"] == "per-item"


def test_structured_call_does_not_cache_without_a_session() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="You are precise."),
            ChatMessage(role="user", content="hello"),
        ],
        output_schema=Output,
    )

    call = messages.calls[0]
    # Unchanged behavior: plain-string system, no cache_control anywhere.
    assert call["system"] == "You are precise."
    assert "cache_control" not in str(call["messages"])


def test_fallback_handle_does_not_trigger_caching() -> None:
    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    # supported=False fail-soft handle ⇒ full context, no markers.
    handle = CachedSessionHandle(provider="anthropic", identity_hash="h", supported=False)

    provider.structured_call(
        messages=[ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="x")],
        output_schema=Output,
        cached_session=handle,
    )

    assert messages.calls[0]["system"] == "sys"


def test_anthropic_usage_parses_cache_tokens() -> None:
    response = _parsed_message(Output(label="ok"))
    response.usage = SimpleNamespace(
        input_tokens=12,
        output_tokens=8,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=0,
    )
    provider = AnthropicProvider(
        default_model="claude-test", anthropic_client=_client(_FakeMessages(response=response))
    )
    captured: list[ProviderUsage] = []

    provider.structured_call(
        messages=[ChatMessage(role="user", content="hello")],
        output_schema=Output,
        usage_sink=captured.append,
    )

    usage = captured[0]
    assert usage.cache_read_tokens == 900
    assert usage.cache_write_tokens == 0
    assert usage.cache_hit is True
    assert usage.usage_details()["cache_read"] == 900


# Status-code classification (401/400/500/…) is covered for all providers in
# test_provider_error_classification.py; the SDK error-type name mapping below
# is Anthropic-specific.
def test_anthropic_provider_classifies_rate_limit_by_sdk_error_type() -> None:
    provider = AnthropicProvider(
        anthropic_client=_client(_FakeMessages(error=_named_error("RateLimitError"))),
    )

    with pytest.raises(ProviderRateLimitError):
        provider.structured_call(messages=[], output_schema=Output)


@pytest.mark.parametrize(
    "error",
    [
        _named_error("RateLimitError"),
        _SdkError("invalid api key sk-ant-secret-token", 401),
        _SdkError("request body rejected: claim text for Avery Morgan", 400),
        _SdkError("upstream exploded with user context", 500),
    ],
)
def test_anthropic_provider_error_reasons_are_sanitized(error: Exception) -> None:
    provider = AnthropicProvider(
        anthropic_client=_client(_FakeMessages(error=error)),
    )

    with pytest.raises(ProviderError) as exc_info:
        provider.structured_call(messages=[], output_schema=Output)

    assert str(exc_info.value) == "anthropic structured call failed"
    assert exc_info.value.reason == "anthropic structured call failed"
    assert str(error) not in str(exc_info.value)
    assert exc_info.value.original is error


def test_anthropic_rate_limit_captures_retry_after_header() -> None:
    from types import SimpleNamespace

    from typeflux.providers.anthropic import _classify_provider_error

    error = _SdkError("rate limited", 429)
    error.response = SimpleNamespace(headers={"retry-after": "7"})
    classified = _classify_provider_error(error, provider="anthropic")
    assert isinstance(classified, ProviderRateLimitError)
    assert classified.retry_after_seconds == 7.0

    garbage = _SdkError("rate limited", 429)
    garbage.response = SimpleNamespace(headers={"retry-after": "soon"})
    assert _classify_provider_error(garbage, provider="anthropic").retry_after_seconds is None


# ValidationError passthrough is covered for all providers in
# test_provider_error_classification.py.


def _resolved_artifact(
    *,
    group: str,
    source: dict[str, Any],
    kind: str,
    media_type: str,
    index: int = 0,
    local_path: Path | None = None,
    size_bytes: int | None = None,
) -> ResolvedArtifact:
    return ResolvedArtifact(
        group=group,
        index=index,
        ref=ArtifactRef(source=source, kind=kind, media_type=media_type),
        source_kind=source["type"],
        kind=kind,
        media_type=media_type,
        role=group,
        sha256=None,
        size_bytes=size_bytes,
        local_path=local_path,
    )


def test_anthropic_prefix_marks_last_cacheable_block_skipping_noncacheable() -> None:
    # The cache breakpoint must land on a cacheable block, not blindly on a
    # trailing provider-extension block that would be rejected (#60 review).
    from typeflux.core.artifacts import ProviderExtensionPart, TextPart

    messages = _FakeMessages()
    provider = AnthropicProvider(default_model="claude-test", anthropic_client=_client(messages))
    handle = CachedSessionHandle(
        provider="anthropic", identity_hash="h", supported=True, style="prefix"
    )

    provider.structured_call(
        messages=[
            ChatMessage(role="system", content="sys"),
            ChatMessage(
                role="user",
                content=(
                    TextPart(text="stable files"),
                    ProviderExtensionPart(
                        provider="anthropic", payload={"type": "redacted_thinking", "data": "z"}
                    ),
                ),
            ),
            ChatMessage(role="user", content="per-item"),
        ],
        output_schema=Output,
        cached_session=handle,
    )

    prefix_blocks = messages.calls[0]["messages"][0]["content"]
    text_block = next(b for b in prefix_blocks if b.get("type") == "text")
    other_block = next(b for b in prefix_blocks if b.get("type") == "redacted_thinking")
    assert text_block.get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in other_block
