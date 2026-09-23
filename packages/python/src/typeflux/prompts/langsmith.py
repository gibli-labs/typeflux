"""LangSmith-backed prompt registry (#59).

Resolves prompts stored and versioned in LangSmith. LangSmith stores prompts as
LangChain serializations, so the registry parses the commit *manifest* (a plain
JSON dict — no langchain runtime needed) into Typeflux ``ChatMessage`` templates,
preserving roles. Each message's template is normalized to Typeflux's mustache
``{{var}}`` syntax: ``mustache`` templates pass through, ``f-string`` ``{var}``
templates are converted (honoring ``{{``/``}}`` literal-brace escapes).

Selection is by LangSmith tag/commit via the prompt ref's ``label`` (e.g.
``production`` or a commit hash); the resolved commit hash is recorded as the
``resolved_version`` so it lands in the activity and execution manifests.
"""

from __future__ import annotations

import re
from typing import Any

from typeflux.core.contracts import ChatMessage, PromptRef, ResolvedPrompt
from typeflux.env import load_env
from typeflux.prompts.errors import (
    PromptNotFoundError,
    PromptRegistryAuthError,
    PromptRegistryConfigError,
    PromptRegistryUnavailableError,
    PromptResolutionError,
)
from typeflux.providers._shared import is_network_or_timeout_error, status_code

_ROLE_BY_MESSAGE_CLASS = {
    "SystemMessagePromptTemplate": "system",
    "HumanMessagePromptTemplate": "user",
    "AIMessagePromptTemplate": "assistant",
}
_PASSTHROUGH_FORMATS = {"mustache"}
_CONVERT_FORMATS = {"f-string", "fstring"}


