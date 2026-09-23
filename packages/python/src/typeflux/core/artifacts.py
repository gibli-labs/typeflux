from __future__ import annotations

import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactKind = Literal[
    "document",
    "image",
    "audio",
    "video",
    "data",
    "archive",
    "provider_file",
    "external_uri",
    "other",
]
ARTIFACT_KINDS: frozenset[str] = frozenset(get_args(ArtifactKind))
ArtifactSourceKind = Literal["local_path", "url", "object_uri", "provider_file"]
ArtifactAttachmentRole = Literal["system", "user", "assistant"]

ScalarArtifactMetadata = str | int | float | bool


class ArtifactSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ArtifactSourceKind
    path: str | None = None
    url: str | None = None
    uri: str | None = None
    provider: str | None = None
    file_id: str | None = None

    @model_validator(mode="after")
    def _validate_source_fields(self) -> ArtifactSource:
        required = {
            "local_path": ("path",),
            "url": ("url",),
            "object_uri": ("uri",),
            "provider_file": ("provider", "file_id"),
        }[self.type]
        missing = [field for field in required if getattr(self, field) in (None, "")]
        if missing:
            raise ValueError(f"artifact source {self.type!r} requires {', '.join(sorted(missing))}")
        extras = {
            "local_path": {"url", "uri", "provider", "file_id"},
            "url": {"path", "uri", "provider", "file_id"},
            "object_uri": {"path", "url", "provider", "file_id"},
            "provider_file": {"path", "url", "uri"},
        }[self.type]
        configured_extras = [field for field in extras if getattr(self, field) is not None]
        if configured_extras:
            raise ValueError(
                f"artifact source {self.type!r} cannot set {', '.join(sorted(configured_extras))}"
            )
        return self

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any]) -> ArtifactSource:
        if isinstance(value, str):
            return cls(type="local_path", path=value)
        return cls.model_validate(value)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: ArtifactSource
    kind: ArtifactKind | None = None
    media_type: str | None = None
    role: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    display_name: str | None = None
    metadata: dict[str, ScalarArtifactMetadata] = Field(default_factory=dict)

    @field_validator("source", mode="before")
    @classmethod
    def _coerce_source(cls, value: object) -> object:
        if isinstance(value, ArtifactSource):
            return value
        if isinstance(value, str):
            return ArtifactSource(type="local_path", path=value)
        if isinstance(value, Mapping):
            return ArtifactSource.model_validate(value)
        return value

    @field_validator("role", "display_name")
    @classmethod
    def _validate_optional_non_empty(cls, value: str | None) -> str | None:
        if value is not None and (not value or value.strip() != value):
            raise ValueError("artifact fields must be non-empty and trimmed")
        return value

    @field_validator("size_bytes")
    @classmethod
    def _validate_size_bytes(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("artifact size_bytes must be >= 0")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("artifact sha256 must be lowercase hex")
        return value


@dataclass(frozen=True)
class ArtifactAttachment:
    role: ArtifactAttachmentRole = "user"
    text: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("artifact attachment role must be system, user, or assistant")
        if self.text is not None and self.text == "":
            raise ValueError("artifact attachment text must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {"role": self.role, "text": self.text}.items()
            if value is not None
        }


@dataclass(frozen=True)
class ArtifactInput:
    name: str
    from_path: str
    required: bool = True
    kind: ArtifactKind | None = None
    media_types: tuple[str, ...] = ()
    max_count: int | None = None
    max_bytes: int | None = None
    attach: ArtifactAttachment | None = None
    #: ``"reference"`` marks this artifact as part of the stable, session-cached
    #: prefix (#60/#363): when caching engages it is resolved once at prep time
    #: and reused across map items rather than re-sent per item. Must be identical
    #: across all items of the map step. None ⇒ ordinary per-item artifact.
    cache_role: Literal["reference"] | None = None

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError("artifact input name must be non-empty and trimmed")
        if not self.from_path.startswith("input."):
            raise ValueError("artifact input from_path must start with 'input.'")
        for media_type in self.media_types:
            if not media_type or media_type.strip() != media_type:
                raise ValueError("artifact input media_types must be non-empty and trimmed")
        if self.max_count is not None and self.max_count < 1:
            raise ValueError("artifact input max_count must be >= 1")
        if self.max_bytes is not None and self.max_bytes < 0:
            raise ValueError("artifact input max_bytes must be >= 0")
        if self.cache_role == "reference" and self.attach is None:
            # A reference artifact reaches the model (and the cache) only via its
            # attach rule. Without attach it is presented nowhere — at prep it is
            # not added to the cached prefix, and per-item it is skipped — so the
            # document would silently vanish. Reject it at the source (#363 review).
            raise ValueError(
                f"artifact input {self.name!r} sets cache: reference but has no "
                "attach rule; a reference artifact must declare how it attaches"
            )
        if (
            self.cache_role == "reference"
            and not self.required
            and self.attach is not None
            and self.attach.text is None
        ):
            # An optional, textless reference artifact emits NO attach message for
            # items where it resolves empty, so the conversation shape (and the
            # prefix-cache breakpoint index derived from the static reference
            # count, #362) would vary across items — breaking the byte-identical
            # prefix contract. A sometimes-absent message is invalid, not merely
            # risky; reject it at the source (fail-closed, #362 review).
            raise ValueError(
                f"artifact input {self.name!r} sets cache: reference with "
                "required: false and no attach text; a cache: reference artifact "
                "must always produce its attach message so the cached prefix is "
                "stable across items — make it required or give attach a static text"
            )
        object.__setattr__(self, "media_types", tuple(self.media_types))

    def safe_definition(self) -> dict[str, Any]:
        safe_attach = {"role": self.attach.role} if self.attach is not None else None
        return _drop_none(
            {
                "name": self.name,
                "from_path": self.from_path,
                "required": self.required,
                "kind": self.kind,
                "media_types": list(self.media_types) if self.media_types else None,
                "max_count": self.max_count,
                "max_bytes": self.max_bytes,
                "attach": safe_attach,
                "cache_role": self.cache_role,
            }
        )


@dataclass(frozen=True)
class ArtifactPolicy:
    local_roots: tuple[Path, ...] = ()
    allowed_source_kinds: tuple[ArtifactSourceKind, ...] = ("local_path",)
    allowed_media_types: tuple[str, ...] = ()
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "local_roots", tuple(Path(root).resolve() for root in self.local_roots)
        )
        object.__setattr__(self, "allowed_source_kinds", tuple(self.allowed_source_kinds))
        object.__setattr__(self, "allowed_media_types", tuple(self.allowed_media_types))
        valid_sources = {"local_path", "url", "object_uri", "provider_file"}
        for source_kind in self.allowed_source_kinds:
            if source_kind not in valid_sources:
                raise ValueError(f"unsupported artifact source kind: {source_kind}")
        for media_type in self.allowed_media_types:
            if not media_type or media_type.strip() != media_type:
                raise ValueError("artifact policy media types must be non-empty and trimmed")
        if self.max_bytes is not None and self.max_bytes < 0:
            raise ValueError("artifact policy max_bytes must be >= 0")


