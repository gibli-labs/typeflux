"""Credential-free application fixture; logs only fixed operation names and test IDs."""

import json
import time
from pathlib import Path

from temporalio import activity

from typeflux.providers.errors import ProviderTransientError
from typeflux.testing import FakeProvider


class DeployedProvider(FakeProvider):
    def __init__(self, *, config):
        super().__init__([])
        self.mode = config["mode"]

    async def async_structured_call(self, *, output_schema, **kwargs):
        info = activity.info()
        with Path("/data/effects.jsonl").open("a") as output:
            output.write(
                json.dumps(
                    {
                        "activity": info.activity_type,
                        "attempt": info.attempt,
                        "runId": info.workflow_run_id,
                        "timeMs": time.time_ns() // 1000000,
                    }
                )
                + "\n"
            )
        if self.mode == "retry" and info.activity_type == "assess_item" and info.attempt == 1:
            raise ProviderTransientError("fixed deployment test fault")
        return output_schema.model_validate({"value": "1", "reviews": [{"value": "1"}]})
