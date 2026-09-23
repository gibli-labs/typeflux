from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, Any

from typeflux.manifests._common import canonical_json

if TYPE_CHECKING:
    from typeflux.yaml.spec import TypefluxYamlSpec
    from typeflux.yaml.workflow import WorkflowCallSpec

# Bump whenever the generated workflow control flow changes semantics for the
# same YAML (command sequence, scheduling order, lifecycle wait behavior).
# Identical YAML through a different generator is a different deterministic
# program and must register as a new workflow type.
#
# v2 (#60): cache-enabled map steps emit a cache-prep activity before the
# fan-out, so the generator's command space changed; cache config also joins the
# per-step digest below so toggling caching alone re-registers the workflow type.
# v3 (#363): the cache-prep activity now takes a representative item as input
# (to resolve cache: reference artifacts) — the scheduled command shape changed,
# so cache-enabled map workflows must re-register rather than replay old no-arg
# prep schedules.
# v4 (#368): reference-style cached map steps schedule a cache-release activity
# after the fan-out — another command added to the sequence, so cache-enabled map
# workflows must re-register.
# v5 (#299): first-class YAML compensation — a `compensate:`-bearing step schedules a
# compensation activity on the failure/cancellation unwind, so the generator's command
# space changed; `compensate` joins the per-step digest below (present-only, so
# compensation-free workflows stay byte-identical WITHIN a generator version). Per the
# NOTE below the bump itself re-registers EVERY workflow type (the accepted pre-adoption
# cutover, mirroring the TS PLAN_INTERPRETER_VERSION bump).
# NOTE: because generator_version is part of every digest, a bump re-registers
# the type of EVERY workflow (not just cached ones) — a one-time hard cutover with
# no overlap window. Acceptable pre-adoption (no in-flight production workflows);
# revisit if/when live workflows must survive a generator upgrade.
GENERATOR_VERSION = "5"

SPEC_DIGEST_ALGORITHM = "typeflux-yaml-graph-v1"

_TYPE_DIGEST_LENGTH = 12


def workflow_spec_digest(spec: TypefluxYamlSpec, calls: tuple[WorkflowCallSpec, ...]) -> str:
    payload = {
        "algorithm": SPEC_DIGEST_ALGORITHM,
        "generator_version": GENERATOR_VERSION,
        "project": spec.project,
        "yaml_name": spec.name,
        "workflow_name": spec.workflow.name,
        "input": spec.workflow.input,
        "output": spec.workflow.output,
        "steps": [_call_payload(call) for call in calls],
        "lifecycle": _lifecycle_payload(spec),
    }
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def registered_workflow_type(spec: TypefluxYamlSpec, spec_digest: str) -> str:
    version = spec.workflow.version
    suffix = version if version is not None else spec_digest[:_TYPE_DIGEST_LENGTH]
    return f"{spec.workflow.name}.{suffix}"


