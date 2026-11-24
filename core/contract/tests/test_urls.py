"""Tests for the shared loopback-host predicate."""

from __future__ import annotations

import pytest

from tai42_contract.urls import is_loopback_host


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.8.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("example.com", False),
        ("10.0.0.1", False),
        ("", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert is_loopback_host(host) is expected
