"""Exercise real optional SDK serialization and parsing without contacting providers."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from typeflux.core.contracts import ChatMessage
from typeflux.providers import AnthropicProvider, GeminiProvider


class Output(BaseModel):
    label: str


@pytest.mark.parametrize("sdk_name", ["anthropic", "gemini"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_structured_call_through_real_sdk_transport(sdk_name, asynchronous):
    requests = []
    if sdk_name == "anthropic":
        sdk = pytest.importorskip("anthropic")
        # SDK 1 moved to httpx2; the package also supports the prior SDK 0 range.
        transport_module = "httpx2" if int(sdk.__version__.split(".")[0]) >= 1 else "httpx"
        http = pytest.importorskip(transport_module)
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
        assert result == Output(label="ok")
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
        else:
            assert requests[0].url.path.endswith(":generateContent")
            assert payload["contents"][0]["role"] == "user"
            schema = payload["generationConfig"]["responseSchema"]
        assert "label" in schema["properties"]
        assert schema["required"] == ["label"]
    finally:
        sync_sdk.close()
        if sdk_name == "anthropic":
            await async_sdk.close()
        else:
            await async_sdk.aclose()