@dataclass(frozen=True)
class ResolvedArtifact:
    group: str
    index: int
    ref: ArtifactRef
    source_kind: ArtifactSourceKind
    kind: ArtifactKind | None
    media_type: str | None
    role: str | None
    sha256: str | None
    size_bytes: int | None
    local_path: Path | None = None

    def safe_summary(self) -> dict[str, Any]:
        return _drop_none(
            {
                "group": self.group,
                "index": self.index,
                "source_kind": self.source_kind,
                "kind": self.kind,
                "media_type": self.media_type,
                "role": self.role,
                "sha256": self.sha256,
                "size_bytes": self.size_bytes,
            }
        )


@dataclass(frozen=True)
class ResolvedArtifactGroup:
    name: str
    artifacts: tuple[ResolvedArtifact, ...]

    def safe_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": len(self.artifacts),
            "artifacts": [artifact.safe_summary() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class TextPart:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ArtifactPart:
    artifact: str
    type: Literal["artifact"] = "artifact"
    text: str | None = None


@dataclass(frozen=True)
class ArtifactGroupPart:
    group: str
    type: Literal["artifact_group"] = "artifact_group"
    text: str | None = None


@dataclass(frozen=True)
class ProviderExtensionPart:
    provider: str
    payload: Mapping[str, Any]
    type: Literal["provider_extension"] = "provider_extension"


ContentPart = TextPart | ArtifactPart | ArtifactGroupPart | ProviderExtensionPart
ChatContent = str | tuple[ContentPart, ...]


def normalize_content_parts(value: object) -> ChatContent:
    if isinstance(value, str):
        return value
    if isinstance(value, (TextPart, ArtifactPart, ArtifactGroupPart, ProviderExtensionPart)):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_coerce_content_part(part) for part in value)
    raise TypeError("ChatMessage content must be a string or a sequence of content parts")


