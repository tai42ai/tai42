"""Channel-spec fixtures: the per-medium ``channel_case`` parametrization and a
per-spec stub reset. The adapters and helpers live in ``_support`` (imported here
and by the spec modules); the ``channel_stack`` / ``fake_*`` fixtures come from
the top-level ``tests/conftest.py``."""

from __future__ import annotations

import pytest
from _support import CASE_CLASSES, ChannelCase

from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.stack import TaiStack


@pytest.fixture(autouse=True)
def _reset_channel_stubs(fake_telegram: FakeTelegram, fake_slack: FakeSlack, fake_twilio: FakeTwilio) -> None:
    """Clear every stub's records before each channel spec — combined with the
    per-spec ``uniq`` text filter, an assertion never leans on shared state."""
    fake_telegram.reset()
    fake_slack.reset()
    fake_twilio.reset()


@pytest.fixture(params=["telegram", "slack", "twilio"])
def channel_case(
    request: pytest.FixtureRequest,
    channel_stack: TaiStack,
    fake_telegram: FakeTelegram,
    fake_slack: FakeSlack,
    fake_twilio: FakeTwilio,
) -> ChannelCase:
    stub = {"telegram": fake_telegram, "slack": fake_slack, "twilio": fake_twilio}[request.param]
    return CASE_CLASSES[request.param](channel_stack, stub)  # type: ignore[arg-type]
