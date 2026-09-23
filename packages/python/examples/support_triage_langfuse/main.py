from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.support_triage_langfuse.activities import ALL_ACTIVITIES
from examples.support_triage_langfuse.fakes import FakeProvider
from examples.support_triage_langfuse.pipeline import PIPELINE_NAME, run
from examples.support_triage_langfuse.schemas import ReviewPacket, TicketInput
from examples.support_triage_langfuse.workflow import SupportTriageLangfuseWorkflow
from typeflux.core import AIActivity, ChatMessage, PromptRef, ResolvedPrompt
from typeflux.env import load_env
from typeflux.execution import build_temporal_activity
from typeflux.execution import execute_workflow as execute_typeflux_workflow
from typeflux.manifests import schema_hash
from typeflux.metadata import TemporalConnectionContributor
from typeflux.observability.semantic import (
    LangfuseAIActivityObserver,
    configure_temporal_langfuse_tracing,
)
from typeflux.prompts import InlinePromptRegistry, LangfusePromptRegistry
from typeflux.providers import OpenAIProvider

HERE = Path(__file__).resolve().parent
PROMPT_DIR = HERE / "prompts"

PROMPTS: dict[str, AIActivity] = {
    "classify.txt": ALL_ACTIVITIES[0],
    "route.txt": ALL_ACTIVITIES[1],
    "draft.txt": ALL_ACTIVITIES[2],
    "package.txt": ALL_ACTIVITIES[3],
}

log = logging.getLogger("typeflux.triage.langfuse.main")


def inline_registry() -> InlinePromptRegistry:
    prompts: dict[PromptRef, ResolvedPrompt] = {}
    for filename, activity in PROMPTS.items():
        ref = activity.prompt_ref
        prompts[ref] = ResolvedPrompt(
            ref=ref,
            messages=(ChatMessage(role="user", content=(PROMPT_DIR / filename).read_text()),),
            resolved_version="local",
            model=os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0,
        )
    return InlinePromptRegistry(prompts)


def langfuse_registry() -> LangfusePromptRegistry:
    return LangfusePromptRegistry(host=_langfuse_host())


def bootstrap_langfuse() -> int:
    load_env()
    _require_langfuse_env()
    registry = langfuse_registry()
    label = os.getenv("LANGFUSE_PROMPT_LABEL", "production")
    model = os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini")

    for filename, activity in PROMPTS.items():
        version = registry.create_prompt(
            name=activity.prompt_ref.name,
            prompt=(PROMPT_DIR / filename).read_text(encoding="utf-8"),
            label=label,
            tags=["typeflux", "typeflux", "support-triage", "mustache"],
            config=_prompt_config(activity, model=model),
            commit_message="Bootstrap from typeflux support_triage_langfuse example",
        )
        print(
            f"pushed {activity.prompt_ref.name} ({activity.output_type.__name__}) -> version {version}"
        )
    return 0


def _prompt_config(activity: AIActivity, *, model: str) -> dict:
    return {
        "config_version": "1.0",
        "runtime_reads_config": False,
        "template_format": "mustache",
        "model": model,
        "temperature": 0,
        "typeflux": {
            "runtime_reads_config": False,
            "pipeline": {
                "name": PIPELINE_NAME,
                "activity_name": activity.name,
                "prompt_ref": activity.prompt_ref.name,
                "prompt_ref_version": activity.prompt_ref.version,
            },
            "contracts": {
                "input_model": activity.input_type.__name__,
                "input_schema_hash": schema_hash(activity.input_type),
                "output_model": activity.output_type.__name__,
                "output_schema_hash": schema_hash(activity.output_type),
            },
            "prompt": {
                "template_format": "mustache",
                "type": "text",
            },
            "provider_hint": {
                "name": "openai",
                "model": model,
            },
        },
    }


def sample_ticket() -> TicketInput:
    return TicketInput(
        customer_id="cust-1042",
        subject="Charged twice for the same invoice",
        body=(
            "Hi, your system billed me twice this month for the same invoice "
            "and I am worried this will affect my credit card. "
            "Can someone look into this urgently?\n\n"
            "You can reach me at jane.doe@example.com or 555-123-4567."
        ),
        received_at=datetime.now(UTC),
    )


