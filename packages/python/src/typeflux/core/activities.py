from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from inspect import Signature, signature
from typing import Any, get_type_hints

from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactInput
from typeflux.core.contracts import (
    AIActivity,
    CacheConfig,
    HookFn,
    ModerationConfig,
    OutputCheck,
    PromptRef,
    RetryPolicy,
    SessionCacheConfig,
)


def _infer_input_type(fn: Callable[..., Any]) -> type[BaseModel]:
    sig = signature(fn)
    params = list(sig.parameters.values())
    # (input, output) or (input, output, ctx); full arity/annotation validation
    # runs in AIActivity.__post_init__ via _validate_hook_signature (#397).
    if len(params) not in (2, 3):
        raise TypeError("ai_activity hooks must accept (input, output) or (input, output, ctx)")

    hints = get_type_hints(fn)
    annotation = hints.get(params[0].name, params[0].annotation)
    if annotation is Signature.empty:
        raise TypeError("first hook parameter must be annotated with input schema")
    if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
        raise TypeError("first hook parameter must be a Pydantic BaseModel type")
    return annotation


class _AIActivityDecorator:
    def defn(
        self,
        *,
        name: str | None = None,
        prompt: PromptRef,
        output: type[BaseModel],
        validation_retries: int = 1,
        task_queue: str | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        artifact_inputs: tuple[ArtifactInput, ...] = (),
        cache: CacheConfig | None = None,
        moderation: ModerationConfig | None = None,
        session_cache: SessionCacheConfig | None = None,
        output_check: OutputCheck | None = None,
    ) -> Callable[[HookFn], AIActivity]:
        def decorator(fn: HookFn) -> AIActivity:
            activity_name = name or fn.__name__
            return AIActivity(
                name=activity_name,
                input_type=_infer_input_type(fn),
                output_type=output,
                prompt_ref=prompt,
                hook=fn,
                validation_retries=validation_retries,
                task_queue=task_queue,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=retry_policy,
                artifact_inputs=artifact_inputs,
                cache=cache,
                moderation=moderation,
                session_cache=session_cache,
                output_check=output_check,
            )

        return decorator


ai_activity = _AIActivityDecorator()


__all__ = ["ai_activity"]
