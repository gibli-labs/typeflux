"""Python conformance to the Temporal binding contract (#618 slice 2).

Asserts this SDK's exported wire constants against
``contracts/temporal-binding/binding.v1.json`` — the normative document. A
divergence here is a bug in the SDK, not in the document.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio import workflow

from tests.test_yaml import _write_demo_project, _write_demo_yaml
from typeflux.core.contracts import (
    ReviewCommand,
    WorkflowLifecycleEvent,
    WorkflowLifecycleStatus,
)
from typeflux.yaml.identity import (
    GENERATOR_VERSION,
    SPEC_DIGEST_ALGORITHM,
    registered_workflow_type,
)
from typeflux.yaml.imports import collect_activities
from typeflux.yaml.loader import load_yaml_spec
from typeflux.yaml.runtime import _workflow_identity_memo
from typeflux.yaml.workflow import create_workflow

BINDING = json.loads(
    (
        Path(__file__).resolve().parents[3] / "contracts" / "temporal-binding" / "binding.v1.json"
    ).read_text(encoding="utf-8")
)
PROFILE = BINDING["profiles"]["python-versioned-type"]
SHARED = BINDING["shared"]


@pytest.fixture()
def lifecycle_less_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_demo_project(tmp_path, export_name="ALL_ACTIVITIES")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = load_yaml_spec(_write_demo_yaml(tmp_path))
    return spec, create_workflow(spec, collect_activities(spec))


def test_signal_and_query_names_match_the_binding_contract(lifecycle_less_class) -> None:
    _, workflow_cls = lifecycle_less_class
    definition = workflow._Definition.must_from_class(workflow_cls)

    assert set(definition.signals) == {
        SHARED["signals"]["request_cancel"]["name"],
        SHARED["signals"]["submit_review"]["name"],
    }
    assert set(definition.queries) == {SHARED["queries"]["lifecycle_status"]["name"]}


def test_lifecycle_surface_is_always_registered_and_answers_disabled(
    lifecycle_less_class,
) -> None:
    # The demo spec has NO lifecycle: the query still answers (state
    # "disabled") and the signals no-op — unified with the TS edition (#618).
    _, workflow_cls = lifecycle_less_class
    instance = workflow_cls()

    status = instance.typeflux_lifecycle_status()
    assert status == WorkflowLifecycleStatus(state="disabled")
    instance.typeflux_request_cancel("reason")
    instance.typeflux_submit_review({"user_decision": "approve"})


def test_versioned_type_rule_matches_the_binding_contract(lifecycle_less_class) -> None:
    spec, workflow_cls = lifecycle_less_class
    digest = workflow_cls.__typeflux_spec_digest__
    prefix_length = PROFILE["workflow_type"]["digest_prefix_length"]
    separator = PROFILE["workflow_type"]["separator"]

    assert workflow_cls.__typeflux_workflow_type__ == (
        f"{spec.workflow.name}{separator}{digest[:prefix_length]}"
    )
    # With a version label the suffix is the label, not the digest.
    assert (
        registered_workflow_type(
            spec.model_copy(
                update={"workflow": spec.workflow.model_copy(update={"version": "v7"})}
            ),
            digest,
        )
        == f"{spec.workflow.name}{separator}v7"
    )


def test_identity_memo_keys_match_the_binding_contract(lifecycle_less_class) -> None:
    _, workflow_cls = lifecycle_less_class
    memo = _workflow_identity_memo(workflow_cls)

    assert set(memo) == set(SHARED["memo"])
    # This profile never writes extra memo keys (the version lives in the
    # type name, not the memo).
    assert PROFILE["memo_extra"] == {}


def test_digest_identifiers_match_the_binding_contract() -> None:
    assert SPEC_DIGEST_ALGORITHM == PROFILE["digest"]["algorithm"]
    assert PROFILE["digest"]["version_field"] == "generator_version"
    assert GENERATOR_VERSION == PROFILE["digest"]["version_value"]


def test_lifecycle_status_shape_matches_the_binding_contract() -> None:
    assert set(WorkflowLifecycleStatus.model_fields) == set(
        SHARED["workflow_lifecycle_status"]["fields"]
    )
    assert set(WorkflowLifecycleEvent.model_fields) == set(
        SHARED["workflow_lifecycle_status"]["event_fields"]
    )
    assert set(ReviewCommand.model_fields) == set(SHARED["review_command"])
    assert "disabled" in SHARED["workflow_lifecycle_status"]["states"]


def test_payload_codec_wire_format_matches_the_binding_contract() -> None:
    # The AES-256-GCM codec's pinned wire constants (#188) match the shared binding doc.
    from typeflux.yaml.payload_codec import (
        ENCRYPTED_ENCODING,
        KEY_ID_METADATA_KEY,
        NONCE_LEN,
        TAG_LEN,
    )

    codec = SHARED["payload_codec"]
    assert codec["algorithm"].startswith("AES-256-GCM")
    encrypted = codec["encrypted_payload"]
    assert ENCRYPTED_ENCODING == encrypted["metadata"]["encoding"].split(" ")[0].encode("utf-8")
    assert KEY_ID_METADATA_KEY in encrypted["metadata"]
    assert encrypted["data"] == "nonce || ciphertext || tag"
    assert encrypted["data_layout"]["nonce"].startswith(f"{NONCE_LEN} bytes")
    assert encrypted["data_layout"]["tag"].startswith(f"{TAG_LEN}-byte")
