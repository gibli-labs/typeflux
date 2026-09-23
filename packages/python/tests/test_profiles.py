from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from typeflux.metadata import ComponentProvenanceContributor, WorkflowMetadataContext
from typeflux.project import (
    ProjectProfileError,
    load_project_profile,
    load_project_spec,
    resolve_project_workflow,
    resolve_workflow_bundle,
    validate_project,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


def _write_demo_package(tmp_path: Path) -> None:
    package = tmp_path / "demo_project"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    _write(
        package / "schemas.py",
        """
        from pydantic import BaseModel


        class InputModel(BaseModel):
            value: str


        class OutputModel(BaseModel):
            value: str
        """,
    )


def _write_workflow_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path / "workflow.yaml",
        """
        project: demo_project
        name: demo
        task_queue: demo-task-queue
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
            type: none
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
        """,
    )


def _write_provider_profile(tmp_path: Path, *, model: str = "claude-sonnet-4-6") -> None:
    _write(
        tmp_path / "profiles" / "provider" / "anthropic-prod.yaml",
        f"""
        version: "1"
        name: anthropic-prod
        kind: provider
        runtime:
          provider:
            type: anthropic
            model: {model}
            api_key:
              value_from:
                env: ANTHROPIC_API_KEY
                required: false
        """,
    )


def _write_runtime_profile(tmp_path: Path) -> None:
    _write(
        tmp_path / "profiles" / "runtime" / "hardened.yaml",
        """
        version: "1"
        name: hardened
        kind: runtime
        runtime:
          provider_retry:
            max_attempts: 2
            initial_backoff_seconds: 1
            max_backoff_seconds: 4
            backoff_multiplier: 2
        """,
    )


def _write_project(
    tmp_path: Path,
    *,
    workflow_profiles: str = "",
    environment_profiles: str = "",
    environment_overrides: str = "",
) -> Path:
    _write(
        tmp_path / "environments" / "local.yaml",
        f"""
        version: "1"
        name: local
        {environment_overrides}
        workflows:
          workflow:
            {environment_profiles or "overrides: {}"}
        """,
    )
    manifest = tmp_path / "typeflux.project.yaml"
    _write(
        manifest,
        f"""
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
            {workflow_profiles}
        environments:
          local: environments/local.yaml
        profiles:
          provider:
            anthropic-prod: profiles/provider/anthropic-prod.yaml
          runtime:
            hardened: profiles/runtime/hardened.yaml
        """,
    )
    return manifest


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **project_kwargs) -> Path:
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TYPEFLUX_ENV_FILE", str(tmp_path / "missing.env"))
    _write_demo_package(tmp_path)
    _write_workflow_yaml(tmp_path)
    _write_provider_profile(tmp_path)
    _write_runtime_profile(tmp_path)
    return _write_project(tmp_path, **project_kwargs)


def test_profile_loads_with_kind_validation_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    project = load_project_spec(manifest)

    profile = load_project_profile(project, kind="provider", profile_id="anthropic-prod")

    assert profile.name == "anthropic-prod"
    assert profile.kind == "provider"
    assert len(profile.content_hash) == 64
    # Hash is content-derived: identical content yields identical hash.
    again = load_project_profile(project, kind="provider", profile_id="anthropic-prod")
    assert again.content_hash == profile.content_hash

    with pytest.raises(ProjectProfileError, match="unknown project provider profile"):
        load_project_profile(project, kind="provider", profile_id="missing")

    # A file declaring the wrong kind for its section fails loudly.
    _write(
        manifest,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
        environments:
          local: environments/local.yaml
        profiles:
          provider:
            anthropic-prod: profiles/provider/anthropic-prod.yaml
          runtime:
            hardened: profiles/runtime/hardened.yaml
            mismatched: profiles/provider/anthropic-prod.yaml
        """,
    )
    project = load_project_spec(manifest)
    with pytest.raises(ProjectProfileError, match="referenced under profiles.runtime"):
        load_project_profile(project, kind="runtime", profile_id="mismatched")


def test_profile_rejects_keys_outside_owned_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(tmp_path, monkeypatch)
    _write(
        tmp_path / "profiles" / "provider" / "anthropic-prod.yaml",
        """
        version: "1"
        name: anthropic-prod
        kind: provider
        runtime:
          provider:
            type: anthropic
          temporal:
            address: sneaky:7233
        """,
    )
    project = load_project_spec(manifest)

    with pytest.raises(ProjectProfileError, match="outside\\s+its owned subtree: temporal"):
        load_project_profile(project, kind="provider", profile_id="anthropic-prod")


def test_workflow_profile_selection_applies_over_workflow_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod
              runtime: hardened""",
    )
    project = load_project_spec(manifest)

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    assert resolved.spec.runtime.provider.type == "anthropic"
    assert resolved.spec.runtime.provider.model == "claude-sonnet-4-6"
    retry = resolved.spec.runtime.provider_retry
    assert retry is not None and retry.max_attempts == 2
    kinds = {component.kind: component for component in resolved.components}
    assert set(kinds) == {"provider", "runtime"}
    assert kinds["provider"].id == "anthropic-prod"
    assert len(kinds["provider"].content_hash) == 64
    assert "runtime.provider.type" in kinds["provider"].override_paths
    # Provenance payload is stashed on the spec for metadata contributors.
    assert resolved.spec._component_provenance[0]["kind"] in {"provider", "runtime"}


