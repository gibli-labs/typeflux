"""Deployment plans + GitHub-PR-based approval gate (#253).

A deployment plan is an in-repo, immutable, content-hashed YAML file under
``deployments/`` that pins the identity a promotion will emit artifacts
for: workflow versioned type + spec digest, code sha, target environment,
policy hash, digest-pinned image, and the preflight checklist.

Approval is the GitHub PR review that merges the file to main; git is the
audit trail. Promotion (``project deploy --apply <file>``) verifies the
plan against the currently-resolved bundle and fails closed on drift.

The plan never carries secret values, prompt text, or rendered config —
identities and hashes only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from yaml import YAMLError

from typeflux.project.bundle import resolve_workflow_bundle
from typeflux.project.deployment import (
    PLACEHOLDER_IMAGE_DIGEST,
    ProjectDeploymentError,
    _validate_image,
    assert_closure_observability_consistent,
    is_placeholder_image_digest,
)
from typeflux.project.environment import resolve_project_workflow
from typeflux.project.loader import validate_project
from typeflux.project.spec import TypefluxProjectSpec
from typeflux.yaml.loader import strict_safe_load

PLAN_VERSION: Literal["1"] = "1"
PLAN_DIR_NAME = "deployments"
#: The SYNTHETIC mismatch path the promote gate emits for a placeholder-digest plan (#757 item 5).
#: Not a literal field diff: ``plan_value`` carries the literal offending image and
#: ``current_value`` the human explanation (the ``resolution``/``parse`` synthetic-path precedent),
#: so generic diff renderers over the sibling literal paths (spec_digest, workflow_type,
#: policy_hash) stay coherent. Renderers that special-case explanations key off this code.
PLACEHOLDER_IMAGE_MISMATCH_PATH = "deployment.image_placeholder"
#: The manifest id charset (`_ID_PATTERN` in both editions' project specs). Plan identity ids
#: are constrained to it at LOAD time so a tampered plan cannot smuggle shell metacharacters
#: into ``plan_id``-derived surfaces (filenames, the console's ``promote_command``) — the whole
#: charset is shell-safe by construction.
_SAFE_PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class PlanIdentityCode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str
    branch: str | None = None
    repo_url: str | None = None


class PlanIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    workflow_name: str
    workflow_type: str
    spec_digest: str
    spec_digest_algorithm: str
    environment_id: str
    code: PlanIdentityCode | None = None


class PlanPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_policy_ids: tuple[str, ...] = ()
    applied_policy_ids: tuple[str, ...] = ()
    policy_hash: str


class PlanPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    issue_codes: tuple[str, ...] = ()


class PlanDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str
    image_digest_pinned: bool
    preflight: PlanPreflight


class DeploymentPlan(BaseModel):
    """An immutable, content-hashed deployment plan.

    ``plan_hash`` is sha256 over the canonical-JSON of the file with ``plan_hash``
    AND ``generated_at`` zeroed (the timestamp is run-provenance), so an identical
    (identity, policy, image, preflight) composition always yields the same hash
    and the same filename.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["1"] = PLAN_VERSION
    plan_hash: str
    generated_at: str
    identity: PlanIdentity
    policy: PlanPolicy
    deployment: PlanDeployment

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)

    @property
    def plan_id(self) -> str:
        """Filename-friendly identifier (workflow.env.<hash12>)."""
        return f"{self.identity.workflow_id}.{self.identity.environment_id}.{self.plan_hash[:12]}"


class PlanMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    plan_value: Any
    current_value: Any


class PlanVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    mismatches: tuple[PlanMismatch, ...] = ()


