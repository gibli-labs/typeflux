"""The Resolver seam: resolution behind the contract interface (#619).

``contracts/resolver/resolver.v1.json`` names resolution — importing a
project's schema/activity modules to produce contract DTOs — as the one
genuinely language-bound control-plane layer. This module binds that
interface in-process for the Python runtime.

In-process binding note: the contract's ``{ok, result} | {ok: false,
error}`` envelope is the *transport* shape (the subprocess/network resolver
of the epic's slice 5 wraps it); in-process, exceptions are the error
channel and the API's exception handlers map them to the same ``ApiError``
wire objects the envelope would carry.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import threading
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from typeflux.core.errors import TypefluxError
from typeflux.project import (
    ActivityCatalog,
    ProjectValidationReport,
    ResolvedWorkflowBundle,
    WorkflowPromptStatus,
    load_project_spec,
    resolve_activity_catalog,
    resolve_workflow_bundle,
    validate_project_bundle,
    workflow_prompt_status,
)


class ResolvedPlan(BaseModel):
    """The ``resolve_plan`` response (resolver.v1.json, #642).

    The raw plan a ts-plan-argument start dispatches as its first workflow
    argument, plus the start identity. ``plan`` is OPAQUE to this edition —
    the control plane dispatches it verbatim; only the runtime that produced
    it interprets it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: dict[str, Any]
    task_queue: str
    spec_digest: str
    workflow_name: str
    version_label: str | None = None
    #: Spliced into a visibility query as an IDENTIFIER downstream (the frozen
    #: -version scan), so the wire boundary re-validates the spec's shape rule —
    #: a third-party resolver build must not be able to smuggle query syntax.
    search_attribute: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")] | None
    ) = None


class PlanNotDispatchableError(TypefluxError, ValueError):
    """``resolve_plan`` addressed at a runtime whose binding profile dispatches no plan.

    The contract's profile_note: a rejection IS the operation's implementation
    for such runtimes. ``ValueError``-rooted so the API maps it to 422.
    """


class ResolverTransportError(TypefluxError):
    """A CONFIGURED subprocess resolver failed at the transport layer.

    Dead/unresponsive process, a non-JSON line, or a payload that does not
    validate as the operation's DTO. Deliberately NOT a ``ValueError``: the
    operator wired a resolver that is not serving — a 500-class server fault
    (the contract's runtime_note), never a silent 501.
    """


