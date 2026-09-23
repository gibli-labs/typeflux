from __future__ import annotations

import os
from typing import Any

import pytest

from typeflux.core.contracts import PromptRef
from typeflux.prompts.errors import (
    PromptNotFoundError,
    PromptRegistryConfigError,
    PromptRegistryUnavailableError,
)
from typeflux.prompts.langsmith import LangSmithPromptRegistry


def _chat_manifest() -> dict[str, Any]:
    return {
        "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
        "kwargs": {
            "messages": [
                {
                    "id": ["langchain", "prompts", "chat", "SystemMessagePromptTemplate"],
                    "kwargs": {
                        "prompt": {
                            "kwargs": {"template": "You are {role}.", "template_format": "f-string"}
                        }
                    },
                },
                {
                    "id": ["langchain", "prompts", "chat", "HumanMessagePromptTemplate"],
                    "kwargs": {
                        "prompt": {
                            "kwargs": {"template": "{{question}}", "template_format": "mustache"}
                        }
                    },
                },
            ]
        },
    }


def _text_manifest() -> dict[str, Any]:
    return {
        "id": ["langchain", "prompts", "prompt", "PromptTemplate"],
        "kwargs": {"template": "Summarize {text}.", "template_format": "f-string"},
    }


class _FakeCommit:
    def __init__(self, manifest: dict[str, Any], commit_hash: str = "commit-abc123") -> None:
        self.manifest = manifest
        self.commit_hash = commit_hash


class _FakeLangSmith:
    def __init__(self, commit: Any = None, error: Exception | None = None) -> None:
        self._commit = commit
        self._error = error
        self.calls: list[str] = []

    def pull_prompt_commit(self, identifier: str, **_: Any) -> Any:
        self.calls.append(identifier)
        if self._error is not None:
            raise self._error
        return self._commit


def test_resolves_chat_prompt_preserving_roles_and_converting_format() -> None:
    client = _FakeLangSmith(commit=_FakeCommit(_chat_manifest()))
    registry = LangSmithPromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("triage", label="production"))

    assert client.calls == ["triage:production"]
    roles = [(m.role, m.content) for m in resolved.messages]
    assert roles == [
        ("system", "You are {{role}}."),  # f-string converted
        ("user", "{{question}}"),  # mustache passed through
    ]
    assert resolved.resolved_version == "commit-abc123"
    assert resolved.metadata["langsmith.prompt_type"] == "chat"
    assert resolved.metadata["langsmith.prompt_commit"] == "commit-abc123"


def test_resolves_text_prompt_as_single_user_message() -> None:
    client = _FakeLangSmith(commit=_FakeCommit(_text_manifest()))
    registry = LangSmithPromptRegistry(client=client)

    resolved = registry.resolve(PromptRef("summarize"))

    assert client.calls == ["summarize"]
    assert [(m.role, m.content) for m in resolved.messages] == [("user", "Summarize {{text}}.")]
    assert resolved.metadata["langsmith.prompt_type"] == "text"


def test_version_selector_maps_to_identifier_suffix() -> None:
    client = _FakeLangSmith(commit=_FakeCommit(_text_manifest()))
    registry = LangSmithPromptRegistry(client=client)

    registry.resolve(PromptRef("summarize", version=5))

    assert client.calls == ["summarize:5"]


def test_missing_prompt_maps_to_not_found() -> None:
    class LangSmithNotFoundError(Exception):
        status_code = 404

    client = _FakeLangSmith(error=LangSmithNotFoundError("missing"))
    registry = LangSmithPromptRegistry(client=client)

    with pytest.raises(PromptNotFoundError):
        registry.resolve(PromptRef("nope", label="production"))


def test_network_oserror_is_retryable_unavailable() -> None:
    # A plain OSError subclass carries none of the name tokens but is transient.
    client = _FakeLangSmith(error=BrokenPipeError("broken pipe"))
    registry = LangSmithPromptRegistry(client=client)

    with pytest.raises(PromptRegistryUnavailableError) as exc_info:
        registry.resolve(PromptRef("svc", label="production"))
    assert exc_info.value.retryable is True


def test_unsupported_template_format_is_a_config_error() -> None:
    manifest = {
        "id": ["langchain", "prompts", "prompt", "PromptTemplate"],
        "kwargs": {"template": "{% if x %}{% endif %}", "template_format": "jinja2"},
    }
    client = _FakeLangSmith(commit=_FakeCommit(manifest))
    registry = LangSmithPromptRegistry(client=client)

    with pytest.raises(PromptRegistryConfigError, match="template_format"):
        registry.resolve(PromptRef("jinja"))