def _compute_plan_hash(payload: dict[str, Any]) -> str:
    """Canonical-JSON sha256 with ``plan_hash`` AND ``generated_at`` zeroed — like policy
    hashing. ``generated_at`` is run-provenance: hashing it would give the identical
    (identity, policy, image, preflight) composition a different hash (and filename) per
    run, breaking the documented same-composition-same-file contract (#798 surfaced this;
    the machine-independent plan story depends on it).

    Deliberately NO legacy-hash acceptance for plans written under the old input set
    (recorded decision, #798 review): the project is pre-adoption — no legacy modes or
    migration shims — and a stale plan regenerates + re-approves in one command. The TS
    twin changes in lockstep."""
    payload = {**payload, "plan_hash": "", "generated_at": ""}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_dir(project: TypefluxProjectSpec) -> Path:
    return project.manifest_path.parent / PLAN_DIR_NAME


def write_deployment_plan(
    project: TypefluxProjectSpec,
    *,
    workflow_id: str,
    environment_id: str,
    image: str,
    policy_ids: tuple[str, ...] = (),
    allow_mutable_image: bool = False,
    allow_placeholder_image: bool = False,
    out_dir: Path | None = None,
    now: datetime | None = None,
    base_env: Mapping[str, str] | None = None,
) -> tuple[Path, DeploymentPlan]:
    """Resolve the bundle, compose the plan, write the immutable file.

    ``out_dir`` overrides the default ``<manifest_dir>/deployments`` location.

    ``base_env`` (#760): the hermetic interpolation base for `${VAR}` spec references —
    the plan's ``spec_digest`` (and the whole sub-workflow closure it folds in) is then
    computed against the injected mapping, never the operator's shell, so a committed
    plan file is machine-independent. Omitted, interpolation reads the process
    environment as before. Pass the same mapping to :func:`verify_deployment_plan` to
    reproduce the digest at promote time. The TS twin is threading an ``env`` into
    ``planResolverFor`` and handing that resolver to ``writeDeploymentPlan``. No CLI
    flag exposes this yet — when a CLI consumer materializes it would attach to the
    ``typeflux-project plan``/``deploy`` verbs as e.g. ``--base-env-file``.
    """
    # Reuse the existing digest-pin + placeholder-digest enforcement; same posture as `deploy`. A
    # plan pinning the all-zeros placeholder is refused at WRITE time (#757 item 5) unless opted in,
    # so an approval file can never carry an image that only fails at pod scheduling.
    image_digest_pinned = _validate_image(
        image,
        allow_mutable_image=allow_mutable_image,
        allow_placeholder_image=allow_placeholder_image,
    )

    bundle = resolve_workflow_bundle(
        project,
        workflow_id=workflow_id,
        environment_id=environment_id,
        policy_ids=policy_ids,
        deployment_image=None,
        base_env=base_env,
    )
    workflow = bundle.workflow
    policy = bundle.policy
    if policy is None:
        raise ProjectDeploymentError(
            f"workflow {workflow_id!r} resolves under no policy in environment "
            f"{environment_id!r}; refusing to write a deployment plan without a policy"
        )
    # #757 review: the plan file is the APPROVAL artifact, so the #756 closure
    # observability-consistency gate runs at write time too — a parent-langfuse/child-langsmith
    # composition must fail authoring (ObservabilityCompositionError), never mint an approvable
    # file that crash-loops at pod boot. Direct API callers (not just the CLI's build-first
    # path) hit this gate.
    assert_closure_observability_consistent(
        project,
        resolve_project_workflow(
            project, workflow_id=workflow_id, environment_id=environment_id, base_env=base_env
        ),
        environment_id=environment_id,
    )
    code = bundle.code
    identity = PlanIdentity(
        workflow_id=workflow.id,
        workflow_name=workflow.workflow_name,
        workflow_type=workflow.workflow_type,
        spec_digest=workflow.spec_digest,
        spec_digest_algorithm=workflow.spec_digest_algorithm,
        environment_id=bundle.environment.id,
        code=PlanIdentityCode(sha=code.sha, branch=code.branch, repo_url=code.repo_url)
        if code is not None
        else None,
    )
    # Reaching here means resolution succeeded: policy admission is enforced by
    # resolve_workflow_bundle (it raises on a disallowed provider/model), the
    # image digest-pin is enforced above, and the no-policy case is rejected
    # below. The structural project preflight is the same gate `deploy` checks
    # before it builds; we record its result without re-running the
    # env-dependent connection resolution (which `deploy` itself does not gate
    # on, and which would make plan-writing depend on runtime env vars).
    structural = validate_project(project)
    preflight = PlanPreflight(
        ok=structural.ok,
        issue_codes=tuple(sorted({issue.code for issue in structural.issues})),
    )
    if not structural.ok:
        raise ProjectDeploymentError(
            f"project fails structural preflight; refusing to write a plan "
            f"(issue codes: {', '.join(preflight.issue_codes) or 'unknown'})"
        )
    deployment = PlanDeployment(
        image=image,
        image_digest_pinned=image_digest_pinned,
        preflight=preflight,
    )
    plan_policy = PlanPolicy(
        selected_policy_ids=policy.selected_policy_ids,
        applied_policy_ids=policy.applied_policy_ids,
        policy_hash=policy.policy_hash,
    )

    skeleton = {
        "plan_version": PLAN_VERSION,
        "plan_hash": "",
        "generated_at": (now or datetime.now(tz=UTC)).isoformat(),
        "identity": identity.model_dump(mode="json"),
        "policy": plan_policy.model_dump(mode="json"),
        "deployment": deployment.model_dump(mode="json"),
    }
    plan_hash = _compute_plan_hash(skeleton)
    plan = DeploymentPlan(
        plan_hash=plan_hash,
        generated_at=skeleton["generated_at"],
        identity=identity,
        policy=plan_policy,
        deployment=deployment,
    )

    plan_dir = out_dir if out_dir is not None else _plan_dir(project)
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{plan.plan_id}.yaml"
    if path.exists():
        existing = load_deployment_plan(project, path)
        if existing.plan_hash != plan.plan_hash:
            raise ProjectDeploymentError(
                f"deployment plan {path} already exists with a different content "
                f"hash ({existing.plan_hash[:12]} vs {plan.plan_hash[:12]}); plans "
                "are immutable — a new composition is a new file"
            )
        # Same content: re-writing is a no-op but harmless.
    path.write_text(plan.to_yaml(), encoding="utf-8")
    return path, plan


