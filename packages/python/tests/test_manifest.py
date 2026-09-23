from __future__ import annotations

import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, create_model

from typeflux.core.contracts import (
    ActivityDefinitionSource,
    AIActivity,
    ChatMessage,
    PromptRef,
    ProviderParams,
    ResolvedPrompt,
)
from typeflux.execution.starter import workflow_invocation_metadata
from typeflux.manifests import (
    build_activity_execution_manifest,
    build_activity_manifest,
    build_activity_rollup_entry,
    build_unresolved_activity_rollup_entry,
    build_workflow_execution_manifest,
    collect_code_provenance,
    compact_activity_manifest,
    merge_activity_rollup,
    reconstruct_execution_manifest,
    schema_hash,
)
from typeflux.manifests import provenance as provenance_module
from typeflux.metadata import RuntimePlacementContributor


class Input(BaseModel):
    text: str


class Output(BaseModel):
    label: str


def test_workflow_execution_manifest_hash_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    monkeypatch.setenv("TYPEFLUX_GIT_REF", "main")
    first = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=["classify"],
    )
    second = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=["classify"],
    )

    assert first.manifest_hash == second.manifest_hash
    assert first.workflow_contract_hash == second.workflow_contract_hash
    assert first.to_dict()["code_provenance"]["git_sha"] == "abc123"


def test_workflow_contract_hash_ignores_runtime_and_deployment_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    monkeypatch.setenv("TYPEFLUX_GIT_REF", "main")
    first = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="local",
        activities=[
            {
                "activity_name": "classify",
                "activity_manifest_hash": "activity-contract",
                "input_schema": {"name": "Input", "hash": "input-hash"},
                "output_schema": {"name": "Output", "hash": "output-hash"},
                "prompt_ref": {"name": "demo/classify"},
                "resolved_prompt_version": "1",
                "prompt_messages_hash": "prompt-hash",
                "provider_model": "gpt-4o-mini",
            }
        ],
        contributions={
            "temporal_connection": {
                "address": "local",
                "namespace": "default",
                "region": "local",
            },
            "yaml_overrides": {"environment_id": "local"},
            "secret_references": {"references": []},
        },
        yaml_project="demo",
        yaml_name="support",
        sdk_version="test",
    )
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "def456")
    monkeypatch.setenv("TYPEFLUX_GIT_REF", "release")
    second = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-2",
        temporal_run_id="run-2",
        task_queue="cloud",
        activities=[
            {
                "activity_name": "classify",
                "activity_manifest_hash": "activity-contract",
                "input_schema": {"name": "Input", "hash": "input-hash"},
                "output_schema": {"name": "Output", "hash": "output-hash"},
                "prompt_ref": {"name": "demo/classify"},
                "resolved_prompt_version": "1",
                "prompt_messages_hash": "prompt-hash",
                "provider_model": "gpt-4o-mini",
            }
        ],
        contributions={
            "temporal_connection": {
                "address": "namespace.tmprl.cloud:7233",
                "namespace": "namespace",
                "region": "us-east",
            },
            "yaml_overrides": {"environment_id": "temporal_cloud_dev"},
            "secret_references": {"references": [{"configured": True}]},
        },
        yaml_project="demo",
        yaml_name="support",
        sdk_version="test",
    )

    assert first.workflow_contract_hash == second.workflow_contract_hash
    assert first.manifest_hash != second.manifest_hash


def test_workflow_manifest_hash_ignores_runtime_placement_trace_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    monkeypatch.setenv("TYPEFLUX_GIT_REF", "main")

    first = workflow_invocation_metadata(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        metadata_contributors=(
            RuntimePlacementContributor(
                platform="kubernetes",
                k8s_namespace="typeflux-smoke",
                k8s_pod_name="worker-a",
                k8s_pod_uid="uid-a",
                k8s_node_name="node-a",
                k8s_deployment_name="worker",
                container_image="typeflux-worker:k8s-smoke",
            ),
        ),
    )
    second = workflow_invocation_metadata(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        metadata_contributors=(
            RuntimePlacementContributor(
                platform="kubernetes",
                k8s_namespace="typeflux-smoke",
                k8s_pod_name="worker-b",
                k8s_pod_uid="uid-b",
                k8s_node_name="node-b",
                k8s_deployment_name="worker",
                container_image="typeflux-worker:k8s-smoke",
            ),
        ),
    )

    first_manifest = first["typeflux"]["execution_manifest"]
    second_manifest = second["typeflux"]["execution_manifest"]
    assert first_manifest["workflow_contract_hash"] == second_manifest["workflow_contract_hash"]
    assert first_manifest["manifest_hash"] == second_manifest["manifest_hash"]
    assert first["typeflux"]["runtime_placement"]["kubernetes"]["pod_name"] == "worker-a"
    assert second["typeflux"]["runtime_placement"]["kubernetes"]["pod_name"] == "worker-b"
    assert "runtime_placement" not in first_manifest.get("contributions", {})


