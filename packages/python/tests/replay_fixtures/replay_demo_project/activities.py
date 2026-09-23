from replay_demo_project.schemas import InputModel, MiddleModel, OutputModel
from typeflux.core import AIActivity, PromptRef

first = AIActivity(
    name="first",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("first"),
)
second = AIActivity(
    name="second",
    input_type=MiddleModel,
    output_type=OutputModel,
    prompt_ref=PromptRef("second"),
)

ALL_ACTIVITIES = (first, second)
