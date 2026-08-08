"""The shared client-address resolver: the two trust statements and their
precedence, the fail-closed fallbacks, the loud rejection of a chain it will not
believe, and the normalisation that keeps one client to one identity."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from tai42_skeleton.utils import client_address as ca


def _conn(peer: str | None, xff: str | None = None) -> Any:
    """A minimal ``HTTPConnection`` stand-in exposing the ``.client`` and ``.headers``
    the resolver reads."""
    headers: dict[str, str] = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    client = SimpleNamespace(host=peer) if peer is not None else None
    return SimpleNamespace(client=client, headers=headers)


def _resolve(peer: str | None, xff: str = "", proxies: list[str] | None = None, hops: int = 0) -> str:
    return ca.resolve_client_address(peer, xff, proxies or [], hops)


@pytest.fixture(autouse=True)
def _quiet_warning_throttle(monkeypatch):
    """Each test starts from a cold warning throttle, so one test's rejection cannot
    suppress the next test's line."""
    monkeypatch.setattr(ca, "_last_reject_warning", 0.0)
    monkeypatch.setattr(ca, "_suppressed_rejects", 0)
    monkeypatch.setattr(ca, "_UNDECLARED_PROXY_WARNED", False)


# -- fail closed: the header is read only under a declared trust statement ----


def test_untrusted_peer_ignores_the_header_entirely():
    assert _resolve("198.51.100.9", xff="1.2.3.4") == "198.51.100.9"


def test_no_trust_declared_ignores_the_header():
    assert _resolve("10.0.0.1", xff="203.0.113.5, 10.0.0.2") == "10.0.0.1"


def test_a_peer_outside_the_declared_roster_is_the_client():
    assert _resolve("198.51.100.9", xff="1.2.3.4", proxies=["10.0.0.0/8"]) == "198.51.100.9"


def test_no_peer_at_all_is_the_unknown_marker():
    assert _resolve(None) == ca.UNKNOWN_CLIENT


# -- address mode ------------------------------------------------------------


def test_trusted_peer_reads_the_right_most_untrusted_hop():
    assert _resolve("10.0.0.1", xff="203.0.113.5, 10.0.0.2", proxies=["10.0.0.1"]) == "10.0.0.2"


def test_a_trusted_suffix_is_walked_past():
    # A trailing trusted hop cannot mask the origin: the walk continues left.
    assert _resolve("10.0.0.1", xff="203.0.113.5, 10.0.0.2", proxies=["10.0.0.1", "10.0.0.2"]) == "203.0.113.5"


def test_a_cidr_block_trusts_a_whole_range():
    # THE point of CIDR support: an ingress pod range renumbers within its block and
    # the roster keeps matching without an entry per address.
    assert _resolve("10.4.7.9", xff="203.0.113.5, 10.4.9.3", proxies=["10.4.0.0/16"]) == "203.0.113.5"


def test_an_ipv6_cidr_block_trusts_a_whole_range():
    assert _resolve("2001:db8:1::7", xff="203.0.113.5", proxies=["2001:db8:1::/48"]) == "203.0.113.5"


def test_a_mixed_version_roster_matches_only_its_own_version():
    # An IPv4 peer against an IPv6-only roster is untrusted — never an error.
    assert _resolve("10.0.0.1", xff="203.0.113.5", proxies=["2001:db8::/32"]) == "10.0.0.1"


def test_an_all_trusted_chain_falls_back_to_the_socket():
    assert _resolve("10.0.0.1", xff="10.0.0.2, 10.0.0.3", proxies=["10.0.0.0/8"]) == "10.0.0.1"


def test_an_ipv4_mapped_peer_matches_an_ipv4_roster_entry():
    assert _resolve("::ffff:10.0.0.1", xff="203.0.113.5", proxies=["10.0.0.1"]) == "203.0.113.5"


# -- hop-count mode ----------------------------------------------------------


