"""Control-plane authentication & operation authorization (#292).

The console mutates state (start / review / cancel / project-refresh), so the API
needs an authorization boundary before it leaves a trusted operator network. The
boundary is pluggable and **open by default** — the single-binary local server
binds ``127.0.0.1`` and trusts its caller, so nothing changes until auth is
configured. Two built-in modes cover the rest:

- :class:`TokenAuthorizer` — bearer tokens, each mapped to a grant set (e.g. a
  read-only token vs an operator token). The internal-operator-console pattern.
- :class:`ProxyHeaderAuthorizer` — trusts an authenticating reverse proxy that
  passes the actor id and granted permissions in headers. The integration point
  for enterprise SSO (multi-tenant hosted).

The resolved :class:`Actor` is used **only** for authorization and for the
console capability report. It is never written to ``typeflux.*`` execution
metadata or the lifecycle audit. (Reviewer identity/notes and cancel reasons are
separately kept out of ``typeflux.*`` metadata too, but — unlike the actor — they
are sent as Temporal signal payloads and persist in workflow history; see #325.)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Permission(StrEnum):
    """An operation category the API authorizes against."""

    INSPECT = "inspect"
    START = "start"
    REVIEW = "review"
    CANCEL = "cancel"
    PROJECT_REFRESH = "project.refresh"


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

#: State-changing grants. Holding any of these is "operate-class"; bare
#: ``INSPECT`` is not. Used to gate side-effectful reads (an audited
#: ``trace=true`` status check writes a lifecycle operation to the trace)
#: behind more than read-only access (#321).
OPERATE_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.START, Permission.REVIEW, Permission.CANCEL, Permission.PROJECT_REFRESH}
)


@dataclass(frozen=True)
class Actor:
    """The authenticated caller's identity and granted permissions.

    ``id`` is opaque and used only for the capability report — never persisted
    into execution metadata.
    """

    id: str | None
    permissions: frozenset[Permission]

    def can(self, permission: Permission) -> bool:
        if permission in self.permissions:
            return True
        # Any operate grant implies the ability to inspect — you cannot operate
        # on what you cannot see, and reads are the weaker capability.
        return permission is Permission.INSPECT and bool(self.permissions)

    def can_operate(self) -> bool:
        """True when the actor holds any state-changing (operate-class) grant.

        A read-only (inspect-only) actor cannot trigger side-effectful reads
        such as an audited ``trace=true`` status check.
        """
        return bool(self.permissions & OPERATE_PERMISSIONS)


@runtime_checkable
class Authorizer(Protocol):
    def authorize(self, request: Any) -> Actor:
        """Resolve the calling :class:`Actor` from the request (headers).

        Must not raise for an unauthenticated caller — return an actor with no
        permissions so the endpoint dependency fails closed with a 403.
        """


class OpenAuthorizer:
    """Default: trust the caller (local / operator network) and grant everything."""

    def authorize(self, request: Any) -> Actor:
        return Actor(id=None, permissions=ALL_PERMISSIONS)


def _bearer_token(request: Any) -> str | None:
    # Read Authorization through the same single-occurrence guard as the proxy trust seam
    # (#738): zero occurrences → no token (unauthenticated, unchanged); MORE THAN ONE
    # occurrence → absent → no token, so a client that smuggles a second Authorization
    # header is rejected exactly like a bad token (no-permission actor → 403) instead of
    # having the first occurrence order-dependently honored. The single value is trimmed.
    header = _single_trusted_header(request.headers, "authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


class TokenAuthorizer:
    """Bearer-token auth: each token maps to a named grant set.

    Tokens are matched by exact value; an unknown or missing token yields an
    actor with no permissions (fail closed). Token *values* are never logged —
    only the configured names are identifying.
    """

    def __init__(self, tokens: Mapping[str, TokenGrant]) -> None:
        self._tokens = dict(tokens)

    def authorize(self, request: Any) -> Actor:
        token = _bearer_token(request)
        grant = self._tokens.get(token) if token is not None else None
        if grant is None:
            return Actor(id=None, permissions=frozenset())
        return Actor(id=grant.name, permissions=grant.permissions)


@dataclass(frozen=True)
class TokenGrant:
    name: str
    permissions: frozenset[Permission]


def _single_trusted_header(headers: Any, name: str) -> str | None:
    """The single trusted occurrence of header ``name``, or ``None``.

    Guards every header that is only ever meaningful once at this trust boundary —
    the proxy actor/permissions headers AND ``Authorization`` (via ``_bearer_token``).
    Fail-closed hardening of the proxy trust seam (#577): behind a
    mis-configured proxy that APPENDS its header instead of replacing the
    client's, a client-smuggled copy coexists with the proxy's — every
    occurrence is then attacker-influenced, so DUPLICATE occurrences read as
    absent (never "first wins" or a joined union). The value is trimmed, and a
    whitespace-only value reads as absent too (a bare-space header must not
    become a literal identity). ``headers`` without ``getlist`` (a plain
    mapping, as in tests) cannot carry duplicates, so its single value is used.
    """
    getlist = getattr(headers, "getlist", None)
    if getlist is not None:
        values = getlist(name)
    else:
        value = headers.get(name)
        values = [] if value is None else [value]
    if len(values) != 1:
        return None
    single = values[0].strip()
    return single if single else None


class ProxyHeaderAuthorizer:
    """Trust an authenticating reverse proxy that injects the actor + grants.

    Only use behind a proxy that strips these headers from client requests and
    sets them itself. Enabled explicitly (``--trust-proxy-auth``) so the headers
    are never honored by accident on a directly-exposed server. The
    single-occurrence rule is ENFORCED, not assumed (#577): a duplicated actor
    or permissions header — the signature of a proxy that appends rather than
    replaces, letting a client-smuggled copy through — reads as absent
    (identity ``None`` / no grants), and values are trimmed with
    whitespace-only reading as absent.
    """

    def __init__(
        self,
        *,
        actor_header: str = "x-typeflux-actor",
        permissions_header: str = "x-typeflux-permissions",
    ) -> None:
        self._actor_header = actor_header
        self._permissions_header = permissions_header

    def authorize(self, request: Any) -> Actor:
        actor_id = _single_trusted_header(request.headers, self._actor_header)
        raw = _single_trusted_header(request.headers, self._permissions_header) or ""
        return Actor(id=actor_id, permissions=parse_permissions(raw))


def parse_permissions(value: str, *, strict: bool = False) -> frozenset[Permission]:
    """Parse permissions from a comma/space-separated string.

    ``"*"`` (or ``"all"``) expands to every permission. Unknown names raise when
    ``strict`` (token config), otherwise they are ignored (a tolerant proxy may
    forward roles Typeflux does not model).
    """
    tokens = [item for item in value.replace(",", " ").split() if item]
    permissions: set[Permission] = set()
    for token in tokens:
        normalized = token.strip().lower()
        if normalized in {"*", "all"}:
            return ALL_PERMISSIONS
        try:
            permissions.add(Permission(normalized))
        except ValueError:
            if strict:
                raise ValueError(f"unknown permission: {token!r}") from None
    return frozenset(permissions)


def parse_token_spec(spec: str) -> tuple[str, TokenGrant]:
    """Parse a ``NAME:PERMS:TOKEN`` token spec into ``(token, grant)``.

    ``PERMS`` is a comma-separated permission list (or ``*``); ``TOKEN`` is the
    bearer value and is the only field allowed to contain ``:`` (it is the
    remainder). Unknown permissions raise (config is fail-closed).
    """
    parts = spec.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"token spec must be NAME:PERMS:TOKEN, got {spec!r}")
    name, perms, token = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not name or not token:
        raise ValueError(f"token spec must be NAME:PERMS:TOKEN, got {spec!r}")
    permissions = parse_permissions(perms, strict=True)
    if not permissions:
        raise ValueError(f"token spec {name!r} grants no permissions")
    return token, TokenGrant(name=name, permissions=permissions)


def build_authorizer(
    token_specs: Iterable[str] = (),
    *,
    trust_proxy: bool = False,
) -> Authorizer | None:
    """Build the configured authorizer, or ``None`` for the open default.

    Proxy and token modes are mutually exclusive — a deployment authenticates
    either at the proxy or with built-in tokens, not both.
    """
    specs = [spec for spec in token_specs if spec.strip()]
    if trust_proxy and specs:
        raise ValueError("--trust-proxy-auth cannot be combined with --auth-token")
    if trust_proxy:
        return ProxyHeaderAuthorizer()
    if not specs:
        return None
    grants: dict[str, TokenGrant] = {}
    for spec in specs:
        token, grant = parse_token_spec(spec)
        if token in grants:
            # A reused token value would silently inherit the last spec's grants
            # — a privilege-escalation footgun. Fail at config time instead.
            raise ValueError(
                f"token for {grant.name!r} is already configured for "
                f"{grants[token].name!r}; each token value must be unique"
            )
        grants[token] = grant
    return TokenAuthorizer(grants)


__all__ = [
    "ALL_PERMISSIONS",
    "OPERATE_PERMISSIONS",
    "Actor",
    "Authorizer",
    "OpenAuthorizer",
    "Permission",
    "ProxyHeaderAuthorizer",
    "TokenAuthorizer",
    "TokenGrant",
    "build_authorizer",
    "parse_permissions",
    "parse_token_spec",
]
