from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ClaimInput(BaseModel):
    claim_id: str
    claimant: str
    policy_number: str
    amount_usd: float
    description: str


class ClaimAssessment(BaseModel):
    claim_id: str
    category: Literal["auto", "property", "medical", "other"]
    severity: Literal["low", "medium", "high"]
    fraud_indicators: list[str]
    summary: str


class CompliancePolicyResult(BaseModel):
    claim_id: str
    severity: str
    requires_senior_review: bool
    policy_flags: list[str]
    audit_code: str
    summary: str


class FinalAdjudication(BaseModel):
    claim_id: str
    decision: str
    audit_code: str
    policy_flags: list[str]
    escalated: bool
