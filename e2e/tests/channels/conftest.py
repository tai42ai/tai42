"""Channel-spec fixtures: the per-medium ``channel_case`` parametrization and a
per-spec stub reset. The adapters and helpers live in ``_support`` (imported here
and by the spec modules); the ``channel_stack`` / ``fake_*`` fixtures come from
the top-level ``tests/conftest.py``."""

from __future__ import annotations

import pytest
from _support import CASE_CLASSES, ChannelCase

from tai42_e2e.netfixtures import FakeSlack, FakeTelegram, FakeTwilio
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# The stub-delivery-asserting channel specs read outbound sends off the in-process
# FakeSlack/FakeTelegram/FakeTwilio (``case.sends_matching``). Under ``TAI_E2E_REAL=<channel>``
# ``build_channel_stack`` drops ``CHANNEL_<X>_API_BASE_URL`` and the plugin talks to the live
# vendor, so those sends never land on the stub and the wait would false-fail. Those specs are
# the channel MOCK leg; the real channel leg is exercised on the dedicated e2e creds host
# (PLAN_2 §F), not in CI. The real-safe NEGATIVES (which assert ``sends_matching(...) == []``)
# stay — a real send simply never lands, which is exactly what they assert — so they are NOT
# listed here. Value ``None`` = every test in the module asserts delivery.
_STUB_DELIVERY_SPECS: dict[str, set[str] | None] = {
    "test_channel_hitl_cross_worker": None,
    "test_notify": {"test_notify_external_is_plain_and_stateless"},
    "test_recipient_allowlist": {"test_allowlisted_recipient_is_delivered_to"},
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Step the matching channel's ``channel_case`` parametrization aside for the
    stub-delivery specs when that channel is real-selected. Keyed on the PER-ITEM param, so
    ``TAI_E2E_REAL=slack`` skips only the slack case of those specs — never another channel's
    case, and never the real-safe negatives. Inert while ``TAI_E2E_REAL`` is empty (no channel
    is real), so collection is byte-for-byte today's."""
    switch = HarnessSettings()
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            continue
        channel = callspec.params.get("channel_case")
        if not isinstance(channel, str) or not switch.is_real(channel):
            continue
        stem = item.path.stem
        if stem not in _STUB_DELIVERY_SPECS:
            continue
        names = _STUB_DELIVERY_SPECS[stem]
        original = getattr(item, "originalname", item.name)
        if names is None or original in names:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"{channel!r} stub-delivery is the channel mock leg; real leg on the creds host (PLAN_2 §F)"
                )
            )


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
