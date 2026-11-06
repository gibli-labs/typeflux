"""Run the explicitly selected local Docker target; never publish or change cloud resources.

Build with prepare.py and the three Dockerfiles first. Each invocation gets a new
output directory, frozen input manifest and owned resource ledger. Failed attempts
remain failed, including when cleanup fails. This is deployment test orchestration,
not an application runtime, release authorization or trusted CI attestation.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

LABEL = "devloop.deployed-review"
TEMPORAL_IMAGE = (
    "temporalio/temporal@sha256:18667097470f91f31042a646926f84b8672693dc94f0e7384d22595ce356b55c"
)


def write(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Target:
    def __init__(self, output: Path, context: str):
        output.mkdir()  # Refuse to erase/reuse an earlier attempt.
        self.output = output
        self.context = context
        self.owner = uuid4().hex
        self.resources: list[dict] = []
        self.commands = 0
        self.node_image = ""
        self.artifacts = Path()
        self.python_lock_sha = ""

    def docker(self, *args: str, timeout: float = 60, check: bool = True) -> str:
        self.commands += 1
        result = subprocess.run(
            ["docker", "--context", self.context, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # Local diagnostic logs may include fixed fake fixture data. Never publish them wholesale.
        (self.output / f"command-{self.commands}.stdout").write_text(result.stdout)
        (self.output / f"command-{self.commands}.stderr").write_text(result.stderr)
        if check and result.returncode:
            raise RuntimeError(f"docker-command-{self.commands}-failed-{result.returncode}")
        return result.stdout.strip()

    def save(self) -> None:
        write(
            self.output / "resources.json",
            {"owner": self.owner, "context": self.context, "resources": self.resources},
        )

    def inspect(self, resource: dict) -> dict:
        kind = resource["kind"]
        identity = resource.get("id", resource["name"])
        raw = self.docker(kind, "inspect", identity)
        info = json.loads(raw)[0]
        labels = (
            info.get("Config", {}).get("Labels", {})
            if kind == "container"
            else info.get("Labels", {})
        ) or {}
        if labels.get(LABEL) != self.owner or info["Name"].lstrip("/") != resource["name"]:
            raise RuntimeError("resource-ownership-mismatch")
        if resource.get("id") and info.get("Id", info["Name"]) != resource["id"]:
            raise RuntimeError("resource-identity-mismatch")
        return info

    def create(self, kind: str, role: str, *args: str) -> dict:
        name = f"typeflux-review-{self.owner[:12]}-{role}"
        resource = {"kind": kind, "role": role, "name": name, "state": "pending"}
        self.resources.append(resource)
        self.save()  # Pending external effect before create; never blindly retry.
        try:
            if kind == "container":
                self.docker(
                    "container",
                    "create",
                    "--name",
                    name,
                    "--label",
                    f"{LABEL}={self.owner}",
                    "--init",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "256",
                    "--cpus",
                    "2",
                    "--memory",
                    "2g",
                    *args,
                )
            else:
                self.docker(kind, "create", "--label", f"{LABEL}={self.owner}", *args, name)
            info = self.inspect(resource)
            resource.update(id=info.get("Id", info["Name"]), state="created")
            self.save()
        except BaseException:
            # Reconcile an ambiguous create once for safe cleanup; the attempt still fails.
            try:
                info = self.inspect(resource)
                resource.update(id=info.get("Id", info["Name"]), state="created-after-error")
            except Exception:
                resource["state"] = "create-unknown"
            self.save()
            raise
        return resource

    def start(self, resource: dict) -> None:
        self.inspect(resource)
        self.docker("container", "start", resource["id"])

    def stop(self, resource: dict, *, kill: bool = False) -> None:
        self.inspect(resource)
        if kill:
            self.docker("container", "kill", "--signal", "KILL", resource["id"])
        else:
            self.docker("container", "stop", "--time", "10", resource["id"])

    def port(self, resource: dict, container_port: str) -> str:
        info = self.inspect(resource)
        entries = info["NetworkSettings"]["Ports"][container_port]
        if len(entries) != 1 or entries[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("unexpected-host-port-binding")
        return f"http://127.0.0.1:{entries[0]['HostPort']}"

    def cleanup(self) -> list[str]:
        failures = []
        for resource in reversed(self.resources):
            if resource["state"] == "removed":
                continue
            try:
                self.inspect(resource)
                if resource["kind"] == "container":
                    self.docker("container", "logs", resource["id"], check=False)
                    self.docker("container", "rm", "--force", resource["id"])
                else:
                    self.docker(resource["kind"], "rm", resource["id"])
                resource["state"] = "removed"
            except Exception:
                failures.append(resource["role"])
            self.save()
        return failures


def request(
    base: str,
    path: str,
    body: dict | None = None,
    *,
    token: str = "test-operator",
    expected: int = 200,
) -> dict:
    req = urllib.request.Request(
        base + path,
        method="GET" if body is None else "POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            status, data = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, data = exc.code, exc.read()
    if status != expected:
        raise AssertionError(f"unexpected-http-status-{status}-expected-{expected}: {data[:500]!r}")
    if not data:
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        if status < 400:
            raise
        return {"httpStatus": status, "bodyFormat": "non-json"}


def poll(operation, predicate, *, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        value = operation()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("observation-window-exceeded")
        time.sleep(0.25)


def ready(base: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            request(base, "/api/v1/meta")
            return
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise TimeoutError("api-startup-window-exceeded") from None
            time.sleep(0.25)


def execute(
    target: Target,
    image: str,
    network: dict | None,
    role: str,
    command: list[str],
    *,
    mounts: list[str] | None = None,
) -> str:
    args = [
        "--network",
        network["name"] if network else "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=256m",
    ]
    for mount in mounts or []:
        args += ["--mount", mount]
    container = target.create("container", role, *args, image, *command)
    target.start(container)
    code = target.docker("container", "wait", container["id"], timeout=60)
    logs = target.docker("container", "logs", container["id"])
    info = target.inspect(container)
    if code != "0" or info["State"].get("OOMKilled"):
        raise RuntimeError(f"consumer-{role}-failed-{code}")
    return logs


def installation(target: Target, edition: str, image: str, *, base: bool = False) -> dict:
    command = (
        ["python", "/app/installation.py"]
        if edition == "python"
        else ["node", "/app/installation.mjs"]
    )
    record = json.loads(
        execute(
            target,
            image,
            None,
            edition + ("-base" if base else "-native") + "-installation",
            command,
        ).splitlines()[-1]
    )
    artifacts = [
        p for p in target.artifacts.iterdir() if p.is_file() and not p.name.startswith(".")
    ]
    if edition == "python":
        wheels = [p for p in artifacts if p.suffix == ".whl"]
        assert len(wheels) == 1
        expected = {p.name: sha(p) for p in wheels}
        with ZipFile(wheels[0]) as wheel:
            assert (
                record["sdkEntrySha256"]
                == hashlib.sha256(wheel.read("typeflux/__init__.py")).hexdigest()
            )
        assert record["dependencyLocks"]["uv.lock"] == target.python_lock_sha
        assert (record["versions"]["temporalio"] is None) == base
        assert record["temporalImportable"] is (not base)
    else:
        selected = (
            [
                p
                for p in artifacts
                if p.name in {"typeflux-temporal-0.1.0.tgz", "typeflux-yaml-0.1.0.tgz"}
            ]
            if base
            else artifacts
        )
        expected = {p.name: sha(p) for p in selected}
        for path in selected:
            if path.suffix != ".tgz":
                continue
            with tarfile.open(path) as package:
                metadata = json.loads(package.extractfile("package/package.json").read())
                name = metadata["name"]
                assert record["versions"][name] == metadata["version"]
                entry = record["entries"][name]
                prefix = "/app/node_modules/" + name + "/"
                assert entry["path"].startswith(prefix)
                member = "package/" + entry["path"][len(prefix) :]
                assert (
                    entry["sha256"]
                    == hashlib.sha256(package.extractfile(member).read()).hexdigest()
                )
        assert (record["versions"]["@temporalio/worker"] is None) == base
        assert bool(record["temporalPackages"]) is (not base)
    assert record["candidateFiles"] == expected
    return record


def journey(
    target: Target,
    edition: str,
    image: str,
    console_image: str,
    network: dict,
    frontend: dict,
    temporal: dict,
    module_root: Path,
) -> dict:
    installed_sdk = installation(target, edition, image)
    prefix = "/api/v1/projects/deployment/workflows/"
    data = target.create("volume", edition + "-data")
    mounts = [f"type=volume,src={data['name']},dst=/data"]
    mount_args = ["--mount", mounts[0]]
    base_command = (
        ["python", "-m", "typeflux.project", "run"]
        if edition == "python"
        else ["/app/node_modules/.bin/typeflux-yaml-worker"]
    )

    def worker(role, workflow="workflow"):
        manifest = (
            "typeflux.project.yaml"
            if workflow == "workflow"
            else f"{workflow}/typeflux.project.yaml"
        )
        command = [
            *base_command,
            manifest,
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--policy",
            "base",
        ]
        execute(
            target, image, network, role + "-preflight", [*command, "--preflight"], mounts=mounts
        )
        result = target.create(
            "container",
            role,
            "--network",
            network["name"],
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            *mount_args,
            "--env",
            "RETRY_FIXTURE=" + ("1" if workflow == "retry" else "0"),
            image,
            *command,
        )
        target.start(result)
        return result

    api_command = (
        ["python", "-m", "typeflux.controlplane", "serve", "--temporal-timeout", "3"]
        if edition == "python"
        else ["/app/node_modules/.bin/typeflux-controlplane", "serve", "--conformance-schemas"]
    )
    api_command += [
        "--registry",
        "typeflux.projects.yaml",
        "--host",
        "0.0.0.0",
        "--port",
        "8400",
        "--auth-token",
        "operator:*:test-operator",
        "--auth-token",
        "reader:inspect:test-reader",
    ]
    # Journeys are sequential: the previous edition's API is stopped before
    # another container can use this frontend alias. A failed journey aborts.
    api = target.create(
        "container",
        edition + "-api",
        "--network",
        frontend["name"],
        "--network-alias",
        "api",
        "--publish",
        "127.0.0.1::8400",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=128m",
        "--env",
        "TYPEFLUX_CONTROLPLANE_TEMPORAL_TIMEOUT_SECONDS=3",
        *mount_args,
        image,
        *api_command,
    )
    target.inspect(network)
    target.inspect(api)
    target.docker("network", "connect", network["id"], api["id"])
    target.start(api)
    base = target.port(api, "8400/tcp")
    ready(base)
    client_output = execute(
        target,
        target.node_image,
        frontend,
        edition + "-generated-client",
        ["node", "/app/client-consumer.mjs", "DeployedReview" + edition.title()],
    )
    installed_client = json.loads(client_output.splitlines()[-1])
    old = worker(edition + "-old-worker")
    # Static bundle/policy must be admitted against the actual installed server.
    bundle = request(base, prefix + "workflow/bundle?environment_id=local")
    policy_hash = bundle["policy"]["policy_hash"]
    execution = f"deployment-{edition}-{target.owner[:12]}"
    start_body = {
        "environment_id": "local",
        "execution_id": execution,
        "input": {"claims": [{"value": "fixed-test"}]},
        "policy_ids": ["base"],
        "expected_policy_hash": policy_hash,
    }
    request(base, prefix + "workflow/start", start_body, token="test-reader", expected=403)
    request(base, prefix + "workflow/start", {**start_body, "input": {}}, expected=422)
    request(
        base,
        prefix + "workflow/start",
        {**start_body, "expected_policy_hash": "wrong"},
        expected=422,
    )
    receipt = request(base, prefix + "workflow/start", start_body)
    observations = {
        "edition": edition,
        "receipt": receipt,
        "completed": False,
        "installedGeneratedClient": installed_client,
        "installedSdk": installed_sdk,
    }
    observation_path = target.output / f"{edition}-observations.json"
    write(observation_path, observations)
    probe_cmd = (
        ["python", "/app/probe.py"] if edition == "python" else ["node", "/app/node-probe.mjs"]
    )
    # The current Python start receipt legitimately has a nullable run_id. Native
    # describe is read-only reconciliation, never a repeated start or guessed ID.
    run_id = receipt["run_id"]
    if not run_id:
        output = execute(
            target,
            image,
            network,
            edition + "-resolve-run",
            [*probe_cmd, "history", execution, "current"],
            mounts=mounts,
        )
        run_id = json.loads(output.splitlines()[-1])["runId"]
    observations["reconciledRunId"] = run_id
    write(observation_path, observations)
    parameters = urllib.parse.urlencode(
        {"environment_id": "local", "execution_id": execution, "run_id": run_id}
    )

    def status():
        value = request(base, prefix + "workflow/status?" + parameters)
        assert value["run_id"] == run_id
        return value

    gate = poll(status, lambda x: x["status"]["state"] == "waiting_for_review")
    observations["gate"] = gate["status"]
    write(observation_path, observations)
    review_body = {
        "environment_id": "local",
        "execution_id": execution,
        "run_id": run_id,
        "command": {"user_decision": "approve"},
    }
    request(base, prefix + "workflow/review", review_body, token="test-reader", expected=403)
    request(
        base,
        prefix + "workflow/review",
        {**review_body, "command": {"user_decision": []}},
        expected=422,
    )
    request(base, "/api/v1/projects/wrong/workflows/workflow/review", review_body, expected=409)
    request(base, "/api/v1/projects/next/workflows/workflow/review", review_body, expected=409)

    def history(role, action="history", exec_id=execution, selected_run=run_id):
        output = execute(
            target,
            image,
            network,
            edition + "-" + role,
            [*probe_cmd, action, exec_id, selected_run],
            mounts=mounts,
        )
        return json.loads(output.splitlines()[-1])

    before = history("before-recovery")
    observations["before"] = before
    write(observation_path, observations)
    assert before["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED", 0) == 0
    assert before["eventCounts"].get("EVENT_TYPE_ACTIVITY_TASK_COMPLETED") == 1
    # v2 drain must see the in-flight v1. Do not decommission its worker on a false zero.
    version_path = "/api/v1/projects/next/workflows/workflow/versions?environment_id=local"
    draining = poll(
        lambda: request(base, version_path),
        lambda x: x["total_running"] >= 1 and x["drained"] is False,
    )
    observations["drainingBefore"] = draining
    write(observation_path, observations)
    target.stop(old, kill=True)
    target.inspect(temporal)
    target.docker("container", "pause", temporal["id"])
    try:
        started = time.monotonic()
        request(base, prefix + "workflow/status?" + parameters, expected=503)
        outage_seconds = time.monotonic() - started
        assert outage_seconds < 8
        observations["backendPausedWithWorkerStopped"] = {
            "httpStatus": 503,
            "seconds": outage_seconds,
            "workerAlreadyStopped": True,
        }
        write(observation_path, observations)
    finally:
        target.inspect(temporal)
        target.docker("container", "unpause", temporal["id"])
    target.stop(temporal, kill=True)
    target.start(temporal)  # Same owned container, persistent SQLite volume, no replacement server.
    recovered = history("persisted-history")
    observations["persistedAfterServerRestart"] = recovered
    write(observation_path, observations)
    assert recovered["runId"] == run_id and recovered["historyEvents"] >= before["historyEvents"]
    # Reachable backend, no worker: deadline or qualified native no-poller condition
    # must report unavailability, never count as a passing lifecycle read.
    started = time.monotonic()
    request(base, prefix + "workflow/status?" + parameters, expected=503)
    worker_outage_seconds = time.monotonic() - started
    assert worker_outage_seconds < 8
    replacement = worker(edition + "-compatible-replacement")
    restored_gate = poll(status, lambda x: x["status"]["state"] == "waiting_for_review")
    assert restored_gate["status"]["completed_units"] == gate["status"]["completed_units"]
    # Real built-console submission and observed lifecycle completion, with no response stubs.
    console = target.create(
        "container",
        edition + "-console",
        "--network",
        frontend["name"],
        "--publish",
        "127.0.0.1::8080",
        "--user",
        "101:101",
        "--read-only",
        "--tmpfs",
        "/var/cache/nginx:rw,size=64m,mode=1777",
        "--tmpfs",
        "/var/run:rw,size=16m,mode=1777",
        console_image,
    )
    target.start(console)
    console_base = target.port(console, "8080/tcp")
    ready(console_base)
    browser = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("browser.mjs")),
            str(module_root),
            console_base,
            execution,
            run_id,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    (target.output / f"{edition}-browser.stdout").write_text(browser.stdout)
    (target.output / f"{edition}-browser.stderr").write_text(browser.stderr)
    assert browser.returncode == 0, "real-console-journey-failed"
    completed = poll(status, lambda x: x["status"]["state"] == "completed")
    after = history("replay", "replay")
    observations["after"] = after
    write(observation_path, observations)
    assert after["runId"] == run_id and after["status"] == "COMPLETED" and after["replayPassed"]
    assert after["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED") == 1
    assert after["eventCounts"].get("EVENT_TYPE_ACTIVITY_TASK_COMPLETED") == 3
    # Deliberate ID reuse after completion: an explicitly pinned old-run decision
    # must never fall through to the newly running execution with the same ID.
    # Existing adapters expose a generic HTTP 500. Its body cannot establish a
    # native error identity; observe the pinned terminal run separately and retain
    # that limitation rather than attributing any 500 to a specific native cause.
    reused_receipt = request(base, prefix + "workflow/start", start_body)
    reused_run = history("reuse-resolve", selected_run=reused_receipt["run_id"] or "current")[
        "runId"
    ]
    assert reused_run != run_id
    reused_query = urllib.parse.urlencode(
        {"environment_id": "local", "execution_id": execution, "run_id": reused_run}
    )
    poll(
        lambda: request(base, prefix + "workflow/status?" + reused_query),
        lambda x: x["status"]["state"] == "waiting_for_review",
    )
    closed_before = history("original-terminal-before-rejection")
    assert closed_before["runId"] == run_id and closed_before["status"] == "COMPLETED"
    closed_rejection = request(base, prefix + "workflow/review", review_body, expected=500)
    reused_before = history("reuse-no-wrong-signal", selected_run=reused_run)
    assert reused_before["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED", 0) == 0
    assert (
        history("original-still-bound")["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED")
        == 1
    )
    request(base, prefix + "workflow/review", {**review_body, "run_id": reused_run}, expected=204)
    poll(
        lambda: request(base, prefix + "workflow/status?" + reused_query),
        lambda x: x["status"]["state"] == "completed",
    )
    reused_after = history("reuse-completed", selected_run=reused_run)
    assert reused_after["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED") == 1
    observations["runIdReuse"] = {
        "oldRun": run_id,
        "newRun": reused_run,
        "pinnedClosedRunHttpStatus": 500,
        "pinnedClosedRunWireResponse": closed_rejection,
        "nativeRejectionReasonObserved": False,
        "pinnedTerminalBeforeRejection": closed_before,
        "newRunBeforeDeliberateDecision": reused_before,
        "newRunAfterDeliberateDecision": reused_after,
    }
    write(observation_path, observations)
    # Deliberately lose a response only AFTER the real API accepted one decision.
    # Reconcile status/history; never repeat the uncertain mutation.
    lost_execution = execution + "-lost-response"
    lost_receipt = request(
        base, prefix + "workflow/start", {**start_body, "execution_id": lost_execution}
    )
    lost_run = history(
        "lost-resolve", exec_id=lost_execution, selected_run=lost_receipt["run_id"] or "current"
    )["runId"]
    lost_query = urllib.parse.urlencode(
        {"environment_id": "local", "execution_id": lost_execution, "run_id": lost_run}
    )
    poll(
        lambda: request(base, prefix + "workflow/status?" + lost_query),
        lambda x: x["status"]["state"] == "waiting_for_review",
    )
    proxy = target.create(
        "container",
        edition + "-fault-proxy",
        "--network",
        frontend["name"],
        "--publish",
        "127.0.0.1::8081",
        "--read-only",
        image if edition == "typescript" else target.node_image,
        "node",
        "/app/fault-proxy.mjs",
    )
    target.start(proxy)
    proxy_base = target.port(proxy, "8081/tcp")
    poll(lambda: target.docker("container", "logs", proxy["id"]), lambda x: '"event":"ready"' in x)
    lost_seen = False
    try:
        request(
            proxy_base,
            prefix + "workflow/review",
            {**review_body, "execution_id": lost_execution, "run_id": lost_run},
            expected=204,
        )
    except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionError):
        lost_seen = True
    assert lost_seen, "expected a lost transport response"
    proxy_logs = target.docker("container", "logs", proxy["id"])
    fault_record = json.loads(proxy_logs.splitlines()[-1])
    assert fault_record == {"event": "upstream-response-discarded", "status": 204, "decisions": 1}
    poll(
        lambda: request(base, prefix + "workflow/status?" + lost_query),
        lambda x: x["status"]["state"] == "completed",
    )
    lost_history = history("lost-reconciled", exec_id=lost_execution, selected_run=lost_run)
    assert lost_history["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED") == 1
    assert lost_history["eventCounts"].get("EVENT_TYPE_ACTIVITY_TASK_COMPLETED") == 3
    target.stop(proxy)
    observations["lostResponse"] = {
        "proxy": fault_record,
        "reconciled": lost_history,
        "deliberateResubmissions": 0,
    }
    write(observation_path, observations)
    # New version runs its changed graph on its own queue while the compatible
    # v1 worker is retained. No claim that arbitrary edits are replay-compatible.
    next_worker = worker(edition + "-next-worker", "next")
    next_execution = execution + "-v2"
    next_prefix = "/api/v1/projects/next/workflows/workflow/"
    next_receipt = request(
        base, next_prefix + "start", {**start_body, "execution_id": next_execution}
    )
    next_run = history(
        "next-resolve", exec_id=next_execution, selected_run=next_receipt["run_id"] or "current"
    )["runId"]
    next_query = urllib.parse.urlencode(
        {"environment_id": "local", "execution_id": next_execution, "run_id": next_run}
    )
    poll(
        lambda: request(base, next_prefix + "status?" + next_query),
        lambda x: x["status"]["state"] == "waiting_for_review",
    )
    request(
        base,
        next_prefix + "review",
        {**review_body, "execution_id": next_execution, "run_id": next_run},
        expected=204,
    )
    poll(
        lambda: request(base, next_prefix + "status?" + next_query),
        lambda x: x["status"]["state"] == "completed",
    )
    next_history = history("next-completed", exec_id=next_execution, selected_run=next_run)
    assert next_history["eventCounts"].get("EVENT_TYPE_ACTIVITY_TASK_COMPLETED") == 4
    target.stop(next_worker)
    observations["nextVersion"] = next_history
    write(observation_path, observations)
    retry_worker = worker(edition + "-retry-worker", "retry")
    retry_execution = execution + "-retry"
    retry_prefix = "/api/v1/projects/retry/workflows/workflow/"
    retry_receipt = request(
        base, retry_prefix + "start", {**start_body, "execution_id": retry_execution}
    )
    retry_run = history(
        "retry-resolve", exec_id=retry_execution, selected_run=retry_receipt["run_id"] or "current"
    )["runId"]
    retry_query = urllib.parse.urlencode(
        {"environment_id": "local", "execution_id": retry_execution, "run_id": retry_run}
    )
    poll(
        lambda: request(base, retry_prefix + "status?" + retry_query),
        lambda x: x["status"]["state"] == "waiting_for_review",
    )
    request(
        base,
        retry_prefix + "review",
        {**review_body, "execution_id": retry_execution, "run_id": retry_run},
        expected=204,
    )
    poll(
        lambda: request(base, retry_prefix + "status?" + retry_query),
        lambda x: x["status"]["state"] == "completed",
    )
    retry_history = history("retry-completed", exec_id=retry_execution, selected_run=retry_run)
    assert retry_history["eventCounts"].get("EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED") == 1
    effects_raw = execute(
        target, image, None, edition + "-effects", ["cat", "/data/effects.jsonl"], mounts=mounts
    )
    effects = [json.loads(line) for line in effects_raw.splitlines()]
    retry_calls = [x for x in effects if x["runId"] == retry_run and x["activity"] == "assess_item"]
    assert [x["attempt"] for x in retry_calls] == [1, 2]
    assert retry_calls[1]["timeMs"] - retry_calls[0]["timeMs"] >= 4900
    main_calls = [x for x in effects if x["runId"] == run_id]
    expected_calls = (
        2 if edition == "python" else 3
    )  # Python's plain decide is not a provider call.
    assert len(main_calls) == expected_calls and all(x["attempt"] == 1 for x in main_calls)
    target.stop(retry_worker)
    observations["activityRetry"] = {"history": retry_history, "attempts": retry_calls}
    observations["acknowledgedForwardProviderCalls"] = main_calls
    write(observation_path, observations)
    drained = poll(
        lambda: request(base, version_path),
        lambda x: x["total_running"] == 0 and x["drained"] is True,
    )
    # Frozen version enforcement is at Python runtime/worker build vs TS new-start time.
    if edition == "typescript":
        rejected = request(
            base,
            "/api/v1/projects/incompatible/workflows/workflow/start",
            {
                **start_body,
                "execution_id": execution + "-incompatible",
                "expected_policy_hash": policy_hash,
            },
            expected=422,
        )
        assert "frozen" in json.dumps(rejected).lower()
    else:
        incompatible = target.create(
            "container",
            edition + "-incompatible",
            "--network",
            network["name"],
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=256m",
            *mount_args,
            image,
            *base_command,
            "incompatible/typeflux.project.yaml",
            "--workflow",
            "workflow",
            "--environment",
            "local",
            "--policy",
            "base",
        )
        target.start(incompatible)
        code = target.docker("container", "wait", incompatible["id"])
        text = target.docker("container", "logs", incompatible["id"])
        text += (target.output / f"command-{target.commands}.stderr").read_text()
        info = target.inspect(incompatible)
        assert (
            code != "0"
            and not info["State"].get("OOMKilled")
            and "is frozen to spec digest" in text
        )
    # Once old runs are actually drained, gracefully stop the compatible v1 worker.
    target.stop(replacement)
    target.stop(console)
    target.stop(api)
    observations["completed"] = True
    write(observation_path, observations)
    return {
        "edition": edition,
        "receipt": receipt,
        "policyHash": policy_hash,
        "before": before,
        "persistedAfterServerRestart": recovered,
        "after": after,
        "lifecycle": completed["status"],
        "drainingBefore": draining,
        "drainedAfter": drained,
        "backendPausedWithWorkerStopped": observations["backendPausedWithWorkerStopped"],
        "workerTimeoutSeconds": worker_outage_seconds,
        "browser": json.loads(browser.stdout.splitlines()[-1]),
        "frozenVersionRefused": True,
        "installedGeneratedClient": installed_client,
        "installedSdk": installed_sdk,
        "lostResponse": observations["lostResponse"],
        "activityRetry": observations["activityRetry"],
        "nextVersion": next_history,
        "acknowledgedForwardProviderCalls": main_calls,
        "runIdReuse": observations["runIdReuse"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="desktop-linux")
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--accepted-main", type=Path, required=True)
    parser.add_argument(
        "--images",
        type=Path,
        required=True,
        help="JSON map: python, python-base, node, node-base, console to immutable sha256 IDs",
    )
    parser.add_argument("--console-modules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not __debug__:
        raise RuntimeError("verification assertions require normal Python execution")
    target = Target(args.output, args.context)
    report = {
        "passed": False,
        "authority": "local Docker observations; not trusted CI or cloud recovery",
        "journeys": [],
        "limits": [
            "one local Docker host",
            "fixed fake providers and inputs",
            "no arbitrary external-effect exactly-once guarantee",
            "no cloud/high-availability/production proof",
        ],
    }
    phase = "freeze"
    try:
        inventory = json.loads(args.inventory.read_text())
        accepted = json.loads(args.accepted_main.read_text())
        assert (
            accepted["passed"] is True
            and accepted["headRevision"]
            == accepted["testedRevision"]
            == inventory["producerRevision"]
        )
        assert accepted["treeDigest"] == inventory["producerInputs"]
        assert sha(args.accepted_main) == inventory["acceptedMain"]["receiptSha256"]
        for artifact in inventory["artifacts"]:
            assert sha(args.prepared / "artifacts" / artifact["name"]) == artifact["sha256"]
        prepared = json.loads((args.prepared / "prepared.json").read_text())
        target.artifacts = args.prepared / "artifacts"
        target.python_lock_sha = prepared["files"]["python/uv.lock"]
        for path, expected in prepared["files"].items():
            assert sha(args.prepared / path) == expected
        images = json.loads(args.images.read_text())
        target.node_image = images["node"]
        assert set(images) == {"python", "python-base", "node", "node-base", "console"}
        for value in images.values():
            assert value.startswith("sha256:") and len(value) == 71
            assert json.loads(target.docker("image", "inspect", value))[0]["Id"] == value
        context = json.loads(target.docker("context", "inspect", args.context))[0]
        assert context["Endpoints"]["docker"]["Host"].startswith("unix://"), "local target required"
        environment = json.loads(target.docker("info", "--format", "{{json .}}"))
        frozen = {
            "candidate": inventory,
            "images": images,
            "temporalImage": TEMPORAL_IMAGE,
            "preparedFiles": prepared["files"],
            "observerFiles": {
                p.name: sha(p)
                for p in Path(__file__).parent.iterdir()
                if p.is_file() and p.suffix in {".py", ".mjs", ".Dockerfile", ".conf"}
            },
            "environment": {
                k: environment[k] for k in ["ServerVersion", "Architecture", "NCPU", "MemTotal"]
            },
            "thresholds": {
                "startupSeconds": 60,
                "observationSeconds": 30,
                "backendSeconds": 3,
                "transportCeilingSeconds": 8,
                "automaticMutationRetries": 0,
            },
        }
        frozen["networkTopology"] = (
            "internal Temporal/worker network plus owned bridge for loopback API/console publication"
        )
        write(target.output / "frozen.json", frozen)  # Must precede every target resource.
        report["frozenSha256"] = sha(target.output / "frozen.json")
        phase = "base-consumers"
        report["localConsumers"] = []
        for edition, image, sdk, cli in [
            (
                "python",
                images["python-base"],
                ["python", "/app/local/local_sdk.py"],
                ["typeflux-local", "/app/local/workflow.yaml", "--input", "/app/local/input.json"],
            ),
            (
                "typescript",
                images["node-base"],
                ["node", "/app/local/local-sdk.mjs"],
                [
                    "/app/node_modules/.bin/typeflux-local",
                    "/app/local/workflow.yaml",
                    "--input",
                    "/app/local/input.json",
                    "--bindings",
                    "/app/local/bindings.mjs",
                ],
            ),
        ]:
            installed_sdk = installation(target, edition, image, base=True)
            consumer = {
                "edition": edition,
                "installation": installed_sdk,
                "sdkPassed": False,
                "cliPassed": False,
            }
            report["localConsumers"].append(consumer)
            for label, command in [("sdk", sdk), ("cli", cli)]:
                output = execute(target, image, None, edition + "-local-" + label, command)
                assert json.loads(output.splitlines()[-1]) == {"text": "local result"}
                consumer[label + "Passed"] = True
        phase = "temporal-startup"
        network = target.create("network", "network", "--internal")
        frontend = target.create("network", "frontend")
        volume = target.create("volume", "temporal-data")
        execute(
            target,
            images["node-base"],
            None,
            "data-permissions",
            ["node", "-e", "require('fs').chmodSync('/data', 0o777)"],
            mounts=[f"type=volume,src={volume['name']},dst=/data"],
        )
        temporal = target.create(
            "container",
            "temporal",
            "--network",
            network["name"],
            "--network-alias",
            "temporal",
            "--mount",
            f"type=volume,src={volume['name']},dst=/data",
            TEMPORAL_IMAGE,
            "server",
            "start-dev",
            "--ip",
            "0.0.0.0",
            "--headless",
            "--db-filename",
            "/data/temporal.db",
        )
        target.start(temporal)
        for edition, key in [("python", "python"), ("typescript", "node")]:
            phase = edition + "-journey"
            report["journeys"].append(
                journey(
                    target,
                    edition,
                    images[key],
                    images["console"],
                    network,
                    frontend,
                    temporal,
                    args.console_modules.resolve(),
                )
            )
        report["passed"] = True
    except BaseException as exc:
        report["failurePhase"] = phase
        report["failureClass"] = type(exc).__name__
        raise
    finally:
        source_drift = False
        if report["passed"]:
            try:
                current_observer = {
                    p.name: sha(p)
                    for p in Path(__file__).parent.iterdir()
                    if p.is_file() and p.suffix in {".py", ".mjs", ".Dockerfile", ".conf"}
                }
                source_drift = current_observer != frozen["observerFiles"] or any(
                    sha(args.prepared / path) != expected
                    for path, expected in frozen["preparedFiles"].items()
                )
            except OSError:
                source_drift = True
            if source_drift:
                report.update(
                    passed=False, failurePhase="inputs-changed", failureClass="SourceDrift"
                )
        failures = target.cleanup()
        report["cleanupFailures"] = failures
        report["passed"] = report["passed"] and not failures
        write(target.output / "report.json", report)
        if failures:
            raise RuntimeError("owned-resource-cleanup-incomplete")
        if source_drift:
            raise RuntimeError("deployment-observation-inputs-changed")


if __name__ == "__main__":
    main()
