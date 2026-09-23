"""Schemas for the canonical conformance fixture project (#617)."""

from pydantic import BaseModel


class ClaimItem(BaseModel):
    value: str


class ClaimInput(BaseModel):
    claims: list[ClaimItem]


class ItemAssessment(BaseModel):
    value: str


class AssessmentBatch(BaseModel):
    reviews: list[ItemAssessment]


class Decision(BaseModel):
    value: str


class Classification(BaseModel):
    value: str
    deep_review: bool


class ReviewFanout(BaseModel):
    """Collect object of the composition fixture's parallel block (#55): fields ARE
    the branch ids (decision D4); the gated ``summary`` branch's field is Optional."""

    assessments: AssessmentBatch
    summary: AssessmentBatch | None
