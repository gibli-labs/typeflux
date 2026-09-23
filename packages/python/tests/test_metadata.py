from __future__ import annotations

import pytest
from pydantic import BaseModel

from typeflux.core.artifacts import ArtifactInput
from typeflux.core.contracts import (
    AIActivity,
    ChatMessage,
    PromptRef,
    ResolvedPrompt,
    WorkflowLifecycleStatus,
)
from typeflux.execution.starter import (
    workflow_invocation_metadata,
    workflow_search_tags,
)
from typeflux.manifests import (
    AIInvocationContext,
    build_activity_execution_manifest,
    build_activity_manifest,
)
from typeflux.metadata import (
    ActivityContextContributor,
    ActivityMetadataContext,
    LifecycleOperationContributor,
    LifecycleOperationMetadataContext,
    MetadataConflictError,
    MetadataContribution,
    PolicyContributor,
    RuntimePlacementContributor,
    SecretReferenceContributor,
    TemporalConnectionContributor,
    WorkflowMetadataContext,
    YamlLifecycleContributor,
    YamlOverrideContributor,
    YamlWorkflowContributor,
    activity_contribution,
    lifecycle_operation_contribution,
    merge_contributions,
    redaction_exclusions,
    workflow_contribution,
)
from typeflux.observability.backend import InMemoryTraceStore
from typeflux.observability.inspect import (
    ObservationRecord,
    TraceRecord,
    WorkflowExecutionManifestView,
    inspect_trace,
)
from typeflux.observability.redaction import RegexPIIRedactor
from typeflux.observability.semantic import semantic_metadata
from typeflux.project.policy import ComposedProjectPolicy
from typeflux.project.policy_enforcement import RuntimePolicyGuard
from typeflux.yaml.overrides import YamlOverrideProvenance
from typeflux.yaml.secrets import SecretReferenceRecord


class Input(BaseModel):
    text: str


class Output(BaseModel):
    label: str


class _TestContributor:
    def workflow(self, context: WorkflowMetadataContext) -> MetadataContribution:
        del context
        return MetadataContribution(
            workflow_manifest={
                "contributions": {
                    "test": {
                        "case_id": "claim-555-123-4567",
                        "enabled": True,
                    }
                }
            },
            workflow_metadata={
                "typeflux": {
                    "test": {
                        "case_id": "claim-555-123-4567",
                        "enabled": True,
                    }
                }
            },
            search_tags=("typeflux.test:enabled",),
            redaction_exclusions=(
                "typeflux.test.case_id",
                "typeflux.execution_manifest.contributions.test.case_id",
            ),
        )

    def activity(self, context: ActivityMetadataContext) -> MetadataContribution:
        del context
        return MetadataContribution(
            activity_metadata={
                "typeflux": {
                    "test_activity": {
                        "batch_id": "batch-555-123-4567",
                    }
                }
            },
            redaction_exclusions=("typeflux.test_activity.batch_id",),
        )


def test_merge_contributions_deep_merges_and_dedupes() -> None:
    merged = merge_contributions(
        (
            MetadataContribution(
                workflow_metadata={"typeflux": {"yaml": {"name": "demo"}}},
                search_tags=("typeflux", "typeflux.workflow:Demo"),
                redaction_exclusions=("typeflux.yaml.*",),
            ),
            MetadataContribution(
                workflow_metadata={"typeflux": {"yaml": {"project": "project"}}},
                search_tags=("typeflux.workflow:Demo", "typeflux.activity:review"),
                redaction_exclusions=("typeflux.yaml.*", "typeflux.map.*"),
            ),
        )
    )

    assert merged.workflow_metadata == {
        "typeflux": {"yaml": {"name": "demo", "project": "project"}}
    }
    assert merged.search_tags == (
        "typeflux",
        "typeflux.activity:review",
        "typeflux.workflow:Demo",
    )
    assert merged.redaction_exclusions == ("typeflux.map.*", "typeflux.yaml.*")


def test_temporal_connection_contributor_emits_safe_workflow_and_manifest_metadata() -> None:
    contribution = TemporalConnectionContributor(
        address="namespace.tmprl.cloud:7233",
        namespace="namespace",
        region="us-east",
        tls_enabled=True,
        tls_mode="boolean",
        api_key_configured=True,
    ).workflow(
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        )
    )

    expected = {
        "address": "namespace.tmprl.cloud:7233",
        "namespace": "namespace",
        "region": "us-east",
        "tls_enabled": True,
        "tls_mode": "boolean",
        "api_key_configured": True,
    }
    assert contribution.workflow_metadata["typeflux"]["temporal_connection"] == expected
    assert contribution.workflow_manifest["contributions"]["temporal_connection"] == expected
    assert contribution.search_tags == ()


