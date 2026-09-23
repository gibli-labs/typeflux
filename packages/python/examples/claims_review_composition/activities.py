"""Code-injected activities for the parent workflow (the YAML+code authoring mode).

The parent ``claims_review.yaml`` declares ``activities.modules: [activities]`` instead
of inline ``activities.definitions``, so these ``AIActivity`` objects are imported and
composed with the graph features (parallel / when / sub-workflows / gates). The
sub-workflow children (``claim_triage`` / ``escalation_review``) stay pure-YAML — one
project exercises BOTH authoring modes composed together (#55 scope addition A).
"""

from __future__ import annotations

from examples.claims_review_composition.schemas import (
    Acknowledgement,
    ClaimBatch,
    Consolidated,
    IntakeFanout,
    ReviewPacket,
)
from typeflux.core import AIActivity, PromptRef

acknowledge = AIActivity(
    name="acknowledge",
    input_type=ClaimBatch,
    output_type=Acknowledgement,
    prompt_ref=PromptRef("acknowledge-intake"),
)
consolidate = AIActivity(
    name="consolidate",
    input_type=IntakeFanout,
    output_type=Consolidated,
    prompt_ref=PromptRef("consolidate-intake"),
)
finalize = AIActivity(
    name="finalize",
    input_type=Consolidated,
    output_type=ReviewPacket,
    prompt_ref=PromptRef("finalize-review"),
)

ALL_ACTIVITIES = (acknowledge, consolidate, finalize)
