"""In-process gate for the HTTP conformance suite (#617 slice 1).

Replays every golden case from ``contracts/controlplane/conformance/fixtures``
against the FastAPI app over ``TestClient``, using the runner's own
``normalize``/``diff`` (loaded from the runner file so there is exactly one
comparison implementation). The true black-box path — a spawned server over
real HTTP — is the dedicated CI job (slice 4); this gate keeps the Python CP
conformant to the fixtures inside the Tests matrix.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from typeflux.controlplane import create_app_from_registry
from typeflux.controlplane.auth import Authorizer, build_authorizer

CONFORMANCE = Path(__file__).resolve().parents[3] / "contracts" / "controlplane" / "conformance"
FIXTURES = CONFORMANCE / "fixtures"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "controlplane_conformance_runner", CONFORMANCE / "runner.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_RUNNER = _load_runner()
_SUITE = json.loads((FIXTURES / "_suite.json").read_text(encoding="utf-8"))
_GLOBAL_KEYS = frozenset(_SUITE["normalize"]["mask_keys"])


def _authorizer_for(profile: str) -> Authorizer | None:
    # The per-profile auth config is suite definition (fixtures/_suite.json):
    # the token profile's grants ride the same NAME:PERMS:TOKEN syntax the
    # serve CLI accepts; the proxy profile is the trust-proxy authorizer.
    config = _SUITE["profiles"][profile]
    return build_authorizer(config.get("grants", ()), trust_proxy=profile == "proxy")


# Function-scoped on purpose (monkeypatch is function-scoped): ~0.1s app build
# per case is fine at this suite size. If the suite grows severalfold
# (operate tier), move env/sys.path handling into a module-scoped context so
# one app per profile serves all cases.
@pytest.fixture()
def make_client(monkeypatch: pytest.MonkeyPatch):
    for name in tuple(sys.modules):
        if name == "conformance_project" or name.startswith("conformance_project."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(CONFORMANCE / "project" / "python"))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(CONFORMANCE / "missing.env"))
    registry = CONFORMANCE / "project" / "python" / "typeflux.projects.yaml"

    def factory(profile: str) -> TestClient:
        return TestClient(
            create_app_from_registry(str(registry), authorizer=_authorizer_for(profile))
        )

    return factory


@pytest.mark.parametrize("case_file", _SUITE["cases"])
def test_http_conformance_case(make_client, case_file: str) -> None:
    case = json.loads((FIXTURES / case_file).read_text(encoding="utf-8"))
    client = make_client(case.get("profile", "open"))
    spec = case["request"]
    response = client.request(
        spec["method"],
        spec["path"],
        params=spec.get("query"),
        json=spec.get("body"),
        headers=spec.get("headers"),
    )
    pointers = case.get("mask", [])
    actual = _RUNNER.normalize(response.json(), global_keys=_GLOBAL_KEYS, pointers=pointers)
    golden = case["response"]
    expected = _RUNNER.normalize(golden["body"], global_keys=_GLOBAL_KEYS, pointers=pointers)
    assert response.status_code == golden["status"], response.json()
    divergences = _RUNNER.diff(expected, actual)
    assert not divergences, "\n".join(divergences)
