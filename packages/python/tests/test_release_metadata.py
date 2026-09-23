from __future__ import annotations

import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

from typeflux import __version__

ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "typeflux"
EXPECTED_LICENSE = "Apache-2.0"


def _project_metadata() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]


def _metadata_value(metadata_text: str, name: str) -> str | None:
    return Parser().parsestr(metadata_text)[name]


def _metadata_values(metadata_text: str, name: str) -> list[str]:
    return Parser().parsestr(metadata_text).get_all(name, [])


def _requirements_for_extra(metadata_text: str, extra: str) -> list[str]:
    marker_single = f"extra == '{extra}'"
    marker_double = f'extra == "{extra}"'
    return [
        requirement
        for requirement in _metadata_values(metadata_text, "Requires-Dist")
        if marker_single in requirement or marker_double in requirement
    ]


def test_project_license_and_version_metadata_are_declared() -> None:
    project = _project_metadata()

    assert project["license"] == EXPECTED_LICENSE
    assert project["license-files"] == ["LICENSE"]
    assert (
        (ROOT / "LICENSE")
        .read_text(encoding="utf-8")
        .startswith("                                 Apache License")
    )
    assert __version__ == project["version"]


def test_optional_extras_are_split_and_live_stays_aggregate() -> None:
    extras = _project_metadata()["optional-dependencies"]

    assert extras["anthropic"] == ["anthropic>=0.77,<1"]
    assert extras["api"] == ["fastapi>=0.115,<1", "uvicorn>=0.30,<1", "jsonschema>=4,<5"]
    assert extras["gemini"] == ["google-genai>=1,<2"]
    assert extras["langfuse"] == ["langfuse>=4,<5"]
    assert extras["langsmith"] == ["langsmith[otel]>=0.8.18,<1"]
    assert extras["openai"] == ["instructor>=1.15,<2", "openai>=2,<3"]
    assert sorted(extras["live"]) == sorted(
        extras["anthropic"]
        + extras["gemini"]
        + extras["langfuse"]
        + extras["langsmith"]
        + extras["openai"]
    )


def test_built_distributions_include_license_version_and_extras(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--sdist",
            "--outdir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
    )

    wheel_path = next(tmp_path.glob("*.whl"))
    sdist_path = next(tmp_path.glob("*.tar.gz"))

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = wheel.namelist()
        metadata_name = next(name for name in wheel_names if name.endswith(".dist-info/METADATA"))
        metadata_text = wheel.read(metadata_name).decode("utf-8")

    assert _metadata_value(metadata_text, "Name") == PROJECT_NAME
    assert _metadata_value(metadata_text, "Version") == __version__
    assert _metadata_value(metadata_text, "License-Expression") == EXPECTED_LICENSE
    assert "LICENSE" in _metadata_values(metadata_text, "License-File")
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)

    assert sorted(_metadata_values(metadata_text, "Provides-Extra")) == [
        "anthropic",
        "api",
        "gemini",
        "langfuse",
        "langsmith",
        "live",
        "openai",
    ]
    assert any(
        requirement.startswith("fastapi")
        for requirement in _requirements_for_extra(metadata_text, "api")
    )
    assert any(
        requirement.startswith("uvicorn")
        for requirement in _requirements_for_extra(metadata_text, "api")
    )
    assert any(
        requirement.startswith("openai")
        for requirement in _requirements_for_extra(metadata_text, "openai")
    )
    assert any(
        requirement.startswith("instructor")
        for requirement in _requirements_for_extra(metadata_text, "openai")
    )
    assert any(
        requirement.startswith("anthropic")
        for requirement in _requirements_for_extra(metadata_text, "anthropic")
    )
    assert any(
        requirement.startswith("langfuse")
        for requirement in _requirements_for_extra(metadata_text, "langfuse")
    )
    for dependency in (
        "anthropic",
        "google-genai",
        "instructor",
        "langfuse",
        "langsmith",
        "openai",
    ):
        assert any(
            requirement.startswith(dependency)
            for requirement in _requirements_for_extra(metadata_text, "live")
        )

    with tarfile.open(sdist_path) as sdist:
        assert any(name.endswith("/LICENSE") for name in sdist.getnames())