def _call_payload(call: Any) -> dict[str, Any]:
    # Import here to avoid a circular import with yaml.workflow.
    from typeflux.yaml.workflow import (
        MapCallSpec,
        MapSubworkflowCallSpec,
        ParallelCallSpec,
        SubworkflowCallSpec,
    )

    payload: dict[str, Any]
    if isinstance(call, ParallelCallSpec):
        # Composition (#55): the whole nested shape — branch order, gates, nested
        # call sequences, the collect type and bound — is control flow, so all of
        # it is graph identity. Recursive through branch calls.
        payload = {
            "kind": "parallel",
            "step_id": call.step_id,
            "branches": [_branch_payload(branch) for branch in call.branches],
            "collect_output": (
                f"{call.collect.output_type.__module__}.{call.collect.output_type.__name__}"
            ),
            "collect_max_bytes": call.collect.max_bytes,
        }
    elif isinstance(call, MapCallSpec):
        payload = {
            "kind": "map",
            "step_id": call.step_id,
            "activity_name": call.activity_name,
            "over": call.over,
            "concurrency": call.concurrency,
            "collect_output": (
                f"{call.collect.output_type.__module__}.{call.collect.output_type.__name__}"
            ),
            "collect_field": call.collect.field,
            "collect_max_bytes": call.collect.max_bytes,
            # Whether (and how) the step prepares a cached session changes the
            # command sequence, so it is part of graph identity. None ⇒ no cache
            # step emitted, keeping the digest of non-cached maps stable within a
            # generator version.
            "session_cache": _session_cache_payload(call.session_cache),
        }
    elif isinstance(call, SubworkflowCallSpec):
        # A sub-workflow step folds the CHILD's identity (its registered type + digest)
        # into the parent digest (#55 §6): a child-graph edit moves child_digest ->
        # this payload -> the parent's registered_workflow_type. child_type + child_digest
        # are baked at generation time, so the fold needs no live resolution here.
        payload = {
            "kind": "subworkflow",
            "step_id": call.step_id,
            "workflow_id": call.child_workflow_id,
            "child_type": call.child_workflow_type,
            "child_digest": call.child_digest,
        }
    elif isinstance(call, MapSubworkflowCallSpec):
        payload = {
            "kind": "subworkflow_map",
            "step_id": call.step_id,
            "workflow_id": call.child_workflow_id,
            "child_type": call.child_workflow_type,
            "child_digest": call.child_digest,
            "over": call.over,
            "concurrency": call.concurrency,
            "collect_output": (
                f"{call.collect.output_type.__module__}.{call.collect.output_type.__name__}"
            ),
            "collect_field": call.collect.field,
            "collect_max_bytes": call.collect.max_bytes,
        }
    else:
        payload = {
            "kind": "activity",
            "step_id": call.step_id,
            "activity_name": call.activity_name,
        }
    # A `when:` gate changes control flow, so it joins the digest — added only when
    # present, so V1 (gate-less) payloads stay byte-identical and their digests hold.
    if getattr(call, "when", None) is not None:
        payload["when"] = _when_payload(call.when)
    # A `compensate:` clause schedules a compensation command on the unwind, so it joins the
    # digest — added only when present (activity/map/subworkflow steps carry it), so
    # compensation-free payloads stay byte-identical WITHIN a generator version. `input_from`
    # selects the compensation argument, so it participates when present.
    #
    # RETRY DIGEST DIVERGENCE (#299 review, Finder G — decided, not a bug): `compensate.retry`
    # is EXCLUDED here, consistent with how THIS edition already treats a normal activity's
    # `retry` (the Python `_call_payload` activity node carries only step_id + activity_name;
    # timeouts/retry never enter the command-graph digest). The TS edition hashes its whole
    # resolved plan, so it includes BOTH ordinary retry (`activityOptions[name].retry`) AND
    # `compensate.retry` in ITS digest. The two profiles' digests already hash different
    # artifacts by design (binding.v1.json digest_divergence_note); retry has diverged this way
    # since before #299, and #299 keeps `compensate.retry` consistent with each edition's
    # existing retry treatment rather than expanding scope to unify the pre-existing case.
    compensate = getattr(call, "compensate", None)
    if compensate is not None:
        payload["compensate"] = _compensate_payload(compensate)
    return payload


def _compensate_payload(compensate: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"activity": compensate.activity_name}
    if compensate.input_from is not None:
        payload["input_from"] = compensate.input_from
    return payload


def _branch_payload(branch: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "branch_id": branch.branch_id,
        "steps": [_call_payload(nested) for nested in branch.calls],
    }
    if branch.when is not None:
        payload["when"] = _when_payload(branch.when)
    return payload


def _when_payload(when: Any) -> dict[str, Any]:
    return {
        "mode": when.mode,
        "predicates": [
            {
                "path": leaf.path,
                "op": leaf.op,
                "value": list(leaf.value) if isinstance(leaf.value, tuple) else leaf.value,
            }
            for leaf in when.predicates
        ],
    }


def _session_cache_payload(config: Any) -> dict[str, Any] | None:
    # A disabled request emits no prep activity, so it is identical to "no cache"
    # for command-sequence purposes and collapses to None.
    if config is None or not config.enabled:
        return None
    return {"enabled": True, "ttl_seconds": config.ttl_seconds}


def _lifecycle_payload(spec: TypefluxYamlSpec) -> dict[str, Any] | None:
    # Only lifecycle settings that alter generated control flow participate in
    # graph identity; progress accounting and status history bounds do not.
    lifecycle = spec.workflow.lifecycle
    if lifecycle is None or not lifecycle.enabled:
        return None
    review = lifecycle.review
    payload: dict[str, Any] = {
        "cancellation": lifecycle.cancellation,
        "review": None if review is None else _review_payload(review),
    }
    # Multiple named gates (#55 slice 4): the `gates` key joins the digest ONLY when a spec
    # declares it, so V1 single-`review` specs emit no new key and their digests hold. Each
    # gate changes the generated wait/route control flow, so it participates in graph identity.
    if lifecycle.gates is not None:
        payload["gates"] = [_gate_payload(gate) for gate in lifecycle.gates]
    return payload


def _review_payload(review: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "after_step": review.after_step,
        "invalid_user_decision": review.invalid_user_decision,
        "user_decisions": {
            user_decision: route.route
            for user_decision, route in sorted(review.user_decisions.items())
        },
    }
    # A review timeout adds a durable timer + a timeout branch to the generated
    # control flow, so it participates in graph identity (#297). Included only
    # when present, so review workflows without a timeout keep their digest.
    if review.timeout is not None:
        payload["timeout"] = {
            "seconds": review.timeout.seconds,
            "on_timeout": review.timeout.on_timeout,
            "route": review.timeout.route,
        }
    return payload


def _gate_payload(gate: Any) -> dict[str, Any]:
    # A gate is a review node plus its id; reuse the review payload and prepend the id.
    return {"id": gate.id, **_review_payload(gate)}
