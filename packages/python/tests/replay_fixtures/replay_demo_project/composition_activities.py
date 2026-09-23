"""Activities for the composition fixture (composition.yaml, #55 slice 2).

Kept in a separate module so the other fixtures collect exactly the activities
they always did — adding coverage here never churns their recorded histories.
"""

from replay_demo_project.schemas import FanoutModel, InputModel, MiddleModel, OutputModel
from typeflux.core import AIActivity, PromptRef

screen_item = AIActivity(
    name="screen_item",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("screen_item"),
)
plain_item = AIActivity(
    name="plain_item",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("plain_item"),
)
merge_fanout = AIActivity(
    name="merge_fanout",
    input_type=FanoutModel,
    output_type=OutputModel,
    prompt_ref=PromptRef("merge_fanout"),
)
escalate_result = AIActivity(
    name="escalate_result",
    input_type=OutputModel,
    output_type=OutputModel,
    prompt_ref=PromptRef("escalate_result"),
)

ALL_ACTIVITIES = (screen_item, plain_item, merge_fanout, escalate_result)