def test_temporal_connection_contributor_redaction_exclusions_do_not_preserve_secrets() -> None:
    redactor = RegexPIIRedactor.default(
        exclude_paths=redaction_exclusions(
            (
                TemporalConnectionContributor(
                    address="namespace.tmprl.cloud:7233",
                    namespace="namespace",
                    api_key_configured=True,
                ),
            )
        )
    )

    redacted = redactor.redact(
        {
            "typeflux": {
                "temporal_connection": {
                    "address": "namespace.tmprl.cloud:7233",
                    "namespace": "namespace",
                    "api_key_configured": True,
                    "api_key": "secret-555-123-4567",
                },
                "execution_manifest": {
                    "contributions": {
                        "temporal_connection": {
                            "address": "namespace.tmprl.cloud:7233",
                            "namespace": "namespace",
                            "api_key_configured": True,
                        }
                    }
                },
            }
        }
    )

    assert redacted["typeflux"]["temporal_connection"]["address"] == ("namespace.tmprl.cloud:7233")
    assert redacted["typeflux"]["temporal_connection"]["namespace"] == "namespace"
    assert redacted["typeflux"]["temporal_connection"]["api_key_configured"] is True
    assert redacted["typeflux"]["temporal_connection"]["api_key"] == ("secret-[REDACTED_PHONE]")


def test_runtime_placement_contributor_emits_safe_trace_metadata_only() -> None:
    contributor = RuntimePlacementContributor(
        platform="kubernetes",
        k8s_namespace="typeflux-smoke",
        k8s_pod_name="typeflux-worker-abc123",
        k8s_pod_uid="pod-uid-123",
        k8s_node_name="minikube",
        k8s_service_account="default",
        k8s_deployment_name="typeflux-worker",
        k8s_worker_name="typeflux-worker",
        container_image="typeflux-worker:k8s-smoke",
    )
    workflow_contribution = contributor.workflow(
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        )
    )

    expected = {
        "platform": "kubernetes",
        "kubernetes": {
            "namespace": "typeflux-smoke",
            "pod_name": "typeflux-worker-abc123",
            "pod_uid": "pod-uid-123",
            "node_name": "minikube",
            "service_account": "default",
            "deployment_name": "typeflux-worker",
            "worker_name": "typeflux-worker",
        },
        "container_image": "typeflux-worker:k8s-smoke",
    }
    assert workflow_contribution.workflow_metadata["typeflux"]["runtime_placement"] == expected
    assert workflow_contribution.workflow_manifest == {}
    assert workflow_contribution.search_tags == ()

    activity_contribution = contributor.activity(
        ActivityMetadataContext(
            manifest=None,
            activity_execution_manifest=None,
            invocation_context=None,
            level="activity",
        )
    )
    assert activity_contribution.activity_metadata["typeflux"]["runtime_placement"] == expected
    assert activity_contribution.workflow_manifest == {}
    assert activity_contribution.search_tags == ()


def test_runtime_placement_contributor_from_env_ignores_absent_and_empty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_names = (
        "TYPEFLUX_RUNTIME_PLATFORM",
        "TYPEFLUX_K8S_NAMESPACE",
        "TYPEFLUX_K8S_POD_NAME",
        "TYPEFLUX_K8S_POD_UID",
        "TYPEFLUX_K8S_NODE_NAME",
        "TYPEFLUX_K8S_SERVICE_ACCOUNT",
        "TYPEFLUX_K8S_DEPLOYMENT_NAME",
        "TYPEFLUX_K8S_WORKER_NAME",
        "TYPEFLUX_CONTAINER_IMAGE",
    )
    for name in env_names:
        monkeypatch.delenv(name, raising=False)

    empty = RuntimePlacementContributor.from_env().workflow(
        WorkflowMetadataContext(workflow_name="ClaimWorkflow", workflow_id="wf", task_queue="q")
    )
    assert empty.workflow_metadata == {}

    monkeypatch.setenv("TYPEFLUX_RUNTIME_PLATFORM", "  ")
    monkeypatch.setenv("TYPEFLUX_K8S_NAMESPACE", "typeflux-smoke")
    partial = RuntimePlacementContributor.from_env().workflow(
        WorkflowMetadataContext(workflow_name="ClaimWorkflow", workflow_id="wf", task_queue="q")
    )
    assert partial.workflow_metadata["typeflux"]["runtime_placement"] == {
        "kubernetes": {"namespace": "typeflux-smoke"}
    }


