"""The local server port is one contract constant both the server and the CLI read."""

from __future__ import annotations

from tai42_contract.app import DEFAULT_LOCAL_PORT


def test_the_default_local_port_is_a_valid_tcp_port() -> None:
    assert isinstance(DEFAULT_LOCAL_PORT, int)
    assert 0 < DEFAULT_LOCAL_PORT < 65536