async def run_temporal_live() -> ReviewPacket:
    load_env()
    _require_langfuse_env()
    _require_openai_env()
    model = os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "support-triage-langfuse-typeflux")
    workflow_id = os.getenv("TEMPORAL_WORKFLOW_ID", f"support-triage-langfuse-{uuid4()}")
    plugin, langfuse_client = _configure_temporal_langfuse_tracing()

    registry = langfuse_registry()
    provider = OpenAIProvider(default_model=model, enable_langfuse=True)
    observer = LangfuseAIActivityObserver(client=langfuse_client)

    async def run_with_client(client: Client) -> ReviewPacket:
        activities = [
            build_temporal_activity(
                activity, registry=registry, provider=provider, observer=observer
            )
            for activity in ALL_ACTIVITIES
        ]
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SupportTriageLangfuseWorkflow],
            activities=activities,
        ):
            return await _execute_support_triage_workflow(
                client,
                workflow_id=workflow_id,
                task_queue=task_queue,
                langfuse_client=langfuse_client,
            )

    address = os.getenv("TEMPORAL_ADDRESS")
    if address:
        client = await Client.connect(
            address,
            namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
            tls=os.getenv("TEMPORAL_TLS", "").lower() in {"1", "true", "yes"},
            api_key=os.getenv("TEMPORAL_API_KEY") or None,
            data_converter=pydantic_data_converter,
            plugins=[plugin],
        )
        try:
            result = await run_with_client(client)
            log.info(
                "Temporal workflow completed: workflow_id=%s task_queue=%s", workflow_id, task_queue
            )
            return result
        finally:
            langfuse_client.flush()

    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        plugins=[plugin],
        dev_server_log_level="warn",
    )
    try:
        result = await run_with_client(env.client)
        log.info(
            "Temporal workflow completed: workflow_id=%s task_queue=%s", workflow_id, task_queue
        )
        return result
    finally:
        langfuse_client.flush()
        await env.shutdown()


def run_offline() -> ReviewPacket:
    load_env()
    return run(
        sample_ticket(),
        registry=inline_registry(),
        provider=FakeProvider(),
    )


async def _execute_support_triage_workflow(
    client: Client,
    *,
    workflow_id: str,
    task_queue: str,
    langfuse_client: Any,
) -> ReviewPacket:
    return await execute_typeflux_workflow(
        client=client,
        workflow=SupportTriageLangfuseWorkflow.run,
        input_value=sample_ticket(),
        id=workflow_id,
        task_queue=task_queue,
        result_type=ReviewPacket,
        langfuse_client=langfuse_client,
        workflow_name="SupportTriageYamlWorkflow",  # match YAML so console traces link precisely
        tags=["typeflux", "support-triage", "temporal"],
        metadata={
            "typeflux.pipeline": PIPELINE_NAME,
            "typeflux.activities": ",".join(activity.name for activity in ALL_ACTIVITIES),
            "temporal.workflow_id": workflow_id,
            "temporal.task_queue": task_queue,
        },
        metadata_contributors=(TemporalConnectionContributor.from_env(),),
        activities=ALL_ACTIVITIES,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Typeflux Temporal Langfuse support-triage demo.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Use local prompts and FakeProvider.")
    mode.add_argument("--bootstrap-langfuse", action="store_true", help="Push prompts to Langfuse.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.bootstrap_langfuse:
        return bootstrap_langfuse()

    packet = run_offline() if args.offline else asyncio.run(run_temporal_live())
    print(packet.model_dump_json(indent=2))
    if not args.offline:
        log.info("Langfuse trace exported for workflow=%s", PIPELINE_NAME)
    return 0


def _require_openai_env() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY must be set")


def _require_langfuse_env() -> None:
    missing = [
        name for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY") if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"missing Langfuse environment variables: {', '.join(missing)}")


def _langfuse_host() -> str | None:
    return os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL")


def _configure_temporal_langfuse_tracing():
    return configure_temporal_langfuse_tracing(
        host=_langfuse_host(),
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        export_all_temporal_spans=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
