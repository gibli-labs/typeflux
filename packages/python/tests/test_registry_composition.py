"""Sub-workflow registry composition (#748): the ONE registry a composed worker serves is
the MERGE of the parent's registry and every (transitively) referenced child's registry —
disjoint prompts merged in, byte-identical duplicates deduped, genuine conflicts a loud
load-time error, and conflicting EXTERNAL registry configs rejected too. Python edition
parity with ``packages/typescript/temporal-yaml/test/registry-composition.test.ts``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from typeflux.core.contracts import PromptRef
from typeflux.yaml import load_yaml_spec
from typeflux.yaml.runtime import (
    RegistryCompositionError,
    _assert_compatible_registry_config,
    _build_registry,
)


def _spec(
    tmp_path: Path,
    name: str,
    registry_body: str,
    *,
    defs: str | None = None,
    steps: str | None = None,
):
    """Write and load a minimal single-activity spec with a caller-chosen registry block."""
    default_def = (
        '    - { name: act, input: "schemas:X", output: "schemas:X", prompt: shared-prompt }'
    )
    default_step = "    - { id: run, activity: act }"
    text = f"""
project: p
name: {name}
task_queue: q
runtime:
  temporal: {{}}
  registry:
{registry_body}
  provider: {{ type: fake }}
  observability: {{ type: none }}