def test_workflow_contract_hash_tracks_contract_changes() -> None:
    base = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            {
                "activity_name": "classify",
                "activity_manifest_hash": "activity-contract",
                "input_schema": {"name": "Input", "hash": "input-hash"},
                "output_schema": {"name": "Output", "hash": "output-hash"},
                "prompt_ref": {"name": "demo/classify"},
                "resolved_prompt_version": "1",
                "prompt_messages_hash": "prompt-hash",
                "provider_model": "gpt-4o-mini",
            }
        ],
        yaml_project="demo",
        yaml_name="support",
    )
    changed_prompt = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            {
                "activity_name": "classify",
                "activity_manifest_hash": "activity-contract-v2",
                "input_schema": {"name": "Input", "hash": "input-hash"},
                "output_schema": {"name": "Output", "hash": "output-hash"},
                "prompt_ref": {"name": "demo/classify"},
                "resolved_prompt_version": "2",
                "prompt_messages_hash": "prompt-hash-v2",
                "provider_model": "gpt-4o-mini",
            }
        ],
        yaml_project="demo",
        yaml_name="support",
    )
    changed_map = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=base.activities,
        map_steps=[{"map_step_id": "fanout", "activity_name": "classify", "over": "items"}],
        yaml_project="demo",
        yaml_name="support",
    )
    changed_workflow_name = build_workflow_execution_manifest(
        workflow_name="OtherWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=base.activities,
        yaml_project="demo",
        yaml_name="support",
    )
    changed_yaml_identity = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=base.activities,
        yaml_project="demo",
        yaml_name="support_v2",
    )

    assert base.workflow_contract_hash != changed_prompt.workflow_contract_hash
    assert base.workflow_contract_hash != changed_map.workflow_contract_hash
    assert base.workflow_contract_hash != changed_workflow_name.workflow_contract_hash
    assert base.workflow_contract_hash != changed_yaml_identity.workflow_contract_hash


def test_provider_behavior_params_change_workflow_contract_hash() -> None:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
        model="test-model",
    )

    first = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            build_activity_rollup_entry(
                activity=activity,
                resolved_prompt=resolved,
                provider_params=ProviderParams(model="test-model", max_tokens=4096),
            )
        ],
    )
    second = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            build_activity_rollup_entry(
                activity=activity,
                resolved_prompt=resolved,
                provider_params=ProviderParams(model="test-model", max_tokens=12000),
            )
        ],
    )

    assert first.workflow_contract_hash != second.workflow_contract_hash
    assert first.manifest_hash != second.manifest_hash


def test_provider_timeout_does_not_change_workflow_contract_hash() -> None:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
        model="test-model",
    )

    first = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            build_activity_rollup_entry(
                activity=activity,
                resolved_prompt=resolved,
                provider_params=ProviderParams(model="test-model", timeout=30),
            )
        ],
    )
    second = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            build_activity_rollup_entry(
                activity=activity,
                resolved_prompt=resolved,
                provider_params=ProviderParams(model="test-model", timeout=60),
            )
        ],
    )

    assert first.workflow_contract_hash == second.workflow_contract_hash
    assert first.manifest_hash != second.manifest_hash


def test_activity_timeout_does_not_change_workflow_contract_hash() -> None:
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
        model="test-model",
    )
    first_activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        start_to_close_timeout=timedelta(seconds=45),
    )
    second_activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        start_to_close_timeout=timedelta(seconds=120),
    )

    first = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[build_activity_rollup_entry(activity=first_activity, resolved_prompt=resolved)],
    )
    second = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            build_activity_rollup_entry(activity=second_activity, resolved_prompt=resolved)
        ],
    )

    assert isinstance(first.activities[0], dict)
    assert first.activities[0]["start_to_close_timeout_seconds"] == 45.0
    assert first.workflow_contract_hash == second.workflow_contract_hash
    assert first.manifest_hash != second.manifest_hash


