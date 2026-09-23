from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256

from pydantic import BaseModel

from typeflux.core.artifacts import content_part_payload
from typeflux.core.contracts import ChatMessage
from typeflux.manifests._common import canonical_json


def schema_hash(model: type[BaseModel]) -> str:
    payload = canonical_json(model.model_json_schema())
    return sha256(payload.encode("utf-8")).hexdigest()


def messages_hash(messages: Sequence[ChatMessage]) -> str:
    return sha256(
        canonical_json([_message_payload(message) for message in messages]).encode("utf-8")
    ).hexdigest()


def _message_payload(message: ChatMessage) -> dict[str, object]:
    payload: dict[str, object] = {
        "role": message.role,
        "content": content_part_payload(message.content),
    }
    if message.name is not None:
        payload["name"] = message.name
    return payload


__all__ = ["messages_hash", "schema_hash"]
