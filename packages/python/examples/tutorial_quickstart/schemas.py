from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TicketInput(BaseModel):
    """A raw support ticket handed to the workflow."""

    subject: str = Field(description="Ticket subject line.")
    body: str = Field(description="Ticket body as written by the customer.")


class Triage(BaseModel):
    """The typed result the model must return."""

    category: Literal["billing", "bug", "how_to", "account", "other"] = Field(
        description="Best-fit category for routing the ticket."
    )
    urgency: Literal["low", "medium", "high"] = Field(
        description="How quickly a human should look at this ticket."
    )
    summary: str = Field(description="One-sentence summary a human agent can skim.")
