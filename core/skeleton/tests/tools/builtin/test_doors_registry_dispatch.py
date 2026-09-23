"""The conversation-door tools dispatched through the tool registry's validating adapter.

``send_conversation_message`` / ``send_conversation_event`` are declared under
``from __future__ import annotations`` with custom-typed parameters (``MediaItem``,
``LocationElement``, ``ConversationEvent``). ``run_tool`` validates a call against the
resolved signature before it invokes the body: a well-shaped call passes validation and
reaches the door (which refuses an unauthenticated run by name), and a mis-shaped call is
refused by the validator. Neither path resolves a stringized annotation in the adapter's
own namespace, so neither raises ``NameError``.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from tai42_skeleton.app.instance import app
from tai42_skeleton.conversations.turn.errors import UnauthenticatedApiCallerError
from tai42_skeleton.manifest import Manifest

_DOORS_MANIFEST = {
    "tools": [
        {
            "title": "doors",
            "module": "tai42_skeleton.tools.builtin.doors",
            "include": ["send_conversation_message", "send_conversation_event"],
        }
    ]
}


def _run(coro) -> None:
    asyncio.run(coro)


def test_message_door_validates_a_well_shaped_call_and_refuses_a_mis_shaped_one() -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate(_DOORS_MANIFEST)):
            # A well-shaped call — including the custom-typed ``attachments`` and ``location`` —
            # passes validation and reaches the door, which refuses the unauthenticated run by
            # name. Reaching that refusal proves the adapter resolved the custom annotations.
            with pytest.raises(UnauthenticatedApiCallerError):
                await app.tools.run_tool(
                    "send_conversation_message",
                    {
                        "route_name": "chat",
                        "external_user_id": "u-7",
                        "text": "hi",
                        "attachments": [{"kind": "image", "url": "https://example.test/a.png"}],
                        "location": {"latitude": 1.0, "longitude": 2.0},
                    },
                )

            # A mis-shaped ``attachments`` (a string where a list of ``MediaItem`` is required) is
            # refused by the validator itself, before the body.
            with pytest.raises(ValidationError):
                await app.tools.run_tool(
                    "send_conversation_message",
                    {"route_name": "chat", "external_user_id": "u-7", "text": "hi", "attachments": "not-a-list"},
                )

    _run(run())


def test_event_door_validates_a_well_shaped_call_and_refuses_a_mis_shaped_one() -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate(_DOORS_MANIFEST)):
            # A well-shaped ``ConversationEvent`` passes validation and reaches the door's
            # unauthenticated-run refusal.
            with pytest.raises(UnauthenticatedApiCallerError):
                await app.tools.run_tool(
                    "send_conversation_event",
                    {"route_name": "room", "event": {"event_id": "e-1", "kind": "provider.update"}, "thread_id": "t-1"},
                )

            # A mis-shaped event (missing the required ``event_id``) is refused by the validator.
            with pytest.raises(ValidationError):
                await app.tools.run_tool(
                    "send_conversation_event",
                    {"route_name": "room", "event": {"kind": "provider.update"}, "thread_id": "t-1"},
                )

    _run(run())
