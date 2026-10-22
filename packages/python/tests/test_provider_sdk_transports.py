"""Exercise real optional SDK serialization and parsing without contacting providers."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from typeflux.core.contracts import ChatMessage
from typeflux.providers import AnthropicProvider, GeminiProvider, OpenAIProvider
from typeflux.providers.errors import ProviderConfigError


class Output(BaseModel):
    label: str


@pytest.mark.parametrize("sdk_name", ["anthropic", "gemini", "openai"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_structured_call_through_real_sdk_transport(sdk_name, asynchronous):
    requests = []
    if sdk_name == "anthropic":
        sdk = pytest.importorskip("anthropic")
        http = pytest.importorskip("httpx2")
        response = {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": '{"label":"ok"}'}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 4},
        }
    elif sdk_name == "openai":
        sdk = pytest.importorskip("openai")
        http = pytest.importorskip("httpx2")
        response = {
            "id": "chatcmpl_fixture",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"label":"ok"}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        }
    else:
        sdk = pytest.importorskip("google.genai")
        http = pytest.importorskip("httpx")
        response = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": '{"label":"ok"}'}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 4},
        }

    def handle(request):
        requests.append(request)
        return http.Response(200, json=response)

    sync_http = http.Client(transport=http.MockTransport(handle))
    async_http = http.AsyncClient(transport=http.MockTransport(handle))
    if sdk_name == "anthropic":
        sync_sdk = sdk.Anthropic(api_key="test-key", http_client=sync_http, max_retries=0)
        async_sdk = sdk.AsyncAnthropic(api_key="test-key", http_client=async_http, max_retries=0)
        provider = AnthropicProvider(anthropic_client=sync_sdk, async_anthropic_client=async_sdk)
    elif sdk_name == "openai":
        sync_sdk = sdk.OpenAI(api_key="test-key", http_client=sync_http, max_retries=0)
        async_sdk = sdk.AsyncOpenAI(api_key="test-key", http_client=async_http, max_retries=0)
        provider = OpenAIProvider(openai_client=async_sdk if asynchronous else sync_sdk)
    else:
        sync_sdk = sdk.Client(
            api_key="test-key",
            http_options={"httpx_client": sync_http, "httpx_async_client": async_http},
        )
        async_sdk = sync_sdk.aio
        # GeminiProvider uses the full client's .aio surface for async requests.
        provider = GeminiProvider(genai_client=sync_sdk, async_genai_client=sync_sdk)

    usage = []
    try:
        kwargs = {
            "messages": [ChatMessage(role="user", content="Return the label ok.")],
            "output_schema": Output,
            "usage_sink": usage.append,
        }
        if asynchronous:
            result = await provider.async_structured_call(**kwargs)
        else:
            result = provider.structured_call(**kwargs)
        assert result.model_dump() == {"label": "ok"}
        assert len(usage) == 1
        assert usage[0].input_tokens == 5
        assert usage[0].output_tokens == 4
        assert len(requests) == 1
        assert requests[0].method == "POST"
        payload = json.loads(requests[0].content)
        if sdk_name == "anthropic":
            assert requests[0].url.path.endswith("/messages")
            assert payload["messages"][0]["role"] == "user"
            schema = payload["output_config"]["format"]["schema"]
        elif sdk_name == "openai":
            assert requests[0].url.path.endswith("/chat/completions")
            assert payload["messages"][0]["role"] == "user"
            schema = payload["response_format"]["json_schema"]["schema"]
        else:
            assert requests[0].url.path.endswith(":generateContent")
            assert payload["contents"][0]["role"] == "user"
            schema = payload["generationConfig"]["responseSchema"]
        assert "label" in schema["properties"]
        assert schema["required"] == ["label"]
    finally:
        sync_sdk.close()
        if sdk_name != "gemini":
            await async_sdk.close()
        else:
            await async_sdk.aclose()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("reason", ["length", "content_filter", "refusal", "empty"])
@pytest.mark.asyncio
async def test_openai_native_parser_rejects_unusable_output(asynchronous, reason):
    sdk = pytest.importorskip("openai")
    http = pytest.importorskip("httpx2")
    requests = []

    def handle(request):
        requests.append(request)
        return http.Response(
            200,
            json={
                "id": "chatcmpl_fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": reason
                        if reason in {"length", "content_filter"}
                        else "stop",
                        "message": {
                            "role": "assistant",
                            "content": None if reason == "empty" else '{"label":"ok"}',
                            "refusal": "private provider refusal" if reason == "refusal" else None,
                        },
                    }
                ],
            },
        )

    client = (
        sdk.AsyncOpenAI(
            api_key="test-key",
            http_client=http.AsyncClient(transport=http.MockTransport(handle)),
            max_retries=0,
        )
        if asynchronous
        else sdk.OpenAI(
            api_key="test-key",
            http_client=http.Client(transport=http.MockTransport(handle)),
            max_retries=0,
        )
    )
    provider = OpenAIProvider(openai_client=client)
    usage = []
    try:
        with pytest.raises(ProviderConfigError) as error:
            kwargs = {
                "messages": [ChatMessage(role="user", content="Return ok")],
                "output_schema": Output,
                "usage_sink": usage.append,
            }
            if asynchronous:
                await provider.async_structured_call(**kwargs)
            else:
                provider.structured_call(**kwargs)
        assert "private provider refusal" not in str(error.value)
        assert usage == []
        assert len(requests) == 1
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()
