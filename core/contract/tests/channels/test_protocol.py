"""Tests for the ``Channel`` Protocol shape and its optional-member conventions."""

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


def test_signal_working_is_optional_and_does_not_tighten_the_check():
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification

    # ``signal_working`` / ``working_signal_expiry_seconds`` follow the optional-member
    # convention: not Protocol members, so a channel that omits both is still a Channel
    # and getattr yields None; a channel that declares them is read the same way.
    class _NoHook:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    class _WithSignal(_NoHook):
        working_signal_expiry_seconds: float | None = 25.0

        async def signal_working(
            self,
            *,
            recipient: str,
            sender_identity: str | None = None,
            provider_message_id: str | None = None,
            active: bool = True,
        ) -> None:
            return None

    no_hook = _NoHook()
    with_signal = _WithSignal()
    assert isinstance(no_hook, Channel)
    assert isinstance(with_signal, Channel)
    assert getattr(no_hook, "working_signal_expiry_seconds", None) is None
    assert getattr(with_signal, "working_signal_expiry_seconds", None) == 25.0
    assert getattr(no_hook, "signal_working", None) is None
    assert getattr(with_signal, "signal_working", None) is not None
    # Neither is a Protocol member.
    assert not hasattr(Channel, "working_signal_expiry_seconds")
    assert not hasattr(Channel, "signal_working")


def test_withdraw_is_optional_and_does_not_tighten_the_check():
    from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification, ChannelWithdrawal

    # ``withdraw`` follows the optional-member convention: not a Protocol member, so a
    # channel that keeps nothing withdrawable omits it and is still a Channel; a channel
    # that declares it is read the same way, with ``getattr(channel, "withdraw", None)``.
    class _NoWithdraw:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    class _WithWithdraw(_NoWithdraw):
        def __init__(self) -> None:
            self.withdrawn: list[ChannelWithdrawal] = []

        async def withdraw(self, withdrawal: ChannelWithdrawal) -> None:
            self.withdrawn.append(withdrawal)

    no_withdraw = _NoWithdraw()
    with_withdraw = _WithWithdraw()
    assert isinstance(no_withdraw, Channel)
    assert isinstance(with_withdraw, Channel)
    assert getattr(no_withdraw, "withdraw", None) is None
    member = getattr(with_withdraw, "withdraw", None)
    assert member is not None
    asyncio.run(member(ChannelWithdrawal(interaction_id="int-1", recipient="alice")))
    assert with_withdraw.withdrawn == [ChannelWithdrawal(interaction_id="int-1", recipient="alice")]
    assert not hasattr(Channel, "withdraw")