def render_content_parts(content: ChatContent, render_text) -> ChatContent:
    if isinstance(content, str):
        return render_text(content)
    rendered: list[ContentPart] = []
    for part in content:
        if isinstance(part, TextPart):
            rendered.append(TextPart(text=render_text(part.text)))
            continue
        if isinstance(part, ArtifactPart) and part.text is not None:
            rendered.append(ArtifactPart(artifact=part.artifact, text=render_text(part.text)))
            continue
        if isinstance(part, ArtifactGroupPart) and part.text is not None:
            rendered.append(ArtifactGroupPart(group=part.group, text=render_text(part.text)))
            continue
        rendered.append(part)
    return tuple(rendered)


def content_part_payload(content: ChatContent) -> Any:
    if isinstance(content, str):
        return content
    payload: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            payload.append({"type": part.type, "text": part.text})
        elif isinstance(part, ArtifactPart):
            payload.append(
                _drop_none({"type": part.type, "artifact": part.artifact, "text": part.text})
            )
        elif isinstance(part, ArtifactGroupPart):
            payload.append(_drop_none({"type": part.type, "group": part.group, "text": part.text}))
        else:
            payload.append(
                {"type": part.type, "provider": part.provider, "payload": dict(part.payload)}
            )
    return payload


def resolve_artifact_inputs(
    input_value: Any,
    artifact_inputs: Sequence[ArtifactInput],
    *,
    policy: ArtifactPolicy | None = None,
) -> tuple[ResolvedArtifactGroup, ...]:
    if not artifact_inputs:
        return ()
    resolved_policy = policy or ArtifactPolicy()
    groups: list[ResolvedArtifactGroup] = []
    for artifact_input in artifact_inputs:
        raw_value = _value_at_path(input_value, artifact_input.from_path)
        refs = _coerce_artifact_refs(raw_value, required=artifact_input.required)
        if artifact_input.max_count is not None and len(refs) > artifact_input.max_count:
            raise ValueError(
                f"artifact input {artifact_input.name!r} allows at most "
                f"{artifact_input.max_count} artifact(s)"
            )
        artifacts = tuple(
            _resolve_artifact(
                artifact_input,
                ref,
                index=index,
                policy=resolved_policy,
            )
            for index, ref in enumerate(refs)
        )
        groups.append(ResolvedArtifactGroup(name=artifact_input.name, artifacts=artifacts))
    return tuple(groups)


def attach_artifact_messages(
    messages: Sequence[Any],
    artifact_inputs: Sequence[ArtifactInput],
    artifact_groups: Sequence[ResolvedArtifactGroup] = (),
) -> list[Any]:
    from typeflux.core.contracts import ChatMessage

    attached = list(messages)
    for artifact_input in artifact_inputs:
        if artifact_input.attach is None:
            continue
        has_artifacts = _artifact_group_has_artifacts(artifact_groups, artifact_input.name)
        if not has_artifacts and artifact_input.attach.text is None:
            continue
        parts: list[ContentPart] = []
        if artifact_input.attach.text is not None:
            parts.append(TextPart(artifact_input.attach.text))
        if has_artifacts:
            parts.append(ArtifactGroupPart(group=artifact_input.name))
        attached.append(
            ChatMessage(
                role=artifact_input.attach.role,
                content=tuple(parts),
            )
        )
    return attached


def _artifact_group_has_artifacts(
    groups: Sequence[ResolvedArtifactGroup],
    name: str,
) -> bool:
    return any(group.name == name and bool(group.artifacts) for group in groups)


def artifact_groups_summary(groups: Sequence[ResolvedArtifactGroup]) -> list[dict[str, Any]]:
    return [group.safe_summary() for group in groups]


