from __future__ import annotations

from pydantic import BaseModel, Field


class Ticket(BaseModel):
    subject: str
    body: str
    customer_tier: str = "standard"


class Classification(BaseModel):
    category: str
    urgency: str
    summary: str


class RoutingDecision(BaseModel):
    team: str
    priority: int = Field(ge=1, le=5)
    reason: str


class DraftReply(BaseModel):
    subject: str
    body: str


class PackagedReply(BaseModel):
    subject: str
    body: str
    team: str = "support"
    priority: int = 3
    internal_note: str
