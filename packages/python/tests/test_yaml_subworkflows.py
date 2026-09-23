"""Sub-workflows (#55 slice 3): a ``workflow:`` step / ``map.workflow`` fan-out runs a
sibling PROJECT workflow as a Temporal CHILD workflow.

Covers project resolution (records carry the child's registered type/digest/IO types),
call-spec building, the parent-digest FOLD (moves on a child-graph edit, stable
otherwise), the acyclic reference constraint, the ``_call_payload`` digest arms, the
topology projection's ``workflow`` node, and — through a time-skipping
``WorkflowEnvironment`` — real parent->child plain-step AND map-over-subworkflow
execution with the normative child ids + parent-link memo. The structural tests always
run; the execution test skips when the Temporal test server is unavailable (as
test_yaml.py does).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from textwrap import dedent, indent
from typing import Any
from uuid import uuid4

import pytest

from typeflux.project import load_project_spec
from typeflux.project.bundle import resolve_workflow_bundle
from typeflux.project.environment import (
    ProjectEnvironmentError,
    resolve_project_workflow,
    resolve_subworkflows_for,
)
from typeflux.yaml import collect_activities, create_workflow
from typeflux.yaml.identity import _call_payload
from typeflux.yaml.workflow import (
    MapSubworkflowCallSpec,
    SubworkflowCallSpec,
)

PROJECT = "subworkflow_project"

try:
    from temporalio import workflow as _temporal_workflow

    @_temporal_workflow.defn(name="SubworkflowIdBlocker", sandboxed=False)
    class _SubworkflowIdBlocker:
        """A never-completing workflow occupying a deterministic child id (the
        ALLOW_DUPLICATE still-running collision live test). Module-level because
        temporalio rejects local @workflow.run classes; unsandboxed because a test
        module is not sandbox-importable."""

        @_temporal_workflow.run
        async def run(self) -> None:
            await _temporal_workflow.wait_condition(lambda: False)
except ModuleNotFoundError:  # pragma: no cover - temporalio is a hard test dep.
    _SubworkflowIdBlocker = None  # type: ignore[assignment]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


# ---------------------------------------------------------------------------
# Live-test harness helpers (shared by both @pytest.mark.live tests below)
# ---------------------------------------------------------------------------

#: The parent workflows here configure ``workflow_search_attribute: TypefluxWorkflow``
#: (see ``_parent_plain_yaml``), so the child-start stamps this keyword attribute.
_WORKFLOW_SEARCH_ATTRIBUTE = "TypefluxWorkflow"

#: Hard ceiling on any single live workflow result await. Comfortably above a
#: healthy fake-provider run (<1s) yet bounded so a stuck execution fails LOUD.
_LIVE_WORKFLOW_TIMEOUT_SECONDS = 60.0

#: Ceiling on the one-shot ``AddSearchAttributes`` operator RPC (sub-second when
#: healthy). Without this bound a stalled operator-service endpoint would hang
#: the test BEFORE any of the bounded workflow awaits — the exact hang class
#: this harness exists to eliminate.
_SEARCH_ATTRIBUTE_RPC_TIMEOUT_SECONDS = 15.0

#: Registration outcomes that are fine to proceed past: the attribute already
#: exists (idempotent rerun), or the server does not implement the operator op
#: (the time-skipping test-server fallback tolerates unregistered attributes,
#: so registration is unnecessary there in the first place).
_REGISTRATION_BENIGN_MARKERS = ("already", "unimplemented", "not implemented", "unsupported")


async def _ensure_workflow_search_attribute_registered(client: Any) -> None:
    """Register the ``TypefluxWorkflow`` keyword search attribute the parent stamps
    onto its child (idempotent).

    On a REAL Temporal dev server an unregistered custom attribute makes the
    child-start — and therefore the parent's workflow task — fail with
    ``BadSearchAttributes``; that is a task failure, so the SDK retries it
    FOREVER and ``execute_workflow`` never returns (the historical hang). The
    time-skipping test server tolerated the unregistered attribute, which is why
    this was long latent.

    Failure handling mirrors ``_ensure_subject_attribute_registered`` in
    ``test_live_subject_index.py`` and goes one step further: already-exists and
    unimplemented/unsupported outcomes proceed silently (registration is either
    done or unnecessary), any OTHER error fails fast with the real RPC error —
    swallowing it would leave the 60s workflow timeout to misdiagnose a
    permission or namespace problem as "attribute not registered". The RPC
    itself is bounded so a stalled operator service cannot hang the test here.
    """

    try:
        from temporalio.api.enums.v1 import IndexedValueType
        from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest
    except ModuleNotFoundError:  # pragma: no cover - temporalio is a hard test dep.
        return
    request = AddSearchAttributesRequest(
        namespace=client.namespace,
        search_attributes={_WORKFLOW_SEARCH_ATTRIBUTE: IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD},
    )
    try:
        await asyncio.wait_for(
            client.operator_service.add_search_attributes(request),
            timeout=_SEARCH_ATTRIBUTE_RPC_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        pytest.fail(
            f"AddSearchAttributes RPC (registering {_WORKFLOW_SEARCH_ATTRIBUTE!r} on "
            f"namespace {client.namespace!r}) did not respond within "
            f"{_SEARCH_ATTRIBUTE_RPC_TIMEOUT_SECONDS:.0f}s — the operator-service "
            "endpoint of the Temporal server appears stalled."
        )
    except Exception as exc:  # noqa: BLE001 - benign outcomes proceed, real failures fail fast
        message = str(exc).lower()
        if any(marker in message for marker in _REGISTRATION_BENIGN_MARKERS):
            return
        pytest.fail(
            f"AddSearchAttributes RPC (registering {_WORKFLOW_SEARCH_ATTRIBUTE!r} on "
            f"namespace {client.namespace!r}) failed: {exc!r}. Without this attribute "
            "the parent's child-start retries forever, so failing here with the real "
            "error beats a later timeout misdiagnosed as 'attribute not registered'."
        )


async def _execute_workflow_bounded(
    client: Any, workflow_run: Any, arg: Any, *, what: str, **kwargs: Any
) -> Any:
    """Await a live workflow result under a hard deadline so a stuck execution
    fails with a diagnostic instead of hanging the suite forever.

    A ``BadSearchAttributes`` (or any) workflow-task failure retries indefinitely
    and never surfaces as a result; without this bound the test process hangs
    with no output. Mirrors the deadline discipline the erase-subject live tests
    use for their poll loops. Non-timeout errors (e.g. the terminal
    ``WorkflowFailureError`` the collision test asserts on) propagate unchanged.
    """

    try:
        return await asyncio.wait_for(
            client.execute_workflow(workflow_run, arg, **kwargs),
            timeout=_LIVE_WORKFLOW_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        pytest.fail(
            f"{what} (id={kwargs.get('id')!r}) did not finish within "
            f"{_LIVE_WORKFLOW_TIMEOUT_SECONDS:.0f}s. The live Temporal dev server is "
            "likely rejecting the workflow task (e.g. BadSearchAttributes: the "
            f"{_WORKFLOW_SEARCH_ATTRIBUTE!r} keyword search attribute is not registered "
            "on the namespace — such task failures retry indefinitely and never surface "
            "as a workflow result)."
        )


async def _best_effort_delete_executions(client: Any, workflow_ids: list[str]) -> None:
    """Shared-dev-server hygiene: delete the executions this test started so
    reruns stay clean. Best-effort — a closed execution may already be gone, and
    the time-skipping server may not implement ``DeleteWorkflowExecution``.
    """

    try:
        from temporalio.api.common.v1 import WorkflowExecution as WorkflowExecutionProto
        from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
    except ModuleNotFoundError:  # pragma: no cover - temporalio is a hard test dep.
        return
    for workflow_id in workflow_ids:
        try:
            await client.workflow_service.delete_workflow_execution(
                DeleteWorkflowExecutionRequest(
                    namespace=client.namespace,
                    workflow_execution=WorkflowExecutionProto(workflow_id=workflow_id),
                )
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


_CHILD_YAML = """\
    project: {pkg}
    name: assess_claim
    task_queue: sub-queue
    runtime:
      temporal:
        address: localhost:7233
      registry:
        type: inline
        prompts:
          assess: assess {{{{text}}}}
      provider:
        type: fake
      observability:
        type: none
    activities:
      definitions:
        - name: assess
          input: schemas:Claim
          output: schemas:Verdict
          prompt: assess
    workflow:
      name: AssessClaimWorkflow
      input: schemas:Claim
      output: schemas:Verdict
      steps:
        - id: assess
          activity: assess
