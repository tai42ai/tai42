"""Tests for the channel contract types.

The ``AppChannels`` protocol membership is covered by the frozen-facade
partition test; here we pin the typed error, the ``Channel`` Protocol shape
(``deliver`` + ``notify``), the ``ChannelDelivery`` model's frozen +
per-format + recipient validation, the ``ChannelNotification`` model's
frozen + message + recipient + sender_identity validation, the ``notify``
list-of-ids return, and the ``AskUser`` ``channel`` / ``recipient`` keywords.
"""

from __future__ import annotations

import asyncio
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


def test_delivery_error_is_a_distinct_exception_type():
    from tai42_contract.channels import ChannelDeliveryError

    assert issubclass(ChannelDeliveryError, Exception)
    # A distinct type so the ask helper can catch delivery failure without also
    # swallowing unrelated errors.
    assert ChannelDeliveryError is not Exception
    with pytest.raises(ChannelDeliveryError):
        raise ChannelDeliveryError("send rejected")


def test_input_error_is_distinct_from_delivery_error():
    from tai42_contract.channels import ChannelDeliveryError, ChannelInputError

    assert issubclass(ChannelInputError, Exception)
    # NOT a ChannelDeliveryError: a permanent input refusal must never be caught by a
    # delivery-failure handler and mapped to a retryable 502.
    assert not issubclass(ChannelInputError, ChannelDeliveryError)
    assert not issubclass(ChannelDeliveryError, ChannelInputError)
    with pytest.raises(ChannelInputError):
        raise ChannelInputError("cannot render a data: image URL")


def test_delivery_error_defaults_to_non_retryable():
    from tai42_contract.channels import ChannelDeliveryError

    # An unclassified failure — the message-only form — is never blind-retried.
    error = ChannelDeliveryError("send rejected")
    assert str(error) == "send rejected"
    assert error.retryable is False
    assert error.retry_after is None


def test_delivery_error_carries_the_transient_classification():
    from tai42_contract.channels import ChannelDeliveryError

    error = ChannelDeliveryError("throttled", retryable=True, retry_after=7.5)
    assert str(error) == "throttled"
    assert error.retryable is True
    assert error.retry_after == 7.5


def test_channel_protocol_is_runtime_checkable_and_shaped():
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification

    class _Ok:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> None:
            return None

    class _DeliverOnly:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

    class _Missing:
        pass

    assert isinstance(_Ok(), Channel)
    # ``notify`` is a required protocol member, not an optional extra.
    assert not isinstance(_DeliverOnly(), Channel)
    assert not isinstance(_Missing(), Channel)


def test_deliver_is_a_coroutine_signature():
    from tai42_contract.channels import Channel

    assert inspect.iscoroutinefunction(Channel.deliver)
    sig = inspect.signature(Channel.deliver)
    assert list(sig.parameters) == ["self", "delivery"]


def test_notify_is_a_coroutine_signature():
    from tai42_contract.channels import Channel

    assert inspect.iscoroutinefunction(Channel.notify)
    sig = inspect.signature(Channel.notify)
    assert list(sig.parameters) == ["self", "notification"]


def test_notify_returns_a_list_of_message_ids():
    from tai42_contract.channels import Channel

    # ``notify`` yields the per-message ids the medium assigned the send, so an
    # out-of-band delivery receipt can be correlated back to what was sent.
    sig = inspect.signature(Channel.notify)
    assert sig.return_annotation == "list[str]"


def test_notify_protocol_default_raises_not_implemented():
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification

    class _DeliverCapableOnly(Channel):
        """Explicit subclass keeping the inherited ``notify`` default body."""

        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

    # Pyright treats the ``raise NotImplementedError`` protocol body as abstract;
    # the contract supplies it as a real, loud default, so instantiating is intended.
    channel = _DeliverCapableOnly()  # pyright: ignore[reportAbstractUsage]
    with pytest.raises(NotImplementedError):
        asyncio.run(channel.notify(ChannelNotification(message="ping")))


