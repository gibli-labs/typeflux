from __future__ import annotations

import os

import pytest

from examples.support_triage_langfuse.main import PROMPTS, bootstrap_langfuse, langfuse_registry
from typeflux.core import PromptRef
from typeflux.env import load_env


@pytest.mark.live
def test_langfuse_prompts_bootstrap_and_resolve(request: pytest.FixtureRequest) -> None:
    load_env()
    if request.config.option.markexpr != "live":
        pytest.skip("live tests run only when explicitly selected with -m live")
    if os.getenv("TYPEFLUX_RUN_LIVE") != "1":
        pytest.skip("TYPEFLUX_RUN_LIVE must be 1")
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        pytest.skip("Langfuse credentials are not set")

    bootstrap_langfuse()
    registry = langfuse_registry()
    label = os.getenv("LANGFUSE_PROMPT_LABEL", "production")

    for _filename, activity in PROMPTS.items():
        resolved = registry.resolve(PromptRef(activity.prompt_ref.name, label=label))
        config = resolved.metadata["langfuse.prompt_config"]
        typeflux = config["typeflux"]

        assert resolved.messages
        assert resolved.resolved_version
        assert config["template_format"] == "mustache"
        assert typeflux["runtime_reads_config"] is False
        assert typeflux["pipeline"]["activity_name"] == activity.name
        assert typeflux["pipeline"]["prompt_ref"] == activity.prompt_ref.name
        assert typeflux["contracts"]["input_model"] == activity.input_type.__name__
        assert typeflux["contracts"]["output_model"] == activity.output_type.__name__