"""


def _parent_plain_yaml(pkg: str) -> str:
    return dedent(
        f"""\
        project: {pkg}
        name: review
        task_queue: sub-queue
        runtime:
          temporal:
            address: localhost:7233
            workflow_search_attribute: TypefluxWorkflow
          registry:
            type: inline
            prompts:
              p: p
          provider:
            type: fake
          observability:
            type: none
        activities:
          definitions: []
        workflow:
          name: ReviewWorkflow
          input: schemas:Claim
          output: schemas:Verdict
          steps:
            - id: assess_one
              workflow: child
        """
    )


def _parent_map_yaml(pkg: str) -> str:
    return dedent(
        f"""\
        project: {pkg}
        name: review_batch
        task_queue: sub-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              p: p
          provider:
            type: fake
          observability:
            type: none
        activities:
          definitions: []
        workflow:
          name: ReviewBatchWorkflow
          input: schemas:Batch
          output: schemas:VerdictBatch
          steps:
            - id: fan
              map:
                workflow: child
                over: input.claims
                concurrency: 3
                collect:
                  output: schemas:VerdictBatch
                  field: verdicts
        """
    )


def _setup_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pkg: str = PROJECT) -> Path:
    for name in tuple(sys.modules):
        if name == pkg or name.startswith(f"{pkg}."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    package = tmp_path / pkg
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class Claim(BaseModel):
            claim_id: str
            text: str


        class Verdict(BaseModel):
            claim_id: str
            substantiated: bool


        class Batch(BaseModel):
            claims: list[Claim]


        class VerdictBatch(BaseModel):
            verdicts: list[Verdict]
        """,
    )
    _write(tmp_path / "assess.yaml", _CHILD_YAML.format(pkg=pkg))
    _write(tmp_path / "review.yaml", _parent_plain_yaml(pkg))
    _write(tmp_path / "review_batch.yaml", _parent_map_yaml(pkg))
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        """
        version: "1"
        name: subworkflow-demo
        workflows:
          - id: child
            path: assess.yaml
          - id: review
            path: review.yaml
          - id: review_batch
            path: review_batch.yaml
        environments:
          local: environments/local.yaml
        """,
    )
    _write(tmp_path / "environments" / "local.yaml", 'version: "1"\nname: local\n')
    return manifest


# ---------------------------------------------------------------------------
# Resolution + call-spec building
# ---------------------------------------------------------------------------


def _build_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow_id: str):
    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id=workflow_id, environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    parent_cls = create_workflow(
        resolved.spec, collect_activities(resolved.spec), subworkflows=subworkflows.records
    )
    return project, resolved, subworkflows, parent_cls


def test_resolve_subworkflows_for_records_child_identity(tmp_path, monkeypatch) -> None:
    _project, _resolved, subworkflows, parent_cls = _build_parent(tmp_path, monkeypatch, "review")
    record = subworkflows.records["child"]
    assert record.workflow_id == "child"
    assert record.workflow_name == "AssessClaimWorkflow"
    assert record.workflow_type.startswith("AssessClaimWorkflow.")
    assert record.input_type.__name__ == "Claim"
    assert record.output_type.__name__ == "Verdict"
    # The child class + its activity are exposed for worker registration.
    assert [getattr(c, "__typeflux_workflow_name__") for c in subworkflows.workflow_classes] == [
        "AssessClaimWorkflow"
    ]
    assert set(subworkflows.activities) == {"assess"}

    calls = getattr(parent_cls, "__typeflux_activity_calls__")
    assert len(calls) == 1 and isinstance(calls[0], SubworkflowCallSpec)
    call = calls[0]
    assert call.child_workflow_id == "child"
    assert call.child_workflow_type == record.workflow_type
    assert call.child_digest == record.spec_digest
    assert call.output_type.__name__ == "Verdict"
    # runtime.temporal.workflow_search_attribute configured on the parent => the child
    # stamps its OWN name into the attribute (built at generation time).
    assert call.search_attributes is not None


def test_map_workflow_builds_map_subworkflow_call_spec(tmp_path, monkeypatch) -> None:
    _project, _resolved, subworkflows, parent_cls = _build_parent(
        tmp_path, monkeypatch, "review_batch"
    )
    calls = getattr(parent_cls, "__typeflux_activity_calls__")
    assert len(calls) == 1 and isinstance(calls[0], MapSubworkflowCallSpec)
    call = calls[0]
    assert call.over == "input.claims"
    assert call.concurrency == 3
    assert call.collect.field == "verdicts"
    assert call.collect.output_type.__name__ == "VerdictBatch"
    assert call.child_workflow_type == subworkflows.records["child"].workflow_type
    # No search attribute configured on this parent => none stamped on the child.
    assert call.search_attributes is None


def test_call_payload_subworkflow_arms(tmp_path, monkeypatch) -> None:
    _p, _r, _s, plain_cls = _build_parent(tmp_path, monkeypatch, "review")
    plain_call = getattr(plain_cls, "__typeflux_activity_calls__")[0]
    payload = _call_payload(plain_call)
    assert payload["kind"] == "subworkflow"
    assert payload["step_id"] == "assess_one"
    assert payload["workflow_id"] == "child"
    assert payload["child_type"] == plain_call.child_workflow_type
    assert payload["child_digest"] == plain_call.child_digest
    # Only control-flow-relevant identity is folded — never the transport search attr.
    assert "search_attributes" not in payload

    _p2, _r2, _s2, map_cls = _build_parent(tmp_path, monkeypatch, "review_batch")
    map_call = getattr(map_cls, "__typeflux_activity_calls__")[0]
    map_payload = _call_payload(map_call)
    assert map_payload["kind"] == "subworkflow_map"
    assert map_payload["over"] == "input.claims"
    assert map_payload["concurrency"] == 3
    assert map_payload["collect_field"] == "verdicts"


# ---------------------------------------------------------------------------
# Parent-digest fold + reference cycle
# ---------------------------------------------------------------------------


def _parent_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _p, _r, _s, parent_cls = _build_parent(tmp_path, monkeypatch, "review")
    return getattr(parent_cls, "__typeflux_spec_digest__")


def test_parent_digest_is_stable_across_identical_builds(tmp_path, monkeypatch) -> None:
    first = _parent_digest(tmp_path, monkeypatch)
    # A second resolution of the SAME project tree produces the SAME parent digest
    # (determinism — the fold is a pure function of the resolved specs).
    manifest = tmp_path / "typeflux.project.yaml"
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="review", environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    again = getattr(
        create_workflow(
            resolved.spec, collect_activities(resolved.spec), subworkflows=subworkflows.records
        ),
        "__typeflux_spec_digest__",
    )
    assert first == again


