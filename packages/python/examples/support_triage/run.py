from __future__ import annotations

import asyncio

from temporalio.client import Client

from examples.support_triage.schemas import Ticket
from examples.support_triage.workflow import SupportTriageWorkflow


async def main() -> None:
    client = await Client.connect("localhost:7233")
    result = await client.execute_workflow(
        SupportTriageWorkflow.run,
        Ticket(
            subject="Invoice question",
            body="Can you explain an unexpected invoice line item?",
            customer_tier="enterprise",
        ),
        id="support-triage-demo",
        task_queue="support-ai",
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