def test_runtime_placement_contributor_redaction_exclusions_preserve_only_safe_fields() -> None:
    redactor = RegexPIIRedactor.default(
        exclude_paths=redaction_exclusions(
            (
                RuntimePlacementContributor(
                    platform="kubernetes",
                    k8s_namespace="typeflux-smoke",
                    k8s_pod_name="worker-555-123-4567",
                    k8s_pod_uid="pod-uid",
                    k8s_node_name="node-a",
                    k8s_service_account="default",
                    k8s_deployment_name="worker",
                    k8s_worker_name="worker",
                    container_image="typeflux-worker:k8s-smoke",
                ),
            )
        )
    )

    redacted = redactor.redact(
        {
            "typeflux": {
                "runtime_placement": {
                    "platform": "kubernetes",
                    "kubernetes": {
                        "namespace": "typeflux-smoke",
                        "pod_name": "worker-555-123-4567",
                        "pod_uid": "pod-uid",
                        "node_name": "node-a",
                        "service_account": "default",
                        "deployment_name": "worker",
                        "worker_name": "worker",
                        "labels": {"owner": "ops-555-123-4567"},
                    },
                    "container_image": "typeflux-worker:k8s-smoke",
                    "service_account_token": "token-555-123-4567",
                }
            }
        }
    )

    placement = redacted["typeflux"]["runtime_placement"]
    assert placement["platform"] == "kubernetes"
    assert placement["kubernetes"]["pod_name"] == "worker-555-123-4567"
    assert placement["container_image"] == "typeflux-worker:k8s-smoke"
    assert placement["kubernetes"]["labels"]["owner"] == "ops-[REDACTED_PHONE]"
    assert placement["service_account_token"] == "token-[REDACTED_PHONE]"


def test_policy_contributor_emits_safe_workflow_and_manifest_metadata() -> None:
    policy = ComposedProjectPolicy(
        selected_policy_ids=("regulated",),
        applied_policy_ids=("base", "regulated"),
        policy_names=("base", "regulated"),
        policy_hash="a" * 64,
        payload={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
        },
    )
    contribution = PolicyContributor.from_policy(policy).workflow(
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        )
    )

    expected = {
        "version": "1",
        "selected_policy_ids": ["regulated"],
        "applied_policy_ids": ["base", "regulated"],
        "policy_names": ["base", "regulated"],
        "policy_hash": "a" * 64,
        "admission_status": "passed",
    }
    assert contribution.workflow_metadata["typeflux"]["policy"] == {
        **expected,
        "enforcement_mode": "runtime",
    }
    assert contribution.workflow_manifest["contributions"]["policy"] == expected
    assert contribution.search_tags == ()


def test_policy_contributor_from_guard_uses_entrypoint_metadata() -> None:
    policy = ComposedProjectPolicy(
        selected_policy_ids=("regulated",),
        applied_policy_ids=("base", "regulated"),
        policy_names=("base", "regulated"),
        policy_hash="a" * 64,
        payload={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
        },
    )
    context = WorkflowMetadataContext(
        workflow_name="ClaimWorkflow",
        workflow_id="claim-workflow-1",
        task_queue="claims",
    )

    contribution = PolicyContributor.from_guard(
        RuntimePolicyGuard(policy=policy, enforcement_mode="project_submit")
    ).workflow(context)
    override = PolicyContributor.from_guard(
        RuntimePolicyGuard(policy=policy, enforcement_mode="project_submit"),
        enforcement_mode="runtime",
    ).workflow(context)
    fallback = PolicyContributor.from_guard(type("Guard", (), {"policy": policy})()).workflow(
        context
    )

    assert contribution.workflow_metadata["typeflux"]["policy"]["enforcement_mode"] == (
        "project_submit"
    )
    assert "enforcement_mode" not in contribution.workflow_manifest["contributions"]["policy"]
    assert override.workflow_metadata["typeflux"]["policy"]["enforcement_mode"] == "runtime"
    assert fallback.workflow_metadata["typeflux"]["policy"]["enforcement_mode"] == "runtime"


