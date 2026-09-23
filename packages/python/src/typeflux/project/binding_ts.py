"""The ts-plan-argument binding driver (#618 slice 4): plan-less operations.

Operates TS-edition executions from the Python control plane using **only
language-neutral inputs** — raw YAML reads for identity/connection config and
the binding contract's shared wire surface (the ``typeflux_lifecycle_status``
query and the two lifecycle signals). Never imports project modules: that is
what makes this a binding driver, not a resolver.

Binding verification per the ts profile (the operator obligation the
temporal-binding contract assigns to drivers): ``describe()`` must show the
constant ``typefluxYamlWorkflow`` type and the memo must carry the routed
``typeflux_project`` and ``typeflux_workflow`` — a mismatch is a 409
``LifecycleBindingError``, fail closed, no memo-less fallback.

``start`` dispatches the plan PINNED at driver construction (resolved via the
runtime's contract resolver, #642) with the identity memo; without a resolver
it keeps failing closed. The selected runtime-kind profile's ``runtime.temporal``
composes into the connection at the canonical precedence (#672).

**Recorded design decision (#812): wire-primitives-only is deliberate.** This driver
never grows bundle/catalog/validate/resolution for TS projects: those surfaces belong
to the TS control plane, which owns the TS resolver — duplicating them here would fork
resolution semantics across editions (two implementations of one project's truth). The
asymmetry with the TS CP mirroring some Python project data is the same principle from
the other side: each CP resolves ITS edition natively and operates the other edition
through the language-neutral wire surface only. Re-open triggers: a mixed-edition
project that needs ONE control plane serving both editions' bundles, or the #573
transport seam landing a shared cross-edition resolver protocol.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typeflux.core.contracts import ReviewCommand, WorkflowLifecycleStatus
from typeflux.core.errors import LifecycleBindingError, TypefluxError
from typeflux.yaml.loader import strict_safe_load

if TYPE_CHECKING:
    from typeflux.project.drain import WorkflowDrainStatus
    from typeflux.project.environment import ProjectResolvedWorkflow
    from typeflux.project.operations import (
        WorkflowOperationStatus,
        WorkflowStartReceipt,
    )
    from typeflux.project.runs import WorkflowExecutionList
    from typeflux.project.workers import WorkflowTaskQueueWorkers

#: The ts-plan-argument profile's constant registered workflow type
#: (contracts/temporal-binding/binding.v1.json).
TS_GENERIC_WORKFLOW_TYPE = "typefluxYamlWorkflow"

#: The TS edition's ``EXECUTIONS_SCAN_LIMIT``: how many visibility rows the
#: memo-filtered listing reads before giving up (bounded best-effort, #671).
EXECUTIONS_SCAN_LIMIT = 1000

#: ``yaml/identity._TYPE_DIGEST_LENGTH`` — Python's registered-type suffix
#: truncation, mirrored so this profile's drain keys render identically to the
#: python binding's versioned type names (and to the TS edition's ``drain.ts``).
_VERSION_DIGEST_LENGTH = 12


#: The fail-safe suffix for a memo with neither label nor digest — see
#: ``_version_identity_key``.
UNIDENTIFIED_VERSION_SUFFIX = "(unidentified memo)"


def _version_identity_key(logical: str, label: str | None, digest: str | None) -> str:
    """One drain-view version identity key (#686; TS ``versionIdentityKey``).

    Python's ``registered_workflow_type`` rendering — ``{logical}.{label}``
    when a version label exists, else ``{logical}.{digest[:12]}`` — derived
    from the MEMO instead of the type name (this profile registers one generic
    type). A memo missing both reads the collision-proof unidentified suffix
    (it contains a space, which no label or hex digest carries — a project
    literally versioned "unknown" cannot collide; finder): fail-safe — it can
    never equal a current key, so it reports draining, never drained. Truthy
    checks throughout so an empty-string memo value never renders an empty
    suffix (the TS mapping matches).
    """
    if label:
        suffix = label
    elif digest:
        suffix = digest[:_VERSION_DIGEST_LENGTH]
    else:
        suffix = UNIDENTIFIED_VERSION_SUFFIX
    return f"{logical}.{suffix}"


_ENV_PATTERN = re.compile(
    r"\$(?P<escaped>\$)?\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


class TsBindingConfigError(TypefluxError, ValueError):
    """The ts project's YAML lacks what the binding driver needs (fail closed).

    A ValueError on purpose: configuration errors surface as 422 with the
    class-name discriminant, like every other config-error surface."""


def _substitute_env(value: str, *, variables: dict[str, str] | None = None) -> str:
    """``${VAR}`` / ``${VAR:-default}`` substitution, fail closed on missing.

    Deliberately parallel to ``yaml/loader.py``'s ``_replace_env`` (which has
    no environment-variables layer); syntax changes must land in both.

    ``variables`` is the selected environment's ``variables:`` map — consulted
    when the process env lacks the name (process env wins, matching the
    canonical non-override application semantics)."""

    def replace(match: re.Match[str]) -> str:
        if match.group("escaped"):
            # `$${...}` is the canonical loader's literal escape.
            return match.group(0)[1:]
        name = match.group("name")
        default = match.group("default")
        resolved = os.environ.get(name)
        if resolved is None and variables is not None:
            candidate = variables.get(name)
            if isinstance(candidate, str):
                resolved = candidate
        if resolved is not None:
            return resolved
        if default is not None:
            return default
        raise TsBindingConfigError(
            f"environment variable {name!r} is required by the ts project's "
            "runtime config and is not set"
        )

    return _ENV_PATTERN.sub(replace, value)


def _read_yaml(path: Path, *, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise TsBindingConfigError(f"{what} not found at {path}")
    loaded = strict_safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TsBindingConfigError(f"{what} at {path} is not a mapping")
    return loaded


def _deep_merge_dicts(base_value: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """The canonical ``_deep_merge`` semantics on plain dicts: nested mappings
    merge key-by-key; anything else replaces. Returns a new dict (no mutation)."""
    merged = dict(base_value)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _deep_merge_dicts(current, value)
        else:
            merged[key] = value
    return merged


def _coerce_tls(value: Any, *, variables: dict[str, str] | None = None) -> Any:
    """A boolean passes through; a mapping (custom CA / mTLS certs, #685) builds
    a real ``temporalio.client.TLSConfig`` through the CANONICAL mapping
    (``yaml/tls.py`` — the spec model and the builder are library code, not
    project modules, so the driver stays raw-YAML). String leaves get the
    driver's per-field ``${VAR}`` substitution first, mirroring the canonical
    loader's whole-document interpolation. Anything else fails closed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        from pydantic import ValidationError as _PydanticValidationError

        from typeflux.yaml.spec import TemporalTLSConfigSpec
        from typeflux.yaml.tls import build_temporal_tls_config

        def substitute(node: Any) -> Any:
            if isinstance(node, str):
                return _substitute_env(node, variables=variables) if "${" in node else node
            if isinstance(node, dict):
                return {key: substitute(item) for key, item in node.items()}
            return node

        try:
            tls_spec = TemporalTLSConfigSpec.model_validate(substitute(value))
        except _PydanticValidationError as exc:
            raise TsBindingConfigError(
                f"the ts project's runtime.temporal.tls block is invalid: {exc}"
            ) from exc
        try:
            return build_temporal_tls_config(tls_spec)
        except ValueError as exc:
            raise TsBindingConfigError(str(exc)) from exc
    raise TsBindingConfigError(
        "runtime.temporal.tls must be a boolean or a structured TLS mapping "
        "(custom CA / client certs) in the ts project's YAML"
    )


def _resolve_payload_codec(
    value: Any,
    *,
    variables: dict[str, str] | None = None,
    subject_keystore: Any | None = None,
) -> Any:
    """Build the AES-256-GCM codec from the ts project's ``runtime.temporal.payload_codec``
    block, or ``None`` when absent. Fail-closed: an invalid block or a missing/wrong-length
    key raises here (startup), so the CP's codec-aware client (inspect/review/migrate of a
    TS-worker run) decrypts history under the SAME wire format the worker encrypts with.

    ``subject_scope`` (#715 slice 4) additionally REQUIRES ``subject_keystore`` — a SHARED
    :class:`~typeflux.yaml.subject_keystore.SubjectKeystore` backend holding the
    same records the workers mint under. The CP is never the sole owner of subject key
    records, so there is NO in-memory default here (fail-closed, #715 Bugbot): a
    silently-minted process-local keystore would seal lifecycle-signal payloads under
    keys the worker does not hold (undeliverable signals) and mint fresh keys for
    subjects whose real records live elsewhere.
    """
    if value is None:
        return None
    from pydantic import ValidationError as _PydanticValidationError

    from typeflux.yaml.payload_codec import (
        PayloadCodecError,
        PayloadCodecSpec,
        build_payload_codec,
    )

    def substitute(node: Any) -> Any:
        if isinstance(node, str):
            return _substitute_env(node, variables=variables) if "${" in node else node
        if isinstance(node, dict):
            return {key: substitute(item) for key, item in node.items()}
        if isinstance(node, list):
            return [substitute(item) for item in node]
        return node

    if not isinstance(value, dict):
        raise TsBindingConfigError(
            "runtime.temporal.payload_codec must be a mapping in the ts project's YAML"
        )
    try:
        codec_spec = PayloadCodecSpec.model_validate(substitute(value))
    except _PydanticValidationError as exc:
        raise TsBindingConfigError(
            f"the ts project's runtime.temporal.payload_codec block is invalid: {exc}"
        ) from exc
    try:
        # A codec key's ``value_from.env`` may be supplied by the selected environment's
        # ``variables:`` map, not only the process env (parity with _resolve_secret /
        # _substitute_env). Thread that layer in as an env override (variables win) rather
        # than mutating os.environ, so a TS project whose codec key comes from the selected
        # environment resolves through every Python control-plane op.
        codec_env = {**os.environ, **(variables or {})}
        codec: Any = build_payload_codec(codec_spec, env=codec_env)
    except (PayloadCodecError, ValueError) as exc:
        raise TsBindingConfigError(str(exc)) from exc
    if codec is not None and codec_spec.subject_scope is not None:
        # #715 slice 4: the CP-side codec mirrors the worker's subject scoping so
        # lifecycle SIGNAL payloads to a subject-scoped execution seal under the same
        # per-subject scheme (bindings resolve via the visibility fallback bound at
        # _connect). FAIL-CLOSED without an injected SHARED keystore (#715 Bugbot):
        # the CP is never the sole owner of subject key records, and a silently-minted
        # process-local keystore would seal signals under keys the worker cannot
        # decode while minting fresh keys for subjects whose real records live in the
        # workers' shared backend.
        if subject_keystore is None:
            raise TsBindingConfigError(
                "the ts project's runtime.temporal.payload_codec.subject_scope requires a "
                "SHARED SubjectKeystore backend injected into the control-plane binding "
                "(#715 slice 4): without the workers' key records this process would seal "
                "lifecycle signals under keys no worker holds. Provide the deployment's "
                "shared keystore (see docs/privacy.md 'Keystore backends')."
            )
        from typeflux.yaml.subject_keystore import SubjectScopedPayloadCodec

        codec = SubjectScopedPayloadCodec(codec, subject_keystore)
    return codec


def _resolve_secret(value: Any, *, variables: dict[str, str] | None = None) -> str | None:
    """A literal string or ``{value_from: {env, required?}}`` — never logged."""
    if value is None:
        return None
    if isinstance(value, str):
        return _substitute_env(value, variables=variables) if "${" in value else value
    if isinstance(value, dict):
        value_from = value.get("value_from")
        if isinstance(value_from, dict) and isinstance(value_from.get("env"), str):
            # ${VAR} substitution INSIDE value_from strings, matching the tls
            # block's behavior in the same driver — one document, one
            # interpolation rule (finder).
            env_name = _substitute_env(value_from["env"], variables=variables)
            resolved = os.environ.get(env_name)
            if resolved is None and value_from.get("required", True):
                raise TsBindingConfigError(
                    f"environment variable {env_name!r} is required by the "
                    "ts project's temporal config and is not set"
                )
            return resolved
        if isinstance(value_from, dict) and isinstance(value_from.get("file"), str):
            secret_file = Path(
                _substitute_env(value_from["file"], variables=variables)
            ).expanduser()
            if not secret_file.is_file():
                if value_from.get("required", True):
                    raise TsBindingConfigError(
                        f"secret file {str(secret_file)!r} required by the ts "
                        "project's temporal config does not exist"
                    )
                return None
            return secret_file.read_text(encoding="utf-8").strip()
    raise TsBindingConfigError("unsupported temporal api_key shape in the ts project's YAML")


class TsBindingTarget:
    """Everything the plan-less operations need, from pure YAML."""

    def __init__(
        self,
        *,
        address: str,
        namespace: str,
        tls: Any,
        api_key: str | None,
        payload_codec: Any = None,
        project_name: str,
        workflow_name: str,
        version_label: str | None = None,
        valid_user_decisions: dict[str, str],
    ) -> None:
        self.address = address
        self.namespace = namespace
        #: ``False`` / ``True`` or a built ``temporalio.client.TLSConfig`` (#685).
        self.tls = tls
        self.api_key = api_key
        #: A built ``TypefluxAesGcmPayloadCodec`` or ``None`` (#188). Codec-aware so the
        #: CP decrypts TS-worker history for inspect/review/migrate under the pinned format.
        self.payload_codec = payload_codec
        self.project_name = project_name
        self.workflow_name = workflow_name
        self.version_label = version_label
        self.valid_user_decisions = valid_user_decisions

    @classmethod
    def from_project_yaml(
        cls,
        manifest_path: str | Path,
        *,
        workflow_id: str,
        environment_id: str,
    ) -> TsBindingTarget:
        from typeflux.env import load_env

        # The canonical loaders load the project .env before interpolating;
        # the driver mirrors that so ${VAR} references satisfied by a .env
        # file resolve identically.
        load_env()
        manifest_file = Path(manifest_path).expanduser().resolve()
        manifest = _read_yaml(manifest_file, what="ts project manifest")
        base = manifest_file.parent

        workflows = manifest.get("workflows")
        if not isinstance(workflows, list):
            raise TsBindingConfigError("ts project manifest lists no workflows")
        entry = next(
            (
                item
                for item in workflows
                if isinstance(item, dict) and item.get("id") == workflow_id
            ),
            None,
        )
        if entry is None:
            raise TsBindingConfigError(f"unknown project workflow: {workflow_id}")
        workflow_rel = entry.get("path")
        if not isinstance(workflow_rel, str) and isinstance(entry.get("directory"), str):
            defaults = manifest.get("defaults")
            filename = "typeflux.yaml"
            if isinstance(defaults, dict) and isinstance(defaults.get("workflow_filename"), str):
                filename = defaults["workflow_filename"]
            workflow_rel = str(Path(entry["directory"]) / filename)
        if not isinstance(workflow_rel, str):
            raise TsBindingConfigError(f"workflow {workflow_id!r} declares no path")
        workflow = _read_yaml(base / workflow_rel, what=f"workflow YAML for {workflow_id!r}")

        environments = manifest.get("environments")
        if not isinstance(environments, dict) or environment_id not in environments:
            raise TsBindingConfigError(f"unknown project environment: {environment_id}")
        environment = _read_yaml(
            base / str(environments[environment_id]), what=f"environment {environment_id!r}"
        )

        # The selected RUNTIME-kind profile's runtime.temporal composes into
        # the connection (#672; the canonical rules of `profiles.py`
        # `resolve_selected_profiles`): workflow-level selection from the
        # manifest workflow entry, environment-level from the environment's
        # PER-WORKFLOW block — environment REPLACES workflow per kind. Only
        # the runtime kind matters here (it owns `temporal`); provider/
        # registry profiles shape worker-side resolution and are composed by
        # the resolver, not this connection.
        # PROFILE_KINDS is a spec CONSTANT (never user project code), so
        # importing it keeps the "no project-module imports" rule intact.
        from typeflux.project.profiles import PROFILE_KINDS

        def _profile_selection(mapping: Any, *, context: str) -> dict[str, str]:
            profiles = mapping.get("profiles") if isinstance(mapping, dict) else None
            if profiles is None:
                return {}
            if not isinstance(profiles, dict):
                raise TsBindingConfigError(f"{context} profile selection is not a mapping")
            selection: dict[str, str] = {}
            for kind, profile_id in profiles.items():
                # Canonical `validate_profile_selection` parity: an unknown
                # kind (a typo like `runtme`) must fail loudly here exactly as
                # it does in the resolver — never silently drop a selection.
                if kind not in PROFILE_KINDS:
                    raise TsBindingConfigError(
                        f"{context} profile selection selects unknown profile kind "
                        f"{kind!r}; valid kinds: {', '.join(PROFILE_KINDS)}"
                    )
                if not isinstance(profile_id, str) or not profile_id:
                    raise TsBindingConfigError(
                        f"{context} profile selection for kind {kind!r} must be a profile id string"
                    )
                selection[kind] = profile_id
            return selection

        environment_workflow = (
            environment.get("workflows", {}).get(workflow_id)
            if isinstance(environment.get("workflows"), dict)
            else None
        )
        effective_selection = {
            **_profile_selection(entry, context=f"workflow {workflow_id!r}"),
            **_profile_selection(
                environment_workflow,
                context=f"environment {environment_id!r} workflow {workflow_id!r}",
            ),
        }
        runtime_profile_id = effective_selection.get("runtime")
        profile_temporal: dict[str, Any] | None = None
        if runtime_profile_id is not None:
            # Manifest-declaration-first (the #568 rule): the selected id must
            # be DECLARED under the manifest's profiles.runtime map; the file
            # must exist; a declared kind mismatch fails closed.
            declared = manifest.get("profiles")
            declared_runtime = declared.get("runtime") if isinstance(declared, dict) else None
            profile_rel = (
                declared_runtime.get(runtime_profile_id)
                if isinstance(declared_runtime, dict)
                else None
            )
            if not isinstance(profile_rel, str):
                raise TsBindingConfigError(f"unknown project runtime profile: {runtime_profile_id}")
            profile_doc = _read_yaml(
                base / profile_rel, what=f"runtime profile {runtime_profile_id!r}"
            )
            declared_kind = profile_doc.get("kind")
            # `kind` is REQUIRED (the canonical ProjectProfileSpec's mandatory
            # Literal) — an unclassified profile must not silently take effect.
            if declared_kind != "runtime":
                raise TsBindingConfigError(
                    f"profile {runtime_profile_id!r} declares kind "
                    f"{declared_kind!r}, not the selected kind 'runtime'"
                )
            # Validate through the CANONICAL profile schema + owned-subtree
            # rule (codex): a profile the resolver/validate paths reject must
            # not take effect via this driver either. These are spec models
            # and constants, never user project code.
            from pydantic import ValidationError as _PydanticValidationError

            from typeflux.project.profiles import (
                ProjectProfileError,
                ProjectProfileSpec,
                _validate_profile_subtree,
            )

            try:
                profile_spec = ProjectProfileSpec.model_validate(
                    {**profile_doc, "profile_path": str(base / profile_rel)}
                )
                _validate_profile_subtree(profile_spec)
            except (_PydanticValidationError, ProjectProfileError) as exc:
                raise TsBindingConfigError(
                    f"runtime profile {runtime_profile_id!r} is invalid: {exc}"
                ) from exc
            if "temporal" in profile_spec.runtime:
                profile_section = profile_spec.runtime["temporal"]
                # A PRESENT-but-malformed temporal subtree must fail closed —
                # silently dropping it would reconnect to the unprofiled
                # cluster, the exact misroute the pre-#672 refusal prevented.
                # (An ABSENT temporal is legal: a runtime profile may override
                # only observability.)
                if not isinstance(profile_section, dict):
                    raise TsBindingConfigError(
                        f"runtime profile {runtime_profile_id!r} has a malformed "
                        "`runtime.temporal` section (not a mapping)"
                    )
                profile_temporal = profile_section

        # Temporal connection, layered at the canonical precedence (#568):
        # manifest defaults < workflow YAML < selected runtime PROFILE <
        # environment overrides < per-workflow environment overrides — the
        # same order the editions' resolvers apply, on the neutral subset the
        # driver needs.
        # DEEP merge per layer (the canonical `_deep_merge` semantics, codex):
        # a layer overriding one nested field (e.g. api_key.value_from.required)
        # must not replace the whole nested mapping a lower layer supplied.
        temporal: dict[str, Any] = {}
        manifest_defaults = manifest.get("defaults")
        if isinstance(manifest_defaults, dict):
            defaults_runtime = manifest_defaults.get("runtime")
            if isinstance(defaults_runtime, dict) and isinstance(
                defaults_runtime.get("temporal"), dict
            ):
                temporal = _deep_merge_dicts(temporal, defaults_runtime["temporal"])
        workflow_runtime = workflow.get("runtime")
        if isinstance(workflow_runtime, dict) and isinstance(
            workflow_runtime.get("temporal"), dict
        ):
            temporal = _deep_merge_dicts(temporal, workflow_runtime["temporal"])
        if profile_temporal is not None:
            temporal = _deep_merge_dicts(temporal, profile_temporal)
        overrides = environment.get("overrides")
        if isinstance(overrides, dict):
            runtime_overrides = overrides.get("runtime")
            if isinstance(runtime_overrides, dict) and isinstance(
                runtime_overrides.get("temporal"), dict
            ):
                temporal = _deep_merge_dicts(temporal, runtime_overrides["temporal"])
        # The per-workflow override layer sits above the environment-wide one,
        # matching the resolver's precedence.
        env_workflows = environment.get("workflows")
        if isinstance(env_workflows, dict) and isinstance(env_workflows.get(workflow_id), dict):
            per_workflow = env_workflows[workflow_id].get("overrides")
            if isinstance(per_workflow, dict):
                per_workflow_runtime = per_workflow.get("runtime")
                if isinstance(per_workflow_runtime, dict) and isinstance(
                    per_workflow_runtime.get("temporal"), dict
                ):
                    temporal = _deep_merge_dicts(temporal, per_workflow_runtime["temporal"])
        env_variables: dict[str, str] = {}
        raw_variables = environment.get("variables")
        if isinstance(raw_variables, dict):
            env_variables = {
                str(k): str(v) for k, v in raw_variables.items() if isinstance(v, (str, int, float))
            }

        address = temporal.get("address")
        if not isinstance(address, str) or not address:
            raise TsBindingConfigError(
                f"the ts project's runtime config resolves no temporal address for "
                f"workflow {workflow_id!r} in environment {environment_id!r}"
            )
        address = _substitute_env(address, variables=env_variables)
        namespace = temporal.get("namespace", "default")
        if not isinstance(namespace, str) or not namespace:
            namespace = "default"
        else:
            namespace = _substitute_env(namespace, variables=env_variables)

        workflow_section_raw = workflow.get("workflow")
        version_label = (
            workflow_section_raw.get("version")
            if isinstance(workflow_section_raw, dict)
            and isinstance(workflow_section_raw.get("version"), str)
            else None
        )
        workflow_name = (
            workflow.get("workflow", {}).get("name")
            if isinstance(workflow.get("workflow"), dict)
            else None
        )
        if not isinstance(workflow_name, str) or not workflow_name:
            raise TsBindingConfigError(f"workflow YAML for {workflow_id!r} declares no name")

        project_name = workflow.get("project")
        if not isinstance(project_name, str) or not project_name:
            raise TsBindingConfigError(f"workflow YAML for {workflow_id!r} declares no project")

        decisions: dict[str, str] = {}
        workflow_section = workflow.get("workflow")
        if isinstance(workflow_section, dict):
            lifecycle = workflow_section.get("lifecycle")
            if isinstance(lifecycle, dict):
                # The resolved-spec fallback: a single `review`, or the union over all
                # `gates` (#55 slice 4). This is only the fallback for a not-waiting
                # execution; the running workflow's own waiting_gates are preferred (§6).
                gate_sources: list[dict[str, Any]] = []
                review = lifecycle.get("review")
                if isinstance(review, dict):
                    gate_sources.append(review)
                gates = lifecycle.get("gates")
                if isinstance(gates, list):
                    gate_sources.extend(gate for gate in gates if isinstance(gate, dict))
                for source in gate_sources:
                    user_decisions = source.get("user_decisions")
                    if isinstance(user_decisions, dict):
                        for decision, route in user_decisions.items():
                            if isinstance(route, dict) and isinstance(route.get("route"), str):
                                decisions[str(decision)] = route["route"]
                decisions = {decision: decisions[decision] for decision in sorted(decisions)}

        return cls(
            address=address,
            namespace=namespace,
            tls=_coerce_tls(temporal.get("tls", False), variables=env_variables),
            api_key=_resolve_secret(temporal.get("api_key"), variables=env_variables),
            payload_codec=_resolve_payload_codec(
                temporal.get("payload_codec"), variables=env_variables
            ),
            project_name=project_name,
            workflow_name=workflow_name,
            version_label=version_label,
            valid_user_decisions=decisions,
        )


class TsPlanArgumentDriver:
    """Operate-tier driver for the ts-plan-argument profile.

    Plan-less for the lifecycle operations (status/review/cancel work from
    pure YAML + the execution memo). ``start`` needs the resolved plan as the
    first workflow argument (binding contract ``start_args: [plan, input]``),
    which arrives through the runtime's contract ``resolver`` when the control
    plane holds one (#642); without a resolver, ``start`` keeps failing closed.
    """

    profile = "ts-plan-argument"
    #: No in-process runtime: the facade's ``runtime`` property fails closed.
    runtime: None = None
    resolved: ProjectResolvedWorkflow | None = None

    def __init__(
        self,
        target: TsBindingTarget,
        *,
        resolver: Any = None,
        manifest_path: str | None = None,
        workflow_id: str | None = None,
        environment_id: str | None = None,
        pinned_plan: Any = None,
    ) -> None:
        self.target = target
        self._client: Any | None = None
        self._resolver = resolver
        self._manifest_path = manifest_path
        self._project_workflow_id = workflow_id
        self._environment_id = environment_id
        self._pinned_plan = pinned_plan
        #: The workflow's input JSON Schema, pinned with the plan (#673):
        #: `_coerce_input` validates foreign-edition starts against it. None +
        #: a stored error reason ⇒ start fails closed (the TS edition's own
        #: "starting requires an injected schema" posture).
        self._input_json_schema: dict[str, Any] | None = None
        self._input_schema_name: str | None = None
        self._input_schema_error: str | None = None

    @classmethod
    async def for_project_workflow(
        cls,
        manifest_path: str | Path,
        *,
        workflow_id: str,
        environment_id: str,
        resolver: Any = None,
        policy_ids: tuple[str, ...] = (),
        prefetched_bundle: Any = None,
    ) -> TsPlanArgumentDriver:
        target = TsBindingTarget.from_project_yaml(
            manifest_path, workflow_id=workflow_id, environment_id=environment_id
        )
        pinned_plan: Any = None
        if resolver is not None:
            # PIN the plan at construction (#642, mirroring the python driver's
            # pinned runtime): policy admission ran against THIS resolution in
            # driver_for_profile, so every start dispatches exactly the admitted
            # plan — never a fresher YAML edit under a stale admission. Restart
            # serve (or POST .../repin) to re-pin after edits, like every pin.
            import asyncio

            resolved_manifest = str(Path(manifest_path).expanduser().resolve())
            pinned_plan = await asyncio.to_thread(
                lambda: resolver.resolve_plan(
                    resolved_manifest,
                    workflow_id=workflow_id,
                    environment_id=environment_id,
                )
            )
            # The pure-YAML target and the resolver read the SAME project tree —
            # identity drift between them means the tree changed mid-request or
            # the resolver resolved something else. Fail loud at pin time; the
            # memo is the binding contract's verification surface.
            if pinned_plan.workflow_name != target.workflow_name:
                raise TsBindingConfigError(
                    f"resolver identity drift: resolve_plan answered workflow "
                    f"{pinned_plan.workflow_name!r}, but the project YAML binds "
                    f"{target.workflow_name!r}"
                )
            if pinned_plan.version_label != target.version_label:
                raise TsBindingConfigError(
                    f"resolver identity drift: resolve_plan answered version "
                    f"{pinned_plan.version_label!r}, but the project YAML declares "
                    f"{target.version_label!r}"
                )
        driver = cls(
            target,
            resolver=resolver,
            manifest_path=str(Path(manifest_path).expanduser().resolve()),
            workflow_id=workflow_id,
            environment_id=environment_id,
            pinned_plan=pinned_plan,
        )
        if resolver is not None:
            # PIN the input schema alongside the plan (#673): `resolve_bundle`
            # carries the workflow's input JSON Schema when the resolver was
            # given the project's schemas. The policy path's already-verified
            # bundle is REUSED when present (one subprocess round-trip, and the
            # schema is the one resolved UNDER the admitted selection); else
            # fetch with the same policy_ids. TOLERANT — a fetch failure (e.g.
            # a schema-less resolver 422s bundle) must not break the plan-less
            # lifecycle ops; `start` then fails closed with the reason.
            import asyncio

            bundle_manifest = driver._manifest_path
            assert bundle_manifest is not None
            try:
                bundle = (
                    prefetched_bundle
                    if prefetched_bundle is not None
                    else await asyncio.to_thread(
                        lambda: resolver.resolve_bundle(
                            bundle_manifest,
                            workflow_id=workflow_id,
                            environment_id=environment_id,
                            policy_ids=policy_ids,
                        )
                    )
                )
            except Exception as exc:  # noqa: BLE001 - stored, surfaced at start
                driver._input_schema_error = str(exc)
            else:
                # Defensive extraction: an unexpected bundle shape stores a
                # reason (start fails closed with it) — never crashes the pin.
                schema_slot = getattr(getattr(bundle, "workflow", None), "input_schema", None)
                name = schema_slot.get("name") if isinstance(schema_slot, dict) else None
                driver._input_schema_name = name if isinstance(name, str) else None
                json_schema = (
                    schema_slot.get("json_schema") if isinstance(schema_slot, dict) else None
                )
                if isinstance(json_schema, dict):
                    driver._input_json_schema = json_schema
                else:
                    driver._input_schema_error = (
                        "the resolver's bundle carries no input JSON Schema for the "
                        "workflow input ref"
                    )
        return driver

    async def _connect(self) -> Any:
        if self._client is None:
            from temporalio.client import Client
            from temporalio.contrib.pydantic import pydantic_data_converter

            data_converter = pydantic_data_converter
            if self.target.payload_codec is not None:
                import dataclasses

                data_converter = dataclasses.replace(
                    pydantic_data_converter, payload_codec=self.target.payload_codec
                )
            kwargs: dict[str, Any] = {
                "namespace": self.target.namespace,
                "data_converter": data_converter,
            }
            if self.target.tls is not False:
                # True, or the TLSConfig the target built from the structured
                # block (#685) — pass through, never weaken to a bare True.
                kwargs["tls"] = self.target.tls
            if self.target.api_key is not None:
                kwargs["api_key"] = self.target.api_key
            self._client = await Client.connect(self.target.address, **kwargs)
            # #715 slice 4: bind the visibility fallback for subject-scope bindings
            # (no-op for a plain codec) so signal encodes to subject-scoped executions
            # resolve their TypefluxSubjectIds via describe.
            from typeflux.yaml.runtime import _bind_subject_scope_client

            _bind_subject_scope_client(data_converter, self._client)
        return self._client

    async def _bound_handle(self, workflow_id: str, run_id: str | None) -> Any:
        client = await self._connect()
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        description = await handle.describe()
        actual_type = getattr(description, "workflow_type", None)
        if actual_type != TS_GENERIC_WORKFLOW_TYPE:
            raise LifecycleBindingError(
                f"lifecycle operation refused: execution {workflow_id!r} has workflow "
                f"type {actual_type!r}, not the ts-plan-argument generic type "
                f"{TS_GENERIC_WORKFLOW_TYPE!r}"
            )
        memo = await self._describe_memo(description)
        actual_project = memo.get("typeflux_project")
        if actual_project != self.target.project_name:
            raise LifecycleBindingError(
                f"lifecycle operation refused: execution {workflow_id!r} belongs to "
                f"project {actual_project!r}, not the bound project "
                f"{self.target.project_name!r}"
            )
        actual_workflow = memo.get("typeflux_workflow")
        if actual_workflow != self.target.workflow_name:
            raise LifecycleBindingError(
                f"lifecycle operation refused: execution {workflow_id!r} is workflow "
                f"{actual_workflow!r}, not the bound workflow "
                f"{self.target.workflow_name!r}"
            )
        if self.target.version_label is not None:
            # The ts profile freezes the version in the memo — routing to a
            # versioned workflow must verify it (binding contract).
            actual_version = memo.get("typeflux_workflow_version")
            if actual_version != self.target.version_label:
                raise LifecycleBindingError(
                    f"lifecycle operation refused: execution {workflow_id!r} carries "
                    f"version {actual_version!r}, not the routed version "
                    f"{self.target.version_label!r}"
                )
        if run_id is None:
            # Pin to the run describe() verified (canonical _bound_handle
            # pattern): an id-only handle targets the LATEST run at dispatch,
            # so workflow-id reuse could swap the target between verification
            # and the query/signal.
            bound_run_id = getattr(description, "run_id", None)
            if isinstance(bound_run_id, str) and bound_run_id:
                handle = client.get_workflow_handle(workflow_id, run_id=bound_run_id)
        return handle

    @staticmethod
    async def _describe_memo(description: Any) -> dict[str, Any]:
        memo: Any = getattr(description, "memo", None)
        if callable(memo):
            memo = memo()
        if hasattr(memo, "__await__"):
            memo = await memo
        from collections.abc import Mapping

        return dict(memo) if isinstance(memo, Mapping) else {}

    async def start(
        self,
        input_value: Any,
        *,
        workflow_id: str,
        task_queue: str | None = None,
        **start_kwargs: Any,
    ) -> WorkflowStartReceipt:
        from typeflux.project.operations import WorkflowStartReceipt

        # Additive provenance memo (#204 migrate): merged AFTER the identity keys
        # so it can never overwrite them. Popped so it never reaches Temporal's
        # start options. The identity keys stay authoritative (drift guard).
        extra_memo = start_kwargs.pop("extra_memo", None)

        if self._pinned_plan is None:
            raise TsBindingConfigError(
                "starting a ts-plan-argument execution requires the resolved plan "
                "as the first workflow argument — configure the typescript "
                "subprocess resolver (#642); use the TS edition to start "
                "executions until then"
            )
        # The PINNED plan (resolved + admitted at driver construction, #642):
        # every start dispatches exactly what admission covered. A YAML edit
        # needs a re-pin (restart serve / POST .../repin), like every pin.
        resolved = self._pinned_plan

        client = await self._connect()
        if resolved.version_label is not None:
            await self._enforce_frozen_version(client, resolved)

        # The binding contract's identity memo (temporal-binding ts-plan-argument):
        # verification reads these on every lifecycle operation.
        memo: dict[str, str] = {
            "typeflux_project": self.target.project_name,
            "typeflux_workflow": resolved.workflow_name,
            "typeflux_spec_digest": resolved.spec_digest,
        }
        if resolved.version_label is not None:
            memo["typeflux_workflow_version"] = resolved.version_label
        if extra_memo:
            for key, value in extra_memo.items():
                if key not in memo:
                    memo[key] = value

        start_options: dict[str, Any] = dict(start_kwargs)
        if resolved.search_attribute is not None:
            from temporalio.common import (
                SearchAttributeKey,
                SearchAttributePair,
                TypedSearchAttributes,
            )

            start_options["search_attributes"] = TypedSearchAttributes(
                [
                    SearchAttributePair(
                        SearchAttributeKey.for_keyword(resolved.search_attribute),
                        resolved.workflow_name,
                    )
                ]
            )

        # Python `task_queue or spec` truthiness: an empty-string queue falls
        # back to the resolved one (the pinned "" parity rule).
        queue = task_queue or resolved.task_queue
        handle = await client.start_workflow(
            TS_GENERIC_WORKFLOW_TYPE,
            args=[resolved.plan, input_value],
            id=workflow_id,
            task_queue=queue,
            memo=memo,
            **start_options,
        )
        run_id = next(
            (
                value
                for value in (
                    getattr(handle, "first_execution_run_id", None),
                    getattr(handle, "result_run_id", None),
                    getattr(handle, "run_id", None),
                )
                if isinstance(value, str) and value
            ),
            None,
        )
        return WorkflowStartReceipt(
            workflow_id=workflow_id,
            run_id=run_id,
            workflow_name=resolved.workflow_name,
            workflow_type=TS_GENERIC_WORKFLOW_TYPE,
            spec_digest=resolved.spec_digest,
            task_queue=queue,
            trace_query_hint={"workflow_id": workflow_id, "limit": 1},
        )

    @staticmethod
    def _visibility_query(resolved: Any) -> str:
        # The profile's listing query: the constant generic type, narrowed by
        # the configured search attribute when the spec opts in. The attribute
        # NAME is spec-validated to [A-Za-z][A-Za-z0-9_]*; the VALUE is
        # quote-escaped — injection-safe, mirroring the TS implementation.
        query = f"WorkflowType = '{TS_GENERIC_WORKFLOW_TYPE}'"
        if resolved.search_attribute is not None:
            escaped = resolved.workflow_name.replace("'", "''")
            query += f" AND {resolved.search_attribute} = '{escaped}'"
        return query

    async def list_executions(self, *, limit: int = 20) -> WorkflowExecutionList:
        """List this workflow's executions (#671; TS ``listWorkflowExecutions``).

        Identity lives in the MEMO under this profile (one generic type), so
        the listing is a bounded visibility scan over
        ``TS_GENERIC_WORKFLOW_TYPE`` — optionally narrowed by the configured
        search attribute — filtered client-side on ``typeflux_workflow`` +
        ``typeflux_project``. ``current_version`` compares the execution's
        ``typeflux_spec_digest`` memo against the PINNED plan digest (profile
        ``version_identity: memo``; the Python driver compares type names).
        Newest first (visibility default ordering), every status; the scan
        stops after :data:`EXECUTIONS_SCAN_LIMIT` rows.
        """
        from typeflux.project.runs import (
            WorkflowExecutionList,
            WorkflowExecutionRecord,
            _iso,
        )

        if self._pinned_plan is None:
            # current_version is digest-vs-digest — meaningless without the
            # resolved plan. Same fail-closed posture as start.
            raise TsBindingConfigError(
                "listing executions requires the resolved plan from the "
                "typescript resolver (current_version compares each execution's "
                "memo digest against the current plan digest); configure one "
                "(serve --ts-resolver-cmd)"
            )
        resolved = self._pinned_plan
        client = await self._connect()
        bounded = max(1, min(limit, 100))
        records: list[WorkflowExecutionRecord] = []
        scanned = 0
        async for execution in client.list_workflows(self._visibility_query(resolved)):
            scanned += 1
            memo = await self._describe_memo(execution)
            if (
                memo.get("typeflux_workflow") == self.target.workflow_name
                and memo.get("typeflux_project") == self.target.project_name
            ):
                records.append(
                    WorkflowExecutionRecord(
                        execution_id=execution.id,
                        run_id=getattr(execution, "run_id", None),
                        workflow_type=getattr(execution, "workflow_type", TS_GENERIC_WORKFLOW_TYPE),
                        current_version=memo.get("typeflux_spec_digest") == resolved.spec_digest,
                        status=getattr(getattr(execution, "status", None), "name", None)
                        or "UNKNOWN",
                        start_time=_iso(getattr(execution, "start_time", None)),
                        close_time=_iso(getattr(execution, "close_time", None)),
                    )
                )
                if len(records) >= bounded:
                    break
            if scanned >= EXECUTIONS_SCAN_LIMIT:
                break
        return WorkflowExecutionList(
            logical_workflow=self.target.workflow_name,
            current_workflow_type=TS_GENERIC_WORKFLOW_TYPE,
            executions=tuple(records),
        )

    async def drain_status(self) -> WorkflowDrainStatus:
        """The cross-version drain view (#686; TS ``drain.ts`` ``workflowDrainStatus``).

        Python's drain identity lives in versioned TYPE names; this profile's
        lives in the MEMO — so the view is a bounded RUNNING-only scan over the
        generic type, memo-filtered to this workflow's identity and grouped by
        ``_version_identity_key`` (label else digest[:12], rendered exactly
        like the python binding's type names so the console's
        "key == current_workflow_type" logic works unchanged).
        ``current_workflow_type`` carries the PINNED plan's version identity.
        The scan stops after :data:`EXECUTIONS_SCAN_LIMIT` rows (bounded
        best-effort, same as the executions listing).
        """
        from typeflux.project.drain import WorkflowDrainStatus

        if self._pinned_plan is None:
            # The current version identity is the resolved plan's digest/label —
            # meaningless without it. Same fail-closed posture as list_executions.
            raise TsBindingConfigError(
                "the drain view requires the resolved plan from the typescript "
                "resolver (the current version identity is the plan's "
                "digest/label); configure one (serve --ts-resolver-cmd)"
            )
        resolved = self._pinned_plan
        logical = self.target.workflow_name
        client = await self._connect()
        # UNLIKE the executions listing, the drain view must NOT narrow by the
        # optional search attribute: runs started BEFORE the attribute was
        # configured would be invisible and an undrained old version could
        # read as drained (codex) — a SAFETY view scans the generic type and
        # filters memos client-side only.
        query = f"WorkflowType = '{TS_GENERIC_WORKFLOW_TYPE}' AND ExecutionStatus = 'Running'"
        running: dict[str, int] = {}
        scanned = 0
        truncated = False
        async for execution in client.list_workflows(query):
            scanned += 1
            memo = await self._describe_memo(execution)
            if (
                memo.get("typeflux_workflow") == logical
                and memo.get("typeflux_project") == self.target.project_name
            ):
                label = memo.get("typeflux_workflow_version")
                digest = memo.get("typeflux_spec_digest")
                key = _version_identity_key(
                    logical,
                    label if isinstance(label, str) else None,
                    digest if isinstance(digest, str) else None,
                )
                running[key] = running.get(key, 0) + 1
            if scanned >= EXECUTIONS_SCAN_LIMIT:
                truncated = True
                break
        current_key = _version_identity_key(logical, resolved.version_label, resolved.spec_digest)
        return WorkflowDrainStatus(
            logical_workflow=logical,
            current_workflow_type=current_key,
            query=query,
            running=dict(sorted(running.items())),
            total_running=sum(running.values()),
            # A TRUNCATED scan fails CLOSED: unseen rows could hide an old
            # version's long-running execution — a partial view must never
            # claim decommissioning is safe (codex; the TS mapping matches).
            drained=not truncated and all(key == current_key for key in running),
        )

    async def task_queue_workers(
        self, *, task_queue: str | None = None
    ) -> WorkflowTaskQueueWorkers:
        """Live task-queue poller presence (#686; ``workers.py`` for the ts profile).

        The describe itself is runtime-neutral (``_poller_count``); only the
        QUEUE and the connection are this profile's — the queue is resolution
        output (the pinned plan's ``task_queue``), the connection is the
        driver's composed one. Degradation posture mirrors Python's
        ``workflow_task_queue_workers`` exactly: a connect/describe failure is
        an IN-BAND ``reachable: false`` + detail, never a 500 (the route's
        Temporal bound still catches a hang as 503).
        """
        from typeflux.project.workers import WorkflowTaskQueueWorkers, _poller_count

        # Python `task_queue or resolved.spec.task_queue` truthiness: an
        # empty-string override falls back to the resolved queue.
        queue = task_queue or (
            self._pinned_plan.task_queue if self._pinned_plan is not None else None
        )
        if not queue:
            raise TsBindingConfigError(
                "describing task-queue workers requires the resolved plan from "
                "the typescript resolver (the task queue is resolution output); "
                "configure one (serve --ts-resolver-cmd) or pass an explicit "
                "task_queue"
            )
        try:
            client = await self._connect()
            count = await _poller_count(client, self.target.namespace, queue)
        except Exception as exc:  # noqa: BLE001 - degrade, never 500 the panel.
            return WorkflowTaskQueueWorkers(
                task_queue=queue,
                reachable=False,
                detail=f"could not reach Temporal: {exc}",
            )
        return WorkflowTaskQueueWorkers(
            task_queue=queue,
            reachable=True,
            workers_polling=count,
        )

    async def _enforce_frozen_version(self, client: Any, resolved: Any) -> None:
        """Frozen workflow.version at start (#662 parity for the foreign CP).

        The ts profile's freeze lives in the MEMO (not the type name): scan the
        generic type's executions most-recent-first for this workflow identity
        and refuse a digest mismatch. Best-effort exactly like the TS edition's
        ``enforceFrozenWorkflowVersion``: no visibility support or a failed
        query warns and degrades; a MISMATCH always refuses.
        """
        import logging

        logger = logging.getLogger(__name__)
        label = resolved.version_label
        list_workflows = getattr(client, "list_workflows", None)
        if list_workflows is None:
            logger.warning(
                "skipping frozen workflow.version check for %s@%s: the client has "
                "no visibility support (list_workflows)",
                resolved.workflow_name,
                label,
            )
            return
        query = self._visibility_query(resolved)
        recorded: str | None = None
        scanned = 0
        scan_limit = 1000  # the TS FROZEN_VERSION_SCAN_LIMIT
        try:
            async for execution in list_workflows(query):
                scanned += 1
                memo = await self._describe_memo(execution)
                if (
                    memo.get("typeflux_workflow") == resolved.workflow_name
                    and memo.get("typeflux_project") == self.target.project_name
                    and memo.get("typeflux_workflow_version") == label
                ):
                    digest = memo.get("typeflux_spec_digest")
                    recorded = digest if isinstance(digest, str) else None
                    break
                if scanned >= scan_limit:
                    logger.warning(
                        "frozen workflow.version check for %s@%s scanned %d executions "
                        "without an identity match; treating the label as fresh",
                        resolved.workflow_name,
                        label,
                        scan_limit,
                    )
                    break
        except Exception as exc:
            logger.warning(
                "skipping frozen workflow.version check for %s@%s: visibility query failed (%s)",
                resolved.workflow_name,
                label,
                type(exc).__name__,
            )
            return
        if recorded is not None and recorded != resolved.spec_digest:
            raise TsBindingConfigError(
                f"workflow.version '{label}' is frozen to spec digest '{recorded}', "
                f"but the loaded YAML graph has digest '{resolved.spec_digest}'; "
                "assign a new workflow.version for graph changes instead of reusing "
                "a version label"
            )

    async def status(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
        trace: bool = False,
    ) -> WorkflowOperationStatus:
        from typeflux.project.operations import (
            WorkflowOperationStatus,
            effective_valid_user_decisions,
        )

        handle = await self._bound_handle(workflow_id, run_id)
        value = await handle.query("typeflux_lifecycle_status", result_type=WorkflowLifecycleStatus)
        status = (
            value
            if isinstance(value, WorkflowLifecycleStatus)
            else WorkflowLifecycleStatus.model_validate(value)
        )
        return WorkflowOperationStatus(
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            # Prefer the running execution's own waiting-gate decisions over the resolved spec
            # (#55 §6 drift caveat); identical for single-gate workflows.
            valid_user_decisions=effective_valid_user_decisions(status, self.valid_user_decisions),
        )

    def valid_user_decisions(self) -> dict[str, str]:
        return dict(self.target.valid_user_decisions)

    async def submit_review(
        self,
        workflow_id: str,
        command: ReviewCommand | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None:
        parsed = (
            command if isinstance(command, ReviewCommand) else ReviewCommand.model_validate(command)
        )
        handle = await self._bound_handle(workflow_id, run_id)
        await handle.signal("typeflux_submit_review", parsed)

    async def request_cancel(
        self,
        workflow_id: str,
        reason: str | None = None,
        *,
        run_id: str | None = None,
    ) -> None:
        handle = await self._bound_handle(workflow_id, run_id)
        await handle.signal("typeflux_request_cancel", reason)

    async def migrate(
        self,
        execution_id: str,
        *,
        run_id: str | None = None,
        abandon_gates: bool = False,
        reason: str | None = None,
        dry_run: bool = False,
    ) -> Any:
        """Terminate a running execution and resubmit it against this version (#204).

        ``dry_run`` (#791): every preflight runs — binding, same-version, frozen
        version, gates, input read — and the preview returns before terminating.

        Under this profile every version shares the generic type; version
        identity lives in the memo (``_version_identity_key``). So — unlike the
        version bound in ``_bound_handle`` — the old execution is verified for
        project + logical workflow only, and its version identity is REQUIRED to
        differ (the same-version guard). The start leg reuses ``start`` (frozen
        version, identity memo, search attributes) with the additive
        ``typeflux_migrated_from`` provenance.
        """
        from typeflux.project.migrate import (
            MigrateExecutionClosedError,
            NoServingWorkersError,
            SameVersionMigrateError,
            WaitingGateMigrateError,
            WorkflowMigrateResult,
            is_execution_closed_error,
            migrate_partial_error,
            migrate_termination_reason,
            read_start_event_input,
        )
        from typeflux.project.workers import _poller_count

        if self._pinned_plan is None:
            raise TsBindingConfigError(
                "migrating a ts-plan-argument execution requires the resolved plan "
                "from the typescript resolver (the target version identity is the "
                "plan's digest/label); configure one (serve --ts-resolver-cmd)"
            )
        resolved = self._pinned_plan
        new_version_key = _version_identity_key(
            self.target.workflow_name, resolved.version_label, resolved.spec_digest
        )

        client = await self._connect()
        handle = client.get_workflow_handle(execution_id, run_id=run_id)
        description = await handle.describe()
        actual_type = getattr(description, "workflow_type", None)
        if actual_type != TS_GENERIC_WORKFLOW_TYPE:
            raise LifecycleBindingError(
                f"migrate refused: execution {execution_id!r} has workflow type "
                f"{actual_type!r}, not the ts-plan-argument generic type "
                f"{TS_GENERIC_WORKFLOW_TYPE!r}"
            )
        memo = await self._describe_memo(description)
        if memo.get("typeflux_project") != self.target.project_name:
            raise LifecycleBindingError(
                f"migrate refused: execution {execution_id!r} belongs to project "
                f"{memo.get('typeflux_project')!r}, not the bound project "
                f"{self.target.project_name!r}"
            )
        if memo.get("typeflux_workflow") != self.target.workflow_name:
            raise LifecycleBindingError(
                f"migrate refused: execution {execution_id!r} is workflow "
                f"{memo.get('typeflux_workflow')!r}, not the bound workflow "
                f"{self.target.workflow_name!r}"
            )
        described_run_id = getattr(description, "run_id", None)
        if run_id is None and isinstance(described_run_id, str) and described_run_id:
            handle = client.get_workflow_handle(execution_id, run_id=described_run_id)
        old_run_id = described_run_id if isinstance(described_run_id, str) else run_id
        old_version_key = _version_identity_key(
            self.target.workflow_name,
            memo.get("typeflux_workflow_version")
            if isinstance(memo.get("typeflux_workflow_version"), str)
            else None,
            memo.get("typeflux_spec_digest")
            if isinstance(memo.get("typeflux_spec_digest"), str)
            else None,
        )
        if old_version_key == new_version_key:
            raise SameVersionMigrateError(
                f"migrate refused: execution {execution_id!r} already runs the current "
                f"version {new_version_key!r}; migrating onto the same graph version is a "
                "no-op — nothing to migrate to"
            )

        pollers = await _poller_count(client, self.target.namespace, resolved.task_queue)
        if pollers <= 0:
            raise NoServingWorkersError(
                f"migrate refused: no workers are polling the target task queue "
                f"{resolved.task_queue!r} for version {new_version_key!r}; deploy the "
                "new version's workers before migrating (the new run would otherwise "
                "sit pending forever)"
            )

        value = await handle.query("typeflux_lifecycle_status", result_type=WorkflowLifecycleStatus)
        status = (
            value
            if isinstance(value, WorkflowLifecycleStatus)
            else WorkflowLifecycleStatus.model_validate(value)
        )
        abandoned_gate_ids: tuple[str, ...] = ()
        if status.waiting_gates:
            gate_ids = tuple(gate.gate_id for gate in status.waiting_gates)
            if not abandon_gates:
                raise WaitingGateMigrateError(
                    f"migrate refused: execution {execution_id!r} is waiting at review "
                    f"gate(s) {', '.join(gate_ids)}; deciding the gate first preserves the "
                    "human decision, or pass abandon_gates to acknowledge that terminate-"
                    "and-resubmit discards the pending review"
                )
            abandoned_gate_ids = gate_ids

        # PREFLIGHT-BEFORE-TERMINATE (#204 review): every start-leg precondition
        # runs while the old execution is still alive, so a static failure never
        # leaves it dead with no replacement. After the terminate below, only a
        # genuine transport/race error remains.
        #
        # 1. Input carry-over: for this profile the workflow input is the SECOND
        #    start argument (args=[plan, input]); read AND validate it against
        #    the CURRENT version's input schema before terminating.
        input_value = await read_start_event_input(handle, client.data_converter, input_index=1)
        self._validate_migrated_input(input_value, execution_id=execution_id)
        # 2. Frozen version: the same check the start leg runs, as a read-only
        #    preflight (a reused label whose digest changed must refuse HERE).
        if resolved.version_label is not None:
            await self._enforce_frozen_version(client, resolved)

        if dry_run:
            # Preview (#791): every preflight above ran; stop before the mutation.
            return WorkflowMigrateResult(
                execution_id=execution_id,
                old_run_id=old_run_id if isinstance(old_run_id, str) else "",
                new_run_id=None,
                old_version_key=old_version_key,
                new_version_key=new_version_key,
                abandoned_gate_ids=abandoned_gate_ids,
                dry_run=True,
            )

        try:
            await handle.terminate(migrate_termination_reason(new_version_key, reason))
        except Exception as exc:
            if is_execution_closed_error(exc):
                raise MigrateExecutionClosedError(
                    f"migrate conflict: execution {execution_id!r} (run "
                    f"{old_run_id or '<unknown>'}) is already closed — it may have "
                    "completed or been migrated concurrently; nothing was terminated "
                    "or started"
                ) from exc
            raise

        provenance: dict[str, str] = {"typeflux_migrated_from_version": old_version_key}
        if isinstance(old_run_id, str) and old_run_id:
            provenance["typeflux_migrated_from"] = old_run_id
        try:
            receipt = await self.start(input_value, workflow_id=execution_id, extra_memo=provenance)
        except Exception as exc:
            # The DISTINGUISHED partial-failure shape (#204 review): the old run
            # is gone, the replacement did not start, the carried input is intact.
            raise migrate_partial_error(execution_id, old_run_id, exc) from exc
        return WorkflowMigrateResult(
            execution_id=execution_id,
            old_run_id=old_run_id if isinstance(old_run_id, str) else "",
            new_run_id=receipt.run_id,
            old_version_key=old_version_key,
            new_version_key=new_version_key,
            abandoned_gate_ids=abandoned_gate_ids,
        )

    def _validate_migrated_input(self, input_value: Any, *, execution_id: str) -> None:
        """Validate the carried-over input against the CURRENT version's schema.

        The migrate preflight twin of the control plane's start-path
        ``_coerce_input`` foreign-edition branch: the input model is project
        CODE in the other language, so the resolver-pinned JSON Schema is the
        wire-portable contract. Fails CLOSED before the terminate — a missing
        schema or an invalid input refuses the migration while the old run is
        still alive.
        """
        from typeflux.project.migrate import MigratedInputError

        if not isinstance(self._input_json_schema, dict):
            reason = self._input_schema_error
            raise TsBindingConfigError(
                "migrating requires the workflow's input schema from the typescript "
                "resolver to validate the carried-over input against the current "
                "version (give it the project's schemas, e.g. --schemas/"
                "--conformance-schemas)" + (f": {reason}" if reason else "")
            )
        try:
            import jsonschema
        except ImportError as exc:  # pragma: no cover - stale env only
            raise TsBindingConfigError(
                "this server is missing the jsonschema dependency required to "
                "validate the carried-over input — reinstall the api extra "
                "(pip install 'typeflux[api]')"
            ) from exc
        schema_name = self._input_schema_name or "input"
        try:
            # Draft 2020-12 + FormatChecker, exactly like the start path's
            # foreign-edition validation (see controlplane/api.py _coerce_input).
            jsonschema.validate(
                input_value,
                self._input_json_schema,
                cls=jsonschema.Draft202012Validator,
                format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
            )
        except jsonschema.ValidationError as exc:
            raise MigratedInputError(
                f"migrate refused: execution {execution_id!r} original input is not "
                f"valid for the current version's input schema {schema_name}: "
                f"{exc.message}"
            ) from exc
        except jsonschema.SchemaError as exc:
            raise TsBindingConfigError(
                f"the workflow's input JSON Schema for {schema_name} is invalid "
                f"(resolver emission): {exc.message}"
            ) from exc

    def shutdown(self) -> None:
        self._client = None


__all__ = [
    "TS_GENERIC_WORKFLOW_TYPE",
    "TsBindingConfigError",
    "TsBindingTarget",
    "TsPlanArgumentDriver",
]
