"""Typed schemas for the child-workflow composition example (#396).

Kept in their own module so the import-light workflow module can depend on them
without pulling in ``typeflux`` (the determinism sandbox re-imports the
workflow module).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Claim(BaseModel):
    """One claim to assess (the child workflow's input)."""

    claim_id: str = Field(description="Stable claim identifier.")
    text: str = Field(description="The claim under review.")


class Verdict(BaseModel):
    """The assessment for a claim (the child workflow's typed result)."""

    claim_id: str = Field(description="The assessed claim's id.")
    substantiated: bool = Field(description="Whether the claim is substantiated.")
    rationale: str = Field(description="Why the claim is (or is not) substantiated.")
