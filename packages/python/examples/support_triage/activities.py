from __future__ import annotations

from examples.support_triage.schemas import (
    Classification,
    DraftReply,
    PackagedReply,
    RoutingDecision,
    Ticket,
)
from typeflux.core import AIActivity, PromptRef, ai_activity

classify_ticket = AIActivity(
    name="classify_ticket",
    input_type=Ticket,
    output_type=Classification,
    prompt_ref=PromptRef("support/classify"),
    validation_retries=1,
)


route_ticket = AIActivity(
    name="route_ticket",
    input_type=Ticket,
    output_type=RoutingDecision,
    prompt_ref=PromptRef("support/route"),
    validation_retries=1,
)


draft_reply = AIActivity(
    name="draft_reply",
    input_type=Ticket,
    output_type=DraftReply,
    prompt_ref=PromptRef("support/draft"),
    validation_retries=2,
)


@ai_activity.defn(
    name="package_reply",
    prompt=PromptRef("support/package"),
    output=PackagedReply,
    validation_retries=0,
)
def package_reply(ticket: Ticket, output: PackagedReply) -> PackagedReply:
    if ticket.customer_tier == "enterprise" and output.priority < 2:
        return output.model_copy(update={"priority": 2})
    return output


ALL_ACTIVITIES = (classify_ticket, route_ticket, draft_reply, package_reply)
