"""The claim-assessment AI activity + its prompt (#396).

Separate from ``workflow.py`` on purpose: this module imports ``typeflux``,
which the workflow determinism sandbox must NOT re-import. The worker registers the
wrapped activity (``build_temporal_activity``); the workflow only names it.
"""

from __future__ import annotations

from examples.child_workflow_composition.schemas import Claim, Verdict
from typeflux import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.prompts import InlinePromptRegistry

#: The activity name the child workflow calls via ``workflow.execute_activity``.
ASSESS_ACTIVITY_NAME = "assess_claim"

ASSESS_PROMPT = PromptRef("cwc/assess-claim")

assess_claim_activity = AIActivity(
    name=ASSESS_ACTIVITY_NAME,
    input_type=Claim,
    output_type=Verdict,
    prompt_ref=ASSESS_PROMPT,
)


def inline_registry() -> InlinePromptRegistry:
    """A representative assessment prompt (a real one would live in LangSmith)."""

    system = (
        "You assess whether a promotional claim is substantiated. Return only the "
        "structured verdict."
    )
    user = "Claim {{ claim_id }}: {{ text }}"
    return InlinePromptRegistry(
        {
            "cwc/assess-claim": ResolvedPrompt(
                ref=ASSESS_PROMPT,
                messages=(
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ),
                resolved_version="v1",
                model="cwc-assess-model",
            ),
        }
    )
