"""A date range with a time on each end, over the callback form page and Slack's modal.

Each end of a range of two ``date`` fields may pair with a ``format: time`` field naming it in
``timeOf``. These legs drive the real doors on the backendless channel stack:

* the callback form page (opened from telegram's ``web_app`` button) draws the four controls in
  drawing order, start date, start time, end date, end time, whatever order the schema declares
  them in, and carries the caller's ``invalid`` words for the page's own time check; the answer
  door refuses a same-day earlier end time on the END's time field with the platform's sentence
  and resolves a later one with four plain strings; a range of two ``date-time`` ends is ordered
  by instant at the door;
* slack's modal draws each ``timepicker`` right after its date's ``datepicker``, and a
  ``view_submission`` with an earlier same-day end time is answered ``response_action: "errors"``
  pinned under the END time's block alone (the caller's ``wording.answer.rejected`` wrapping it).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tai42_e2e.channel_stubs import FakeSlack, FakeTelegram
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

from ._support import cancel_and_join, is_pending, post_callback, post_inbound

pytestmark = [
    pytest.mark.backendless,
    # These legs read the recording stubs; under a real telegram/slack selection the plugin
    # talks to the live vendor and the captures never land.
    pytest.mark.skipif(
        HarnessSettings().is_real("telegram") or HarnessSettings().is_real("slack"),
        reason="form delivery over telegram/slack is a stub-capture leg; the real legs run against the live vendor",
    ),
]

_TIME_ORDER = "The end time must be on or after the start time"

# The times are declared LAST, so every surface's drawing order has to move each one up to
# right after its date.
_TIMED_RANGE = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date", "title": "From"},
        "end": {"type": "string", "format": "date", "title": "To", "rangeStart": "start"},
        "start_time": {"type": "string", "format": "time", "title": "From time", "timeOf": "start"},
        "end_time": {"type": "string", "format": "time", "title": "To time", "timeOf": "end"},
    },
    "required": ["start", "end", "start_time", "end_time"],
}

_INSTANT_RANGE = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date-time"},
        "end": {"type": "string", "format": "date-time", "rangeStart": "start"},
    },
    "required": ["start", "end"],
}

_SLACK_FORM_OPEN_ACTION = "tai42_form_open"
_SLACK_FORM_SUBMIT_CALLBACK = "tai42_form_submit"
_SLACK_FIELD_ACTION = "tai42_form_field"
_SLACK_INTERACTIVE = "/api/channels/slack/interactive"

_TELEGRAM_NEEDS = (
    "kind:channels:telegram",
    "topology:replicas",
    "helper:channel-fake:telegram",
    "setting:CHANNEL_TELEGRAM_DEFAULT_RECIPIENT",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)
_SLACK_NEEDS = (
    "kind:channels:slack",
    "topology:replicas",
    "helper:channel-fake:slack",
    "setting:CHANNEL_SLACK_SIGNING_SECRET",
    "setting:INTERACTIONS_PUBLIC_BASE_URL",
)


async def _wait_one_send(fake: FakeTelegram | FakeSlack, needle: str) -> dict:
    """Wait until exactly one recorded outbound send carries ``needle``; return it."""

    async def probe() -> list[dict]:
        return fake.sends_matching(needle)

    records = await wait_for_async(probe, deadline=8.0, message=f"no send carrying {needle!r} was recorded")
    assert len(records) == 1, f"expected exactly one send carrying {needle!r}, saw {len(records)}"
    return records[0]


def _ask_task(stack: TaiStack, arguments: dict[str, Any]) -> asyncio.Task[object]:
    """Start an ``ask`` on replica A; the task resolves with the answer."""

    async def ask() -> object:
        async with stack.mcp(port=stack.port_a) as mcp:
            result = await mcp.call_tool("ask", arguments)
        return result.data.answer

    return asyncio.create_task(ask())


async def _callback_page(fake_telegram: FakeTelegram, question: str) -> tuple[str, str]:
    """The callback URL behind the form question's web_app button, and the page it serves."""
    record = await _wait_one_send(fake_telegram, question)
    callback_url = record["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        page = await client.get(callback_url)
    assert page.status_code == 200, page.text
    return callback_url, page.text


def _control_tag(page: str, field: str) -> str:
    """The opening tag of the control drawn for ``field`` on the page."""
    start = page.index(f'data-field="{field}"')
    return page[page.rindex("<", 0, start) : page.index(">", start) + 1]


@pytest.mark.needs(*_TELEGRAM_NEEDS)
async def test_callback_page_draws_each_time_after_its_date_and_the_door_orders_by_time(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_timed_range_q")
    task = _ask_task(
        stack, {"question": question, "channel": "telegram", "answer_format": "form", "schema": _TIMED_RANGE}
    )
    try:
        callback_url, page = await _callback_page(fake_telegram, question)
        # One group, four controls in drawing order (DOM order is the tab order).
        drawn = [page.index(f'data-field="{name}"') for name in ("start", "start_time", "end", "end_time")]
        assert drawn == sorted(drawn), drawn
        end_time = _control_tag(page, "end_time")
        assert 'data-time-of="end"' in end_time
        assert 'data-range-start-time="start_time"' in end_time
        assert f'data-msg-range_order="{_TIME_ORDER}"' in end_time

        # A same-day earlier end time is refused on the END's time field; the ask stays answerable.
        early = {"start": "2026-03-01", "end": "2026-03-01", "start_time": "10:00", "end_time": "09:00"}
        refused = await post_callback(callback_url, json.dumps({"answer": early}).encode())
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"] == _TIME_ORDER
        assert refused.json()["field"] == "end_time"
        assert await is_pending(stack, stack.port_a, question)

        # A later end time on the same day resolves the ask with the four plain strings.
        later = {**early, "end_time": "10:30"}
        accepted = await post_callback(callback_url, json.dumps({"answer": later}).encode())
        assert accepted.status_code == 200, accepted.text
        resolved = await asyncio.wait_for(task, timeout=15.0)
    finally:
        await cancel_and_join(task)
    assert resolved == later


@pytest.mark.needs(*_TELEGRAM_NEEDS)
async def test_callback_page_carries_the_caller_words_for_the_time_order(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_timed_words_q")
    words = "Pick an end time after the start"
    task = _ask_task(
        stack,
        {
            "question": question,
            "channel": "telegram",
            "answer_format": "form",
            "schema": _TIMED_RANGE,
            "wording": {"form": {"fields": {"end_time": {"invalid": words}}}},
        },
    )
    try:
        callback_url, page = await _callback_page(fake_telegram, question)
        # The page shows the caller's words in place of the time sentence it checks in the form.
        assert f'data-invalid="{words}"' in _control_tag(page, "end_time")
        # The answer door, checking every shape again, refuses with the platform's sentence.
        early = {"start": "2026-03-01", "end": "2026-03-01", "start_time": "10:00", "end_time": "09:00"}
        refused = await post_callback(callback_url, json.dumps({"answer": early}).encode())
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"] == _TIME_ORDER
        assert refused.json()["field"] == "end_time"
    finally:
        await cancel_and_join(task)


@pytest.mark.needs(*_TELEGRAM_NEEDS)
async def test_answer_door_orders_two_date_time_ends_by_instant(
    channel_stack: TaiStack, fake_telegram: FakeTelegram, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("tg_instant_range_q")
    task = _ask_task(
        stack, {"question": question, "channel": "telegram", "answer_format": "form", "schema": _INSTANT_RANGE}
    )
    try:
        callback_url, page = await _callback_page(fake_telegram, question)
        # The page leaves the instant order to the answer door.
        assert "data-range-instant" in _control_tag(page, "end")
        early = {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T09:00:00Z"}
        refused = await post_callback(callback_url, json.dumps({"answer": early}).encode())
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"] == _TIME_ORDER
        assert refused.json()["field"] == "end"
        assert await is_pending(stack, stack.port_a, question)
    finally:
        await cancel_and_join(task)


async def _open_slack_modal(stack: TaiStack, fake_slack: FakeSlack, question: str, trigger: str) -> tuple[dict, str]:
    """Open the form modal from the slack message's button; return the view and the signing secret."""
    signing_secret = stack.config.env["CHANNEL_SLACK_SIGNING_SECRET"]
    post = await _wait_one_send(fake_slack, question)
    interaction_id = next(
        element["value"]
        for block in post["blocks"] or []
        for element in block.get("elements", [])
        if element.get("action_id") == _SLACK_FORM_OPEN_ACTION
    )
    opened = await post_inbound(
        stack,
        _SLACK_INTERACTIVE,
        fake_slack.build_interactive(
            signing_secret=signing_secret,
            payload={
                "type": "block_actions",
                "trigger_id": trigger,
                "actions": [{"action_id": _SLACK_FORM_OPEN_ACTION, "value": interaction_id}],
            },
        ),
    )
    assert opened.status_code == 200, opened.text
    return fake_slack.views[-1], signing_secret


def _slack_submission(view: dict, values: dict[str, str]) -> dict:
    """A view_submission carrying the picked dates and times."""
    state: dict[str, Any] = {}
    for name, value in values.items():
        if name.endswith("_time"):
            state[name] = {_SLACK_FIELD_ACTION: {"type": "timepicker", "selected_time": value}}
        else:
            state[name] = {_SLACK_FIELD_ACTION: {"type": "datepicker", "selected_date": value}}
    return {
        "type": "view_submission",
        "view": {
            "callback_id": _SLACK_FORM_SUBMIT_CALLBACK,
            "id": view.get("id"),
            "private_metadata": view["private_metadata"],
            "state": {"values": state},
        },
    }


@pytest.mark.needs(*_SLACK_NEEDS)
async def test_slack_modal_draws_each_timepicker_after_its_date_and_pins_the_time_order(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_timed_range_q")
    task = _ask_task(stack, {"question": question, "channel": "slack", "answer_format": "form", "schema": _TIMED_RANGE})
    try:
        view, secret = await _open_slack_modal(stack, fake_slack, question, uniq("slack_timed_trigger"))
        inputs = [(block["block_id"], block["element"]["type"]) for block in view["blocks"] if block["type"] == "input"]
        assert inputs == [
            ("start", "datepicker"),
            ("start_time", "timepicker"),
            ("end", "datepicker"),
            ("end_time", "timepicker"),
        ]

        early = {"start": "2026-03-01", "end": "2026-03-01", "start_time": "10:00", "end_time": "09:00"}
        refused = await post_inbound(
            stack,
            _SLACK_INTERACTIVE,
            fake_slack.build_interactive(signing_secret=secret, payload=_slack_submission(view, early)),
        )
        assert refused.status_code == 200, refused.text
        body = refused.json()
        assert body["response_action"] == "errors", body
        assert body["errors"] == {"end_time": _TIME_ORDER}, body

        later = {**early, "end_time": "10:30"}
        accepted = await post_inbound(
            stack,
            _SLACK_INTERACTIVE,
            fake_slack.build_interactive(signing_secret=secret, payload=_slack_submission(view, later)),
        )
        assert accepted.status_code == 200, accepted.text
        resolved = await asyncio.wait_for(task, timeout=15.0)
    finally:
        await cancel_and_join(task)
    assert resolved == later
    assert not await is_pending(stack, stack.port_b, question)


@pytest.mark.needs(*_SLACK_NEEDS)
async def test_slack_time_order_refusal_reads_the_caller_rejected_wrapper(
    channel_stack: TaiStack, fake_slack: FakeSlack, uniq: Callable[[str], str]
) -> None:
    stack = channel_stack
    question = uniq("slack_timed_words_q")
    task = _ask_task(
        stack,
        {
            "question": question,
            "channel": "slack",
            "answer_format": "form",
            "schema": _TIMED_RANGE,
            "wording": {"answer": {"rejected": "Rejected: {reason}"}},
        },
    )
    try:
        view, secret = await _open_slack_modal(stack, fake_slack, question, uniq("slack_words_trigger"))
        early = {"start": "2026-03-01", "end": "2026-03-01", "start_time": "10:00", "end_time": "09:00"}
        refused = await post_inbound(
            stack,
            _SLACK_INTERACTIVE,
            fake_slack.build_interactive(signing_secret=secret, payload=_slack_submission(view, early)),
        )
        assert refused.status_code == 200, refused.text
        # The door's time sentence, wrapped by the caller's rejected words, under the END time alone.
        assert refused.json()["errors"] == {"end_time": f"Rejected: {_TIME_ORDER}"}, refused.json()
    finally:
        await cancel_and_join(task)