def test_policy_contributor_emits_only_identity_not_full_payload() -> None:
    # Safety regression: the composed policy payload carries sensitive governance
    # detail (allow-lists, Temporal addresses, module roots). None of it may leak
    # into persisted metadata — only the safe identity/audit fields.
    policy = ComposedProjectPolicy(
        selected_policy_ids=("regulated",),
        applied_policy_ids=("base", "regulated"),
        policy_names=("base", "regulated"),
        policy_hash="a" * 64,
        payload={
            "version": "1",
            "selected_policy_ids": ["regulated"],
            "applied_policy_ids": ["base", "regulated"],
            "policy_names": ["base", "regulated"],
            "providers": {"allowed": {"openai": {"models": ["gpt-4o-mini"]}}},
            "runtime": {
                "temporal": {
                    "allowed_addresses": ["temporal.internal.example.com:7233"],
                    "allowed_regions": ["us-east"],
                }
            },
            "imports": {"allowed_module_roots": ["secret_pkg"]},
        },
    )
    contribution = PolicyContributor.from_policy(policy).workflow(
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        )
    )

    assert set(contribution.workflow_metadata["typeflux"]["policy"]) == {
        "version",
        "selected_policy_ids",
        "applied_policy_ids",
        "policy_names",
        "policy_hash",
        "enforcement_mode",
        "admission_status",
    }
    assert set(contribution.workflow_manifest["contributions"]["policy"]) == {
        "version",
        "selected_policy_ids",
        "applied_policy_ids",
        "policy_names",
        "policy_hash",
        "admission_status",
    }
    serialized = repr(contribution.workflow_metadata) + repr(contribution.workflow_manifest)
    for forbidden in (
        "providers",
        "allowed_addresses",
        "temporal.internal",
        "us-east",
        "secret_pkg",
        "gpt-4o-mini",
    ):
        assert forbidden not in serialized


def test_policy_contributor_emits_nothing_for_ungoverned_run() -> None:
    context = WorkflowMetadataContext(
        workflow_name="UngovernedWorkflow",
        workflow_id="ungoverned-1",
        task_queue="default",
    )
    for contributor in (
        PolicyContributor.from_policy(None),
        PolicyContributor.from_guard(None),
        PolicyContributor(),
    ):
        contribution = contributor.workflow(context)
        assert "typeflux" not in contribution.workflow_metadata
        assert contribution.workflow_manifest == {}
        assert contribution.redaction_exclusions == ()


def test_policy_contributor_redaction_exclusions_preserve_only_safe_fields() -> None:
    redactor = RegexPIIRedactor.default(
        exclude_paths=redaction_exclusions(
            (
                PolicyContributor(
                    version="1",
                    selected_policy_ids=("regulated-555-123-4567",),
                    applied_policy_ids=("base-555-123-4567", "regulated-555-123-4567"),
                    policy_names=("regulated-555-123-4567",),
                    policy_hash="b" * 64,
                    enforcement_mode="runtime",
                    admission_status="passed",
                ),
            )
        )
    )

    redacted = redactor.redact(
        {
            "typeflux": {
                "policy": {
                    "version": "1",
                    "selected_policy_ids": ["regulated-555-123-4567"],
                    "applied_policy_ids": ["base-555-123-4567"],
                    "policy_names": ["regulated-555-123-4567"],
                    "policy_hash": "b" * 64,
                    "enforcement_mode": "runtime",
                    "admission_status": "passed",
                    "description": "call 555-123-4567",
                },
                "execution_manifest": {
                    "contributions": {
                        "policy": {
                            "selected_policy_ids": ["regulated-555-123-4567"],
                            "policy_hash": "b" * 64,
                            "description": "call 555-123-4567",
                        }
                    }
                },
            }
        }
    )

    policy = redacted["typeflux"]["policy"]
    # Every excluded identity field must survive verbatim — including the list-valued
    # fields, whose values here embed phone-pattern substrings that would otherwise
    # be masked. A regression dropping any single exclusion path must fail here.
    assert policy["selected_policy_ids"] == ["regulated-555-123-4567"]
    assert policy["applied_policy_ids"] == ["base-555-123-4567"]
    assert policy["policy_names"] == ["regulated-555-123-4567"]
    assert policy["policy_hash"] == "b" * 64
    assert policy["version"] == "1"
    assert policy["enforcement_mode"] == "runtime"
    assert policy["admission_status"] == "passed"
    # Non-excluded fields are still redacted.
    assert policy["description"] == "call [REDACTED_PHONE]"
    assert redacted["typeflux"]["execution_manifest"]["contributions"]["policy"][
        "selected_policy_ids"
    ] == ["regulated-555-123-4567"]
    assert (
        redacted["typeflux"]["execution_manifest"]["contributions"]["policy"]["description"]
        == "call [REDACTED_PHONE]"
    )