def load_deployment_plan(_project: TypefluxProjectSpec, path: Path) -> DeploymentPlan:
    """Strict-parse + validate a plan file, and verify its content INTEGRITY.

    The canonical hash is RECOMPUTED over the loaded content (hash field zeroed) and compared
    to the stored ``plan_hash`` — a post-approval edit (image, policy ids, digest) with the
    stale hash left in place is rejected as tampering, so ``--apply`` promotion can never
    consume tampered values. Identity ids are additionally constrained to the shell-safe
    manifest id charset (they feed ``plan_id``-derived filenames and the promote command).
    """
    try:
        raw = strict_safe_load(path.read_text(encoding="utf-8"))
    except (ValueError, YAMLError) as exc:
        # The strict loader raises on duplicate keys / size / alias bounds where the old
        # bare safe_load did not — wrap so listing keeps its skip-malformed behavior.
        raise ProjectDeploymentError(f"deployment plan {path} failed to parse: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectDeploymentError(f"deployment plan {path} is not a YAML mapping")
    plan = DeploymentPlan.model_validate(raw)
    recomputed = _compute_plan_hash(plan.model_dump(mode="json"))
    if recomputed != plan.plan_hash:
        raise ProjectDeploymentError(
            f"deployment plan {path} failed its integrity check: plan_hash "
            f"{plan.plan_hash[:12]} does not match the file content (recomputed "
            f"{recomputed[:12]}); the file was modified after it was written — plans are "
            "immutable, regenerate a new plan instead of editing one"
        )
    for field_name, value in (
        ("workflow_id", plan.identity.workflow_id),
        ("environment_id", plan.identity.environment_id),
    ):
        if _SAFE_PLAN_ID_PATTERN.fullmatch(value) is None:
            raise ProjectDeploymentError(
                f"deployment plan {path} carries an unsafe identity.{field_name}: {value!r} "
                "(must match the manifest id charset)"
            )
    return plan


class DeploymentPlanDirEntry(BaseModel):
    """One file under ``deployments/``: a loaded plan, or the load error for a malformed file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    plan: DeploymentPlan | None = None
    error: str | None = None


def read_deployment_plan_dir(project: TypefluxProjectSpec) -> tuple[DeploymentPlanDirEntry, ...]:
    """Read every ``*.yaml`` under the plan dir, keeping malformed files VISIBLE.

    A file that fails to load (parse, validation, or the integrity check) becomes an error
    entry (filename + load error) rather than silently dropping out — GET /deployments
    surfaces each so an operator sees a corrupt/tampered plan file, not a shorter listing.
    """
    plan_dir = _plan_dir(project)
    if not plan_dir.exists():
        return ()
    entries: list[DeploymentPlanDirEntry] = []
    for path in sorted(plan_dir.glob("*.yaml")):
        try:
            entries.append(
                DeploymentPlanDirEntry(file=path.name, plan=load_deployment_plan(project, path))
            )
        except (ProjectDeploymentError, ValidationError) as exc:
            entries.append(DeploymentPlanDirEntry(file=path.name, error=str(exc)))
    return tuple(entries)


def list_deployment_plans(project: TypefluxProjectSpec) -> tuple[DeploymentPlan, ...]:
    """Every VALID plan under ``deployments/``; malformed files are omitted here — use
    :func:`read_deployment_plan_dir` to surface them as error entries."""
    return tuple(
        entry.plan for entry in read_deployment_plan_dir(project) if entry.plan is not None
    )


def verify_deployment_plan(
    project: TypefluxProjectSpec,
    plan: DeploymentPlan,
    *,
    allow_placeholder_image: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> PlanVerification:
    """Compare the plan against the currently-resolved bundle. Fail closed.

    PROMOTE GATE (#757 item 5): a plan pinning the well-known all-zeros placeholder digest is refused
    here too, so a placeholder plan that slipped past write-time still cannot promote — it would only
    fail at pod scheduling (ImagePullBackOff). The refusal carries its OWN synthetic check identity
    (:data:`PLACEHOLDER_IMAGE_MISMATCH_PATH`): ``plan_value`` is the literal offending image and
    ``current_value`` the explanation — never a prose blob in a literal diff slot. Pass
    ``allow_placeholder_image`` to promote a placeholder plan intentionally.

    ``base_env`` (#760): pass the SAME hermetic interpolation base the plan was written
    with — the current resolution's ``spec_digest`` is recomputed under it, so a
    hermetically-written plan verifies on any machine regardless of the local shell.
    """
    bundle = resolve_workflow_bundle(
        project,
        workflow_id=plan.identity.workflow_id,
        environment_id=plan.identity.environment_id,
        policy_ids=plan.policy.selected_policy_ids,
        deployment_image=None,
        base_env=base_env,
    )
    mismatches: list[PlanMismatch] = []
    if not allow_placeholder_image and is_placeholder_image_digest(plan.deployment.image):
        # A distinct synthetic check code (the `resolution`/`parse` precedent): the literal
        # offending value rides plan_value, the explanation rides current_value — the sibling
        # literal-diff paths (spec_digest, workflow_type, policy_hash) keep same-typed values.
        mismatches.append(
            PlanMismatch(
                path=PLACEHOLDER_IMAGE_MISMATCH_PATH,
                plan_value=plan.deployment.image,
                current_value=(
                    f"the all-zeros placeholder digest ({PLACEHOLDER_IMAGE_DIGEST}) resolves to no "
                    "published image and fails at pod scheduling (ImagePullBackOff); regenerate the "
                    "plan with a real published digest, or pass --allow-placeholder-image "
                    "(allow_placeholder_image) to promote it intentionally"
                ),
            )
        )
    if bundle.workflow.spec_digest != plan.identity.spec_digest:
        mismatches.append(
            PlanMismatch(
                path="identity.spec_digest",
                plan_value=plan.identity.spec_digest,
                current_value=bundle.workflow.spec_digest,
            )
        )
    if bundle.workflow.workflow_type != plan.identity.workflow_type:
        mismatches.append(
            PlanMismatch(
                path="identity.workflow_type",
                plan_value=plan.identity.workflow_type,
                current_value=bundle.workflow.workflow_type,
            )
        )
    current_policy_hash = bundle.policy.policy_hash if bundle.policy is not None else ""
    if current_policy_hash != plan.policy.policy_hash:
        mismatches.append(
            PlanMismatch(
                path="policy.policy_hash",
                plan_value=plan.policy.policy_hash,
                current_value=current_policy_hash,
            )
        )
    structural = validate_project(project)
    if not structural.ok:
        mismatches.append(
            PlanMismatch(
                path="preflight.ok",
                plan_value=True,
                current_value=False,
            )
        )
    return PlanVerification(ok=not mismatches, mismatches=tuple(mismatches))


__all__ = [
    "PLACEHOLDER_IMAGE_MISMATCH_PATH",
    "PLAN_VERSION",
    "DeploymentPlan",
    "DeploymentPlanDirEntry",
    "PlanDeployment",
    "PlanIdentity",
    "PlanIdentityCode",
    "PlanMismatch",
    "PlanPolicy",
    "PlanPreflight",
    "PlanVerification",
    "list_deployment_plans",
    "load_deployment_plan",
    "read_deployment_plan_dir",
    "verify_deployment_plan",
    "write_deployment_plan",
]


def verify_plan_merged_to_default_branch(plan_path: Path, *, project_root: Path) -> str | None:
    """The #790 merged-plan gate: ``None`` when the plan file's exact bytes exist at
    its path on the remote default branch, else a human-readable refusal.

    Git-native and hermetic (no network, no GitHub API): plans are immutable and
    content-hashed, so byte-identity on ``origin/<default>`` is equivalent to "this
    exact reviewed artifact was merged". The default branch comes from
    ``git symbolic-ref refs/remotes/origin/HEAD`` (set by clone; restorable with
    ``git remote set-head origin --auto``)."""
    import os
    import subprocess

    # Anchor EVERYTHING to the project manifest's checkout: a plan path (absolute, or
    # with a symlinked directory component) must never redirect git commands into some
    # other repository.
    project_root = project_root.resolve()

    supplied_path = plan_path
    if plan_path.is_symlink():
        # A symlinked plan would redirect verification to WHATEVER repo the target
        # lives in — verify only real files at their supplied path.
        return (
            f"--require-merged-plan: plan {plan_path} is a symlink; promote the real plan "
            "file so the gate verifies this repository's merged artifact."
        )
    plan_path = plan_path.parent.resolve() / plan_path.name
    if not plan_path.is_file():
        return f"--require-merged-plan: plan {plan_path} does not exist locally."

    def _git(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            text=False,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()

    code, top_level = _git(["rev-parse", "--show-toplevel"])
    if code != 0 or not top_level:
        return (
            f"--require-merged-plan: the project manifest at {project_root} is not inside a "
            "git checkout, so the merged-plan gate cannot verify plans. Promote from the "
            "repository clone (CI), or drop the flag for non-repo promote flows."
        )
    try:
        plan_path.relative_to(Path(top_level).resolve())
    except ValueError:
        return (
            f"--require-merged-plan: plan {plan_path} resolves outside the project's git "
            f"checkout ({top_level}) — the gate verifies only this repository's merged "
            "artifacts."
        )
    # Reject symlinked DIRECTORY components inside the checkout: a local alias
    # (deployments -> approved) would otherwise verify a different merged path than
    # the one the operator supplied. Walk the supplied (uncanonicalized) ancestors up
    # to the repo root; the root's own canonical prefix (e.g. macOS /var -> /private
    # /var) sits above the repo and is never visited.
    top_real = Path(top_level).resolve()
    probe = Path(os.path.abspath(str(supplied_path))).parent
    while True:
        if probe.resolve() == top_real:
            # `ln -s . alias` (an IN-repo symlink back to the root) must still be
            # rejected; a checkout whose own root is reached through a symlinked
            # spelling from OUTSIDE the repo (macOS /var) is fine.
            parent_real = probe.parent.resolve()
            if probe.is_symlink() and (
                parent_real == top_real or parent_real.is_relative_to(top_real)
            ):
                return (
                    f"--require-merged-plan: plan path component {probe} is a symlink — "
                    "supply the plan's real repository path so the gate verifies the "
                    "merged artifact at that exact path."
                )
            break
        if probe.parent == probe:
            break
        if probe.is_symlink():
            return (
                f"--require-merged-plan: plan path component {probe} is a symlink — supply "
                "the plan's real repository path so the gate verifies the merged artifact "
                "at that exact path."
            )
        probe = probe.parent

    code, default_ref = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if code != 0 or not default_ref:
        return (
            "--require-merged-plan: the remote default branch is unknown "
            "(refs/remotes/origin/HEAD is unset). Run `git remote set-head origin --auto` "
            "and retry."
        )
    rel = plan_path.relative_to(Path(top_level).resolve())

    def _git_top(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", "-C", top_level, *args],
            capture_output=True,
            text=False,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()

    # Pathspecs are CWD-relative: run every path-taking command from the repo
    # toplevel (a NESTED project manifest would otherwise look up the wrong tree
    # path — Bugbot on #845; the TS gate already anchors to the toplevel).
    code, tree_entry = _git_top(["ls-tree", default_ref, "--", rel.as_posix()])
    if code != 0 or not tree_entry:
        return (
            f"--require-merged-plan: plan {rel.as_posix()} does not exist on the default "
            f"branch ({default_ref}) — it has not been merged. Open a PR with the plan file, "
            "merge it, then promote."
        )
    # <mode> <type> <oid>\t<path>: the merged entry must be a REGULAR file blob — a
    # merged symlink whose target text matches would otherwise satisfy the OID compare.
    entry_fields = tree_entry.split()
    if len(entry_fields) < 3 or not entry_fields[0].startswith("100") or entry_fields[1] != "blob":
        return (
            f"--require-merged-plan: plan {rel.as_posix()} on {default_ref} is not a regular "
            "file — merge the plan as a plain file, not a symlink."
        )
    merged_oid = entry_fields[2]
    # A content FILTER on the plan path (LFS, custom filter.*.clean) would let a
    # locally edited file hash to the merged blob while different bytes get promoted —
    # refuse filtered plan paths outright. eol/autocrlf ride the `text` attribute and
    # stay fine.
    code, filter_attr = _git_top(["check-attr", "filter", "--", rel.as_posix()])
    if code == 0 and filter_attr and not filter_attr.endswith((": unspecified", ": unset")):
        return (
            f"--require-merged-plan: plan {rel.as_posix()} carries a git content filter "
            f"({filter_attr.split(': ')[-1]}) — the gate cannot prove the promoted bytes "
            "were merged. Store plans unfiltered."
        )
    # Compare blob OIDs with the clean filter applied (--path) rather than raw bytes:
    # a checkout with eol/autocrlf line-ending conversion must not fail a clean,
    # merged plan.
    code, local_oid = _git_top(["hash-object", "--path", rel.as_posix(), str(plan_path)])
    if code != 0 or local_oid != merged_oid:
        return (
            f"--require-merged-plan: plan {rel.as_posix()} differs from the version merged on "
            f"{default_ref} — the local file is not the reviewed artifact. Promote the merged "
            "plan bytes (git checkout the file from the default branch)."
        )
    return None
