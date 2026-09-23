"""Typeflux exception root.

Every Typeflux-raised domain error derives from :class:`TypefluxError`, so
callers can catch all Typeflux failures with one handler while still
narrowing to domain roots (``ProviderError``, ``PromptResolutionError``,
``ProjectPolicyError``, …). Domain errors that historically subclassed
``ValueError`` or ``RuntimeError`` keep those bases via multiple
inheritance, so existing ``except ValueError`` handlers continue to catch
them — the root is purely additive.
"""

from __future__ import annotations


class TypefluxError(Exception):
    """Base class for all Typeflux domain errors."""


class LifecycleBindingError(TypefluxError):
    """A lifecycle operation targeted an execution that is not the bound one.

    Raised before any query or signal is dispatched when the execution at the
    addressed id is not the workflow type/project this runtime routes — an id
    collision, a typo, or a cross-project/hijack attempt (#320). Failing closed
    here means the operation never reaches a foreign execution.

    Deliberately **not** a :class:`ValueError`: it is a routing/identity
    conflict (HTTP 409), not a malformed request (422).
    """


__all__ = ["LifecycleBindingError", "TypefluxError"]
