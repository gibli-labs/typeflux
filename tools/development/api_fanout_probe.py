"""Bounded local API-read diagnostic; no runtime instrumentation or acceptance claims."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import platform
import pstats
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from tools.project_graph.compiler import compile_graph
from tools.project_graph.repository import Repository

MAX_READS = 128
MAX_BODY = 8 * 1024 * 1024
REQUEST_TIMEOUT = 15.0
PROJECTS = ("examples", "examples_b")
REASON_CODES = frozenset(
    {
        "probe-incomplete",
        "probe-server-unavailable",
        "probe-server-shutdown-incomplete",
        "oversized-response",
        "request-budget-exceeded",
        "fixture-inventory-unavailable",
        "fixture-inventory-invalid",
        "local-fixture-environment-missing",
        "workload-limit-exceeded",
        "invalid-probe-configuration",
        "profiling-requires-sequential-workload",
        "invalid-validation-shape",
        "payload-identity-mismatch",
    }
)


class ProbeError(ValueError):
    """Only fixed diagnostic reason codes cross the output boundary."""

    def __init__(self, code):
        self.reason_code = code if code in REASON_CODES else "probe-incomplete"
        super().__init__(self.reason_code)


class ResolverObservations:
    def __init__(self, profile=False):
        self.profile = profile
        self.lock = threading.Lock()
        self.samples = []
        self.frames = {}
        self.frames_omitted = 0
        self.samples_omitted = 0

    def call(self, operation, action):
        profile = cProfile.Profile() if self.profile else None
        wall, cpu = time.perf_counter(), time.thread_time()
        failed = True
        if profile:
            profile.enable()
        try:
            value = action()
            failed = False
            return value
        finally:
            if profile:
                profile.disable()
            sample = {
                "operation": operation,
                "elapsedSeconds": time.perf_counter() - wall,
                "threadCpuSeconds": time.thread_time() - cpu,
                "failed": failed,
            }
            with self.lock:
                if len(self.samples) < MAX_READS * 2:
                    self.samples.append(sample)
                else:
                    self.samples_omitted += 1
                if profile:
                    for (file, line, name), (_, calls, own, cumulative, _) in pstats.Stats(
                        profile
                    ).stats.items():
                        if "/typeflux/" in file:
                            label = "typeflux/" + file.split("/typeflux/", 1)[1] + f":{line}:{name}"
                        elif file == "~" and "_thread.lock" in name and "acquire" in name:
                            label = "thread-lock-acquire"
                        else:
                            continue
                        label = label[:160]
                        if label not in self.frames and len(self.frames) >= 256:
                            self.frames_omitted += 1
                            continue
                        total = self.frames.setdefault(
                            label, {"calls": 0, "ownSeconds": 0.0, "cumulativeSeconds": 0.0}
                        )
                        total["calls"] += calls
                        total["ownSeconds"] += own
                        total["cumulativeSeconds"] += cumulative

    def result(self):
        frames = sorted(
            self.frames.items(), key=lambda row: row[1]["cumulativeSeconds"], reverse=True
        )
        return {
            "samples": self.samples,
            "samplesOmitted": self.samples_omitted,
            "profileEnabled": self.profile,
            "topRetainedFrames": [{"function": key, **value} for key, value in frames[:20]],
            "profileCollectionComplete": self.frames_omitted == 0 if self.profile else None,
            "profileCollectionEntriesOmitted": self.frames_omitted,
            "summaryFunctionsOmitted": max(0, len(frames) - 20),
        }


def application(root, observations):
    from typeflux.controlplane import create_app_from_registry
    from typeflux.controlplane.resolver import InProcessPythonResolver

    class ObservedResolver(InProcessPythonResolver):
        def resolve_bundle(self, *args, **kwargs):
            method = super().resolve_bundle
            return observations.call("bundle", lambda: method(*args, **kwargs))

        def validate_project(self, *args, **kwargs):
            method = super().validate_project
            return observations.call("validation", lambda: method(*args, **kwargs))

    return create_app_from_registry(
        root / "clients/console/e2e/typeflux.projects.yaml", resolvers=(ObservedResolver(),)
    )


@contextmanager
def serve(app):
    # Bind first: no fixed port, existing-server reuse, or port-based termination.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app, access_log=False, log_level="critical", timeout_graceful_shutdown=10
            )
        )
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if not worker.is_alive() or time.monotonic() >= deadline:
                    raise ProbeError("probe-server-unavailable")
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            if worker.is_alive():
                raise ProbeError("probe-server-shutdown-incomplete")


def read(client, path, params=None):
    deadline = time.perf_counter() + REQUEST_TIMEOUT
    with client.stream("GET", path, params=params) as response:
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BODY:
                raise ProbeError("oversized-response")
            if time.perf_counter() > deadline:
                raise ProbeError("request-budget-exceeded")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)


def workload(client, mode):
    descriptors = []
    for project in PROJECTS:
        prefix = f"/api/v1/projects/{project}"
        inventories = []
        for suffix, key in (("workflows", "workflows"), ("environments", "environments")):
            status, body = read(client, f"{prefix}/{suffix}")
            if status != 200:
                raise ProbeError("fixture-inventory-unavailable")
            ids = [item["id"] for item in json.loads(body)[key]]
            if (
                not ids
                or len(ids) != len(set(ids))
                or any(not isinstance(value, str) for value in ids)
            ):
                raise ProbeError("fixture-inventory-invalid")
            inventories.append(ids)
        workflows, environments = inventories
        if mode == "overview":
            if "local" not in environments:
                raise ProbeError("local-fixture-environment-missing")
            environments = ["local"]
        for env in environments:
            descriptors.append(
                {
                    "operation": "validation",
                    "path": f"{prefix}/validate",
                    "params": {"environment_id": env},
                }
            )
            descriptors.extend(
                {
                    "operation": "bundle",
                    "path": f"{prefix}/workflows/{quote(name, safe='')}/bundle",
                    "params": {"environment_id": env},
                    "workflow": name,
                }
                for name in workflows
            )
    if len(descriptors) > MAX_READS:
        raise ProbeError("workload-limit-exceeded")
    return descriptors


def measure(root, *, concurrency=16, mode="matrix", profile=False):
    if not 1 <= concurrency <= 16 or mode not in {"matrix", "overview"}:
        raise ProbeError("invalid-probe-configuration")
    if profile and concurrency != 1:
        # CPython 3.12 cProfile instances compete for one monitoring slot.
        # Do not serialize concurrent resolver work just to collect profiles.
        raise ProbeError("profiling-requires-sequential-workload")
    source = Repository(root)
    observations = ResolverObservations(profile)
    app = application(root, observations)
    rounds = []
    with (
        serve(app) as url,
        httpx.Client(
            base_url=url,
            timeout=REQUEST_TIMEOUT,
            trust_env=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
        ) as client,
    ):
        descriptors = workload(client, mode)
        for index in range(2):
            start = time.perf_counter()

            def request(item):
                begin = time.perf_counter()
                result = {
                    "operation": item["operation"],
                    "queueSeconds": begin - start,
                    "statusCode": None,
                    "completed": False,
                }
                try:
                    status, body = read(client, item["path"], item["params"])
                    result["statusCode"] = status
                    if status == 200:
                        payload = json.loads(body)
                        if item["operation"] == "bundle":
                            valid = (
                                payload["workflow"]["id"] == item["workflow"]
                                and payload["environment"]["id"] == item["params"]["environment_id"]
                            )
                            result["validationOk"] = payload["validation"]["ok"]
                        else:
                            valid = isinstance(payload["ok"], bool)
                            result["validationOk"] = payload["ok"]
                        if not isinstance(result["validationOk"], bool):
                            result.pop("validationOk")
                            raise ProbeError("invalid-validation-shape")
                        if not valid:
                            raise ProbeError("payload-identity-mismatch")
                        result["completed"] = True
                        result["responseBytes"] = len(body)
                    else:
                        result["reasonCode"] = "http-error"
                except ProbeError as error:
                    result["reasonCode"] = error.reason_code
                except httpx.TimeoutException:
                    result["reasonCode"] = "request-timeout"
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    result["reasonCode"] = "read-or-shape-error"
                result["elapsedSeconds"] = time.perf_counter() - begin
                result["withinReadinessBudget"] = (
                    result["queueSeconds"] + result["elapsedSeconds"] <= REQUEST_TIMEOUT
                )
                return result

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(request, descriptors))
            rounds.append(
                {
                    "index": index,
                    "label": "cold" if index == 0 else "warm",
                    "elapsedSeconds": time.perf_counter() - start,
                    "reads": results,
                }
            )
    same = Repository(root).tree_digest == source.tree_digest
    return {
        "authority": "local diagnostic; not CI acceptance or deployed recovery",
        "sourceRevision": source.revision,
        "sourceInputs": source.tree_digest,
        "graphDigest": compile_graph(source)["digest"],
        "inputsUnchanged": same,
        "readStatus": "completed"
        if same and all(r["completed"] for batch in rounds for r in batch["reads"])
        else "failed",
        "workload": mode,
        "concurrency": concurrency,
        "readsPerRound": len(descriptors),
        "withinReadinessBudget": all(
            r["withinReadinessBudget"] for batch in rounds for r in batch["reads"]
        ),
        "requestTimeoutSeconds": REQUEST_TIMEOUT,
        "retries": 0,
        "python": platform.python_version(),
        "dependencies": {
            name: version(name)
            for name in ("fastapi", "httpx", "uvicorn", "pydantic", "PyYAML", "temporalio")
        },
        "platform": platform.system(),
        "gitVersion": subprocess.check_output(["git", "--version"], text=True).strip(),
        "cpuCount": os.cpu_count(),
        "loadAverage": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "rounds": rounds,
        "resolver": observations.result(),
        "limits": {
            "maxReadsPerRound": MAX_READS,
            "maxResponseBytes": MAX_BODY,
            "maxResolverSamples": MAX_READS * 2,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=16)
    parser.add_argument("--workload", choices=("overview", "matrix"), default="matrix")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args(argv)
    # Synthetic read-only browser fixture values, never inherited service credentials.
    from tools.project_graph.collection import collection_environment

    env = collection_environment()
    for key in tuple(env):
        if key.startswith(
            (
                "ANTHROPIC_",
                "OPENAI_",
                "LANGFUSE_",
                "LANGSMITH_",
                "TEMPORAL_",
                "VERCEL_",
                "AI_GATEWAY_",
            )
        ):
            env.pop(key)
    env.update(
        TYPEFLUX_ENV_FILE=str(args.root / ".typeflux-probe-no-env"),
        TEMPORAL_ADDRESS="127.0.0.1:1",
        TEMPORAL_NAMESPACE="browser-fixture",
        TEMPORAL_TLS="false",
    )
    os.environ.clear()
    os.environ.update(env)
    try:
        result = measure(
            args.root.resolve(),
            concurrency=args.concurrency,
            mode=args.workload,
            profile=args.profile,
        )
    except ProbeError as error:
        result = {"readStatus": "failed", "reasonCode": error.reason_code}
    except (OSError, ValueError, KeyError, TypeError):
        result = {"readStatus": "failed", "reasonCode": "probe-incomplete"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["readStatus"] == "completed" and result.get("withinReadinessBudget") else 1


if __name__ == "__main__":
    raise SystemExit(main())