def test_yaml_override_contributor_emits_paths_without_values() -> None:
    contribution = YamlOverrideContributor(
        YamlOverrideProvenance(
            source="project_environment",
            project_name="claims-platform",
            environment_id="prod",
            environment_name="production",
            workflow_id="claim_review",
            override_paths=(
                "runtime.temporal.api_key",
                "runtime.temporal.tls.client_private_key_file",
                "runtime.provider.api_key",
                "task_queue",
            ),
        )
    ).workflow(
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        )
    )

    expected = {
        "source": "project_environment",
        "project_name": "claims-platform",
        "environment_id": "prod",
        "environment_name": "production",
        "workflow_id": "claim_review",
        "override_paths": [
            "runtime.temporal.api_key",
            "runtime.temporal.tls.client_private_key_file",
            "runtime.provider.api_key",
            "task_queue",
        ],
    }
    assert contribution.workflow_metadata["typeflux"]["yaml_overrides"] == expected
    assert contribution.workflow_manifest["contributions"]["yaml_overrides"] == expected
    assert contribution.search_tags == ()
    assert "api-key-value" not in repr(contribution.workflow_metadata)


def test_secret_reference_contributor_emits_safe_workflow_and_manifest_metadata() -> None:
    contribution = SecretReferenceContributor(
        (
            SecretReferenceRecord(
                runtime_path="runtime.provider.api_key",
                source_kind="env",
                source_name="OPENAI_API_KEY",
                configured=True,
            ),
            SecretReferenceRecord(
                runtime_path="runtime.temporal.tls.client_private_key",
                source_kind="file",
                source_name="/run/secrets/temporal-client-key.pem",
                configured=False,
            ),
        )
    ).workflow(
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        )
    )

    expected = {
        "references": [
            {
                "runtime_path": "runtime.provider.api_key",
                "source_kind": "env",
                "source_name": "OPENAI_API_KEY",
                "configured": True,
            },
            {
                "runtime_path": "runtime.temporal.tls.client_private_key",
                "source_kind": "file",
                "source_name": "/run/secrets/temporal-client-key.pem",
                "configured": False,
            },
        ]
    }
    assert contribution.workflow_metadata["typeflux"]["secret_references"] == expected
    assert contribution.workflow_manifest["contributions"]["secret_references"] == expected
    assert contribution.search_tags == ()


def test_secret_reference_contributor_redaction_exclusions_do_not_preserve_secret_values() -> None:
    redactor = RegexPIIRedactor.default(
        exclude_paths=redaction_exclusions(
            (
                SecretReferenceContributor(
                    (
                        SecretReferenceRecord(
                            runtime_path="runtime.provider.api_key",
                            source_kind="env",
                            source_name="OPENAI_API_KEY",
                            configured=True,
                        ),
                    )
                ),
            )
        )
    )

    redacted = redactor.redact(
        {
            "typeflux": {
                "secret_references": {
                    "references": [
                        {
                            "runtime_path": "runtime.provider.api_key",
                            "source_kind": "env",
                            "source_name": "OPENAI_API_KEY",
                            "configured": True,
                            "api_key": "secret-555-123-4567",
                        }
                    ]
                },
                "execution_manifest": {
                    "contributions": {
                        "secret_references": {
                            "references": [
                                {
                                    "runtime_path": "runtime.provider.api_key",
                                    "source_kind": "env",
                                    "source_name": "OPENAI_API_KEY",
                                    "configured": True,
                                }
                            ]
                        }
                    }
                },
            }
        }
    )

    reference = redacted["typeflux"]["secret_references"]["references"][0]
    assert reference["source_name"] == "OPENAI_API_KEY"
    assert reference["configured"] is True
    assert reference["api_key"] == "secret-[REDACTED_PHONE]"


def test_merge_contributions_rejects_conflicting_values() -> None:
    with pytest.raises(MetadataConflictError):
        merge_contributions(
            (
                MetadataContribution(workflow_metadata={"typeflux": {"yaml": {"name": "a"}}}),
                MetadataContribution(workflow_metadata={"typeflux": {"yaml": {"name": "b"}}}),
            )
        )


def test_merge_contributions_allows_identical_duplicate_values() -> None:
    merged = merge_contributions(
        (
            MetadataContribution(workflow_metadata={"typeflux": {"yaml": {"name": "demo"}}}),
            MetadataContribution(workflow_metadata={"typeflux": {"yaml": {"name": "demo"}}}),
        )
    )

    assert merged.workflow_metadata["typeflux"]["yaml"]["name"] == "demo"


