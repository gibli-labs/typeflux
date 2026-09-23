"""Schemas for the privacy-governance example (#188)."""

from __future__ import annotations

from pydantic import BaseModel


class DisclosureRequest(BaseModel):
    case_id: str
    body: str


class DisclosureAssessment(BaseModel):
    case_id: str
    risk_level: str
    summary: str
