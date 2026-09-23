"""Deterministic identity for a provider-side cached session (#60).

The identity hash ties a per-item cached call to the exact stable prefix it
reused, and lets the runtime decide whether a previously-prepared session still
applies. It must cover everything the provider would cache and nothing per-item:

  provider profile (name + backend/region/project — caches are scoped to these),
  model, behavior params, the system messages, the reference-artifact identities,
  and the output schema (it participates in the cached prefix for structured
  output). It explicitly EXCLUDES operational fields (timeout, trace metadata,
  workflow/run ids) and the per-item input.

Computed once over the stable prefix before a map fan-out; recorded on the
``CachedSessionHandle`` so manifests/traces can show cache identity without the
cached content itself.
"""

from __future__ import annotations

import dataclasses
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

from typeflux.core.artifacts import ResolvedArtifactGroup
from typeflux.core.contracts import ChatMessage, ProviderParams
from typeflux.core.render import has_template_variables
from typeflux.manifests._common import canonical_json

#: Suffixes for the auto-generated cache-prep/release activities that bracket a
#: cached map step's fan-out. Kept here so the worker (registration) and the
#: generated workflow (the execute_activity calls) agree on the names.
CACHE_PREP_ACTIVITY_SUFFIX = ".__prepare_cache__"
CACHE_RELEASE_ACTIVITY_SUFFIX = ".__release_cache__"


def cache_prep_activity_name(activity_name: str) -> str:
    return f"{activity_name}{CACHE_PREP_ACTIVITY_SUFFIX}"


def cache_release_activity_name(activity_name: str) -> str:
    return f"{activity_name}{CACHE_RELEASE_ACTIVITY_SUFFIX}"


class UnstableCachePrefixError(ValueError):
    """A session-cached activity has a per-item template variable in its system
    prefix, so the prefix is not stable across map items and cannot be cached."""


def _message_texts(message: ChatMessage) -> list[str]:
    content = message.content
    if isinstance(content, str):
        return [content]
    # Any renderable text field counts: TextPart.text, but also the optional
    # ArtifactPart.text / ArtifactGroupPart.text — render_content_parts templates
    # all of them, so a {{var}} in any would vary per item (#60 review / Bugbot).
    return [text for part in content if isinstance(text := getattr(part, "text", None), str)]


def assert_stable_system_prefix(system_messages: list[ChatMessage]) -> None:
    """Reject ``{{var}}`` placeholders in system messages of a cached activity.

    Static, content-cheap detection (no cross-item render comparison): a system
    message that interpolates per-item input cannot be the stable cached prefix.
    Raised at prep time so the failure is loud and per-activity, not a silent
    cache miss or — worse — a cache poisoned with one item's values (#60).
    """
    for message in system_messages:
        for text in _message_texts(message):
            if has_template_variables(text):
                raise UnstableCachePrefixError(
                    "session cache requires a stable system prefix, but a system "
                    f"message contains a per-item template variable: {text!r}"
                )


def _part_identity(part: Any) -> Any:
    """A stable, JSON-able identity for one content part."""
    tag = type(part).__name__
    if isinstance(part, BaseModel):
        return {"type": tag, **part.model_dump()}
    if dataclasses.is_dataclass(part) and not isinstance(part, type):
        return {"type": tag, **dataclasses.asdict(part)}
    return {"type": tag, "repr": repr(part)}


def _message_identity(message: ChatMessage) -> dict[str, Any]:
    content = message.content
    rendered: Any = content if isinstance(content, str) else [_part_identity(p) for p in content]
    return {"role": message.role, "name": message.name, "content": rendered}


def _group_identity(group: ResolvedArtifactGroup) -> dict[str, Any]:
    # Identity over the artifact *bytes* (sha256) + kind/media-type, never the
    # bytes themselves — same content ⇒ same identity, across items and workers.
    return {
        "name": group.name,
        "artifacts": [
            {
                "sha256": getattr(artifact, "sha256", None),
                "kind": getattr(artifact, "kind", None),
                "media_type": getattr(artifact, "media_type", None),
            }
            for artifact in group.artifacts
        ],
    }


def session_cache_identity(
    *,
    provider_name: str | None,
    provider_profile: dict[str, Any] | None = None,
    model: str | None,
    provider_params: ProviderParams | None = None,
    system_messages: list[ChatMessage],
    reference_artifacts: tuple[ResolvedArtifactGroup, ...] = (),
    output_schema: type[BaseModel] | None = None,
) -> str:
    """Deterministic hex identity over the stable cached prefix (see module doc)."""
    payload = {
        "provider": provider_name,
        "profile": provider_profile or {},
        "model": model,
        "params": provider_params.to_dict() if provider_params is not None else {},
        "system": [_message_identity(message) for message in system_messages],
        "artifacts": [_group_identity(group) for group in reference_artifacts],
        "output_schema": (output_schema.model_json_schema() if output_schema is not None else None),
    }
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "CACHE_PREP_ACTIVITY_SUFFIX",
    "CACHE_RELEASE_ACTIVITY_SUFFIX",
    "UnstableCachePrefixError",
    "assert_stable_system_prefix",
    "cache_prep_activity_name",
    "cache_release_activity_name",
    "session_cache_identity",
]
