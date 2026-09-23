from __future__ import annotations

import ipaddress
import os
import socket
import sys
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

_ISOLATED_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
    "LANGFUSE_PROMPT_LABEL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "OPENAI_API_KEY",
    "TEMPORAL_ADDRESS",
    "TEMPORAL_API_KEY",
    "TEMPORAL_NAMESPACE",
    "TEMPORAL_TASK_QUEUE",
    "TEMPORAL_TLS",
    "TEMPORAL_WORKFLOW_ID",
    "TYPEFLUX_ANTHROPIC_MODEL",
    "TYPEFLUX_CONTRACT_ARTIFACT_ROOT",
    "TYPEFLUX_CONTRACT_PROMPT_REGISTRY",
    "TYPEFLUX_DEPLOYMENT_ID",
    "TYPEFLUX_ENV_FILE",
    "TYPEFLUX_ENVIRONMENT",
    "TYPEFLUX_GIT_REF",
    "TYPEFLUX_GIT_SHA",
    "TYPEFLUX_LIFECYCLE_OBSERVABILITY",
    "TYPEFLUX_OPENAI_MODEL",
    "TYPEFLUX_REPO_URL",
    "TYPEFLUX_RUN_LIVE",
    "TYPEFLUX_TEMPORAL_REGION",
}
_ISOLATED_ENV_PREFIXES = (
    "ANTHROPIC_",
    "LANGCHAIN_",
    "LANGFUSE_",
    "LANGSMITH_",
    "OPENAI_",
    "TEMPORAL_",
)


@pytest.fixture(autouse=True)
def isolated_test_environment(request: pytest.FixtureRequest) -> Iterator[None]:
    if _runs_explicit_live_selection(request):
        yield
        return

    original_environ = dict(os.environ)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    _clear_runtime_environment()
    os.environ["TYPEFLUX_ENV_FILE"] = str(
        Path(request.config.rootpath) / ".typeflux-tests-do-not-load.env"
    )
    _clear_code_provenance_cache()

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        _raise_if_external_address(address)
        return original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        _raise_if_external_address(address)
        return original_connect_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        os.environ.clear()
        os.environ.update(original_environ)
        _clear_code_provenance_cache()


def _runs_explicit_live_selection(request: pytest.FixtureRequest) -> bool:
    return str(request.config.option.markexpr).strip() == "live"


def _clear_runtime_environment() -> None:
    for name in tuple(os.environ):
        if name in _ISOLATED_ENV_NAMES or name.startswith(_ISOLATED_ENV_PREFIXES):
            os.environ.pop(name, None)


def _raise_if_external_address(address: Any) -> None:
    if _is_loopback_address(address):
        return
    raise RuntimeError(f"external network is disabled for non-live tests: {address!r}")


def _is_loopback_address(address: Any) -> bool:
    if isinstance(address, str):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# --- shared minimal-project scaffold ---------------------------------------
#
# The project.* unit-test modules all exercise the same canonical single-step
# project layout (fake package + workflow.yaml + typeflux.project.yaml +
# env.yaml). ``make_minimal_project`` is the one factory for that scaffold;
# the ``*_block`` arguments splice pre-indented YAML fragments (12-space
# indent, matching the 8-space template margin below) into the workflow spec.

_MINIMAL_SCHEMAS_SOURCE = """
from pydantic import BaseModel


class InputModel(BaseModel):
    value: str


class OutputModel(BaseModel):
    value: str
"""

_DEFAULT_REGISTRY_BLOCK = """\
            type: inline
            prompts:
              first: first {{value}}"""

_DEFAULT_OBSERVABILITY_BLOCK = "            type: none"

_DEFAULT_ACTIVITIES_BLOCK = """\
            - name: first
              input: schemas:InputModel
              output: schemas:OutputModel
              prompt: first"""

_DEFAULT_STEPS_BLOCK = """\
            - id: first
              activity: first"""


