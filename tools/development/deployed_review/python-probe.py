"""Read actual native history/result and replay against the installed Python candidate."""

import asyncio
import json
import sys
from collections import Counter

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from typeflux.yaml import collect_activities, create_workflow, load_yaml_spec
from typeflux.yaml.workflow import create_yaml_workflow_runner


async def main():
    action, execution_id, run_id = sys.argv[1:4]
    client = await Client.connect("temporal:7233", data_converter=pydantic_data_converter)
    handle = client.get_workflow_handle(
        execution_id, run_id=None if run_id == "current" else run_id
    )
    description = await handle.describe()
    history = await handle.fetch_history()
    counts = Counter(event.event_type for event in history.events)
    from temporalio.api.enums.v1 import EventType

    record = {
        "runId": description.run_id,
        "status": description.status.name,
        "eventCounts": {EventType.Name(k): v for k, v in sorted(counts.items())},
        "historyEvents": len(history.events),
    }
    if action == "replay":
        spec = load_yaml_spec("workflow.yaml")
        workflow_class = create_workflow(spec, collect_activities(spec))
        await Replayer(
            workflows=[workflow_class],
            workflow_runner=create_yaml_workflow_runner(),
            data_converter=pydantic_data_converter,
        ).replay_workflow(history)
        record["replayPassed"] = True
    if description.status.name == "COMPLETED":
        result = await handle.result()
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        assert result == {"value": "1"}, "unexpected validated candidate result"
        record["validatedResult"] = result
    print(json.dumps(record))


asyncio.run(asyncio.wait_for(main(), timeout=30))
