"""Engine boundaries and static local capability checks, without runtime imports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal, NoReturn

from typeflux.core.errors import TypefluxError
from typeflux.yaml.spec import (
    TypefluxYamlSpec,
    WorkflowAnyStepSpec,
    WorkflowMapStepSpec,
    WorkflowParallelStepSpec,
    WorkflowSubworkflowStepSpec,
)


class RuntimeCapabilityError(TypefluxError, ValueError):
    """A configuration requires capabilities unavailable at an engine boundary."""

    def __init__(self, workflow: str, path: str, feature: str, guidance: str) -> None:
        self.workflow = workflow
        self.path = path
        self.feature = feature
        super().__init__(f"workflow {workflow!r}: {feature} at {path}. {guidance}")


def runtime_engine(spec: TypefluxYamlSpec) -> Literal["local", "temporal"]:
    return spec.runtime.engine or "temporal"


def require_temporal_engine(spec: TypefluxYamlSpec) -> str:
    """Guard an existing Temporal-only surface and return its required queue."""
    if runtime_engine(spec) != "temporal":
        raise RuntimeCapabilityError(
            spec.workflow.name,
            "runtime.engine",
            "local engine is not supported by this Temporal entry point",
            "Use runtime.engine: temporal and configure task_queue to use this entry point. "
            "Local execution is not available through Temporal workers or control-plane APIs.",
        )
    if spec.task_queue is None:
        raise RuntimeCapabilityError(
            spec.workflow.name,
            "task_queue",
            "Temporal task queue is missing",
            "Configure task_queue for runtime.engine: temporal.",
        )
    return spec.task_queue


def validate_local_capabilities(
    spec: TypefluxYamlSpec,
    *,
    resolve_workflow: Callable[[str], TypefluxYamlSpec | None] | None = None,
    workflow_id: str | None = None,
) -> tuple[str, ...]:
    """Inspect the entire reachable YAML closure without importing user activities.

    Returns visited IDs in first-reference order (the root uses its YAML name
    when workflow_id is omitted). This is a capability
    check, not a plan, policy-compliance attestation, or executable runtime. The
    resolver must return specs under the same project/environment and must raise
    its own errors for declared but invalid workflows. Trusted resolver code is
    not sandboxed. Activity descriptors and policies still require assembly checks.
    """
    visited: set[str] = set()
    labels: list[str] = []
    active: set[str] = set()
    active_specs: set[int] = set()

    def reject(current: TypefluxYamlSpec, path: str, feature: str) -> NoReturn:
        guidance = (
            "Fix the project workflow reference; local and Temporal graphs both require valid same-project references."
            if "reference" in feature or "resolver" in feature
            else "Use runtime.engine: temporal for this capability; local execution does not provide it."
        )
        raise RuntimeCapabilityError(current.workflow.name, path, feature, guidance)

    def visit(current: TypefluxYamlSpec, key: str | None) -> None:
        if runtime_engine(current) != "local":
            raise RuntimeCapabilityError(
                current.workflow.name,
                "runtime.engine",
                "local graph requires local engine on every workflow",
                "Use runtime.engine: local on every reachable workflow and remove Temporal-only settings.",
            )
        if current.task_queue is not None:
            reject(current, "task_queue", "Temporal task queue")
        # Pydantic defaults have values, but only declared fields configure behavior.
        for name in sorted(current.runtime.temporal.model_fields_set):
            reject(current, f"runtime.temporal.{name}", "Temporal connection configuration")
        if current.runtime.activity_retry is not None:
            reject(current, "runtime.activity_retry", "Temporal activity retry policy")
        if current.workflow.lifecycle is not None:
            reject(current, "workflow.lifecycle", "workflow lifecycle/review control")
        if current.workflow.risk_tier not in (None, "safe"):
            reject(
                current,
                "workflow.risk_tier",
                "risk-tier enforcement requires a policy-aware runtime",
            )
        for index, definition in enumerate(current.activities.definitions):
            for name in ("retry", "start_to_close_timeout_seconds", "heartbeat_timeout_seconds"):
                if getattr(definition, name) is not None:
                    reject(
                        current,
                        f"activities.definitions[{index}].{name}",
                        "Temporal activity policy",
                    )

        labels.append(key if key is not None else current.name)
        if key is not None:
            visited.add(key)
            active.add(key)
        active_specs.add(id(current))

        def walk(steps: Sequence[WorkflowAnyStepSpec], prefix: str) -> None:
            for index, step in enumerate(steps):
                path = f"{prefix}[{index}]"
                if getattr(step, "compensate", None) is not None:
                    reject(current, f"{path}.compensate", "compensation")
                reference: str | None = None
                reference_path = f"{path}.workflow"
                if isinstance(step, WorkflowSubworkflowStepSpec):
                    reference = step.workflow
                elif isinstance(step, WorkflowMapStepSpec):
                    reference = step.map.workflow
                    reference_path = f"{path}.map.workflow"
                elif isinstance(step, WorkflowParallelStepSpec):
                    for branch_index, branch in enumerate(step.parallel.branches):
                        walk(branch.steps, f"{path}.parallel.branches[{branch_index}].steps")
                if reference is None:
                    continue
                if reference in active:
                    reject(current, reference_path, "cyclic workflow reference")
                if reference in visited:
                    continue
                if resolve_workflow is None:
                    reject(
                        current, reference_path, "workflow reference requires a project resolver"
                    )
                child = resolve_workflow(reference)
                if child is None:
                    reject(
                        current, reference_path, "workflow reference is not declared in the project"
                    )
                if child.project != spec.project:
                    reject(current, reference_path, "cross-project workflow reference")
                if id(child) in active_specs:
                    reject(current, reference_path, "cyclic workflow reference")
                visit(child, reference)

        walk(current.workflow.steps, "workflow.steps")
        if key is not None:
            active.remove(key)
        active_specs.remove(id(current))

    visit(spec, workflow_id)
    return tuple(labels)


__all__ = [
    "RuntimeCapabilityError",
    "require_temporal_engine",
    "runtime_engine",
    "validate_local_capabilities",
]
