"""Schemas for the claims-review composition example (#55).

The types chain the parent graph end to end: a `parallel` block collects into
``IntakeFanout`` (its fields ARE the branch ids, Optional where the branch is gated —
decision D4), which ``consolidate`` folds into ``Consolidated``; the ``escalation``
sub-workflow keeps that type, and ``finalize`` yields the terminal ``ReviewPacket``.
"""

from __future__ import annotations

from pydantic import BaseModel


class Claim(BaseModel):
    claim_id: str
    text: str


class ClaimBatch(BaseModel):
    """Workflow input: a priority tier plus the claims under review."""

    priority: str
    claims: list[Claim]


class Triage(BaseModel):
    """The ``claim_triage`` sub-workflow's per-claim output (and the map item type)."""

    claim_id: str
    risk: str


class TriageBatch(BaseModel):
    """Collect object of the ``map.workflow`` fan-out — one ``Triage`` per claim."""

    triaged: list[Triage]


class Acknowledgement(BaseModel):
    note: str


class IntakeFanout(BaseModel):
    """Collect object of the ``screen`` parallel block (#55 §3.1): the fields are the
    branch ids ``fast_track`` / ``full_review``, each Optional because both branches
    are ``when``-gated (a gated-out branch contributes ``None``)."""

    fast_track: Acknowledgement | None
    full_review: TriageBatch | None


class Consolidated(BaseModel):
    """The consolidated intake — also the ``escalation_review`` sub-workflow's IO type."""

    summary: str
    escalate: bool


class ReviewPacket(BaseModel):
    decision: str
