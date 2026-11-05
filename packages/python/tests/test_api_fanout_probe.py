"""The diagnostic observes all reads without hiding failure or collecting payloads."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from tools.development import api_fanout_probe as probe

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def fixture_api(monkeypatch):
    state = {
        "calls": [],
        "fail_first": False,
        "observations": None,
        "invalid_shape": False,
        "wrong_identity": False,
    }

    def build(root, observations):
        state["observations"] = observations
        app = FastAPI()

        @app.get("/api/v1/projects/{project}/workflows")
        def workflows(project):
            return {"workflows": [{"id": "sample"}]}

        @app.get("/api/v1/projects/{project}/environments")
        def environments(project):
            return {"environments": [{"id": "local"}]}

        @app.get("/api/v1/projects/{project}/validate")
        def validate(project, environment_id):
            state["calls"].append((project, "validation"))
            return observations.call("validation", lambda: {"ok": False})

        @app.get("/api/v1/projects/{project}/workflows/{workflow}/bundle")
        def bundle(project, workflow, environment_id):
            def value():
                state["calls"].append((project, "bundle"))
                if (
                    state["fail_first"]
                    and len([c for c in state["calls"] if c == (project, "bundle")]) == 1
                ):
                    return JSONResponse(
                        {"error": "secret-token alice@example.com"}, status_code=503
                    )
                return {
                    "workflow": {"id": "wrong" if state["wrong_identity"] else workflow},
                    "environment": {"id": environment_id},
                    "validation": {
                        "ok": "secret-token alice@example.com" if state["invalid_shape"] else False
                    },
                    "private": "secret-token alice@example.com",
                }

            return observations.call("bundle", value)

        return app

    monkeypatch.setattr(probe, "application", build)
    return state


@pytest.mark.parametrize("concurrency", [1, 4])
def test_api_probe_observes_every_read_and_keeps_validation_distinct(fixture_api, concurrency):
    record = probe.measure(ROOT, concurrency=concurrency, mode="matrix", profile=concurrency == 1)
    assert record["readStatus"] == "completed" and record["inputsUnchanged"]
    assert record["readsPerRound"] == 4 and len(record["rounds"]) == 2
    assert len(fixture_api["calls"]) == 8
    assert len(record["resolver"]["samples"]) == 8
    assert record["requestTimeoutSeconds"] == 15 and record["retries"] == 0
    for batch in record["rounds"]:
        assert len(batch["reads"]) == 4 and all(r["statusCode"] == 200 for r in batch["reads"])
        assert all(r["validationOk"] is False for r in batch["reads"])
    serialized = json.dumps(record)
    assert "secret-token" not in serialized and "alice@example.com" not in serialized
    assert all("responseSha256" not in r for batch in record["rounds"] for r in batch["reads"])
    assert "not CI acceptance" in record["authority"]


def test_api_probe_retains_original_failed_round_after_later_success(fixture_api):
    fixture_api["fail_first"] = True
    record = probe.measure(ROOT, concurrency=4, mode="overview")
    assert record["readStatus"] == "failed" and len(fixture_api["calls"]) == 8
    first, second = record["rounds"]
    assert len([r for r in first["reads"] if r["statusCode"] == 503]) == 2
    assert all(r["completed"] for r in second["reads"])
    assert "secret-token" not in json.dumps(record)


def test_api_probe_timing_keeps_original_error_and_bounded_observations(monkeypatch):
    monkeypatch.setattr(probe, "MAX_READS", 1)
    observations = probe.ResolverObservations(profile=True)
    error = RuntimeError("secret-token alice@example.com")

    def fail():
        raise error

    with pytest.raises(RuntimeError) as caught:
        observations.call("bundle", fail)
    assert caught.value is error
    assert observations.call("validation", lambda: "result") == "result"
    assert observations.call("validation", lambda: "result") == "result"
    record = observations.result()
    assert len(record["samples"]) == 2 and record["samplesOmitted"] == 1
    assert record["samples"][0]["failed"] is True
    assert "secret-token" not in json.dumps(record)


def test_api_probe_read_budget_is_total_and_does_not_retry(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"private-response")

    clocks = iter((0, 16))
    monkeypatch.setattr(probe, "time", SimpleNamespace(perf_counter=lambda: next(clocks)))
    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="http://127.0.0.1"
    ) as client:
        with pytest.raises(probe.ProbeError, match="request-budget-exceeded"):
            probe.read(client, "/read")
    assert len(calls) == 1


def test_api_probe_workload_limit_fails_instead_of_dropping_reads():
    requests = []

    def inventory(request):
        requests.append(request.url.path)
        key = "workflows" if request.url.path.endswith("workflows") else "environments"
        items = [{"id": str(i)} for i in range(128)] if key == "workflows" else [{"id": "local"}]
        return httpx.Response(200, json={key: items})

    with httpx.Client(
        transport=httpx.MockTransport(inventory), base_url="http://127.0.0.1"
    ) as client:
        with pytest.raises(probe.ProbeError, match="workload-limit-exceeded"):
            probe.workload(client, "matrix")
    assert len(requests) == 4 and all(not path.endswith("/bundle") for path in requests)


def test_api_probe_rejects_concurrent_profiling_before_starting_api(monkeypatch):
    def unexpected(*args):
        pytest.fail("unsupported profiler mode must fail before starting API")

    monkeypatch.setattr(probe, "application", unexpected)
    with pytest.raises(probe.ProbeError, match="profiling-requires-sequential-workload"):
        probe.measure(ROOT, concurrency=4, profile=True)


def test_api_probe_rejects_success_status_with_invalid_bundle_shape(fixture_api):
    fixture_api["invalid_shape"] = True
    record = probe.measure(ROOT, concurrency=4)
    assert record["readStatus"] == "failed"
    bundles = [
        r for batch in record["rounds"] for r in batch["reads"] if r["operation"] == "bundle"
    ]
    assert len(bundles) == 4
    assert all(r["statusCode"] == 200 and not r["completed"] for r in bundles)
    assert all("validationOk" not in r for r in bundles)
    assert "secret-token" not in json.dumps(record)
    assert "alice@example.com" not in json.dumps(record)


def test_api_probe_retains_fixed_read_failure_codes(fixture_api, monkeypatch):
    original = probe.read

    def fail_bundle(client, path, params=None):
        if path.endswith("/bundle"):
            raise probe.ProbeError("oversized-response")
        return original(client, path, params)

    monkeypatch.setattr(probe, "read", fail_bundle)
    record = probe.measure(ROOT, concurrency=4)
    bundles = [
        r for batch in record["rounds"] for r in batch["reads"] if r["operation"] == "bundle"
    ]
    assert record["readStatus"] == "failed" and len(bundles) == 4
    assert all(r["reasonCode"] == "oversized-response" and not r["completed"] for r in bundles)


def test_api_probe_classifies_payload_identity_mismatch(fixture_api):
    fixture_api["wrong_identity"] = True
    record = probe.measure(ROOT, concurrency=4)
    bundles = [
        r for batch in record["rounds"] for r in batch["reads"] if r["operation"] == "bundle"
    ]
    assert record["readStatus"] == "failed" and len(bundles) == 4
    assert all(
        r["statusCode"] == 200 and r["reasonCode"] == "payload-identity-mismatch" for r in bundles
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (probe.ProbeError("workload-limit-exceeded"), "workload-limit-exceeded"),
        (probe.ProbeError("secret-token alice@example.com"), "probe-incomplete"),
        (ValueError("secret-token alice@example.com"), "probe-incomplete"),
    ],
)
def test_api_probe_cli_keeps_fixed_codes_without_exception_payloads(
    error, expected, tmp_path, monkeypatch
):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(probe, "measure", fail)
    monkeypatch.setattr(probe.os, "environ", dict(probe.os.environ))
    output = tmp_path / "diagnostic.json"
    assert probe.main(["--root", str(ROOT), "--output", str(output)]) == 1
    assert json.loads(output.read_text()) == {"readStatus": "failed", "reasonCode": expected}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("secret-token alice@example.com"), "fixture-inventory-timeout"),
        (httpx.ConnectError("secret-token alice@example.com"), "fixture-inventory-transport-error"),
    ],
)
def test_api_probe_inventory_http_failures_write_safe_failed_report(
    fixture_api, error, expected, tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from urllib.parse import urlsplit

    attempts, addresses = [], []
    original_serve = probe.serve

    @contextmanager
    def server(app):
        with original_serve(app) as address:
            addresses.append(address)
            yield address

    def unavailable(client, path, params=None):
        attempts.append(path)
        raise error

    monkeypatch.setattr(probe, "serve", server)
    monkeypatch.setattr(probe, "read", unavailable)
    monkeypatch.setattr(probe.os, "environ", dict(probe.os.environ))
    output = tmp_path / "diagnostic.json"
    assert probe.main(["--root", str(ROOT), "--output", str(output)]) == 1
    assert json.loads(output.read_text()) == {"readStatus": "failed", "reasonCode": expected}
    assert attempts == ["/api/v1/projects/examples/workflows"]
    assert fixture_api["calls"] == []
    assert len(addresses) == 1
    import socket

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", urlsplit(addresses[0]).port))
        listener.listen()
