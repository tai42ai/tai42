"""Every public web door declares its flood-control family, budget, error statuses and success media types.

The family is a declaration, not the path stem, so a remapped mount base keeps the budget.
"""

from __future__ import annotations

import pytest
from tai42_contract.app import RateLimitBudget

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.routes.limits import CHAT_RATE_LIMIT_BUDGET, CHAT_RATE_LIMIT_FAMILY

from .conftest import _StubApp

_CHAT_DOORS = {
    ("GET", "/chat/{identity}"): (False, (400, 403, 500, 501), {"GET": ("text/html",)}),
    ("GET", "/assets/{file}"): (False, (404, 500), {"GET": ("application/octet-stream",)}),
    ("GET", "/stream"): (False, (400, 401, 501, 503), {"GET": ("text/event-stream",)}),
    ("POST", "/session/rotate"): (True, (400, 403, 413, 422, 501), None),
    ("POST", "/messages"): (True, (400, 401, 403, 404, 409, 413, 422, 501, 503), None),
    ("POST", "/forms/{token}"): (True, (400, 401, 403, 404, 413, 422, 501, 503), None),
    ("POST", "/questions/{interaction_id}/answer"): (True, (400, 401, 403, 404, 409, 413, 422, 500, 501), None),
    ("POST", "/questions/{interaction_id}/react"): (
        True,
        (400, 401, 403, 404, 409, 413, 422, 500, 501, 502),
        None,
    ),
}


def _routes(stub_app: _StubApp) -> dict[tuple[str, str], object]:
    return {(method, route.path): route for route in stub_app.http.routes for method in route.methods}


def test_the_chat_budget_is_120_and_30() -> None:
    assert CHAT_RATE_LIMIT_FAMILY == "channels_web"
    assert RateLimitBudget(limit=120, burst=30) == CHAT_RATE_LIMIT_BUDGET


@pytest.mark.parametrize("door", sorted(_CHAT_DOORS))
def test_every_chat_door_declares_the_chat_family_and_its_true_metadata(stub_app: _StubApp, door) -> None:
    route = _routes(stub_app)[door]
    reads_body, statuses, media = _CHAT_DOORS[door]
    declared = route.declared  # type: ignore[attr-defined]
    assert declared is not None
    # The family is declared, so a remapped mount (``channels_<other>`` by path) keeps it.
    assert declared.rate_limit_family == CHAT_RATE_LIMIT_FAMILY
    assert declared.rate_limit_budget == CHAT_RATE_LIMIT_BUDGET
    assert declared.reload_gated is False
    assert declared.reads_body is reads_body
    assert declared.error_statuses == statuses
    assert declared.success_status == 200
    assert route.success_media_types == media  # type: ignore[attr-defined]


def test_the_upload_door_declares_its_own_family(stub_app: _StubApp) -> None:
    declared = _routes(stub_app)[("POST", "/uploads")].declared  # type: ignore[attr-defined]
    assert declared.rate_limit_family == "channels_web_uploads"
    assert declared.rate_limit_budget == RateLimitBudget(limit=20, burst=5)


def test_every_public_door_declares_a_family_and_a_budget(stub_app: _StubApp) -> None:
    # The stub records ``authed`` only when a route passes it; a declared plugin route takes
    # its publicness from tai-plugin.yml, so the public set is every door that declares no
    # action-class (the gate and words management doors each declare one).
    public = [route for route in stub_app.http.routes if route.action is None]
    assert {route.path for route in public} >= {path for _method, path in _CHAT_DOORS} | {"/uploads"}
    for route in public:
        assert route.declared is not None, route.path
        assert route.declared.rate_limit_family is not None, route.path
        assert route.declared.rate_limit_budget is not None, route.path
