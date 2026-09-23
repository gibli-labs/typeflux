"""Activities for the compensation replay fixture (#299): a saga whose second step
fails, unwinding the first step's compensation. ``charge`` retries at most once so the
recorded history fails fast."""

from temporalio.common import RetryPolicy

from replay_demo_project.schemas import FailModel, InputModel, MiddleModel
from typeflux.core import AIActivity, PromptRef

book = AIActivity(
    name="book",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("book"),
)
cancel_book = AIActivity(
    name="cancel_book",
    input_type=MiddleModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("cancel_book"),
)
charge = AIActivity(
    name="charge",
    input_type=MiddleModel,
    output_type=FailModel,
    prompt_ref=PromptRef("charge"),
    # Fail fast (no default 5-attempt retry) so the recorded history stays small.
    retry_policy=RetryPolicy(maximum_attempts=1),
)

ALL_ACTIVITIES = (book, cancel_book, charge)
