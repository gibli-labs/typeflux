from __future__ import annotations

import os

import pytest

from examples.contract_risk_review.main import LANGSMITH_PROMPT_NAME, bootstrap_langsmith
from typeflux.core import PromptRef
from typeflux.env import load_env
from typeflux.prompts.langsmith import LangSmithPromptRegistry


@pytest.mark.live
def test_langsmith_prompt_bootstraps_and_resolves(request: pytest.FixtureRequest) -> None:
    load_env()
    if request.config.option.markexpr != "live":
        pytest.skip("live tests run only when explicitly selected with -m live")
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1":
        pytest.skip("TYPEFLUX_RUN_LIVE must be 1")
    if not os.getenv("LANGSMITH_API_KEY"):
        pytest.skip("LANGSMITH_API_KEY is not set")

    # Pushing builds a LangChain ChatPromptTemplate; only do it where
    # langchain-core is installed. The worker that resolves the prompt does not
    # need it, so fall back to resolving whatever commit is already in LangSmith.
    try:
        import langchain_core  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        bootstrap_langsmith()

    registry = LangSmithPromptRegistry()
    resolved = registry.resolve(PromptRef(LANGSMITH_PROMPT_NAME))

    assert [message.role for message in resolved.messages] == ["system", "user"]
    assert resolved.resolved_version
    assert resolved.metadata["langsmith.prompt_type"] == "chat"
    assert resolved.metadata["langsmith.prompt_commit"] == resolved.resolved_version

    user = next(message for message in resolved.messages if message.role == "user")
    # Stored in mustache; the registry passes the example's own placeholders
    # through unchanged.
    assert "{{ engagement_id }}" in user.content