def test_hop_count_takes_the_entry_the_count_names():
    # One proxy in front: the socket peer IS that proxy, so the last XFF entry is the
    # client — with no address of the proxy declared anywhere.
    assert _resolve("10.9.9.9", xff="203.0.113.5, 198.51.100.7", hops=1) == "198.51.100.7"


def test_hop_count_two_skips_two_right_most_entries():
    assert _resolve("10.9.9.9", xff="203.0.113.5, 198.51.100.7", hops=2) == "203.0.113.5"


def test_hop_count_wins_over_the_address_roster():
    # Both configured, and they DISAGREE: the load balancer has renumbered out of the
    # declared block, so the roster alone would call the proxy itself the client. The
    # count is authoritative, so the real client still resolves.
    resolved = _resolve("172.31.5.5", xff="203.0.113.5", proxies=["10.0.0.0/8"], hops=1)
    assert resolved == "203.0.113.5"
    assert _resolve("172.31.5.5", xff="203.0.113.5", proxies=["10.0.0.0/8"]) == "172.31.5.5"


def test_hop_count_ignores_a_forged_prefix():
    # A client forging extra entries only lengthens the left of the chain; the count
    # counts from the right, so the forged text is never selected.
    assert _resolve("10.9.9.9", xff="9.9.9.9, 8.8.8.8, 198.51.100.7", hops=1) == "198.51.100.7"


def test_a_chain_too_short_for_the_hop_count_falls_back_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger=ca.logger.name):
        assert _resolve("10.9.9.9", hops=1) == "10.9.9.9"
    assert [r for r in caplog.records if "trusted_hops" in r.getMessage()]


# -- malformed chains are loud, never a shared bucket ------------------------


def test_an_unparseable_selected_entry_falls_back_to_the_socket_loudly(caplog):
    with caplog.at_level(logging.WARNING, logger=ca.logger.name):
        resolved = _resolve("10.0.0.1", xff="not-an-ip", proxies=["10.0.0.1"])
    assert resolved == "10.0.0.1"
    assert [r for r in caplog.records if "names no IP address" in r.getMessage()]


def test_an_unparseable_entry_left_of_the_client_is_irrelevant():
    # Only the entry the walk REACHES matters; a client's own forged prefix never is.
    assert _resolve("10.0.0.1", xff="garbage, 203.0.113.5", proxies=["10.0.0.1"]) == "203.0.113.5"


def test_a_malformed_chain_never_collapses_two_clients_into_one_bucket(caplog):
    # The failure this guards: believing an unparseable entry would key every such
    # request to the SAME string. The socket address keeps them apart instead.
    with caplog.at_level(logging.WARNING, logger=ca.logger.name):
        first = _resolve("10.0.0.1", xff="???", proxies=["10.0.0.0/8"])
        second = _resolve("10.0.0.2", xff="???", proxies=["10.0.0.0/8"])
    assert first != second


def test_the_rejection_warning_is_throttled_with_a_suppressed_count(caplog):
    with caplog.at_level(logging.WARNING, logger=ca.logger.name):
        for _ in range(50):
            _resolve("10.0.0.1", xff="???", proxies=["10.0.0.1"])
    lines = [r for r in caplog.records if "names no IP address" in r.getMessage()]
    assert len(lines) == 1, "a hostile header must not flood the log"
    assert ca._suppressed_rejects == 49


# -- misconfiguration is surfaced, not silently survived ---------------------


def test_an_xff_from_an_untrusted_peer_warns_once_that_a_proxy_may_be_undeclared(caplog):
    with caplog.at_level(logging.WARNING, logger=ca.logger.name):
        _resolve("10.0.0.1", xff="203.0.113.5")
        _resolve("10.0.0.1", xff="203.0.113.6")
    lines = [r for r in caplog.records if "no proxy trust is declared" in r.getMessage()]
    assert len(lines) == 1


def test_no_undeclared_proxy_warning_without_a_forwarded_header(caplog):
    with caplog.at_level(logging.WARNING, logger=ca.logger.name):
        _resolve("10.0.0.1")
    assert not [r for r in caplog.records if "no proxy trust is declared" in r.getMessage()]