def _artifact_cache_identity(artifact: ResolvedArtifact) -> dict[str, Any]:
    identity = artifact.safe_summary()
    if artifact.sha256 is None:
        # No content hash pins the bytes (URL/object_uri/provider_file, or a
        # hand-built local ref): fold the source itself, or swapping the location
        # under the same group/index would serve a stale cached output (#504).
        identity["source"] = artifact.ref.source.model_dump(exclude_none=True)
    return identity


def artifact_groups_cache_identity(groups: Sequence[ResolvedArtifactGroup]) -> list[dict[str, Any]]:
    """The cross-run cache-key fold (#504): the safe summary, extended with the
    artifact SOURCE whenever no ``sha256`` pins the bytes, minus empty groups
    (an optional input that resolved to nothing must not change the key).

    Deliberately distinct from :func:`artifact_groups_summary`: the summary feeds
    manifests/observability and must stay redacted (no paths/URLs/ids), while
    this shape is only ever hashed into a cache key.
    """

    return [
        {
            "name": group.name,
            "count": len(group.artifacts),
            "artifacts": [_artifact_cache_identity(artifact) for artifact in group.artifacts],
        }
        for group in groups
        if group.artifacts
    ]


def _resolve_artifact(
    artifact_input: ArtifactInput,
    ref: ArtifactRef,
    *,
    index: int,
    policy: ArtifactPolicy,
) -> ResolvedArtifact:
    source = ref.source
    if source.type not in policy.allowed_source_kinds:
        raise ValueError(
            f"artifact input {artifact_input.name!r} source {source.type!r} is not allowed"
        )
    media_type = ref.media_type or _guess_media_type(source)
    resolved_kind = ref.kind or artifact_input.kind or _guess_kind(media_type, source)
    if ref.kind is not None and artifact_input.kind is not None and ref.kind != artifact_input.kind:
        raise ValueError(
            f"artifact input {artifact_input.name!r} expected kind {artifact_input.kind!r}, "
            f"got {ref.kind!r}"
        )
    kind = resolved_kind
    _validate_media_type(
        artifact_input=artifact_input,
        media_type=media_type,
        policy=policy,
    )
    local_path = _resolve_local_path(source, policy) if source.type == "local_path" else None
    actual_sha256 = ref.sha256
    actual_size_bytes = ref.size_bytes
    if local_path is not None:
        digest, size_bytes = _hash_file(local_path)
        if actual_sha256 is not None and actual_sha256 != digest:
            raise ValueError(
                f"artifact input {artifact_input.name!r} hash mismatch for local artifact"
            )
        actual_sha256 = digest
        actual_size_bytes = size_bytes
    max_bytes = (
        artifact_input.max_bytes if artifact_input.max_bytes is not None else policy.max_bytes
    )
    if max_bytes is not None and actual_size_bytes is not None and actual_size_bytes > max_bytes:
        raise ValueError(
            f"artifact input {artifact_input.name!r} exceeds max_bytes "
            f"({actual_size_bytes} > {max_bytes})"
        )
    return ResolvedArtifact(
        group=artifact_input.name,
        index=index,
        ref=ref,
        source_kind=source.type,
        kind=kind,
        media_type=media_type,
        role=ref.role or artifact_input.name,
        sha256=actual_sha256,
        size_bytes=actual_size_bytes,
        local_path=local_path,
    )


