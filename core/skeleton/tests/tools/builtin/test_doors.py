"""The builtin conversation-door tools, over the REAL doors and the REAL tool-edge authorization.

Each tool applies the SAME gate the HTTP door applies (``authz.check`` over the door route's
registered template) and then calls the real in-process door under the run's execution identity.
The authorization tests bind a real execution identity and assert the verdict — a scope miss, a
narrowed owner, a failing jq condition, or no bound identity — matches the HTTP door's; the
receipt/principal tests drive the real doors over the conversation suite's fakes and read the
accountable principal off the persisted record.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_contract.app import tai42_app
from tai42_contract.conversations import ConversationEvent
from tai42_identity_redis import redis_api_key_provider as provider_module
from tai42_kit.utils.data.string_util import hash_api_key

import tai42_skeleton.conversations as conversations_package
from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import role_grants as role_grants_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.role_gate import reset_route_index
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.authz.check import reset_tool_edge_verifier
from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn.errors import UnauthenticatedApiCallerError
from tai42_skeleton.operations.errors import PermissionDeniedError
from tai42_skeleton.tools.builtin import doors

from ...access_control.conftest import (
    FakeAccessControlPg,
    make_pg_ctx,
)
from ...access_control.conftest import FakeRedis as ACFakeRedis
from ...access_control.conftest import (
    _FakeApp as ACFakeApp,
)
from ...access_control.conftest import (
    make_client_ctx as ac_make_client_ctx,
)
from ...conversations.conftest import (
    EchoAgent,
    FakeManager,
    _accepting_callback,
    _all_record_ids,
    _api_route,
    _connected,
    _settle,
    _store,
    _tool_api_route,
    _wire,
    _wire_tool,
    delivery_module,
    env,  # noqa: F401  (re-exported fixture)
)

# alru caches are held across the several loops these tests open — a benign loop-reset artifact.
pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")


@pytest.fixture
def door_routes_registered():
    """Register the conversation door routes on the route registry, then rebuild the resolver index.

    The tools pin their authorization to the door route's registered template, so the route must
    be recorded (its module registers it at import, with the real app bound exactly as boot does)
    and the resolver index rebuilt against it.
    """
    from tai42_skeleton.app.instance import app as skeleton_app

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import conversations  # noqa: F401

    reset_route_index()
    yield
    reset_route_index()


@pytest.fixture
def ac(monkeypatch) -> FakeAccessControlPg:
    """Wire the access-control fakes the tool-edge check reads through and return the fake PG to seed.

    Concrete route rows map the synthesized door path to a scope; a policy row carries the run
    key's scopes and its per-mint fingerprint.
    """
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    pg = FakeAccessControlPg()
    redis = ACFakeRedis()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(verifier_module, "client_ctx", ac_make_client_ctx(redis))
    monkeypatch.setattr(policy_module, "client_ctx", ac_make_client_ctx(redis))
    role_grants_module.reset_role_grants_cache()
    reset_tool_edge_verifier()
    return pg


@pytest.fixture
def bound_app():
    """Bind a fake app the tool-edge check renders a policy condition through."""
    app = ACFakeApp()
    with tai42_app.bound(app):
        yield app


def _seed_key(pg: FakeAccessControlPg, user_id: str, scopes: list[str], **policy_fields) -> None:
    policy_data = {KEY_FINGERPRINT_CLAIM: f"fp-{user_id}"}
    policy_data.update(policy_fields.pop("policy_data", {}))
    pg.add_policy(user_id, scopes=scopes, policy_data=policy_data, **policy_fields)


# -- the message door tool ----------------------------------------------------


async def test_message_tool_posts_to_an_authorized_route_as_the_run_identity(
    env,  # noqa: F811
    ac,
    bound_app,
    door_routes_registered,
    monkeypatch,
):
    _wire(monkeypatch, FakeManager(_api_route()))
    env.seed_route("chat")
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    ac.add_route("/api/conversations/chat/messages", "conv-x")
    _seed_key(ac, "runner", ["conv-x"])

    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        result = await doors.send_conversation_message("chat", "u-7", "hello")
    await _settle()

    # The receipt is the accepted id pair, nothing else.
    assert set(result) == {"message_id", "thread_id"}
    # The accountable principal persisted on the record is the RUN's execution identity.
    record = await _store().get_record(result["message_id"])
    assert record is not None
    assert record.caller_principal == "runner"


async def test_message_tool_is_refused_on_a_route_the_run_lacks_scope_for(
    env,  # noqa: F811
    ac,
    bound_app,
    door_routes_registered,
    monkeypatch,
):
    _wire(monkeypatch, FakeManager(_api_route("other")))
    env.seed_route("other")
    ac.add_route("/api/conversations/other/messages", "conv-y")
    _seed_key(ac, "runner", ["conv-x"])

    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        with pytest.raises(PermissionDeniedError):
            await doors.send_conversation_message("other", "u-7", "hello")

    # The refusal is the gate's, before the door — nothing was submitted.
    assert await _all_record_ids(_store()) == []


async def test_a_star_identity_posts_anywhere(
    env,  # noqa: F811
    ac,
    bound_app,
    door_routes_registered,
    monkeypatch,
):
    _wire(monkeypatch, FakeManager(_api_route()))
    env.seed_route("chat")
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    # No route row scopes the door, yet a ``*`` identity still passes the scope test.
    ac.add_route("/api/conversations/chat/messages", "conv-x")
    _seed_key(ac, "super", ["*"])

    async with bind_execution_identity("super", bound_fingerprint="fp-super"):
        result = await doors.send_conversation_message("chat", "u-7", "hi")
    await _settle()
    assert set(result) == {"message_id", "thread_id"}


async def test_an_owned_key_whose_owners_scopes_were_narrowed_is_refused(
    env,  # noqa: F811
    ac,
    bound_app,
    door_routes_registered,
    monkeypatch,
):
    ac.add_route("/api/conversations/chat/messages", "conv-x")
    # The key holds the door's scope; its owner holds it too — until narrowed.
    _seed_key(ac, "owned", ["conv-x"], policy_data={OWNER_USER_ID_CLAIM: "owner1"})
    ac.add_policy("owner1", scopes=["conv-x"], policy_data={KEY_FINGERPRINT_CLAIM: "fp-owner1"})

    async with bind_execution_identity("owned", bound_fingerprint="fp-owned"):
        # Before the narrowing: the effective (owner-attenuated) scope still covers the door.
        _wire(monkeypatch, FakeManager(_api_route()))
        env.seed_route("chat")
        monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
        monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
        result = await doors.send_conversation_message("chat", "u-7", "hi")
        assert set(result) == {"message_id", "thread_id"}
        await _settle()

        # Narrow the OWNER off the door's scope; the very next dispatch re-derives the
        # attenuation live and is refused — exactly where the HTTP door refuses it.
        owner = ac.policy("owner1")
        owner["scopes"] = ["unrelated"]
        with pytest.raises(PermissionDeniedError):
            await doors.send_conversation_message("chat", "u-7", "hi again")


async def test_a_failing_jq_condition_refuses_the_run(ac, bound_app, door_routes_registered):
    ac.add_route("/api/conversations/chat/messages", "conv-x")
    # A condition the run cannot satisfy (its subject is not "nobody").
    _seed_key(ac, "runner", ["conv-x"], condition={"content": '.sub == "nobody"'})

    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        with pytest.raises(PermissionDeniedError):
            await doors.send_conversation_message("chat", "u-7", "hi")


async def test_no_bound_identity_is_refused_by_name(door_routes_registered):
    # No execution identity is bound and the gate is on: the tool refuses before the check,
    # since the check has no identity to gate on.
    with pytest.raises(UnauthenticatedApiCallerError):
        await doors.send_conversation_message("chat", "u-7", "hi")


# -- the event door tool ------------------------------------------------------


async def test_event_tool_addresses_an_existing_thread_by_id_as_the_run_identity(
    env,  # noqa: F811
    ac,
    bound_app,
    door_routes_registered,
    monkeypatch,
):
    _wire(monkeypatch, FakeManager(_tool_api_route()))
    env.seed_route("tool-api")
    monkeypatch.setattr(delivery_module, "_post_callback", _accepting_callback())
    _wire_tool(monkeypatch, lambda kw: "seed")
    seed = await turn_module.submit_api_message(
        "tool-api", "u-7", "hi", "alice", wait_seconds=0, client_connected=_connected
    )
    await _settle()

    ac.add_route("/api/conversations/tool-api/events", "conv-x")
    _seed_key(ac, "runner", ["conv-x"])
    _wire_tool(monkeypatch, lambda kw: "handled")
    event = ConversationEvent(event_id="evt-1", kind="provider.update")

    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        result = await doors.send_conversation_event("tool-api", event, thread_id=seed.thread_id)
    await _settle()

    assert set(result) == {"message_id", "thread_id"}
    assert result["thread_id"] == seed.thread_id
    # The event records the RUN's execution identity as its accountable authorizer.
    record = await _store().get_record(result["message_id"])
    assert record is not None
    assert record.submitted_by == "runner"


async def test_event_tool_is_refused_on_a_route_the_run_lacks_scope_for(ac, bound_app, door_routes_registered):
    ac.add_route("/api/conversations/tool-api/events", "conv-y")
    _seed_key(ac, "runner", ["conv-x"])
    event = ConversationEvent(event_id="evt-1", kind="provider.update")

    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        with pytest.raises(PermissionDeniedError):
            await doors.send_conversation_event("tool-api", event, thread_id="bridge:tool-api:x")


# -- HTTP door and tool: one identity, one verdict ----------------------------


class _Recorder:
    """Stands in for the message engine so the DECISION is what both surfaces are tested on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(
        self,
        route_name,
        external_user_id,
        text,
        caller_principal,
        wait_seconds,
        params=None,
        form=None,
        attachments=None,
        location=None,
        locale=None,
        *,
        client_connected,
    ):
        from tai42_skeleton.conversations.turn import ApiSubmitResult

        self.calls.append((route_name, external_user_id, caller_principal))
        return ApiSubmitResult(message_id="m-1", thread_id=f"bridge:{route_name}:{external_user_id}", answer=None)


