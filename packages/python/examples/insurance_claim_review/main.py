from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from examples.insurance_claim_review.activities import ALL_ACTIVITIES
from examples.insurance_claim_review.fakes import FakeProvider
from examples.insurance_claim_review.pipeline import inline_registry, run_offline_pipeline
from examples.insurance_claim_review.schemas import (
    ClaimInput,
    ClaimReviewPacket,
    EvidenceItem,
)
from typeflux.core import AIActivity
from typeflux.env import load_env
from typeflux.manifests import schema_hash
from typeflux.prompts import LangfusePromptRegistry
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
PROMPT_DIR = HERE / "prompts"
YAML_PATH = HERE / "typeflux.yaml"

PROMPTS: dict[str, AIActivity] = {
    "review_evidence.txt": ALL_ACTIVITIES[0],
    "consolidate_claim.txt": ALL_ACTIVITIES[1],
}

log = logging.getLogger("typeflux.insurance_claim_review")


def sample_claim() -> ClaimInput:
    received_at = datetime.now(UTC)
    claim_id = "CLM-2026-0042"
    return ClaimInput(
        claim_id=claim_id,
        policy_id="POL-AUTO-7788",
        claimant_name="Jordan Lee",
        loss_description=(
            "Rear-end collision after heavy rain. Claimant reports bumper damage, "
            "trunk alignment issues, and towing from the scene."
        ),
        loss_date=datetime(2026, 5, 21, 14, 30, tzinfo=UTC),
        evidence=[
            EvidenceItem(
                claim_id=claim_id,
                evidence_id="EV-001",
                kind="photo",
                source="mobile upload",
                received_at=received_at,
                content=(
                    "Photo note: rear bumper dent and cracked tail light. "
                    "Uploaded by jordan.lee@example.com."
                ),
            ),
            EvidenceItem(
                claim_id=claim_id,
                evidence_id="EV-002",
                kind="invoice",
                source="Northside Auto Body",
                received_at=received_at,
                content=(
                    "Invoice INV-8842 for bumper replacement, paint, and labor. "
                    "Payment card listed as 4242 4242 4242 4242."
                ),
            ),
            EvidenceItem(
                claim_id=claim_id,
                evidence_id="EV-003",
                kind="email",
                source="claimant email",
                received_at=received_at,
                content=(
                    "Please call me at 555-123-4567. The shop also sent me "
                    "another copy of invoice INV-8842 and I am not sure if it was paid twice."
                ),
            ),
            EvidenceItem(
                claim_id=claim_id,
                evidence_id="EV-004",
                kind="adjuster_note",
                source="field adjuster",
                received_at=received_at,
                content=(
                    "Vehicle inspected. Damage is consistent with low-speed rear impact. "
                    "Need final repair estimate before payment authorization."
                ),
            ),
        ],
    )


def bootstrap_langfuse() -> int:
    load_env()
    _require_langfuse_env()
    registry = LangfusePromptRegistry(host=_langfuse_host())
    label = os.getenv("LANGFUSE_PROMPT_LABEL", "production")
    model = os.getenv("TYPEFLUX_OPENAI_MODEL", "gpt-4o-mini")

    for filename, activity in PROMPTS.items():
        version = registry.create_prompt(
            name=activity.prompt_ref.name,
            prompt=(PROMPT_DIR / filename).read_text(encoding="utf-8"),
            label=label,
            tags=["typeflux", "typeflux", "insurance-claim-review", "mustache"],
            config=_prompt_config(activity, model=model),
            commit_message="Bootstrap from typeflux insurance_claim_review example",
        )
        print(f"pushed {activity.prompt_ref.name} ({activity.output_type.__name__}) -> {version}")
    return 0


async def run_live() -> ClaimReviewPacket:
    load_env()
    _require_langfuse_env()
    _require_openai_env()
    workflow_id = os.getenv("TEMPORAL_WORKFLOW_ID", f"insurance-claim-review-{uuid4().hex[:8]}")
    runtime = await build_runtime(load_yaml_spec(YAML_PATH))
    try:
        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                sample_claim(),
                id=workflow_id,
                result_type=ClaimReviewPacket,
                tags=["typeflux", "insurance-claim-review", "fan-out"],
                metadata={
                    "typeflux.pipeline": "InsuranceClaimEvidenceReview",
                    "typeflux.example": "insurance_claim_review",
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


def run_offline() -> ClaimReviewPacket:
    load_env()
    return run_offline_pipeline(
        sample_claim(),
        registry=inline_registry(),
        provider=FakeProvider(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Insurance claim evidence review demo.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Use local prompts and FakeProvider.")
    mode.add_argument("--bootstrap-langfuse", action="store_true", help="Push prompts to Langfuse.")
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
                "name": "InsuranceClaimEvidenceReview",
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
