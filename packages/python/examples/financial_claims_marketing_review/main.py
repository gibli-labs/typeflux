from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from examples.financial_claims_marketing_review.activities import ALL_ACTIVITIES
from examples.financial_claims_marketing_review.fakes import FakeProvider
from examples.financial_claims_marketing_review.pipeline import (
    inline_registry,
    run_offline_pipeline,
)
from examples.financial_claims_marketing_review.schemas import (
    MarketingClaim,
    MarketingReviewPacket,
    MarketingSubmission,
)
from typeflux.core import AIActivity, ChatMessage
from typeflux.env import load_env
from typeflux.manifests import schema_hash
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
PROMPT_DIR = HERE / "prompts"
YAML_PATH = HERE / "typeflux.yaml"

log = logging.getLogger("typeflux.financial_claims_marketing_review")


@dataclass(frozen=True)
class ChatPromptPart:
    role: Literal["system", "user", "assistant"]
    filename: str

    @property
    def content(self) -> str:
        return (PROMPT_DIR / self.filename).read_text(encoding="utf-8")


@dataclass(frozen=True)
class ChatPromptDefinition:
    activity: AIActivity
    messages: tuple[ChatPromptPart, ...]


CHAT_PROMPTS: dict[str, ChatPromptDefinition] = {
    "review_claim": ChatPromptDefinition(
        activity=ALL_ACTIVITIES[0],
        messages=(
            ChatPromptPart(role="system", filename="review_claim_system.txt"),
            ChatPromptPart(role="user", filename="review_claim_user.txt"),
        ),
    ),
    "consolidate_review": ChatPromptDefinition(
        activity=ALL_ACTIVITIES[1],
        messages=(
            ChatPromptPart(role="system", filename="consolidate_review_system.txt"),
            ChatPromptPart(role="user", filename="consolidate_review_user.txt"),
        ),
    ),
}


def sample_submission() -> MarketingSubmission:
    campaign_id = "CMP-RET-2026-Q3"
    submission_id = "MKT-2026-RET-0042"
    return MarketingSubmission(
        submission_id=submission_id,
        brand="Northstar Wealth",
        reviewer_email="compliance.reviewer@example.com",
        claims=[
            MarketingClaim(
                submission_id=submission_id,
                campaign_id=campaign_id,
                claim_id="CLM-001",
                product_category="investment",
                channel="web",
                jurisdiction="US",
                audience="pre-retirees with investable assets over $250k",
                claim_text="Lock in a guaranteed 12% annual return for retirement.",
                evidence=(
                    "Backtest deck shows an average annualized return of 8.1% over a selected "
                    "five-year period. No guarantee language is approved."
                ),
                contact="maria.chen@example.com",
            ),
            MarketingClaim(
                submission_id=submission_id,
                campaign_id=campaign_id,
                claim_id="CLM-002",
                product_category="retirement",
                channel="email",
                jurisdiction="US",
                audience="retirement newsletter subscribers",
                claim_text="Enjoy risk-free retirement income with our managed portfolio.",
                evidence=(
                    "Portfolio income depends on market performance. Disclosure draft says "
                    "principal is subject to investment risk."
                ),
                contact="marketing-ops@example.com",
            ),
            MarketingClaim(
                submission_id=submission_id,
                campaign_id=campaign_id,
                claim_id="CLM-003",
                product_category="cash_management",
                channel="brochure",
                jurisdiction="US",
                audience="existing banking customers",
                claim_text="No advisory management fees on the cash reserve sleeve.",
                evidence=(
                    "Approved fee schedule states the cash reserve sleeve has no advisory "
                    "management fee. Other account fees may apply."
                ),
                contact="555-867-5309",
            ),
            MarketingClaim(
                submission_id=submission_id,
                campaign_id=campaign_id,
                claim_id="CLM-004",
                product_category="investment",
                channel="advisor_script",
                jurisdiction="US",
                audience="prospective advisory clients",
                claim_text="Approved by regulators for safer long-term investing.",
                evidence=(
                    "Product registration filing is complete. No evidence of regulator approval "
                    "or endorsement is included."
                ),
                contact="legal.review@example.com",
            ),
        ],
    )


def chat_messages(definition: ChatPromptDefinition) -> tuple[ChatMessage, ...]:
    return tuple(ChatMessage(role=part.role, content=part.content) for part in definition.messages)


def chat_prompt_payload(definition: ChatPromptDefinition) -> list[dict[str, str]]:
    return [
        {"role": message.role, "content": message.content} for message in chat_messages(definition)
    ]


def bootstrap_langfuse() -> int:
    load_env()
    _require_langfuse_env()
    client = _langfuse_client()
    label = os.getenv("LANGFUSE_PROMPT_LABEL", "production")
    model = os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini")

    for definition in CHAT_PROMPTS.values():
        activity = definition.activity
        created = client.create_prompt(
            name=activity.prompt_ref.name,
            prompt=chat_prompt_payload(definition),
            labels=[label],
            tags=[
                "typeflux",
                "typeflux",
                "financial-claims-marketing-review",
                "chat-prompt",
                "mustache",
            ],
            type="chat",
            config=_prompt_config(activity, model=model),
            commit_message=("Bootstrap from typeflux financial_claims_marketing_review example"),
        )
        version = str(created.version) if getattr(created, "version", None) is not None else None
        print(f"pushed {activity.prompt_ref.name} ({activity.output_type.__name__}) -> {version}")
    return 0


async def run_live() -> MarketingReviewPacket:
    load_env()
    _require_langfuse_env()
    _require_openai_env()
    workflow_id = os.getenv(
        "TEMPORAL_WORKFLOW_ID", f"financial-claims-marketing-review-{uuid4().hex[:8]}"
    )
    runtime = await build_runtime(load_yaml_spec(YAML_PATH))
    try:
        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                sample_submission(),
                id=workflow_id,
                result_type=MarketingReviewPacket,
                tags=["typeflux", "financial-claims-marketing-review", "chat-prompt"],
                metadata={
                    "typeflux.pipeline": "FinancialClaimsMarketingReview",
                    "typeflux.example": "financial_claims_marketing_review",
                    "temporal.workflow_id": workflow_id,
                    "temporal.task_queue": runtime.spec.task_queue,
                },
            )
        print(f"workflow_id={workflow_id}")
        return result
    finally:
        if runtime.langfuse_client is not None:
            runtime.langfuse_client.flush()
        runtime.observability.writer.shutdown()


def run_offline() -> MarketingReviewPacket:
    load_env()
    return run_offline_pipeline(
        sample_submission(),
        registry=inline_registry(),
        provider=FakeProvider(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Financial claims marketing review demo.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline", action="store_true", help="Use local chat prompts and FakeProvider."
    )
    mode.add_argument(
        "--bootstrap-langfuse", action="store_true", help="Push chat prompts to Langfuse."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.bootstrap_langfuse:
        return bootstrap_langfuse()
    result = run_offline() if args.offline else asyncio.run(run_live())
    print(result.model_dump_json(indent=2))
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
                "name": "FinancialClaimsMarketingReview",
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
                "type": "chat",
            },
            "provider_hint": {
                "name": "openai",
                "model": model,
            },
        },
    }


def _langfuse_client():
    try:
        from langfuse import Langfuse
    except ModuleNotFoundError as exc:
        raise RuntimeError("langfuse is required to bootstrap chat prompts") from exc
    return Langfuse(host=_langfuse_host())


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


if __name__ == "__main__":
    raise SystemExit(main())
