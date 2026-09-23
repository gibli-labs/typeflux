from __future__ import annotations

import pytest

from typeflux.controlplane.auth import (
    ALL_PERMISSIONS,
    Actor,
    OpenAuthorizer,
    Permission,
    ProxyHeaderAuthorizer,
    TokenAuthorizer,
    TokenGrant,
    build_authorizer,
    parse_permissions,
    parse_token_spec,
)


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class _MultiHeaders:
    """A Starlette-Headers-shaped multidict: ``get`` + ``getlist`` over raw pairs."""

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def get(self, name: str) -> str | None:
        for key, value in self._items:
            if key == name:
                return value
        return None

    def getlist(self, name: str) -> list[str]:
        return [value for key, value in self._items if key == name]


class _FakeMultiRequest:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self.headers = _MultiHeaders(items)


def test_parse_permissions_csv_and_wildcard() -> None:
    assert parse_permissions("inspect, start") == frozenset({Permission.INSPECT, Permission.START})
    assert parse_permissions("*") == ALL_PERMISSIONS
    assert parse_permissions("all") == ALL_PERMISSIONS


def test_parse_permissions_unknown_tolerant_vs_strict() -> None:
    assert parse_permissions("inspect bogus") == frozenset({Permission.INSPECT})
    with pytest.raises(ValueError, match="unknown permission"):
        parse_permissions("bogus", strict=True)


def test_open_authorizer_grants_everything() -> None:
    actor = OpenAuthorizer().authorize(_FakeRequest())
    assert actor.permissions == ALL_PERMISSIONS


def test_token_authorizer_maps_token_to_grant_and_fails_closed() -> None:
    authorizer = TokenAuthorizer(
        {"tok": TokenGrant(name="reader", permissions=frozenset({Permission.INSPECT}))}
    )

    granted = authorizer.authorize(_FakeRequest({"authorization": "Bearer tok"}))
    assert granted.id == "reader"
    assert granted.permissions == frozenset({Permission.INSPECT})

    # Unknown token, missing header, and wrong scheme all yield no permissions.
    assert (
        authorizer.authorize(_FakeRequest({"authorization": "Bearer nope"})).permissions
        == frozenset()
    )
    assert authorizer.authorize(_FakeRequest()).permissions == frozenset()
    assert (
        authorizer.authorize(_FakeRequest({"authorization": "Basic tok"})).permissions
        == frozenset()
    )


def test_token_authorizer_fails_closed_on_duplicate_authorization_header() -> None:
    # A duplicated Authorization header is the signature of a client smuggling a second
    # credential — every occurrence is attacker-influenced, so it reads as absent (no token →
    # no permissions), never "first wins". Same fail-closed outcome as a bad token; the route
    # gate then 403s. NOTE: a genuine WIRE duplicate can't be expressed in fixture JSON (object
    # shape), so this multidict test — not a conformance fixture — is the parity pin (follow-up
    # to #738; TS mirror: "fails closed on WIRE-duplicated Authorization ..." in
    # test/http-adapter.test.ts).
    authorizer = TokenAuthorizer(
        {"tok": TokenGrant(name="reader", permissions=frozenset({Permission.INSPECT}))}
    )
    # Two valid tokens.
    two_valid = authorizer.authorize(
        _FakeMultiRequest([("authorization", "Bearer tok"), ("authorization", "Bearer tok")])
    )
    assert two_valid.permissions == frozenset()
    # Valid + garbage.
    valid_plus_garbage = authorizer.authorize(
        _FakeMultiRequest([("authorization", "Bearer tok"), ("authorization", "Bearer nope")])
    )
    assert valid_plus_garbage.permissions == frozenset()
    # A single valid occurrence is unchanged — the duplicate guard only bites on >1.
    single = authorizer.authorize(_FakeMultiRequest([("authorization", "Bearer tok")]))
    assert single.id == "reader"
    assert single.permissions == frozenset({Permission.INSPECT})


def test_proxy_header_authorizer_reads_actor_and_permissions() -> None:
    authorizer = ProxyHeaderAuthorizer()
    actor = authorizer.authorize(
        _FakeRequest({"x-typeflux-actor": "alice", "x-typeflux-permissions": "inspect,cancel"})
    )
    assert actor.id == "alice"
    assert actor.permissions == frozenset({Permission.INSPECT, Permission.CANCEL})
    # No header → no grants (fail closed).
    assert authorizer.authorize(_FakeRequest()).permissions == frozenset()


def test_proxy_header_authorizer_fails_closed_on_duplicate_actor_header() -> None:
    # A duplicated actor header is the signature of a proxy that APPENDS instead of
    # replacing — a client-smuggled copy coexists with the proxy's, so no occurrence is
    # trustworthy: identity reads as absent, never "first wins" (#577).
    authorizer = ProxyHeaderAuthorizer()
    actor = authorizer.authorize(
        _FakeMultiRequest(
            [
                ("x-typeflux-actor", "mallory"),
                ("x-typeflux-actor", "alice"),
                ("x-typeflux-permissions", "inspect"),
            ]
        )
    )
    assert actor.id is None
    assert actor.permissions == frozenset({Permission.INSPECT})


