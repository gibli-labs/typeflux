from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from typeflux.core.artifacts import render_content_parts
from typeflux.core.contracts import ChatMessage

_MUSTACHE_VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*}}")


def _resolve_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"missing prompt field: {path}")
        current = current[part]
    return current


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def has_template_variables(template: str) -> bool:
    """True if ``template`` contains at least one ``{{var}}`` placeholder.

    Used to reject per-item template variables in a session-cached system prefix
    (#60): a prefix that varies per item cannot be the stable cached content.
    """
    return _MUSTACHE_VARIABLE_RE.search(template) is not None


def render_template(template: str, input_model: BaseModel) -> str:
    """Substitute validated ``{{var}}`` placeholders with literal field values.

    Substitution is plain text: user content round-trips byte-for-byte,
    including ``&``, ``<``, ``>``, and quotes. The placeholder grammar is
    exactly what ``_MUSTACHE_VARIABLE_RE`` validates — dot-path identifiers,
    no mustache sections or partials.
    """
    data = input_model.model_dump(mode="python")
    return _MUSTACHE_VARIABLE_RE.sub(
        lambda match: _format_value(_resolve_path(data, match.group(1))),
        template,
    )


def render_messages(messages: tuple[ChatMessage, ...], input_model: BaseModel) -> list[ChatMessage]:
    return [
        ChatMessage(
            role=message.role,
            content=render_content_parts(
                message.content,
                lambda text: render_template(text, input_model),
            ),
            name=message.name,
        )
        for message in messages
    ]


__all__ = ["has_template_variables", "render_messages", "render_template"]
