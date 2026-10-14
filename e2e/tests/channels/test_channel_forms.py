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
# a plain text field, an integer field, and two ``inputMode`` string fields — ``contact`` draws
# the email keyboard/control each channel has for it, ``code`` the numeric keyboard — all required
# (a wrong-typed field is the 400 negative). ``inputMode`` only picks the control: the value is
# always the typed string (``code`` keeps ``"007"``, never coerced to a number).
_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "amount": {"type": "integer"},
        "contact": {"type": "string", "inputMode": "email"},
        "code": {"type": "string", "inputMode": "numeric"},
    },
    "required": ["label", "amount", "contact", "code"],
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
    return _form_open_button(blocks)["value"]


def _form_open_button(blocks: list[dict] | None) -> dict:
    """The slack form-open button element (its ``value`` is the interaction id, its ``text`` the label)."""
    for block in blocks or []:
        for element in block.get("elements", []):
            if element.get("action_id") == _SLACK_FORM_OPEN_ACTION:
                value = element.get("value")
                assert isinstance(value, str), f"form-open button carried a non-string value: {element!r}"
                assert value, f"form-open button carried an empty value: {element!r}"
                return element
    raise AssertionError(f"no {_SLACK_FORM_OPEN_ACTION!r} button in the slack message blocks: {blocks!r}")


# A schema carrying the new form controls, to prove each channel's documented DEGRADE of a
# control it has no native widget for. ``colour`` a chips string enum, ``tags`` a chips array,
# ``notes`` a textarea string with a drawn maximum, ``start``/``end`` a rangeStart date pair.
_CONTROLS_SCHEMA = {
    "type": "object",
    "properties": {
        "colour": {"type": "string", "enum": ["red", "green", "blue"], "control": "chips"},
        "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b", "c"]}, "control": "chips"},
        "notes": {"type": "string", "control": "textarea", "maxLength": 500},
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 1, "maxDays": 30},
    },
}


