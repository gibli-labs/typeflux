from __future__ import annotations

import pytest

from typeflux import TypefluxError
from typeflux.core.errors import TypefluxError as CoreTypefluxError
from typeflux.execution.executor import AIActivityOutputValidationError
from typeflux.execution.preflight import PreflightError
from typeflux.metadata import MetadataConflictError
from typeflux.project import (
    ProjectDeploymentError,
    ProjectEnvironmentError,
    ProjectPolicyEnforcementError,
    ProjectPolicyError,
)
from typeflux.prompts.errors import PromptResolutionError
from typeflux.providers.errors import ProviderError

_DOMAIN_ERRORS = (
    AIActivityOutputValidationError,
    MetadataConflictError,
    PreflightError,
    ProjectDeploymentError,
    ProjectEnvironmentError,
    ProjectPolicyEnforcementError,
    ProjectPolicyError,
    PromptResolutionError,
    ProviderError,
)


def test_root_export_is_the_core_class() -> None:
    assert TypefluxError is CoreTypefluxError


@pytest.mark.parametrize("error_cls", _DOMAIN_ERRORS, ids=lambda cls: cls.__name__)
def test_domain_errors_derive_from_typeflux_root(error_cls: type[BaseException]) -> None:
    assert issubclass(error_cls, TypefluxError)


@pytest.mark.parametrize(
    ("error_cls", "legacy_base"),
    (
        (MetadataConflictError, ValueError),
        (ProjectDeploymentError, ValueError),
        (ProjectEnvironmentError, ValueError),
        (ProjectPolicyEnforcementError, ValueError),
        (ProjectPolicyError, ValueError),
        (PreflightError, RuntimeError),
    ),
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_legacy_bases_are_preserved(
    error_cls: type[BaseException], legacy_base: type[BaseException]
) -> None:
    # Reparenting is additive: every pre-existing `except ValueError` /
    # `except RuntimeError` handler must keep catching these.
    assert issubclass(error_cls, legacy_base)


def test_one_handler_catches_any_typeflux_failure() -> None:
    with pytest.raises(TypefluxError):
        raise ProjectPolicyError("conflicting policy")
    with pytest.raises(TypefluxError):
        raise ProviderError(reason="provider exploded", provider="fake")
