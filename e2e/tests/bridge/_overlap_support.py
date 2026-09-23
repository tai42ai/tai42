"""Shared helpers for the overlap bridge suite.

The overlap specs drive the ``e2e_overlap_probe`` / ``e2e_overlap_yield`` tool targets over the
web channel (its own public doors are the medium, and each opened page mints a fresh visitor so
no per-address cap is shared between specs) and read back the per-turn payloads the probe RPUSHes.
These helpers build the tool ``start_expr`` that maps the turn's overlap keys onto the probe
kwargs, open a visitor, send a message, and match a reply frame — the shape every overlap spec
shares.

Named ``_overlap_support`` (leading underscore) so pytest never collects it as a test module.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness


def probe_start_expr(marker: str, *, hold_seconds: float = 0.0) -> str:
    """The ``start_expr`` mapping a turn's overlap keys onto ``e2e_overlap_probe`` kwargs.

    ``message`` is the turn's whole text, ``messages`` / ``superseded`` the ``deliver="all"``
    batch and dropped records (``null`` under ``deliver="one"``), ``key`` the fixed probe marker
    the spec reads back under, and ``hold_seconds`` the hold that keeps the turn open."""
    return (
        f'{{key: "{marker}", message: .message, messages: .messages, '
        f"superseded: .superseded, hold_seconds: {hold_seconds}}}"
    )


def yield_start_expr(marker: str, *, wait_seconds: float = 8.0) -> str:
    """The ``start_expr`` mapping a turn's overlap keys onto ``e2e_overlap_yield`` kwargs."""
    return (
        f'{{key: "{marker}", message: .message, messages: .messages, '
        f"superseded: .superseded, wait_seconds: {wait_seconds}}}"
    )


async def create_web_tool_route(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    tag: str,
    *,
    tool: str,
    start_expr: str,
    overlap: dict[str, object],
) -> tuple[str, str]:
    """Create a ``target_kind=tool`` web route carrying an overlap policy; returns
    ``(route name, our_identity)``. Each spec gets its own identity so one ``(channel,
    our_identity)`` pair routes to exactly one route on the shared stack."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    exec_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool=tool,
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        start_expr=start_expr,
        overlap=overlap,
    )
    return route_name, identity


async def open_visitor(bridge: BridgeHarness, identity: str) -> WebChatClient:
    """Open the web chat page as a fresh first-time visitor and adopt its session."""
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def send_web(web: WebChatClient, text: str) -> str:
    """Send one visitor message and return the accepted record's ``message_id``.

    The web door acks on accept (the turn runs in the background), so the id is returned before
    the turn completes — a spec posts the next overlapping message while this turn is still held."""
    sent = await web.send(text)
    assert sent.status_code == 200, sent.text
    return sent.json()["data"]["message_id"]


def reply_matching(text: str) -> Callable[[str, dict], bool]:
    """A predicate for an outbound reply frame whose text carries ``text``."""
    return lambda event, data: event == "chat.message" and data.get("direction") == "out" and text in data["text"]


def entry_texts(entry: dict, field: str) -> list[str]:
    """The ordered ``text`` values of a probe entry's ``messages`` / ``superseded`` list.

    ``e2e_overlap_probe`` records the raw payload entries, each an ``{id, text, accepted_at}``
    object; a spec asserts on the ordered texts. A ``null`` field (``deliver="one"``) reads as an
    empty list."""
    values = entry.get(field)
    if values is None:
        return []
    return [item["text"] for item in values]


def joined(*texts: str) -> str:
    """The whole-turn text the platform joins a batch into: the parts in order, blank-line joined."""
    return "\n\n".join(text for text in texts if text.strip())


def decode_reply(data: dict) -> object:
    """A web reply frame's text decoded from JSON (a tool route replying a JSON string)."""
    return json.loads(data["text"])