async def test_the_http_door_and_the_tool_reach_the_same_verdict(ac, bound_app, door_routes_registered, monkeypatch):
    # One access-control world seeds both surfaces: the same key/principal, one route it may
    # write and one it may not.
    ac_settings = AccessControlSettings()
    redis = ACFakeRedis(
        hashes={
            f"{ac_settings.key_prefix}{hash_api_key('run-key')}": {
                "user_id": "runner",
                "description": "d",
                "owner_user_id": "owner1",
            }
        }
    )
    monkeypatch.setattr(verifier_module, "client_ctx", ac_make_client_ctx(redis))
    monkeypatch.setattr(policy_module, "client_ctx", ac_make_client_ctx(redis))
    monkeypatch.setattr(provider_module, "client_ctx", ac_make_client_ctx(redis))
    ac.add_route("/api/conversations/chat/messages", "conv-x")
    ac.add_route("/api/conversations/locked/messages", "conv-y")
    _seed_key(ac, "runner", ["conv-x"])
    ac.add_policy("owner1", scopes=["*"], policy_data={KEY_FINGERPRINT_CLAIM: "fp-owner1"})

    recorder = _Recorder()
    monkeypatch.setattr(conversations_package, "submit_api_message", recorder)

    door_handler = _message_door_handler()
    routes = [Route("/api/conversations/{route_name}/messages", door_handler, methods=["POST"])]
    client = TestClient(Starlette(routes=routes, middleware=AuthAdapter(ac_settings).get_middleware()))

    def _http(route_name: str):
        return client.post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": "u-7", "text": "hi"},
            headers={"X-API-Key": "run-key"},
        )

    # The AUTHORIZED route: both surfaces admit and reach the (stubbed) engine as the SAME principal.
    assert _http("chat").status_code == 202
    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        await doors.send_conversation_message("chat", "u-7", "hi")
    assert ("chat", "u-7", "runner") in recorder.calls
    admitted = len(recorder.calls)

    # The FORBIDDEN route: the HTTP door answers 403 and the tool raises — neither reaches the engine.
    assert _http("locked").status_code == 403
    async with bind_execution_identity("runner", bound_fingerprint="fp-runner"):
        with pytest.raises(PermissionDeniedError):
            await doors.send_conversation_message("locked", "u-7", "hi")
    assert len(recorder.calls) == admitted


def _message_door_handler():
    """The message door handler itself, imported with the real app bound as a boot does."""
    from tai42_skeleton.app.instance import app as skeleton_app

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import conversations as router

    return router.send_conversation_message