def test_lifecycle_operation_contributor_emits_safe_metadata_and_tags() -> None:
    contribution = lifecycle_operation_contribution(
        (LifecycleOperationContributor(),),
        LifecycleOperationMetadataContext(
            operation_type="query",
            operation_name="typeflux_lifecycle_status",
            workflow_name="LifecycleReviewWorkflow",
            workflow_id="lifecycle-review-1",
            run_id="run-1",
            status=WorkflowLifecycleStatus(
                state="waiting_for_review",
                current_step="package_for_review",
                completed_units=2,
                total_units=3,
                cancellation_requested=False,
                cancellation_reason="contains sensitive freeform reason",
                waiting_checkpoint="package_for_review",
                terminal_status=None,
                event_count=8,
                events_truncated=True,
            ),
        ),
    )

    assert contribution.operation_metadata == {
        "typeflux": {
            "level": "lifecycle_operation",
            "lifecycle_operation": {
                "operation_type": "query",
                "operation_name": "typeflux_lifecycle_status",
                "workflow_name": "LifecycleReviewWorkflow",
                "workflow_id": "lifecycle-review-1",
                "run_id": "run-1",
                "status": {
                    "state": "waiting_for_review",
                    "current_step": "package_for_review",
                    "completed_units": 2,
                    "total_units": 3,
                    "waiting_checkpoint": "package_for_review",
                    "cancellation_requested": False,
                    "event_count": 8,
                    "events_truncated": True,
                },
            },
        }
    }
    assert contribution.search_tags == (
        "typeflux",
        "typeflux.lifecycle",
        "typeflux.lifecycle.query:typeflux_lifecycle_status",
        "typeflux.workflow:LifecycleReviewWorkflow",
    )
    assert "contains sensitive freeform reason" not in str(contribution.operation_metadata)


def test_lifecycle_operation_contributor_preserves_review_route_without_reviewer_notes() -> None:
    contribution = lifecycle_operation_contribution(
        (LifecycleOperationContributor(),),
        LifecycleOperationMetadataContext(
            operation_type="signal",
            operation_name="typeflux_submit_review",
            workflow_name="LifecycleReviewWorkflow",
            workflow_id="lifecycle-review-1",
            review_user_decision="send_email",
            review_route_target="send_email",
        ),
    )

    operation = contribution.operation_metadata["typeflux"]["lifecycle_operation"]
    assert operation["review_user_decision"] == "send_email"
    assert operation["review_route_target"] == "send_email"
    assert "reviewer" not in operation
    assert "notes" not in operation


def test_merge_contributions_includes_operation_metadata() -> None:
    merged = merge_contributions(
        (
            MetadataContribution(
                operation_metadata={
                    "typeflux": {"lifecycle_operation": {"operation_type": "query"}}
                }
            ),
            MetadataContribution(
                operation_metadata={
                    "typeflux": {"lifecycle_operation": {"operation_name": "status"}}
                }
            ),
        )
    )

    assert merged.operation_metadata == {
        "typeflux": {
            "lifecycle_operation": {
                "operation_type": "query",
                "operation_name": "status",
            }
        }
    }


def test_yaml_workflow_contributor_emits_spec_identity() -> None:
    contributor = YamlWorkflowContributor(
        project="demo_project",
        name="demo_yaml",
        spec_digest="a" * 64,
        spec_digest_algorithm="typeflux-yaml-graph-v1",
        generator_version="1",
        workflow_type="DemoWorkflow.v7",
        workflow_version_label="v7",
    )

    contribution = contributor.workflow(
        WorkflowMetadataContext(workflow_name="DemoWorkflow", workflow_id="wf-1", task_queue="q")
    )

    yaml_metadata = contribution.workflow_metadata["typeflux"]["yaml"]
    assert yaml_metadata["spec_digest"] == "a" * 64
    assert yaml_metadata["spec_digest_algorithm"] == "typeflux-yaml-graph-v1"
    assert yaml_metadata["generator_version"] == "1"
    assert yaml_metadata["workflow_type"] == "DemoWorkflow.v7"
    assert yaml_metadata["workflow_version_label"] == "v7"
    assert contribution.workflow_manifest["contributions"]["yaml"]["spec_digest"] == "a" * 64
    assert "typeflux.yaml.spec_digest" in contribution.redaction_exclusions
    assert "typeflux.yaml.workflow_type" in contribution.redaction_exclusions


def test_yaml_lifecycle_contributor_preserves_only_enumerated_safe_fields() -> None:
    contributor = YamlLifecycleContributor({"state": "pending"})
    redactor = RegexPIIRedactor.default(exclude_paths=redaction_exclusions((contributor,)))

    redacted = redactor.redact(
        {
            "typeflux": {
                "lifecycle": {
                    "state": "pending",
                    "current_step": "package_for_review",
                    "completed_units": 0,
                    "total_units": 3,
                    "waiting_checkpoint": "package_for_review",
                    "review_after_step": "package_for_review",
                    "contact_note": "call me at 555-123-4567",
                }
            }
        }
    )

    lifecycle = redacted["typeflux"]["lifecycle"]
    assert lifecycle["state"] == "pending"
    assert lifecycle["current_step"] == "package_for_review"
    assert lifecycle["waiting_checkpoint"] == "package_for_review"
    assert lifecycle["review_after_step"] == "package_for_review"
    assert lifecycle["contact_note"] == "call me at [REDACTED_PHONE]"


