from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

try:  # pragma: no cover - exercised when temporalio is installed.
    from temporalio.api.enums.v1 import EventType
    from temporalio.converter import DataConverter
except ModuleNotFoundError:  # pragma: no cover - dependency guard for minimal imports.
    EventType = Any  # type: ignore[misc, assignment]
    DataConverter = Any  # type: ignore[misc, assignment]

from typeflux.core.contracts import ReviewCommand


class WorkflowLifecycleAuditEvent(BaseModel):
    event_id: int
    event_type: str
    lifecycle_event: str
    timestamp: str | None = None
    workflow_id: str | None = None
    run_id: str | None = None
    activity_id: str | None = None
    activity_type: str | None = None
    step_id: str | None = None
    signal_name: str | None = None
    review_user_decision: str | None = None
    review_route_target: str | None = None
    reviewer: str | None = None
    cancellation_reason: str | None = None
    scheduled_event_id: int | None = None
    started_event_id: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class WorkflowLifecycleAuditExport(BaseModel):
    workflow_id: str | None = None
    run_id: str | None = None
    event_count: int = 0
    events: tuple[WorkflowLifecycleAuditEvent, ...] = ()


async def export_workflow_lifecycle_audit(
    handle: Any,
    *,
    page_size: int | None = None,
    data_converter: DataConverter | None = None,
    redactor: Any | None = None,
) -> WorkflowLifecycleAuditExport:
    """Export lifecycle-relevant audit events from durable Temporal history.

    Reviewer identity, cancellation reasons, and failure messages are
    preserved verbatim by default so the export stays usable as a compliance
    audit record. Pass a ``redactor`` (any object with a ``redact(value)``
    method, such as ``RegexPIIRedactor``) to redact those sensitive fields
    before the export leaves the trust boundary.
    """

    scheduled_activities: dict[int, dict[str, str | None]] = {}
    events: list[WorkflowLifecycleAuditEvent] = []
    workflow_id = _workflow_id(handle)
    run_id = _run_id(handle)
    if workflow_id is None or run_id is None:
        workflow_id, run_id = await _describe_workflow_identity(
            handle,
            workflow_id=workflow_id,
            run_id=run_id,
        )

    async for history_event in handle.fetch_history_events(page_size=page_size):
        audit_event = workflow_lifecycle_audit_event_from_history_event(
            history_event,
            workflow_id=workflow_id,
            run_id=run_id,
            scheduled_activities=scheduled_activities,
            data_converter=data_converter,
        )
        if audit_event is not None:
            if redactor is not None:
                audit_event = _redact_audit_event(audit_event, redactor)
            events.append(audit_event)

    return WorkflowLifecycleAuditExport(
        workflow_id=workflow_id,
        run_id=run_id,
        event_count=len(events),
        events=tuple(events),
    )


def _redact_audit_event(
    event: WorkflowLifecycleAuditEvent, redactor: Any
) -> WorkflowLifecycleAuditEvent:
    updates: dict[str, Any] = {}
    if event.reviewer is not None:
        updates["reviewer"] = redactor.redact(event.reviewer)
    if event.cancellation_reason is not None:
        updates["cancellation_reason"] = redactor.redact(event.cancellation_reason)
    if event.details:
        updates["details"] = redactor.redact(dict(event.details))
    if not updates:
        return event
    return event.model_copy(update=updates)


async def _describe_workflow_identity(
    handle: Any,
    *,
    workflow_id: str | None,
    run_id: str | None,
) -> tuple[str | None, str | None]:
    describe = getattr(handle, "describe", None)
    if not callable(describe):
        return workflow_id, run_id
    try:
        description = await describe()
    except Exception:
        return workflow_id, run_id
    return (
        workflow_id or _string_attr(description, "id"),
        run_id or _string_attr(description, "run_id"),
    )