def test_notification_model_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="deploy finished")
    with pytest.raises(ValidationError):
        notification.message = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_rejects_blank_message(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-blank"):
        ChannelNotification(message=blank)


def test_notification_message_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_MESSAGE_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_MESSAGE_MAX_CHARS
    assert ChannelNotification(message=at_cap).message == at_cap
    with pytest.raises(ValidationError, match="at most"):
        ChannelNotification(message="x" * (NOTIFICATION_MESSAGE_MAX_CHARS + 1))


def test_notification_recipient_defaults_to_none_and_accepts_an_address():
    from tai42_contract.channels import ChannelNotification

    assert ChannelNotification(message="hi").recipient is None
    assert ChannelNotification(message="hi", recipient="@ops-team").recipient == "@ops-team"


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_notification_recipient_rejects_empty_when_present(empty: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-empty address"):
        ChannelNotification(message="hi", recipient=empty)


def test_notification_sender_identity_defaults_to_none_and_accepts_an_address():
    from tai42_contract.channels import ChannelNotification

    assert ChannelNotification(message="hi").sender_identity is None
    assert ChannelNotification(message="hi", sender_identity="+15550001111").sender_identity == "+15550001111"


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_notification_sender_identity_rejects_empty_when_present(empty: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-empty address"):
        ChannelNotification(message="hi", sender_identity=empty)


def test_notification_recipient_capped():
    # recipient is a short routing value that persists into the replayed record, so it
    # is length-capped just as the message is.
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_ADDRESS_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS
    assert ChannelNotification(message="hi", recipient=at_cap).recipient == at_cap
    with pytest.raises(ValidationError, match="address must be at most"):
        ChannelNotification(message="hi", recipient="x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1))


def test_notification_sender_identity_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_ADDRESS_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_ADDRESS_MAX_CHARS
    assert ChannelNotification(message="hi", sender_identity=at_cap).sender_identity == at_cap
    with pytest.raises(ValidationError, match="address must be at most"):
        ChannelNotification(message="hi", sender_identity="x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1))


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


def test_delivery_non_select_forbids_options():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelDelivery

    with pytest.raises(ValidationError, match="carries no options"):
        ChannelDelivery(**_delivery_kwargs(options=["stray"]))


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


def _image_item() -> Any:
    from tai42_contract.interactions.models import MediaItem, MediaKind

    return MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/product.jpg", caption="A product")


def test_notification_accepts_media():
    from tai42_contract.channels import ChannelNotification

    item = _image_item()
    notification = ChannelNotification(message="here it is", media=[item])
    assert notification.media == [item]
    # Absent by default; freeform text needs neither field.
    assert ChannelNotification(message="hi").media is None


def test_notification_media_rejects_present_but_empty_list():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-empty list"):
        ChannelNotification(message="hi", media=[])


def test_notification_media_over_max_items_raises():
    # The item-count cap mirrors InteractionRequest — one notification cannot fan out an
    # unbounded media list into the durable record and the frame it replays in.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_MAX_ITEMS, MediaItem, MediaKind

    items = [MediaItem(kind=MediaKind.IMAGE, url=f"https://host/{i}.png") for i in range(MEDIA_MAX_ITEMS + 1)]
    with pytest.raises(ValidationError, match=f"at most {MEDIA_MAX_ITEMS} items"):
        ChannelNotification(message="hi", media=items)


def test_notification_media_at_max_items_accepted():
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_MAX_ITEMS, MediaItem, MediaKind

    items = [MediaItem(kind=MediaKind.IMAGE, url=f"https://host/{i}.png") for i in range(MEDIA_MAX_ITEMS)]
    notification = ChannelNotification(message="hi", media=items)
    assert notification.media is not None
    assert len(notification.media) == MEDIA_MAX_ITEMS


def test_notification_media_total_uri_budget_raises():
    # Each item is within the per-item data: cap, but the summed url text exceeds the
    # per-notification MEDIA_TOTAL_URI_CHARS budget.
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_DATA_URI_MAX_CHARS, MEDIA_TOTAL_URI_CHARS, MediaItem, MediaKind

    per_item = "data:image/png;base64," + "A" * 400_000
    assert len(per_item) <= MEDIA_DATA_URI_MAX_CHARS
    items = [MediaItem(kind=MediaKind.IMAGE, url=per_item) for _ in range(3)]
    assert sum(len(item.url) for item in items) > MEDIA_TOTAL_URI_CHARS
    with pytest.raises(ValidationError, match=f"total url length must be at most {MEDIA_TOTAL_URI_CHARS}"):
        ChannelNotification(message="hi", media=items)


def test_notification_media_total_uri_budget_at_cap_accepted():
    # The total budget is a strict ``>``; a list summing to EXACTLY MEDIA_TOTAL_URI_CHARS
    # is accepted (guards a ``>`` -> ``>=`` regression).
    from tai42_contract.channels import ChannelNotification
    from tai42_contract.interactions.models import MEDIA_TOTAL_URI_CHARS, MediaItem, MediaKind

    prefix = "data:image/png;base64,"
    half = MEDIA_TOTAL_URI_CHARS // 2
    url_a = prefix + "A" * (half - len(prefix))
    url_b = prefix + "B" * (MEDIA_TOTAL_URI_CHARS - half - len(prefix))
    items = [MediaItem(kind=MediaKind.IMAGE, url=url_a), MediaItem(kind=MediaKind.IMAGE, url=url_b)]
    assert sum(len(item.url) for item in items) == MEDIA_TOTAL_URI_CHARS
    notification = ChannelNotification(message="hi", media=items)
    assert notification.media is not None
    assert len(notification.media) == 2


def test_template_roundtrips_on_a_notification():
    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    template = ChannelTemplate(name="status_update", language="en_US", parameters=["A-42", "done"])
    notification = ChannelNotification(message="Your item is done", template=template)
    assert notification.template is not None
    assert notification.template == template
    assert notification.template.name == "status_update"
    assert notification.template.language == "en_US"
    assert notification.template.parameters == ["A-42", "done"]
    # parameters is optional — a template with no body placeholders is valid.
    assert ChannelTemplate(name="ping", language="en").parameters == []


def test_template_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    template = ChannelTemplate(name="status_update", language="en_US")
    with pytest.raises(ValidationError):
        template.name = "changed"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_template_rejects_blank_name(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    with pytest.raises(ValidationError, match="non-blank"):
        ChannelTemplate(name=blank, language="en_US")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_template_rejects_blank_language(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelTemplate

    with pytest.raises(ValidationError, match="non-blank"):
        ChannelTemplate(name="status_update", language=blank)


def test_notification_media_and_template_are_mutually_exclusive():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(
            message="both set",
            media=[_image_item()],
            template=ChannelTemplate(name="status_update", language="en_US"),
        )


def test_notification_accepts_options():
    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="pick one", options=["Item A", "Item B"])
    assert notification.options == ["Item A", "Item B"]
    # Absent by default; freeform text needs no options.
    assert ChannelNotification(message="hi").options is None


def test_notification_options_rejects_present_but_empty_list():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="non-empty list"):
        ChannelNotification(message="hi", options=[])


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_notification_options_rejects_a_blank_option(blank: str):
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification

    with pytest.raises(ValidationError, match="each option must be a non-blank string"):
        ChannelNotification(message="hi", options=["Item A", blank])


def test_notification_options_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_OPTIONS_MAX, ChannelNotification

    ok = [f"Item {n}" for n in range(NOTIFICATION_OPTIONS_MAX)]
    assert ChannelNotification(message="hi", options=ok).options == ok
    with pytest.raises(ValidationError, match=f"options carries at most {NOTIFICATION_OPTIONS_MAX} entries"):
        ChannelNotification(message="hi", options=[*ok, "one too many"])


def test_notification_option_length_capped():
    from pydantic import ValidationError

    from tai42_contract.channels import NOTIFICATION_OPTION_MAX_CHARS, ChannelNotification

    at_cap = "x" * NOTIFICATION_OPTION_MAX_CHARS
    assert ChannelNotification(message="hi", options=[at_cap]).options == [at_cap]
    with pytest.raises(ValidationError, match="at most"):
        ChannelNotification(message="hi", options=["x" * (NOTIFICATION_OPTION_MAX_CHARS + 1)])


def test_notification_options_and_template_are_mutually_exclusive():
    from pydantic import ValidationError

    from tai42_contract.channels import ChannelNotification, ChannelTemplate

    with pytest.raises(ValidationError, match="mutually exclusive"):
        ChannelNotification(
            message="both set",
            options=["Item A"],
            template=ChannelTemplate(name="status_update", language="en_US"),
        )


def test_notification_options_may_combine_with_media():
    from tai42_contract.channels import ChannelNotification

    notification = ChannelNotification(message="a card with a list", media=[_image_item()], options=["Item A"])
    assert notification.media == [_image_item()]
    assert notification.options == ["Item A"]


def test_channel_capability_flags_are_an_optional_getattr_convention():
    from tai42_contract.channels import Channel

    # The flags are NOT Protocol members — the Channel Protocol carries no
    # class-level default, so a channel that sets them opts in explicitly.
    assert not hasattr(Channel, "supports_media_notifications")
    assert not hasattr(Channel, "supports_template_notifications")
    assert not hasattr(Channel, "supports_interactive_notifications")

    class _Rich(Channel):
        supports_media_notifications = True
        supports_template_notifications = True

        async def deliver(self, delivery: Any) -> None:
            return None

        async def notify(self, notification: Any) -> list[str]:
            return []

    rich = _Rich()
    # A channel that sets the flags = True advertises support.
    assert getattr(rich, "supports_media_notifications", False) is True
    assert getattr(rich, "supports_template_notifications", False) is True


def test_capability_flags_do_not_tighten_structural_channel_check():
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification

    # A text-only channel that never declares the optional capability flags is
    # still a Channel — the flags are not Protocol members, so they play no part
    # in the runtime structural check, and getattr yields the False default.
    class _TextOnly:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    text_only = _TextOnly()
    assert isinstance(text_only, Channel)
    assert not hasattr(text_only, "supports_media_notifications")
    assert getattr(text_only, "supports_media_notifications", False) is False
    assert getattr(text_only, "supports_template_notifications", False) is False


def test_validate_form_schema_hook_is_optional_and_does_not_tighten_the_check():
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification

    # ``validate_form_schema`` follows the capability-flag convention: an optional
    # member, NOT a Protocol method, so a channel that omits it is still a Channel
    # and getattr yields None; a channel that declares it is read the same way.
    class _NoHook:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    class _WithHook(_NoHook):
        def validate_form_schema(self, schema: dict[str, Any], question: str) -> None:
            raise ValueError("nope")

    no_hook = _NoHook()
    with_hook = _WithHook()
    assert isinstance(no_hook, Channel)
    assert isinstance(with_hook, Channel)
    assert getattr(no_hook, "validate_form_schema", None) is None
    assert getattr(with_hook, "validate_form_schema", None) is not None
    with pytest.raises(ValueError, match="nope"):
        with_hook.validate_form_schema({"type": "object"}, "q")


def test_channel_delivery_shape_is_unchanged():
    from tai42_contract.channels import ChannelDelivery

    # The ask_user delivery path carries the form ``schema`` but deliberately gains
    # NO media/template fields.
    assert set(ChannelDelivery.model_fields) == {
        "interaction_id",
        "recipient",
        "question",
        "answer_format",
        "schema",
        "options",
        "callback_url",
        "timeout_at",
    }
    assert "media" not in ChannelDelivery.model_fields
    assert "template" not in ChannelDelivery.model_fields


def test_ask_user_accepts_channel_and_recipient_keywords():
    from tai42_contract.interactions.asker import AskUser

    params = inspect.signature(AskUser.__call__).parameters
    for name in ("channel", "recipient"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None
    # ``recipient`` sits between ``channel`` and ``sensitive`` in the ordered
    # call surface.
    ordered = list(params)
    assert ordered[ordered.index("channel") + 1] == "recipient"
    assert ordered[ordered.index("recipient") + 1] == "sensitive"


# -- Correlation: the per-address record of ONE parked ask awaiting a reply ------


def _correlation_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "callback_url": "https://host.example/api/interactions/callback/tkt",
        "interaction_id": "int-1",
        "ttl_deadline": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_correlation_roundtrips_its_fields():
    from tai42_contract.channels import Correlation

    entry = Correlation(**_correlation_kwargs())
    assert entry.callback_url == "https://host.example/api/interactions/callback/tkt"
    assert entry.interaction_id == "int-1"
    assert entry.ttl_deadline == datetime(2026, 1, 1, tzinfo=UTC)


def test_correlation_is_frozen():
    from pydantic import ValidationError

    from tai42_contract.channels import Correlation

    entry = Correlation(**_correlation_kwargs())
    with pytest.raises(ValidationError):
        entry.interaction_id = "other"


def test_correlation_ttl_deadline_must_be_tz_aware():
    from pydantic import ValidationError

    from tai42_contract.channels import Correlation

    with pytest.raises(ValidationError, match="timezone-aware"):
        Correlation(**_correlation_kwargs(ttl_deadline=datetime(2026, 1, 1)))


def test_correlation_ttl_deadline_normalized_to_utc():
    from datetime import timedelta, timezone

    from tai42_contract.channels import Correlation

    plus_two = timezone(timedelta(hours=2))
    entry = Correlation(**_correlation_kwargs(ttl_deadline=datetime(2026, 1, 1, 3, tzinfo=plus_two)))
    # Normalized to UTC, matching ChannelDelivery.timeout_at.
    assert entry.ttl_deadline == datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert entry.ttl_deadline.tzinfo is UTC


# -- CorrelationStore: a standalone optional runtime_checkable port --------------


def test_correlation_store_is_runtime_checkable_and_shaped():
    from tai42_contract.channels import Correlation, CorrelationStore

    class _Ok:
        async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
            return True

        async def get_correlation(self, key: str) -> Correlation | None:
            return None

        async def release_correlation(self, key: str) -> None:
            return None

    class _MissingRelease:
        async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
            return True

        async def get_correlation(self, key: str) -> Correlation | None:
            return None

    # A fake implementing all three methods conforms structurally; one missing a
    # method does not — the port is the full set/get/release surface, never partial.
    assert isinstance(_Ok(), CorrelationStore)
    assert not isinstance(_MissingRelease(), CorrelationStore)
    assert not isinstance(object(), CorrelationStore)


def test_correlation_store_is_not_an_extension_of_channel():
    # The port stands alone: a Channel is NOT structurally a CorrelationStore and a
    # CorrelationStore is NOT structurally a Channel — a channel implements the store
    # separately (or not at all), never off the channel instance.
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification, Correlation, CorrelationStore

    class _Channel:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    class _Store:
        async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
            return True

        async def get_correlation(self, key: str) -> Correlation | None:
            return None

        async def release_correlation(self, key: str) -> None:
            return None

    assert isinstance(_Channel(), Channel)
    assert not isinstance(_Channel(), CorrelationStore)
    assert isinstance(_Store(), CorrelationStore)
    assert not isinstance(_Store(), Channel)


def test_correlation_types_exported():
    import tai42_contract.channels as channels_module

    assert "Correlation" in channels_module.__all__
    assert "CorrelationStore" in channels_module.__all__
