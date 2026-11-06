"""Ownership/refusal tests; these are not deployed journey or recovery evidence."""

import json
from pathlib import Path

import pytest
from tools.development.deployed_review.prepare import prepare
from tools.development.deployed_review.run import LABEL, Target

from typeflux.project import load_project_spec
from typeflux.project.loader import validate_project


def test_existing_attempt_is_never_overwritten(tmp_path):
    output = tmp_path / "attempt"
    output.mkdir()
    marker = output / "report.json"
    marker.write_text('{"passed": false}')
    with pytest.raises(FileExistsError):
        Target(output, "desktop-linux")
    assert marker.read_text() == '{"passed": false}'


@pytest.mark.parametrize("mismatch", ["owner", "name", "id"])
def test_cleanup_refuses_another_resource(tmp_path, monkeypatch, mismatch):
    target = Target(tmp_path / "attempt", "desktop-linux")
    resource = {
        "kind": "container",
        "role": "worker",
        "name": "owned",
        "id": "abc",
        "state": "created",
    }
    target.resources.append(resource)
    info = {"Id": "abc", "Name": "/owned", "Config": {"Labels": {LABEL: target.owner}}}
    if mismatch == "owner":
        info["Config"]["Labels"][LABEL] = "unrelated"
    else:
        info[{"name": "Name", "id": "Id"}[mismatch]] = "unrelated"
    commands = []

    def docker(*args, **kwargs):
        commands.append(args)
        assert args[:2] == ("container", "inspect"), "unowned resource must never be mutated"
        return json.dumps([info])

    monkeypatch.setattr(target, "docker", docker)
    assert target.cleanup() == ["worker"]
    assert len(commands) == 1
    assert resource["state"] == "created"


def test_ambiguous_create_is_reconciled_but_remains_a_failed_attempt(tmp_path, monkeypatch):
    target = Target(tmp_path / "attempt", "desktop-linux")
    commands = []

    def docker(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("container", "create"):
            recorded = json.loads((target.output / "resources.json").read_text())
            assert recorded["resources"][0]["state"] == "pending"
            raise TimeoutError("create acknowledgement lost")
        if args[:2] == ("container", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": "abc",
                        "Name": "/" + target.resources[0]["name"],
                        "Config": {"Labels": {LABEL: target.owner}},
                    }
                ]
            )
        return ""

    monkeypatch.setattr(target, "docker", docker)
    with pytest.raises(TimeoutError, match="acknowledgement lost"):
        target.create("container", "worker", "sha256:fake")
    assert sum(command[:2] == ("container", "create") for command in commands) == 1
    assert target.resources[0]["state"] == "created-after-error"
    assert target.cleanup() == []
    assert target.resources[0]["state"] == "removed"


def test_prepared_version_projects_pass_real_manifest_validation(tmp_path):
    root = Path(__file__).resolve().parents[3]
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    output = tmp_path / "prepared"
    prepare(root, artifacts, output)
    for edition in ["python", "typescript"]:
        for manifest in (output / edition).rglob("typeflux.project.yaml"):
            project = load_project_spec(manifest)
            report = validate_project(project)
            assert report.ok, report
    assert not (output / "python/src").exists(), (
        "consumer image context must not contain SDK sources"
    )
