"""Safe in-memory faults for the optional Temporal adoption demonstration only."""

import asyncio

from temporalio import activity

from typeflux.providers.errors import ProviderTransientError
from typeflux.testing import FakeProvider


class RecoveryProvider(FakeProvider):
    def __init__(self, *, config):
        super().__init__([])
        self.mode = config["mode"]
        self.calls = []
        self.first_failure = asyncio.Event()

    async def async_structured_call(self, *, output_schema, **kwargs):
        attempt = activity.info().attempt
        self.calls.append(attempt)
        if self.mode == "retry" and attempt == 1:
            self.first_failure.set()
            raise ProviderTransientError("demonstration fault; no external side effects")
        if self.mode == "timeout":
            await asyncio.sleep(2)
        return output_schema(text="local result")
