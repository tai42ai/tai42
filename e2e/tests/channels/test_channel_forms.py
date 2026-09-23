"""Form-over-channel delivery on the backendless channel stack.

``ask(answer_format="form", schema=..., channel=...)`` renders a schema-driven
answer surface on the channels that advertise ``supports_form_delivery`` and refuses
loudly on one that does not:

* telegram carries a ``web_app`` button opening the callback door's schema-rendered form
  page; the page's POST resolves the ask (and a mismatched object is a 400 that leaves the
  ask answerable) — the callback form-page leg and the telegram web_app button in one flow;
* slack posts a Block Kit message whose button drives the interactivity door to open a
  modal (``views.open`` captured on the stub), and a ``view_submission`` forwards the coerced
  typed dict;
* twilio advertises no form delivery, so the ask is refused with a loud ``ValueError`` naming
  the channel BEFORE any state is written — nothing pending, nothing sent.

web's own in-chat form widget is driven through the web plugin's public doors in
``test_web_public_chat``. The whatsapp Flow + nfm_reply leg needs the conversations backend
and lives on the bridge stack (``tests/bridge/test_whatsapp_cloud``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
from fastmcp.client.client import CallToolResult

from tai42_e2e.channel_stubs import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._support import cancel_and_join, is_pending, post_callback, post_inbound, tool_content_text

pytestmark = [
    pytest.mark.backendless,
    # Form delivery over telegram/slack rides their recording stub; under a real selection of
    # either the plugin talks to the live vendor and these captures never land. The real legs
    # run on the dedicated creds host. Inert on the all-mock default (both checks False).
    pytest.mark.skipif(
        HarnessSettings().is_real("telegram") or HarnessSettings().is_real("slack"),
        reason="form delivery over telegram/slack is a stub-capture leg; the real legs run on the creds host",
    ),
]

# The smallest form answer schema exercising every rendered control kind these legs assert on:
# a text field and an integer field, both required (a wrong-typed field is the 400 negative).
_FORM_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "amount": {"type": "integer"}},
    "required": ["label", "amount"],
}

# The slack form mapping's fixed ids (mirrored from the plugin's Block Kit contract).
_SLACK_FORM_OPEN_ACTION = "tai42_form_open"
_SLACK_FORM_SUBMIT_CALLBACK = "tai42_form_submit"
_SLACK_FIELD_ACTION = "tai42_form_field"


async def _wait_one_send(fake: FakeTelegram | FakeSlack, needle: str) -> dict:
    """Wait until exactly one recorded outbound send carries ``needle``; return it."""

    async def probe() -> list[dict]:
        return fake.sends_matching(needle)

    records = await wait_for_async(probe, deadline=8.0, message=f"no send carrying {needle!r} was recorded")
    assert len(records) == 1, f"expected exactly one send carrying {needle!r}, saw {len(records)}"
    return records[0]


async def _get(url: str) -> httpx.Response:
    """GET a full callback URL (the form page door). Returns the raw httpx response."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url)


def _form_open_value(blocks: list[dict] | None) -> str:
    """The interaction id carried on the slack form-open button's ``value``."""
    for block in blocks or []:
        for element in block.get("elements", []):
            if element.get("action_id") == _SLACK_FORM_OPEN_ACTION:
                value = element.get("value")
                assert isinstance(value, str), f"form-open button carried a non-string value: {element!r}"
                assert value, f"form-open button carried an empty value: {element!r}"
                return value
    raise AssertionError(f"no {_SLACK_FORM_OPEN_ACTION!r} button in the slack message blocks: {blocks!r}")


