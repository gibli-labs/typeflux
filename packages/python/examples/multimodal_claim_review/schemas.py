from __future__ import annotations

from pydantic import BaseModel


class ClaimReviewInput(BaseModel):
    claim_id: str
    claimant_summary: str
    documents: list[str]
    photos: list[str] = []


class ClaimReviewOutput(BaseModel):
    disposition: str
    risk_level: str
    evidence_summary: str