def workflow_lifecycle_audit_event_from_history_event(
    history_event: Any,
    *,
    workflow_id: str | None = None,
    run_id: str | None = None,
    scheduled_activities: dict[int, dict[str, str | None]] | None = None,
    data_converter: DataConverter | None = None,
) -> WorkflowLifecycleAuditEvent | None:
    event_type = _event_type_name(history_event)
    event_id = int(getattr(history_event, "event_id", 0) or 0)
    timestamp = _event_timestamp(history_event)
    scheduled = scheduled_activities if scheduled_activities is not None else {}

    if event_type == "WORKFLOW_EXECUTION_STARTED":
        return _audit_event(
            history_event,
            event_type=event_type,
            lifecycle_event="workflow_started",
            workflow_id=workflow_id,
            run_id=run_id,
            timestamp=timestamp,
        )
    if event_type == "WORKFLOW_EXECUTION_COMPLETED":
        return _audit_event(
            history_event,
            event_type=event_type,
            lifecycle_event="workflow_completed",
            workflow_id=workflow_id,
            run_id=run_id,
            timestamp=timestamp,
        )
    if event_type == "WORKFLOW_EXECUTION_FAILED":
        return _audit_event(
            history_event,
            event_type=event_type,
            lifecycle_event="workflow_failed",
            workflow_id=workflow_id,
            run_id=run_id,
            timestamp=timestamp,
            details=_failure_details(_event_attributes(history_event, event_type)),
        )
    if event_type in {"WORKFLOW_EXECUTION_CANCELED", "WORKFLOW_EXECUTION_CANCELLED"}:
        return _audit_event(
            history_event,
            event_type=event_type,
            lifecycle_event="workflow_cancelled",
            workflow_id=workflow_id,
            run_id=run_id,
            timestamp=timestamp,
        )
    if event_type == "ACTIVITY_TASK_SCHEDULED":
        attrs = _event_attributes(history_event, event_type)
        activity_id = _string_attr(attrs, "activity_id")
        activity_type = _activity_type_name(attrs)
        scheduled[event_id] = {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "step_id": _step_id_from_activity_id(activity_id),
        }
        return _audit_event(
            history_event,
            event_type=event_type,
            lifecycle_event="activity_scheduled",
            workflow_id=workflow_id,
            run_id=run_id,
            timestamp=timestamp,
            activity_id=activity_id,
            activity_type=activity_type,
            step_id=_step_id_from_activity_id(activity_id),
        )
    if event_type in _ACTIVITY_EVENTS:
        attrs = _event_attributes(history_event, event_type)
        activity = scheduled.get(int(getattr(attrs, "scheduled_event_id", 0) or 0), {})
        return _audit_event(
            history_event,
            event_type=event_type,
            lifecycle_event=_ACTIVITY_EVENTS[event_type],
            workflow_id=workflow_id,
            run_id=run_id,
            timestamp=timestamp,
            activity_id=_string_mapping_value(activity, "activity_id"),
            activity_type=_string_mapping_value(activity, "activity_type"),
            step_id=_string_mapping_value(activity, "step_id"),
            scheduled_event_id=_int_attr(attrs, "scheduled_event_id"),
            started_event_id=_int_attr(attrs, "started_event_id"),
            details=_failure_details(attrs) if event_type.endswith(("FAILED", "TIMED_OUT")) else {},
        )
    if event_type == "WORKFLOW_EXECUTION_SIGNALED":
        attrs = _event_attributes(history_event, event_type)
        signal_name = _string_attr(attrs, "signal_name")
        if signal_name == "typeflux_request_cancel":
            reason = _decode_first_payload(attrs, data_converter=data_converter)
            return _audit_event(
                history_event,
                event_type=event_type,
                lifecycle_event="cancellation_requested",
                workflow_id=workflow_id,
                run_id=run_id,
                timestamp=timestamp,
                signal_name=signal_name,
                cancellation_reason=reason if isinstance(reason, str) else None,
            )
        if signal_name == "typeflux_submit_review":
            command = _decode_review_command(attrs, data_converter=data_converter)
            return _audit_event(
                history_event,
                event_type=event_type,
                lifecycle_event="review_submitted",
                workflow_id=workflow_id,
                run_id=run_id,
                timestamp=timestamp,
                signal_name=signal_name,
                review_user_decision=command.user_decision if command is not None else None,
                reviewer=command.reviewer if command is not None else None,
            )
    return None


_ACTIVITY_EVENTS = {
    "ACTIVITY_TASK_STARTED": "activity_started",
    "ACTIVITY_TASK_COMPLETED": "activity_completed",
    "ACTIVITY_TASK_FAILED": "activity_failed",
    "ACTIVITY_TASK_CANCELED": "activity_cancelled",
    "ACTIVITY_TASK_CANCELLED": "activity_cancelled",
    "ACTIVITY_TASK_TIMED_OUT": "activity_timed_out",
}