activities:
  definitions:
{defs or default_def}
workflow:
  name: {name}
  input: schemas:X
  output: schemas:X
  steps:
{steps or default_step}
"""
    path = tmp_path / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return load_yaml_spec(path, load_dotenv=False)


def _inline(prompts: dict[str, str]) -> str:
    lines = ["    type: inline", "    prompts:"]
    lines += [f"      {k}: {v!r}" for k, v in prompts.items()]
    return "\n".join(lines)


def _content(registry, name: str):
    return registry.resolve(PromptRef(name)).messages[0].content


def test_merges_disjoint_inline_registries(tmp_path: Path) -> None:
    parent = _spec(tmp_path, "P", _inline({"shared-prompt": "hi", "a": "parent-a"}))
    child = _spec(tmp_path, "C", _inline({"shared-prompt": "hi", "b": "child-b"}))
    registry = _build_registry(parent, [("child", child)])
    assert _content(registry, "a") == "parent-a"
    assert _content(registry, "b") == "child-b"


def test_dedupes_byte_identical_duplicate(tmp_path: Path) -> None:
    parent = _spec(tmp_path, "P", _inline({"dup": "same"}))
    child = _spec(tmp_path, "C", _inline({"dup": "same"}))
    registry = _build_registry(parent, [("child", child)])
    assert _content(registry, "dup") == "same"


def test_conflicting_same_name_prompt_rejects_naming_both_sources(tmp_path: Path) -> None:
    parent = _spec(tmp_path, "P", _inline({"dup": "parent text"}))
    child = _spec(tmp_path, "C", _inline({"dup": "child text"}))
    with pytest.raises(RegistryCompositionError) as exc:
        _build_registry(parent, [("child-wf", child)])
    message = str(exc.value)
    assert "'dup'" in message
    assert "'P'" in message  # parent spec.name
    assert "'child-wf'" in message


def test_conflict_names_first_differing_field_for_structured_prompt(tmp_path: Path) -> None:
    def structured(text: str) -> str:
        return "\n".join(
            [
                "    type: inline",
                "    prompts:",
                "      p:",
                "        messages:",
                f"          - {{ role: user, content: {text!r} }}",
            ]
        )

    parent = _spec(tmp_path, "P", structured("alpha"))
    child = _spec(tmp_path, "C", structured("beta"))
    with pytest.raises(
        RegistryCompositionError, match=r"first differing field: messages\[0\]\.content"
    ):
        _build_registry(parent, [("child-wf", child)])


def test_conflict_reports_optional_field_set_in_one_absent_in_other(tmp_path: Path) -> None:
    # #748 review: `model` set only in the child — the diff walk must report absence as a
    # first-class state (`set in ... absent in ...`), not crash or misattribute the field.
    # Pydantic dumps an UNSET optional as None, so None counts as absent (TS parity — zod
    # omits the key; both editions report the same copy for the same YAML pair).
    def structured(extra: str) -> str:
        lines = [
            "    type: inline",
            "    prompts:",
            "      p:",
            "        messages:",
            '          - { role: user, content: "same" }',
        ]
        if extra:
            lines.append(f"        {extra}")
        return "\n".join(lines)

    parent = _spec(tmp_path, "P", structured(""))
    child = _spec(tmp_path, "C", structured("model: gpt-4o"))
    with pytest.raises(
        RegistryCompositionError,
        match=r"first differing field: model \(set in 'child-wf', absent in 'P'\)",
    ):
        _build_registry(parent, [("child-wf", child)])


def test_structured_prompts_with_optional_field_absent_in_both_dedupe(tmp_path: Path) -> None:
    structured = "\n".join(
        [
            "    type: inline",
            "    prompts:",
            "      p:",
            "        messages:",
            '          - { role: user, content: "same" }',
        ]
    )
    parent = _spec(tmp_path, "P", structured)
    child = _spec(tmp_path, "C", structured)
    registry = _build_registry(parent, [("child-wf", child)])
    assert _content(registry, "p") == "same"


def test_child_with_different_registry_type_rejects(tmp_path: Path) -> None:
    parent = _spec(tmp_path, "P", _inline({"shared": "hi"}))
    child = _spec(tmp_path, "C", "    type: langfuse\n    host: https://cloud.langfuse.com")
    with pytest.raises(RegistryCompositionError, match=r"declares a 'langfuse' registry.*'inline'"):
        _build_registry(parent, [("child-wf", child)])


def test_external_registries_with_different_config_reject(tmp_path: Path) -> None:
    parent = _spec(tmp_path, "P", "    type: langfuse\n    host: https://a.example")
    child = _spec(tmp_path, "C", "    type: langfuse\n    host: https://b.example")
    with pytest.raises(RegistryCompositionError, match=r"DIFFERENT backend config"):
        _build_registry(parent, [("child-wf", child)])


def test_custom_registries_with_divergent_config_blocks_reject(tmp_path: Path) -> None:
    """#792: a custom registry's declared config is part of its composition identity — the
    same class with different config is a different backend, never a silent use-the-parent's."""
    parent = _spec(
        tmp_path,
        "P",
        "    type: custom\n    class: pkg.mod:Reg\n"
        "    config: { endpoint: https://parent.example }",
    )
    child = _spec(
        tmp_path,
        "C",
        "    type: custom\n    class: pkg.mod:Reg\n    config: { endpoint: https://child.example }",
    )
    with pytest.raises(RegistryCompositionError, match=r"DIFFERENT backend config"):
        _assert_compatible_registry_config(parent, "child-wf", child)


def test_custom_registries_with_identical_config_blocks_pass(tmp_path: Path) -> None:
    cfg = (
        "    type: custom\n    class: pkg.mod:Reg\n"
        "    config:\n      endpoint: https://same.example\n"
        "      api_key: { value_from: { env: REG_KEY } }"
    )
    parent = _spec(tmp_path, "P", cfg)
    child = _spec(tmp_path, "C", cfg)
    _assert_compatible_registry_config(parent, "child-wf", child)  # must not raise


def test_external_registries_with_identical_config_pass_the_check(tmp_path: Path) -> None:
    # Identical external config ⇒ nothing to merge and the composition rule is satisfied.
    # Asserted through the config check directly (building the real Langfuse client needs
    # credentials this offline test intentionally avoids).
    cfg = "    type: langfuse\n    host: https://same.example"
    parent = _spec(tmp_path, "P", cfg)
    child = _spec(tmp_path, "C", cfg)
    _assert_compatible_registry_config(parent, "child-wf", child)  # must not raise
