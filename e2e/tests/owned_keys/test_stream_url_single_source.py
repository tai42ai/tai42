"""Proof that a test's stream-URL builder addresses a run's target through the stack's
``origin()`` helper, so an HTTPS target gets ``https://host`` (not cleartext to :443).

A run drives an :class:`~tai42_e2e.target.TargetStack` whose ``origin()``/``api()`` carry
the target's real scheme, host and port. A site that builds ``http://{host}:{port}`` by
hand bypasses that override: against an HTTPS tenant it sends cleartext to port 443 and
cannot reach the target. This pins the representative :func:`_stream_url` site to the
single source. It builds no stack, so it runs on any leg."""

from __future__ import annotations

import pytest

from tai42_e2e.target import Target, TargetStack

from .test_isolation_interactions import _stream_url

pytestmark = [pytest.mark.backendless, pytest.mark.needs("no-stack")]

_HTTPS_TARGET = "https://tenant.example.com"


def test_stream_url_follows_origin_for_an_https_target() -> None:
    """``_stream_url`` yields the target's HTTPS origin, never cleartext to :443."""
    stack = TargetStack(Target(_HTTPS_TARGET), boot_timeout=1.0)

    # The helper is the single source: it carries the target's real scheme and host.
    assert stack.origin() == _HTTPS_TARGET
    assert stack.origin(stack.port_a) == _HTTPS_TARGET
    # A hand-built ``http://{host}:{port}`` is wrong for an HTTPS target — cleartext to 443.
    assert f"http://{stack.host}:{stack.port_a}" == "http://tenant.example.com:443"

    url = _stream_url(stack, stack.port_a)

    assert url == f"{_HTTPS_TARGET}/api/interactions/stream"
    assert url.startswith("https://")
    assert ":443" not in url