class LangSmithPromptRegistry:
    def __init__(
        self,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        load_env()
        self._client = client or _langsmith_client(api_url=api_url, api_key=api_key)

    def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        identifier = _prompt_identifier(ref)
        try:
            commit = self._client.pull_prompt_commit(identifier)
        except Exception as exc:  # noqa: BLE001 - classified into typed prompt errors.
            raise _classify_langsmith_prompt_error(exc, ref) from exc

        manifest = getattr(commit, "manifest", None)
        if not isinstance(manifest, dict):
            raise PromptRegistryConfigError(
                f"LangSmith prompt {ref.name!r} returned no manifest", ref=ref
            )
        commit_hash = getattr(commit, "commit_hash", None)
        commit_version = str(commit_hash) if commit_hash else None
        prompt_kind = _manifest_kind(manifest)
        messages = _messages_from_manifest(manifest, ref)
        return ResolvedPrompt(
            ref=ref,
            messages=messages,
            resolved_version=commit_version,
            metadata={
                "langsmith.prompt_type": "chat" if prompt_kind == "ChatPromptTemplate" else "text",
                "langsmith.prompt_commit": commit_version,
            },
        )


def _prompt_identifier(ref: PromptRef) -> str:
    # LangSmith selects by tag or commit hash (a string), carried on the ref's
    # label; an integer version is accepted as a string selector too.
    if ref.label is not None:
        selector: str | None = ref.label
    elif ref.version is not None:
        selector = str(ref.version)
    else:
        selector = None
    return f"{ref.name}:{selector}" if selector else ref.name


def _manifest_kind(manifest: dict[str, Any]) -> str | None:
    identifier = manifest.get("id")
    return identifier[-1] if isinstance(identifier, list) and identifier else None


def _mapping(value: Any) -> dict[str, Any]:
    # Manifest fields are external SDK-shaped data: a present-but-None value must
    # degrade to a clean config error downstream, not an opaque AttributeError.
    return value if isinstance(value, dict) else {}


def _messages_from_manifest(manifest: dict[str, Any], ref: PromptRef) -> tuple[ChatMessage, ...]:
    kind = _manifest_kind(manifest)
    kwargs = _mapping(manifest.get("kwargs"))
    if kind == "PromptTemplate":
        return (ChatMessage(role="user", content=_template_text(kwargs, ref)),)
    if kind == "ChatPromptTemplate":
        raw_messages = kwargs.get("messages")
        raw_messages = raw_messages if isinstance(raw_messages, list) else []
        messages = tuple(_chat_message_from_manifest(message, ref) for message in raw_messages)
        if not messages:
            raise PromptRegistryConfigError(
                f"LangSmith prompt {ref.name!r} contained no messages", ref=ref
            )
        return messages
    if kind in ("RunnableSequence", "RunnableBinding"):
        raise PromptRegistryConfigError(
            f"LangSmith prompt {ref.name!r} is bound to a model ({kind}); Typeflux owns "
            "provider selection, so store the prompt without a model binding",
            ref=ref,
        )
    raise PromptRegistryConfigError(
        f"unsupported LangSmith prompt type {kind!r} for {ref.name!r}", ref=ref
    )


def _chat_message_from_manifest(message: Any, ref: PromptRef) -> ChatMessage:
    message = _mapping(message)
    identifier = message.get("id")
    message_class = identifier[-1] if isinstance(identifier, list) and identifier else None
    role = _ROLE_BY_MESSAGE_CLASS.get(message_class or "")
    if role is None:
        raise PromptRegistryConfigError(
            f"unsupported LangSmith chat message {message_class!r} for {ref.name!r}", ref=ref
        )
    prompt_kwargs = _mapping(_mapping(message.get("kwargs")).get("prompt")).get("kwargs")
    return ChatMessage(role=role, content=_template_text(_mapping(prompt_kwargs), ref))


def _template_text(template_kwargs: dict[str, Any], ref: PromptRef) -> str:
    template = template_kwargs.get("template")
    if not isinstance(template, str):
        raise PromptRegistryConfigError(
            f"LangSmith prompt {ref.name!r} message has no template text", ref=ref
        )
    return _to_mustache(template, template_kwargs.get("template_format", "f-string"), ref)


def _to_mustache(template: str, template_format: Any, ref: PromptRef) -> str:
    normalized = (template_format if isinstance(template_format, str) else "f-string").lower()
    if normalized in _PASSTHROUGH_FORMATS:
        return template
    if normalized in _CONVERT_FORMATS:
        return _fstring_to_mustache(template, ref)
    raise PromptRegistryConfigError(
        f"LangSmith prompt {ref.name!r} uses unsupported template_format "
        f"{template_format!r}; Typeflux supports 'mustache' and 'f-string'",
        ref=ref,
    )


_FSTRING_VARIABLE = re.compile(r"\{([^{}]+)\}")
# Typeflux's renderer only substitutes plain dotted identifiers (mirrors
# core.render._MUSTACHE_VARIABLE_RE); anything else would pass through as
# un-substituted literal text.
_TYPEFLUX_IDENTIFIER = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _fstring_to_mustache(template: str, ref: PromptRef) -> str:
    """Translate Python f-string ``{var}`` placeholders to mustache ``{{var}}``.

    In f-string format ``{{`` and ``}}`` are literal braces and ``{name}`` is a
    variable; map variables to ``{{name}}`` while preserving literal braces. A
    variable carrying a format spec or conversion (``{x:.2f}``, ``{x!r}``) cannot
    be rendered by Typeflux (it substitutes raw field values, not Python
    formatting), so it is rejected loud rather than silently passed through as
    literal text.
    """
    open_marker, close_marker = "\x00", "\x01"
    staged = template.replace("{{", open_marker).replace("}}", close_marker)

    def _convert(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if not _TYPEFLUX_IDENTIFIER.fullmatch(name):
            raise PromptRegistryConfigError(
                f"LangSmith prompt {ref.name!r} has an f-string placeholder {match.group(0)!r} "
                "that is not a plain {{name}} Typeflux can render; pre-format it in the prompt "
                "or store the prompt in mustache format",
                ref=ref,
            )
        return f"{{{{{name}}}}}"

    staged = _FSTRING_VARIABLE.sub(_convert, staged)
    return staged.replace(open_marker, "{").replace(close_marker, "}")


def _langsmith_client(*, api_url: str | None = None, api_key: str | None = None) -> Any:
    try:
        from langsmith import Client
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("langsmith is required for LangSmithPromptRegistry") from exc

    return Client(api_url=api_url, api_key=api_key)


def _classify_langsmith_prompt_error(exc: Exception, ref: PromptRef) -> PromptResolutionError:
    code = status_code(exc)
    name = type(exc).__name__
    reason = f"failed to resolve LangSmith prompt {ref.name}@{ref.selector}"
    if "NotFound" in name or code == 404:
        return PromptNotFoundError(reason, ref=ref, status_code=code, original=exc)
    if any(token in name for token in ("Auth", "Unauthorized", "Forbidden")) or code in {401, 403}:
        return PromptRegistryAuthError(reason, ref=ref, status_code=code, original=exc)
    if (
        any(token in name for token in ("RateLimit", "Connection", "Timeout", "Unavailable"))
        or code in {408, 429}
        or (code is not None and code >= 500)
        # Plain OSError subclasses (BrokenPipeError, socket.gaierror, …) often
        # carry none of the tokens above but are transient and worth retrying.
        or is_network_or_timeout_error(exc)
    ):
        return PromptRegistryUnavailableError(reason, ref=ref, status_code=code, original=exc)
    if code in {400, 405, 422}:
        return PromptRegistryConfigError(reason, ref=ref, status_code=code, original=exc)
    return PromptResolutionError(
        reason=reason, ref=ref, retryable=False, status_code=code, original=exc
    )


__all__ = ["LangSmithPromptRegistry"]
