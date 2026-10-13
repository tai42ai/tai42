"""Tests for the request-scoped caller-identity context: the default anonymous
value, the set/get/reset round-trip, nested restore, and isolation across
concurrent asyncio tasks — and the parallel secret-read capability.
"""

from __future__ import annotations

import asyncio

from tai42_contract.access_control.context import (
    caller_may_read_secrets,
    get_current_user_id,
    get_request_resource,
    reset_request_resource,
    reset_request_secret_capability,
    reset_request_user_id,
    set_request_resource,
    set_request_secret_capability,
    set_request_user_id,
)


def test_default_is_none():
    # No caller bound at module/thread scope: an anonymous caller reads None.
    assert get_current_user_id() is None


def test_set_get_reset_round_trip():
    token = set_request_user_id("u-1")
    assert get_current_user_id() == "u-1"
    reset_request_user_id(token)
    assert get_current_user_id() is None


def test_reset_restores_previous_value():
    outer = set_request_user_id("outer")
    inner = set_request_user_id("inner")
    assert get_current_user_id() == "inner"
    reset_request_user_id(inner)
    # Resetting the inner token restores the outer binding, not the default.
    assert get_current_user_id() == "outer"
    reset_request_user_id(outer)
    assert get_current_user_id() is None


def test_set_none_is_allowed():
    token = set_request_user_id("u-2")
    none_token = set_request_user_id(None)
    # Binding None models an explicitly anonymous caller: it shadows the
    # previous binding and reads back as None while bound.
    assert get_current_user_id() is None
    reset_request_user_id(none_token)
    assert get_current_user_id() == "u-2"
    reset_request_user_id(token)


def test_isolation_across_tasks():
    async def scenario() -> None:
        set_request_user_id("parent")
        seen: dict[str, str | None] = {}

        async def worker(name: str) -> None:
            # A task starts from a copy of the parent context (sees "parent"),
            # then binds its own id without disturbing siblings or the parent.
            assert get_current_user_id() == "parent"
            set_request_user_id(name)
            await asyncio.sleep(0)
            seen[name] = get_current_user_id()

        await asyncio.gather(worker("a"), worker("b"))
        assert seen == {"a": "a", "b": "b"}
        # A child task's binding never leaks back into the parent context.
        assert get_current_user_id() == "parent"

    asyncio.run(scenario())


def test_secret_capability_default_is_false():
    # No caller bound: fail-closed — the caller is never treated as secret-capable.
    assert caller_may_read_secrets() is False


def test_secret_capability_set_get_reset_round_trip():
    token = set_request_secret_capability(True)
    assert caller_may_read_secrets() is True
    reset_request_secret_capability(token)
    # Reset restores the fail-closed default, not a lingering True.
    assert caller_may_read_secrets() is False


def test_secret_capability_reset_restores_previous_value():
    outer = set_request_secret_capability(True)
    inner = set_request_secret_capability(False)
    assert caller_may_read_secrets() is False
    reset_request_secret_capability(inner)
    # Resetting the inner token restores the outer True binding.
    assert caller_may_read_secrets() is True
    reset_request_secret_capability(outer)
    assert caller_may_read_secrets() is False


def test_request_resource_default_is_none():
    # No request bound, or a request that addresses no advertised protected resource.
    assert get_request_resource() is None


def test_request_resource_set_get_reset_round_trip():
    token = set_request_resource("https://mcp.example/mcp")
    assert get_request_resource() == "https://mcp.example/mcp"
    reset_request_resource(token)
    assert get_request_resource() is None


def test_request_resource_is_isolated_across_tasks():
    async def scenario() -> None:
        seen: dict[str, str | None] = {}

        async def worker(name: str) -> None:
            set_request_resource(f"https://mcp.example/{name}")
            await asyncio.sleep(0)
            seen[name] = get_request_resource()

        await asyncio.gather(worker("a"), worker("b"))
        assert seen == {"a": "https://mcp.example/a", "b": "https://mcp.example/b"}
        assert get_request_resource() is None

    asyncio.run(scenario())


def test_request_resource_accessors_are_exported_from_the_package():
    import tai42_contract.access_control as package

    for name in ("get_request_resource", "set_request_resource", "reset_request_resource"):
        assert name in package.__all__
        assert getattr(package, name) is globals()[name]