def test_environment_overrides_beat_profiles_and_env_selection_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Environment override on provider.model wins over the profile's model,
    # and the environment-level profile selection replaces workflow-level.
    _write(
        tmp_path / "profiles" / "provider" / "fake-local.yaml",
        """
        version: "1"
        name: fake-local
        kind: provider
        runtime:
          provider:
            type: fake
        """,
    )
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod""",
        environment_profiles="""profiles:
              provider: fake-local""",
    )
    # register the extra profile
    _write(
        manifest,
        """
        version: "1"
        name: demo-project
        workflows:
          - id: workflow
            path: workflow.yaml
            profiles:
              provider: anthropic-prod
        environments:
          local: environments/local.yaml
        profiles:
          provider:
            anthropic-prod: profiles/provider/anthropic-prod.yaml
            fake-local: profiles/provider/fake-local.yaml
          runtime:
            hardened: profiles/runtime/hardened.yaml
        """,
    )
    project = load_project_spec(manifest)

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    assert resolved.spec.runtime.provider.type == "fake"
    kinds = {component.kind: component.id for component in resolved.components}
    assert kinds == {"provider": "fake-local"}


def test_environment_override_wins_over_profile_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod""",
        environment_profiles="""overrides:
              runtime:
                provider:
                  model: claude-haiku-4-5""",
    )
    project = load_project_spec(manifest)

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    assert resolved.spec.runtime.provider.type == "anthropic"
    assert resolved.spec.runtime.provider.model == "claude-haiku-4-5"


def test_bundle_exposes_component_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod""",
    )
    project = load_project_spec(manifest)

    bundle = resolve_workflow_bundle(project, workflow_id="workflow", environment_id="local")

    assert len(bundle.components) == 1
    component = bundle.components[0]
    assert component["kind"] == "provider"
    assert component["id"] == "anthropic-prod"
    assert len(component["content_hash"]) == 64
    assert "runtime.provider.type" in component["override_paths"]
    payload = bundle.to_dict()
    assert "ANTHROPIC_API_KEY" in str(payload)  # secret reference name is safe
    # Provenance carries key paths and identity only, never config values.
    assert "claude-sonnet-4-6" not in str(component)
    assert "anthropic" not in str(component.get("override_paths"))


def test_validate_project_flags_unknown_and_invalid_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: nonexistent""",
    )
    _write(
        tmp_path / "profiles" / "runtime" / "hardened.yaml",
        """
        version: "1"
        name: hardened
        kind: runtime
        runtime:
          provider:
            type: fake
        """,
    )
    project = load_project_spec(manifest)

    report = validate_project(project)

    assert report.ok is False
    codes = {issue.code for issue in report.issues}
    assert "unknown_profile_reference" in codes
    assert "invalid_component_profile" in codes


def test_component_provenance_contributor_emits_safe_payload() -> None:
    contributor = ComponentProvenanceContributor(
        [
            {
                "kind": "provider",
                "id": "anthropic-prod",
                "name": "anthropic-prod",
                "content_hash": "a" * 64,
                "source_path": "/secret/host/path.yaml",
                "override_paths": ("runtime.provider.type",),
            }
        ]
    )

    contribution = contributor.workflow(
        WorkflowMetadataContext(
            workflow_name="DemoWorkflow",
            workflow_id="wf-1",
            task_queue="q",
        )
    )

    components = contribution.workflow_metadata["typeflux"]["components"]
    assert components == [
        {
            "kind": "provider",
            "id": "anthropic-prod",
            "name": "anthropic-prod",
            "content_hash": "a" * 64,
        }
    ]
    # Manifest contribution mirrors the metadata; host paths never leak.
    manifest_components = contribution.workflow_manifest["contributions"]["components"]
    assert manifest_components == components
    assert "/secret/host/path.yaml" not in str(contribution)


