"""A deterministic, offline provider for the live proof (repo convention, cf. the
replay fixtures' ``ReplayFixtureProvider``). It returns a canned instance per output
schema so the composition runs end-to-end on a real Temporal dev server WITHOUT any
vendor API calls — the committed workflow YAML still declares the real env-keyed
provider for out-of-the-box use.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from examples.claims_review_composition.schemas import (
    Acknowledgement,
    Consolidated,
    ReviewPacket,
    Triage,
)
from typeflux import ChatMessage


class ClaimsReviewFakeProvider:
    provider_name = "fake"
    default_model = "fake-claims-review"

    def structured_call(
        self,
        *,
        messages: list[ChatMessage],
        output_schema: type[BaseModel],
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BaseModel:
        del messages, model, temperature, metadata
        if output_schema is Triage:
            return Triage(claim_id="demo", risk="medium")
        if output_schema is Acknowledgement:
            return Acknowledgement(note="acknowledged")
        if output_schema is Consolidated:
            return Consolidated(summary="two claims triaged; escalation advised", escalate=True)
        if output_schema is ReviewPacket:
            return ReviewPacket(decision="approved")
        raise AssertionError(f"unexpected output schema: {output_schema!r}")
