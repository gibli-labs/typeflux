"""One opt-in synthetic request covering Jev's three text primitives."""

import asyncio
import json
import os

import pytest
from pydantic import BaseModel

from typeflux import ChatMessage, ProviderParams, TypeSafeProvider
from typeflux.providers.errors import ProviderError


class Decision(BaseModel):
    route: dict
    urgency: dict
    refund: dict


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_typesafe_decisions():
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1" or not os.getenv("TYPESAFE_API_KEY"):
        pytest.skip("requires TYPEFLUX_RUN_LIVE=1 and TYPESAFE_API_KEY")
    questions = {
        "route": {
            "type": "choice",
            "instructions": "Route this ticket",
            "criteria": {"billing": "Payments and refunds", "technical": "Software bugs"},
        },
        "urgency": {"type": "score", "instructions": "Urgency", "criteria": ["Routine", "Urgent"]},
        "refund": {"type": "noul", "instructions": "Requests a refund"},
    }
    usage = []
    failure = None
    try:
        provider = TypeSafeProvider(api_key=os.environ["TYPESAFE_API_KEY"])
        result = await asyncio.wait_for(
            provider.async_structured_call(
                messages=[
                    ChatMessage(role="system", content=json.dumps(questions)),
                    ChatMessage(role="user", content="Please refund my duplicate charge."),
                ],
                output_schema=Decision,
                provider_params=ProviderParams(timeout=20),
                usage_sink=usage.append,
            ),
            timeout=25,
        )
    except Exception as exc:
        category = type(exc).__name__ if isinstance(exc, ProviderError) else "unclassified"
        status = exc.status_code if isinstance(exc, ProviderError) else None
        failure = f"TypeSafe live request failed ({category}, HTTP {status}); no retry"
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    assert isinstance(result, Decision)
    assert result.route["choice"] == "billing"
    assert result.refund["noul"] > 0.5
    assert 0 <= result.urgency["score"] <= 1
    assert usage and usage[0].input_tokens is not None
    print(
        "TypeSafe smoke",
        {
            "provider": "typesafe",
            "model": usage[0].model,
            **usage[0].response_identity(),
            **usage[0].usage_details(),
        },
    )