def _resolve_local_path(source: ArtifactSource, policy: ArtifactPolicy) -> Path:
    if not policy.local_roots:
        raise ValueError(
            "local artifact sources require at least one configured artifact local_root"
        )
    raw_path = Path(source.path or "").expanduser()
    candidates = (
        (raw_path.resolve(),)
        if raw_path.is_absolute()
        else tuple((root / raw_path).resolve() for root in policy.local_roots)
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise FileNotFoundError(
            f"artifact local_path does not exist or is not a file: {source.path}"
        )
    for root in policy.local_roots:
        if path == root or path.is_relative_to(root):
            return path
    raise ValueError("artifact local_path is outside configured artifact local_roots")


def _validate_media_type(
    *,
    artifact_input: ArtifactInput,
    media_type: str | None,
    policy: ArtifactPolicy,
) -> None:
    allowed = artifact_input.media_types or policy.allowed_media_types
    if not allowed:
        return
    if media_type is None:
        raise ValueError(
            f"artifact input {artifact_input.name!r} has unknown media type "
            "and cannot be checked against allowed media types"
        )
    for pattern in allowed:
        if _media_type_matches(media_type, pattern):
            return
    raise ValueError(
        f"artifact input {artifact_input.name!r} media type {media_type!r} is not allowed"
    )


def _media_type_matches(media_type: str, pattern: str) -> bool:
    if pattern == media_type:
        return True
    if pattern.endswith("/*"):
        return media_type.startswith(pattern[:-1])
    return False


def _hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _guess_media_type(source: ArtifactSource) -> str | None:
    name = source.path or source.url or source.uri
    if name is None:
        return None
    media_type, _ = mimetypes.guess_type(name)
    return media_type


def _guess_kind(media_type: str | None, source: ArtifactSource) -> ArtifactKind:
    if source.type == "provider_file":
        return "provider_file"
    if source.type in {"url", "object_uri"} and media_type is None:
        return "external_uri"
    if media_type is None:
        return "other"
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if media_type.startswith("video/"):
        return "video"
    if media_type in {"application/json", "text/csv"} or media_type.startswith("text/"):
        return "data"
    if media_type in {"application/pdf"}:
        return "document"
    if media_type in {"application/zip", "application/gzip"}:
        return "archive"
    return "document"


def _coerce_artifact_refs(value: Any, *, required: bool) -> tuple[ArtifactRef, ...]:
    if value is None:
        if required:
            raise ValueError("required artifact input resolved to null")
        return ()
    if isinstance(value, ArtifactRef):
        return (value,)
    if isinstance(value, (str, Mapping)):
        return (_coerce_single_artifact_ref(value),)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        refs = tuple(_coerce_single_artifact_ref(item) for item in value)
        if required and not refs:
            raise ValueError("required artifact input resolved to an empty list")
        return refs
    raise TypeError("artifact input values must be ArtifactRef, string path, mapping, or list")


def _coerce_single_artifact_ref(value: Any) -> ArtifactRef:
    if isinstance(value, ArtifactRef):
        return value
    if isinstance(value, str):
        return ArtifactRef(source=ArtifactSource.from_value(value))
    if isinstance(value, Mapping):
        if "source" in value:
            return ArtifactRef.model_validate(value)
        return ArtifactRef(source=ArtifactSource.from_value(value))
    raise TypeError("artifact list entries must be ArtifactRef, string path, or mapping")


def _value_at_path(value: Any, path: str) -> Any:
    parts = path.split(".")
    current = value
    for part in parts[1:]:
        if isinstance(current, BaseModel):
            current = getattr(current, part, None)
        elif isinstance(current, Mapping):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return None
    return current


def _coerce_content_part(value: object) -> ContentPart:
    if isinstance(value, (TextPart, ArtifactPart, ArtifactGroupPart, ProviderExtensionPart)):
        return value
    if isinstance(value, Mapping):
        kind = value.get("type")
        if kind == "text":
            return TextPart(text=str(value["text"]))
        if kind == "artifact":
            return ArtifactPart(
                artifact=str(value["artifact"]),
                text=str(value["text"]) if value.get("text") is not None else None,
            )
        if kind == "artifact_group":
            return ArtifactGroupPart(
                group=str(value["group"]),
                text=str(value["text"]) if value.get("text") is not None else None,
            )
        if kind == "provider_extension":
            payload = value.get("payload")
            if not isinstance(payload, Mapping):
                raise TypeError("provider_extension content parts require mapping payload")
            return ProviderExtensionPart(provider=str(value["provider"]), payload=payload)
    raise TypeError("unsupported ChatMessage content part")


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


__all__ = [
    "ARTIFACT_KINDS",
    "ArtifactAttachment",
    "ArtifactGroupPart",
    "ArtifactInput",
    "ArtifactKind",
    "ArtifactPart",
    "ArtifactPolicy",
    "ArtifactRef",
    "ArtifactSource",
    "ArtifactSourceKind",
    "ChatContent",
    "ContentPart",
    "ProviderExtensionPart",
    "ResolvedArtifact",
    "ResolvedArtifactGroup",
    "TextPart",
    "artifact_groups_cache_identity",
    "artifact_groups_summary",
    "attach_artifact_messages",
    "content_part_payload",
    "normalize_content_parts",
    "render_content_parts",
    "resolve_artifact_inputs",
]
