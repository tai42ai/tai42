"""Slack form modal reactions over the media-bridge profile, which carries the Slack channel and
the ``e2e_form_reaction`` probe handler.

A reacting field's change re-renders the open modal through ``views.update``. When the reaction
fails, the modal shows a banner: the sender's ``wording.form.update_failed`` line with its
``{reason}`` filled by the reaction door's own reason.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.manifests import SLACK_ALLOWED_RECIPIENTS
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import BridgeHarness, post_inbound

pytestmark = [
    pytest.mark.needs("kind:identity", "kind:channels:slack", "helper:slack"),
    pytest.mark.skipif(
        HarnessSettings().is_real("slack"),
        reason="the reaction banner leg drives the Slack stub's views.update; the real leg runs on the creds host",
    ),
]

_SLACK_INTERACTIVE_PATH = "/api/channels/slack/interactive"
_SLACK_FORM_OPEN_ACTION = "tai42_form_open"
_SLACK_FIELD_ACTION = "tai42_form_field"

# A form whose ``tier`` field reacts; a silver pick makes the ``e2e_form_reaction`` probe raise.
_REACTING_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "title": "Label"},
        "tier": {"type": "string", "enum": ["gold", "silver"], "title": "Tier"},
    },
    "required": ["label", "tier"],
}

# The reason the platform's reaction door gives when the probe raises.
_REACTION_RAISED_REASON = "reaction handler 'e2e_form_reaction' failed"


def _form_open_value(blocks: list[dict[str, Any]] | None) -> str:
    """The interaction id the form message's open button carries."""
    for block in blocks or []:
        for element in block.get("elements") or []:
            if element.get("action_id") == _SLACK_FORM_OPEN_ACTION:
                return element["value"]
    raise AssertionError(f"no form open button in {blocks!r}")


async def test_slack_modal_reaction_failure_shows_the_callers_could_not_update_banner(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("slack-react-q")
    fake_slack = media_bridge.slack
    signing_secret = media_bridge.stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]
    caller_key = await media_bridge.mint_key(user_id=uniq("slack-react-key"), scopes=["e2e-all"])

    async with media_bridge.stack.mcp(port=media_bridge.stack.port_a, auth=caller_key) as mcp:
        await mcp.call_tool(
            "e2e_reacting_form_ask",
            {
                "question": question,
                "channel": "slack",
                "recipient": SLACK_ALLOWED_RECIPIENTS[0],
                "schema": _REACTING_SCHEMA,
                "reactions": {"field_changed": ["tier"]},
                "expiry_seconds": 120.0,
                "wording": {"form": {"update_failed": "No update ({reason})"}},
            },
        )

    async def message_posted() -> list[dict[str, Any]]:
        return fake_slack.sends_matching(question)

    posts = await wait_for_async(message_posted, deadline=15.0, message=f"the slack form message for {question}")
    interaction_id = _form_open_value(posts[0]["blocks"])
    opened = await post_inbound(
        media_bridge.stack,
        _SLACK_INTERACTIVE_PATH,
        fake_slack.build_interactive(
            signing_secret=signing_secret,
            payload={
                "type": "block_actions",
                "trigger_id": uniq("slack-react-trigger"),
                "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
            },
        ),
    )
    assert opened.status_code == 200, opened.text
    view = fake_slack.views[-1]

    # Picking silver runs the reaction, which raises: the modal re-renders with the caller's banner
    # right under the question, its {reason} the reaction door's own sentence.
    changed = await post_inbound(
        media_bridge.stack,
        _SLACK_INTERACTIVE_PATH,
        fake_slack.build_interactive(
            signing_secret=signing_secret,
            payload={
                "type": "block_actions",
                "actions": [{"action_id": _SLACK_FIELD_ACTION, "block_id": "tier"}],
                "view": {
                    "id": uniq("slack-react-view"),
                    "hash": uniq("slack-react-hash"),
                    "private_metadata": view["private_metadata"],
                    "state": {
                        "values": {
                            "label": {_SLACK_FIELD_ACTION: {"type": "plain_text_input", "value": "x"}},
                            "tier": {
                                _SLACK_FIELD_ACTION: {"type": "radio_buttons", "selected_option": {"value": "silver"}}
                            },
                        }
                    },
                },
            },
        ),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"status": "updated"}
    banner = fake_slack.view_updates[-1]["view"]["blocks"][1]
    assert banner == {"type": "section", "text": {"type": "mrkdwn", "text": f"No update ({_REACTION_RAISED_REASON})"}}
