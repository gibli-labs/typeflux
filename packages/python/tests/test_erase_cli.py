"""The ``typeflux erase`` CLI (#715 slice 5): arg parsing, the typed confirmation
gate, surface selection, and a full cache-only dry-run through a real resolved
project environment (no Temporal server needed — the client connects only when the
temporal surface is selected).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from typeflux.project import __main__ as project_cli


def _dedent(content: str) -> str:
    lines = content.strip("\n").splitlines()
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:] for line in lines) + "\n"


def _write_erase_fixture(
    tmp_path: Path, *, env_extra: str = "", observability: str = "none"
) -> Path:
    package_dir = tmp_path / "demo_project"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "backends.py").write_text(
        _dedent(
            """
            import os

            # Recorded at __init__ time so a test can prove backends are constructed
            # INSIDE the environment overlay (#715 fix round, item 1).
            SEEN_MARKERS: list = []


            class RecordingCacheStore:
                def __init__(self) -> None:
                    SEEN_MARKERS.append(os.environ.get("ERASE_TEST_MARKER"))

                def get(self, key):
                    return None

                def set(self, key, record):
                    return None
            """
        ),
        encoding="utf-8",
    )
    (package_dir / "schemas.py").write_text(
        _dedent(
            """
            from pydantic import BaseModel


            class InputModel(BaseModel):
                value: str


            class OutputModel(BaseModel):
                value: str
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "workflow.yaml").write_text(
        _dedent(
            """
            project: demo_project
            name: demo
            task_queue: demo-queue
            runtime:
              temporal:
                address: localhost:7233
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
              observability:
                type: {observability}
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ).replace("{observability}", observability),
        encoding="utf-8",
    )
    (tmp_path / "env.yaml").write_text('version: "1"\nname: local\n' + env_extra, encoding="utf-8")
    project_path = tmp_path / "typeflux.project.yaml"
    project_path.write_text(
        _dedent(
            """
            version: "1"
            name: demo-project
            workflows:
              - id: workflow
                path: workflow.yaml
            environments:
              local: env.yaml
            """
        ),
        encoding="utf-8",
    )
    return project_path


def _erase_argv(project_path: Path, *extra: str) -> list[str]:
    return [
        "erase",
        str(project_path),
        "--workflow",
        "workflow",
        "--environment",
        "local",
        "--subject",
        "subject-0001",
        *extra,
    ]


def test_execute_on_temporal_surface_requires_the_acknowledgment_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    exit_code = project_cli.main(_erase_argv(project_path, "--execute"))
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "IRREVERSIBLE" in captured.err
    assert "--acknowledge-irreversible" in captured.err


def test_execute_without_temporal_surface_needs_no_acknowledgment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    # cache-only execute passes the gate (no irreversible temporal mutation); it
    # proceeds into the seam and succeeds with the surface honestly skipped
    # (no cache store injected).
    exit_code = project_cli.main(
        _erase_argv(project_path, "--execute", "--surface", "cache", "--json")
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    receipt = json.loads(captured.out)
    assert receipt["dry_run"] is False
    assert receipt["surfaces"]["cache"]["status"] == "skipped"
    assert "no cache store" in receipt["surfaces"]["cache"]["skip_reason"]


def test_unknown_surface_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    exit_code = project_cli.main(_erase_argv(project_path, "--surface", "provider_logs"))
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "unknown surface" in captured.err


def test_bad_since_timestamp_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    exit_code = project_cli.main(_erase_argv(project_path, "--since", "not-a-date"))
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--since" in captured.err
    assert "ISO-8601" in captured.err


def test_non_positive_limit_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    for raw in ("0", "-1"):
        exit_code = project_cli.main(_erase_argv(project_path, "--limit", raw))
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "--limit must be a positive integer" in captured.err


def test_langfuse_is_configured_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    # #715 Bugbot: a whitespace-only key is not a credential — it must read as
    # unconfigured, never build a client that fails at runtime.
    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert LangfuseObservabilityBackend.is_configured() is False
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "   ")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "\t")
    assert LangfuseObservabilityBackend.is_configured() is False
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "  ")
    assert LangfuseObservabilityBackend.is_configured() is False
    # Padded-but-real keys ARE configured (and from_env strips the same way).
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", " pk-test ")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", " sk-test ")
    assert LangfuseObservabilityBackend.is_configured() is True


def test_whitespace_langfuse_creds_report_the_surface_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A langfuse-observability project with whitespace-only creds: the erase CLI
    # must report the surface honestly skipped-with-reason, not build a client
    # that fails per subject at runtime.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "   ")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "\t")
    project_path = _write_erase_fixture(tmp_path, observability="langfuse")
    exit_code = project_cli.main(_erase_argv(project_path, "--surface", "langfuse", "--json"))
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    receipt = json.loads(captured.out)
    langfuse_surface = receipt["surfaces"]["langfuse"]
    assert langfuse_surface["status"] == "skipped"
    assert "LANGFUSE_PUBLIC_KEY" in langfuse_surface["skip_reason"]


def test_spec_declared_langfuse_creds_drive_the_erase_surface(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#793: a workflow whose Langfuse credentials are spec-declared (value_from) must not
    be skipped as unconfigured by the erase CLI — the spec is in hand, resolution mirrors
    the worker build (spec wins, env fallback)."""
    monkeypatch.setenv("SPEC_LF_PUBLIC", "pk-from-spec")
    monkeypatch.setenv("SPEC_LF_SECRET", "sk-from-spec")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    project_path = _write_erase_fixture(
        tmp_path,
        # Post-dedent the fixture's `type:` sits at 4 spaces; keep the block aligned.
        observability=(
            "langfuse\n"
            "    langfuse:\n"
            "      public_key: { value_from: { env: SPEC_LF_PUBLIC } }\n"
            "      secret_key: { value_from: { env: SPEC_LF_SECRET } }"
        ),
    )

    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    captured_kwargs: dict[str, object] = {}

    from typeflux.observability.inspect import SubjectTraceDeletionReport

    class _StubReader:
        def delete_traces_for_subject(self, subject_id, *, dry_run=True, since=None, until=None):
            return SubjectTraceDeletionReport(subject_id=subject_id, dry_run=dry_run)

    class _StubBackend:
        reader = _StubReader()

    def _fake_from_env(cls, **kwargs):
        captured_kwargs.update(kwargs)
        return _StubBackend()

    monkeypatch.setattr(LangfuseObservabilityBackend, "from_env", classmethod(_fake_from_env))
    exit_code = project_cli.main(_erase_argv(project_path, "--surface", "langfuse", "--json"))
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    receipt = json.loads(captured.out)
    # The surface is DRIVEN (dry-run report), not skipped-as-unconfigured.
    assert receipt["surfaces"]["langfuse"]["status"] != "skipped"
    assert captured_kwargs["public_key"] == "pk-from-spec"
    assert captured_kwargs["secret_key"] == "sk-from-spec"


def test_mixed_spec_and_env_langfuse_creds_drive_the_erase_surface(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex round 2: a MIXED pair (spec public_key, env secret_key) is configured — the
    guard combines per field exactly like from_env's fallback."""
    monkeypatch.setenv("SPEC_LF_PUBLIC", "pk-from-spec")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-from-env")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    project_path = _write_erase_fixture(
        tmp_path,
        observability=(
            "langfuse\n    langfuse:\n      public_key: { value_from: { env: SPEC_LF_PUBLIC } }"
        ),
    )

    from typeflux.observability.inspect import SubjectTraceDeletionReport
    from typeflux.observability.langfuse import LangfuseObservabilityBackend

    class _StubReader:
        def delete_traces_for_subject(self, subject_id, *, dry_run=True, since=None, until=None):
            return SubjectTraceDeletionReport(subject_id=subject_id, dry_run=dry_run)

    class _StubBackend:
        reader = _StubReader()

    monkeypatch.setattr(
        LangfuseObservabilityBackend, "from_env", classmethod(lambda cls, **kw: _StubBackend())
    )
    exit_code = project_cli.main(_erase_argv(project_path, "--surface", "langfuse", "--json"))
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    receipt = json.loads(captured.out)
    assert receipt["surfaces"]["langfuse"]["status"] != "skipped"


def test_dry_run_and_execute_are_mutually_exclusive(tmp_path: Path) -> None:
    project_path = _write_erase_fixture(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        project_cli.main(_erase_argv(project_path, "--dry-run", "--execute"))
    assert excinfo.value.code == 2


def test_subject_is_required(tmp_path: Path) -> None:
    project_path = _write_erase_fixture(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        project_cli.main(
            ["erase", str(project_path), "--workflow", "workflow", "--environment", "local"]
        )
    assert excinfo.value.code == 2


def test_cache_only_dry_run_emits_receipt_json_through_a_resolved_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    exit_code = project_cli.main(
        _erase_argv(
            project_path,
            "--surface",
            "cache",
            "--cache-store-class",
            "typeflux.execution.cache:InMemoryCacheStore",
            "--actor",
            "compliance-ops",
            "--json",
        )
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    receipt = json.loads(captured.out)
    assert receipt["dry_run"] is True
    assert receipt["actor"] == "compliance-ops"
    assert receipt["subject_ids"] == ["subject-0001"]
    # Deselected surfaces are loudly skipped, never omitted.
    assert receipt["surfaces"]["temporal"]["status"] == "skipped"
    assert receipt["surfaces"]["langfuse"]["status"] == "skipped"
    cache_surface = receipt["surfaces"]["cache"]
    assert cache_surface["status"] == "ok"
    assert cache_surface["reports"][0]["keys_found"] == 0
    # The document-only surfaces are always present.
    assert {note["surface"] for note in receipt["unreachable"]} == {
        "provider_logs",
        "exported_artifacts",
        "mixed_workflow_payloads",
    }


def test_human_output_names_the_receipt_as_the_proof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    exit_code = project_cli.main(
        _erase_argv(
            project_path,
            "--surface",
            "cache",
            "--cache-store-class",
            "typeflux.execution.cache:InMemoryCacheStore",
            "--actor",
            "compliance-ops",
        )
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "DRY RUN" in captured.out
    assert "proof of erasure" in captured.out
    assert "Unreachable surfaces" in captured.out


def test_backends_are_constructed_inside_the_environment_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # #715 fix round item 1: a backend reading env at __init__ must see the
    # environment's variable overlay, not the ambient shell env — otherwise an
    # --execute could shred/delete against the wrong backing store.
    monkeypatch.delenv("ERASE_TEST_MARKER", raising=False)
    for name in tuple(sys.modules):
        if name == "demo_project" or name.startswith("demo_project."):
            del sys.modules[name]
    project_path = _write_erase_fixture(
        tmp_path, env_extra="variables:\n  ERASE_TEST_MARKER: from-overlay\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        exit_code = project_cli.main(
            _erase_argv(
                project_path,
                "--surface",
                "cache",
                "--cache-store-class",
                "demo_project.backends:RecordingCacheStore",
                "--json",
            )
        )
        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        backends = sys.modules["demo_project.backends"]
        assert backends.SEEN_MARKERS == ["from-overlay"]
    finally:
        for name in tuple(sys.modules):
            if name == "demo_project" or name.startswith("demo_project."):
                del sys.modules[name]


def test_codec_free_spec_strips_the_payload_codec_and_keeps_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #715 fix round item 4: the erase client never encodes/decodes payloads, so a
    # subject_scope spec without an injected keystore must not block execution
    # deletion behind the codec's fail-closed keystore requirement.
    from temporalio.contrib.pydantic import pydantic_data_converter

    from typeflux.yaml.loader import load_yaml_spec
    from typeflux.yaml.payload_codec import PayloadCodecError
    from typeflux.yaml.runtime import _codec_data_converter

    monkeypatch.setenv("TF_ERASE_CODEC_KEY", "typeflux-test-subject-key-32byte")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(
        _dedent(
            """
            project: demo_project
            name: demo
            task_queue: demo-queue
            runtime:
              temporal:
                address: temporal.example:7233
                namespace: prod-ns
                payload_codec:
                  type: aes
                  current: main
                  keys:
                    - id: main
                      value_from:
                        env: TF_ERASE_CODEC_KEY
                  subject_scope: {}
              registry:
                type: inline
                prompts:
                  first: first {{value}}
              provider:
                type: fake
            activities:
              definitions:
                - name: first
                  input: schemas:InputModel
                  output: schemas:OutputModel
                  prompt: first
            workflow:
              name: DemoWorkflow
              input: schemas:InputModel
              output: schemas:OutputModel
              steps:
                - id: first
                  activity: first
            """
        ),
        encoding="utf-8",
    )
    spec = load_yaml_spec(spec_path)
    # The un-stripped spec fails closed at converter construction (no keystore).
    with pytest.raises(PayloadCodecError, match="subject_scope"):
        _codec_data_converter(pydantic_data_converter, spec.runtime.temporal.payload_codec)
    stripped = project_cli._codec_free_spec(spec)
    assert stripped.runtime.temporal.payload_codec is None
    # The connection identity is untouched.
    assert stripped.runtime.temporal.address == "temporal.example:7233"
    assert stripped.runtime.temporal.namespace == "prod-ns"
    # And a codec-free converter builds without any keystore.
    converter = _codec_data_converter(pydantic_data_converter, None)
    assert converter is pydantic_data_converter
    # A codec-less spec passes through unchanged (no needless copies).
    assert project_cli._codec_free_spec(stripped) is stripped


def test_bad_keystore_class_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path = _write_erase_fixture(tmp_path)
    # InMemoryCacheStore is NOT a SubjectKeystore — the CLI must refuse it rather
    # than let a wrong backend absorb a shred.
    exit_code = project_cli.main(
        _erase_argv(
            project_path,
            "--surface",
            "temporal",
            "--keystore-class",
            "typeflux.execution.cache:InMemoryCacheStore",
        )
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "SubjectKeystore" in captured.err