def test_proxy_header_authorizer_fails_closed_on_duplicate_permissions_header() -> None:
    # Same proxy-append misconfig on the permissions header: a duplicated occurrence would
    # otherwise be order-dependent (first wins) or a union — both attacker-influenced. It
    # reads as ABSENT: no grants, fail closed (#577).
    authorizer = ProxyHeaderAuthorizer()
    actor = authorizer.authorize(
        _FakeMultiRequest(
            [
                ("x-typeflux-actor", "alice"),
                ("x-typeflux-permissions", "inspect"),
                ("x-typeflux-permissions", "*"),
            ]
        )
    )
    assert actor.permissions == frozenset()
    # The single actor header is still read — identity and grants fail independently.
    assert actor.id == "alice"


def test_proxy_header_authorizer_trims_and_maps_blank_actor_to_none() -> None:
    authorizer = ProxyHeaderAuthorizer()
    # Whitespace-only never becomes a literal identity (#577).
    blank = authorizer.authorize(
        _FakeRequest({"x-typeflux-actor": "   ", "x-typeflux-permissions": "inspect"})
    )
    assert blank.id is None
    # A padded value is trimmed to the principal.
    padded = authorizer.authorize(
        _FakeRequest({"x-typeflux-actor": "  alice  ", "x-typeflux-permissions": "inspect"})
    )
    assert padded.id == "alice"
    # Empty string stays None (the truthiness parity pin).
    empty = authorizer.authorize(
        _FakeRequest({"x-typeflux-actor": "", "x-typeflux-permissions": "inspect"})
    )
    assert empty.id is None


def test_proxy_header_authorizer_single_occurrence_via_getlist_still_reads() -> None:
    # The multidict path (real Starlette headers) with exactly one occurrence behaves
    # exactly like the plain-mapping path — the duplicate guard only bites on >1.
    authorizer = ProxyHeaderAuthorizer()
    actor = authorizer.authorize(
        _FakeMultiRequest(
            [("x-typeflux-actor", "alice"), ("x-typeflux-permissions", "inspect,cancel")]
        )
    )
    assert actor.id == "alice"
    assert actor.permissions == frozenset({Permission.INSPECT, Permission.CANCEL})


def test_parse_token_spec_roundtrip_and_validation() -> None:
    token, grant = parse_token_spec("operator:start,review:tok-abc:123")
    assert token == "tok-abc:123"  # the token keeps colons after the second one
    assert grant.name == "operator"
    assert grant.permissions == frozenset({Permission.START, Permission.REVIEW})

    for bad in (
        "noperms",  # no colons
        ":inspect:token",  # empty name
        "name:inspect:",  # empty token
        "name::token",  # empty perms → grants nothing
        "name:inspect:   ",  # whitespace-only token
    ):
        with pytest.raises(ValueError):
            parse_token_spec(bad)


def test_build_authorizer_modes() -> None:
    assert build_authorizer() is None  # open default
    assert build_authorizer(["r:inspect:tok"]).__class__ is TokenAuthorizer
    assert build_authorizer(trust_proxy=True).__class__ is ProxyHeaderAuthorizer
    with pytest.raises(ValueError, match="cannot be combined"):
        build_authorizer(["r:inspect:tok"], trust_proxy=True)


def test_build_authorizer_rejects_duplicate_token_values() -> None:
    # A reused token value would silently inherit the last spec's grants.
    with pytest.raises(ValueError, match="must be unique"):
        build_authorizer(["reader:inspect:SAME", "admin:*:SAME"])


def test_operate_permission_implies_inspect() -> None:
    starter = Actor(id="s", permissions=frozenset({Permission.START}))
    assert starter.can(Permission.INSPECT) is True  # implied
    assert starter.can(Permission.START) is True
    assert starter.can(Permission.CANCEL) is False  # not the other way
    # An empty grant set implies nothing.
    assert Actor(id=None, permissions=frozenset()).can(Permission.INSPECT) is False


def test_can_operate_distinguishes_inspect_only_from_operate_class() -> None:
    # Bare inspect is not operate-class — it cannot trigger audited writes.
    assert Actor(id="r", permissions=frozenset({Permission.INSPECT})).can_operate() is False
    assert Actor(id=None, permissions=frozenset()).can_operate() is False
    # Any state-changing grant is operate-class.
    for grant in (
        Permission.START,
        Permission.REVIEW,
        Permission.CANCEL,
        Permission.PROJECT_REFRESH,
    ):
        assert Actor(id="o", permissions=frozenset({grant})).can_operate() is True


def test_api_capabilities_for_actor() -> None:
    from typeflux.controlplane.api import ApiCapabilities

    operator = ApiCapabilities.for_actor(Actor(id="op", permissions=ALL_PERMISSIONS))
    assert (operator.can_start, operator.can_refresh_project) == (True, True)

    reader = ApiCapabilities.for_actor(Actor(id="r", permissions=frozenset({Permission.INSPECT})))
    assert (reader.can_start, reader.can_review, reader.can_cancel, reader.can_refresh_project) == (
        False,
        False,
        False,
        False,
    )
