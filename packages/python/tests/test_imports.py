from __future__ import annotations

from typing import get_type_hints


def test_decomposed_manifest_and_observability_imports() -> None:
    from typeflux import (
        KubernetesRuntimePlacementView as TopLevelKubernetesRuntimePlacementView,
    )
    from typeflux import PolicyContributor as TopLevelPolicyContributor
    from typeflux import PolicyView as TopLevelPolicyView
    from typeflux import RuntimePlacementContributor as TopLevelRuntimePlacementContributor
    from typeflux import RuntimePlacementView as TopLevelRuntimePlacementView
    from typeflux.manifests import (
        ActivityRollupEntry,
    )
    from typeflux.manifests import (
        WorkflowExecutionManifest as PackageWorkflowExecutionManifest,
    )
    from typeflux.manifests import schema_hash as package_schema_hash
    from typeflux.manifests.activity import ActivityExecutionManifest
    from typeflux.manifests.hashing import schema_hash
    from typeflux.manifests.provenance import CodeProvenance
    from typeflux.manifests.reconstruction import ReconstructedExecutionManifest
    from typeflux.manifests.workflow import (
        ActivityRollupEntry as WorkflowActivityRollupEntry,
    )
    from typeflux.manifests.workflow import WorkflowExecutionManifest
    from typeflux.metadata import PolicyContributor, RuntimePlacementContributor
    from typeflux.observability import (
        KubernetesRuntimePlacementView as PackageKubernetesRuntimePlacementView,
    )
    from typeflux.observability import (
        RuntimePlacementView as PackageRuntimePlacementView,
    )
    from typeflux.observability import TraceSearchQuery as PackageTraceSearchQuery
    from typeflux.observability.backend import (
        InMemoryTraceStore,
        NoOpWorkflowObservation,
        ObservabilityBackend,
    )
    from typeflux.observability.diff import TraceDiff
    from typeflux.observability.dto import (
        NoOpWorkflowObservation as DtoNoOpWorkflowObservation,
    )
    from typeflux.observability.inspect import (
        KubernetesRuntimePlacementView,
        PolicyView,
        RuntimePlacementView,
        TraceSearchQuery,
    )

    assert schema_hash is package_schema_hash
    assert WorkflowExecutionManifest is PackageWorkflowExecutionManifest
    assert TraceSearchQuery is PackageTraceSearchQuery
    assert ActivityRollupEntry == WorkflowActivityRollupEntry
    assert ActivityExecutionManifest is not None
    assert CodeProvenance is not None
    assert PolicyContributor is not None
    assert PolicyView is not None
    assert RuntimePlacementContributor is not None
    assert KubernetesRuntimePlacementView is not None
    assert RuntimePlacementView is not None
    assert TopLevelPolicyContributor is PolicyContributor
    assert TopLevelPolicyView is PolicyView
    assert TopLevelRuntimePlacementContributor is RuntimePlacementContributor
    assert TopLevelKubernetesRuntimePlacementView is KubernetesRuntimePlacementView
    assert TopLevelRuntimePlacementView is RuntimePlacementView
    assert PackageKubernetesRuntimePlacementView is KubernetesRuntimePlacementView
    assert PackageRuntimePlacementView is RuntimePlacementView
    assert ReconstructedExecutionManifest is not None
    assert InMemoryTraceStore is not None
    assert NoOpWorkflowObservation is DtoNoOpWorkflowObservation
    assert ObservabilityBackend is not None
    assert TraceDiff is not None


def test_observability_function_type_hints_are_runtime_resolvable() -> None:
    from typeflux.observability.diff import diff_traces
    from typeflux.observability.inspect import (
        export_execution_manifest,
        inspect_trace,
        list_traces,
        search_traces,
    )

    for function in (
        inspect_trace,
        list_traces,
        search_traces,
        export_execution_manifest,
        diff_traces,
    ):
        assert get_type_hints(function)


def test_project_policy_imports_are_public() -> None:
    from typeflux import RuntimePolicyGuard as TopLevelRuntimePolicyGuard
    from typeflux import TypefluxProjectPolicySpec as TopLevelPolicySpec
    from typeflux import (
        build_project_policy_runtime_guard as top_level_build_policy_guard,
    )
    from typeflux.project import RuntimePolicyGuard as ProjectRuntimePolicyGuard
    from typeflux.project import (
        TypefluxProjectPolicySpec as ProjectPolicySpec,
    )
    from typeflux.project import (
        build_project_policy_runtime_guard as package_build_policy_guard,
    )
    from typeflux.project import compose_project_policies as package_compose
    from typeflux.project import policy_content_hash as package_policy_hash
    from typeflux.project import validate_project_policy as package_validate_policy
    from typeflux.project.policy import (
        TypefluxProjectPolicySpec,
        compose_project_policies,
        policy_content_hash,
    )
    from typeflux.project.policy_enforcement import (
        RuntimePolicyGuard,
        build_project_policy_runtime_guard,
        validate_project_policy,
    )

    assert TypefluxProjectPolicySpec is ProjectPolicySpec
    assert TopLevelPolicySpec is TypefluxProjectPolicySpec
    assert TopLevelRuntimePolicyGuard is RuntimePolicyGuard
    assert ProjectRuntimePolicyGuard is RuntimePolicyGuard
    assert package_compose is compose_project_policies
    assert package_policy_hash is policy_content_hash
    assert package_validate_policy is validate_project_policy
    assert package_build_policy_guard is build_project_policy_runtime_guard
    assert top_level_build_policy_guard is build_project_policy_runtime_guard