def test_lifecycle_operation_contributor_declares_redaction_exclusions() -> None:
    redactor = RegexPIIRedactor.default(
        exclude_paths=redaction_exclusions((LifecycleOperationContributor(),))
    )

    redacted = redactor.redact(
        {
            "typeflux": {
                "lifecycle_operation": {
                    "workflow_id": "lifecycle-review-555-123-4567",
                    "run_id": "run-555-123-4567",
                    "status": {"state": "waiting_for_review"},
                }
            },
            "review": {
                "reviewer": "reviewer@example.com",
                "notes": "call me at 555-123-4567",
            },
        }
    )

    assert redacted["typeflux"]["lifecycle_operation"]["workflow_id"] == (
        "lifecycle-review-555-123-4567"
    )
    assert redacted["typeflux"]["lifecycle_operation"]["run_id"] == "run-555-123-4567"
    assert redacted["review"]["reviewer"] == "[REDACTED_EMAIL]"
    assert redacted["review"]["notes"] == "call me at [REDACTED_PHONE]"


def test_future_contributor_surfaces_across_metadata_views_and_redaction() -> None:
    contributor = _TestContributor()
    activity, resolved, rendered = _activity_context()
    activity_manifest = build_activity_manifest(activity, resolved)
    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=activity_manifest,
        resolved_prompt=resolved,
        rendered_messages=rendered,
        validation_attempt=0,
    )
    workflow_metadata = workflow_invocation_metadata(
        workflow_name="ClaimWorkflow",
        workflow_id="claim-workflow-1",
        task_queue="claims",
        activities=(activity,),
        metadata_contributors=(contributor,),
    )
    workflow_metadata["tags"] = workflow_search_tags(
        workflow_name="ClaimWorkflow",
        activities=(activity,),
        metadata=workflow_metadata,
        metadata_contributors=(contributor,),
    )
    activity_metadata = semantic_metadata(
        manifest=activity_manifest,
        activity_execution_manifest=execution_manifest,
        invocation_context=AIInvocationContext(
            temporal_namespace="default",
            temporal_workflow_type="ClaimWorkflow",
            temporal_workflow_id="claim-workflow-1",
            temporal_run_id="run-1",
            temporal_activity_type="classify",
            temporal_activity_id="classify-0",
            temporal_activity_attempt=1,
            typeflux_activity_name="classify",
            typeflux_manifest_hash=activity_manifest.manifest_hash,
            temporal_task_queue="claims",
            map_step_id="review_evidence",
            map_index=0,
            map_size=2,
            map_concurrency=1,
        ),
        level="activity",
        metadata_contributors=(contributor,),
    )

    assert workflow_metadata["typeflux"]["test"] == {
        "case_id": "claim-555-123-4567",
        "enabled": True,
    }
    assert workflow_metadata["typeflux"]["execution_manifest"]["contributions"]["test"] == {
        "case_id": "claim-555-123-4567",
        "enabled": True,
    }
    assert "typeflux.test:enabled" in workflow_metadata["tags"]
    assert activity_metadata["typeflux"]["temporal"]["workflow_id"] == "claim-workflow-1"
    assert activity_metadata["typeflux"]["temporal"]["task_queue"] == "claims"
    assert activity_metadata["typeflux"]["map"]["map_step_id"] == "review_evidence"
    assert activity_metadata["typeflux"]["test_activity"]["batch_id"] == ("batch-555-123-4567")

    workflow_view = WorkflowExecutionManifestView.from_payload(
        workflow_metadata["typeflux"]["execution_manifest"]
    )
    assert workflow_view.to_public_dict()["contributions"]["test"]["enabled"] is True

    trace = TraceRecord(
        trace_id="trace-1",
        metadata=workflow_metadata,
        observations=(
            ObservationRecord(
                observation_id="obs-1",
                name="classify",
                type="span",
                metadata=activity_metadata,
            ),
        ),
    )
    inspection = inspect_trace(InMemoryTraceStore((trace,)), "trace-1")
    manifest = inspection.to_manifest_dict()
    assert manifest["workflow"]["contributions"]["test"]["case_id"] == ("claim-555-123-4567")
    public_trace = trace.to_public_dict()
    assert public_trace["observations"][0]["metadata"]["typeflux"]["test_activity"] == {
        "batch_id": "batch-555-123-4567"
    }

    redactor = RegexPIIRedactor.default(exclude_paths=redaction_exclusions((contributor,)))
    redacted = redactor.redact(
        {
            "typeflux": {
                "test": {"case_id": "claim-555-123-4567"},
                "execution_manifest": {
                    "contributions": {
                        "test": {"case_id": "claim-555-123-4567"},
                    }
                },
            },
            "customer": {"phone": "555-123-4567"},
        }
    )
    assert redacted["typeflux"]["test"]["case_id"] == "claim-555-123-4567"
    assert redacted["typeflux"]["execution_manifest"]["contributions"]["test"]["case_id"] == (
        "claim-555-123-4567"
    )
    assert redacted["customer"]["phone"] == "[REDACTED_PHONE]"