async def test_form_over_telegram_web_app_button_and_the_callback_form_page(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_form_q")
    good_answer = {"label": uniq("tg_form_label"), "amount": 7}

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {"question": question, "channel": "telegram", "answer_format": "form", "schema": _FORM_SCHEMA},
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(fake_telegram, question)
        # A form question rides a web_app button opening the schema-rendered callback page as
        # an in-chat webview — no ForceReply correlation, unlike a text/select ask.
        button = record["reply_markup"]["inline_keyboard"][0][0]
        assert button["text"] == "Fill form"
        callback_url = button["web_app"]["url"]
        assert "/api/interactions/callback/" in callback_url
        assert callback_url.startswith(f"http://{stack.host}:{stack.port_b}")

        # GET renders the schema as a form page (a GET never mutates state).
        page = await _get(callback_url)
        assert page.status_code == 200, page.text
        assert "text/html" in page.headers["content-type"]
        assert 'id="askform"' in page.text
        assert 'data-field="label"' in page.text
        assert 'data-field="amount"' in page.text

        # A mismatched object is refused with the door's 400 and the ask stays answerable.
        bad = await post_callback(callback_url, json.dumps({"answer": {"label": "x", "amount": "nope"}}).encode())
        assert bad.status_code == 400, bad.text
        assert await is_pending(stack, stack.port_a, question)
        assert not ask_task.done()

        # A conforming object POSTed by the form page resolves the ask with the typed dict.
        good = await post_callback(callback_url, json.dumps({"answer": good_answer}).encode())
        assert good.status_code == 200, good.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == good_answer
    assert not await is_pending(stack, stack.port_b, question)
    # A form is a Tier-1 send (no inbound correlation): still exactly one send, ever.
    assert len(fake_telegram.sends_matching(question)) == 1


async def test_form_over_slack_opens_a_modal_and_a_view_submission_answers(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_form_q")
    good_answer = {"label": uniq("slack_form_label"), "amount": 5}
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {"question": question, "channel": "slack", "answer_format": "form", "schema": _FORM_SCHEMA},
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        interaction_id = _form_open_value(post["blocks"])

        # A block_actions form-open click drives the interactivity door to open the modal
        # (views.open captured on the stub), carrying the interaction id in private_metadata.
        open_payload = {
            "type": "block_actions",
            "trigger_id": uniq("slack_trigger"),
            "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
        }
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(signing_secret=signing_secret, payload=open_payload),
        )
        assert opened.status_code == 200, opened.text
        assert len(fake_slack.views) == 1, f"expected exactly one opened modal, saw {fake_slack.views!r}"
        view = fake_slack.views[0]
        assert view["callback_id"] == _SLACK_FORM_SUBMIT_CALLBACK
        assert view["private_metadata"] == interaction_id

        # A view_submission carrying the filled state is coerced per the schema (amount "5" ->
        # 5) and forwarded as the typed dict, resolving the ask.
        submit_payload = {
            "type": "view_submission",
            "view": {
                "callback_id": _SLACK_FORM_SUBMIT_CALLBACK,
                "private_metadata": interaction_id,
                "state": {
                    "values": {
                        "label": {_SLACK_FIELD_ACTION: {"type": "plain_text_input", "value": good_answer["label"]}},
                        "amount": {_SLACK_FIELD_ACTION: {"type": "number_input", "value": "5"}},
                    }
                },
            },
        }
        submitted = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(signing_secret=signing_secret, payload=submit_payload),
        )
        assert submitted.status_code == 200, submitted.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == good_answer
    assert not await is_pending(stack, stack.port_b, question)


