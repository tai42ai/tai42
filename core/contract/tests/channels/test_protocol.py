"""Tests for the ``Channel`` Protocol shape, its optional-member conventions, and
``notify_in_order`` (the default sequential in-order primitive)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest


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


# -- notify_in_order: the default sequential in-order primitive ------------------


class _RecordingChannel:
    """A channel that records the order it was asked to notify in, returning one id per
    send. ``fail_on`` raises after recording the nth message (1-based) to stand in for a
    provider refusing mid-sequence."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.sent: list[str] = []
        self._fail_on = fail_on

    async def deliver(self, delivery: Any) -> None:  # pragma: no cover - unused here
        return None

    async def notify(self, notification: Any) -> list[str]:
        self.sent.append(notification.message)
        if self._fail_on is not None and len(self.sent) == self._fail_on:
            from tai42_contract.channels import ChannelDeliveryError

            raise ChannelDeliveryError("provider refused")
        return [f"id-{len(self.sent)}"]


def _notifications(*messages: str) -> list[Any]:
    from tai42_contract.channels import ChannelNotification

    return [ChannelNotification(message=m) for m in messages]


def test_notify_in_order_is_exported():
    import tai42_contract.channels as channels_module

    assert "notify_in_order" in channels_module.__all__


def test_notify_in_order_delivers_sequentially_and_returns_ids_in_order():
    from tai42_contract.channels import notify_in_order

    channel = _RecordingChannel()
    seen: list[tuple[int, list[str]]] = []
    results = asyncio.run(
        notify_in_order(channel, _notifications("one", "two", "three"), on_sent=lambda i, ids: seen.append((i, ids)))
    )

    # Strictly in order, one id list per notification, and the progress hook fired once per
    # send in send order.
    assert channel.sent == ["one", "two", "three"]
    assert results == [["id-1"], ["id-2"], ["id-3"]]
    assert seen == [(0, ["id-1"]), (1, ["id-2"]), (2, ["id-3"])]


def test_notify_in_order_stops_at_the_first_failure():
    from tai42_contract.channels import ChannelDeliveryError, notify_in_order

    channel = _RecordingChannel(fail_on=2)
    seen: list[int] = []
    with pytest.raises(ChannelDeliveryError):
        asyncio.run(
            notify_in_order(channel, _notifications("one", "two", "three"), on_sent=lambda i, _ids: seen.append(i))
        )

    # It stopped at the raise: the third message never went out and its progress never fired.
    assert channel.sent == ["one", "two"]
    assert seen == [0]  # only the first send's progress was recorded before the raise


def test_notify_in_order_dispatches_to_a_native_deliver_ordered_when_declared():
    from tai42_contract.channels import notify_in_order

    class _BatchChannel:
        """A channel that declares the OPTIONAL ``deliver_ordered`` native batch member."""

        def __init__(self) -> None:
            self.batches: list[list[str]] = []

        async def deliver(self, delivery: Any) -> None:  # pragma: no cover - unused here
            return None

        async def notify(self, notification: Any) -> list[str]:  # pragma: no cover - never used here
            raise AssertionError("notify_in_order must use the native deliver_ordered batch")

        async def deliver_ordered(self, notifications: Any) -> list[list[str]]:
            messages = [n.message for n in notifications]
            self.batches.append(messages)
            return [[f"batch-{i}"] for i in range(len(messages))]

    channel = _BatchChannel()
    seen: list[tuple[int, list[str]]] = []
    results = asyncio.run(
        notify_in_order(channel, _notifications("a", "b"), on_sent=lambda i, ids: seen.append((i, ids)))
    )

    # The whole batch went to the native member, never the per-message notify loop, and the
    # progress hook still fired once per message in order.
    assert channel.batches == [["a", "b"]]
    assert results == [["batch-0"], ["batch-1"]]
    assert seen == [(0, ["batch-0"]), (1, ["batch-1"])]
