"""Project registry, project-scoped routes, and the resolution lock (#256)."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_controlplane_api import _setup
from typeflux.controlplane import create_app, create_app_from_registry
from typeflux.controlplane.git_source import ProjectRepoSource
from typeflux.controlplane.registry import (
    ProjectRegistry,
    ProjectRegistryEntry,
    ProjectRegistryError,
    load_project_registry,
)
from typeflux.project.environment import (
    ProjectEnvironmentApplication,
    project_environment_context,
)

# --- Registry loading ---------------------------------------------------


def test_registry_single_is_a_default_alias(tmp_path: Path) -> None:
    manifest = tmp_path / "typeflux.project.yaml"
    manifest.write_text("version: '1'\n", encoding="utf-8")
    registry = ProjectRegistry.single(manifest)

    assert registry.default_id == "default"
    assert [e.id for e in registry.entries] == ["default"]
    # The default resolves whether asked by id or by None.
    assert registry.entry(None).manifest_path == manifest
    assert registry.entry("default").manifest_path == manifest


def _git_in(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def test_plan_file_sha_rebases_onto_the_manifest_dir(tmp_path: Path) -> None:
    # #727 P0-1: plan_file_sha takes the MANIFEST-relative plan path and rebases it onto the
    # manifest's location within the repo before `git log`. Here the manifest lives in a
    # subdir, so `deployments/p.yaml` resolves to `svc/deployments/p.yaml` in the clone.
    cache = tmp_path / "clones"
    clone = cache / "proj"
    (clone / "svc" / "deployments").mkdir(parents=True)
    _git_in(["init", "-b", "main"], clone)
    plan = clone / "svc" / "deployments" / "p.yaml"
    plan.write_text("plan: 1\n", encoding="utf-8")
    _git_in(["add", "svc/deployments/p.yaml"], clone)
    _git_in(["commit", "-m", "add plan"], clone)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(clone), check=True, capture_output=True, text=True
    ).stdout.strip()

    entry = ProjectRegistryEntry(
        id="proj",
        repo=ProjectRepoSource(
            url="https://github.com/acme/proj", manifest="svc/typeflux.project.yaml"
        ),
    )
    registry = ProjectRegistry((entry,), default_id="proj", cache_dir=cache)

    assert registry.plan_file_sha("proj", "deployments/p.yaml") == head
    # An unknown plan file (never committed) → None, never fabricated.
    assert registry.plan_file_sha("proj", "deployments/missing.yaml") is None


def test_plan_file_sha_none_for_local_entry(tmp_path: Path) -> None:
    manifest = tmp_path / "typeflux.project.yaml"
    manifest.write_text("version: '1'\n", encoding="utf-8")
    registry = ProjectRegistry.single(manifest)
    # A local checkout records no git source → no commit provenance to resolve.
    assert registry.plan_file_sha("default", "deployments/p.yaml") is None


def test_load_registry_resolves_manifests_relative_to_file(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "typeflux.project.yaml").write_text("version: '1'\n", encoding="utf-8")
    (tmp_path / "b" / "typeflux.project.yaml").write_text("version: '1'\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "version: '1'\n"
        "default: beta\n"
        "projects:\n"
        "  - id: alpha\n    manifest: a/typeflux.project.yaml\n"
        "  - id: beta\n    manifest: b/typeflux.project.yaml\n",
        encoding="utf-8",
    )

    registry = load_project_registry(registry_file)

    assert [e.id for e in registry.entries] == ["alpha", "beta"]
    assert registry.default_id == "beta"
    assert registry.entry("alpha").manifest_path == (tmp_path / "a" / "typeflux.project.yaml")


def test_registry_runtime_field_defaults_parses_and_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "typeflux.project.yaml").write_text("version: '1'\n", encoding="utf-8")

    def registry_for(runtime_line: str) -> ProjectRegistry:
        registry_file = tmp_path / "typeflux.projects.yaml"
        registry_file.write_text(
            "version: '1'\n"
            "projects:\n"
            "  - id: one\n    manifest: typeflux.project.yaml\n" + runtime_line,
            encoding="utf-8",
        )
        return load_project_registry(registry_file)

    # Absent -> python (every pre-existing project is Python).
    assert registry_for("").entry("one").runtime == "python"
    assert registry_for("    runtime: typescript\n").entry("one").runtime == "typescript"
    # The empty string must never silently mean the default (#619).
    with pytest.raises(ProjectRegistryError, match="'runtime' must be"):
        registry_for("    runtime: ''\n")
    with pytest.raises(ProjectRegistryError, match="'runtime' must be"):
        registry_for("    runtime: rust\n")


def test_unsupported_runtime_fails_closed_and_reports_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A registry with the Python fixture project as default plus a
    # typescript-runtime project: resolution-dependent routes answer 501
    # UnsupportedRuntime; pure-YAML reads keep working; meta/projects tell
    # the truth about resolvability (#619).
    manifest = _setup(tmp_path, monkeypatch)
    ts_dir = tmp_path / "ts-project"
    ts_dir.mkdir()
    (ts_dir / "typeflux.project.yaml").write_text(
        "version: '1'\nname: ts-stub\nworkflows:\n  - id: flow\n    path: workflow.yaml\n",
        encoding="utf-8",
    )
    (ts_dir / "workflow.yaml").write_text("placeholder: true\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "version: '1'\n"
        "default: py\n"
        "projects:\n"
        f"  - id: py\n    manifest: {manifest.name}\n"
        "  - id: ts\n    manifest: ts-project/typeflux.project.yaml\n    runtime: typescript\n",
        encoding="utf-8",
    )
    client = TestClient(create_app_from_registry(registry_file))

    bundle = client.get(
        "/api/v1/projects/ts/workflows/anything/bundle", params={"environment_id": "local"}
    )
    assert bundle.status_code == 501
    body = bundle.json()
    assert body["error"] == "UnsupportedRuntime"
    assert "typescript" in body["message"]

    # Pure-YAML reads stay available: the project is inspectable.
    assert client.get("/api/v1/projects/ts/environments").status_code == 200
    assert client.get("/api/v1/projects/ts/workflows").status_code == 200

    # Every module-importing route fails closed — including the three that
    # resolve the workflow spec for identity/config rather than for a bundle
    # (workers/connections/correlation; found by review, #619).
    for path_suffix in (
        "workflows/flow/workers?environment_id=local",
        "workflows/flow/connections?environment_id=local",
        "workflows/flow/correlation?environment_id=local&execution_id=x",
        "deployments",
    ):
        response = client.get(f"/api/v1/projects/ts/{path_suffix}")
        assert response.status_code == 501, path_suffix
        assert response.json()["error"] == "UnsupportedRuntime", path_suffix

    meta = client.get("/api/v1/projects/ts/meta").json()
    assert meta["runtime"] == "typescript"
    assert meta["capabilities"]["can_resolve"] is False
    assert meta["capabilities"]["can_start"] is False
    # Lifecycle operations need only a binding driver (#618): the
    # ts-plan-argument driver operates TS executions plan-lessly.
    assert meta["capabilities"]["can_review"] is True
    assert meta["capabilities"]["can_cancel"] is True

    default_meta = client.get("/api/v1/meta").json()
    assert default_meta["runtime"] == "python"
    assert default_meta["capabilities"]["can_resolve"] is True

    # An unknown project on a gated route is still a 404 NotFound — the
    # runtime gate runs before _project() and must map KeyError identically
    # (codex finding).
    ghost = client.get(
        "/api/v1/projects/ghost/workflows/flow/bundle", params={"environment_id": "local"}
    )
    assert ghost.status_code == 404
    assert ghost.json()["error"] == "NotFound"

    projects = {item["id"]: item for item in client.get("/api/v1/projects").json()}
    assert projects["ts"]["runtime"] == "typescript"
    assert projects["ts"]["resolvable"] is False
    assert projects["py"]["resolvable"] is True


def test_load_registry_defaults_to_first_entry(tmp_path: Path) -> None:
    (tmp_path / "p.yaml").write_text("version: '1'\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text("projects:\n  - id: only\n    manifest: p.yaml\n", encoding="utf-8")

    assert load_project_registry(registry_file).default_id == "only"


@pytest.mark.parametrize(
    "body, message",
    [
        ("projects: []\n", "non-empty"),
        ("default: ghost\nprojects:\n  - id: a\n    manifest: p.yaml\n", "not a registered id"),
        (
            "projects:\n  - id: a\n    manifest: p.yaml\n  - id: a\n    manifest: p.yaml\n",
            "duplicate ids",
        ),
        ("projects:\n  - manifest: p.yaml\n", "missing a string 'id'"),
    ],
)
def test_load_registry_rejects_malformed(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / "p.yaml").write_text("version: '1'\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(body, encoding="utf-8")

    with pytest.raises(ProjectRegistryError, match=message):
        load_project_registry(registry_file)


# --- Project-scoped routes ----------------------------------------------


def test_default_and_scoped_routes_return_identical_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    default = client.get("/api/v1/workflows")
    scoped = client.get("/api/v1/projects/default/workflows")

    assert default.status_code == 200
    assert scoped.status_code == 200
    assert default.json() == scoped.json()


def test_projects_listing_marks_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    listing = client.get("/api/v1/projects")

    assert listing.status_code == 200
    assert listing.json() == [
        {
            "id": "default",
            "name": "controlplane-demo",
            "manifest_path": str(manifest),
            "default": True,
            "runtime": "python",
            "resolvable": True,
            "source": "local",
            "repo_url": None,
            "repo_ref": None,
            "repo_sha": None,
            "manifest_repo_path": None,
            "last_refresh": None,
            "available": True,
            "detail": None,
        }
    ]


def test_unknown_project_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    client = TestClient(create_app(manifest))

    response = client.get("/api/v1/projects/ghost/meta")

    assert response.status_code == 404
    assert "unknown project" in response.json()["message"]


def test_registry_app_serves_each_project_under_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two checkouts of the same fixture under different ids; the scoped routes
    # must resolve each independently and the listing must show both.
    manifest = _setup(tmp_path, monkeypatch)
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n"
        f"  - id: one\n    manifest: {manifest}\n"
        f"  - id: two\n    manifest: {manifest}\n",
        encoding="utf-8",
    )
    client = TestClient(create_app_from_registry(registry_file))

    ids = [p["id"] for p in client.get("/api/v1/projects").json()]
    one = client.get("/api/v1/projects/one/meta")
    two = client.get("/api/v1/projects/two/meta")
    default = client.get("/api/v1/meta")  # default alias = first entry

    assert ids == ["one", "two"]
    assert one.status_code == 200 and two.status_code == 200
    assert default.json()["project"] == "controlplane-demo"


# --- Resolution lock ----------------------------------------------------


def _application(profile_path: Path, variables: dict[str, str]) -> ProjectEnvironmentApplication:
    app = ProjectEnvironmentApplication(
        environment_id="env",
        environment_name="env",
        profile_path=str(profile_path),
    )
    # `variables` is an excluded field; set it directly.
    object.__setattr__(app, "variables", variables)
    return app


def test_resolution_lock_serializes_disjoint_env_blocks(tmp_path: Path) -> None:
    # Two threads apply disjoint variable sets concurrently. With the
    # process-wide resolution lock, neither ever observes the other's value in
    # os.environ while inside its own env-context block.
    profile = tmp_path / "profile.yaml"
    profile.write_text("x", encoding="utf-8")
    leaked: list[str] = []
    start = threading.Barrier(2)

    def worker(my_key: str, other_key: str) -> None:
        app = _application(profile, {my_key: "mine"})
        start.wait()
        for _ in range(50):
            with project_environment_context(app):
                if os.environ.get(other_key) is not None:
                    leaked.append(other_key)

    t1 = threading.Thread(target=worker, args=("TYPEFLUX_LOCK_A", "TYPEFLUX_LOCK_B"))
    t2 = threading.Thread(target=worker, args=("TYPEFLUX_LOCK_B", "TYPEFLUX_LOCK_A"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert leaked == []


def test_async_env_context_cancelled_acquire_releases_the_lock(tmp_path: Path) -> None:
    # Cancelling a caller mid-acquire (the control-plane Temporal-tier timeout
    # does exactly this, #581) must not leak the env lock: the acquire thread
    # cannot be stopped, so ownership settles through the acquire handoff.
    import asyncio

    from typeflux.project import environment as env_module
    from typeflux.project.environment import async_project_environment_context

    profile = tmp_path / "profile.yaml"
    profile.write_text("x", encoding="utf-8")

    async def main() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with async_project_environment_context(
                _application(profile, {"TYPEFLUX_CANCEL_HOLD": "a"})
            ):
                entered.set()
                await release.wait()

        holder_task = asyncio.create_task(holder())
        await entered.wait()

        async def blocked() -> None:
            async with async_project_environment_context(
                _application(profile, {"TYPEFLUX_CANCEL_WAIT": "b"})
            ):
                raise AssertionError("cancelled before the lock was granted")

        blocked_task = asyncio.create_task(blocked())
        await asyncio.sleep(0.1)  # let it reach the worker-thread acquire
        blocked_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked_task

        release.set()
        await holder_task

        # The orphaned acquire eventually takes the lock and the handoff
        # releases it; without that, this loop times out on a leaked lock.
        deadline = asyncio.get_running_loop().time() + 5
        while not env_module._env_resolution_lock.acquire(blocking=False):
            assert asyncio.get_running_loop().time() < deadline, "env lock leaked"
            await asyncio.sleep(0.02)
        env_module._env_resolution_lock.release()

    asyncio.run(main())


def test_env_lock_survives_loop_teardown_with_an_orphaned_blocked_acquire(
    tmp_path: Path,
) -> None:
    """#597: asyncio.run teardown must not turn an orphaned acquire into a leak.

    Sequence (the CI deadlock, faulthandler-diagnosed): a caller is cancelled
    while its acquire thread is BLOCKED (the lock held elsewhere); teardown's
    _cancel_all_tasks then cancels the pending to_thread wrapper task itself.
    A release keyed on the wrapper's state (task.cancelled()) skips there, so
    the thread's eventual acquisition leaks the process-wide lock — and any
    later blocked acquire wedges the default executor's shutdown join forever.
    The handoff keys the release to the THREAD's acquisition, so the lock is
    free after teardown regardless of task state.
    """
    import asyncio

    from typeflux.project import environment as env_module
    from typeflux.project.environment import async_project_environment_context

    profile = tmp_path / "profile.yaml"
    profile.write_text("x", encoding="utf-8")

    # Hold the lock from OUTSIDE the loop so the worker's acquire thread blocks.
    assert env_module._env_resolution_lock.acquire(blocking=False)
    # Release only once teardown is already joining the executor: the acquire
    # thread then completes with its wrapper task already cancelled.
    releaser = threading.Timer(0.3, env_module._env_resolution_lock.release)
    releaser.daemon = True

    async def blocked() -> None:
        async with async_project_environment_context(
            _application(profile, {"TYPEFLUX_TEARDOWN": "x"})
        ):
            raise AssertionError("the lock was held; entry is unreachable")

    async def main() -> None:
        blocked_task = asyncio.create_task(blocked())
        await asyncio.sleep(0.1)  # let the acquire thread block on the held lock
        blocked_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked_task
        releaser.start()
        # Return with the acquire thread still blocked: asyncio.run now cancels
        # the pending wrapper task and joins the default executor.

    asyncio.run(main())
    releaser.join(timeout=5)

    # Bounded: the thread self-releases via the handoff shortly after taking
    # the lock; poll instead of racing it.
    deadline = time.monotonic() + 5
    while not env_module._env_resolution_lock.acquire(blocking=False):
        assert time.monotonic() < deadline, "env lock leaked across loop teardown (#597)"
        time.sleep(0.02)
    env_module._env_resolution_lock.release()


def test_async_env_context_does_not_deadlock_the_event_loop(tmp_path: Path) -> None:
    # Two coroutines each hold the env-context lock across an await (mimicking
    # a Temporal call). With the async wrapper acquiring in a worker thread,
    # the event loop is never blocked, so both complete. A blocking acquire on
    # the loop thread would hang here forever (guarded by the timeout).
    import asyncio

    from typeflux.project.environment import async_project_environment_context

    profile = tmp_path / "profile.yaml"
    profile.write_text("x", encoding="utf-8")
    order: list[str] = []

    async def worker(name: str, key: str) -> None:
        app = _application(profile, {key: name})
        async with async_project_environment_context(app):
            order.append(f"{name}:enter")
            await asyncio.sleep(0.05)  # stand-in for a Temporal await
            assert os.environ[key] == name  # no other coroutine leaked in
            order.append(f"{name}:exit")

    async def main() -> None:
        await asyncio.wait_for(
            asyncio.gather(
                worker("a", "TYPEFLUX_ASYNC_A"),
                worker("b", "TYPEFLUX_ASYNC_B"),
            ),
            timeout=5,
        )

    asyncio.run(main())

    # Serialized (not interleaved): each enter is immediately followed by its
    # own exit before the other enters.
    assert order in (
        ["a:enter", "a:exit", "b:enter", "b:exit"],
        ["b:enter", "b:exit", "a:enter", "a:exit"],
    )


def test_resolution_lock_allows_nested_same_thread_entry(tmp_path: Path) -> None:
    # A nested same-thread re-entry must not self-deadlock (the reentrancy
    # guard makes the inner acquire a no-op).
    profile = tmp_path / "profile.yaml"
    profile.write_text("x", encoding="utf-8")
    outer = _application(profile, {"TYPEFLUX_NEST_OUTER": "1"})
    inner = _application(profile, {"TYPEFLUX_NEST_INNER": "1"})

    with project_environment_context(outer):
        with project_environment_context(inner):
            assert os.environ["TYPEFLUX_NEST_OUTER"] == "1"
            assert os.environ["TYPEFLUX_NEST_INNER"] == "1"
    assert "TYPEFLUX_NEST_OUTER" not in os.environ
    assert "TYPEFLUX_NEST_INNER" not in os.environ


_MANIFEST_PROJECT = (
    'version: "1"\n'
    "name: {name}\n"
    "workflows:\n  - id: wf\n    path: wf.yaml\n"
    "environments: {{local: env/local.yaml}}\n"
    "policies: {{}}\n"
    "validation: {{targets: {{}}}}\n"
)


def _setup_local_project(tmp_path: Path) -> Path:
    """A minimal local manifest that load_project_spec accepts (name only path)."""
    manifest = tmp_path / "local.project.yaml"
    manifest.write_text(_MANIFEST_PROJECT.format(name="good-local"), encoding="utf-8")
    return manifest


# --- Git-sourced projects (#256 phase 2) --------------------------------

_MANIFEST = (
    'version: "1"\n'
    "name: {name}\n"
    "workflows:\n  - id: wf\n    path: wf.yaml\n"
    "environments:\n  local: env/local.yaml\n"
    "policies: {{}}\n"
    "validation:\n  targets: {{}}\n"
)


def _git(args: list[str], cwd: Path) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _make_remote(tmp_path: Path, name: str) -> Path:
    """A bare repo (the stand-in remote) seeded with a project manifest."""
    import subprocess

    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    (work / "typeflux.project.yaml").write_text(_MANIFEST.format(name=name), encoding="utf-8")
    _git(["add", "."], work)
    _git(["commit", "-m", "init"], work)
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True
    )
    return bare


def _registry_file(tmp_path: Path, bare: Path, *, ref: str = "main") -> Path:
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n"
        "  - id: cloned\n"
        "    repo:\n"
        f"      url: {bare}\n"
        f"      ref: {ref}\n"
        "      manifest: typeflux.project.yaml\n",
        encoding="utf-8",
    )
    return registry_file


def test_git_sourced_project_clones_on_resolve(tmp_path: Path) -> None:
    bare = _make_remote(tmp_path, "git-project")
    registry = load_project_registry(_registry_file(tmp_path, bare), cache_dir=tmp_path / "clones")

    spec = registry.resolve("cloned")

    assert spec.name == "git-project"
    assert (tmp_path / "clones" / "cloned" / ".git").exists()
    # Summary reports the git source.
    summary = next(s for s in registry.summaries() if s.id == "cloned")
    assert summary.source == "git"
    assert summary.repo_url == str(bare)
    # The manifest's path WITHIN the repo — the console builds source links from it (#606).
    assert summary.manifest_repo_path == "typeflux.project.yaml"


def test_git_sourced_project_refresh_picks_up_new_commits(tmp_path: Path) -> None:
    bare = _make_remote(tmp_path, "before")
    registry = load_project_registry(_registry_file(tmp_path, bare), cache_dir=tmp_path / "clones")
    assert registry.resolve("cloned").name == "before"

    # Push a new commit to the remote, then refresh.
    work2 = tmp_path / "work2"
    import subprocess

    subprocess.run(["git", "clone", str(bare), str(work2)], check=True, capture_output=True)
    (work2 / "typeflux.project.yaml").write_text(_MANIFEST.format(name="after"), encoding="utf-8")
    _git(["commit", "-am", "rename"], work2)
    _git(["push", "origin", "main"], work2)

    result = registry.refresh("cloned")

    assert result.source == "git"
    assert result.refreshed is True
    assert result.sha
    assert registry.resolve("cloned").name == "after"


def test_local_project_refresh_is_a_noop(tmp_path: Path) -> None:
    (tmp_path / "p.yaml").write_text(_MANIFEST.format(name="local"), encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text("projects:\n  - id: local\n    manifest: p.yaml\n", encoding="utf-8")
    registry = load_project_registry(registry_file)

    result = registry.refresh("local")

    assert result.source == "local"
    assert result.refreshed is False


@pytest.mark.parametrize(
    "body, message",
    [
        (
            "projects:\n  - id: a\n    manifest: p.yaml\n    repo:\n      url: x\n",
            "exactly one of 'manifest' or 'repo'",
        ),
        ("projects:\n  - id: a\n", "exactly one of 'manifest' or 'repo'"),
    ],
)
def test_registry_rejects_ambiguous_source(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / "p.yaml").write_text("version: '1'\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(body, encoding="utf-8")

    with pytest.raises(ProjectRegistryError, match=message):
        load_project_registry(registry_file)


def test_repo_manifest_escape_is_rejected(tmp_path: Path) -> None:
    from typeflux.controlplane.git_source import (
        ProjectRepoSource,
        ProjectSourceError,
        ensure_repo_manifest,
    )

    bare = _make_remote(tmp_path, "esc")
    repo = ProjectRepoSource(url=str(bare), ref="main", manifest="../escape.yaml")

    with pytest.raises(ProjectSourceError, match="escapes the project clone"):
        ensure_repo_manifest(repo, cache_dir=tmp_path / "clones", project_id="cloned")


def test_refresh_endpoint_invalidates_operations_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A POST /refresh drops the project's pinned operations so the next call
    # re-resolves. Use a local project (no git needed) and seed a fake cache.
    manifest = _setup(tmp_path, monkeypatch)
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        f"projects:\n  - id: solo\n    manifest: {manifest}\n", encoding="utf-8"
    )
    from typeflux.controlplane import create_app_from_registry

    app = create_app_from_registry(registry_file)

    from typeflux.controlplane.api import _PinnedOperations

    class _Stub:
        shutdown_called = False

        def shutdown(self) -> None:
            type(self).shutdown_called = True

    app.state.operations_cache[("solo", "wf", "local", (), None)] = _PinnedOperations(
        operations=_Stub(), spec_digest=None, pinned_at="2026-01-01T00:00:00+00:00"
    )
    client = TestClient(app)

    response = client.post("/api/v1/projects/solo/refresh")

    assert response.status_code == 200
    assert response.json()["source"] == "local"
    assert _Stub.shutdown_called is True
    assert app.state.operations_cache == {}


def test_git_refresh_records_provenance(tmp_path: Path) -> None:
    bare = _make_remote(tmp_path, "prov")
    registry = load_project_registry(_registry_file(tmp_path, bare), cache_dir=tmp_path / "clones")
    registry.resolve("cloned")  # clone

    result = registry.refresh("cloned")

    assert result.refreshed is True
    assert result.sha and result.refreshed_at
    summary = next(s for s in registry.summaries() if s.id == "cloned")
    assert summary.repo_sha == result.sha
    assert summary.last_refresh is not None
    assert summary.last_refresh.refreshed is True


def test_git_refresh_failure_preserves_prior_clone(tmp_path: Path) -> None:
    import shutil

    bare = _make_remote(tmp_path, "resilient")
    registry = load_project_registry(_registry_file(tmp_path, bare), cache_dir=tmp_path / "clones")
    assert registry.resolve("cloned").name == "resilient"  # clone the good state

    # Make the remote unreachable, then refresh — fetch fails.
    shutil.rmtree(bare)
    result = registry.refresh("cloned")

    assert result.refreshed is False
    assert result.source == "git"
    assert result.detail  # carries the git error
    # The previous good clone is untouched, so reads still resolve.
    assert registry.resolve("cloned").name == "resilient"
    summary = next(s for s in registry.summaries() if s.id == "cloned")
    assert summary.repo_sha is not None
    assert summary.last_refresh is not None and summary.last_refresh.refreshed is False


def test_refresh_provenance_via_projects_endpoint(tmp_path: Path) -> None:
    bare = _make_remote(tmp_path, "via-api")
    registry_file = _registry_file(tmp_path, bare)
    app = create_app_from_registry(registry_file, clone_cache=tmp_path / "clones")
    client = TestClient(app)

    refreshed = client.post("/api/v1/projects/cloned/refresh")
    listing = client.get("/api/v1/projects")

    assert refreshed.status_code == 200
    assert refreshed.json()["refreshed"] is True
    entry = next(p for p in listing.json() if p["id"] == "cloned")
    assert entry["source"] == "git"
    assert entry["repo_sha"] == refreshed.json()["sha"]
    assert entry["last_refresh"]["refreshed"] is True


def test_git_sourced_python_project_resolves_bundle(tmp_path: Path) -> None:
    # A Git-sourced project whose activities live in a Python package must
    # resolve end-to-end: the clone root has to be on sys.path so
    # import_module(project.<...>) finds the package (#256 follow-up).
    import subprocess
    import sys

    from typeflux.project import resolve_workflow_bundle

    work = tmp_path / "work"
    pkg = work / "gitresolvepkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "schemas.py").write_text(
        "from pydantic import BaseModel\n"
        "class In(BaseModel):\n    text: str\n"
        "class Out(BaseModel):\n    label: str\n",
        encoding="utf-8",
    )
    (work / "wf.yaml").write_text(
        "project: gitresolvepkg\n"
        "name: demo\n"
        "task_queue: demo-q\n"
        "runtime:\n"
        "  temporal: {address: localhost:7233}\n"
        "  registry: {type: inline, prompts: {classify: 'classify {{text}}'}}\n"
        "  provider: {type: fake, model: fake-1}\n"
        "  observability: {type: none}\n"
        "activities:\n"
        "  definitions:\n"
        "    - name: classify\n"
        "      input: gitresolvepkg.schemas:In\n"
        "      output: gitresolvepkg.schemas:Out\n"
        "      prompt: classify\n"
        "workflow:\n"
        "  name: DemoWorkflow\n"
        "  input: gitresolvepkg.schemas:In\n"
        "  output: gitresolvepkg.schemas:Out\n"
        "  steps:\n    - id: classify\n      activity: classify\n",
        encoding="utf-8",
    )
    (work / "env").mkdir()
    (work / "env" / "local.yaml").write_text("version: '1'\nname: local\n", encoding="utf-8")
    (work / "policies").mkdir()
    (work / "policies" / "base.yaml").write_text(
        "version: '1'\nname: base\nproviders: {allowed: {fake: {models: [fake-1]}}}\n",
        encoding="utf-8",
    )
    (work / "typeflux.project.yaml").write_text(
        "version: '1'\nname: gitresolve\n"
        "workflows:\n  - id: demo\n    path: wf.yaml\n"
        "environments: {local: env/local.yaml}\n"
        "policies: {base: policies/base.yaml}\n"
        "validation: {targets: {default: {workflows: [demo], environment: local, policies: [base]}}}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True)
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=work,
        check=True,
    )
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True
    )

    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n"
        "  - id: gitresolve\n"
        "    repo:\n"
        f"      url: {bare}\n      ref: main\n      manifest: typeflux.project.yaml\n",
        encoding="utf-8",
    )
    clone_root = tmp_path / "clones" / "gitresolve"
    try:
        registry = load_project_registry(registry_file, cache_dir=tmp_path / "clones")
        project = registry.resolve("gitresolve")  # clones + puts the root on sys.path
        bundle = resolve_workflow_bundle(project, workflow_id="demo", environment_id="local")
        assert bundle.workflow.workflow_name == "DemoWorkflow"
        assert bundle.validation.ok
    finally:
        # The clone root was added to sys.path and the package imported; undo
        # both so the process-global state does not leak to other tests.
        sys.path[:] = [p for p in sys.path if p != str(clone_root.resolve())]
        for name in list(sys.modules):
            if name == "gitresolvepkg" or name.startswith("gitresolvepkg."):
                del sys.modules[name]


def test_summaries_degrade_when_a_git_source_is_unreachable(tmp_path: Path) -> None:
    # One unreachable Git repo must not fail the whole listing — the healthy
    # local project still appears; the bad one is marked unavailable.
    local = _setup_local_project(tmp_path)
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n"
        f"  - id: good\n    manifest: {local}\n"
        "  - id: bad\n    repo:\n"
        f"      url: {tmp_path / 'does-not-exist.git'}\n      ref: main\n",
        encoding="utf-8",
    )
    registry = load_project_registry(registry_file, cache_dir=tmp_path / "clones")

    summaries = {s.id: s for s in registry.summaries()}

    assert summaries["good"].available is True
    assert summaries["bad"].available is False
    assert summaries["bad"].detail  # carries the git error
    assert summaries["bad"].source == "git"


def test_summaries_degrade_when_a_manifest_is_schema_invalid(tmp_path: Path) -> None:
    # A schema-invalid manifest raises pydantic ValidationError during load
    # (#344). ValidationError subclasses ValueError, so the per-entry degrade
    # handler catches it — one unloadable project must not 500 the listing.
    good = _setup_local_project(tmp_path)
    invalid = tmp_path / "invalid.project.yaml"
    # `name` of the wrong type + `workflows` not a list → ValidationError, not a
    # plain ValueError from the model_validator.
    invalid.write_text("version: '1'\nname: 123\nworkflows: not-a-list\n", encoding="utf-8")
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n"
        f"  - id: good\n    manifest: {good}\n"
        f"  - id: invalid\n    manifest: {invalid}\n",
        encoding="utf-8",
    )
    registry = load_project_registry(registry_file, cache_dir=tmp_path / "clones")

    summaries = {s.id: s for s in registry.summaries()}

    assert summaries["good"].available is True
    assert summaries["invalid"].available is False
    assert summaries["invalid"].detail  # carries the validation error
    assert summaries["invalid"].source == "local"


def test_tag_ref_refresh_picks_up_a_moved_tag(tmp_path: Path) -> None:
    # reset --hard FETCH_HEAD (not origin/<ref>) makes a tag-pinned project
    # refreshable even though tags have no origin/<tag> remote-tracking ref.
    import subprocess

    bare = _make_remote(tmp_path, "before")
    work = tmp_path / "tagwork"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "v1"], cwd=work, check=True, capture_output=True)
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n  - id: cloned\n    repo:\n"
        f"      url: {bare}\n      ref: v1\n      manifest: typeflux.project.yaml\n",
        encoding="utf-8",
    )
    registry = load_project_registry(registry_file, cache_dir=tmp_path / "clones")
    assert registry.resolve("cloned").name == "before"

    # Move the tag to a new commit and force-push it.
    (work / "typeflux.project.yaml").write_text(_MANIFEST.format(name="after"), encoding="utf-8")
    _git(["commit", "-am", "v1.1"], work)
    subprocess.run(["git", "tag", "-f", "v1"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "-f", "origin", "v1"], cwd=work, check=True, capture_output=True)

    result = registry.refresh("cloned")

    assert result.refreshed is True
    assert registry.resolve("cloned").name == "after"


def test_interrupted_clone_directory_self_heals(tmp_path: Path) -> None:
    # A leftover non-.git directory (an interrupted clone) must not wedge the
    # project — ensure_repo_manifest clears it and re-clones.
    from typeflux.controlplane.git_source import ProjectRepoSource, ensure_repo_manifest

    bare = _make_remote(tmp_path, "heal")
    cache = tmp_path / "clones"
    (cache / "cloned").mkdir(parents=True)
    (cache / "cloned" / "leftover.txt").write_text("junk", encoding="utf-8")  # no .git

    repo = ProjectRepoSource(url=str(bare), ref="main", manifest="typeflux.project.yaml")
    manifest = ensure_repo_manifest(repo, cache_dir=cache, project_id="cloned")

    assert manifest.exists()
    assert (cache / "cloned" / ".git").exists()


def test_cli_serve_clone_cache_requires_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.controlplane import __main__ as cli

    with pytest.raises(SystemExit):
        cli.main(["serve", "manifest.yaml", "--clone-cache", str(tmp_path)])


# --- Git credential scrubbing (#322) ------------------------------------------


def test_scrub_git_url_strips_userinfo() -> None:
    from typeflux.controlplane.git_source import scrub_git_url

    assert scrub_git_url("https://user:tok@github.com/o/r.git") == "https://github.com/o/r.git"
    assert scrub_git_url("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert scrub_git_url("https://user:tok@host:8443/r") == "https://host:8443/r"
    # Any scheme with userinfo is scrubbed — not just a whitelist.
    assert scrub_git_url("git+https://user:tok@host/r") == "git+https://host/r"
    # ssh shorthand carries no token and is left untouched.
    assert scrub_git_url("git@github.com:o/r.git") == "git@github.com:o/r.git"
    assert scrub_git_url(None) is None


def test_scrub_git_text_strips_embedded_userinfo() -> None:
    from typeflux.controlplane.git_source import scrub_git_text

    scrubbed = scrub_git_text("git clone https://user:s3cr3t@github.com/o/r.git failed: fatal")
    assert "s3cr3t" not in scrubbed
    assert "https://github.com/o/r.git" in scrubbed
    # Uppercase scheme must not bypass scrubbing.
    assert "tok" not in scrub_git_text("remote HTTPS://user:tok@host/r unreachable")


def test_git_credentials_scrubbed_from_summaries_and_refresh(tmp_path: Path) -> None:
    # A credential-bearing remote must never leak through /projects or refresh
    # responses. The clone uses the real URL but every output surface is scrubbed.
    registry_file = tmp_path / "typeflux.projects.yaml"
    registry_file.write_text(
        "projects:\n"
        "  - id: secret\n"
        "    repo:\n"
        "      url: https://user:s3cr3t-token@127.0.0.1:1/repo.git\n"  # port 1: refused fast
        "      ref: main\n"
        "      manifest: typeflux.project.yaml\n",
        encoding="utf-8",
    )
    registry = load_project_registry(registry_file, cache_dir=tmp_path / "clones")

    summary = next(s for s in registry.summaries() if s.id == "secret")
    assert "s3cr3t-token" not in (summary.repo_url or "")
    assert summary.repo_url == "https://127.0.0.1:1/repo.git"
    assert "s3cr3t-token" not in (summary.manifest_path or "")
    assert "s3cr3t-token" not in (summary.detail or "")

    result = registry.refresh("secret")
    assert result.refreshed is False
    assert "s3cr3t-token" not in (result.detail or "")
