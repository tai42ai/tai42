"""``list_changed`` broadcast infrastructure.

FastMCP has no notify-all helper and no on-disconnect hook, so the skeleton owns
the active-session registry and the per-session broadcast. These tests drive the
registry directly with fake sessions (each fake session records which
list-changed method fired) and exercise the reload diff-guard through a started
app.
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.app.sessions import SessionRegistry, SessionTrackingMiddleware


class _FakeSession:
    """Records which per-session list-changed method fired. ``fail`` makes every
    send raise, standing in for a dead session."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def _emit(self, name: str) -> None:
        if self.fail:
            raise RuntimeError("dead session")
        self.calls.append(name)

    async def send_tool_list_changed(self) -> None:
        await self._emit("tool")

    async def send_prompt_list_changed(self) -> None:
        await self._emit("prompt")

    async def send_resource_list_changed(self) -> None:
        await self._emit("resource")


def test_emit_maps_singular_kind_to_plural_and_reaches_every_session():
    reg = SessionRegistry()
    a, b = _FakeSession(), _FakeSession()

    async def go() -> None:
        reg.track(a)
        reg.track(b)
        await reg.emit_list_changed("tool")

    asyncio.run(go())
    assert a.calls == ["tool"]
    assert b.calls == ["tool"]


def test_emit_only_fires_the_requested_kind():
    reg = SessionRegistry()
    s = _FakeSession()

    async def go() -> None:
        reg.track(s)
        await reg.emit_list_changed("prompt")
        await reg.emit_list_changed("resource")

    asyncio.run(go())
    assert s.calls == ["prompt", "resource"]


def test_emit_unknown_kind_raises():
    reg = SessionRegistry()

    async def go() -> None:
        await reg.emit_list_changed("preset")

    with pytest.raises(ValueError, match="Unknown list_changed kind"):
        asyncio.run(go())


def test_dead_session_is_pruned_and_broadcast_continues():
    reg = SessionRegistry()
    dead, live = _FakeSession(fail=True), _FakeSession()

    async def go() -> None:
        reg.track(dead)
        reg.track(live)
        await reg.emit_list_changed("tool")

    asyncio.run(go())
    # The live session still received the notification (prune-and-continue, not
    # abort), and the dead session was removed from the registry.
    assert live.calls == ["tool"]
    assert reg.active_count() == 1


def test_concurrent_track_during_broadcast_does_not_raise():
    """A ``track()`` racing an ``emit_list_changed`` iteration must not raise
    ``dictionary changed size during iteration`` — the lock snapshots the session
    list before the awaited sends, so a concurrent insert is safe."""
    reg = SessionRegistry()
    existing = [_FakeSession() for _ in range(20)]

    async def go() -> None:
        for s in existing:
            reg.track(s)

        async def broadcaster() -> None:
            for _ in range(20):
                await reg.emit_list_changed("tool")

        async def tracker() -> None:
            for _ in range(50):
                reg.track(_FakeSession())
                await asyncio.sleep(0)

        await asyncio.gather(broadcaster(), tracker())

    asyncio.run(go())  # no RuntimeError


def test_tracking_middleware_registers_the_session():
    reg = SessionRegistry()
    mw = SessionTrackingMiddleware(reg)
    session = _FakeSession()

    class _Ctx:
        pass

    fastmcp_ctx = _Ctx()
    fastmcp_ctx.session = session  # type: ignore[attr-defined]

    class _MwCtx:
        fastmcp_context = fastmcp_ctx

    seen: list[str] = []

    async def call_next(_ctx):
        seen.append("next")
        return "result"

    async def go():
        return await mw.on_message(_MwCtx(), call_next)  # type: ignore[arg-type]

    result = asyncio.run(go())
    assert result == "result"
    assert seen == ["next"]
    assert reg.active_count() == 1


# -- reload diff-guard through a started app ----------------------------------


# A per-reload list_changed broadcast is deliberately absent in the epoch model: a
# reload swaps in a fresh epoch whose FastMCP lifespan is entered anew and retires the
# previous one, terminating every live MCP transport. Clients re-initialise
# against the new session-id space and re-list from scratch, so there is no persistent
# session across the swap to notify — the session termination supersedes the old
# in-place reload's diff-guarded list_changed.
