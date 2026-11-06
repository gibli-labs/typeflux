"""Scope and false-pass guards. No Docker daemon or paid model calls are needed."""

import difflib
import json

import pytest
from test_project_graph import graph_repo as graph_repo
from tools.development.study86 import validate


def diff(path, before, after):
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
        )
    )


def test_scoped_patch_applies_without_changing_other_inputs(graph_repo):
    path = "src/worker.py"
    original = (graph_repo / path).read_text()
    patch = diff(path, original, original.replace("original", "changed"))
    assert validate.apply_patch(graph_repo, patch, [path]) == [path]
    assert "changed" in (graph_repo / path).read_text()


def test_policy_weakening_path_rejects_entire_patch_before_mutation(graph_repo):
    path = "src/worker.py"
    original = (graph_repo / path).read_text()
    policy = "tools/development/checks.json"
    policy_before = (graph_repo / policy).read_text()
    patch = diff(path, original, original.replace("original", "changed"))
    patch += diff(policy, policy_before, "{}\n")
    with pytest.raises(validate.RejectedPatch, match="edit scope"):
        validate.apply_patch(graph_repo, patch, [path])
    assert (graph_repo / path).read_text() == original
    assert (graph_repo / policy).read_text() == policy_before


def test_allowed_path_cannot_become_a_symlink(graph_repo):
    secret = graph_repo / "secret"
    secret.write_text("controlled original")
    patch = (
        "diff --git a/src/new.py b/src/new.py\nnew file mode 120000\n"
        "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+"
        + str(secret)
        + "\n\\ No newline at end of file\n"
    )
    with pytest.raises(validate.RejectedPatch, match="regular file"):
        validate.apply_patch(graph_repo, patch, ["src/new.py"])
    assert secret.read_text() == "controlled original"


def test_heldout_cannot_overwrite_an_editable_implementation(graph_repo):
    task = {"heldout": ["src/worker.py"], "editable": ["src/worker.py"]}
    with pytest.raises(ValueError, match="overwrite"):
        validate.overlay_heldout(graph_repo, graph_repo, task)


@pytest.fixture
def native(tmp_path, monkeypatch):
    state = {
        "report": {
            "passed": True,
            "checks": [
                {
                    "kind": "python",
                    "exitCode": 0,
                    "cases": 3,
                    "failed": 0,
                    "errors": 0,
                    "skipped": 0,
                    "passed": True,
                }
            ],
        },
        "cleanup": [],
        "exitCode": 0,
        "oom": False,
        "created": 0,
    }

    class Target:
        def __init__(self, output, context):
            output.mkdir()
            self.output = output

        def create(self, *args):
            state["created"] += 1
            assert args[args.index("--network") + 1] == "none"
            assert "--read-only" in args
            (self.output.parent / "native/results.json").write_text(json.dumps(state["report"]))
            return {"id": "owned"}

        def start(self, resource):
            assert resource["id"] == "owned"

        def docker(self, *args, **kwargs):
            if state.get("timeout"):
                import subprocess

                raise subprocess.TimeoutExpired("owned docker wait", 180)
            return str(state["exitCode"])

        def inspect(self, resource):
            return {
                "State": {"ExitCode": state["exitCode"], "OOMKilled": state["oom"]},
                "Config": {"User": "10001:10001"},
                "HostConfig": state.get(
                    "limits",
                    {
                        "NanoCpus": 2_000_000_000,
                        "Memory": 2_147_483_648,
                        "PidsLimit": 256,
                        "ReadonlyRootfs": True,
                        "NetworkMode": "none",
                        "Tmpfs": {"/tmp": "rw,size=512m,mode=1777"},
                    },
                ),
            }

        def cleanup(self):
            return state["cleanup"]

    monkeypatch.setattr(validate, "Target", Target)
    task = {"id": "fixture", "expectedChecks": {"python": 3}}

    def run():
        return validate.execute_native(tmp_path, tmp_path / "execution", "sha256:" + "a" * 64, task)

    return state, run


def test_native_complete_observation_is_accepted(native):
    state, run = native
    assert run()["passed"] and state["created"] == 1


@pytest.mark.parametrize("change", ["empty", "missing-cases", "skipped", "failed"])
def test_native_green_summary_cannot_hide_missing_or_failed_cases(native, change):
    state, run = native
    if change == "empty":
        state["report"]["checks"] = []
    else:
        case = state["report"]["checks"][0]
        case[{"missing-cases": "cases", "skipped": "skipped", "failed": "failed"}[change]] = (
            0 if change == "missing-cases" else 1
        )
    assert run()["passed"] is False


@pytest.mark.parametrize("failure", ["cleanup", "exit", "oom"])
def test_native_success_requires_process_and_owned_cleanup_success(native, failure):
    state, run = native
    if failure == "cleanup":
        state["cleanup"] = ["owned"]
    elif failure == "exit":
        state["exitCode"] = 1
    else:
        state["oom"] = True
    if failure == "cleanup":
        with pytest.raises(RuntimeError, match="cleanup incomplete"):
            run()
    else:
        assert run()["passed"] is False


@pytest.mark.parametrize("change", ["permissions", "trigger", "existing-step", "early-upload"])
def test_ci_diagnostic_upload_cannot_weaken_or_precede_existing_gate(change):
    before = """on: workflow_run
permissions: {checks: write}
jobs:
  accept:
    steps:
      - {run: verify-existing-policy}
      - {uses: 'actions/upload-artifact@pinned', with: {path: existing}}
"""
    after = (
        before
        + "      - {uses: 'actions/upload-artifact@pinned', if: 'always()', with: {path: '.project-graph/accepted/job-observations/'}}\n"
    )
    assert validate.workflow_preserved(before, after)
    if change == "early-upload":
        import yaml

        data = yaml.load(after, Loader=yaml.BaseLoader)
        steps = data["jobs"]["accept"]["steps"]
        steps.insert(0, steps.pop())
        after = yaml.safe_dump(data)
    else:
        after = after.replace(
            {
                "permissions": "checks: write",
                "trigger": "workflow_run",
                "existing-step": "verify-existing-policy",
            }[change],
            {
                "permissions": "contents: write",
                "trigger": "pull_request_target",
                "existing-step": "echo ignored",
            }[change],
        )
    assert not validate.workflow_preserved(before, after)


def test_native_refuses_actual_resource_limit_drift_before_candidate_start(native):
    state, run = native
    state["limits"] = {
        "NanoCpus": 0,
        "Memory": 0,
        "PidsLimit": 0,
        "ReadonlyRootfs": True,
        "NetworkMode": "none",
        "Tmpfs": {"/tmp": "rw,size=512m,mode=1777"},
    }
    with pytest.raises(RuntimeError, match="actual Docker restrictions"):
        run()


def test_container_wait_timeout_is_a_failed_candidate_outcome(native):
    state, run = native
    state["timeout"] = True
    result = run()
    assert not result["passed"] and result["candidateTimedOut"]
    assert result["failureClass"] == "implementation-timeout" and result["cleanupFailures"] == []
