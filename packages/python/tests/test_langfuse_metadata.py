from __future__ import annotations

from pydantic import BaseModel

from typeflux.core.contracts import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.manifests import (
    AIInvocationContext,
    build_activity_execution_manifest,
    build_activity_manifest,
)
from typeflux.observability import semantic as semantic_module
from typeflux.observability.semantic import semantic_metadata


class Input(BaseModel):
    text: str


class Output(BaseModel):
    label: str


def test_activity_metadata_contains_full_manifest() -> None:
    activity, resolved, rendered = _activity_context()
    manifest = build_activity_manifest(activity, resolved)
    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=manifest,
        resolved_prompt=resolved,
        rendered_messages=rendered,
        validation_attempt=0,
    )

    metadata = semantic_metadata(
        manifest=manifest,
        activity_execution_manifest=execution_manifest,
        invocation_context=None,
        level="activity",
    )

    typeflux = metadata["typeflux"]
    assert typeflux["level"] == "activity"
    assert typeflux["activity_name"] == "classify"
    assert typeflux["activity_execution_manifest"]["activity_name"] == "classify"
    assert typeflux["join"]["activity_execution_manifest_hash"] == execution_manifest.manifest_hash
    assert "activity" not in typeflux


def test_generation_metadata_uses_compact_join_without_full_manifest() -> None:
    activity, resolved, rendered = _activity_context()
    manifest = build_activity_manifest(activity, resolved)
    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=manifest,
        resolved_prompt=resolved,
        rendered_messages=rendered,
        validation_attempt=0,
    )

    metadata = semantic_metadata(
        manifest=manifest,
        activity_execution_manifest=execution_manifest,
        invocation_context=None,
        level="generation",
        validation_attempt=0,
    )

    typeflux = metadata["typeflux"]
    assert typeflux["level"] == "generation"
    assert typeflux["activity_name"] == "classify"
    assert typeflux["join"]["activity_execution_manifest_hash"] == execution_manifest.manifest_hash
    assert typeflux["activity"]["activity_name"] == "classify"
    assert "activity_execution_manifest" not in typeflux
    assert "validation_attempt" not in typeflux["activity"]
    assert typeflux["generation"] == {"validation_attempt": 0}


def test_generation_metadata_preserves_provider_controls() -> None:
    activity, resolved, rendered = _activity_context()
    manifest = build_activity_manifest(activity, resolved)
    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=manifest,
        resolved_prompt=resolved,
        rendered_messages=rendered,
        validation_attempt=0,
    )

    metadata = semantic_metadata(
        manifest=manifest,
        activity_execution_manifest=execution_manifest,
        invocation_context=None,
        level="generation",
        validation_attempt=0,
        provider_controls={
            "execution_mode": "async",
            "provider_name": "openai",
            "provider_model": "gpt-4o-mini",
            "retry_attempt": 0,
            "max_attempts": 1,
        },
    )

    assert metadata["typeflux"]["provider_controls"] == {
        "execution_mode": "async",
        "provider_name": "openai",
        "provider_model": "gpt-4o-mini",
        "retry_attempt": 0,
        "max_attempts": 1,
    }


def test_provider_controls_metadata_extracts_typeflux_provider_controls() -> None:
    metadata = {
        "typeflux": {
            "provider_controls": {
                "execution_mode": "async",
                "queued": True,
            }
        }
    }

    assert semantic_module._provider_controls_metadata(metadata) == {
        "execution_mode": "async",
        "queued": True,
    }


def test_activity_metadata_includes_map_context() -> None:
    activity, resolved, rendered = _activity_context()
    manifest = build_activity_manifest(activity, resolved)
    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=manifest,
        resolved_prompt=resolved,
        rendered_messages=rendered,
        validation_attempt=0,
    )

    metadata = semantic_metadata(
        manifest=manifest,
        activity_execution_manifest=execution_manifest,
        invocation_context=AIInvocationContext(
            temporal_namespace="default",
            temporal_workflow_type="MapWorkflow",
            temporal_workflow_id="wf-map",
            temporal_run_id="run-map",
            temporal_activity_type="classify",
            temporal_activity_id="review_pages-2",
            temporal_activity_attempt=1,
            typeflux_activity_name="classify",
            typeflux_manifest_hash="pending",
            map_step_id="review_pages",
            map_index=2,
            map_size=4,
            map_concurrency=2,
        ),
        level="activity",
    )

    assert metadata["typeflux"]["map"] == {
        "map_step_id": "review_pages",
        "map_index": 2,
        "map_size": 4,
        "map_concurrency": 2,
    }


def _activity_context():
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="7",
    )
    rendered = (ChatMessage(role="user", content="Classify hello"),)
    return activity, resolved, rendered
