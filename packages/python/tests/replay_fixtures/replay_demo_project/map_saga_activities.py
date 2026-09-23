"""Activities for the live map-saga (#299 review): a map fan-out whose second item fails,
so the FIRST item's compensation must still run. ``process_item`` fails fast (1 attempt)."""

from temporalio.common import RetryPolicy

from replay_demo_project.schemas import InputModel, MiddleModel
from typeflux.core import AIActivity, PromptRef

process_item = AIActivity(
    name="process_item",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("process"),
    retry_policy=RetryPolicy(maximum_attempts=1),
)
undo_item = AIActivity(
    name="undo_item",
    input_type=MiddleModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("undo"),
)

ALL_ACTIVITIES = (process_item, undo_item)
