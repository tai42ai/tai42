"""The send's ``wording`` catalogue carried onto the web transcript frames.

``deliver`` carries the question's ``wording`` onto the ``chat.question`` frame, and
``notify`` carries a card's / form's ``wording`` onto the ``chat.media`` / ``chat.form``
frame, so the widget draws the caller's words. Absent, the key is omitted and the widget
shows its own words.
"""

from __future__ import annotations

import json

import pytest
from tai42_contract.channels import ChannelNotification, ReplyOption
from tai42_contract.interactions.models import (
    AnswerWording,
    ChoiceWording,
    FieldWording,
    FormWording,
    StatusWording,
    Wording,
)

from tai42_channel_web.channel import WebChannel
from tai42_channel_web.store.transcript import append_form, append_media, append_question

from .conftest import IDENTITY, VISITOR_ID, FakeRedis, _deadline, make_delivery

pytestmark = pytest.mark.usefixtures("web_env")

_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"

_FORM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def _only_entry(fake: FakeRedis) -> dict:
    entries = fake.streams[_TRANSCRIPT_KEY]
    assert len(entries) == 1
    return json.loads(entries[0][1]["data"])


async def test_deliver_carries_the_question_wording_in_the_frame(fake_redis: FakeRedis):
    # A text ask's answer controls, refusal line and settled-state words ride the frame
    # under ``wording``, with only the members the caller set present.
    await WebChannel().deliver(
        make_delivery(
            answer_format="text",
            wording=Wording(
                answer=AnswerWording(submit="Send it", reply_hint="Type a reply", rejected="No good: {reason}"),
                status=StatusWording(answered="Done", expired="Too late", sending="Sending"),
            ),
        )
    )
    assert _only_entry(fake_redis)["wording"] == {
        "answer": {"submit": "Send it", "reply_hint": "Type a reply", "rejected": "No good: {reason}"},
        "status": {"answered": "Done", "expired": "Too late", "sending": "Sending"},
    }


async def test_deliver_omits_the_wording_key_when_absent(fake_redis: FakeRedis):
    await WebChannel().deliver(make_delivery(answer_format="text"))
    assert "wording" not in _only_entry(fake_redis)


async def test_deliver_carries_the_select_group_label(fake_redis: FakeRedis):
    await WebChannel().deliver(
        make_delivery(
            answer_format="select",
            options=["a", "b"],
            wording=Wording(choice=ChoiceWording(group_label="Pick one")),
        )
    )
    assert _only_entry(fake_redis)["wording"] == {"choice": {"group_label": "Pick one"}}


async def test_deliver_carries_the_form_field_placeholder(fake_redis: FakeRedis):
    await WebChannel().deliver(
        make_delivery(
            answer_format="form",
            question="Fill this",
            schema=_FORM_SCHEMA,
            wording=Wording(form=FormWording(submit="File it", fields={"name": FieldWording(choice_label="Pick")})),
        )
    )
    assert _only_entry(fake_redis)["wording"] == {
        "form": {"submit": "File it", "fields": {"name": {"choice_label": "Pick"}}}
    }


async def test_notify_options_carries_the_group_label_on_the_media_card(fake_redis: FakeRedis):
    await WebChannel().notify(
        ChannelNotification(
            message="pick one",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            options=[ReplyOption(text="Item A"), ReplyOption(text="Item B")],
            wording=Wording(choice=ChoiceWording(group_label="Replies")),
        )
    )
    assert _only_entry(fake_redis)["wording"] == {"choice": {"group_label": "Replies"}}


async def test_notify_media_omits_the_wording_key_when_absent(fake_redis: FakeRedis):
    await WebChannel().notify(
        ChannelNotification(
            message="pick one",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            options=[ReplyOption(text="Item A")],
        )
    )
    assert "wording" not in _only_entry(fake_redis)


async def test_notify_form_carries_the_wording_on_the_form_card(fake_redis: FakeRedis):
    await WebChannel().notify(
        ChannelNotification(
            message="Fill this",
            recipient=VISITOR_ID,
            sender_identity=IDENTITY,
            schema=_FORM_SCHEMA,
            wording=Wording(form=FormWording(submit="Send it"), status=StatusWording(sent="Submitted")),
        )
    )
    assert _only_entry(fake_redis)["wording"] == {
        "form": {"submit": "Send it", "fields": {}},
        "status": {"sent": "Submitted"},
    }


async def test_append_question_carries_the_wording_dict_verbatim(fake_redis: FakeRedis):
    await append_question(
        IDENTITY,
        VISITOR_ID,
        "int-1",
        "Deploy?",
        "text",
        None,
        _deadline(),
        wording={"answer": {"submit": "Go"}},
    )
    assert _only_entry(fake_redis)["wording"] == {"answer": {"submit": "Go"}}


async def test_append_media_omits_wording_when_absent(fake_redis: FakeRedis):
    await append_media(IDENTITY, VISITOR_ID, "hi", options=[{"kind": "reply", "text": "a"}])
    assert "wording" not in _only_entry(fake_redis)


async def test_append_form_carries_the_wording_dict(fake_redis: FakeRedis):
    await append_form(
        IDENTITY,
        VISITOR_ID,
        "Fill this",
        _FORM_SCHEMA,
        "tok-1",
        wording={"status": {"sent": "Done"}},
    )
    assert _only_entry(fake_redis)["wording"] == {"status": {"sent": "Done"}}