def test_workflow_metadata_replaces_stale_contributor_owned_typeflux_sections() -> None:
    activity, _, _ = _activity_context()

    metadata = workflow_invocation_metadata(
        workflow_name="ClaimWorkflow",
        workflow_id="claim-workflow-1",
        task_queue="claims",
        activities=(activity,),
        metadata={
            "external": {"keep": True},
            "typeflux": {
                "custom": {"keep": True},
                "execution_manifest": {
                    "workflow_id": "stale-workflow",
                    "obsolete": "stale-only",
                },
            },
        },
    )

    assert metadata["external"] == {"keep": True}
    assert metadata["typeflux"]["custom"] == {"keep": True}
    manifest = metadata["typeflux"]["execution_manifest"]
    assert manifest["workflow_id"] == "claim-workflow-1"
    assert "obsolete" not in manifest


def test_activity_context_contributor_declares_temporal_redaction_exclusions() -> None:
    redactor = RegexPIIRedactor.default(
        exclude_paths=redaction_exclusions((ActivityContextContributor(),))
    )

    redacted = redactor.redact(
        {
            "typeflux": {
                "temporal": {
                    "workflow_id": "claim-workflow-555-123-4567",
                    "run_id": "run-555-123-4567",
                }
            },
            "customer": {"phone": "555-123-4567"},
        }
    )

    assert redacted["typeflux"]["temporal"] == {
        "workflow_id": "claim-workflow-555-123-4567",
        "run_id": "run-555-123-4567",
    }
    assert redacted["customer"]["phone"] == "[REDACTED_PHONE]"


def test_activity_contribution_preserves_existing_context_shape() -> None:
    contributor = _TestContributor()
    contribution = activity_contribution(
        (contributor,),
        ActivityMetadataContext(
            manifest=None,
            activity_execution_manifest=None,
            invocation_context=None,
            level="activity",
        ),
    )

    assert contribution.activity_metadata["typeflux"]["test_activity"] == {
        "batch_id": "batch-555-123-4567"
    }


def test_workflow_contribution_preserves_existing_workflow_shape() -> None:
    contributor = _TestContributor()
    contribution = workflow_contribution(
        (contributor,),
        WorkflowMetadataContext(
            workflow_name="ClaimWorkflow",
            workflow_id="claim-workflow-1",
            task_queue="claims",
        ),
    )

    assert contribution.workflow_manifest["contributions"]["test"]["enabled"] is True
    assert contribution.workflow_metadata["typeflux"]["test"]["enabled"] is True


def test_activity_manifest_metadata_includes_safe_artifact_input_definitions() -> None:
    activity = AIActivity(
        name="review_claim",
        input_type=Input,
        output_type=Output,
        prompt_ref=PromptRef("demo/review"),
        artifact_inputs=(
            ArtifactInput(
                name="claim_documents",
                from_path="input.text",
                kind="data",
                media_types=("text/plain",),
            ),
        ),
    )
    resolved = ResolvedPrompt(
        ref=PromptRef("demo/review"),
        messages=(ChatMessage(role="user", content="Review {{ text }}"),),
        resolved_version="7",
    )
    manifest = build_activity_manifest(activity, resolved)
    execution_manifest = build_activity_execution_manifest(
        activity=activity,
        activity_manifest=manifest,
        resolved_prompt=resolved,
        rendered_messages=(ChatMessage(role="user", content="Review hello"),),
        validation_attempt=0,
    )

    metadata = semantic_metadata(
        manifest=manifest,
        activity_execution_manifest=execution_manifest,
        invocation_context=None,
        level="generation",
    )

    activity_payload = metadata["typeflux"]["activity"]
    assert activity_payload["artifact_inputs"] == [
        {
            "name": "claim_documents",
            "from_path": "input.text",
            "required": True,
            "kind": "data",
            "media_types": ["text/plain"],
        }
    ]
    assert "Review hello" not in repr(metadata["typeflux"])


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
