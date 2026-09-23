from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, get_type_hints

from typeflux.core.contracts import YamlWorkflowActivity
from typeflux.core.subjects import subject_trace_tags
from typeflux.manifests import WorkflowExecutionManifest, build_workflow_execution_manifest
from typeflux.metadata import (
    CoreWorkflowContributor,
    ExtraTypefluxContributor,
    MetadataContributor,
    WorkflowMetadataContext,
    workflow_contribution,
)
from typeflux.observability.backend import TraceWriter
from typeflux.observability.langfuse import LangfuseTraceWriter


async def execute_workflow(
    *,
    client: Any,
    workflow: Any,
    input_value: Any,
    id: str,
    task_queue: str,
    result_type: type | None = None,
    langfuse_client: Any | None = None,
    trace_writer: TraceWriter | None = None,
    workflow_name: str | None = None,
    tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
    subject_ids: Sequence[str] | None = None,
    activities: Sequence[YamlWorkflowActivity] = (),
    activity_rollup: Sequence[str | dict[str, Any]] | None = None,
    extra_typeflux: dict[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
    include_execution_manifest: bool = True,
    **execute_kwargs: Any,
) -> Any:
    resolved_workflow_name = workflow_name or _workflow_name(workflow)
    resolved_subject_ids = tuple(subject_ids or ())
    resolved_trace_writer = trace_writer
    if resolved_trace_writer is None and langfuse_client is not None:
        resolved_trace_writer = LangfuseTraceWriter(client=langfuse_client)
    workflow_manifest = (
        _build_workflow_manifest(
            workflow_name=resolved_workflow_name,
            workflow_id=id,
            task_queue=task_queue,
            activities=activities,
            activity_rollup=activity_rollup,
            extra_typeflux=extra_typeflux,
            metadata_contributors=metadata_contributors,
        )
        if include_execution_manifest
        else None
    )
    workflow_metadata = workflow_invocation_metadata(
        workflow_name=resolved_workflow_name,
        workflow_id=id,
        task_queue=task_queue,
        activities=activities,
        activity_rollup=activity_rollup,
        metadata=metadata,
        tags=tags,
        extra_typeflux=extra_typeflux,
        metadata_contributors=metadata_contributors,
        include_execution_manifest=include_execution_manifest,
        execution_manifest=workflow_manifest,
    )
    workflow_tags = _with_subject_tags(
        workflow_search_tags(
            workflow_name=resolved_workflow_name,
            activities=activities,
            activity_rollup=activity_rollup,
            user_tags=tags,
            metadata=workflow_metadata,
            metadata_contributors=metadata_contributors,
        ),
        resolved_subject_ids,
    )
    workflow_metadata["tags"] = workflow_tags
    if resolved_trace_writer is None or not getattr(resolved_trace_writer, "enabled", False):
        return await _execute_temporal_workflow(
            client=client,
            workflow=workflow,
            input_value=input_value,
            id=id,
            task_queue=task_queue,
            result_type=result_type,
            **execute_kwargs,
        )

    try:
        with resolved_trace_writer.observe_workflow_invocation(
            workflow_name=resolved_workflow_name,
            input_value=input_value,
            metadata=workflow_metadata,
            tags=workflow_tags,
            subject_ids=resolved_subject_ids,
        ) as observation:
            result, run_id = await _start_temporal_workflow(
                client=client,
                workflow=workflow,
                input_value=input_value,
                id=id,
                task_queue=task_queue,
                result_type=result_type,
                **execute_kwargs,
            )
            if run_id is not None:
                updated_workflow_manifest = (
                    workflow_manifest.with_temporal_run_id(run_id)
                    if workflow_manifest is not None
                    else None
                )
                workflow_metadata = workflow_invocation_metadata(
                    workflow_name=resolved_workflow_name,
                    workflow_id=id,
                    temporal_run_id=run_id,
                    task_queue=task_queue,
                    activities=activities,
                    activity_rollup=activity_rollup,
                    metadata=metadata,
                    tags=tags,
                    extra_typeflux=extra_typeflux,
                    metadata_contributors=metadata_contributors,
                    include_execution_manifest=include_execution_manifest,
                    execution_manifest=updated_workflow_manifest,
                )
                workflow_tags = _with_subject_tags(
                    workflow_search_tags(
                        workflow_name=resolved_workflow_name,
                        activities=activities,
                        activity_rollup=activity_rollup,
                        user_tags=tags,
                        metadata=workflow_metadata,
                        metadata_contributors=metadata_contributors,
                    ),
                    resolved_subject_ids,
                )
                workflow_metadata["tags"] = workflow_tags
                observation.update_metadata(workflow_metadata)
            observation.update_output(result)
            return result
    finally:
        resolved_trace_writer.flush()


def workflow_invocation_metadata(
    *,
    workflow_name: str,
    workflow_id: str,
    task_queue: str,
    temporal_run_id: str | None = None,
    activities: Sequence[YamlWorkflowActivity] = (),
    activity_rollup: Sequence[str | dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: Sequence[str] | None = None,
    extra_typeflux: dict[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
    include_execution_manifest: bool = True,
    execution_manifest: WorkflowExecutionManifest | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(metadata or {})
    resolved_contributors = _metadata_contributors(metadata_contributors, extra_typeflux)
    if include_execution_manifest:
        workflow_manifest = execution_manifest or _build_workflow_manifest(
            workflow_name=workflow_name,
            workflow_id=workflow_id,
            temporal_run_id=temporal_run_id,
            task_queue=task_queue,
            activities=activities,
            activity_rollup=activity_rollup,
            extra_typeflux=extra_typeflux,
            metadata_contributors=metadata_contributors,
        )
    else:
        workflow_manifest = None
    context = WorkflowMetadataContext(
        workflow_name=workflow_name,
        workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        task_queue=task_queue,
        activities=activities,
        activity_rollup=activity_rollup,
        user_metadata=metadata,
        user_tags=tags or (),
        include_execution_manifest=include_execution_manifest,
        execution_manifest=workflow_manifest,
    )
    contribution = workflow_contribution(resolved_contributors, context)
    payload = _merge_workflow_metadata(payload, contribution.workflow_metadata)
    if tags:
        payload["tags"] = sorted(set(tags))
    return payload


def workflow_search_tags(
    *,
    workflow_name: str,
    activities: Sequence[YamlWorkflowActivity] = (),
    activity_rollup: Sequence[str | dict[str, Any]] | None = None,
    user_tags: Sequence[str] | None = None,
    metadata: dict[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
) -> list[str]:
    typeflux = metadata.get("typeflux") if isinstance(metadata, dict) else None
    execution_manifest = typeflux.get("execution_manifest") if isinstance(typeflux, dict) else None
    workflow_id = _string_mapping_value(execution_manifest, "workflow_id") or ""
    temporal_run_id = _string_mapping_value(execution_manifest, "temporal_run_id")
    task_queue = _string_mapping_value(execution_manifest, "task_queue") or ""
    context = WorkflowMetadataContext(
        workflow_name=workflow_name,
        workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        task_queue=task_queue,
        activities=activities,
        activity_rollup=activity_rollup,
        user_metadata=metadata,
        user_tags=user_tags or (),
        execution_manifest=execution_manifest,
    )
    contribution = workflow_contribution(
        _metadata_contributors(metadata_contributors, None),
        context,
    )
    tags = set(contribution.search_tags)
    tags.update(user_tags or ())
    return sorted(tags)


def _build_workflow_manifest(
    *,
    workflow_name: str,
    workflow_id: str,
    task_queue: str,
    temporal_run_id: str | None = None,
    activities: Sequence[YamlWorkflowActivity] = (),
    activity_rollup: Sequence[str | dict[str, Any]] | None = None,
    extra_typeflux: dict[str, Any] | None = None,
    metadata_contributors: Sequence[MetadataContributor] = (),
) -> WorkflowExecutionManifest:
    context = WorkflowMetadataContext(
        workflow_name=workflow_name,
        workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        task_queue=task_queue,
        activities=activities,
        activity_rollup=activity_rollup,
    )
    contribution = workflow_contribution(
        _metadata_contributors(metadata_contributors, extra_typeflux),
        context,
    )
    manifest_payload = contribution.workflow_manifest
    rollup_or_names: Sequence[str | dict[str, Any]] = (
        activity_rollup
        if activity_rollup is not None
        else [activity.name for activity in activities]
    )
    return build_workflow_execution_manifest(
        workflow_name=workflow_name,
        workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        task_queue=task_queue,
        activities=rollup_or_names,
        map_steps=manifest_payload.get("map_steps", ()),
        contributions=manifest_payload.get("contributions"),
        yaml_project=manifest_payload.get("yaml_project"),
        yaml_name=manifest_payload.get("yaml_name"),
    )


async def _start_temporal_workflow(
    *,
    client: Any,
    workflow: Any,
    input_value: Any,
    id: str,
    task_queue: str,
    result_type: type | None,
    **execute_kwargs: Any,
) -> tuple[Any, str | None]:
    start_workflow = getattr(client, "start_workflow", None)
    if start_workflow is None:
        return (
            await _execute_temporal_workflow(
                client=client,
                workflow=workflow,
                input_value=input_value,
                id=id,
                task_queue=task_queue,
                result_type=result_type,
                **execute_kwargs,
            ),
            None,
        )

    kwargs = _workflow_kwargs(
        workflow=workflow,
        id=id,
        task_queue=task_queue,
        result_type=result_type,
        execute_kwargs=execute_kwargs,
    )
    handle = await start_workflow(workflow, input_value, **kwargs)
    result = await handle.result()
    run_id = (
        getattr(handle, "result_run_id", None)
        or getattr(handle, "run_id", None)
        or getattr(handle, "first_execution_run_id", None)
    )
    return result, run_id


async def _execute_temporal_workflow(
    *,
    client: Any,
    workflow: Any,
    input_value: Any,
    id: str,
    task_queue: str,
    result_type: type | None,
    **execute_kwargs: Any,
) -> Any:
    kwargs = _workflow_kwargs(
        workflow=workflow,
        id=id,
        task_queue=task_queue,
        result_type=result_type,
        execute_kwargs=execute_kwargs,
    )
    return await client.execute_workflow(workflow, input_value, **kwargs)


def _workflow_kwargs(
    *,
    workflow: Any,
    id: str,
    task_queue: str,
    result_type: type | None,
    execute_kwargs: dict[str, Any],
) -> dict[str, Any]:
    kwargs = dict(execute_kwargs)
    kwargs["id"] = id
    kwargs["task_queue"] = task_queue
    resolved_result_type = result_type or _workflow_return_type(workflow)
    if resolved_result_type is not None:
        kwargs["result_type"] = resolved_result_type
    return kwargs


def _workflow_name(workflow: Any) -> str:
    if isinstance(workflow, str):
        return workflow
    qualname = getattr(workflow, "__qualname__", None)
    if qualname:
        return qualname.removesuffix(".run")
    name = getattr(workflow, "__name__", None)
    if name:
        return name
    return str(workflow)


def _workflow_return_type(workflow: Any) -> type | None:
    try:
        return_type = get_type_hints(workflow).get("return")
    except Exception:
        return_type = getattr(workflow, "__annotations__", {}).get("return")
    return return_type if isinstance(return_type, type) else None


def _metadata_contributors(
    contributors: Sequence[MetadataContributor],
    extra_typeflux: dict[str, Any] | None,
) -> tuple[MetadataContributor, ...]:
    resolved: list[MetadataContributor] = [CoreWorkflowContributor()]
    resolved.extend(contributors)
    if extra_typeflux:
        resolved.append(ExtraTypefluxContributor(extra_typeflux))
    return tuple(resolved)


def _with_subject_tags(tags: list[str], subject_ids: Sequence[str]) -> list[str]:
    """Fold the portable ``typeflux.subject:{id}`` tags into the trace tags (#715).

    The native Langfuse ``userId`` (primary subject) and these tags always agree;
    the tags carry the FULL subject list so tag-based ``trace.list`` still finds a
    multi-subject execution. Sorted-set merge keeps the tag list stable/dedup'd."""

    if not subject_ids:
        return tags
    return sorted(set(tags) | set(subject_trace_tags(subject_ids)))


def _string_mapping_value(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    item = value.get(key)
    return item if isinstance(item, str) else None


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = dict(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = value
            continue
        existing = merged[key]
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
            continue
        if existing == value:
            continue
        raise ValueError(f"conflicting metadata value for {key!r}")
    return merged


def _merge_workflow_metadata(
    payload: dict[str, Any],
    contributor_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    typeflux = contributor_metadata.get("typeflux")
    if not isinstance(typeflux, Mapping):
        return _deep_merge(payload, dict(contributor_metadata))

    merged = _deep_merge(
        payload,
        {key: value for key, value in contributor_metadata.items() if key != "typeflux"},
    )
    existing_typeflux = merged.get("typeflux")
    if existing_typeflux is None:
        existing_typeflux = {}
    if not isinstance(existing_typeflux, dict):
        raise ValueError("conflicting metadata value for 'typeflux'")

    merged_typeflux = dict(existing_typeflux)
    for key, value in typeflux.items():
        merged_typeflux[key] = value
    merged["typeflux"] = merged_typeflux
    return merged


__all__ = ["execute_workflow", "workflow_invocation_metadata", "workflow_search_tags"]