async def test_form_to_a_non_advertising_channel_is_refused_and_persists_nothing(
    channel_stack: TaiStack, fake_twilio: FakeTwilio, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("twilio_form_q")

    # twilio advertises no ``supports_form_delivery`` — a form ask is refused with a loud
    # ValueError naming the channel, raised BEFORE any state is written.
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool(
            "ask",
            {"question": question, "channel": "twilio", "answer_format": "form", "schema": _FORM_SCHEMA},
            raise_on_error=False,
        )
    assert result.is_error
    detail = tool_content_text(result)
    assert "does not deliver form questions" in detail
    assert "twilio" in detail
    # The refusal is pre-persist and pre-send: nothing is pending, and no SMS was sent.
    assert not await is_pending(stack, stack.port_b, question)
    assert fake_twilio.sends_matching(question) == []


# The ask-less form a ``notify_user`` send carries: a schema, a prefill + per-send options, and a
# stepped page layout — the same FormData/FormPage vocabulary the ask path uses.
_NOTIFY_FORM_SCHEMA = {
    "type": "object",
    "required": ["label"],
    "properties": {
        "label": {"type": "string"},
        "color": {"type": "string", "enum": ["red", "blue"]},
        "count": {"type": "integer"},
    },
}


async def _notify(stack: TaiStack, arguments: dict) -> CallToolResult:
    async with stack.mcp(port=stack.port_a) as mcp:
        return await mcp.call_tool("notify_user", arguments, raise_on_error=False, retry_on_reloading=True)


async def test_notify_form_over_a_channel_carries_the_prefilled_values_and_pages(
    channel_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    recipient = uniq("nf-rcpt")
    label = uniq("nf-label")
    data = {
        "values": {"label": label, "color": "blue"},
        "options": {"color": [{"value": "blue", "label": "Blue"}, {"value": "red", "label": "Red"}]},
    }
    pages = [{"title": "Who", "fields": ["label", "color"]}, {"title": "How many", "fields": ["count"]}]

    result = await _notify(
        stack,
        {
            "message": uniq("nf-msg"),
            "channel": "stub_form",
            "recipient": recipient,
            "schema": _NOTIFY_FORM_SCHEMA,
            "data": data,
            "pages": pages,
        },
    )
    assert not result.is_error, result

    async def _captured() -> dict | None:
        records = stack.records(f"notify_form:{recipient}")
        return json.loads(records[0]) if records else None

    form = await wait_for_async(_captured, deadline=15.0, message="the form notification was never delivered")
    # The delivered form carries the prefilled values and the stepped pages the send named.
    assert form["data"]["values"] == {"label": label, "color": "blue"}, form
    assert [p["fields"] for p in form["pages"]] == [["label", "color"], ["count"]], form
    assert form["schema"] == _NOTIFY_FORM_SCHEMA, form


async def test_notify_form_bad_prefill_and_bad_page_are_refused_and_nothing_sent(
    channel_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack

    # A ``values`` key the schema does not declare is refused, naming the field; nothing delivered.
    bad_rcpt = uniq("nf-bad-rcpt")
    bad_values = await _notify(
        stack,
        {
            "message": uniq("nf-bad"),
            "channel": "stub_form",
            "recipient": bad_rcpt,
            "schema": _NOTIFY_FORM_SCHEMA,
            "data": {"values": {"ghost": "x"}},
        },
    )
    assert bad_values.is_error
    assert "ghost" in json.dumps([b.model_dump(mode="json") for b in (bad_values.content or [])])
    assert stack.records(f"notify_form:{bad_rcpt}") == []

    # A ``pages`` layout omitting a declared property is refused the same way, naming it.
    bad_page_rcpt = uniq("nf-badpage-rcpt")
    bad_page = await _notify(
        stack,
        {
            "message": uniq("nf-badpage"),
            "channel": "stub_form",
            "recipient": bad_page_rcpt,
            "schema": _NOTIFY_FORM_SCHEMA,
            "pages": [{"title": "Only", "fields": ["label", "color"]}],
        },
    )
    assert bad_page.is_error
    assert "count" in json.dumps([b.model_dump(mode="json") for b in (bad_page.content or [])])
    assert stack.records(f"notify_form:{bad_page_rcpt}") == []


async def test_notify_form_over_whatsapp_live_leg_skips_without_creds(uniq: Callable[[str], str]) -> None:
    import os

    if not (os.environ.get("CHANNEL_WHATSAPP_ACCESS_TOKEN") and os.environ.get("CHANNEL_WHATSAPP_PHONE_NUMBER_ID")):
        pytest.skip("whatsapp form-notification live leg needs CHANNEL_WHATSAPP_ACCESS_TOKEN + _PHONE_NUMBER_ID")
    raise AssertionError("whatsapp live form-notification leg is unimplemented on the creds host")