def test_boot_posture_warning_only_when_nothing_is_declared(caplog):
    log = logging.getLogger("test.client_address.posture")
    with caplog.at_level(logging.WARNING, logger=log.name):
        ca.warn_if_proxy_trust_undeclared(log, [], 0)
        ca.warn_if_proxy_trust_undeclared(log, ["10.0.0.1"], 0)
        ca.warn_if_proxy_trust_undeclared(log, [], 2)
    assert len([r for r in caplog.records if "no proxy trust declared" in r.getMessage()]) == 1


# -- roster parsing ----------------------------------------------------------


def test_a_bare_address_and_a_cidr_share_one_code_path():
    assert [str(n) for n in ca.parse_trusted_networks(["10.0.0.1", "192.168.0.0/16"])] == [
        "10.0.0.1/32",
        "192.168.0.0/16",
    ]


def test_a_nonsense_roster_entry_raises():
    with pytest.raises(ValueError, match="neither an IP address nor a CIDR block"):
        ca.parse_trusted_networks(["not-a-network"])


def test_a_host_bearing_cidr_is_accepted_as_its_network():
    # ``strict=False``: 10.0.0.7/8 is the operator naming the block through a host in
    # it, not an error to refuse.
    assert [str(n) for n in ca.parse_trusted_networks(["10.0.0.7/8"])] == ["10.0.0.0/8"]


# -- normalisation -----------------------------------------------------------


def test_zone_identifier_is_stripped():
    assert str(ca.parse_address("fe80::1%eth0")) == "fe80::1"


def test_ipv4_mapped_address_unwraps():
    assert str(ca.parse_address("::ffff:1.2.3.4")) == "1.2.3.4"


def test_a_bracketed_ipv6_with_a_port_reduces_to_its_address():
    assert str(ca.parse_address("[2001:db8::1]:4711")) == "2001:db8::1"


def test_an_ipv4_with_a_port_reduces_to_its_address():
    assert str(ca.parse_address("203.0.113.5:1234")) == "203.0.113.5"


def test_a_bare_ipv6_is_never_cut_at_its_colons():
    assert str(ca.parse_address("2001:db8::1")) == "2001:db8::1"


def test_an_unparseable_entry_parses_to_none():
    assert ca.parse_address("not-an-ip") is None


# -- bucket collapsing -------------------------------------------------------


def test_bucket_ipv4_used_as_is():
    assert ca.bucket_of("203.0.113.7") == "203.0.113.7"


def test_bucket_ipv6_collapses_to_slash_64():
    a = ca.bucket_of("2001:db8:abcd:1234::1")
    b = ca.bucket_of("2001:db8:abcd:1234::abcd")
    assert a == b == "2001:db8:abcd:1234::"


def test_bucket_ipv4_mapped_unwrapped():
    assert ca.bucket_of("::ffff:1.2.3.4") == "1.2.3.4"


def test_bucket_unparseable_keeps_the_raw_string():
    assert ca.bucket_of("unknown") == "unknown"


# -- the request-level entry points read the deployment's statement ----------


def test_client_bucket_reads_the_configured_trust(monkeypatch):
    monkeypatch.setenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", '["10.0.0.0/8"]')
    from tai42_kit.settings import reset_all_settings

    reset_all_settings()
    try:
        assert ca.client_bucket(_conn("10.0.0.1", xff="2001:db8:abcd:1234::9")) == "2001:db8:abcd:1234::"
        assert ca.client_address(_conn("10.0.0.1", xff="203.0.113.5")) == "203.0.113.5"
    finally:
        monkeypatch.delenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", raising=False)
        reset_all_settings()


def test_client_bucket_of_an_untrusted_peer_is_the_peer(monkeypatch):
    monkeypatch.delenv("TAI_RATE_LIMIT_TRUSTED_PROXIES", raising=False)
    from tai42_kit.settings import reset_all_settings

    reset_all_settings()
    try:
        assert ca.client_bucket(_conn("198.51.100.9", xff="1.2.3.4")) == "198.51.100.9"
    finally:
        reset_all_settings()
