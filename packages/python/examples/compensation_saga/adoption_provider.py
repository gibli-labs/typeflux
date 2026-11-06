"""Fixed fake faults for the optional Temporal compensation adoption example."""

from temporalio import activity

from examples.compensation_saga.fakes import CompensationSagaProvider
from examples.compensation_saga.schemas import (
    Booking,
    CancelResult,
    ChargeResult,
    Fulfillment,
    RefundResult,
)
from typeflux.providers.errors import ProviderTransientError


class AdoptionProvider(CompensationSagaProvider):
    def __init__(self, *, config):
        super().__init__()
        self.scenario = config["scenario"]
        self.calls = []

    async def async_structured_call(
        self, *, messages, output_schema, model=None, temperature=None, metadata=None
    ):
        action = {
            Booking: "book_room",
            ChargeResult: "charge_card",
            Fulfillment: "fulfill_order",
            RefundResult: "refund_card",
            CancelResult: "cancel_room",
        }[output_schema]
        attempt = activity.info().attempt
        self.calls.append((action, attempt))
        if output_schema is Fulfillment:
            raise ProviderTransientError("demonstration forward failure; fake effects only")
        if output_schema is RefundResult and (
            self.scenario == "partial" or (self.scenario == "replacement" and attempt == 1)
        ):
            raise ProviderTransientError("demonstration compensation failure; fake effects only")
        return super().structured_call(
            messages=messages,
            output_schema=output_schema,
            model=model,
            temperature=temperature,
            metadata=metadata,
        )
