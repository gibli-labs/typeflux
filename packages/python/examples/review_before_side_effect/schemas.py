from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimInput(BaseModel):
    claim_id: str = Field(description="Claim identifier.")
    claimant: str = Field(description="Claimant name.")
    amount: float = Field(description="Requested disbursement amount.")


class Assessment(BaseModel):
    claim_id: str
    recommended: bool
    rationale: str


class Disbursement(BaseModel):
    """The output of the side-effecting payment. ``idempotency_key`` keys the external
    transfer so a retry never double-pays."""

    claim_id: str
    transfer_id: str
    idempotency_key: str
    amount: float


class Reversal(BaseModel):
    claim_id: str
    reversed: bool
