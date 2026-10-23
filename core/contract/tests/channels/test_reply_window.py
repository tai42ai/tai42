"""Tests for ``ReplyWindow`` — whether a free-form reply to one address is open, closed or not limited."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError


def test_reply_window_is_exported_from_the_channels_package():
    import tai42_contract.channels as channels
    from tai42_contract.channels.reply_window import ReplyWindow

    assert channels.ReplyWindow is ReplyWindow
    assert "ReplyWindow" in channels.__all__


def test_open_window_carries_its_close_time():
    from tai42_contract.channels import ReplyWindow

    closes_at = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    window = ReplyWindow(state="open", closes_at=closes_at)
    assert window.state == "open"
    assert window.closes_at == closes_at


@pytest.mark.parametrize("state", ["closed", "none"])
def test_closed_and_none_carry_no_close_time(state: str):
    from tai42_contract.channels import ReplyWindow

    window = ReplyWindow(state=state, closes_at=None)  # type: ignore[arg-type]
    assert window.state == state
    assert window.closes_at is None


def test_open_without_a_close_time_is_refused():
    from tai42_contract.channels import ReplyWindow

    with pytest.raises(ValidationError, match="closes_at"):
        ReplyWindow(state="open", closes_at=None)


@pytest.mark.parametrize("state", ["closed", "none"])
def test_closed_or_none_with_a_close_time_is_refused(state: str):
    from tai42_contract.channels import ReplyWindow

    with pytest.raises(ValidationError, match="closes_at"):
        ReplyWindow(state=state, closes_at=datetime(2026, 10, 22, tzinfo=UTC))  # type: ignore[arg-type]


def test_a_naive_close_time_is_refused():
    from tai42_contract.channels import ReplyWindow

    with pytest.raises(ValidationError):
        ReplyWindow(state="open", closes_at=datetime(2026, 10, 22, 12, 0))  # the naive value under test


def test_an_unknown_state_is_refused():
    from tai42_contract.channels import ReplyWindow

    with pytest.raises(ValidationError):
        ReplyWindow(state="ajar", closes_at=None)  # type: ignore[arg-type]


def test_an_unknown_key_is_refused():
    from tai42_contract.channels import ReplyWindow

    with pytest.raises(ValidationError):
        ReplyWindow.model_validate({"state": "none", "closes_at": None, "extra": 1})


def test_close_time_is_kept_in_utc():
    from tai42_contract.channels import ReplyWindow

    offset = timezone(timedelta(hours=2))
    window = ReplyWindow(state="open", closes_at=datetime(2026, 10, 22, 14, 0, tzinfo=offset))
    assert window.closes_at == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert window.closes_at is not None
    assert window.closes_at.utcoffset() == timedelta(0)


def test_json_dump_gives_a_whole_second_iso_z_string_or_null():
    from tai42_contract.channels import ReplyWindow

    opened = ReplyWindow(state="open", closes_at=datetime(2026, 10, 23, 9, 30, 0, 123_000, tzinfo=UTC))
    assert opened.model_dump(mode="json") == {"state": "open", "closes_at": "2026-10-23T09:30:00Z"}
    assert opened.model_dump_json() == '{"state":"open","closes_at":"2026-10-23T09:30:00Z"}'
    assert ReplyWindow(state="none", closes_at=None).model_dump(mode="json") == {"state": "none", "closes_at": None}


def test_reply_window_is_frozen():
    from tai42_contract.channels import ReplyWindow

    window = ReplyWindow(state="none", closes_at=None)
    with pytest.raises(ValidationError):
        window.state = "closed"  # type: ignore[misc]


def test_close_time_is_held_in_whole_seconds_rounded_down():
    from tai42_contract.channels import ReplyWindow

    window = ReplyWindow(state="open", closes_at=datetime(2026, 10, 23, 9, 30, 0, 999_999, tzinfo=UTC))
    assert window.closes_at == datetime(2026, 10, 23, 9, 30, 0, tzinfo=UTC)
