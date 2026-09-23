from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from threading import Lock
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from typeflux.core.artifacts import (
    ArtifactGroupPart,
    ArtifactPart,
    ProviderExtensionPart,
    ResolvedArtifact,
    ResolvedArtifactGroup,
    TextPart,
)
from typeflux.core.contracts import CachedSessionHandle, ChatMessage, ProviderParams
from typeflux.env import load_env
from typeflux.providers._shared import (
    artifact_for_name,
    artifacts_for_group,
    is_network_or_timeout_error,
    matches_provider_error,
    raise_if_truncated,
    retry_after_seconds,
    status_code,
)
from typeflux.providers.base import ProviderUsage, validate_provider_params_supported
from typeflux.providers.errors import (
    ProviderAuthError,
    ProviderCacheUnavailableError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTransientError,
)

logger = logging.getLogger(__name__)

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_SUPPORTED_IMAGE_MEDIA_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}

#: Gemini's inline-data request cap is ~20 MB; a local artifact larger than this
#: must be sent via the Files API (upload once, reference by ``file_uri``) rather
#: than inlined as bytes (#358).
_GEMINI_INLINE_MAX_BYTES = 20 * 1024 * 1024
#: How long to wait for an uploaded file to leave PROCESSING (video transcoding)
#: and become ACTIVE before referencing it.
_GEMINI_FILE_ACTIVE_TIMEOUT_SECONDS = 120.0
_GEMINI_FILE_POLL_INTERVAL_SECONDS = 2.0


