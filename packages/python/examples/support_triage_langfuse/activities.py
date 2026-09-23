from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from examples.support_triage_langfuse import domain
from examples.support_triage_langfuse.schemas import (
    Classification,
    DraftReply,
    ReviewPacket,
    RoutingDecision,
    TicketInput,
    Urgency,
)
from typeflux.core import PromptRef, ai_activity

log = logging.getLogger("typeflux.triage.langfuse")


def _default_clock() -> datetime:
    return datetime.now(UTC)


_CLOCK: Callable[[], datetime] = _default_clock
_CRITICAL_KEYWORDS = frozenset({"outage", "down", "broken", "cannot access", "can't access"})


@ai_activity.defn(
    name="classify_ticket",
    prompt=PromptRef("triage-langfuse-classify"),
    output=Classification,
    validation_retries=2,
)
def classify_ticket(input: TicketInput, output: Classification) -> Classification:
    if output.urgency is not Urgency.CRITICAL:
        return output

    text = f"{input.subject}\n{input.body}".lower()
    has_keyword = any(keyword in text for keyword in _CRITICAL_KEYWORDS)
    very_negative = output.sentiment < -0.3
    if has_keyword or very_negative:
        return output

    log.info("clamping urgency critical to high for %s", input.customer_id)
    return output.model_copy(update={"urgency": Urgency.HIGH})


@ai_activity.defn(
    name="route_ticket",
    prompt=PromptRef("triage-langfuse-route"),
    output=RoutingDecision,
)
def route_ticket(input: Classification, output: RoutingDecision) -> RoutingDecision:
    cfg = domain.load_config()
    if output.team not in cfg.teams:
        raise ValueError(
            f"route_ticket returned unknown team {output.team!r}; known teams: {sorted(cfg.teams)}"
        )

    after_hours = not domain.is_within_business_hours(_CLOCK(), cfg)
    urgent = input.urgency in (Urgency.HIGH, Urgency.CRITICAL)
    if after_hours and urgent:
        return output.model_copy(
            update={
                "team": "oncall-engineer",
                "on_call_engineer": cfg.on_call_primary,
                "escalated": True,
                "sla_hours": 1 if input.urgency is Urgency.CRITICAL else 4,
            }
        )
    return output


@ai_activity.defn(
    name="draft_response",
    prompt=PromptRef("triage-langfuse-draft"),
    output=DraftReply,
)
def draft_response(input: RoutingDecision, output: DraftReply) -> DraftReply:
    redacted_body, count = domain.redact_pii(output.body)
    return output.model_copy(
        update={
            "body": redacted_body + domain.COMPLIANCE_FOOTER,
            "redaction_count": count,
            "escalated": input.escalated,
        }
    )


@ai_activity.defn(
    name="package_for_review",
    prompt=PromptRef("triage-langfuse-package"),
    output=ReviewPacket,
)
def package_for_review(input: DraftReply, output: ReviewPacket) -> ReviewPacket:
    approval_required = input.redaction_count > 0 or input.escalated
    return output.model_copy(update={"approval_required": approval_required})


ALL_ACTIVITIES = (classify_ticket, route_ticket, draft_response, package_for_review)