def test_activity_execution_manifest_records_effective_provider_params() -> None:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        start_to_close_timeout=timedelta(seconds=120),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
        model="test-model",
    )

    execution = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=build_activity_manifest(
            activity,
            resolved,
            provider_params=ProviderParams(model="test-model", max_tokens=12000),
        ),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
        provider_params=ProviderParams(model="test-model", max_tokens=12000, timeout=45),
    ).to_dict()

    assert execution["provider_params"] == {
        "model": "test-model",
        "max_tokens": 12000,
        "timeout": 45,
    }
    assert execution["start_to_close_timeout_seconds"] == 120.0


def _model_source_fixture(
    *,
    prompt_model: str | None,
    activity_model: str | None = None,
) -> tuple[AIActivity, ResolvedPrompt]:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        provider_params=ProviderParams(model=activity_model),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
        model=prompt_model,
    )
    return activity, resolved


def _execution_manifest_dict(activity: AIActivity, resolved: ResolvedPrompt) -> dict:
    return build_activity_execution_manifest(
        activity=activity,
        activity_manifest=build_activity_manifest(activity, resolved),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
        provider_params=ProviderParams(model="yaml-model"),
    ).to_dict()


def test_execution_manifest_records_yaml_provider_model_source() -> None:
    activity, resolved = _model_source_fixture(prompt_model=None)

    execution = _execution_manifest_dict(activity, resolved)

    assert execution["provider_model"] == "yaml-model"
    assert execution["provider_model_source"] == "yaml_provider"


def test_execution_manifest_records_prompt_config_model_source() -> None:
    activity, resolved = _model_source_fixture(prompt_model="prompt-model")

    execution = _execution_manifest_dict(activity, resolved)

    assert execution["provider_model_source"] == "prompt_config"


def test_activity_level_model_keeps_yaml_provider_source() -> None:
    activity, resolved = _model_source_fixture(
        prompt_model="prompt-model",
        activity_model="activity-model",
    )

    execution = _execution_manifest_dict(activity, resolved)

    assert execution["provider_model_source"] == "yaml_provider"


def test_rollup_entry_and_compact_manifest_carry_provider_model_source() -> None:
    activity, resolved = _model_source_fixture(prompt_model="prompt-model")

    rollup = build_activity_rollup_entry(activity=activity, resolved_prompt=resolved)
    compact = compact_activity_manifest(_execution_manifest_dict(activity, resolved))

    assert rollup["provider_model_source"] == "prompt_config"
    assert compact["provider_model_source"] == "prompt_config"


def test_workflow_contract_hash_ignores_activity_execution_only_rollup_fields() -> None:
    contract_activity = {
        "activity_name": "classify",
        "activity_manifest_hash": "activity-contract",
        "input_schema": {"name": "Input", "hash": "input-hash"},
        "output_schema": {"name": "Output", "hash": "output-hash"},
        "prompt_ref": {"name": "demo/classify"},
        "resolved_prompt_version": "1",
        "prompt_messages_hash": "prompt-hash",
        "provider_model": "gpt-4o-mini",
    }
    before_execution = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[contract_activity],
    )
    after_execution = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            {
                **contract_activity,
                "manifest_hash": "activity-execution",
                "rendered_messages_hash": "rendered-hash",
                "artifacts": [{"sha256": "artifact-hash", "byte_size": 42}],
                "validation_attempt": 1,
            }
        ],
    )

    assert before_execution.workflow_contract_hash == after_execution.workflow_contract_hash
    assert before_execution.manifest_hash != after_execution.manifest_hash


def test_workflow_contract_hash_is_preserved_when_temporal_run_id_is_added() -> None:
    manifest = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=["classify"],
    )
    updated = manifest.with_temporal_run_id("run-1")

    assert updated.workflow_contract_hash == manifest.workflow_contract_hash
    assert updated.manifest_hash != manifest.manifest_hash


def test_workflow_execution_manifest_can_include_map_steps() -> None:
    manifest = build_workflow_execution_manifest(
        workflow_name="MapWorkflow",
        workflow_id="workflow-map",
        task_queue="demo",
        activities=["review_page", "consolidate"],
        map_steps=[
            {
                "map_step_id": "review_pages",
                "activity_name": "review_page",
                "over": "input.pages",
                "map_size": 3,
                "map_concurrency": 2,
                "collect_output": "PageReviewBatch",
                "collect_field": "reviews",
            }
        ],
    )

    assert manifest.to_dict()["map_steps"] == [
        {
            "map_step_id": "review_pages",
            "activity_name": "review_page",
            "over": "input.pages",
            "map_size": 3,
            "map_concurrency": 2,
            "collect_output": "PageReviewBatch",
            "collect_field": "reviews",
        }
    ]