@pytest.mark.needs(
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_telegram_web_app_button_and_the_callback_form_page(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_form_q")
    good_answer = {"label": uniq("tg_form_label"), "amount": 7, "contact": "alice@example.com", "code": "007"}

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
        assert callback_url.startswith(stack.origin(stack.port_b))

        # GET renders the schema as a form page (a GET never mutates state).
        page = await _get(callback_url)
        assert page.status_code == 200, page.text
        assert "text/html" in page.headers["content-type"]
        assert 'id="askform"' in page.text
        assert 'data-field="label"' in page.text
        assert 'data-field="amount"' in page.text
        # The inputMode fields draw their HTML keyboards: email -> type="email"; numeric -> a
        # plain text input carrying inputmode="numeric" (so the typed "007" survives, uncoerced).
        assert '<input data-field="contact" data-kind="string" type="email" required>' in page.text
        assert '<input data-field="code" data-kind="string" type="text" inputmode="numeric" required>' in page.text

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


@pytest.mark.needs(
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_telegram_web_app_button_reads_the_caller_open_label(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_openlbl_q")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "telegram",
                    "answer_format": "form",
                    "schema": _FORM_SCHEMA,
                    "wording": {"form": {"open": "Open the form"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(fake_telegram, question)
        button = record["reply_markup"]["inline_keyboard"][0][0]
        # The web_app button opening the form draws the caller's open label, not the default.
        assert button["text"] == "Open the form"
        assert "web_app" in button
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_confirm_over_telegram_tier1_url_button_reads_the_caller_open_label(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_confirm_q")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "telegram",
                    "answer_format": "confirm",
                    "wording": {"answer": {"open": "Review and sign"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(fake_telegram, question)
        button = record["reply_markup"]["inline_keyboard"][0][0]
        # A confirm (Tier-1) ask opens its answer page through a url button reading the caller's open.
        assert button["text"] == "Review and sign"
        assert "url" in button
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
)
async def test_text_over_telegram_force_reply_reads_the_caller_reply_hint(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_text_q")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "telegram",
                    "answer_format": "text",
                    "wording": {"answer": {"reply_hint": "Type your reply"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(fake_telegram, question)
        reply_markup = record["reply_markup"]
        # A plain text ask (no options) draws a force_reply whose placeholder is the caller's hint.
        assert reply_markup["force_reply"] is True
        assert reply_markup["input_field_placeholder"] == "Type your reply"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
)
async def test_text_over_telegram_draws_the_caller_deadline_line(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_deadline_q")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "telegram",
                    "answer_format": "text",
                    "wording": {"lines": {"deadline": "Respond by {deadline} please."}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(fake_telegram, question)
        # Telegram draws the deadline line under every question; the caller's wording.lines.deadline
        # replaces the platform default, its {deadline} token filled with the channel's own instant.
        assert "Respond by " in record["text"]
        assert "(Answer before" not in record["text"]
        assert "{deadline}" not in record["text"]
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_telegram_callback_page_draws_the_caller_chrome_and_sentences(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_chrome_q")

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "telegram",
                    "answer_format": "form",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "size": {"type": "string", "enum": ["xs", "s", "m", "l", "xl", "xxl"]},
                        },
                    },
                    "wording": {
                        "form": {
                            "title": "Tom & Jerry <sign>",
                            "submit": "Send answers",
                            "back": "Go back",
                            "fields": {"size": {"choice_label": "Choose a size"}},
                        },
                        "answer": {"rejected": "We could not accept that: {reason}"},
                        "answer_page": {
                            "submitted": {"heading": "All set", "text": "Thanks for answering."},
                            "form": {
                                "failed": "That did not send.",
                                "network_error": "No connection — try again.",
                                "update_failed": "Could not refresh: {reason}",
                                "choose_one": "Pick at least one.",
                                "date_unavailable": "That date is taken.",
                            },
                        },
                    },
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        record = await _wait_one_send(fake_telegram, question)
        callback_url = record["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        page = await _get(callback_url)
        assert page.status_code == 200, page.text
        # The caller's form title heads the page (<title> and <h1>), HTML-escaped.
        assert "Tom &amp; Jerry &lt;sign&gt;" in page.text
        # A page-less form's single step carries the caller's submit label; the Back button its text.
        assert 'data-button-label="Send answers"' in page.text
        assert ">Go back</button>" in page.text
        # The refusal, the page's own validation/error sentences, and the submitted-state copy ride
        # escaped data-* attributes the submit script reads; each is present only when set.
        # The long-enum field renders a <select> whose empty-state prompt is the caller's choice_label.
        assert '<option value="">Choose a size</option>' in page.text
        assert 'data-text-rejected="We could not accept that: {reason}"' in page.text
        assert 'data-text-failed="That did not send."' in page.text
        assert 'data-text-network_error="No connection — try again."' in page.text
        assert 'data-text-update_failed="Could not refresh: {reason}"' in page.text
        assert 'data-text-choose_one="Pick at least one."' in page.text
        assert 'data-text-date_unavailable="That date is taken."' in page.text
        assert 'data-submitted-heading="All set"' in page.text
        assert 'data-submitted-text="Thanks for answering."' in page.text
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_opens_a_modal_and_a_view_submission_answers(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_form_q")
    good_answer = {"label": uniq("slack_form_label"), "amount": 5, "contact": "alice@example.com", "code": "007"}
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
        assert json.loads(view["private_metadata"])["id"] == interaction_id

        # The inputMode fields pick Slack's controls: email -> email_text_input (Slack's own
        # built-in check); numeric has no Slack keyboard hint, so it degrades to plain_text_input
        # and the typed string arrives verbatim.
        by_id = {block["block_id"]: block for block in view["blocks"] if block.get("type") == "input"}
        assert by_id["contact"]["element"]["type"] == "email_text_input"
        assert by_id["code"]["element"]["type"] == "plain_text_input"

        # A view_submission carrying the filled state is coerced per the schema (amount "5" ->
        # 5) and forwarded as the typed dict, resolving the ask.
        submit_payload = {
            "type": "view_submission",
            "view": {
                "callback_id": _SLACK_FORM_SUBMIT_CALLBACK,
                "private_metadata": view["private_metadata"],
                "state": {
                    "values": {
                        "label": {_SLACK_FIELD_ACTION: {"type": "plain_text_input", "value": good_answer["label"]}},
                        "amount": {_SLACK_FIELD_ACTION: {"type": "number_input", "value": "5"}},
                        "contact": {_SLACK_FIELD_ACTION: {"type": "email_text_input", "value": good_answer["contact"]}},
                        "code": {_SLACK_FIELD_ACTION: {"type": "plain_text_input", "value": good_answer["code"]}},
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


# A schema whose two fields exercise slack's native date/time controls end to end.
_SLACK_DATE_TIME_SCHEMA = {
    "type": "object",
    "properties": {
        "on": {"type": "string", "format": "date"},
        "at": {"type": "string", "format": "time"},
    },
    "required": ["on", "at"],
}


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_renders_native_date_and_time_pickers(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_dt_form_q")
    good_answer = {"on": "2026-01-31", "at": "09:05"}
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {"question": question, "channel": "slack", "answer_format": "form", "schema": _SLACK_DATE_TIME_SCHEMA},
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        interaction_id = _form_open_value(post["blocks"])

        open_payload = {
            "type": "block_actions",
            "trigger_id": uniq("slack_dt_trigger"),
            "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
        }
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(signing_secret=signing_secret, payload=open_payload),
        )
        assert opened.status_code == 200, opened.text
        view = fake_slack.views[-1]
        # The date field renders a native datepicker, the time field a native timepicker.
        by_id = {b["block_id"]: b for b in view["blocks"] if b.get("type") == "input"}
        assert by_id["on"]["element"]["type"] == "datepicker"
        assert by_id["at"]["element"]["type"] == "timepicker"

        # A view_submission carrying the picked values resolves the ask with the ISO strings
        # unchanged (no coercion).
        submit_payload = {
            "type": "view_submission",
            "view": {
                "callback_id": _SLACK_FORM_SUBMIT_CALLBACK,
                "private_metadata": view["private_metadata"],
                "state": {
                    "values": {
                        "on": {_SLACK_FIELD_ACTION: {"type": "datepicker", "selected_date": good_answer["on"]}},
                        "at": {_SLACK_FIELD_ACTION: {"type": "timepicker", "selected_time": good_answer["at"]}},
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


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_draws_the_caller_chrome_labels(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_chrome_q")
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]
    pages = [{"title": "Only", "fields": ["label", "amount", "contact", "code"], "button_label": "Send request"}]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "slack",
                    "answer_format": "form",
                    "schema": _FORM_SCHEMA,
                    "pages": pages,
                    "wording": {"form": {"open": "Open it", "cancel": "Dismiss", "title": "Please fill this in"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        # The posted open-form button draws the caller's open label.
        assert _form_open_button(post["blocks"])["text"]["text"] == "Open it"
        interaction_id = _form_open_value(post["blocks"])

        open_payload = {
            "type": "block_actions",
            "trigger_id": uniq("slack_chrome_trigger"),
            "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
        }
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(signing_secret=signing_secret, payload=open_payload),
        )
        assert opened.status_code == 200, opened.text
        view = fake_slack.views[-1]
        # The modal's chrome carries the caller's words: the title from wording.form.title, the close
        # from wording.form.cancel, the one submit from the last page's button_label.
        assert view["title"]["text"] == "Please fill this in"
        assert view["close"]["text"] == "Dismiss"
        assert view["submit"]["text"] == "Send request"
    finally:
        await cancel_and_join(ask_task)


# A schema whose one choice field carries more than RADIO_OPTION_THRESHOLD (5) options, so Slack
# draws it as a ``static_select`` — the only choice control with a placeholder slot.
_SELECT_PLACEHOLDER_SCHEMA = {
    "type": "object",
    "properties": {"pick": {"type": "string", "enum": ["a", "b", "c", "d", "e", "f"]}},
}


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_page_less_submit_and_select_placeholder(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_pl_q")
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "slack",
                    "answer_format": "form",
                    "schema": _SELECT_PLACEHOLDER_SCHEMA,
                    "wording": {"form": {"submit": "Send it", "fields": {"pick": {"choice_label": "Pick one"}}}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        interaction_id = _form_open_value(post["blocks"])
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(
                signing_secret=signing_secret,
                payload={
                    "type": "block_actions",
                    "trigger_id": uniq("slack_pl_trigger"),
                    "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
                },
            ),
        )
        assert opened.status_code == 200, opened.text
        view = fake_slack.views[-1]
        # A page-less form's one submit reads the caller's wording.form.submit (no page carries it).
        assert view["submit"]["text"] == "Send it"
        # The long choice draws a static_select whose placeholder is the field's caller choice_label.
        by_id = {b["block_id"]: b for b in view["blocks"] if b.get("type") == "input"}
        element = by_id["pick"]["element"]
        assert element["type"] == "static_select"
        assert element["placeholder"]["text"] == "Pick one"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_review_page_draws_the_caller_empty_line(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_review_q")
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]
    # A stepped form with a review page and no prefilled values, so the review readback is empty.
    pages = [
        {"title": "Fill", "fields": ["label"]},
        {"title": "Review", "fields": [], "kind": "review", "display": []},
    ]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "slack",
                    "answer_format": "form",
                    "schema": {"type": "object", "properties": {"label": {"type": "string"}}},
                    "pages": pages,
                    "wording": {"form": {"review_empty": "Nothing filled in yet."}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        interaction_id = _form_open_value(post["blocks"])
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(
                signing_secret=signing_secret,
                payload={
                    "type": "block_actions",
                    "trigger_id": uniq("slack_review_trigger"),
                    "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
                },
            ),
        )
        assert opened.status_code == 200, opened.text
        view = fake_slack.views[-1]
        # The review page's empty readback shows the caller's wording.form.review_empty line.
        section_texts = [b["text"]["text"] for b in view["blocks"] if b.get("type") == "section" and b.get("text")]
        assert "Nothing filled in yet." in section_texts
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_rejected_submission_shows_the_caller_inline_error(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_reject_q")
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "slack",
                    "answer_format": "form",
                    "schema": _FORM_SCHEMA,
                    "wording": {"answer": {"rejected": "Rejected: {reason}"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        interaction_id = _form_open_value(post["blocks"])
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(
                signing_secret=signing_secret,
                payload={
                    "type": "block_actions",
                    "trigger_id": uniq("slack_reject_trigger"),
                    "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
                },
            ),
        )
        assert opened.status_code == 200, opened.text
        view = fake_slack.views[-1]

        # A view_submission whose amount is not an integer is door-rejected and re-answerable: the
        # modal stays open with an inline error carrying the caller's wording.answer.rejected, its
        # {reason} token taking the door's own reason.
        submit_payload = {
            "type": "view_submission",
            "view": {
                "callback_id": _SLACK_FORM_SUBMIT_CALLBACK,
                "id": view.get("id") or interaction_id,
                "private_metadata": view["private_metadata"],
                "state": {
                    "values": {
                        "label": {_SLACK_FIELD_ACTION: {"type": "plain_text_input", "value": uniq("l")}},
                        "amount": {_SLACK_FIELD_ACTION: {"type": "number_input", "value": "not-a-number"}},
                        "contact": {_SLACK_FIELD_ACTION: {"type": "email_text_input", "value": "alice@example.com"}},
                        "code": {_SLACK_FIELD_ACTION: {"type": "plain_text_input", "value": "007"}},
                    }
                },
            },
        }
        rejected = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(signing_secret=signing_secret, payload=submit_payload),
        )
        assert rejected.status_code == 200, rejected.text
        body = rejected.json()
        assert body["response_action"] == "errors", body
        error_text = next(iter(body["errors"].values()))
        assert error_text.startswith("Rejected: "), body
        assert "{reason}" not in error_text, body
        assert error_text != "Rejected: ", body
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
async def test_form_over_slack_degrades_the_new_controls(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_degrade_q")
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "slack",
                    "answer_format": "form",
                    "schema": _CONTROLS_SCHEMA,
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        post = await _wait_one_send(fake_slack, question)
        interaction_id = _form_open_value(post["blocks"])
        open_payload = {
            "type": "block_actions",
            "trigger_id": uniq("slack_degrade_trigger"),
            "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
        }
        opened = await post_inbound(
            stack,
            "/api/channels/slack/interactive",
            fake_slack.build_interactive(signing_secret=signing_secret, payload=open_payload),
        )
        assert opened.status_code == 200, opened.text
        view = fake_slack.views[-1]
        by_id = {b["block_id"]: b for b in view["blocks"] if b.get("type") == "input"}
        # Slack has no chip input, so chips degrade to the default choice control — a string chips
        # to radio_buttons, an array chips to checkboxes (both documented degrades).
        assert by_id["colour"]["element"]["type"] == "radio_buttons"
        assert by_id["tags"]["element"]["type"] == "checkboxes"
        # A textarea draws a multiline plain_text_input carrying the declared maximum (Slack's own
        # in-modal length check then shows and enforces it).
        notes = by_id["notes"]["element"]
        assert notes["type"] == "plain_text_input"
        assert notes["multiline"] is True
        assert notes["max_length"] == 500
        # Slack has no range control, so a rangeStart pair draws two independent datepickers.
        assert by_id["start"]["element"]["type"] == "datepicker"
        assert by_id["end"]["element"]["type"] == "datepicker"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs("kind:channels:twilio", "topology:replicas", "helper:channel-fake:twilio")
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


@pytest.mark.needs("store:redis", "setting:tai42_e2e_fixtures.stub_form_channel")
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


@pytest.mark.needs("store:redis", "setting:tai42_e2e_fixtures.stub_form_channel")
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


@pytest.mark.needs("store:redis", "setting:tai42_e2e_fixtures.stub_form_channel")
async def test_notify_form_records_the_caller_wording(channel_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = channel_stack
    recipient = uniq("nf-chrome-rcpt")

    # An ask-less form notify carries the caller's form chrome via the wording catalogue — the
    # buttons that are not a page's and the form heading — which the delivered notification records.
    result = await _notify(
        stack,
        {
            "message": uniq("nf-chrome-msg"),
            "channel": "stub_form",
            "recipient": recipient,
            "schema": _NOTIFY_FORM_SCHEMA,
            "wording": {
                "form": {"open": "Open form", "cancel": "Not now", "back": "Go back", "title": "Tell us about it"}
            },
        },
    )
    assert not result.is_error, result

    async def _captured() -> dict | None:
        records = stack.records(f"notify_form:{recipient}")
        return json.loads(records[0]) if records else None

    form = await wait_for_async(_captured, deadline=15.0, message="the form notification was never delivered")
    assert form["wording"]["form"]["open"] == "Open form", form
    assert form["wording"]["form"]["cancel"] == "Not now", form
    assert form["wording"]["form"]["back"] == "Go back", form
    assert form["wording"]["form"]["title"] == "Tell us about it", form


@pytest.mark.needs("no-stack", "setting:real-credentials")
async def test_notify_form_over_whatsapp_live_leg_skips_without_creds(uniq: Callable[[str], str]) -> None:
    import os

    if not (os.environ.get("CHANNEL_WHATSAPP_ACCESS_TOKEN") and os.environ.get("CHANNEL_WHATSAPP_PHONE_NUMBER_ID")):
        pytest.skip("whatsapp form-notification live leg needs CHANNEL_WHATSAPP_ACCESS_TOKEN + _PHONE_NUMBER_ID")
    raise AssertionError("whatsapp live form-notification leg is unimplemented on the creds host")