def test_parent_digest_moves_when_child_graph_changes(tmp_path, monkeypatch) -> None:
    baseline = _parent_digest(tmp_path, monkeypatch)
    # Edit ONLY the child graph (add a second activity step) — the child digest moves,
    # which must cascade into the parent digest via the SubworkflowCallSpec fold.
    (tmp_path / PROJECT / "schemas.py").write_text(
        (tmp_path / PROJECT / "schemas.py").read_text() + "\n", encoding="utf-8"
    )
    # Rewrite the child with a SECOND graph step (assess_again: Verdict -> Verdict).
    _write(
        tmp_path / "assess.yaml",
        """
        project: subworkflow_project
        name: assess_claim
        task_queue: sub-queue
        runtime:
          temporal: { address: localhost:7233 }
          registry: { type: inline, prompts: { assess: assess } }
          provider: { type: fake }
          observability: { type: none }
        activities:
          definitions:
            - { name: assess, input: schemas:Claim, output: schemas:Verdict, prompt: assess }
            - { name: assess_again, input: schemas:Verdict, output: schemas:Verdict, prompt: assess }
        workflow:
          name: AssessClaimWorkflow
          input: schemas:Claim
          output: schemas:Verdict
          steps:
            - id: assess
              activity: assess
            - id: assess_again
              activity: assess_again
        """,
    )
    for name in tuple(sys.modules):
        if name == PROJECT or name.startswith(f"{PROJECT}."):
            del sys.modules[name]
    project = load_project_spec(tmp_path / "typeflux.project.yaml")
    resolved = resolve_project_workflow(project, workflow_id="review", environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    moved = getattr(
        create_workflow(
            resolved.spec, collect_activities(resolved.spec), subworkflows=subworkflows.records
        ),
        "__typeflux_spec_digest__",
    )
    assert moved != baseline


def test_reference_cycle_rejects(tmp_path, monkeypatch) -> None:
    # A -> B -> A: a workflow must not transitively invoke itself (#55).
    pkg = "cycle_project"
    for name in tuple(sys.modules):
        if name == pkg or name.startswith(f"{pkg}."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    package = tmp_path / pkg
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class Claim(BaseModel):
            claim_id: str
        """,
    )

    def _ref_yaml(name: str, workflow_name: str, ref: str) -> str:
        return dedent(
            f"""\
            project: {pkg}
            name: {name}
            task_queue: q
            runtime:
              temporal: {{ address: localhost:7233 }}
              registry: {{ type: inline, prompts: {{ p: p }} }}
              provider: {{ type: fake }}
              observability: {{ type: none }}
            activities:
              definitions: []
            workflow:
              name: {workflow_name}
              input: schemas:Claim
              output: schemas:Claim
              steps:
                - id: step
                  workflow: {ref}
            """
        )

    _write(tmp_path / "a.yaml", _ref_yaml("a", "AWorkflow", "b"))
    _write(tmp_path / "b.yaml", _ref_yaml("b", "BWorkflow", "a"))
    _write(
        tmp_path / "typeflux.project.yaml",
        """
        version: "1"
        name: cycle-demo
        workflows:
          - id: a
            path: a.yaml
          - id: b
            path: b.yaml
        environments:
          local: environments/local.yaml
        """,
    )
    _write(tmp_path / "environments" / "local.yaml", 'version: "1"\nname: local\n')
    project = load_project_spec(tmp_path / "typeflux.project.yaml")
    resolved_a = resolve_project_workflow(project, workflow_id="a", environment_id="local")
    with pytest.raises(ProjectEnvironmentError, match="cycle detected"):
        resolve_subworkflows_for(project, resolved_a)


# ---------------------------------------------------------------------------
# Hermetic base_env through the closure (#760 fix round)
# ---------------------------------------------------------------------------


def test_subworkflow_closure_resolves_children_under_the_parent_base_env(
    tmp_path, monkeypatch
) -> None:
    # A hermetically-resolved parent's CHILDREN must resolve under the SAME injected
    # base — the poison in the real shell must never reach a child's resolved spec.
    # Covers all three re-resolution sites via the base retained on the artifact:
    # `create_workflow_with_subworkflows` -> `resolve_subworkflows_for` (child specs)
    # and `_iter_subworkflow_closure` (closure-policy admission walk).
    from typeflux.project.environment import create_workflow_with_subworkflows
    from typeflux.project.policy_enforcement import _iter_subworkflow_closure

    manifest = _setup_project(tmp_path, monkeypatch)
    # The child's task_queue interpolates from the base env; poison the shell.
    _write(
        tmp_path / "assess.yaml",
        _CHILD_YAML.format(pkg=PROJECT).replace(
            "task_queue: sub-queue", "task_queue: ${TF760_CHILD_QUEUE}"
        ),
    )
    monkeypatch.setenv("TF760_CHILD_QUEUE", "POISON")
    project = load_project_spec(manifest)
    base = {"TF760_CHILD_QUEUE": "hermetic-child-queue"}

    resolved = resolve_project_workflow(
        project, workflow_id="review", environment_id="local", base_env=base
    )
    _cls, subworkflows, _activities = create_workflow_with_subworkflows(project, resolved)
    assert subworkflows.child_specs["child"].task_queue == "hermetic-child-queue"

    members = list(_iter_subworkflow_closure(project, resolved=resolved, environment_id="local"))
    assert [ref for ref, _child, _error in members] == ["child"]
    child = members[0][1]
    assert child is not None and members[0][2] is None
    # The closure-policy walk resolves the child under the parent's retained base.
    assert child.spec.task_queue == "hermetic-child-queue"
    assert child.base_env == base


# ---------------------------------------------------------------------------
# Topology projection
# ---------------------------------------------------------------------------


def test_bundle_topology_projects_workflow_nodes(tmp_path, monkeypatch) -> None:
    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    plain = resolve_workflow_bundle(project, workflow_id="review", environment_id="local")
    plain_nodes = [n.model_dump(exclude_none=True) for n in plain.topology.nodes]
    assert plain_nodes == [{"id": "assess_one", "kind": "workflow", "workflow": "child"}]
    # A sub-workflow node calls no PARENT activity: it never appears in steps/activities.
    assert plain.steps == ()
    assert all(a.name != "AssessClaimWorkflow" for a in plain.activities)

    batch = resolve_workflow_bundle(project, workflow_id="review_batch", environment_id="local")
    batch_nodes = [n.model_dump(exclude_none=True) for n in batch.topology.nodes]
    assert batch_nodes == [{"id": "fan", "kind": "workflow", "workflow": "child"}]


# ---------------------------------------------------------------------------
# Live child execution (time-skipping) — parent->child plain + map
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
async def test_parent_runs_child_plain_and_map_with_ids_and_memo(tmp_path, monkeypatch) -> None:
    try:
        from temporalio.client import Client
        from temporalio.contrib.pydantic import pydantic_data_converter
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker
    except ModuleNotFoundError as exc:  # pragma: no cover - temporalio always present.
        pytest.skip(f"temporalio not installed: {exc}")

    from typeflux.execution.worker import build_temporal_activity
    from typeflux.testing import FakeProvider
    from typeflux.yaml.runtime import _build_registry
    from typeflux.yaml.workflow import create_yaml_workflow_runner

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    from subworkflow_project.schemas import Batch, Claim, Verdict, VerdictBatch  # type: ignore

    # Resolve BOTH parents; the child class + activity come from the resolver.
    plain = resolve_project_workflow(project, workflow_id="review", environment_id="local")
    batch = resolve_project_workflow(project, workflow_id="review_batch", environment_id="local")
    plain_sub = resolve_subworkflows_for(project, plain)
    batch_sub = resolve_subworkflows_for(project, batch)
    plain_cls = create_workflow(
        plain.spec, collect_activities(plain.spec), subworkflows=plain_sub.records
    )
    batch_cls = create_workflow(
        batch.spec, collect_activities(batch.spec), subworkflows=batch_sub.records
    )
    child_cls = plain_sub.workflow_classes[0]
    child_type = getattr(child_cls, "__typeflux_workflow_type__")

    child_resolved = resolve_project_workflow(project, workflow_id="child", environment_id="local")
    registry = _build_registry(child_resolved.spec)
    provider = FakeProvider([Verdict(claim_id=str(i), substantiated=True) for i in range(10)])
    assess_fn = build_temporal_activity(
        plain_sub.activities["assess"], registry=registry, provider=provider
    )

    # Prefer a reachable local dev server (`temporal server start-dev`) — no test-server
    # binary download — and fall back to the time-skipping test server otherwise.
    address = os.environ.get("TYPEFLUX_LIVE_TEMPORAL_ADDRESS", "localhost:7233")
    try:
        client = await Client.connect(address, data_converter=pydantic_data_converter)
        env = WorkflowEnvironment.from_client(client)
    except Exception:
        try:
            env = await WorkflowEnvironment.start_time_skipping(
                data_converter=pydantic_data_converter
            )
        except Exception as exc:  # pragma: no cover - server binary unavailable.
            pytest.skip(f"Temporal test server unavailable: {exc}")

    task_queue = f"sw-{uuid4().hex}"
    started_ids: list[str] = []
    async with env:
        # Register the child's search attribute BEFORE any workflow runs — an
        # unregistered attribute makes the parent's task retry forever (see helper).
        await _ensure_workflow_search_attribute_registered(env.client)
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[plain_cls, batch_cls, child_cls],
            activities=[assess_fn],
            workflow_runner=create_yaml_workflow_runner(),
        ):
            try:
                # Plain: parent -> one child on the running value.
                plain_id = f"review-{uuid4().hex}"
                started_ids += [plain_id, f"{plain_id}.assess_one"]
                result = await _execute_workflow_bounded(
                    env.client,
                    plain_cls.run,
                    Claim(claim_id="CLM-1", text="cuts risk 30%"),
                    what="parent -> child plain execution",
                    id=plain_id,
                    task_queue=task_queue,
                    result_type=Verdict,
                )
                assert isinstance(result, Verdict)
                # Deterministic child id `{parent}.{step_id}` + parent-link memo.
                child_desc = await env.client.get_workflow_handle(
                    f"{plain_id}.assess_one"
                ).describe()
                child_memo = await child_desc.memo()
                assert child_memo["typeflux_parent_workflow_id"] == plain_id
                assert child_memo["typeflux_workflow"] == "AssessClaimWorkflow"
                assert child_desc.workflow_type == child_type

                # Map: parent -> a child per item, ids `{parent}.{step}-{index}`.
                batch_id = f"review-batch-{uuid4().hex}"
                started_ids += [batch_id, f"{batch_id}.fan-0", f"{batch_id}.fan-1"]
                batch_result = await _execute_workflow_bounded(
                    env.client,
                    batch_cls.run,
                    Batch(claims=[Claim(claim_id="A", text="a"), Claim(claim_id="B", text="b")]),
                    what="parent -> map-over-child execution",
                    id=batch_id,
                    task_queue=task_queue,
                    result_type=VerdictBatch,
                )
                assert len(batch_result.verdicts) == 2
                child0 = await env.client.get_workflow_handle(f"{batch_id}.fan-0").describe()
                memo0 = await child0.memo()
                assert memo0["typeflux_parent_workflow_id"] == batch_id
            finally:
                await _best_effort_delete_executions(env.client, started_ids)


# ---------------------------------------------------------------------------
# Review round (#55 slice 3): resolution structure, collisions, error precision,
# search-attribute inheritance, memo-key drift guard, binding-driver prelude
# ---------------------------------------------------------------------------


def _mini_project(tmp_path, monkeypatch, *, pkg: str, workflows: dict[str, str]):
    """Scaffold a tiny project (one shared `schemas.py` with Claim) whose workflow
    files are given verbatim, and return the loaded project spec."""
    for name in tuple(sys.modules):
        if name == pkg or name.startswith(f"{pkg}."):
            del sys.modules[name]
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    package = tmp_path / pkg
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class Claim(BaseModel):
            claim_id: str
        """,
    )
    entries = []
    for wid, text in workflows.items():
        _write(tmp_path / f"{wid}.yaml", text)
        entries.append(f"  - id: {wid}\n    path: {wid}.yaml")
    (tmp_path / "typeflux.project.yaml").write_text(
        'version: "1"\nname: '
        + pkg.replace("_", "-")
        + "\nworkflows:\n"
        + "\n".join(entries)
        + "\nenvironments:\n  local: environments/local.yaml\n",
        encoding="utf-8",
    )
    _write(tmp_path / "environments" / "local.yaml", 'version: "1"\nname: local\n')
    return load_project_spec(tmp_path / "typeflux.project.yaml")


def _wf(
    pkg: str,
    name: str,
    workflow_name: str,
    *,
    steps: str,
    definitions: str = "[]",
    search_attribute: str | None = None,
    version: str | None = None,
) -> str:
    attr = (
        f", workflow_search_attribute: {search_attribute}" if search_attribute is not None else ""
    )
    version_line = f"\n          version: {version}" if version is not None else ""
    return dedent(
        f"""\
        project: {pkg}
        name: {name}
        task_queue: q
        runtime:
          temporal: {{ address: localhost:7233{attr} }}
          registry: {{ type: inline, prompts: {{ p: p }} }}
          provider: {{ type: fake }}
          observability: {{ type: none }}
        activities:
          definitions: {definitions}
        workflow:
          name: {workflow_name}{version_line}
          input: schemas:Claim
          output: schemas:Claim
          steps:
        """
    ) + indent(steps, "    ")


_STEP_REF = "- id: {sid}\n  workflow: {ref}\n"
_STEP_ACT = "- id: {sid}\n  activity: {act}\n"
_DEF_ASSESS = (
    '[{{ name: {act}, input: "schemas:Claim", output: "schemas:Claim", prompt: {prompt} }}]'
)


def test_activity_collision_parent_vs_child_rejects_naming_both(tmp_path, monkeypatch) -> None:
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="collide_pc",
        workflows={
            "parent": _wf(
                "collide_pc",
                "parent",
                "ParentWorkflow",
                steps=_STEP_ACT.format(sid="own", act="assess")
                + _STEP_REF.format(sid="sub", ref="child"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
            # The child declares the SAME name with a DIFFERENT definition (other prompt).
            "child": _wf(
                "collide_pc",
                "child",
                "ChildWorkflow",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions='[{ name: assess, input: "schemas:Claim", output: "schemas:Claim", prompt: p, validation_retries: 3 }]',
            ),
        },
    )
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    with pytest.raises(ProjectEnvironmentError) as excinfo:
        resolve_subworkflows_for(project, resolved)
    message = str(excinfo.value)
    assert "'assess'" in message
    assert "'parent'" in message and "'child'" in message


def test_activity_collision_sibling_vs_sibling_and_deep_chain(tmp_path, monkeypatch) -> None:
    # Sibling-vs-sibling: b and c both declare `shared` differently.
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="collide_sib",
        workflows={
            "parent": _wf(
                "collide_sib",
                "parent",
                "PWorkflow",
                steps=_STEP_REF.format(sid="sb", ref="b") + _STEP_REF.format(sid="sc", ref="c"),
            ),
            "b": _wf(
                "collide_sib",
                "b",
                "BWorkflow",
                steps=_STEP_ACT.format(sid="go", act="shared"),
                definitions=_DEF_ASSESS.format(act="shared", prompt="p"),
            ),
            "c": _wf(
                "collide_sib",
                "c",
                "CWorkflow",
                steps=_STEP_ACT.format(sid="go", act="shared"),
                definitions='[{ name: shared, input: "schemas:Claim", output: "schemas:Claim", prompt: p, validation_retries: 7 }]',
            ),
        },
    )
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    with pytest.raises(ProjectEnvironmentError) as excinfo:
        resolve_subworkflows_for(project, resolved)
    assert "'shared'" in str(excinfo.value)
    assert "'b'" in str(excinfo.value) and "'c'" in str(excinfo.value)

    # Deep chain: the GRANDCHILD's divergent declaration collides with the parent's.
    project2 = _mini_project(
        tmp_path / "deep",
        monkeypatch,
        pkg="collide_deep",
        workflows={
            "parent": _wf(
                "collide_deep",
                "parent",
                "P2Workflow",
                steps=_STEP_ACT.format(sid="own", act="assess")
                + _STEP_REF.format(sid="sm", ref="mid"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
            "mid": _wf(
                "collide_deep",
                "mid",
                "MidWorkflow",
                steps=_STEP_REF.format(sid="sl", ref="leaf"),
            ),
            "leaf": _wf(
                "collide_deep",
                "leaf",
                "LeafWorkflow",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions='[{ name: assess, input: "schemas:Claim", output: "schemas:Claim", prompt: p, validation_retries: 9 }]',
            ),
        },
    )
    resolved2 = resolve_project_workflow(project2, workflow_id="parent", environment_id="local")
    with pytest.raises(ProjectEnvironmentError) as excinfo2:
        resolve_subworkflows_for(project2, resolved2)
    assert "'assess'" in str(excinfo2.value)
    assert "'parent'" in str(excinfo2.value) and "'leaf'" in str(excinfo2.value)


def test_activity_collision_parent_module_activity_rejects(tmp_path, monkeypatch) -> None:
    # The parent contributes `assess` via activities.modules (Python code), not a YAML
    # definition; the child's divergent YAML `assess` must still collide (Bugbot: the
    # registry used to seed from the parent's YAML definitions only).
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="collide_mod",
        workflows={
            "parent": _wf(
                "collide_mod",
                "parent",
                "ParentWorkflow",
                steps=_STEP_ACT.format(sid="own", act="assess")
                + _STEP_REF.format(sid="sub", ref="child"),
            ).replace(
                "definitions: []",
                "modules: [acts]\n  definitions: []",
            ),
            "child": _wf(
                "collide_mod",
                "child",
                "ChildWorkflow",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
        },
    )
    _write(
        tmp_path / "collide_mod" / "acts.py",
        """
        from temporalio import activity

        from collide_mod.schemas import Claim


        @activity.defn(name="assess")
        async def assess(value: Claim) -> Claim:
            return value
        """,
    )
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    with pytest.raises(ProjectEnvironmentError) as excinfo:
        resolve_subworkflows_for(project, resolved)
    message = str(excinfo.value)
    assert "'assess'" in message
    assert "'parent'" in message and "'child'" in message


def test_activity_collision_identical_definitions_dedupe(tmp_path, monkeypatch) -> None:
    # The SAME canonical definition under one name across two workflows is allowed —
    # it deduplicates to one registered activity.
    definitions = _DEF_ASSESS.format(act="assess", prompt="p")
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="collide_ok",
        workflows={
            "parent": _wf(
                "collide_ok",
                "parent",
                "POkWorkflow",
                steps=_STEP_ACT.format(sid="own", act="assess")
                + _STEP_REF.format(sid="sub", ref="child"),
                definitions=definitions,
            ),
            "child": _wf(
                "collide_ok",
                "child",
                "COkWorkflow",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions=definitions,
            ),
        },
    )
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    assert list(subworkflows.records) == ["child"]
    assert list(subworkflows.activities) == ["assess"]


def test_typoed_ref_names_step_and_manifest_requirement(tmp_path, monkeypatch) -> None:
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="typo_ref",
        workflows={
            "parent": _wf(
                "typo_ref",
                "parent",
                "TypoWorkflow",
                steps=_STEP_REF.format(sid="assess_one", ref="chidl"),
            ),
        },
    )
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    with pytest.raises(ProjectEnvironmentError) as excinfo:
        resolve_subworkflows_for(project, resolved)
    message = str(excinfo.value)
    assert "step 'assess_one' of workflow 'parent'" in message
    assert "'chidl'" in message
    assert "not declared in the project manifest" in message
    assert "typeflux.project.yaml" in message


def test_declared_but_broken_sibling_surfaces_its_real_error(tmp_path, monkeypatch) -> None:
    # The child is DECLARED but its own graph is broken (references an undeclared
    # activity): the parent's resolution fails with the sibling's REAL error,
    # attributed to the sibling — not "not declared" (item distinction).
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="broken_child",
        workflows={
            "parent": _wf(
                "broken_child",
                "parent",
                "BrokenParentWorkflow",
                steps=_STEP_REF.format(sid="sub", ref="child"),
            ),
            "child": _wf(
                "broken_child",
                "child",
                "BrokenChildWorkflow",
                steps=_STEP_ACT.format(sid="go", act="missing_activity"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
        },
    )
    resolved = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    with pytest.raises(ProjectEnvironmentError) as excinfo:
        resolve_subworkflows_for(project, resolved)
    message = str(excinfo.value)
    assert "sibling workflow 'child'" in message
    assert "failed to build" in message
    assert "unknown activity" in message
    assert "not declared in the project manifest" not in message


def test_root_search_attribute_inherited_down_the_chain(tmp_path, monkeypatch) -> None:
    # A (RootAttr) -> B (OtherAttr) -> C: the ROOT's configured attribute NAME is
    # inherited by every descendant's child-start options, overriding B's own
    # config (TS start-context parity); children still stamp their OWN name as the
    # VALUE. And the attribute never enters the digest (deployment config).
    workflows = {
        "a": _wf(
            "attr_chain",
            "a",
            "AChainWorkflow",
            steps=_STEP_REF.format(sid="ab", ref="b"),
            search_attribute="RootAttr",
        ),
        "b": _wf(
            "attr_chain",
            "b",
            "BChainWorkflow",
            steps=_STEP_REF.format(sid="bc", ref="c"),
            search_attribute="OtherAttr",
        ),
        "c": _wf(
            "attr_chain",
            "c",
            "CChainWorkflow",
            steps=_STEP_ACT.format(sid="go", act="assess"),
            definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
        ),
    }
    project = _mini_project(tmp_path, monkeypatch, pkg="attr_chain", workflows=workflows)
    resolved = resolve_project_workflow(project, workflow_id="a", environment_id="local")
    subworkflows = resolve_subworkflows_for(project, resolved)
    a_cls = create_workflow(
        resolved.spec, collect_activities(resolved.spec), subworkflows=subworkflows.records
    )

    def _attr_name(call) -> str:
        pairs = list(call.search_attributes)
        assert len(pairs) == 1
        return pairs[0].key.name, pairs[0].value

    a_call = getattr(a_cls, "__typeflux_activity_calls__")[0]
    assert _attr_name(a_call) == ("RootAttr", "BChainWorkflow")
    b_cls = next(
        cls
        for cls in subworkflows.workflow_classes
        if getattr(cls, "__typeflux_workflow_name__") == "BChainWorkflow"
    )
    b_call = getattr(b_cls, "__typeflux_activity_calls__")[0]
    # B's own OtherAttr config is IGNORED at runtime: C is stamped under A's name.
    assert _attr_name(b_call) == ("RootAttr", "CChainWorkflow")

    # Digest neutrality: the attribute is not in the digest payload, and renaming
    # the root attribute leaves the parent digest byte-identical.
    payload = _call_payload(a_call)
    assert "search" not in str(sorted(payload))
    baseline = getattr(a_cls, "__typeflux_spec_digest__")
    (tmp_path / "a.yaml").write_text(
        (tmp_path / "a.yaml").read_text().replace("RootAttr", "RenamedAttr"), encoding="utf-8"
    )
    resolved2 = resolve_project_workflow(project, workflow_id="a", environment_id="local")
    subworkflows2 = resolve_subworkflows_for(project, resolved2)
    a_cls2 = create_workflow(
        resolved2.spec, collect_activities(resolved2.spec), subworkflows=subworkflows2.records
    )
    assert getattr(a_cls2, "__typeflux_spec_digest__") == baseline


def test_diamond_reference_resolves_shared_child_once(tmp_path, monkeypatch) -> None:
    # A -> B, A -> C, B -> D, C -> D: D resolves exactly once per
    # resolve_subworkflows_for call (memoized), while cycle detection stays per-path.
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="diamond",
        workflows={
            "a": _wf(
                "diamond",
                "a",
                "ADiamond",
                steps=_STEP_REF.format(sid="sb", ref="b") + _STEP_REF.format(sid="sc", ref="c"),
            ),
            "b": _wf("diamond", "b", "BDiamond", steps=_STEP_REF.format(sid="sd", ref="d")),
            "c": _wf("diamond", "c", "CDiamond", steps=_STEP_REF.format(sid="sd", ref="d")),
            "d": _wf(
                "diamond",
                "d",
                "DDiamond",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
        },
    )
    import typeflux.project.environment as environment_module

    resolved = resolve_project_workflow(project, workflow_id="a", environment_id="local")
    real_resolve = environment_module.resolve_project_workflow
    counts: dict[str, int] = {}

    def counting_resolve(project_arg, *, workflow_id, environment_id, base_env=None):
        counts[workflow_id] = counts.get(workflow_id, 0) + 1
        return real_resolve(
            project_arg,
            workflow_id=workflow_id,
            environment_id=environment_id,
            base_env=base_env,
        )

    monkeypatch.setattr(environment_module, "resolve_project_workflow", counting_resolve)
    subworkflows = resolve_subworkflows_for(project, resolved)
    assert counts == {"b": 1, "c": 1, "d": 1}
    assert set(subworkflows.records) == {"b", "c"}
    assert {
        getattr(cls, "__typeflux_workflow_name__") for cls in subworkflows.workflow_classes
    } == {
        "BDiamond",
        "CDiamond",
        "DDiamond",
    }


def test_child_identity_memo_keys_match_top_level_identity_memo(tmp_path, monkeypatch) -> None:
    # DRIFT GUARD (#55 §9): the child memo key set == the top-level identity-memo
    # key set ∪ {typeflux_parent_workflow_id} — the `children` correlation listing
    # joins on these exact keys, so a rename in one place must fail here.
    from typeflux.yaml.runtime import _workflow_identity_memo
    from typeflux.yaml.workflow import _child_identity_memo

    _p, _r, subworkflows, parent_cls = _build_parent(tmp_path, monkeypatch, "review")
    child_cls = subworkflows.workflow_classes[0]
    call = getattr(parent_cls, "__typeflux_activity_calls__")[0]
    child_keys = set(_child_identity_memo(call, "parent-id"))
    top_level_keys = set(_workflow_identity_memo(child_cls))
    assert child_keys == top_level_keys | {"typeflux_parent_workflow_id"}


@pytest.mark.asyncio
async def test_binding_driver_prepared_runtime_registers_children(tmp_path, monkeypatch) -> None:
    # Item: the CP operations driver's pinned-runtime prelude must resolve
    # sub-workflows (a `workflow:`-step workflow operated through the driver used to
    # hit the standalone rejection). Exercises for_project_workflow itself with
    # build_runtime stubbed out (no Temporal connection in unit tests).
    from types import SimpleNamespace

    from typeflux.project import binding as binding_module

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    captured: dict = {}

    async def fake_build_runtime(spec, *, policy_guard=None, prepared=None):
        captured["prepared"] = prepared
        return SimpleNamespace(spec=spec)

    monkeypatch.setattr(binding_module, "build_runtime", fake_build_runtime)
    driver = await binding_module.PythonVersionedTypeDriver.for_project_workflow(
        project, workflow_id="review", environment_id="local"
    )
    prepared = captured["prepared"]
    assert prepared is not None
    assert getattr(prepared.workflow_class, "__typeflux_workflow_name__") == "ReviewWorkflow"
    assert [
        getattr(cls, "__typeflux_workflow_name__") for cls in prepared.child_workflow_classes
    ] == ["AssessClaimWorkflow"]
    # The child's activity is merged into the prepared activity map for the worker.
    assert "assess" in prepared.activities
    assert driver.resolved is not None


def test_child_undeclared_activity_fails_parent_resolution_and_maps_to_422(
    tmp_path, monkeypatch
) -> None:
    # Item 3: a child step referencing an undeclared activity fails the PARENT's
    # resolution loudly (wrapped as a 422-mapping ProjectEnvironmentError) and a CP
    # read route answers 422, not 500.
    from fastapi.testclient import TestClient

    from typeflux.controlplane import create_app

    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="cp_broken_child",
        workflows={
            "parent": _wf(
                "cp_broken_child",
                "parent",
                "CpBrokenParent",
                steps=_STEP_REF.format(sid="sub", ref="child"),
            ),
            "child": _wf(
                "cp_broken_child",
                "child",
                "CpBrokenChild",
                steps=_STEP_ACT.format(sid="go", act="missing_activity"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
        },
    )
    del project  # the app loads the manifest itself
    client = TestClient(create_app(tmp_path / "typeflux.project.yaml"))
    response = client.get("/api/v1/workflows/parent/catalog", params={"environment_id": "local"})
    assert response.status_code == 422, response.text
    detail = response.json()
    assert "unknown activity" in str(detail)


# ---------------------------------------------------------------------------
# Sub-workflow visibility notice (#55 §6 mitigation b)
# ---------------------------------------------------------------------------


def _validation_checks(project, workflow_id: str):
    from typeflux.project.validation import validate_project_bundle

    report = validate_project_bundle(project, environment_id="local", workflow_ids=(workflow_id,))
    (validation,) = report.resolved_workflows
    return list(validation.checks)


def test_subworkflow_visibility_check_placement_and_details(tmp_path, monkeypatch) -> None:
    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="vis_check",
        workflows={
            # version + NO search attribute => notice.
            "noticed": _wf(
                "vis_check",
                "noticed",
                "NoticedWorkflow",
                steps=_STEP_REF.format(sid="sub", ref="leaf"),
                version="v1",
            ),
            # version + attribute => no notice.
            "quiet": _wf(
                "vis_check",
                "quiet",
                "QuietWorkflow",
                steps=_STEP_REF.format(sid="sub", ref="leaf"),
                search_attribute="TypefluxWorkflow",
                version="v2",
            ),
            # refs, no version => check present, no notice.
            "unversioned": _wf(
                "vis_check",
                "unversioned",
                "UnversionedWorkflow",
                steps=_STEP_REF.format(sid="sub", ref="leaf"),
            ),
            # V1 workflow (no refs) => NO subworkflow_visibility check at all.
            "leaf": _wf(
                "vis_check",
                "leaf",
                "LeafVisWorkflow",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
        },
    )
    from typeflux.project.validation import SUBWORKFLOW_VISIBILITY_NOTICE

    noticed = _validation_checks(project, "noticed")
    codes = [check.code for check in noticed]
    graph_index = codes.index("workflow_graph")
    assert codes[graph_index + 1] == "subworkflow_visibility"
    check = noticed[graph_index + 1]
    assert check.status == "passed"
    assert check.details == {
        "search_attribute_configured": False,
        "workflow_version": "v1",
        "notice": SUBWORKFLOW_VISIBILITY_NOTICE,
    }

    quiet = _validation_checks(project, "quiet")
    quiet_check = next(c for c in quiet if c.code == "subworkflow_visibility")
    assert quiet_check.details == {
        "search_attribute_configured": True,
        "workflow_version": "v2",
    }

    unversioned = _validation_checks(project, "unversioned")
    unversioned_check = next(c for c in unversioned if c.code == "subworkflow_visibility")
    assert unversioned_check.details == {
        "search_attribute_configured": False,
        "workflow_version": None,
    }

    leaf = _validation_checks(project, "leaf")
    assert all(check.code != "subworkflow_visibility" for check in leaf)


def test_deployment_preview_carries_the_visibility_notice(tmp_path, monkeypatch) -> None:
    from typeflux.project.validation import SUBWORKFLOW_VISIBILITY_NOTICE

    project = _mini_project(
        tmp_path,
        monkeypatch,
        pkg="vis_preview",
        workflows={
            "noticed": _wf(
                "vis_preview",
                "noticed",
                "NoticedPreviewWorkflow",
                steps=_STEP_REF.format(sid="sub", ref="leaf"),
                version="v1",
            ),
            "leaf": _wf(
                "vis_preview",
                "leaf",
                "LeafPreviewWorkflow",
                steps=_STEP_ACT.format(sid="go", act="assess"),
                definitions=_DEF_ASSESS.format(act="assess", prompt="p"),
            ),
        },
    )
    bundle = resolve_workflow_bundle(
        project,
        workflow_id="noticed",
        environment_id="local",
        deployment_image="registry/image:dev",
    )
    preview = bundle.deployment_preview
    assert preview is not None
    assert preview.get("notices") == [SUBWORKFLOW_VISIBILITY_NOTICE]
    # A workflow without refs never carries notices.
    quiet = resolve_workflow_bundle(
        project,
        workflow_id="leaf",
        environment_id="local",
        deployment_image="registry/image:dev",
    )
    assert quiet.deployment_preview is not None
    assert "notices" not in quiet.deployment_preview


# ---------------------------------------------------------------------------
# `children` correlation listing (#55 §9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_children_are_memo_filtered_by_parent_id(tmp_path, monkeypatch) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from conftest import patch_workflow_list_client
    from typeflux.project.runs import WorkflowChildExecution, workflow_run_correlation

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    resolved = resolve_project_workflow(project, workflow_id="review", environment_id="local")
    child_type = resolve_subworkflows_for(project, resolved).records["child"].workflow_type

    parent_id = "review-run-1"
    matching = SimpleNamespace(
        id=f"{parent_id}.assess_one",
        status=SimpleNamespace(name="COMPLETED"),
        start_time=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        memo={
            "typeflux_parent_workflow_id": parent_id,
            "typeflux_workflow": "AssessClaimWorkflow",
        },
    )
    other_parent = SimpleNamespace(
        id="someone-else.assess_one",
        status=SimpleNamespace(name="RUNNING"),
        start_time=None,
        memo={
            "typeflux_parent_workflow_id": "someone-else",
            "typeflux_workflow": "AssessClaimWorkflow",
        },
    )
    memoless = SimpleNamespace(id="legacy.assess_one", status=None, start_time=None, memo=None)
    client = patch_workflow_list_client(monkeypatch, [matching, other_parent, memoless])

    result = await workflow_run_correlation(
        project, workflow_id="review", environment_id="local", execution_id=parent_id
    )
    # One bounded scan per referenced child TYPE, memo-filtered client-side.
    assert client.queries == [f"WorkflowType = '{child_type}'"]
    assert result.children == (
        WorkflowChildExecution(
            workflow_id=f"{parent_id}.assess_one",
            workflow_name="AssessClaimWorkflow",
            status="COMPLETED",
            start_time="2026-07-01T09:00:00+00:00",
        ),
    )
    assert result.warning is None
    assert result.reachable is True


@pytest.mark.asyncio
async def test_correlation_surfaces_migrated_from_for_a_composed_parent(
    tmp_path, monkeypatch
) -> None:
    # #204 review: a COMPOSED parent whose memo carries typeflux_migrated_from —
    # the provenance describe runs alongside the children scan and surfaces it.
    from conftest import patch_workflow_list_client
    from typeflux.project.runs import WorkflowMigrationProvenance, workflow_run_correlation

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    parent_id = "review-run-migrated"
    client = patch_workflow_list_client(
        monkeypatch,
        [],
        describe_memos={
            parent_id: {
                "typeflux_migrated_from": "old-run-7",
                "typeflux_migrated_from_version": "ReviewWorkflow.v1",
            }
        },
    )

    result = await workflow_run_correlation(
        project, workflow_id="review", environment_id="local", execution_id=parent_id
    )

    assert result.migrated_from == WorkflowMigrationProvenance(
        run_id="old-run-7", version_key="ReviewWorkflow.v1"
    )
    assert result.children == ()  # the scan ran and found none
    assert result.warning is None
    assert client.described == [parent_id]
    assert len(client.queries) == 1  # the composed children scan still ran


@pytest.mark.asyncio
async def test_correlation_children_none_with_warning_when_temporal_unreachable(
    tmp_path, monkeypatch
) -> None:
    from typeflux.project.runs import workflow_run_correlation

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    async def refusing_connect(spec, *, plugin):
        raise RuntimeError("Failed client connect: connection refused")

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", refusing_connect)
    result = await workflow_run_correlation(
        project, workflow_id="review", environment_id="local", execution_id="review-run-1"
    )
    assert result.children is None
    assert "temporal tier unreachable" in (result.warning or "")
    # The observability flag is NOT overloaded: the observer tier (none) stays reachable.
    assert result.reachable is True


@pytest.mark.asyncio
async def test_correlation_children_empty_without_children_scan_when_no_refs(
    tmp_path, monkeypatch
) -> None:
    # The amended no-refs contract (#204 review): children stays () with NO
    # visibility scan, but correlation makes exactly ONE describe of the parent
    # execution for the migration-provenance memo — decoupled from children.
    from conftest import patch_workflow_list_client
    from typeflux.project.runs import workflow_run_correlation

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    client = patch_workflow_list_client(monkeypatch, [])

    # The CHILD workflow itself references no siblings.
    result = await workflow_run_correlation(
        project, workflow_id="child", environment_id="local", execution_id="child-run-1"
    )
    assert result.children == ()
    assert result.warning is None
    assert result.migrated_from is None
    # No children SCAN ran; exactly one provenance describe did.
    assert client.queries == []
    assert client.described == ["child-run-1"]


# ---------------------------------------------------------------------------
# ALLOW_DUPLICATE still-running collision (live)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
async def test_still_running_child_id_collision_fails_the_parent_loudly(
    tmp_path, monkeypatch
) -> None:
    # ALLOW_DUPLICATE permits reuse of a CLOSED workflow id; a STILL-RUNNING
    # execution occupying the deterministic child id must reject the child start
    # and fail the parent — never adopt the stranger as its child.
    try:
        from temporalio.client import Client, WorkflowFailureError
        from temporalio.contrib.pydantic import pydantic_data_converter
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker
    except ModuleNotFoundError as exc:  # pragma: no cover - temporalio always present.
        pytest.skip(f"temporalio not installed: {exc}")

    from typeflux.execution.worker import build_temporal_activity
    from typeflux.testing import FakeProvider
    from typeflux.yaml.runtime import _build_registry
    from typeflux.yaml.workflow import create_yaml_workflow_runner

    manifest = _setup_project(tmp_path, monkeypatch)
    project = load_project_spec(manifest)
    from subworkflow_project.schemas import Claim, Verdict  # type: ignore

    plain = resolve_project_workflow(project, workflow_id="review", environment_id="local")
    plain_sub = resolve_subworkflows_for(project, plain)
    plain_cls = create_workflow(
        plain.spec, collect_activities(plain.spec), subworkflows=plain_sub.records
    )
    child_cls = plain_sub.workflow_classes[0]
    child_resolved = resolve_project_workflow(project, workflow_id="child", environment_id="local")
    assess_fn = build_temporal_activity(
        plain_sub.activities["assess"],
        registry=_build_registry(child_resolved.spec),
        provider=FakeProvider([Verdict(claim_id="X", substantiated=True)]),
    )

    _Blocker = _SubworkflowIdBlocker  # module-level: temporalio rejects local classes

    address = os.environ.get("TYPEFLUX_LIVE_TEMPORAL_ADDRESS", "localhost:7233")
    try:
        client = await Client.connect(address, data_converter=pydantic_data_converter)
        env = WorkflowEnvironment.from_client(client)
    except Exception:
        try:
            env = await WorkflowEnvironment.start_time_skipping(
                data_converter=pydantic_data_converter
            )
        except Exception as exc:  # pragma: no cover - server binary unavailable.
            pytest.skip(f"Temporal test server unavailable: {exc}")

    task_queue = f"sw-collide-{uuid4().hex}"
    async with env:
        # Same latent hang as the plain/map live test: this parent also stamps
        # TypefluxWorkflow onto its child, so the attribute must be registered or
        # the parent's workflow task retries forever instead of failing loudly.
        await _ensure_workflow_search_attribute_registered(env.client)
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[plain_cls, child_cls, _Blocker],
            activities=[assess_fn],
            workflow_runner=create_yaml_workflow_runner(),
        ):
            parent_id = f"review-collide-{uuid4().hex}"
            blocker = await env.client.start_workflow(
                _Blocker.run,
                id=f"{parent_id}.assess_one",
                task_queue=task_queue,
            )
            try:
                with pytest.raises(WorkflowFailureError) as excinfo:
                    # Bounded: the collision must surface as a TERMINAL parent
                    # failure, not a retry-forever task failure that hangs.
                    await _execute_workflow_bounded(
                        env.client,
                        plain_cls.run,
                        Claim(claim_id="CLM-1", text="occupied"),
                        what="parent whose child id is occupied",
                        id=parent_id,
                        task_queue=task_queue,
                        result_type=Verdict,
                    )
                # The failure chain names the child-start rejection.
                chain: list[str] = []
                error: BaseException | None = excinfo.value
                while error is not None:
                    chain.append(f"{type(error).__name__}: {error}")
                    error = error.__cause__
                assert any("already started" in item.lower() for item in chain), chain
                # The blocker was never adopted: it is still RUNNING as its own top-level
                # execution (TERMINATE parent-close never reached it).
                description = await blocker.describe()
                assert description.status.name == "RUNNING"
            finally:
                await blocker.terminate("test cleanup")
                await _best_effort_delete_executions(
                    env.client, [parent_id, f"{parent_id}.assess_one"]
                )


# ---------------------------------------------------------------------------
# Transitive-closure admission (#55 slice 5, governance closure)
# ---------------------------------------------------------------------------


def _setup_closure_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child_provider: str,
    pkg: str = "closure_project",
) -> Path:
    """A parent (`fake` provider) that references a child whose provider is
    ``child_provider`` — so a policy that allows only `fake` passes the parent's own
    checks but must reject it transitively when the child diverges."""
    for name in tuple(sys.modules):
        if name == pkg or name.startswith(f"{pkg}."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    package = tmp_path / pkg
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class Claim(BaseModel):
            claim_id: str
            text: str


        class Verdict(BaseModel):
            claim_id: str
            substantiated: bool
        """,
    )
    _write(
        tmp_path / "child.yaml",
        f"""
        project: {pkg}
        name: assess_child
        task_queue: closure-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              assess: assess {{{{text}}}}
          provider:
            type: {child_provider}
          observability:
            type: none
        activities:
          definitions:
            - name: assess
              input: schemas:Claim
              output: schemas:Verdict
              prompt: assess
        workflow:
          name: AssessChildWorkflow
          input: schemas:Claim
          output: schemas:Verdict
          steps:
            - id: assess
              activity: assess
        """,
    )
    _write(
        tmp_path / "parent.yaml",
        f"""
        project: {pkg}
        name: review_parent
        task_queue: closure-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
            type: inline
            prompts:
              p: p
          provider:
            type: fake
          observability:
            type: none
        activities:
          definitions: []
        workflow:
          name: ReviewParentWorkflow
          input: schemas:Claim
          output: schemas:Verdict
          steps:
            - id: assess_one
              workflow: child
        """,
    )
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        """
        version: "1"
        name: closure-demo
        workflows:
          - id: child
            path: child.yaml
          - id: parent
            path: parent.yaml
        environments:
          local: environments/local.yaml
        policies:
          only_fake: policies/only_fake.yaml
        """,
    )
    _write(tmp_path / "environments" / "local.yaml", 'version: "1"\nname: local\n')
    _write(
        tmp_path / "policies" / "only_fake.yaml",
        """
        version: "1"
        name: only_fake
        providers:
          allowed:
            fake: {}
        """,
    )
    return manifest


def _closure_check(tmp_path, monkeypatch, *, child_provider: str):
    from typeflux.project.policy import compose_project_policies

    manifest = _setup_closure_project(tmp_path, monkeypatch, child_provider=child_provider)
    project = load_project_spec(manifest)
    policy = compose_project_policies(project, ["only_fake"])
    parent = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    child = resolve_project_workflow(project, workflow_id="child", environment_id="local")
    return project, policy, parent, child


def test_subworkflow_closure_admission_rejects_noncompliant_child(tmp_path, monkeypatch) -> None:
    from typeflux.project.policy_enforcement import (
        validate_subworkflow_closure_policy,
    )

    # The child uses `openai`, which the parent's `only_fake` policy forbids: the
    # parent's own provider check passes (it uses `fake`), but admitting the parent
    # must reject the composed program because the referenced child diverges.
    project, policy, parent, _child = _closure_check(tmp_path, monkeypatch, child_provider="openai")
    check = validate_subworkflow_closure_policy(
        project=project, resolved=parent, policy=policy, environment_id="local"
    )
    assert check is not None
    assert check.code == "policy_subworkflow_closure"
    assert check.status == "failed"
    assert "child" in check.message
    assert "openai" in check.message
    assert check.details["referenced_workflows"] == ["child"]


def test_subworkflow_closure_admission_passes_compliant_child(tmp_path, monkeypatch) -> None:
    from typeflux.project.policy_enforcement import (
        validate_subworkflow_closure_policy,
    )

    # The child also uses `fake` — the whole composed program is compliant.
    project, policy, parent, _child = _closure_check(tmp_path, monkeypatch, child_provider="fake")
    check = validate_subworkflow_closure_policy(
        project=project, resolved=parent, policy=policy, environment_id="local"
    )
    assert check is not None
    assert check.status == "passed"
    assert check.details["referenced_workflows"] == ["child"]


def test_subworkflow_closure_admission_fails_closed_on_unresolvable_child(
    tmp_path, monkeypatch
) -> None:
    from typeflux.project.policy import compose_project_policies
    from typeflux.project.policy_enforcement import (
        validate_subworkflow_closure_policy,
    )

    # A parent that references a sub-workflow id not in the manifest must fail closed:
    # the closure check is the runtime guard's only view of the sub-workflow tree, so a
    # dangling reference cannot silently pass.
    manifest = _setup_closure_project(tmp_path, monkeypatch, child_provider="fake")
    project = load_project_spec(manifest)
    policy = compose_project_policies(project, ["only_fake"])
    parent = resolve_project_workflow(project, workflow_id="parent", environment_id="local")
    # Point the parent at a non-existent child by rewriting its resolved spec's step.
    parent.spec.workflow.steps[0].workflow = "ghost_child"
    check = validate_subworkflow_closure_policy(
        project=project, resolved=parent, policy=policy, environment_id="local"
    )
    assert check is not None
    assert check.status == "failed"
    assert "ghost_child" in check.message
    assert "ghost_child" in " ".join(check.details["unresolved_references"])


def test_subworkflow_closure_admission_absent_without_references(tmp_path, monkeypatch) -> None:
    from typeflux.project.policy import compose_project_policies
    from typeflux.project.policy_enforcement import (
        validate_subworkflow_closure_policy,
    )

    # A workflow with no sub-workflow references emits no closure check (the V1 path —
    # its validate output stays byte-identical).
    manifest = _setup_closure_project(tmp_path, monkeypatch, child_provider="fake")
    project = load_project_spec(manifest)
    policy = compose_project_policies(project, ["only_fake"])
    child = resolve_project_workflow(project, workflow_id="child", environment_id="local")
    check = validate_subworkflow_closure_policy(
        project=project, resolved=child, policy=policy, environment_id="local"
    )
    assert check is None


def test_child_inherits_parent_subject_ids_search_attribute() -> None:
    # #715 slice 1: a child inherits the PARENT's subject ids into its own
    # TypefluxSubjectIds (children of a subject's review are that subject's data).
    # The parent reads its own subjects off search attributes at runtime, so this
    # is exercised via the runtime helper with a fake workflow info.
    from temporalio.common import (
        SearchAttributeKey,
        SearchAttributePair,
        TypedSearchAttributes,
    )

    from typeflux.core.subjects import SUBJECT_IDS_SEARCH_ATTRIBUTE
    from typeflux.yaml.workflow import (
        _child_search_attributes_with_subjects,
        _parent_subject_ids,
    )

    subject_key = SearchAttributeKey.for_keyword_list(SUBJECT_IDS_SEARCH_ATTRIBUTE)

    class _Info:
        typed_search_attributes = TypedSearchAttributes(
            [
                SearchAttributePair(SearchAttributeKey.for_keyword("TypefluxWorkflow"), "ParentWf"),
                SearchAttributePair(subject_key, ["pt-1", "pt-2"]),
            ]
        )

    info = _Info()
    assert _parent_subject_ids(info) == ("pt-1", "pt-2")

    # The child's own logical-name attribute (baked at generation time) is preserved;
    # the inherited subjects are merged in.
    base = TypedSearchAttributes(
        [SearchAttributePair(SearchAttributeKey.for_keyword("TypefluxWorkflow"), "ChildWf")]
    )
    merged = _child_search_attributes_with_subjects(info, base)
    names = {pair.key.name: pair.value for pair in merged}
    assert names["TypefluxWorkflow"] == "ChildWf"
    assert names[SUBJECT_IDS_SEARCH_ATTRIBUTE] == ["pt-1", "pt-2"]


def test_child_without_parent_subjects_leaves_search_attributes_untouched() -> None:
    from temporalio.common import (
        SearchAttributeKey,
        SearchAttributePair,
        TypedSearchAttributes,
    )

    from typeflux.yaml.workflow import _child_search_attributes_with_subjects

    class _Info:
        typed_search_attributes = TypedSearchAttributes([])

    base = TypedSearchAttributes(
        [SearchAttributePair(SearchAttributeKey.for_keyword("TypefluxWorkflow"), "ChildWf")]
    )
    # No parent subjects ⇒ the base is returned unchanged (identity), so a
    # subject-free composition stays byte-identical to pre-#715 behavior.
    assert _child_search_attributes_with_subjects(_Info(), base) is base