def test_workflow_execution_manifest_can_include_generic_contributions() -> None:
    manifest = build_workflow_execution_manifest(
        workflow_name="ContributionWorkflow",
        workflow_id="workflow-contributions",
        task_queue="demo",
        activities=["review_page"],
        contributions={"demo": {"enabled": True, "source": "test"}},
    )

    assert manifest.to_dict()["contributions"] == {"demo": {"enabled": True, "source": "test"}}


def test_activity_execution_manifest_hashes_prompts_without_storing_text() -> None:
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
        model="test-model",
        temperature=0,
    )
    activity_manifest = build_activity_manifest(activity, resolved)

    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=activity_manifest,
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
        provider_model="test-model",
    )

    payload = execution_manifest.to_dict()
    assert payload["prompt_ref"] == {"name": "demo/classify", "version": None, "label": None}
    assert payload["resolved_prompt_version"] == "7"
    assert "prompt_messages_hash" in payload
    assert "rendered_messages_hash" in payload
    assert "Classify" not in str(payload)
    assert "hello" not in str(payload)


def test_activity_execution_manifest_includes_definition_source_in_execution_hash() -> None:
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="7",
    )
    yaml_activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        definition_source=ActivityDefinitionSource(
            kind="yaml",
            yaml_project="demo",
            yaml_name="demo_yaml",
        ),
    )
    python_activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        definition_source=ActivityDefinitionSource(
            kind="python",
            module="demo.activities",
            export="ALL_ACTIVITIES",
        ),
    )
    yaml_activity_manifest = build_activity_manifest(yaml_activity, resolved)
    python_activity_manifest = build_activity_manifest(python_activity, resolved)

    yaml_execution_manifest = build_activity_execution_manifest(
        activity=yaml_activity,
        activity_manifest=yaml_activity_manifest,
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
    )
    python_execution_manifest = build_activity_execution_manifest(
        activity=python_activity,
        activity_manifest=python_activity_manifest,
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
    )

    assert yaml_activity_manifest.manifest_hash == python_activity_manifest.manifest_hash
    assert yaml_execution_manifest.manifest_hash != python_execution_manifest.manifest_hash
    assert yaml_execution_manifest.to_dict()["definition_source"] == {
        "kind": "yaml",
        "yaml_project": "demo",
        "yaml_name": "demo_yaml",
    }


def test_compact_activity_manifest_keeps_rebuild_identity_without_attempt_detail() -> None:
    payload = _activity_execution_manifest("classify")

    compact = compact_activity_manifest(payload)

    assert compact["activity_name"] == "classify"
    assert compact["manifest_hash"] == payload["manifest_hash"]
    assert compact["input_schema"] == payload["input_schema"]
    assert compact["output_schema"] == payload["output_schema"]
    assert compact["prompt_ref"] == payload["prompt_ref"]
    assert compact["definition_source"] == payload["definition_source"]
    assert compact["prompt_messages_hash"] == payload["prompt_messages_hash"]
    assert compact["rendered_messages_hash"] == payload["rendered_messages_hash"]
    assert "validation_attempt" not in compact


def test_unresolved_activity_rollup_entry_keeps_definition_source() -> None:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
        definition_source=ActivityDefinitionSource(
            kind="yaml",
            yaml_project="demo",
            yaml_name="demo_yaml",
        ),
    )

    rollup = build_unresolved_activity_rollup_entry(activity=activity, error=RuntimeError("boom"))

    assert rollup["definition_source"] == {
        "kind": "yaml",
        "yaml_project": "demo",
        "yaml_name": "demo_yaml",
    }


def test_unresolved_activity_rollup_uses_nested_prompt_resolution_only() -> None:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
    )

    rollup = build_unresolved_activity_rollup_entry(activity=activity, error=RuntimeError("boom"))

    assert rollup["prompt_resolution"] == {
        "status": "failed",
        "error_type": "RuntimeError",
        "retryable": False,
        "reason": "unexpected prompt resolution failure",
    }
    assert not any(key.startswith("typeflux.") for key in rollup)


