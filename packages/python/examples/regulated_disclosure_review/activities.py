from __future__ import annotations

from examples.regulated_disclosure_review.schemas import (
    DisclosureInput,
    FinalDecision,
    ReviewPacket,
    RiskAssessment,
)
from typeflux import AIActivity, PromptRef


def normalize_assessment(input: DisclosureInput, output: RiskAssessment) -> RiskAssessment:
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


assess_disclosure = AIActivity(
    name="assess_disclosure",
    input_type=DisclosureInput,
    output_type=RiskAssessment,
    prompt_ref=PromptRef("regulated-disclosure-assess"),
    hook=normalize_assessment,
)

package_for_review = AIActivity(
    name="package_for_review",
    input_type=RiskAssessment,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("regulated-disclosure-package"),
    hook=normalize_review_packet,
)

prepare_submission = AIActivity(
    name="prepare_submission",
    input_type=ReviewPacket,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("regulated-disclosure-prepare"),
    hook=normalize_review_route,
)

route_to_compliance = AIActivity(
    name="route_to_compliance",
    input_type=ReviewPacket,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("regulated-disclosure-route"),
    hook=normalize_review_route,
)

finalize_disclosure = AIActivity(
    name="finalize_disclosure",
    input_type=ReviewPacket,
    output_type=FinalDecision,
    prompt_ref=PromptRef("regulated-disclosure-finalize"),
    hook=normalize_final_decision,
)


ALL_ACTIVITIES = (
    assess_disclosure,
    package_for_review,
    prepare_submission,
    route_to_compliance,
    finalize_disclosure,
)