def test_validate_project_flags_environment_profile_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Environment per-workflow selections validate at project validate time,
    # not only at resolution.
    manifest = _setup(
        tmp_path,
        monkeypatch,
        environment_profiles="""profiles:
              provider: nonexistent""",
    )
    project = load_project_spec(manifest)

    report = validate_project(project)

    assert report.ok is False
    issues = {issue.code: issue.message for issue in report.issues}
    assert "unknown_profile_reference" in issues
    assert "environment 'local'" in issues["unknown_profile_reference"]


def test_override_provenance_excludes_profile_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Profile-applied paths live on component provenance only; yaml_overrides
    # provenance covers true environment overrides.
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod""",
        environment_profiles="""overrides:
              runtime:
                provider:
                  model: claude-haiku-4-5""",
    )
    project = load_project_spec(manifest)

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    provenance = resolved.spec._override_provenance
    assert provenance is not None
    assert provenance.override_paths == ("runtime.provider.model",)
    component = resolved.components[0]
    assert "runtime.provider.type" in component.override_paths


def test_profiles_only_resolution_has_no_override_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod""",
    )
    project = load_project_spec(manifest)

    resolved = resolve_project_workflow(project, workflow_id="workflow", environment_id="local")

    assert resolved.spec._override_provenance is None
    assert resolved.spec.runtime.provider.type == "anthropic"


def test_profile_definition_redacts_credential_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#600: a profile detail must never echo credential material.

    A strict value_from reference is provenance and renders as-is; anything
    else occupying a fixed secret slot — a literal string, a number, or a
    malformed reference smuggling a fallback beside value_from — is masked to
    "***" (the TS control plane's marker), on a COPY (the loaded spec stays
    untouched).
    """
    import json

    from typeflux.project import profile_definition

    manifest = _setup(tmp_path, monkeypatch)
    _write(
        tmp_path / "profiles" / "provider" / "anthropic-prod.yaml",
        """
        version: "1"
        name: anthropic-prod
        kind: provider
        runtime:
          provider:
            type: anthropic
            api_key: sk-live-supersecret
        """,
    )
    project = load_project_spec(manifest)
    detail = profile_definition(project, kind="provider", profile_id="anthropic-prod")
    assert detail.runtime["provider"]["api_key"] == "***"
    assert "sk-live-supersecret" not in json.dumps(detail.to_dict())
    # The loaded spec itself is untouched — redaction happens on a copy.
    reloaded = load_project_profile(project, kind="provider", profile_id="anthropic-prod")
    assert reloaded.runtime["provider"]["api_key"] == "sk-live-supersecret"

    # Numeric values and TLS literal leaves mask; a strict reference renders as-is.
    _write(
        tmp_path / "profiles" / "runtime" / "hardened.yaml",
        """
        version: "1"
        name: hardened
        kind: runtime
        runtime:
          temporal:
            api_key: 123456
            tls:
              client_private_key: PEM-PRIVATE-KEY
              server_root_ca_cert:
                value_from:
                  env: CA_CERT_PEM
        """,
    )
    hardened = profile_definition(project, kind="runtime", profile_id="hardened")
    temporal = hardened.runtime["temporal"]
    assert temporal["api_key"] == "***"
    assert temporal["tls"]["client_private_key"] == "***"
    assert temporal["tls"]["server_root_ca_cert"] == {"value_from": {"env": "CA_CERT_PEM"}}
    payload = json.dumps(hardened.to_dict())
    assert "PEM-PRIVATE-KEY" not in payload
    assert "123456" not in payload

    # A malformed reference (credential smuggled beside value_from) masks WHOLE.
    _write(
        tmp_path / "profiles" / "provider" / "anthropic-prod.yaml",
        """
        version: "1"
        name: anthropic-prod
        kind: provider
        runtime:
          provider:
            type: anthropic
            api_key:
              value_from:
                env: KEY
              fallback: sk-live-smuggled
        """,
    )
    smuggled = profile_definition(project, kind="provider", profile_id="anthropic-prod")
    assert smuggled.runtime["provider"]["api_key"] == "***"
    assert "sk-live-smuggled" not in json.dumps(smuggled.to_dict())


def test_profile_definition_redacts_custom_extension_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#792: a custom extension's ``config`` map is a secret slot per entry — literal
    values mask to ``***``; exact ``value_from`` references render as provenance."""
    import json

    from typeflux.project import profile_definition

    manifest = _setup(tmp_path, monkeypatch)
    # Reuse the declared provider profile id with custom-extension content.
    _write(
        tmp_path / "profiles" / "provider" / "anthropic-prod.yaml",
        """
        version: "1"
        name: anthropic-prod
        kind: provider
        runtime:
          provider:
            type: custom
            class: acme.providers:AcmeProvider
            config:
              endpoint: https://acme.internal
              api_key: sk-acme-supersecret
              token:
                value_from:
                  env: ACME_TOKEN
        """,
    )
    project = load_project_spec(manifest)
    detail = profile_definition(project, kind="provider", profile_id="anthropic-prod")
    config = detail.runtime["provider"]["config"]
    assert config["endpoint"] == "***"
    assert config["api_key"] == "***"
    assert config["token"] == {"value_from": {"env": "ACME_TOKEN"}}
    assert "sk-acme-supersecret" not in json.dumps(detail.to_dict())

    # A raw fragment can put a NON-MAP at the config slot (full-spec validation never runs
    # on profiles) — the whole value masks rather than leaking (codex).
    _write(
        tmp_path / "profiles" / "provider" / "anthropic-prod.yaml",
        """
        version: "1"
        name: anthropic-prod
        kind: provider
        runtime:
          provider:
            type: custom
            class: acme.providers:AcmeProvider
            config: sk-live-bare-string
        """,
    )
    bare = profile_definition(project, kind="provider", profile_id="anthropic-prod")
    assert bare.runtime["provider"]["config"] == "***"
    assert "sk-live-bare-string" not in json.dumps(bare.to_dict())


