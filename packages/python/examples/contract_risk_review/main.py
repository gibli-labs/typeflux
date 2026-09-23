from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from examples.contract_risk_review.schemas import ContractAnalysisInput, ContractRiskReview
from typeflux.core import AIActivity, ChatMessage, PromptRef
from typeflux.env import load_env
from typeflux.manifests import schema_hash
from typeflux.yaml import build_runtime, load_yaml_spec

HERE = Path(__file__).resolve().parent
YAML_PATH = HERE / "typeflux.yaml"
LANGSMITH_YAML_PATH = HERE / "typeflux.langsmith.yaml"
DEFAULT_CONTRACT_PATH = HERE / "fixtures" / "sample-contract.pdf"
PIPELINE_NAME = "ContractRiskReview"
# Prompt name pulled from LangSmith by the langsmith-registry variant
# (typeflux.langsmith.yaml); must match the activity's prompt.name there.
LANGSMITH_PROMPT_NAME = "typeflux-contract-risk-review"

SYSTEM_PROMPT = (
    "You are a contract review assistant for intake triage. Extract entities, important "
    "clauses, obligations, monetary terms, dates, and operational risks from the attached PDF. "
    "This is not legal advice. Ground findings in the contract and avoid inventing facts when "
    "the PDF is silent."
)
USER_PROMPT = """Engagement ID: {{ engagement_id }}
Business context: {{ business_context }}
Review objective: {{ review_objective }}

Build a structured contract risk review. Prefer concise
evidence quotes for every material party, clause, and risk.
Mark ambiguous or missing terms explicitly.
For every risk item, populate severity, category, issue, why_it_matters,
recommended_action, owner, and evidence.
"""
ANALYZE_CONTRACT_ACTIVITY = AIActivity(
    name="analyze_contract",
    input_type=ContractAnalysisInput,
    output_type=ContractRiskReview,
    prompt_ref=PromptRef("analyze_contract", prompt_type="chat"),
)


def sample_input(contract_path: Path) -> ContractAnalysisInput:
    return ContractAnalysisInput(
        engagement_id="contract-risk-review-live",
        business_context=(
            "Initial commercial/legal intake before routing a vendor or customer contract "
            "to specialist reviewers."
        ),
        review_objective=(
            "Extract parties, dates, monetary terms, material clauses, missing information, "
            "and practical risks requiring follow-up."
        ),
        contract_files=[str(contract_path)],
    )


def chat_prompt_messages() -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=USER_PROMPT),
    )


def chat_prompt_payload() -> list[dict[str, object]]:
    return [
        {"role": message.role, "content": message.content} for message in chat_prompt_messages()
    ]


def bootstrap_langfuse() -> int:
    load_env()
    _require_env("LANGFUSE_PUBLIC_KEY")
    _require_env("LANGFUSE_SECRET_KEY")
    client = _langfuse_client()
    label = os.getenv("LANGFUSE_PROMPT_LABEL", "production")
    created = client.create_prompt(
        name=ANALYZE_CONTRACT_ACTIVITY.prompt_ref.name,
        prompt=chat_prompt_payload(),
        labels=[label],
        tags=[
            "typeflux",
            "typeflux",
            "contract-risk-review",
            "chat-prompt",
            "artifact-group",
            "pdf-artifact",
            "mustache",
        ],
        type="chat",
        config=_prompt_config(ANALYZE_CONTRACT_ACTIVITY),
        commit_message="Bootstrap from typeflux contract_risk_review example",
    )
    version = str(created.version) if getattr(created, "version", None) is not None else None
    print(f"pushed analyze_contract ({ContractRiskReview.__name__}) -> {version}")
    return 0


def bootstrap_langsmith() -> int:
    """Push the contract chat prompt to LangSmith Prompt Hub.

    Stored in mustache form (the example's own ``{{ var }}`` templates), so the
    LangSmith registry passes the template through unchanged. Provider model and
    inference params stay in Typeflux YAML, exactly like the Langfuse path.

    Assigns the ``production`` commit tag — LangSmith's moveable label — so the
    YAML's ``registry.label: production`` resolves it, mirroring Langfuse labels.
    Re-running moves the tag to the new commit.
    """
    load_env()
    _require_env("LANGSMITH_API_KEY")
    try:
        from langchain_core.prompts import ChatPromptTemplate
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "langchain-core is required to push prompts to LangSmith. Run with "
            "`uv run --with langchain-core python -m examples.contract_risk_review.main "
            "--bootstrap-langsmith`."
        ) from exc
    from langsmith import Client
    from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError

    # The bootstrap always publishes the `production` tag (the moveable label the
    # YAML resolves by default). It is intentionally NOT keyed off
    # LANGSMITH_PROMPT_TAG: that var is a *resolution* override (a tag OR a commit
    # hash), and creating a commit tag literally named after a hash would be
    # nonsensical.
    label = "production"
    template = ChatPromptTemplate.from_messages(
        [("system", SYSTEM_PROMPT), ("user", USER_PROMPT)],
        template_format="mustache",
    )
    client = Client()
    owner_name = f"-/{LANGSMITH_PROMPT_NAME}"

    # Publish the prompt: a new commit when the text changed, a no-op otherwise.
    try:
        client.push_prompt(LANGSMITH_PROMPT_NAME, object=template)
    except LangSmithConflictError as exc:
        if "Nothing to commit" not in str(exc):
            raise  # a real conflict, not the unchanged-content no-op.

    # Move the moveable label to the latest commit — LangSmith's analogue of a
    # Langfuse label. `push_prompt(commit_tags=...)` only tags brand-new commits
    # and 409s on an existing tag, so set it explicitly: drop the tag (only when
    # it doesn't exist do we ignore the error — auth/network failures surface),
    # then re-create it on the latest commit. These use internal endpoints —
    # fine for this dev-only bootstrap; the registry resolves tags through the
    # public pull API.
    latest = next(iter(client.list_prompt_commits(LANGSMITH_PROMPT_NAME, limit=1)))
    try:
        client.request_with_retries("DELETE", f"/repos/-/{LANGSMITH_PROMPT_NAME}/tags/{label}")
    except LangSmithNotFoundError:
        pass  # tag didn't exist yet — nothing to move.
    client._create_commit_tags(owner_name, str(latest.id), label)
    print(f"published {LANGSMITH_PROMPT_NAME}; tag {label!r} -> commit {latest.commit_hash[:12]}")
    return 0


