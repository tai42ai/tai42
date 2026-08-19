"""Signature verification: HMAC over the exact raw bytes, constant-time,
uniform rejects, fail-closed on misconfiguration, bounded body before any work.

Every case drives the real route handler via ``make_request`` — there is no
verification bypass anywhere in the suite.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tai42_channel_slack import inbound

from .conftest import TEST_SIGNING_SECRET, body_json, make_request, signed_headers

pytestmark = pytest.mark.usefixtures("slack_env")

# A deliberately non-JSON body: a request that PASSES verification answers 400
# ("body must be a JSON object"), which proves the signature check succeeded
# without engaging the event flow.
_RAW_BODY = b"not-json"

_REJECT_BODY = {"error": "signature verification failed"}

_FROZEN_NOW = 2_000_000_000


@pytest.fixture
def frozen_time(monkeypatch: pytest.MonkeyPatch) -> int:
    """Pin ``time.time()`` inside the inbound module so replay-window boundary
    assertions are exact, never racing the wall clock."""
    monkeypatch.setattr(inbound, "time", SimpleNamespace(time=lambda: float(_FROZEN_NOW)))
    return _FROZEN_NOW


async def test_valid_signature_passes_to_body_logic():
    response = await inbound.slack_inbound(make_request(_RAW_BODY, signed_headers(_RAW_BODY, TEST_SIGNING_SECRET)))
    assert response.status_code == 400
    assert body_json(response) == {"error": "body must be a JSON object"}


def _tampered_cases() -> dict[str, dict[str, str]]:
    now = int(time.time())
    valid = signed_headers(_RAW_BODY, TEST_SIGNING_SECRET)
    return {
        "missing timestamp header": {"X-Slack-Signature": valid["X-Slack-Signature"]},
        "non-integer timestamp": {**valid, "X-Slack-Request-Timestamp": "not-a-number"},
        # Bare int() would parse a NBSP-prefixed value (int("\xa0123") == 123),
        # but the ascii-encode of the signing base cannot carry it — the
        # ASCII-digit gate must turn it into the same uniform 401, never a
        # UnicodeEncodeError escaping as a 500.
        "non-ascii NBSP-prefixed timestamp": {
            **valid,
            "X-Slack-Request-Timestamp": "\xa0" + valid["X-Slack-Request-Timestamp"],
        },
        "non-ascii digit timestamp": {**valid, "X-Slack-Request-Timestamp": "\xb9" * 10},
        # Far outside the skew window so the reject reason is timing-independent —
        # these cases are captured once at parametrize time, and the exact
        # one-second boundary is pinned deterministically by
        # ``test_timestamp_just_outside_window_rejected`` under frozen time.
        "timestamp far in the past": signed_headers(_RAW_BODY, TEST_SIGNING_SECRET, timestamp=now - 100_000),
        "timestamp far in the future": signed_headers(_RAW_BODY, TEST_SIGNING_SECRET, timestamp=now + 100_000),
        "missing signature header": {"X-Slack-Request-Timestamp": valid["X-Slack-Request-Timestamp"]},
        "wrong prefix": {**valid, "X-Slack-Signature": "v1=" + valid["X-Slack-Signature"][3:]},
        "digest not 64 chars": {**valid, "X-Slack-Signature": "v0=abc123"},
        "digest not hex": {**valid, "X-Slack-Signature": "v0=" + "z" * 64},
        "digest mismatch": {**valid, "X-Slack-Signature": "v0=" + "0" * 64},
        "signature valid for different body": signed_headers(b"other-bytes", TEST_SIGNING_SECRET),
    }


@pytest.mark.parametrize("case", _tampered_cases())
async def test_fail_closed_matrix_uniform_401(case):
    # Every reject shares one constant body — no oracle distinguishing the
    # failure modes; all answer the same uniform 401.
    headers = _tampered_cases()[case]

    response = await inbound.slack_inbound(make_request(_RAW_BODY, headers))

    assert response.status_code == 401
    assert body_json(response) == _REJECT_BODY


async def test_timestamp_exactly_300s_old_is_accepted(frozen_time):
    headers = signed_headers(_RAW_BODY, TEST_SIGNING_SECRET, timestamp=frozen_time - 300)

    response = await inbound.slack_inbound(make_request(_RAW_BODY, headers))

    # Past verification: the non-JSON body answers 400, not 401.
    assert response.status_code == 400


@pytest.mark.parametrize("skew", [-301, 301])
async def test_timestamp_just_outside_window_rejected(frozen_time, skew):
    headers = signed_headers(_RAW_BODY, TEST_SIGNING_SECRET, timestamp=frozen_time + skew)

    response = await inbound.slack_inbound(make_request(_RAW_BODY, headers))

    assert response.status_code == 401
    assert body_json(response) == _REJECT_BODY


async def test_unset_signing_secret_handled_500_not_401(no_slack_env, caplog):
    # Misconfiguration fails CLOSED as a handled, logged 500 with a constant
    # body — never an open door, never an ordinary 401 reject, and never a
    # stack trace leaked to the client.
    response = await inbound.slack_inbound(make_request(_RAW_BODY, signed_headers(_RAW_BODY, TEST_SIGNING_SECRET)))

    assert response.status_code == 500
    assert body_json(response) == {"error": "channel misconfigured"}
    assert "CHANNEL_SLACK_SIGNING_SECRET" in caplog.text


async def test_empty_signing_secret_fails_closed(monkeypatch):
    # The env path cannot produce SecretStr("") (env_ignore_empty), so the
    # accessor is stubbed — an empty key would make the HMAC forgeable, so it
    # gets the same constant 500 as an unset one.
    from pydantic import SecretStr

    monkeypatch.setattr(inbound, "slack_settings", lambda: SimpleNamespace(signing_secret=SecretStr(""), channel="C1"))

    response = await inbound.slack_inbound(make_request(_RAW_BODY, signed_headers(_RAW_BODY, TEST_SIGNING_SECRET)))

    assert response.status_code == 500
    assert body_json(response) == {"error": "channel misconfigured"}


async def test_oversized_body_413_before_any_signature_work(monkeypatch):
    def _never_consulted():
        raise AssertionError("slack_settings must not be consulted before the bounded read rejects")

    monkeypatch.setattr(inbound, "slack_settings", _never_consulted)
    oversized = b"x" * (1 * 1024 * 1024 + 1)

    response = await inbound.slack_inbound(make_request(oversized, {}))

    assert response.status_code == 413
    assert body_json(response) == {"error": "payload too large"}
