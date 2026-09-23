"""The subprocess resolver transport (#642).

Unit-level: a FAKE resolver process (``python -c`` speaking the contract's
newline-delimited JSON envelope — no Node required, so these run in CI) proves
the client's protocol handling: readiness, DTO validation, verbatim error
forwarding, deadlines, crash + respawn. The real Node resolver is exercised by
the node-gated integration tests and the live e2e.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from typeflux.controlplane.resolver import (
    InProcessPythonResolver,
    PlanNotDispatchableError,
    ResolvedPlan,
    ResolverOperationError,
    ResolverTransportError,
    SubprocessResolver,
)

# A scripted resolver: answers the contract envelope per operation. `resolve_bundle`
# is scripted to FAIL with a structured 404 so forwarding is observable.
FAKE_RESOLVER = textwrap.dedent(
    """
    import json, sys
    print(json.dumps({"event": "ready", "resolver_version": "1", "runtime": "typescript"}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        op = req["operation"]
        params = req["params"]
        if op == "resolve_plan":
            out = {"ok": True, "result": {
                "plan": {"workflow": "W", "steps": [{"id": "s", "kind": "activity"}]},
                "task_queue": "ts-queue",
                "spec_digest": "abc123def456",
                "workflow_name": "W",
                "version_label": None,
                "search_attribute": None,
            }}
        elif op == "validate_project":
            out = {"ok": True, "result": {
                "project_name": "conformance-fixture",
                "manifest_path": params["manifest_path"],
                "ok": True,
            }}
        elif op == "resolve_bundle":
            out = {"ok": False, "status": 404,
                   "error": {"error": "NotFound", "message": "unknown project workflow: nope"}}
        elif op == "prompt_status":
            out = {"ok": True, "result": {"bogus": "shape"}}
        else:
            out = {"ok": False, "status": 422,
                   "error": {"error": "InvalidRequest", "message": f"unknown resolver operation: {op}"}}
        print(json.dumps(out), flush=True)
    """
)


def _fake(script: str = FAKE_RESOLVER, **kwargs) -> SubprocessResolver:
    return SubprocessResolver([sys.executable, "-u", "-c", script], **kwargs)


def test_happy_path_validates_the_wire_result_into_the_dto() -> None:
    resolver = _fake()
    try:
        report = resolver.validate_project("/tmp/x/typeflux.project.yaml")
        assert report.ok is True
        assert report.project_name == "conformance-fixture"
    finally:
        resolver.close()


def test_resolve_plan_round_trips_the_opaque_plan() -> None:
    resolver = _fake()
    try:
        resolved = resolver.resolve_plan(
            "/tmp/x/typeflux.project.yaml", workflow_id="flow", environment_id="local"
        )
        assert isinstance(resolved, ResolvedPlan)
        assert resolved.plan == {"workflow": "W", "steps": [{"id": "s", "kind": "activity"}]}
        assert resolved.task_queue == "ts-queue"
        assert resolved.spec_digest == "abc123def456"
    finally:
        resolver.close()


def test_structured_failures_forward_status_and_apierror_verbatim() -> None:
    resolver = _fake()
    try:
        with pytest.raises(ResolverOperationError) as excinfo:
            resolver.resolve_bundle(
                "/tmp/x/typeflux.project.yaml", workflow_id="nope", environment_id="local"
            )
        assert excinfo.value.status == 404
        assert excinfo.value.error == "NotFound"
        assert excinfo.value.message == "unknown project workflow: nope"
    finally:
        resolver.close()


def test_a_result_that_does_not_validate_is_a_transport_fault() -> None:
    # prompt_status is scripted to answer a bogus shape: cross-edition DTO
    # drift must surface as a loud 500-class fault, never a partial DTO.
    resolver = _fake()
    try:
        with pytest.raises(ResolverTransportError, match="does not validate"):
            resolver.prompt_status(
                "/tmp/x/typeflux.project.yaml", workflow_id="flow", environment_id="local"
            )
    finally:
        resolver.close()


def test_a_resolver_that_never_readies_times_out() -> None:
    resolver = _fake("import time\ntime.sleep(60)\n", ready_timeout_seconds=0.5)
    try:
        with pytest.raises(ResolverTransportError, match="readiness line"):
            resolver.validate_project("/tmp/x/typeflux.project.yaml")
    finally:
        resolver.close()


def test_a_wrong_runtime_announcement_fails_closed() -> None:
    script = 'import json\nprint(json.dumps({"event": "ready", "resolver_version": "1", "runtime": "python"}), flush=True)\n'
    resolver = _fake(script)
    try:
        with pytest.raises(ResolverTransportError, match="not a ready typescript resolver"):
            resolver.validate_project("/tmp/x/typeflux.project.yaml")
    finally:
        resolver.close()


def test_a_dead_process_is_respawned_once_per_request() -> None:
    # The script answers exactly ONE request then exits: request 1 succeeds,
    # the process dies, request 2 finds it dead and respawns a fresh one.
    one_shot = textwrap.dedent(
        """
        import json, sys
        print(json.dumps({"event": "ready", "resolver_version": "1", "runtime": "typescript"}), flush=True)
        line = sys.stdin.readline()
        req = json.loads(line)
        out = {"ok": True, "result": {"project_name": "p", "manifest_path": req["params"]["manifest_path"], "ok": True}}
        print(json.dumps(out), flush=True)
        """
    )
    resolver = _fake(one_shot)
    try:
        assert resolver.validate_project("/tmp/x/typeflux.project.yaml").ok is True
        assert resolver.validate_project("/tmp/x/typeflux.project.yaml").ok is True
    finally:
        resolver.close()


def test_a_request_deadline_kills_and_faults() -> None:
    stalled = textwrap.dedent(
        """
        import json, sys, time
        print(json.dumps({"event": "ready", "resolver_version": "1", "runtime": "typescript"}), flush=True)
        sys.stdin.readline()
        time.sleep(60)
        """
    )
    resolver = _fake(stalled, request_timeout_seconds=0.5)
    try:
        with pytest.raises(ResolverTransportError, match="did not produce a response"):
            resolver.validate_project("/tmp/x/typeflux.project.yaml")
    finally:
        resolver.close()


def test_a_non_json_response_line_is_a_transport_fault() -> None:
    garbled = textwrap.dedent(
        """
        import json, sys
        print(json.dumps({"event": "ready", "resolver_version": "1", "runtime": "typescript"}), flush=True)
        sys.stdin.readline()
        print("not json", flush=True)
        """
    )
    resolver = _fake(garbled)
    try:
        with pytest.raises(ResolverTransportError, match="non-JSON"):
            resolver.validate_project("/tmp/x/typeflux.project.yaml")
    finally:
        resolver.close()


def test_close_is_idempotent() -> None:
    resolver = _fake()
    assert resolver.validate_project("/tmp/x/typeflux.project.yaml").ok is True
    resolver.close()
    resolver.close()


def test_the_python_resolver_rejects_resolve_plan_as_the_contract_documents() -> None:
    # The profile_note: python-versioned-type dispatches no plan — the
    # rejection IS the operation's Python implementation (422-class).
    with pytest.raises(PlanNotDispatchableError, match="no plan travels as a"):
        InProcessPythonResolver().resolve_plan(
            "/tmp/x/typeflux.project.yaml", workflow_id="w", environment_id="local"
        )


def test_duplicate_runtime_resolvers_fail_at_startup(tmp_path, monkeypatch) -> None:
    from tests.test_controlplane_api import _setup
    from typeflux.controlplane import create_app

    manifest = _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="duplicate resolver for runtime"):
        create_app(
            manifest,
            resolvers=[InProcessPythonResolver(), InProcessPythonResolver()],
        )