class ResolverOperationError(TypefluxError):
    """A structured ``{ok: false}`` resolver failure, forwarded verbatim.

    Carries the HTTP ``status`` and ``ApiError`` fields the resolver's own
    edition would have served — the API forwards them instead of translating
    (the contract's error_shape).
    """

    def __init__(self, *, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


@runtime_checkable
class Resolver(Protocol):
    """The four resolution operations, exactly as the contract names them.

    A resolver resolves projects of exactly one language runtime (its own),
    declared via ``runtime``: the app derives its supported-runtime gate from
    the resolver it serves with, so an injected resolver's projects are
    honestly resolvable and everything else fails closed (501
    ``UnsupportedRuntime``). Per-runtime multiplexing arrives with the
    subprocess-resolver slice.
    """

    runtime: str

    def resolve_bundle(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...] = (),
        deployment_image: str | None = None,
    ) -> ResolvedWorkflowBundle: ...

    def resolve_catalog(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> ActivityCatalog: ...

    def validate_project(
        self,
        manifest_path: str,
        *,
        environment_id: str | None = None,
        workflow_ids: tuple[str, ...] = (),
        policy_ids: tuple[str, ...] = (),
    ) -> ProjectValidationReport: ...

    def prompt_status(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> WorkflowPromptStatus: ...

    def resolve_plan(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> ResolvedPlan: ...


class InProcessPythonResolver:
    """The Python runtime's resolver: load the manifest, resolve in-process.

    Deliberately a black box over ``manifest_path`` (no preloaded-spec
    shortcut): the caller's request-guard load and this load read the same
    file microseconds apart, preserving the per-request freshness contract
    while keeping the seam transport-shaped.
    """

    runtime = "python"

    def resolve_bundle(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...] = (),
        deployment_image: str | None = None,
    ) -> ResolvedWorkflowBundle:
        return resolve_workflow_bundle(
            load_project_spec(manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
            policy_ids=policy_ids,
            deployment_image=deployment_image,
        )

    def resolve_catalog(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> ActivityCatalog:
        return resolve_activity_catalog(
            load_project_spec(manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
        )

    def validate_project(
        self,
        manifest_path: str,
        *,
        environment_id: str | None = None,
        workflow_ids: tuple[str, ...] = (),
        policy_ids: tuple[str, ...] = (),
    ) -> ProjectValidationReport:
        return validate_project_bundle(
            load_project_spec(manifest_path),
            environment_id=environment_id,
            workflow_ids=workflow_ids,
            policy_ids=policy_ids,
        )

    def prompt_status(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> WorkflowPromptStatus:
        return workflow_prompt_status(
            load_project_spec(manifest_path),
            workflow_id=workflow_id,
            environment_id=environment_id,
        )

    def resolve_plan(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> ResolvedPlan:
        # The contract's profile_note: python-versioned-type registers the plan
        # into a versioned workflow type (its digest algorithm is
        # typeflux-yaml-graph-v1, not the plan's typeflux-yaml-plan-v1 — the
        # ABIs are deliberately divergent, #618). There is no plan argument to
        # resolve; the rejection IS this operation's Python implementation.
        raise PlanNotDispatchableError(
            "resolve_plan is not applicable to the python runtime: its binding "
            "profile (python-versioned-type) registers the plan into a versioned "
            "workflow type and dispatches [input] only — no plan travels as a "
            "start argument (resolver.v1.json profile_note)"
        )


class _ResolverExited(Exception):
    """Internal: the subprocess died mid-exchange (retry-once signal, never public)."""


class SubprocessResolver:
    """A resolver for a FOREIGN runtime, spoken to over stdio (#642).

    Spawns the configured command (e.g. ``node .../dist/resolver-stdio.js``)
    and exchanges the contract's newline-delimited JSON envelope: one boot
    ``{"event": "ready", ...}`` line, then one request per line and exactly
    one response per line, in order. The process is long-lived and lazily
    spawned; a dead process is respawned once per request, then fails as a
    :class:`ResolverTransportError` (a 500-class server fault — the operator
    configured a resolver that is not serving, never a silent 501).

    Results validate into the SAME pydantic DTOs the in-process resolver
    returns — cross-edition DTO parity is what the conformance suite enforces.
    A structured ``{ok: false}`` failure raises :class:`ResolverOperationError`
    carrying the wire ``status`` + ``ApiError`` verbatim for the API to forward.

    Requests SERIALIZE through one lock per resolver: the stdio stream pairs
    responses by order, so one in-flight request at a time is the transport's
    correctness model (a slow resolution delays the queue behind it, bounded
    by ``request_timeout_seconds``). Scale-out is more processes, not more
    threads on one pipe.
    """

    def __init__(
        self,
        command: str | list[str],
        *,
        runtime: str = "typescript",
        cwd: str | None = None,
        ready_timeout_seconds: float = 30.0,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        self.runtime = runtime
        self._command = shlex.split(command) if isinstance(command, str) else list(command)
        self._cwd = cwd
        self._ready_timeout = ready_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()

    # -- transport -----------------------------------------------------------

    def _spawn(self) -> None:
        self._lines = queue.Queue()
        try:
            self._process = subprocess.Popen(
                self._command,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # diagnostics pass through to the server's stderr
                text=True,
                # Its own process group so teardown can kill the whole tree
                # (the live-harness precedent).
                start_new_session=True,
            )
        except OSError as exc:
            raise ResolverTransportError(
                f"failed to spawn the {self.runtime} resolver ({self._command[0]!r}): {exc}"
            ) from exc
        process = self._process
        assert process.stdout is not None
        # The pump CAPTURES its own queue: a prior (killed) process's straggler
        # thread must keep writing to ITS queue, never the fresh one a respawn
        # installed on self — otherwise a late line/EOF sentinel would corrupt
        # the new process's request/response pairing.
        lines = self._lines
        stdout = process.stdout

        def _pump() -> None:
            # Push every stdout line (and a final EOF sentinel) to the queue so
            # request reads can time out without blocking on the pipe.
            for line in stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=_pump, daemon=True).start()

        try:
            ready = self._next_line(self._ready_timeout, what="the readiness line")
        except _ResolverExited as exc:
            # A process that dies before announcing readiness is a spawn
            # failure, not a retryable mid-request death — never leak the
            # internal signal type past this boundary.
            raise ResolverTransportError(str(exc)) from exc
        try:
            event = json.loads(ready)
        except json.JSONDecodeError as exc:
            self._kill()
            raise ResolverTransportError(
                f"the {self.runtime} resolver's first line is not JSON: {ready!r}"
            ) from exc
        if event.get("event") != "ready" or event.get("runtime") != self.runtime:
            self._kill()
            raise ResolverTransportError(
                f"the configured resolver announced {event!r}, not a ready {self.runtime} resolver"
            )

    def _next_line(self, timeout: float, *, what: str) -> str:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            raise ResolverTransportError(
                f"the {self.runtime} resolver did not produce {what} within {timeout:g}s"
            ) from None
        if line is None:
            self._kill()
            raise _ResolverExited(f"the {self.runtime} resolver exited before producing {what}")
        return line

    def _kill(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            # REAP the SIGKILLed child — without this wait the zombie holds a
            # PID slot until interpreter exit (Popen.__del__ is not a contract).
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def close(self) -> None:
        """Terminate the subprocess (idempotent)."""
        with self._lock:
            self._kill()

    def _request(self, operation: str, params: dict[str, Any]) -> Any:
        payload = json.dumps({"operation": operation, "params": params})
        line: str | None = None
        with self._lock:
            # Lazy spawn + one retry against a fresh process when the current
            # one is dead (a broken pipe or an EOF mid-request — `poll()` can
            # lag the actual death, so pre-checking alone is racy). Retrying is
            # safe: every resolver operation is a pure read. A TIMEOUT is never
            # retried (the resolver is too slow, not dead — doubling the wait
            # helps nobody). A SECOND death in the same request surfaces.
            for attempt in (1, 2):
                if self._process is None or self._process.poll() is not None:
                    self._kill()
                    self._spawn()
                process = self._process
                assert process is not None and process.stdin is not None
                try:
                    process.stdin.write(payload + "\n")
                    process.stdin.flush()
                    line = self._next_line(self._request_timeout, what=f"a response to {operation}")
                    break
                except (BrokenPipeError, OSError, _ResolverExited) as exc:
                    self._kill()
                    if attempt == 2:
                        raise ResolverTransportError(
                            f"the {self.runtime} resolver died serving {operation} "
                            f"(twice, once after a respawn): {exc}"
                        ) from exc
        assert line is not None
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            self.close()
            raise ResolverTransportError(
                f"the {self.runtime} resolver answered {operation} with a non-JSON line"
            ) from exc
        if not isinstance(response, dict):
            self.close()
            raise ResolverTransportError(
                f"the {self.runtime} resolver answered {operation} with a non-object payload"
            )
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error")
        error_name = error.get("error") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        status = response.get("status")
        raise ResolverOperationError(
            status=status if isinstance(status, int) else 500,
            error=error_name if isinstance(error_name, str) else "ResolverError",
            message=message if isinstance(message, str) else "resolver operation failed",
        )

    def _validated(self, operation: str, params: dict[str, Any], model: type[Any]) -> Any:
        result = self._request(operation, params)
        try:
            return model.model_validate(result)
        except ValidationError as exc:
            raise ResolverTransportError(
                f"the {self.runtime} resolver's {operation} result does not validate "
                f"as {model.__name__}: {exc}"
            ) from exc

    # -- the contract operations ----------------------------------------------

    def resolve_bundle(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
        policy_ids: tuple[str, ...] = (),
        deployment_image: str | None = None,
    ) -> ResolvedWorkflowBundle:
        return self._validated(
            "resolve_bundle",
            {
                "manifest_path": manifest_path,
                "workflow_id": workflow_id,
                "environment_id": environment_id,
                "policy_ids": list(policy_ids),
                "deployment_image": deployment_image,
            },
            ResolvedWorkflowBundle,
        )

    def resolve_catalog(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> ActivityCatalog:
        return self._validated(
            "resolve_catalog",
            {
                "manifest_path": manifest_path,
                "workflow_id": workflow_id,
                "environment_id": environment_id,
            },
            ActivityCatalog,
        )

    def validate_project(
        self,
        manifest_path: str,
        *,
        environment_id: str | None = None,
        workflow_ids: tuple[str, ...] = (),
        policy_ids: tuple[str, ...] = (),
    ) -> ProjectValidationReport:
        return self._validated(
            "validate_project",
            {
                "manifest_path": manifest_path,
                "environment_id": environment_id,
                "workflow_ids": list(workflow_ids),
                "policy_ids": list(policy_ids),
            },
            ProjectValidationReport,
        )

    def prompt_status(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> WorkflowPromptStatus:
        return self._validated(
            "prompt_status",
            {
                "manifest_path": manifest_path,
                "workflow_id": workflow_id,
                "environment_id": environment_id,
            },
            WorkflowPromptStatus,
        )

    def resolve_plan(
        self,
        manifest_path: str,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> ResolvedPlan:
        return self._validated(
            "resolve_plan",
            {
                "manifest_path": manifest_path,
                "workflow_id": workflow_id,
                "environment_id": environment_id,
            },
            ResolvedPlan,
        )


__all__ = [
    "InProcessPythonResolver",
    "PlanNotDispatchableError",
    "ResolvedPlan",
    "Resolver",
    "ResolverOperationError",
    "ResolverTransportError",
    "SubprocessResolver",
]
