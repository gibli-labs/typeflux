from __future__ import annotations

from pydantic import BaseModel, Field


class DisclosureInput(BaseModel):
    case_id: str = Field(description="Disclosure case identifier.")
    customer_name: str = Field(description="Customer name.")
    request: str = Field(description="Disclosure request to review.")
    risk_notes: list[str] = Field(default_factory=list, description="Known risk notes.")


class RiskAssessment(BaseModel):
    case_id: str
    risk_level: str
    summary: str
    flags: list[str] = Field(default_factory=list)


class ReviewPacket(BaseModel):
    case_id: str
    recommendation: str
    summary: str
    approval_required: bool
    flags: list[str] = Field(default_factory=list)


class FinalDecision(BaseModel):
    case_id: str
    decision: str
    summary: str
    approved: bool
