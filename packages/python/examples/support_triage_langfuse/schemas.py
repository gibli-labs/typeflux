from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


Category = Literal["billing", "technical", "account", "other"]
Tone = Literal["apologetic", "informative", "escalation", "celebratory"]
Verdict = Literal["approve", "flag", "reject"]


class TicketInput(BaseModel):
    customer_id: str = Field(description="Internal customer identifier.")
    subject: str = Field(description="Ticket subject line.")
    body: str = Field(description="Full ticket body as submitted by the customer.")
    received_at: datetime = Field(description="Server-side receive timestamp.")


class Classification(BaseModel):
    category: Category = Field(description="Primary ticket category.")
    urgency: Urgency = Field(description="Urgency bucket.")
    sentiment: float = Field(
        ge=-1.0,
        le=1.0,
        description="Customer sentiment, -1 angry to +1 happy.",
    )
    topics: list[str] = Field(description="Keyword-like topics mentioned in the ticket.")

    @field_validator("topics")
    @classmethod
    def _normalize_topics(cls, value: list[str]) -> list[str]:
        return sorted({topic.strip().lower() for topic in value if topic.strip()})


class RoutingDecision(BaseModel):
    team: str = Field(description="Handling team slug. Must be a known team.")
    sla_hours: int = Field(ge=1, le=168, description="Target response SLA in hours.")
    on_call_engineer: str | None = Field(
        default=None,
        description="Assigned on-call engineer, if escalated.",
    )
    escalated: bool = Field(
        default=False,
        description="True if routed to on-call due to urgency or timing.",
    )


class DraftReply(BaseModel):
    subject: str = Field(description="Reply subject line.")
    body: str = Field(description="Reply body in plain text.")
    tone: Tone = Field(description="Overall tone of the reply.")
    redaction_count: int = Field(
        default=0,
        ge=0,
        description="Count of PII tokens the hook redacted from body.",
    )
    escalated: bool = Field(
        default=False,
        description="Propagated from RoutingDecision.escalated by the hook.",
    )


class ReviewPacket(BaseModel):
    verdict: Verdict = Field(description="Suggested final disposition.")
    summary: str = Field(description="One-paragraph ticket and reply summary.")
    approval_required: bool = Field(
        default=True,
        description="True if a human must approve before sending.",
    )
