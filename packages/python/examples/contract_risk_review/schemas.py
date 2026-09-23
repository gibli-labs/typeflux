from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RiskSeverity = Literal["low", "medium", "high", "critical"]


class EvidenceCitation(BaseModel):
    quote: str = Field(description="Short supporting quote copied from the contract.")
    location: str | None = Field(
        default=None,
        description="Page, section, exhibit, or other location if available from the PDF.",
    )


class ContractAnalysisInput(BaseModel):
    engagement_id: str
    business_context: str = Field(
        description="Why this contract is being reviewed and which lens to use."
    )
    review_objective: str = Field(
        description="Specific extraction/risk-review objective for this run."
    )
    contract_files: list[str] = Field(description="Local PDF paths visible to the worker process.")

    @field_validator("contract_files")
    @classmethod
    def _require_contract_file(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("contract_files must contain at least one PDF")
        return value


class ContractEntity(BaseModel):
    name: str
    entity_type: str = Field(description="Example: company, person, agency, address.")
    role: str = Field(description="Contract role, such as supplier, customer, licensor.")
    jurisdiction_or_address: str | None = None
    evidence: list[EvidenceCitation] = []


class KeyDate(BaseModel):
    label: str = Field(description="Example: effective date, renewal date, notice deadline.")
    date_text: str
    normalized_date: str | None = Field(
        default=None,
        description="ISO date when unambiguous; otherwise null.",
    )
    evidence: list[EvidenceCitation] = []


class MonetaryTerm(BaseModel):
    label: str = Field(description="Example: fees, cap, penalty, payment deadline.")
    amount_or_formula: str
    currency: str | None = None
    cadence_or_basis: str | None = None
    evidence: list[EvidenceCitation] = []


class ClauseFinding(BaseModel):
    clause_type: str = Field(
        description=(
            "Open text category, such as termination, liability, indemnity, payment, "
            "confidentiality, IP, data protection, assignment, audit, governing law."
        )
    )
    present: bool
    title: str
    summary: str
    obligations: list[str] = []
    rights: list[str] = []
    missing_or_ambiguous_terms: list[str] = []
    evidence: list[EvidenceCitation] = []


class RiskFinding(BaseModel):
    severity: RiskSeverity
    category: str
    issue: str
    why_it_matters: str
    recommended_action: str
    owner: str | None = Field(
        default=None,
        description="Suggested owner, such as legal, finance, security, procurement.",
    )
    evidence: list[EvidenceCitation] = []


class ContractRiskReview(BaseModel):
    contract_title: str
    document_type: str
    executive_summary: str
    parties: list[ContractEntity]
    key_dates: list[KeyDate]
    monetary_terms: list[MonetaryTerm]
    clauses: list[ClauseFinding]
    risks: list[RiskFinding]
    missing_information: list[str]
    recommended_next_steps: list[str]
