from __future__ import annotations

from examples.lifecycle_review.schemas import (
    CaseInput,
    FinalDecision,
    ReviewPacket,
    RiskAssessment,
)
from typeflux import AIActivity, PromptRef


def normalize_assessment(input: CaseInput, output: RiskAssessment) -> RiskAssessment:
    return output.model_copy(
        update={
            "case_id": input.case_id,
            "risk_level": output.risk_level or ("high" if input.risk_notes else "medium"),
            "flags": sorted(set((*input.risk_notes, *output.flags))),
        }
    )


def normalize_review_packet(input: RiskAssessment, output: ReviewPacket) -> ReviewPacket:
    return output.model_copy(
        update={
            "case_id": input.case_id,
            "approval_required": True,
            "flags": sorted(set((*input.flags, *output.flags))),
        }
    )


def normalize_review_route(input: ReviewPacket, output: ReviewPacket) -> ReviewPacket:
    return output.model_copy(
        update={
            "case_id": input.case_id,
            "approval_required": input.approval_required,
            "flags": sorted(set((*input.flags, *output.flags))),
        }
    )


def normalize_final_decision(input: ReviewPacket, output: FinalDecision) -> FinalDecision:
    return output.model_copy(
        update={
            "case_id": input.case_id,
            "decision": output.decision or "approved",
            "approved": output.decision != "rejected",
        }
    )


assess_case = AIActivity(
    name="assess_case",
    input_type=CaseInput,
    output_type=RiskAssessment,
    prompt_ref=PromptRef("lifecycle-review-assess"),
    hook=normalize_assessment,
)


package_for_review = AIActivity(
    name="package_for_review",
    input_type=RiskAssessment,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("lifecycle-review-package"),
    hook=normalize_review_packet,
)


prepare_submission = AIActivity(
    name="prepare_submission",
    input_type=ReviewPacket,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("lifecycle-review-prepare-submission"),
    hook=normalize_review_route,
)


route_to_department = AIActivity(
    name="route_to_department",
    input_type=ReviewPacket,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("lifecycle-review-route-department"),
    hook=normalize_review_route,
)


send_email = AIActivity(
    name="send_email",
    input_type=ReviewPacket,
    output_type=FinalDecision,
    prompt_ref=PromptRef("lifecycle-review-send-email"),
    hook=normalize_final_decision,
)


ALL_ACTIVITIES = (
    assess_case,
    package_for_review,
    prepare_submission,
    route_to_department,
    send_email,
)