def write_project_file(path: Path, content: str) -> None:
    """Write ``dedent(content)`` to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def make_minimal_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_name: str,
    *,
    registry_block: str = _DEFAULT_REGISTRY_BLOCK,
    observability_block: str = _DEFAULT_OBSERVABILITY_BLOCK,
    activities_block: str = _DEFAULT_ACTIVITIES_BLOCK,
    steps_block: str = _DEFAULT_STEPS_BLOCK,
    package_modules: Mapping[str, str] | None = None,
    project_files: Mapping[str, str] | None = None,
    extra_manifest: str = "",
) -> Path:
    """Scaffold the canonical single-step test project and return its manifest.

    Every name is derived from ``package_name`` (``bundle_unit_project`` →
    workflow spec ``bundle_demo`` on ``bundle-demo-queue`` running
    ``BundleDemoWorkflow`` in manifest ``bundle-unit-demo``); the deriving
    tests assert those names explicitly, so drift fails loudly.
    ``package_modules`` adds modules inside the fake package,
    ``project_files`` adds files next to the manifest (both dedented), and
    ``extra_manifest`` appends dedented lines to the manifest.
    """
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(tmp_path))

    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    write_project_file(package / "schemas.py", _MINIMAL_SCHEMAS_SOURCE)
    for module_name, source in (package_modules or {}).items():
        write_project_file(package / module_name, source)

    slug = package_name.removesuffix("_project").removesuffix("_unit")
    dashed_slug = slug.replace("_", "-")
    workflow_class = "".join(part.capitalize() for part in slug.split("_")) + "DemoWorkflow"
    write_project_file(
        tmp_path / "workflow.yaml",
        f"""
        project: {package_name}
        name: {slug}_demo
        task_queue: {dashed_slug}-demo-queue
        runtime:
          temporal:
            address: localhost:7233
          registry:
{registry_block}
          provider:
            type: fake
          observability:
{observability_block}
        activities:
          definitions:
{activities_block}
        workflow:
          name: {workflow_class}
          input: schemas:InputModel
          output: schemas:OutputModel
          steps:
{steps_block}
        """,
    )
    for relative_path, content in (project_files or {}).items():
        write_project_file(tmp_path / relative_path, content)

    manifest_name = package_name.removesuffix("_project").replace("_", "-") + "-demo"
    manifest_text = dedent(
        f"""
        version: "1"
        name: {manifest_name}
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: env.yaml
        """
    )
    if extra_manifest:
        manifest_text += dedent(extra_manifest).lstrip("\n")
    manifest = tmp_path / "typeflux.project.yaml"
    manifest.write_text(manifest_text, encoding="utf-8")
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n', encoding="utf-8")
    return manifest


class FakeWorkflowListClient:
    """Stand-in Temporal client for ``list_workflows``-based project helpers.

    Records every visibility ``query`` issued and every execution actually
    ``consumed`` from the returned async iterator (so tests can assert that
    limits abandon the iterator rather than draining it). Also answers
    ``get_workflow_handle(...).describe()`` from ``describe_memos`` (empty by
    default) and records the described ids — the correlation card's migration
    provenance read (#204) goes through it.
    """

    def __init__(
        self,
        executions: Sequence[Any],
        *,
        describe_memos: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.executions = list(executions)
        self.queries: list[str] = []
        self.consumed: list[Any] = []
        self.describe_memos = describe_memos or {}
        self.described: list[str] = []

    def list_workflows(self, query: str) -> AsyncIterator[Any]:
        self.queries.append(query)

        async def _iterate() -> AsyncIterator[Any]:
            for item in self.executions:
                self.consumed.append(item)
                yield item

        return _iterate()

    def get_workflow_handle(self, workflow_id: str, run_id: str | None = None) -> Any:
        outer = self

        class _Handle:
            async def describe(self) -> Any:
                from types import SimpleNamespace

                outer.described.append(workflow_id)
                return SimpleNamespace(memo=outer.describe_memos.get(workflow_id, {}))

        return _Handle()


def patch_workflow_list_client(
    monkeypatch: pytest.MonkeyPatch,
    executions: Sequence[Any],
    *,
    describe_memos: dict[str, dict[str, Any]] | None = None,
) -> FakeWorkflowListClient:
    """Route the yaml-runtime Temporal connection to a fake list client."""
    client = FakeWorkflowListClient(executions, describe_memos=describe_memos)

    async def fake_connect(spec: Any, *, plugin: Any) -> FakeWorkflowListClient:
        return client

    monkeypatch.setattr("typeflux.yaml.runtime._connect_client", fake_connect)
    return client


def _clear_code_provenance_cache() -> None:
    try:
        from typeflux.manifests import provenance as provenance_module
    except ImportError:
        return
    provenance_module._clear_code_provenance_cache()