def test_activity_execution_manifest_omits_unstable_schema_modules() -> None:
    dynamic_input = create_model("DynamicInput", text=(str, ...), __module__="__main__")
    dynamic_output = create_model("DynamicOutput", label=(str, ...), __module__="__main__")
    activity = AIActivity(
        name="dynamic",
        input_type=dynamic_input,
        output_type=dynamic_output,
        prompt_ref=PromptRef("demo/dynamic"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/dynamic"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
    )

    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=build_activity_manifest(activity, resolved),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
    ).to_dict()

    assert execution_manifest["input_schema"] == {
        "name": "DynamicInput",
        "hash": schema_hash(dynamic_input),
        "module_status": "unstable",
        "module_warning": "schema module omitted because it is not a stable import path",
    }
    assert execution_manifest["output_schema"] == {
        "name": "DynamicOutput",
        "hash": schema_hash(dynamic_output),
        "module_status": "unstable",
        "module_warning": "schema module omitted because it is not a stable import path",
    }


def test_activity_rollups_use_schema_identity_fallback_for_unstable_modules() -> None:
    dynamic_input = create_model("DynamicInput", text=(str, ...), __module__="__main__")
    dynamic_output = create_model("DynamicOutput", label=(str, ...), __module__="__main__")
    activity = AIActivity(
        name="dynamic",
        input_type=dynamic_input,
        output_type=dynamic_output,
        prompt_ref=PromptRef("demo/dynamic"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/dynamic"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
    )

    rollup = build_activity_rollup_entry(activity=activity, resolved_prompt=resolved)
    unresolved = build_unresolved_activity_rollup_entry(
        activity=activity, error=RuntimeError("boom")
    )

    assert rollup["input_schema"] == unresolved["input_schema"]
    assert rollup["output_schema"] == unresolved["output_schema"]
    assert "module" not in rollup["input_schema"]
    assert rollup["input_schema"]["module_status"] == "unstable"
    assert rollup["output_schema"]["module_warning"] == (
        "schema module omitted because it is not a stable import path"
    )


def test_activity_execution_manifest_keeps_stable_schema_modules() -> None:
    activity = AIActivity(
        name="classify",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/classify"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/classify"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
    )

    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=build_activity_manifest(activity, resolved),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
    ).to_dict()

    assert execution_manifest["input_schema"] == {
        "module": __name__,
        "name": "Input",
        "hash": schema_hash(Input),
    }
    assert execution_manifest["output_schema"] == {
        "module": __name__,
        "name": "Output",
        "hash": schema_hash(Output),
    }


def test_unstable_schema_manifest_hash_ignores_reported_dynamic_module() -> None:
    first_input = create_model("DynamicInput", text=(str, ...), __module__="__main__")
    first_output = create_model("DynamicOutput", label=(str, ...), __module__="__main__")
    second_input = create_model("DynamicInput", text=(str, ...), __module__="pydantic.dynamic")
    second_output = create_model("DynamicOutput", label=(str, ...), __module__="pydantic.dynamic")
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/dynamic"),
        messages=(ChatMessage(role="user", content="Classify {{ text }}"),),
        resolved_version="1",
    )

    first_activity = AIActivity(
        name="dynamic",
        input_type=first_input,
        output_type=first_output,
        prompt_ref=PromptRef("demo/dynamic"),
    )
    second_activity = AIActivity(
        name="dynamic",
        input_type=second_input,
        output_type=second_output,
        prompt_ref=PromptRef("demo/dynamic"),
    )
    first = build_activity_execution_manifest(
        activity=first_activity,
        activity_manifest=build_activity_manifest(first_activity, resolved),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
    )
    second = build_activity_execution_manifest(
        activity=second_activity,
        activity_manifest=build_activity_manifest(second_activity, resolved),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Classify hello"),),
        validation_attempt=0,
    )

    assert first.to_dict()["input_schema"] == second.to_dict()["input_schema"]
    assert first.to_dict()["output_schema"] == second.to_dict()["output_schema"]
    assert first.manifest_hash == second.manifest_hash


def test_merge_activity_rollup_replaces_expected_name_with_compact_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    workflow = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=["classify", "route"],
    ).to_dict()
    activity = _activity_execution_manifest("classify")

    merged = merge_activity_rollup(workflow, activity)

    assert isinstance(merged["activities"][0], dict)
    assert merged["activities"][0]["activity_name"] == "classify"
    assert merged["activities"][1] == "route"


def test_collect_code_provenance_prefers_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "sha-from-env")
    monkeypatch.setenv("TYPEFLUX_GIT_REF", "refs/heads/main")
    monkeypatch.setenv("TYPEFLUX_REPO_URL", "https://github.com/acme/typeflux-app")
    monkeypatch.setenv("TYPEFLUX_DEPLOYMENT_ID", "deploy-123")
    monkeypatch.setenv("TYPEFLUX_ENVIRONMENT", "production")

    provenance = collect_code_provenance(tmp_path)

    assert provenance.available is True
    assert provenance.source == "env"
    assert provenance.git_sha == "sha-from-env"
    assert provenance.deployment_id == "deploy-123"


