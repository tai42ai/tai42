"""Access control reasons on the SAME form the router matches for a raw-path record door.

A state record ``{key}`` legitimately carries ``/`` (a thread key is
``bridge:{route}:{quote(principal)}/{id}``), sent as one percent-encoded segment. The
record doors are raw-path-matched, so the resolver keeps the encoded slash to ONE segment
and resolves them to their protected resource — while an encoded slash on ANY OTHER route
is fenced off fail-closed (the router would decode it and serve a different path).
"""

from __future__ import annotations

import pytest

from tai42_skeleton.access_control.path_canon import MalformedPathError
from tai42_skeleton.access_control.role_gate import reset_route_index, resolve_route_meta

_RECORD = "/api/states/s/records/agent/a-42/thread/bridge:web:acme.example.com%2F+1555%2Fu-42"


@pytest.fixture(autouse=True)
def _routes_imported():
    """Bind the app and import the states router so the resolver's route index reflects the
    record doors (raw-path-matched) and the SPA catch-all."""
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app import instance

    tai42_app.bind(instance.build_app())
    import tai42_skeleton.routers.states  # noqa: F401 - registers the record doors

    reset_route_index()


def test_encoded_slash_key_resolves_to_the_record_route():
    meta = resolve_route_meta(_RECORD, "GET")
    assert meta is not None
    assert meta.name == "read_state_record"
    assert meta.raw_path_matched is True


def test_a_plain_key_resolves_to_the_same_record_route():
    meta = resolve_route_meta("/api/states/s/records/agent/a-42/thread/plainkey", "GET")
    assert meta is not None
    assert meta.name == "read_state_record"


def test_double_encoded_slash_stays_distinct_and_still_resolves():
    # ``%252F`` is a thread key whose principal itself carried ``/`` (double-encoded). It
    # is DISTINCT from a single ``%2F`` and still a legitimate record key — it resolves to
    # the record door rather than being conflated with a single encoded slash.
    single = resolve_route_meta("/api/states/s/records/agent/a-42/thread/a%2Fb", "GET")
    double = resolve_route_meta("/api/states/s/records/agent/a-42/thread/a%252Fb", "GET")
    assert single is not None
    assert single.name == "read_state_record"
    assert double is not None
    assert double.name == "read_state_record"


def test_encoded_slash_on_a_non_raw_route_is_refused_fail_closed():
    # A state NAME with an encoded slash resolves to no raw-path-matched route: the router
    # would decode ``%2F`` to a real ``/`` and route a different path, so authz must not
    # reason on this form — it raises (a fail-closed deny), never returns a route.
    with pytest.raises(MalformedPathError):
        resolve_route_meta("/api/states/a%2Fb", "GET")


def test_encoded_slash_matching_the_spa_catch_all_is_refused():
    # The greedy public SPA catch-all would otherwise swallow an encoded-slash path and
    # read it as public; it is not raw-path-matched, so it is refused fail-closed.
    with pytest.raises(MalformedPathError):
        resolve_route_meta("/api%2Fsecret", "GET")


def test_a_traversal_in_the_record_path_is_normalized_not_followed_to_the_door():
    # A ``..`` (plain or percent-encoded) is dot-resolved per decoded segment BEFORE route
    # matching, so it cannot smuggle a traversal into the record door: it pops the segment
    # ahead of it and lands on a different path, which is not the record route.
    for traversal in ("thread/..", "thread/%2E%2E"):
        meta = resolve_route_meta(f"/api/states/s/records/agent/a-42/{traversal}/plain", "GET")
        assert meta is None or meta.name != "read_state_record"
    # A ``..`` that climbs out of ``/api`` normalizes to ``/etc/passwd`` — a non-/api path
    # that lands on the public SPA shell, never re-entering the API/control-plane surface.
    climbed = resolve_route_meta("/api/../../../etc/passwd", "GET")
    assert climbed is not None
    assert climbed.name == "serve_spa"
    assert climbed.public is True


def test_a_key_whose_tail_is_a_subaction_resolves_to_the_record_door():
    # A key ending in ``writes`` addresses the record door, not the ``/writes`` sub-action.
    meta = resolve_route_meta("/api/states/s/records/agent/a-42/thread/conv%2Fwrites", "GET")
    assert meta is not None
    assert meta.name == "read_state_record"
    # …while the real ``/writes`` sub-action of the same slashed key resolves to its own door.
    writes = resolve_route_meta("/api/states/s/records/agent/a-42/thread/conv%2Fx/writes", "GET")
    assert writes is not None
    assert writes.name == "list_state_writes"
