"""Activities for the live compensation saga (#299): book_hotel -> book_flight -> charge,
where charge fails and the two bookings' compensations (cancel_hotel / cancel_flight) unwind
in reverse. ``charge`` retries at most once so the failure surfaces fast."""

from temporalio.common import RetryPolicy

from replay_demo_project.schemas import FailModel, InputModel, MiddleModel
from typeflux.core import AIActivity, PromptRef

book_hotel = AIActivity(
    name="book_hotel",
    input_type=InputModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("book_hotel"),
)
book_flight = AIActivity(
    name="book_flight",
    input_type=MiddleModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("book_flight"),
)
charge = AIActivity(
    name="charge",
    input_type=MiddleModel,
    output_type=FailModel,
    prompt_ref=PromptRef("charge"),
    retry_policy=RetryPolicy(maximum_attempts=1),
)
cancel_hotel = AIActivity(
    name="cancel_hotel",
    input_type=MiddleModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("cancel_hotel"),
)
cancel_flight = AIActivity(
    name="cancel_flight",
    input_type=MiddleModel,
    output_type=MiddleModel,
    prompt_ref=PromptRef("cancel_flight"),
    retry_policy=RetryPolicy(maximum_attempts=1),
)

ALL_ACTIVITIES = (book_hotel, book_flight, charge, cancel_hotel, cancel_flight)