def test_collect_code_provenance_deployment_vars_do_not_suppress_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #829: the documented default .env sets ONLY the deployment pair; git detection
    # must still run and traces must carry git identity (deployment fields merge in).
    _clear_provenance_env(monkeypatch)
    monkeypatch.setenv("TYPEFLUX_DEPLOYMENT_ID", "deploy-123")
    monkeypatch.setenv("TYPEFLUX_ENVIRONMENT", "local")
    provenance_module._clear_code_provenance_cache()

    def fake_git(args, cwd):
        if args == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        if args == ["status", "--porcelain"]:
            return ""
        if args == ["remote", "get-url", "origin"]:
            return "https://example.com/acme/typeflux-app.git"
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        raise AssertionError(args)

    monkeypatch.setattr(provenance_module, "_git", fake_git)

    provenance = collect_code_provenance(tmp_path)

    assert provenance.available is True
    assert provenance.source == "git"
    assert provenance.git_sha == "abc123"
    assert provenance.deployment_id == "deploy-123"
    assert provenance.environment == "local"


def test_collect_code_provenance_deployment_vars_only_without_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #829: deployment identity without a repo or the TYPEFLUX_GIT_* triplet is
    # honestly `unavailable` code identity (it was `source: env` before the fix).
    _clear_provenance_env(monkeypatch)
    monkeypatch.setenv("TYPEFLUX_DEPLOYMENT_ID", "deploy-123")
    monkeypatch.setenv("TYPEFLUX_ENVIRONMENT", "local")
    provenance_module._clear_code_provenance_cache()
    monkeypatch.setattr(provenance_module, "_git", lambda args, cwd: None)

    provenance = collect_code_provenance(tmp_path)

    assert provenance.available is False
    assert provenance.source == "unavailable"
    assert provenance.git_sha is None
    assert provenance.deployment_id == "deploy-123"
    assert provenance.environment == "local"


def test_collect_code_provenance_cached_entry_serves_fresh_deployment_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #829 review finding: the cache holds code identity only — deployment vars set
    # AFTER the first (cached) read must still appear on later reads.
    _clear_provenance_env(monkeypatch)
    provenance_module._clear_code_provenance_cache()
    monkeypatch.setattr(provenance_module, "_git", lambda args, cwd: None)

    first = collect_code_provenance(tmp_path)
    assert first.deployment_id is None

    monkeypatch.setenv("TYPEFLUX_DEPLOYMENT_ID", "deploy-456")
    monkeypatch.setenv("TYPEFLUX_ENVIRONMENT", "staging")

    second = collect_code_provenance(tmp_path)
    assert second.source == "unavailable"
    assert second.deployment_id == "deploy-456"
    assert second.environment == "staging"


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    (
        pytest.param(
            "https://user:token@example.com/acme/typeflux-app.git",
            "https://example.com/acme/typeflux-app.git",
            id="https-user-token",
        ),
        pytest.param(
            "https://token@example.com/acme/typeflux-app.git",
            "https://example.com/acme/typeflux-app.git",
            id="https-token-only",
        ),
        pytest.param(
            "https://example.com/acme/typeflux-app.git?access_token=token#main",
            "https://example.com/acme/typeflux-app.git",
            id="https-query-fragment",
        ),
        pytest.param(
            "https://x-access-token:ghp_secret@github.com/acme/typeflux-app.git?token=secret",
            "https://github.com/acme/typeflux-app.git",
            id="github-token-url",
        ),
        pytest.param(
            "ssh://git@example.com/acme/typeflux-app.git",
            "ssh://example.com/acme/typeflux-app.git",
            id="ssh-url",
        ),
        pytest.param(
            "git@example.com:acme/typeflux-app.git",
            "example.com:acme/typeflux-app.git",
            id="ssh-scp-like",
        ),
        pytest.param(
            "../typeflux-app.git",
            "../typeflux-app.git",
            id="non-url",
        ),
    ),
)
def test_collect_code_provenance_sanitizes_env_repo_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_url: str,
    expected: str,
) -> None:
    monkeypatch.setenv("TYPEFLUX_REPO_URL", repo_url)

    provenance = collect_code_provenance(tmp_path)

    assert provenance.source == "env"
    assert provenance.repo_url == expected


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    (
        pytest.param(
            "https://user:token@example.com/acme/typeflux-app.git",
            "https://example.com/acme/typeflux-app.git",
            id="https-user-token",
        ),
        pytest.param(
            "https://token@example.com/acme/typeflux-app.git",
            "https://example.com/acme/typeflux-app.git",
            id="https-token-only",
        ),
        pytest.param(
            "https://example.com/acme/typeflux-app.git?access_token=token#main",
            "https://example.com/acme/typeflux-app.git",
            id="https-query-fragment",
        ),
        pytest.param(
            "https://x-access-token:ghp_secret@github.com/acme/typeflux-app.git?token=secret",
            "https://github.com/acme/typeflux-app.git",
            id="github-token-url",
        ),
        pytest.param(
            "ssh://git@example.com/acme/typeflux-app.git",
            "ssh://example.com/acme/typeflux-app.git",
            id="ssh-url",
        ),
        pytest.param(
            "git@example.com:acme/typeflux-app.git",
            "example.com:acme/typeflux-app.git",
            id="ssh-scp-like",
        ),
        pytest.param(
            "../typeflux-app.git",
            "../typeflux-app.git",
            id="non-url",
        ),
    ),
)
def test_collect_code_provenance_sanitizes_git_repo_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_url: str,
    expected: str,
) -> None:
    _clear_provenance_env(monkeypatch)
    provenance_module._clear_code_provenance_cache()

    def fake_git(args, cwd):
        if args == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        if args == ["status", "--porcelain"]:
            return ""
        if args == ["remote", "get-url", "origin"]:
            return repo_url
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        raise AssertionError(args)

    monkeypatch.setattr(provenance_module, "_git", fake_git)

    provenance = collect_code_provenance(tmp_path)

    assert provenance.source == "git"
    assert provenance.repo_url == expected


