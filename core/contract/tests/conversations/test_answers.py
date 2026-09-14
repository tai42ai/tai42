"""Tests for the turn outcome: ``AnswerPart`` (the rich multi-message part),
``ConversationAnswer`` and its ordered ``parts``, and ``joined_answer_text``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

# -- ConversationAnswer (status) ------------------------------------------------


def test_conversation_answer_is_frozen_and_carries_status():
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(message_id="m-1", thread_id="bridge:r:u", status="answered", answer="the reply")
    assert answer.status == "answered"
    with pytest.raises(ValidationError):
        answer.answer = "changed"


def test_conversation_answer_rejects_unknown_status():
    from tai42_contract.conversations import ConversationAnswer

    bad: dict[str, Any] = {"message_id": "m-1", "thread_id": "t", "status": "maybe", "answer": "x"}
    with pytest.raises(ValidationError):
        ConversationAnswer(**bad)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_conversation_answer_rejects_blank_answer(blank: str):
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError):
        ConversationAnswer(message_id="m-1", thread_id="t", status="error", answer=blank)


@pytest.mark.parametrize("status", ["answered", "error"])
def test_conversation_answer_requires_answer_text_for_non_silent(status: str):
    from tai42_contract.conversations import ConversationAnswer

    body: dict[str, Any] = {"message_id": "m-1", "thread_id": "t", "status": status, "answer": None}
    with pytest.raises(ValidationError, match="carries answer text"):
        ConversationAnswer(**body)


def test_conversation_answer_silent_carries_no_answer_text():
    from tai42_contract.conversations import ConversationAnswer

    silent = ConversationAnswer(message_id="m-1", thread_id="t", status="silent")
    assert silent.status == "silent"
    assert silent.answer is None


@pytest.mark.parametrize("answer", ["some text", "", "   "])
def test_conversation_answer_silent_rejects_any_answer_field(answer: str):
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="silent answer carries no answer text"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="silent", answer=answer)


# -- AnswerPart (the rich multi-message part shape) -----------------------------


def _part_form_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"size": {"type": "string"}}}


def test_answer_part_is_a_text_only_part():
    from tai42_contract.conversations import AnswerPart

    part = AnswerPart(message="just text")
    assert part.message == "just text"
    assert part.is_plain_text()


def test_answer_part_carries_media_and_options():
    from tai42_contract.channels import ReplyOption
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    part = AnswerPart(
        message="here is the chart",
        media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
        options=[ReplyOption(text="Yes"), ReplyOption(text="No")],
    )
    assert not part.is_plain_text()
    assert part.media is not None
    assert part.media[0].url == "https://cdn.example/c.png"
    assert part.options == [ReplyOption(text="Yes"), ReplyOption(text="No")]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_answer_part_rejects_a_blank_message(blank: str):
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="message must be non-blank"):
        AnswerPart(message=blank)


def test_answer_part_is_strict_from_birth_unknown_key_refused():
    from tai42_contract.conversations import AnswerPart

    # extra="forbid": a NEW authoring surface never silently drops an unknown key.
    with pytest.raises(ValidationError):
        # recipient is per-delivery, not per-part
        AnswerPart(message="hi", recipient="+15550001111")  # pyright: ignore[reportCallIssue]


def test_answer_part_media_and_template_are_exclusive():
    from tai42_contract.channels import ChannelTemplate
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    with pytest.raises(ValidationError, match="media and template are mutually exclusive"):
        AnswerPart(
            message="hi",
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
            template=ChannelTemplate(name="t", language="en"),
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_answer_part_media_only_message_may_be_blank(blank: str):
    # A media-only part: a caption-less image carries the content, message may be blank/omitted.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    part = AnswerPart(message=blank, media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")])
    assert part.message == blank
    assert not part.is_plain_text()  # media makes it a rich part, never dropped from parts
    assert AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")]).message == ""


def test_answer_part_media_only_carries_no_options():
    from tai42_contract.channels import ReplyOption
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    with pytest.raises(ValidationError, match=r"content-only .* carries no options"):
        AnswerPart(
            message="",
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
            options=[ReplyOption(text="Yes")],
        )


def test_answer_part_carries_a_form_schema():
    # An ask-less form part: the message is the form's prompt, the schema the fillable
    # form; the participant's submission enters the conversation as a participant message.
    from tai42_contract.conversations import AnswerPart

    part = AnswerPart(message="tell us your size", schema=_part_form_schema())
    assert part.schema == _part_form_schema()
    assert not part.is_plain_text()
    assert AnswerPart(message="hi").schema is None


def test_answer_part_schema_rejects_present_but_empty_dict():
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="non-empty dict"):
        AnswerPart(message="hi", schema={})


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_answer_part_schema_requires_a_non_blank_message(blank: str):
    # A form needs a prompt: a media-only (blank-message) part carries no schema.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    with pytest.raises(ValidationError, match=r"carries no schema; a form needs a prompt"):
        AnswerPart(
            message=blank,
            media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
            schema=_part_form_schema(),
        )


def test_answer_part_schema_and_template_are_exclusive():
    from tai42_contract.channels import ChannelTemplate
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="schema and template are mutually exclusive"):
        AnswerPart(message="hi", schema=_part_form_schema(), template=ChannelTemplate(name="t", language="en"))


def test_answer_part_schema_and_options_are_exclusive():
    # One message carries ONE interactive surface: a form's fields or a tap list, never both.
    from tai42_contract.channels import ReplyOption
    from tai42_contract.conversations import AnswerPart

    with pytest.raises(ValidationError, match="schema and options are mutually exclusive"):
        AnswerPart(message="hi", schema=_part_form_schema(), options=[ReplyOption(text="Yes")])


def test_answer_part_schema_may_combine_with_media():
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    part = AnswerPart(
        message="pick from the chart",
        media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")],
        schema=_part_form_schema(),
    )
    assert part.media is not None
    assert part.schema == _part_form_schema()


def test_answer_part_carries_form_prefill_data_and_pages():
    # A reply part can open its form ALREADY FILLED IN: per-send prefill (values +
    # option lists) and a stepped-page layout ride the same form part as its schema.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import FormData, FormPage

    part = AnswerPart(
        message="tell us your size",
        schema=_part_form_schema(),
        data=FormData(values={"size": "M"}),
        pages=[FormPage(title="Details", fields=["size"])],
    )
    assert part.data is not None
    assert part.data.values == {"size": "M"}
    assert part.pages is not None
    assert part.pages[0].fields == ["size"]
    assert not part.is_plain_text()


def test_answer_part_data_and_pages_still_forbid_unknown_keys():
    # The new prefill surface does not loosen strictness: an unknown key is still refused.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import FormData

    with pytest.raises(ValidationError):
        AnswerPart(
            message="tell us your size",
            schema=_part_form_schema(),
            data=FormData(values={"size": "M"}),
            surprise="nope",  # pyright: ignore[reportCallIssue]
        )


def test_answer_part_data_rides_a_form_part_only():
    # ``data``/``pages`` enrich a form's schema; on a non-form part they name a form that
    # is not there and are refused loudly, never silently dropped.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import FormData, FormPage

    with pytest.raises(ValidationError, match="data rides a form part"):
        AnswerPart(message="hi", data=FormData(values={"size": "M"}))
    with pytest.raises(ValidationError, match="pages ride a form part"):
        AnswerPart(message="hi", pages=[FormPage(title="Details", fields=["size"])])


def test_answer_part_bad_prefill_value_is_refused():
    # A prefill value that fails its property's schema is refused at the part, so a partly
    # filled form is never delivered — the same check the ask path's InteractionRequest runs.
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import FormData

    with pytest.raises(ValidationError, match="must be a string"):
        AnswerPart(message="tell us your size", schema=_part_form_schema(), data=FormData(values={"size": 42}))
    with pytest.raises(ValidationError, match="unknown property"):
        AnswerPart(message="tell us your size", schema=_part_form_schema(), data=FormData(values={"nope": "x"}))


def test_answer_part_field_set_is_the_notification_content_surface():
    # The mirror invariant, enforced MECHANICALLY: AnswerPart's field set IS
    # ChannelNotification's CONTENT surface — every notification field minus the
    # per-delivery routing fields, which stay on the single delivery and never ride a
    # part. A field added to one model and not the other (or added to the routing set
    # without a decision here) fails this test instead of drifting silently.
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.conversations import AnswerPart

    routing_fields = {"recipient", "sender_identity"}
    assert routing_fields <= set(ChannelNotification.model_fields)
    content_fields = set(ChannelNotification.model_fields) - routing_fields
    assert set(AnswerPart.model_fields) == content_fields


# -- ConversationAnswer.parts (ordered multi-message) ---------------------------


def _parts(*messages: str):
    from tai42_contract.conversations import AnswerPart

    return [AnswerPart(message=m) for m in messages]


def _media_part():
    from tai42_contract.conversations import AnswerPart
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return AnswerPart(media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/c.png")])


def test_conversation_answer_single_message_carries_no_parts():
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(message_id="m-1", thread_id="t", status="answered", answer="just one")
    # A single-message answer carries parts=None; a legacy consumer reads ``answer``.
    assert answer.parts is None


def test_conversation_answer_parts_join_to_the_answer():
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(
        message_id="m-1",
        thread_id="t",
        status="answered",
        answer="one\n\ntwo\n\nthree",
        parts=_parts("one", "two", "three"),
    )
    # Order-significant parts, and ``answer`` is exactly the blank-line join of the part
    # messages so every legacy consumer keeps reading the whole text.
    assert answer.parts is not None
    assert [p.message for p in answer.parts] == ["one", "two", "three"]
    assert answer.answer == "one\n\ntwo\n\nthree"


def test_conversation_answer_parts_must_join_to_the_answer():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="answer must equal the non-blank part messages joined"):
        ConversationAnswer(
            message_id="m-1", thread_id="t", status="answered", answer="one\n\ntwo", parts=_parts("one", "different")
        )


def test_conversation_answer_parts_reject_an_empty_list():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="parts must be a non-empty list"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="answered", answer="x", parts=[])


def test_conversation_answer_silent_carries_no_parts():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="silent answer carries no parts"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="silent", parts=_parts("one", "two"))


def test_conversation_answer_all_media_answer_is_empty_string():
    # Every part media-only: the joined text is "" and that is admissible BECAUSE parts carry
    # the content. A media-only part contributes nothing to ``answer``.
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(
        message_id="m-1", thread_id="t", status="answered", answer="", parts=[_media_part(), _media_part()]
    )
    assert answer.answer == ""
    assert answer.parts is not None
    assert len(answer.parts) == 2


def test_conversation_answer_mixed_media_only_joins_only_text_parts():
    # [text, media-only, text]: the media-only part drops out of the joined answer.
    from tai42_contract.conversations import ConversationAnswer

    answer = ConversationAnswer(
        message_id="m-1",
        thread_id="t",
        status="answered",
        answer="one\n\ntwo",
        parts=[*_parts("one"), _media_part(), *_parts("two")],
    )
    assert answer.answer == "one\n\ntwo"
    assert answer.parts is not None
    assert len(answer.parts) == 3


def test_conversation_answer_blank_answer_without_parts_is_refused():
    from tai42_contract.conversations import ConversationAnswer

    with pytest.raises(ValidationError, match="blank text must carry media-only parts"):
        ConversationAnswer(message_id="m-1", thread_id="t", status="answered", answer="")