def test_profile_definition_sanitizes_registry_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typeflux.project import profile_definition

    manifest = _setup(tmp_path, monkeypatch)
    _write(
        tmp_path / "profiles" / "registry" / "langfuse.yaml",
        """
        version: "1"
        name: langfuse
        kind: registry
        runtime:
          registry:
            type: langfuse
            host: https://user:sk-token@lf.example/api?token=abc
        """,
    )
    # Declare the registry profile alongside the fixture's provider/runtime ones.
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace(
            "profiles:\n",
            "profiles:\n  registry:\n    langfuse: profiles/registry/langfuse.yaml\n",
            1,
        ),
        encoding="utf-8",
    )
    project = load_project_spec(manifest)

    def sanitized_host() -> str:
        detail = profile_definition(project, kind="registry", profile_id="langfuse")
        return detail.runtime["registry"]["host"]

    host = sanitized_host()
    assert "sk-token" not in host
    assert "token=abc" not in host
    assert "lf.example" in host

    # Malformed authority forms (extra/missing slashes) must strip too — the TS side gets
    # this from the WHATWG parser's normalization; Python normalizes explicitly. An
    # authority urlsplit REJECTS falls all the way through and masks whole ("***") —
    # unparseable + credential-suspect is fail-closed (codex).
    for malformed, keeps_host in (
        ("https:////user:sk-token@lf.example/api", True),
        ("http:/user:sk-token@lf.example/api", True),
        ("https://[bad-authority-user:sk-token@lf.example", False),
    ):
        _write(
            tmp_path / "profiles" / "registry" / "langfuse.yaml",
            f"""
            version: "1"
            name: langfuse
            kind: registry
            runtime:
              registry:
                type: langfuse
                host: {malformed}
            """,
        )
        host = sanitized_host()
        assert "sk-token" not in host
        assert ("lf.example" in host) is keeps_host
        if not keeps_host:
            assert host == "***"


def test_definition_projections_and_used_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from typeflux.project import (
        environment_definition,
        profile_definition,
        profile_definitions,
    )

    manifest = _setup(
        tmp_path,
        monkeypatch,
        workflow_profiles="""profiles:
              provider: anthropic-prod""",
    )
    project = load_project_spec(manifest)

    environment = environment_definition(project, "local")
    assert environment.name == "local"
    assert "workflow" in environment.used_by
    # Variable values never serialize — names only.
    assert "variable_names" in environment.to_dict()
    assert "variables" not in environment.to_dict()

    summaries = profile_definitions(project)
    assert {(entry.kind, entry.id) for entry in summaries} == {
        ("provider", "anthropic-prod"),
        ("runtime", "hardened"),
    }

    profile = profile_definition(project, kind="provider", profile_id="anthropic-prod")
    assert profile.name == "anthropic-prod"
    assert len(profile.content_hash) == 64
    assert "workflow" in profile.used_by
    payload = json.dumps(profile.to_dict())
    # Secret-bearing fields stay value_from references.
    assert "ANTHROPIC_API_KEY" in payload
    assert "sk-" not in payload