def test_collect_code_provenance_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_provenance_env(monkeypatch)

    provenance = collect_code_provenance(tmp_path)

    assert provenance.available is False
    assert provenance.source == "unavailable"


def test_collect_code_provenance_reads_clean_and_dirty_git_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    _clear_provenance_env(monkeypatch)
    provenance_module._clear_code_provenance_cache()
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tester@example.com")
    _git(tmp_path, "config", "user.name", "Tester")
    (tmp_path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")

    clean = collect_code_provenance(tmp_path)
    (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    provenance_module._clear_code_provenance_cache()
    dirty = collect_code_provenance(tmp_path)

    assert clean.available is True
    assert clean.source == "git"
    assert clean.git_sha is not None
    assert clean.dirty is False
    assert dirty.dirty is True
    assert dirty.dirty_hash is not None


def test_collect_code_provenance_caches_git_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_provenance_env(monkeypatch)
    provenance_module._clear_code_provenance_cache()
    calls = []

    def fake_git(args, cwd):
        calls.append((tuple(args), cwd))
        if args == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        if args == ["status", "--porcelain"]:
            return ""
        if args == ["remote", "get-url", "origin"]:
            return "https://github.com/acme/typeflux-app"
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        raise AssertionError(args)

    monkeypatch.setattr(provenance_module, "_git", fake_git)

    first = collect_code_provenance(tmp_path)
    second = collect_code_provenance(tmp_path)

    # Value equality, not identity: the cache holds code identity and each return
    # overlays fresh deployment vars onto a copy (#829).
    assert first == second
    assert first.git_sha == "abc123"
    assert len(calls) == 5


def test_collect_code_provenance_env_bypasses_git_and_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provenance_module._clear_code_provenance_cache()
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "sha-from-env")

    def fail_git(args, cwd):
        raise AssertionError("git should not be called when env provenance is present")

    monkeypatch.setattr(provenance_module, "_git", fail_git)

    provenance = collect_code_provenance(tmp_path)

    assert provenance.source == "env"
    assert provenance.git_sha == "sha-from-env"


def test_clear_code_provenance_cache_allows_fresh_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_provenance_env(monkeypatch)
    provenance_module._clear_code_provenance_cache()
    calls = []

    def fake_git(args, cwd):
        calls.append(tuple(args))
        if args == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        if args == ["status", "--porcelain"]:
            return ""
        if args == ["remote", "get-url", "origin"]:
            return None
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main"
        if args == ["rev-parse", "HEAD"]:
            return "abc123"
        raise AssertionError(args)

    monkeypatch.setattr(provenance_module, "_git", fake_git)

    collect_code_provenance(tmp_path)
    provenance_module._clear_code_provenance_cache()
    collect_code_provenance(tmp_path)

    assert len(calls) == 10


def test_reconstruct_execution_manifest_merges_root_and_child_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    workflow = merge_activity_rollup(
        build_workflow_execution_manifest(
            workflow_name="DemoWorkflow",
            workflow_id="workflow-1",
            task_queue="demo",
            activities=["classify", "route"],
        ).to_dict(),
        _activity_execution_manifest("classify"),
    )
    activity = _activity_execution_manifest("classify")
    trace = {
        "metadata": {"typeflux": {"execution_manifest": workflow}},
        "observations": [
            {"metadata": {"typeflux": {"activity_execution_manifest": activity}}},
        ],
    }

    reconstructed = reconstruct_execution_manifest(trace)

    assert reconstructed.workflow == workflow
    assert reconstructed.activities == (workflow["activities"][0],)
    assert reconstructed.missing_activities == ("route",)
    assert reconstructed.warnings == ("missing activity execution manifest(s): route",)


def test_reconstruct_execution_manifest_falls_back_to_child_activity_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    workflow = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=["classify"],
    ).to_dict()
    activity = _activity_execution_manifest("classify")
    trace = {
        "metadata": {"typeflux": {"execution_manifest": workflow}},
        "observations": [
            {"metadata": {"typeflux": {"activity_execution_manifest": activity}}},
        ],
    }

    reconstructed = reconstruct_execution_manifest(trace)

    assert reconstructed.activities == (activity,)
    assert reconstructed.missing_activities == ()
    assert reconstructed.warnings == ()


