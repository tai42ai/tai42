"""The per-identity words management doors (authed): read, replace and clear a web route's words."""

from __future__ import annotations

import pytest

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.store.words import get_words

from .conftest import IDENTITY, FakeRedis, _body, build_request

_WORDS = "/words/{identity}"

_BODY = {
    "page": {"entry_refused": {"heading": "Members only", "text": "Use the link from your invitation."}},
    "transport": {"busy": "Hold on a moment.", "too_long": "{subject} is over the limit."},
}


def _route(stub_app, method: str):
    routes = [route for route in stub_app.http.routes if route.path == _WORDS and method in route.methods]
    assert len(routes) == 1
    return routes[0]


def _request(method: str, *, identity: str = IDENTITY, json_body=None):
    return build_request(method=method, path=_WORDS, json_body=json_body, path_params={"identity": identity})


def test_words_doors_are_authed_with_the_pinned_action_class(stub_app):
    read = _route(stub_app, "GET")
    assert read.authed is None
    assert read.action == "read"
    for method in ("PUT", "DELETE"):
        route = _route(stub_app, method)
        assert route.authed is None
        assert route.action == "write"


async def test_put_then_get_round_trips_the_words(web_env, stub_app, fake_redis: FakeRedis):
    put = await _route(stub_app, "PUT").handler(_request("PUT", json_body=_BODY))
    assert put.status_code == 200
    assert _body(put)["data"] == _BODY
    got = await _route(stub_app, "GET").handler(_request("GET"))
    assert got.status_code == 200
    assert _body(got)["data"] == _BODY


async def test_put_replaces_the_whole_object(web_env, stub_app, fake_redis: FakeRedis):
    route = _route(stub_app, "PUT")
    await route.handler(_request("PUT", json_body=_BODY))
    await route.handler(_request("PUT", json_body={"transport": {"failed": "That did not go through."}}))
    got = await _route(stub_app, "GET").handler(_request("GET"))
    assert _body(got)["data"] == {"transport": {"failed": "That did not go through."}}


async def test_get_answers_404_when_no_words_are_set(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _route(stub_app, "GET").handler(_request("GET"))
    assert resp.status_code == 404
    assert _body(resp)["error"] == "no words are set for this web route"


async def test_delete_clears_the_words_and_404s_when_none(web_env, stub_app, fake_redis: FakeRedis):
    await _route(stub_app, "PUT").handler(_request("PUT", json_body=_BODY))
    route = _route(stub_app, "DELETE")
    cleared = await route.handler(_request("DELETE"))
    assert _body(cleared)["data"] == {"status": "cleared"}
    assert await get_words(IDENTITY) is None
    gone = await route.handler(_request("DELETE"))
    assert gone.status_code == 404


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"transport": {"no_such_sentence": "x"}}, "transport.no_such_sentence"),
        ({"transport": {"busy": "   "}}, "transport.busy"),
        ({"page": {"link_invalid": {"title": "x"}}}, "page.link_invalid.title"),
        ({"colour": "blue"}, "colour"),
        (["not", "an", "object"], "body"),
        ({}, "body"),
        ({"page": {}}, "page"),
        ({"transport": {}}, "transport"),
        ({"page": {"link_invalid": {}}}, "page.link_invalid"),
        ({"page": {"link_invalid": {"heading": None}}}, "page.link_invalid"),
        ({"page": {"link_invalid": {}}, "transport": {"busy": "Hold on a moment."}}, "page.link_invalid"),
    ],
)
async def test_put_outside_the_words_model_answers_422(web_env, stub_app, fake_redis: FakeRedis, body, field: str):
    resp = await _route(stub_app, "PUT").handler(_request("PUT", json_body=body))
    assert resp.status_code == 422
    assert _body(resp)["error"].startswith(f"invalid request body: {field}: ")
    assert await get_words(IDENTITY) is None


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_words_doors_answer_the_store_off_refusal(no_web_env, stub_app, method: str):
    resp = await _route(stub_app, method).handler(_request(method, json_body=_BODY if method == "PUT" else None))
    assert resp.status_code == 501
    assert _body(resp) == {
        "error": "web channel transcript store is not configured",
        "code": "web_transcript_store_off",
    }


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_words_doors_refuse_an_unusable_identity(web_env, stub_app, fake_redis: FakeRedis, method: str):
    resp = await _route(stub_app, method).handler(
        _request(method, identity="site:alpha", json_body=_BODY if method == "PUT" else None)
    )
    assert resp.status_code == 422
