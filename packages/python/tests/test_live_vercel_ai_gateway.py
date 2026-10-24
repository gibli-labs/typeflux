"""One explicitly selected, bounded gateway request; never part of offline verification."""

from __future__ import annotations

import asyncio
import os
from typing import Literal

import pytest
from pydantic import BaseModel

from typeflux.core.contracts import ChatMessage, ProviderParams
from typeflux.providers import VercelAIGatewayProvider
from typeflux.providers.errors import ProviderError


class GatewaySmokeOutput(BaseModel):
    label: Literal["ok"]


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_vercel_ai_gateway():
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1" or not os.getenv("VERCEL_API_KEY"):
        pytest.skip("requires TYPEFLUX_RUN_LIVE=1 and VERCEL_API_KEY in this execution context")
    usage = []
    failure = None
    try:
        # No dotenv lookup for availability, SDK retries, repair loop or fallback request.
        provider = VercelAIGatewayProvider(
            default_model="openai/gpt-4o-mini", api_key=os.environ["VERCEL_API_KEY"]
        )
        result = await asyncio.wait_for(
            provider.async_structured_call(
                messages=[ChatMessage(role="user", content='Return {"label":"ok"}.')],
                output_schema=GatewaySmokeOutput,
                provider_params=ProviderParams(max_tokens=64, timeout=30),
                usage_sink=usage.append,
            ),
            timeout=30,
        )
    except Exception as exc:
        # Keep only our typed error category and numeric status, never SDK text.
        category = type(exc).__name__ if isinstance(exc, ProviderError) else "unclassified"
        status = exc.status_code if isinstance(exc, ProviderError) else None
        failure = f"Vercel AI Gateway live request failed ({category}, HTTP {status}); no retry"
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    assert usage
    print(
        "Gateway smoke",
        {
            "provider": "vercel-ai-gateway",
            "model": usage[0].model,
            **usage[0].response_identity(),
            **usage[0].usage_details(),
        },
    )
    assert result.label == "ok"
    assert usage and usage[0].model == "openai/gpt-4o-mini"
    assert usage[0].input_tokens is not None and usage[0].output_tokens is not None