def test_reconstruct_execution_manifest_prefers_root_rollup_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    classify = _activity_execution_manifest("classify")
    route = _activity_execution_manifest("route")
    workflow = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[
            compact_activity_manifest(classify),
            compact_activity_manifest(route),
        ],
    ).to_dict()
    trace = {
        "metadata": {"typeflux": {"execution_manifest": workflow}},
        "observations": [
            {"metadata": {"typeflux": {"execution_manifest": workflow}}},
            {"metadata": {"typeflux": {"activity_execution_manifest": classify}}},
            {"metadata": {"typeflux": {"activity_execution_manifest": route}}},
        ],
    }

    reconstructed = reconstruct_execution_manifest(trace)

    assert [activity["activity_name"] for activity in reconstructed.activities] == [
        "classify",
        "route",
    ]
    assert reconstructed.activities == tuple(workflow["activities"])
    assert reconstructed.missing_activities == ()
    assert reconstructed.warnings == ()


def test_reconstruct_execution_manifest_warns_when_workflow_manifest_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    activity = _activity_execution_manifest("classify")
    trace = {
        "observations": [
            {"metadata": {"typeflux": {"activity_execution_manifest": activity}}},
        ],
    }

    reconstructed = reconstruct_execution_manifest(trace)

    assert reconstructed.workflow is None
    assert reconstructed.activities == (activity,)
    assert "missing workflow execution manifest" in reconstructed.warnings


def test_reconstruct_execution_manifest_warns_on_activity_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TYPEFLUX_GIT_SHA", "abc123")
    classify = _activity_execution_manifest("classify")
    workflow = build_workflow_execution_manifest(
        workflow_name="DemoWorkflow",
        workflow_id="workflow-1",
        task_queue="demo",
        activities=[compact_activity_manifest(classify)],
    ).to_dict()
    tampered = {**classify, "activity_manifest_hash": "deadbeef"}
    trace = {
        "metadata": {"typeflux": {"execution_manifest": workflow}},
        "observations": [
            {"metadata": {"typeflux": {"activity_execution_manifest": tampered}}},
        ],
    }

    reconstructed = reconstruct_execution_manifest(trace)

    assert "activity manifest hash mismatch(es): classify" in reconstructed.warnings


def _activity_execution_manifest(name: str) -> dict:
    activity = AIActivity(
        name=name,
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef(f"demo/{name}"),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef(f"demo/{name}"),
        messages=(ChatMessage(role="user", content="Hello {{ text }}"),),
        resolved_version="1",
    )
    return build_activity_execution_manifest(
        activity=activity,
        activity_manifest=build_activity_manifest(activity, resolved),
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Hello world"),),
        validation_attempt=0,
    ).to_dict()


def _clear_provenance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TYPEFLUX_GIT_SHA",
        "TYPEFLUX_GIT_REF",
        "TYPEFLUX_REPO_URL",
        "TYPEFLUX_DEPLOYMENT_ID",
        "TYPEFLUX_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env, capture_output=True, text=True)