class GeminiProvider:
    provider_name = "gemini"
    supported_provider_params = frozenset(
        {
            "model",
            "temperature",
            "max_tokens",
            "top_p",
            "top_k",
            "stop",
            "timeout",
            "thinking_budget",
        }
    )
    # This provider's messages path attaches images, PDF documents, and audio /
    # video as inline data (Gemini natively ingests all four). The set is stated
    # as exactly what is handled so preflight rejects unsupported kinds
    # (provider_file/archive/…) early and loud rather than passing preflight then
    # failing mid-run. audio/video are attached only as local inline data; a
    # URL-sourced audio/video fails loud (the Files API path is #358). Large
    # media via the Gemini Files API (upload + reference) shares #60's upload path.
    supported_artifact_kinds = frozenset({"image", "document", "audio", "video"})
    # Provider-side session caching (#60). Gemini is "reference" style: the stable
    # prefix is uploaded once to a server-side ``cachedContent`` object via
    # ``caches.create``, and per-item calls reference it by name (the prefix is
    # NOT re-sent). Explicit ``caches.delete`` on release.
    supports_session_cache = True
    session_cache_style = "reference"

    def __init__(
        self,
        *,
        default_model: str = _DEFAULT_GEMINI_MODEL,
        api_key: str | None = None,
        use_vertex: bool = False,
        project: str | None = None,
        location: str | None = None,
        default_provider_params: ProviderParams | None = None,
        genai_client: Any | None = None,
        async_genai_client: Any | None = None,
    ) -> None:
        self.default_model = default_model
        self.api_key = api_key
        self.use_vertex = use_vertex
        self.project = project
        self.location = location
        self.default_provider_params = ProviderParams(model=default_model).merge(
            default_provider_params
        )
        validate_provider_params_supported(self, self.default_provider_params)
        self._client = genai_client
        self._async_client = async_genai_client
        self._async_client_lock = Lock()
        # An injected client (sync or async) means the caller owns construction:
        # never import the SDK, load env, or build the missing counterpart.
        self._injected_client = genai_client is not None or async_genai_client is not None
        if self._injected_client:
            return

        load_env()
        self._client = _build_client(
            api_key=api_key, use_vertex=use_vertex, project=project, location=location
        )

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        provider_params: ProviderParams | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
        observation_context: dict[str, Any] | None = None,
        usage_sink: Callable[[ProviderUsage], None] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        del metadata, observation_context
        client = self._client
        if client is None:
            raise ProviderConfigError(
                "injected Gemini async client does not support sync structured calls",
                provider="gemini",
            )
        params = self.default_provider_params.merge(
            provider_params,
            ProviderParams(model=model, temperature=temperature),
        )
        validate_provider_params_supported(self, params)
        cached_content = _resolve_gemini_cache_id(cached_session, model=params.model)
        # Oversize local artifacts go through the Files API (upload + reference)
        # rather than inline bytes (#358). Only artifacts actually referenced by
        # the messages are uploaded; when a reference cache is in play the prefix
        # artifacts already live in the cache, so only the per-item artifacts (not
        # skipped at prepare) are uploaded here.
        uploaded = _upload_oversize_artifacts(client, messages, artifacts)
        call_kwargs = _gemini_call_kwargs(
            messages=messages,
            artifacts=artifacts,
            output_schema=output_schema,
            params=params,
            cached_content=cached_content,
            uploaded=uploaded,
        )
        try:
            result = client.models.generate_content(**call_kwargs)
        except Exception as exc:
            _raise_gemini_provider_error(exc, cached_content=cached_content)
        return _validated_provider_result(
            result, output_schema, usage_sink=usage_sink, model=params.model
        )

    async def async_structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        provider_params: ProviderParams | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
        observation_context: dict[str, Any] | None = None,
        usage_sink: Callable[[ProviderUsage], None] | None = None,
        cached_session: CachedSessionHandle | None = None,
    ) -> BaseModel:
        del metadata, observation_context
        client = await self._get_async_client_async()
        params = self.default_provider_params.merge(
            provider_params,
            ProviderParams(model=model, temperature=temperature),
        )
        validate_provider_params_supported(self, params)
        cached_content = _resolve_gemini_cache_id(cached_session, model=params.model)
        # Files-API upload of oversize artifacts uses the sync client surface; run
        # it off the event loop (#358).
        uploaded = await asyncio.to_thread(_upload_oversize_artifacts, client, messages, artifacts)
        call_kwargs = _gemini_call_kwargs(
            messages=messages,
            artifacts=artifacts,
            output_schema=output_schema,
            params=params,
            cached_content=cached_content,
            uploaded=uploaded,
        )
        try:
            result = await client.aio.models.generate_content(**call_kwargs)
        except Exception as exc:
            _raise_gemini_provider_error(exc, cached_content=cached_content)
        return _validated_provider_result(
            result, output_schema, usage_sink=usage_sink, model=params.model
        )

    def prepare_cached_session(
        self,
        *,
        messages: list[ChatMessage],
        artifacts: tuple[ResolvedArtifactGroup, ...] = (),
        model: str | None = None,
        provider_params: ProviderParams | None = None,
        identity_hash: str,
        ttl_seconds: int | None = None,
    ) -> CachedSessionHandle:
        """Upload the stable prefix to a server-side ``cachedContent`` (#60).

        Reference style: the system instruction + prefix contents are stored once
        via ``caches.create``; per-item calls reference the returned name and do
        not re-send the prefix. This is the non-deterministic call Typeflux runs
        in its own prep activity. ``created_at`` is left for the runtime to stamp.
        """
        client = self._client
        if client is None:
            raise ProviderConfigError(
                "injected Gemini async client cannot prepare a cached session synchronously",
                provider="gemini",
            )
        params = self.default_provider_params.merge(provider_params, ProviderParams(model=model))
        # Oversize reference artifacts (e.g. a >20MB video) are uploaded via the
        # Files API and referenced by uri inside the cached content, not inlined
        # (caches.create has the same inline cap as a normal request). #358
        uploaded = _upload_oversize_artifacts(client, messages, artifacts)
        system_parts, contents = _gemini_contents(messages, artifacts, uploaded)
        if not system_parts and not contents:
            raise ProviderConfigError(
                "a cached session needs a stable prefix (system instructions or contents)",
                provider="gemini",
            )
        cache_config: dict[str, Any] = {}
        if system_parts:
            cache_config["system_instruction"] = {"parts": system_parts}
        if contents:
            cache_config["contents"] = contents
        if ttl_seconds is not None:
            cache_config["ttl"] = f"{ttl_seconds}s"
        try:
            cache = client.caches.create(model=params.model, config=cache_config)
        except Exception as exc:
            _raise_gemini_provider_error(exc)
        cache_id = getattr(cache, "name", None)
        if cache_id is None and isinstance(cache, dict):
            cache_id = cache.get("name")
        if not isinstance(cache_id, str) or not cache_id:
            raise ProviderConfigError(
                "Gemini caches.create returned no cache name", provider="gemini"
            )
        return CachedSessionHandle(
            provider=self.provider_name,
            identity_hash=identity_hash,
            supported=True,
            style="reference",
            cache_id=cache_id,
            model=params.model,
            ttl_seconds=ttl_seconds,
        )

    def release_cached_session(self, handle: CachedSessionHandle) -> None:
        """Delete the server-side cache, best-effort: a fallback handle or a
        client without a cache id has nothing to release, and a delete failure
        (already expired / 404 / transient) is swallowed — the cache TTL-expires
        regardless, so a failed cleanup must not fail the workflow (#60 review)."""
        if not handle.cache_id or self._client is None:
            return
        try:
            self._client.caches.delete(name=handle.cache_id)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
            logger.warning(
                "best-effort release of Gemini cache %s failed (%s); TTL will reap it",
                handle.cache_id,
                type(exc).__name__,
            )

    def _get_async_client(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        if self._injected_client:
            raise ProviderConfigError(
                "injected Gemini client does not support async structured calls",
                provider="gemini",
            )
        with self._async_client_lock:
            if self._async_client is not None:
                return self._async_client
            load_env()
            # The google-genai client exposes async via ``client.aio``; the same
            # client object serves both, so reuse the sync client when present.
            self._async_client = self._client or _build_client(
                api_key=self.api_key,
                use_vertex=self.use_vertex,
                project=self.project,
                location=self.location,
            )
            return self._async_client

    async def _get_async_client_async(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        return await asyncio.to_thread(self._get_async_client)


def _build_client(
    *, api_key: str | None, use_vertex: bool, project: str | None, location: str | None
) -> Any:
    try:
        from google import genai
    except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard.
        raise RuntimeError("google-genai is required for GeminiProvider") from exc

    if use_vertex:
        # Vertex AI: project + location + Application Default Credentials.
        kwargs: dict[str, Any] = {"vertexai": True}
        if project is not None:
            kwargs["project"] = project
        if location is not None:
            kwargs["location"] = location
        return genai.Client(**kwargs)

    # Gemini Developer API: the SDK reads GEMINI_API_KEY / GOOGLE_API_KEY from the
    # environment when api_key is not passed explicitly.
    resolved_key = api_key
    if resolved_key is None:
        import os

        resolved_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    kwargs = {}
    if resolved_key is not None:
        kwargs["api_key"] = resolved_key
    return genai.Client(**kwargs)


def _gemini_contents(
    messages: list[ChatMessage],
    artifacts: tuple[ResolvedArtifactGroup, ...],
    uploaded: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split messages into (system parts, conversation contents) as provider
    dicts (the SDK coerces them) — shared by the generate call and cache create.
    ``uploaded`` maps oversize artifacts to their Files-API references (#358)."""
    system_parts: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            system_parts.extend(_to_gemini_parts(message, artifacts=artifacts, uploaded=uploaded))
            continue
        if message.role not in {"user", "assistant"}:
            raise ProviderConfigError(
                f"unsupported Gemini message role: {message.role!r}", provider="gemini"
            )
        role = "user" if message.role == "user" else "model"
        contents.append(
            {
                "role": role,
                "parts": _to_gemini_parts(message, artifacts=artifacts, uploaded=uploaded),
            }
        )
    return system_parts, contents


def _gemini_call_kwargs(
    *,
    messages: list[ChatMessage],
    artifacts: tuple[ResolvedArtifactGroup, ...],
    output_schema: type[BaseModel],
    params: ProviderParams,
    cached_content: str | None = None,
    uploaded: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Build provider-agnostic dicts (the SDK coerces ``contents`` and ``config``
    # dicts into google-genai types) so the request shape needs no SDK import —
    # the SDK is only required to construct the client and make the call.
    system_parts, contents = _gemini_contents(messages, artifacts, uploaded)

    if not contents:
        raise ProviderConfigError(
            "Gemini requires at least one user or assistant message; a system-only "
            "prompt has no content to generate from",
            provider="gemini",
        )

    config: dict[str, Any] = {
        "response_mime_type": "application/json",
        "response_schema": output_schema,
    }
    if cached_content is not None:
        # The stable prefix (incl. system instruction) lives in the referenced
        # cache; it must not also be sent inline, so the per-item call omits
        # system_instruction and sends only the item contents (#60, reference style).
        config["cached_content"] = cached_content
    elif system_parts:
        config["system_instruction"] = {"parts": system_parts}
    if params.temperature is not None:
        config["temperature"] = params.temperature
    if params.max_tokens is not None:
        config["max_output_tokens"] = params.max_tokens
    if params.top_p is not None:
        config["top_p"] = params.top_p
    if params.top_k is not None:
        config["top_k"] = params.top_k
    if params.stop:
        config["stop_sequences"] = list(params.stop)
    if params.thinking_budget is not None:
        # 0 disables thinking (frees the whole max_output_tokens budget for the
        # answer); a positive value caps the thinking budget.
        config["thinking_config"] = {"thinking_budget": params.thinking_budget}
    if params.timeout is not None:
        # google-genai takes per-request timeout (milliseconds) via http_options.
        config["http_options"] = {"timeout": int(params.timeout * 1000)}

    return {"model": params.model, "contents": contents, "config": config}


def _to_gemini_parts(
    message: ChatMessage,
    *,
    artifacts: tuple[ResolvedArtifactGroup, ...],
    uploaded: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    content = message.content
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"text": part.text})
        elif isinstance(part, ArtifactGroupPart):
            if part.text is not None:
                parts.append({"text": part.text})
            for artifact in artifacts_for_group(artifacts, part.group, provider="gemini"):
                parts.append(_to_gemini_artifact_part(artifact, uploaded))
        elif isinstance(part, ArtifactPart):
            if part.text is not None:
                parts.append({"text": part.text})
            parts.append(
                _to_gemini_artifact_part(
                    artifact_for_name(artifacts, part.artifact, provider="gemini"), uploaded
                )
            )
        elif isinstance(part, ProviderExtensionPart):
            if part.provider != "gemini":
                raise ProviderConfigError(
                    f"content part provider extension {part.provider!r} is not for Gemini",
                    provider="gemini",
                )
            parts.append(dict(part.payload))
        else:
            raise ProviderConfigError(
                f"unsupported Gemini content part: {type(part).__name__}", provider="gemini"
            )
    return parts


def _is_audio_or_video(artifact: ResolvedArtifact) -> bool:
    media_type = artifact.media_type or ""
    return artifact.kind in ("audio", "video") or media_type.startswith(("audio/", "video/"))


def _artifact_byte_size(artifact: ResolvedArtifact) -> int | None:
    if artifact.size_bytes is not None:
        return artifact.size_bytes
    if artifact.local_path is not None:
        try:
            return artifact.local_path.stat().st_size
        except OSError:
            return None
    return None


def _artifact_needs_files_api(artifact: ResolvedArtifact) -> bool:
    # Only local-file artifacts inline their bytes; a large one must go through the
    # Files API. URL-sourced artifacts already reference by uri (no inline bytes).
    if artifact.local_path is None:
        return False
    size = _artifact_byte_size(artifact)
    return size is not None and size > _GEMINI_INLINE_MAX_BYTES


def _artifact_upload_key(artifact: ResolvedArtifact) -> str:
    # Dedupe uploads within a request by content identity when available, else by
    # resolved path, so an artifact referenced twice uploads once.
    return artifact.sha256 or str(artifact.local_path)


def _gemini_file_state(file: Any) -> str:
    # File.state is a FileState enum (ACTIVE/PROCESSING/FAILED); use its name and
    # fall back to str for a plain-string state in tests.
    state = getattr(file, "state", None)
    return getattr(state, "name", None) or str(state or "")


def _await_gemini_file_active(client: Any, file: Any) -> Any:
    # Images/PDFs/audio upload ACTIVE immediately; video may sit in PROCESSING
    # while it transcodes. Poll until ACTIVE (bounded), failing loud on a FAILED
    # upload or timeout rather than referencing an unusable file.
    deadline = time.monotonic() + _GEMINI_FILE_ACTIVE_TIMEOUT_SECONDS
    while _gemini_file_state(file) == "PROCESSING":
        if time.monotonic() > deadline:
            raise ProviderConfigError(
                f"Gemini file {getattr(file, 'name', '?')!r} did not become ACTIVE "
                f"within {_GEMINI_FILE_ACTIVE_TIMEOUT_SECONDS:.0f}s",
                provider="gemini",
            )
        time.sleep(_GEMINI_FILE_POLL_INTERVAL_SECONDS)
        file = client.files.get(name=file.name)
    if _gemini_file_state(file) == "FAILED":
        raise ProviderConfigError(
            f"Gemini file upload failed: {getattr(file, 'name', '?')!r}", provider="gemini"
        )
    return file


def _gemini_referenced_artifacts(
    messages: list[ChatMessage], artifacts: tuple[ResolvedArtifactGroup, ...]
) -> list[ResolvedArtifact]:
    """The resolved artifacts actually referenced by the messages' Artifact(Group)
    parts (#358 review). Resolution mirrors what _to_gemini_parts does, so we never
    upload an artifact that was resolved into the request but never referenced
    (a pure-text call must not touch the Files API or pay for unsent data)."""
    referenced: list[ResolvedArtifact] = []
    seen: set[int] = set()

    def _add(artifact: ResolvedArtifact) -> None:
        if id(artifact) not in seen:
            seen.add(id(artifact))
            referenced.append(artifact)

    for message in messages:
        content = message.content
        if isinstance(content, str):
            continue
        for part in content:
            if isinstance(part, ArtifactGroupPart):
                for artifact in artifacts_for_group(artifacts, part.group, provider="gemini"):
                    _add(artifact)
            elif isinstance(part, ArtifactPart):
                _add(artifact_for_name(artifacts, part.artifact, provider="gemini"))
    return referenced


def _upload_oversize_artifacts(
    client: Any,
    messages: list[ChatMessage],
    artifacts: tuple[ResolvedArtifactGroup, ...],
) -> dict[str, Any]:
    """Upload each referenced local artifact over the inline cap via the Files API,
    once each, returning {upload_key: active File} for the request to reference
    (#358). Only artifacts actually referenced by the messages are uploaded."""
    uploaded: dict[str, Any] = {}
    for artifact in _gemini_referenced_artifacts(messages, artifacts):
        if _artifact_needs_files_api(artifact):
            key = _artifact_upload_key(artifact)
            if key in uploaded:
                continue
            config = {"mime_type": artifact.media_type} if artifact.media_type else None
            try:
                file = client.files.upload(file=str(artifact.local_path), config=config)
                active = _await_gemini_file_active(client, file)
            except ProviderError:
                raise
            except Exception as exc:
                _raise_gemini_provider_error(exc)
            if not getattr(active, "uri", None):
                raise ProviderConfigError(
                    f"Gemini Files API returned no uri for {key!r}", provider="gemini"
                )
            uploaded[key] = active
    return uploaded


def _to_gemini_artifact_part(
    artifact: ResolvedArtifact, uploaded: dict[str, Any] | None = None
) -> dict[str, Any]:
    # A local artifact uploaded via the Files API (oversize) is referenced by uri.
    if uploaded is not None and artifact.local_path is not None:
        file = uploaded.get(_artifact_upload_key(artifact))
        if file is not None:
            mime_type = artifact.media_type or getattr(file, "mime_type", None)
            if mime_type is None:
                # Parity with the inline path, which also requires a media type.
                raise ProviderConfigError(
                    "GeminiProvider cannot reference an uploaded artifact without a media type",
                    provider="gemini",
                )
            return {"file_data": {"file_uri": file.uri, "mime_type": mime_type}}
    return _to_gemini_inline_artifact_part(artifact)


def _to_gemini_inline_artifact_part(artifact: ResolvedArtifact) -> dict[str, Any]:
    source = artifact.ref.source
    # Invariant guard: an oversize local artifact must have been routed through the
    # Files API (_upload_oversize_artifacts) before reaching the inline builder.
    # Fail loud rather than inline bytes the API will reject (#358).
    if _artifact_needs_files_api(artifact):
        raise ProviderConfigError(
            "GeminiProvider reached the inline path for an artifact over the inline "
            "size cap; it must be uploaded via the Files API first",
            provider="gemini",
        )
    is_image = artifact.kind == "image" or (artifact.media_type or "").startswith("image/")
    if source.type == "local_path" and artifact.local_path is not None:
        media_type = artifact.media_type or ("image/png" if is_image else None)
        if is_image and media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ProviderConfigError(
                f"GeminiProvider does not support image media type {media_type!r}",
                provider="gemini",
            )
        if media_type is None:
            raise ProviderConfigError(
                "GeminiProvider cannot attach an artifact without a media type",
                provider="gemini",
            )
        return {"inline_data": {"mime_type": media_type, "data": artifact.local_path.read_bytes()}}
    if source.type == "url":
        if artifact.media_type is None:
            raise ProviderConfigError(
                "GeminiProvider needs a media type to attach a URL artifact",
                provider="gemini",
            )
        # audio/video are supported only as local inline data (the advertised
        # contract); a URL-sourced one needs the Files API path (#358). Fail loud
        # rather than silently sending it down the image/document file_data route.
        if _is_audio_or_video(artifact):
            raise ProviderConfigError(
                "GeminiProvider attaches audio/video only as local inline data; a "
                "URL-sourced audio/video artifact needs the Files API (tracked in #358)",
                provider="gemini",
            )
        return {"file_data": {"file_uri": source.url, "mime_type": artifact.media_type}}
    raise ProviderConfigError(
        "GeminiProvider cannot attach this artifact through the messages path",
        provider="gemini",
    )


def _validated_provider_result(
    result: Any,
    output_schema: type[BaseModel],
    *,
    usage_sink: Callable[[ProviderUsage], None] | None = None,
    model: str | None = None,
) -> BaseModel:
    _raise_if_truncated(result)
    if usage_sink is not None:
        usage = _gemini_usage(result, model=model)
        if usage is not None:
            usage_sink(usage)
    parsed = getattr(result, "parsed", None)
    if isinstance(parsed, output_schema):
        return parsed
    if parsed is not None:
        return output_schema.model_validate(parsed)
    text = getattr(result, "text", None)
    if isinstance(text, str) and text:
        return output_schema.model_validate_json(text)
    raise ProviderConfigError(
        "Gemini client returned a result without parsed structured output",
        provider="gemini",
    )


def _raise_if_truncated(result: Any) -> None:
    finish_reason = _finish_reason(result)
    raise_if_truncated(
        truncated=finish_reason == "MAX_TOKENS", provider="gemini", display_name="Gemini"
    )


def _finish_reason(result: Any) -> str | None:
    candidates = getattr(result, "candidates", None)
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    # The SDK exposes finish_reason as an enum; normalize to its name.
    return getattr(reason, "name", str(reason))


def _resolve_gemini_cache_id(
    cached_session: CachedSessionHandle | None, *, model: str | None
) -> str | None:
    """The cachedContent name to reference, or None when not caching.

    Fail loud rather than silently dropping the cached prefix: a ``supported``
    reference-style handle MUST carry a cache_id, match this provider, and match
    the call model (Gemini caches are model-bound) (#60 review)."""
    if cached_session is None or not cached_session.supported:
        return None
    if cached_session.provider != "gemini":
        raise ProviderConfigError(
            f"cached session was prepared by {cached_session.provider!r}, not gemini",
            provider="gemini",
        )
    if not cached_session.cache_id:
        raise ProviderConfigError(
            "Gemini reference-style cached session has no cache_id; the cached "
            "prefix would be silently lost",
            provider="gemini",
        )
    if cached_session.model and model and cached_session.model != model:
        raise ProviderConfigError(
            f"cached session model {cached_session.model!r} does not match call model "
            f"{model!r} (Gemini caches are model-bound)",
            provider="gemini",
        )
    return cached_session.cache_id


def _gemini_usage(result: Any, *, model: str | None) -> ProviderUsage | None:
    usage = getattr(result, "usage_metadata", None)
    if usage is None:
        return None
    input_tokens = _usage_int(usage, "prompt_token_count")
    output_tokens = _usage_int(usage, "candidates_token_count")
    # total_token_count includes thinking tokens for reasoning models, so it is
    # forwarded verbatim rather than derived from input + output.
    total_tokens = _usage_int(usage, "total_token_count")
    # Reference-cache accounting (#60): tokens served from a cachedContent (a hit).
    cache_read = _usage_int(usage, "cached_content_token_count")
    if all(v is None for v in (input_tokens, output_tokens, total_tokens, cache_read)):
        return None
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        model=model,
        cache_read_tokens=cache_read,
    )


def _usage_int(usage: Any, field: str) -> int | None:
    value = getattr(usage, field, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(field)
    return value if isinstance(value, int) else None


def _is_stale_cache_error(exc: Exception) -> bool:
    # Consulted ONLY when the call actually set cached_content. A referenced
    # cachedContent that expired or was deleted comes back as a 403/404, e.g.
    # "403 PERMISSION_DENIED ... CachedContent not found (or permission denied)"
    # (verified live, #368). Match on the status code (robust to message-wording
    # drift) OR the explicit CachedContent token (covers an odd-status variant).
    # A genuine auth/permission failure on a cached call is rare and recovers
    # safely: the uncached retry hits the same failure and ends terminally, so a
    # false positive costs at most one extra call — worth it for not silently
    # losing stale-cache recovery when Google rewords the message.
    return status_code(exc) in {403, 404} or "cachedcontent" in str(exc).lower()


def _raise_gemini_provider_error(exc: Exception, *, cached_content: str | None = None) -> NoReturn:
    if isinstance(exc, ValidationError):
        raise exc
    if isinstance(exc, ProviderError):
        raise exc
    if cached_content is not None and _is_stale_cache_error(exc):
        raise ProviderCacheUnavailableError(
            "gemini cached content is unavailable (expired or deleted)",
            provider="gemini",
            status_code=status_code(exc),
            original=exc,
        ) from exc
    raise _classify_gemini_error(exc) from exc


def _classify_gemini_error(exc: Exception) -> ProviderError:
    code = status_code(exc)
    reason = "gemini structured call failed"
    if matches_provider_error(exc, "google", "RateLimitError") or code == 429:
        return ProviderRateLimitError(
            reason,
            provider="gemini",
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if code in {401, 403}:
        return ProviderAuthError(reason, provider="gemini", status_code=code, original=exc)
    if is_network_or_timeout_error(exc) or (code is not None and code >= 500):
        return ProviderTransientError(
            reason,
            provider="gemini",
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if code in {408, 409}:
        return ProviderTransientError(
            reason,
            provider="gemini",
            status_code=code,
            original=exc,
            retry_after_seconds=retry_after_seconds(exc),
        )
    if code is not None and 400 <= code < 500:
        return ProviderConfigError(reason, provider="gemini", status_code=code, original=exc)
    return ProviderError(
        reason=reason, provider="gemini", retryable=False, status_code=code, original=exc
    )


__all__ = ["GeminiProvider"]
