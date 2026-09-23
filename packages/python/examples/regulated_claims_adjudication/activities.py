"""Deterministic compliance steps as normal Temporal activities.

These are plain ``@temporalio.activity.defn`` callables: no prompt, no
provider, no AI wrapper. The YAML workflow discovers them from module
globals and registers them with the worker as-is, so the regulated rules
engine never couples to registry/provider configuration or the AI repair
loop.
"""

from __future__ import annotations

from temporalio import activity

from examples.regulated_claims_adjudication.schemas import (
    ClaimAssessment,
    CompliancePolicyResult,
    FinalAdjudication,
)

_SENIOR_REVIEW_THRESHOLD_FLAGS = 1


@activity.defn(name="apply_compliance_policy")
async def apply_compliance_policy(value: ClaimAssessment) -> CompliancePolicyResult:
    policy_flags: list[str] = []
    if value.severity == "high":
        policy_flags.append("SEVERITY_HIGH")
    if value.fraud_indicators:
        policy_flags.append("FRAUD_SCREEN")
    if value.category == "medical":
        policy_flags.append("PHI_HANDLING")
    requires_senior_review = len(policy_flags) >= _SENIOR_REVIEW_THRESHOLD_FLAGS
    audit_code = f"REG-{value.claim_id}-{value.severity.upper()}-{len(policy_flags)}"
    return CompliancePolicyResult(
        claim_id=value.claim_id,
        severity=value.severity,
        requires_senior_review=requires_senior_review,
        policy_flags=policy_flags,
        audit_code=audit_code,
        summary=value.summary,
    )


@activity.defn(name="escalate_case")
async def escalate_case(value: CompliancePolicyResult) -> CompliancePolicyResult:
    flags = [*value.policy_flags, "ESCALATED_TO_SIU"]
    return CompliancePolicyResult(
        claim_id=value.claim_id,
        severity=value.severity,
        requires_senior_review=True,
        policy_flags=flags,
        audit_code=f"{value.audit_code}-SIU",
        summary=value.summary,
    )


@activity.defn(name="finalize_adjudication")
async def finalize_adjudication(value: CompliancePolicyResult) -> FinalAdjudication:
    escalated = "ESCALATED_TO_SIU" in value.policy_flags
    decision = "referred_to_siu" if escalated else "approved_for_payment"
    return FinalAdjudication(
        claim_id=value.claim_id,
        decision=decision,
        audit_code=value.audit_code,
        policy_flags=value.policy_flags,
        escalated=escalated,
    )
