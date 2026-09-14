"""Tests for ``ChannelDelivery`` — the one question handed to a channel for delivery —
plus the ``ask_user`` channel-delivery keyword surface."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest


def _delivery_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "interaction_id": "int-1",
        "question": "Approve the deploy?",
        "answer_format": "text",
        "callback_url": "https://host.example/api/interactions/callback/tkt",
        "timeout_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def _image_item() -> Any:
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/product.jpg", caption="A product")


def test_delivery_model_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    delivery = ChannelDelivery(**_delivery_kwargs())
    with pytest.raises(ValidationError):
        delivery.question = "changed"


def test_delivery_select_requires_options():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    with pytest.raises(ValidationError, match="non-empty options"):
        ChannelDelivery(**_delivery_kwargs(answer_format="select"))
    ok = ChannelDelivery(**_delivery_kwargs(answer_format="select", options=["yes", "no"]))
    assert ok.options == ["yes", "no"]


def test_delivery_text_allows_suggested_reply_options():
    from tai42_contract.channels import ChannelDelivery

    # TEXT MAY carry options as SUGGESTED REPLIES — a tap submits the option's own text as
    # the free-text answer, so they are an optional enhancement, never a constrained set.
    ok = ChannelDelivery(**_delivery_kwargs(answer_format="text", options=["ok", "later"]))
    assert ok.options == ["ok", "later"]


def test_delivery_text_options_must_be_non_empty_when_present():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    # A present-but-empty options list on a text question is a caller bug, not "no options".
    with pytest.raises(ValidationError, match="non-empty list when present"):
        ChannelDelivery(**_delivery_kwargs(answer_format="text", options=[]))


def test_delivery_non_select_non_text_forbids_options():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    # Only SELECT (required answer set) and TEXT (suggested replies) carry options; every
    # other channel-deliverable format rejects them.
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    for fmt, extra in (("confirm", {}), ("form", {"schema": schema}), ("external", {})):
        with pytest.raises(ValidationError, match="carries no options"):
            ChannelDelivery(**_delivery_kwargs(answer_format=fmt, options=["stray"], **extra))


def test_delivery_unknown_answer_format_rejected():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    with pytest.raises(ValidationError, match="answer_format"):
        ChannelDelivery(**_delivery_kwargs(answer_format="carrier-pigeon"))


def test_delivery_form_answer_format_accepted_with_schema():
    from tai42_contract.channels import ChannelDelivery

    # "form" is channel-deliverable behind the channel's ``supports_form_delivery``
    # flag; the delivery carries the form's JSON answer schema.
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    delivery = ChannelDelivery(**_delivery_kwargs(answer_format="form", schema=schema))
    assert delivery.answer_format == "form"
    assert delivery.schema == schema


def test_delivery_form_requires_schema():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    # A form delivery with no schema (or an empty one) has nothing to render.
    with pytest.raises(ValidationError, match="requires a non-empty schema"):
        ChannelDelivery(**_delivery_kwargs(answer_format="form"))
    with pytest.raises(ValidationError, match="requires a non-empty schema"):
        ChannelDelivery(**_delivery_kwargs(answer_format="form", schema={}))


def test_delivery_non_form_forbids_schema():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    # A schema is meaningful only for "form"; any other format rejects it.
    with pytest.raises(ValidationError, match="carries no schema"):
        ChannelDelivery(**_delivery_kwargs(schema={"type": "object"}))


def test_delivery_accepts_media():
    from tai42_contract.channels import ChannelDelivery

    # A question's display media rides the delivery (full parity with the inbox); it is a
    # pure enhancement, so it is admitted on ANY format the channel deliverer supports.
    item = _image_item()
    delivery = ChannelDelivery(**_delivery_kwargs(media=[item]))
    assert delivery.media == [item]
    # Absent by default — a text-only question carries none.
    assert ChannelDelivery(**_delivery_kwargs()).media is None


def test_delivery_media_rejects_present_but_empty_list():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    with pytest.raises(ValidationError, match="non-empty list when present"):
        ChannelDelivery(**_delivery_kwargs(media=[]))


def test_delivery_media_over_max_items_raises():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery
    from tai42_contract.interactions.models import MEDIA_MAX_ITEMS, MediaItem, MediaKind

    items = [MediaItem(kind=MediaKind.IMAGE, url=f"https://host/{i}.png") for i in range(MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(ValidationError, match=f"at most {MEDIA_MAX_ITEMS} items"):
        ChannelDelivery(**_delivery_kwargs(media=items))


def test_delivery_media_combines_with_select_options():
    from tai42_contract.channels import ChannelDelivery

    # Media (enhancement) and options (the select answer set) are independent — a select
    # question may carry both, the card-with-a-list shape.
    delivery = ChannelDelivery(**_delivery_kwargs(answer_format="select", options=["a", "b"], media=[_image_item()]))
    assert delivery.options == ["a", "b"]
    assert delivery.media is not None


def test_delivery_recipient_defaults_to_none_and_accepts_an_address():
    from tai42_contract.channels import ChannelDelivery

    assert ChannelDelivery(**_delivery_kwargs()).recipient is None
    assert ChannelDelivery(**_delivery_kwargs(recipient="@ops-team")).recipient == "@ops-team"


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_delivery_recipient_rejects_empty_when_present(empty: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    with pytest.raises(ValidationError, match="non-empty address"):
        ChannelDelivery(**_delivery_kwargs(recipient=empty))


def test_delivery_timeout_must_be_tz_aware():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    with pytest.raises(ValidationError, match="timezone-aware"):
        ChannelDelivery(**_delivery_kwargs(timeout_at=datetime(2026, 1, 1)))


def test_channel_delivery_shape():
    from tai42_contract.channels import ChannelDelivery

    # The ask_user delivery path carries the form ``schema``, its per-send ``data`` and
    # ``pages``, and the question's display ``media`` (full parity with the inbox), but
    # never a ``template`` — a template is an out-of-window notification send, not a
    # question delivery.
    assert set(ChannelDelivery.model_fields) == {
        "interaction_id",
        "recipient",
        "question",
        "answer_format",
        "schema",
        "data",
        "pages",
        "options",
        "media",
        "on_mismatch",
        "mismatch_notice",
        "callback_url",
        "timeout_at",
    }
    assert "template" not in ChannelDelivery.model_fields


def test_channel_delivery_carries_form_data_and_pages():
    from datetime import UTC, datetime

    from tai42_contract.channels import ChannelDelivery
    from tai42_contract.interactions import FormData, FormOption, FormPage

    schema = {"type": "object", "properties": {"color": {"type": "string"}}}
    delivery = ChannelDelivery(
        interaction_id="i1",
        question="Pick",
        answer_format="form",
        schema=schema,
        data=FormData(values={"color": "red"}, options={"color": [FormOption(value="red", label="Red")]}),
        pages=[FormPage(title="Step", fields=["color"])],
        callback_url="https://x/api/interactions/callback/t",
        timeout_at=datetime.now(UTC),
    )
    assert delivery.data is not None
    assert delivery.data.values == {"color": "red"}
    assert delivery.data.options["color"][0].label == "Red"
    assert delivery.pages is not None
    assert delivery.pages[0].fields == ["color"]


def test_channel_delivery_rejects_form_extras_on_non_form():
    from datetime import UTC, datetime

    from tai42_contract.channels import ChannelDelivery
    from tai42_contract.interactions import FormData

    with pytest.raises(ValueError, match="carries no form data"):
        ChannelDelivery(
            interaction_id="i1",
            question="Hi",
            answer_format="text",
            data=FormData(values={"x": 1}),
            callback_url="https://x/cb",
            timeout_at=datetime.now(UTC),
        )


def test_ask_user_accepts_channel_and_recipient_keywords():
    from tai42_contract.interactions.asker import AskUser

    params = inspect.signature(AskUser.__call__).parameters
    for name in ("channel", "recipient"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None
    # The channel-delivery group is ordered ``channel`` -> ``recipient`` ->
    # ``on_mismatch`` -> ``mismatch_notice`` -> ``sensitive`` in the call surface.
    ordered = list(params)
    assert ordered[ordered.index("channel") + 1] == "recipient"
    assert ordered[ordered.index("recipient") + 1] == "on_mismatch"
    assert ordered[ordered.index("on_mismatch") + 1] == "mismatch_notice"
    assert ordered[ordered.index("mismatch_notice") + 1] == "sensitive"


def test_ask_user_accepts_on_mismatch_and_mismatch_notice_keywords():
    from tai42_contract.interactions import AnswerMismatchPolicy
    from tai42_contract.interactions.asker import AskUser

    params = inspect.signature(AskUser.__call__).parameters
    # ``on_mismatch`` is the contract's own policy enum, defaulting to RETRY
    # (today's behavior); ``mismatch_notice`` is optional custom retry text.
    on_mismatch = params["on_mismatch"]
    assert on_mismatch.kind is inspect.Parameter.KEYWORD_ONLY
    assert on_mismatch.default is AnswerMismatchPolicy.RETRY
    notice = params["mismatch_notice"]
    assert notice.kind is inspect.Parameter.KEYWORD_ONLY
    assert notice.default is None
