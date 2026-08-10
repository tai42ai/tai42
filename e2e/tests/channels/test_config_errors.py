"""Item E — the delivery-time config error, and the boot-abort half.

Decision (A) splits misconfiguration by WHERE it surfaces. On the deliver/notify
path EVERY failure — operator misconfiguration included — is a
``ChannelDeliveryError`` (retyped from the config check), so a booted stack whose
default recipient is unset fails a recipient-less send LOUDLY at delivery time,
nothing sent, the ask pruned. The startup path keeps its own error types: the
telegram plugin's ``on_startup`` hook REQUIRES the bot token and aborts boot
without it (a channel that cannot receive replies never boots half-deaf). Slack
and twilio have no startup hook, so a missing token there is only a delivery-time
error, not a boot abort — the boot-abort half is telegram-specific by design.

A delivery-time missing-TOKEN error is reachable in production only via
live-reload credential rotation after boot; exercising that would add a
reload-choreography spec for one message assertion — consciously out of scope."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _support import CASE_CLASSES, ChannelCase, is_pending, tool_content_text

from tai42_e2e.manifests import build_channel_stack
from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


def _stub_kwargs(telegram: FakeTelegram, slack: FakeSlack, twilio: FakeTwilio) -> dict[str, str]:
    return {
        "telegram_api_base_url": telegram.api_base_url,
        "slack_api_base_url": slack.api_base_url,
        "twilio_api_base_url": twilio.api_base_url,
    }


@pytest.mark.parametrize("channel", ["telegram", "slack", "twilio"])
async def test_missing_default_recipient_is_a_delivery_error(
    channel: str,
    fresh_stack: Callable[..., TaiStack],
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    fake_twilio: FakeTwilio,
    uniq: Callable[[str], str],
) -> None:
    default_env = f"CHANNEL_{channel.upper()}_DEFAULT_RECIPIENT"
    # Boot with this medium's default recipient UNSET (empty -> None) but its
    # allowlist still set, so telegram's startup hook passes and the stack boots.
    stack = fresh_stack(
        build_channel_stack,
        resource_kwargs=_stub_kwargs(fake_telegram, fake_slack, fake_twilio),
        env_overrides={default_env: ""},
    )
    case: ChannelCase = CASE_CLASSES[channel](
        stack, {"telegram": fake_telegram, "slack": fake_slack, "twilio": fake_twilio}[channel]
    )

    # A recipient-less ask resolves to the (now unset) default -> a delivery
    # error naming the env var (a ChannelDeliveryError retyping, NOT a raw
    # ValueError surface); nothing sent, the ask pruned.
    question = uniq(f"{channel}_q")
    async with stack.mcp(port=stack.port_a) as mcp:
        ask_result = await mcp.call_tool("ask_user", {"question": question, "channel": channel}, raise_on_error=False)
    assert ask_result.is_error
    assert default_env in tool_content_text(ask_result)
    assert case.sends_matching(question) == []
    assert not await is_pending(stack, stack.port_a, question)
    assert not await is_pending(stack, stack.port_b, question)

    # notify_user hits the same resolution -> same delivery error, same zero-send,
    # nothing stored (notify has nothing to prune).
    message = uniq(f"{channel}_notify")
    async with stack.mcp(port=stack.port_a) as mcp:
        notify_result = await mcp.call_tool(
            "notify_user", {"message": message, "channel": channel}, raise_on_error=False
        )
    assert notify_result.is_error
    assert default_env in tool_content_text(notify_result)
    assert case.sends_matching(message) == []


async def test_missing_bot_token_aborts_boot(
    fresh_stack: Callable[..., TaiStack],
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    fake_twilio: FakeTwilio,
) -> None:
    # The telegram startup hook requires the bot token; without it every serve
    # worker exits early and the readiness wait surfaces the child's failure with
    # the token error in the log tail — a channel that cannot receive replies
    # aborts startup rather than booting half-deaf. (Slack/twilio have no startup
    # hook, so this half is telegram-specific.)
    with pytest.raises(RuntimeError, match="CHANNEL_TELEGRAM_BOT_TOKEN"):
        fresh_stack(
            build_channel_stack,
            resource_kwargs=_stub_kwargs(fake_telegram, fake_slack, fake_twilio),
            env_overrides={"CHANNEL_TELEGRAM_BOT_TOKEN": ""},
        )