def _audit_event(
    history_event: Any,
    *,
    event_type: str,
    lifecycle_event: str,
    workflow_id: str | None,
    run_id: str | None,
    timestamp: str | None,
    activity_id: str | None = None,
    activity_type: str | None = None,
    step_id: str | None = None,
    signal_name: str | None = None,
    review_user_decision: str | None = None,
    review_route_target: str | None = None,
    reviewer: str | None = None,
    cancellation_reason: str | None = None,
    scheduled_event_id: int | None = None,
    started_event_id: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> WorkflowLifecycleAuditEvent:
    return WorkflowLifecycleAuditEvent(
        event_id=int(getattr(history_event, "event_id", 0) or 0),
        event_type=event_type,
        lifecycle_event=lifecycle_event,
        timestamp=timestamp,
        workflow_id=workflow_id,
        run_id=run_id,
        activity_id=activity_id,
        activity_type=activity_type,
        step_id=step_id,
        signal_name=signal_name,
        review_user_decision=review_user_decision,
        review_route_target=review_route_target,
        reviewer=reviewer,
        cancellation_reason=cancellation_reason,
        scheduled_event_id=scheduled_event_id,
        started_event_id=started_event_id,
        details=dict(details or {}),
    )


def _workflow_id(handle: Any) -> str | None:
    for attr in ("id", "workflow_id"):
        value = getattr(handle, attr, None)
        if isinstance(value, str):
            return value
    return None


def _run_id(handle: Any) -> str | None:
    value = getattr(handle, "run_id", None)
    return value if isinstance(value, str) else None


def _event_type_name(history_event: Any) -> str:
    event_type = getattr(history_event, "event_type", None)
    name = getattr(event_type, "name", None)
    if isinstance(name, str):
        return name.removeprefix("EVENT_TYPE_")
    if isinstance(event_type, str):
        return event_type.removeprefix("EVENT_TYPE_")
    if isinstance(event_type, int):
        try:
            return EventType.Name(event_type).removeprefix("EVENT_TYPE_")
        except Exception:
            return str(event_type)
    return str(event_type).removeprefix("EVENT_TYPE_")


def _event_timestamp(history_event: Any) -> str | None:
    value = getattr(history_event, "event_time", None)
    if value is None:
        return None
    if hasattr(value, "ToDatetime"):
        value = value.ToDatetime()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _event_attributes(history_event: Any, event_type: str) -> Any:
    attr_name = event_type.lower() + "_event_attributes"
    attr_name = attr_name.replace("cancelled", "canceled")
    has_field = getattr(history_event, "HasField", None)
    if callable(has_field):
        try:
            if not has_field(attr_name):
                return None
        except Exception:
            return None
    return getattr(history_event, attr_name, None)


def _activity_type_name(attrs: Any) -> str | None:
    activity_type = getattr(attrs, "activity_type", None)
    value = getattr(activity_type, "name", None)
    return value if isinstance(value, str) else None


def _step_id_from_activity_id(activity_id: str | None) -> str | None:
    if activity_id is None:
        return None
    return (
        activity_id.rsplit("-", 1)[0] if activity_id.rsplit("-", 1)[-1].isdigit() else activity_id
    )


def _string_attr(value: Any, attr: str) -> str | None:
    item = getattr(value, attr, None)
    return item if isinstance(item, str) else None


def _int_attr(value: Any, attr: str) -> int | None:
    item = getattr(value, attr, None)
    return int(item) if isinstance(item, int) and item > 0 else None


def _string_mapping_value(value: Mapping[str, str | None], key: str) -> str | None:
    item = value.get(key)
    return item if isinstance(item, str) else None


def _failure_details(attrs: Any) -> dict[str, Any]:
    if attrs is None:
        return {}
    failure = getattr(attrs, "failure", None)
    if failure is None:
        return {}
    message = getattr(failure, "message", None)
    failure_type = getattr(failure, "application_failure_info", None)
    failure_type_value = getattr(failure_type, "type", None)
    return {
        key: value
        for key, value in {
            "failure_message": message if isinstance(message, str) else None,
            "failure_type": failure_type_value if isinstance(failure_type_value, str) else None,
        }.items()
        if value is not None
    }


def _decode_first_payload(attrs: Any, *, data_converter: DataConverter | None) -> Any:
    decoded = _decode_payloads(attrs, data_converter=data_converter)
    return decoded[0] if decoded else None


def _decode_review_command(
    attrs: Any,
    *,
    data_converter: DataConverter | None,
) -> ReviewCommand | None:
    value = _decode_first_payload(attrs, data_converter=data_converter)
    if value is None:
        return None
    if isinstance(value, ReviewCommand):
        return value
    if isinstance(value, Mapping):
        return ReviewCommand.model_validate(value)
    return None


def _decode_payloads(attrs: Any, *, data_converter: DataConverter | None) -> list[Any]:
    if data_converter is None:
        return []
    input_value = getattr(attrs, "input", None)
    payloads = getattr(input_value, "payloads", None)
    if payloads is None:
        return []
    payloads = list(payloads)
    # Codec-aware (#188): this is a SYNCHRONOUS decode, but a codec-carrying converter's
    # history payloads are ciphertext (review/cancel signals ride the codec). The AES-GCM
    # codec exposes a sync `decode_sync`, so decrypt first, then run the payload converter.
    # A codec failure (unknown key / tampered) fails closed — never a plaintext passthrough.
    payload_codec = getattr(data_converter, "payload_codec", None)
    if payload_codec is not None and hasattr(payload_codec, "decode_sync"):
        payloads = list(payload_codec.decode_sync(payloads))
    payload_converter = getattr(data_converter, "payload_converter", None)
    if payload_converter is not None and hasattr(payload_converter, "from_payloads"):
        try:
            return list(payload_converter.from_payloads(payloads))
        except Exception:
            return []
    return []


__all__ = [
    "WorkflowLifecycleAuditEvent",
    "WorkflowLifecycleAuditExport",
    "export_workflow_lifecycle_audit",
    "workflow_lifecycle_audit_event_from_history_event",
]