async def run_live(
    *,
    contract_path: Path,
    workflow_id: str | None,
    config_path: Path,
    task_queue: str | None = None,
) -> ContractRiskReview:
    load_env()
    contract_path = contract_path.expanduser().resolve()
    if not contract_path.is_file():
        raise FileNotFoundError(f"contract PDF not found: {contract_path}")
    os.environ.setdefault("TYPEFLUX_CONTRACT_ARTIFACT_ROOT", str(contract_path.parent))
    os.environ.setdefault("TYPEFLUX_CONTRACT_PROMPT_REGISTRY", "langfuse")
    if task_queue:
        os.environ["TEMPORAL_TASK_QUEUE"] = task_queue
    spec = load_yaml_spec(config_path)
    _require_observability_env(spec.runtime.observability.type)
    _require_provider_env(spec.runtime.provider.type)

    runtime = await build_runtime(spec)
    resolved_workflow_id = workflow_id or f"contract-risk-review-{uuid4().hex[:8]}"
    try:
        async with runtime.worker.build_worker():
            result = await runtime.execute_workflow(
                sample_input(contract_path),
                id=resolved_workflow_id,
                result_type=ContractRiskReview,
                tags=(
                    "typeflux",
                    "contract-risk-review",
                    "multimodal",
                    "pdf-artifact",
                ),
                metadata={
                    "typeflux.example": "contract_risk_review",
                    "typeflux.pipeline": PIPELINE_NAME,
                    "contract.file_name": contract_path.name,
                    "temporal.workflow_id": resolved_workflow_id,
                    "temporal.task_queue": runtime.spec.task_queue,
                },
            )
        print(json.dumps({"workflow_id": resolved_workflow_id}, indent=2, sort_keys=True))
        return result
    finally:
        if runtime.langfuse_client is not None:
            runtime.langfuse_client.flush()
        runtime.observability.writer.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the contract risk review example against Temporal and Langfuse."
    )
    parser.add_argument(
        "--bootstrap-langfuse",
        action="store_true",
        help="Push the contract chat prompt to Langfuse Prompt Management.",
    )
    parser.add_argument(
        "--bootstrap-langsmith",
        action="store_true",
        help="Push the contract chat prompt to LangSmith Prompt Hub (mustache).",
    )
    parser.add_argument(
        "--contract",
        default=str(DEFAULT_CONTRACT_PATH),
        help="Local PDF path visible to the worker. Defaults to the checked-in sample contract.",
    )
    parser.add_argument(
        "--config",
        default=str(YAML_PATH),
        help="Typeflux YAML config to run. Defaults to the OpenAI contract config.",
    )
    parser.add_argument(
        "--task-queue",
        help=(
            "Temporal task queue for this run. Use a queue only your local worker is polling "
            "when passing local-path artifacts."
        ),
    )
    parser.add_argument("--workflow-id", help="Temporal workflow ID. Defaults to a unique ID.")
    args = parser.parse_args(argv)

    if args.bootstrap_langfuse:
        return bootstrap_langfuse()

    if args.bootstrap_langsmith:
        return bootstrap_langsmith()

    result = asyncio.run(
        run_live(
            contract_path=Path(args.contract),
            workflow_id=args.workflow_id,
            config_path=Path(args.config),
            task_queue=args.task_queue,
        )
    )
    print(result.model_dump_json(indent=2))
    return 0


def _require_env(name: str) -> None:
    if not os.getenv(name):
        raise RuntimeError(f"{name} must be set")


def _require_provider_env(provider_type: str) -> None:
    if provider_type == "openai":
        _require_env("OPENAI_API_KEY")
    elif provider_type == "anthropic":
        _require_env("ANTHROPIC_API_KEY")


def _require_observability_env(observability_type: str | None) -> None:
    if observability_type == "langfuse":
        _require_env("LANGFUSE_PUBLIC_KEY")
        _require_env("LANGFUSE_SECRET_KEY")
    elif observability_type == "langsmith":
        _require_env("LANGSMITH_API_KEY")


def _prompt_config(activity: AIActivity) -> dict:
    return {
        "config_version": "1.0",
        "runtime_reads_config": True,
        "template_format": "mustache",
        "provider_params": {
            "temperature": 0,
        },
        "typeflux": {
            "runtime_reads_config": True,
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
                "type": "chat",
            },
            "artifact_inputs": {
                "contracts": {
                    "kind": "document",
                    "media_types": ["application/pdf"],
                    "max_count": 3,
                    "max_bytes": 20971520,
                    "attach": {
                        "role": "user",
                        "text": "Contract PDFs:",
                    },
                },
            },
            "provider_hint": {
                "name": "configured-by-typeflux-yaml",
            },
        },
    }


def _langfuse_client():
    try:
        from langfuse import Langfuse
    except ModuleNotFoundError as exc:
        raise RuntimeError("langfuse is required to bootstrap contract prompts") from exc
    return Langfuse(host=os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL"))


if __name__ == "__main__":
    raise SystemExit(main())