def test_fstring_format_spec_placeholder_is_rejected() -> None:
    # A format spec / conversion can't be rendered by Typeflux; fail loud rather
    # than silently passing '{{amount:.2f}}' through as literal text.
    manifest = {
        "id": ["langchain", "prompts", "prompt", "PromptTemplate"],
        "kwargs": {"template": "Total: {amount:.2f}", "template_format": "f-string"},
    }
    registry = LangSmithPromptRegistry(client=_FakeLangSmith(commit=_FakeCommit(manifest)))

    with pytest.raises(PromptRegistryConfigError, match="not a plain"):
        registry.resolve(PromptRef("invoice"))


def test_fstring_literal_braces_are_preserved() -> None:
    manifest = {
        "id": ["langchain", "prompts", "prompt", "PromptTemplate"],
        "kwargs": {"template": 'JSON {{"k": 1}} for {user}', "template_format": "f-string"},
    }
    registry = LangSmithPromptRegistry(client=_FakeLangSmith(commit=_FakeCommit(manifest)))

    resolved = registry.resolve(PromptRef("doc"))
    assert resolved.messages[0].content == 'JSON {"k": 1} for {{user}}'


def test_null_manifest_field_is_a_clean_config_error() -> None:
    # A present-but-None manifest field must not raise an opaque AttributeError.
    manifest = {
        "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
        "kwargs": {
            "messages": [
                {
                    "id": ["langchain", "prompts", "chat", "HumanMessagePromptTemplate"],
                    "kwargs": {"prompt": None},
                }
            ]
        },
    }
    registry = LangSmithPromptRegistry(client=_FakeLangSmith(commit=_FakeCommit(manifest)))

    with pytest.raises(PromptRegistryConfigError):
        registry.resolve(PromptRef("broken"))


def test_unsupported_message_class_is_a_config_error() -> None:
    manifest = {
        "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
        "kwargs": {
            "messages": [
                {"id": ["langchain", "prompts", "chat", "MessagesPlaceholder"], "kwargs": {}}
            ]
        },
    }
    client = _FakeLangSmith(commit=_FakeCommit(manifest))
    registry = LangSmithPromptRegistry(client=client)

    with pytest.raises(PromptRegistryConfigError, match="chat message"):
        registry.resolve(PromptRef("placeholder"))


@pytest.mark.live
def test_langsmith_live_resolves_real_prompt() -> None:
    """Resolve a real LangSmith prompt against the live SDK.

    Opt-in: set ``LANGSMITH_API_KEY`` and ``TYPEFLUX_LANGSMITH_TEST_PROMPT`` (a
    prompt name, or ``name:tag``/``name:commit``) — in a gitignored ``.env`` or
    the environment. Verifies the manifest parser against LangSmith's actual
    serialization end to end. Run with::

        uv run pytest -m live -k langsmith_live -s
    """
    from typeflux.env import load_env

    load_env()
    if not os.getenv("LANGSMITH_API_KEY") or not os.getenv("TYPEFLUX_LANGSMITH_TEST_PROMPT"):
        pytest.skip("set LANGSMITH_API_KEY and TYPEFLUX_LANGSMITH_TEST_PROMPT")

    name, _, tag = os.environ["TYPEFLUX_LANGSMITH_TEST_PROMPT"].partition(":")
    resolved = LangSmithPromptRegistry().resolve(PromptRef(name=name, label=tag or None))

    assert resolved.messages, "resolved to no messages"
    assert all(message.role in ("system", "user", "assistant") for message in resolved.messages)
    assert resolved.resolved_version, "no commit hash recorded as resolved_version"
    for message in resolved.messages:
        content = message.content if isinstance(message.content, str) else ""
        # Every placeholder must be mustache {{var}} after normalization; a bare
        # single-brace f-string variable would mean the converter missed it.
        assert "{{" in content or "{" not in content, f"un-normalized placeholder: {content!r}"

    # Print for visual inspection under -s.
    for message in resolved.messages:
        print(f"\n[{message.role}] {message.content!r}")
    print(f"\nresolved_version (commit): {resolved.resolved_version}")
    print(f"metadata: {resolved.metadata}")
