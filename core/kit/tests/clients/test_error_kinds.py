"""The kit's transport error kinds: httpx/anyio registrations and the MCP error-code stamp at the lease."""

from __future__ import annotations

import anyio
import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, METHOD_NOT_FOUND, URL_ELICITATION_REQUIRED, ErrorData
from tai42_contract.errors import ErrorKind, error_kind, is_transport_error

import tai42_kit.clients.impl.mcp  # noqa: F401  (importing the MCP client module registers the kinds)
from tai42_kit.clients.impl.mcp import FastMCPClient

_REQUEST = httpx.Request("GET", "http://upstream.invalid/")


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.ConnectTimeout("slow", request=_REQUEST), ErrorKind.TIMED_OUT),
        (httpx.ReadTimeout("slow", request=_REQUEST), ErrorKind.TIMED_OUT),
        (httpx.ConnectError("refused", request=_REQUEST), ErrorKind.UNAVAILABLE),
        (httpx.RemoteProtocolError("torn", request=_REQUEST), ErrorKind.UNAVAILABLE),
        (anyio.ClosedResourceError(), ErrorKind.UNAVAILABLE),
        (anyio.BrokenResourceError(), ErrorKind.UNAVAILABLE),
    ],
)
def test_transport_classes_resolve_their_kind_and_are_transport_errors(exc: BaseException, kind: ErrorKind):
    assert error_kind(exc) is kind
    assert is_transport_error(exc) is True


class _StubSession:
    """Stands in for the fastmcp ``Client`` the pool leases."""


def _stub_mcp_client_cls() -> type[FastMCPClient]:
    """A FastMCP pooled client over a stub session (fresh class per test: the pool is keyed by class)."""

    class _StubFastMCPClient(FastMCPClient):
        async def _create(self, **kwargs):
            return _StubSession()

        async def _close(self, client):
            return None

    return _StubFastMCPClient


async def _raise_inside_lease(exc: BaseException) -> BaseException:
    with pytest.raises(type(exc)) as caught:
        async with _stub_mcp_client_cls()().current(config={"url": "http://upstream.invalid/mcp"}):
            raise exc
    return caught.value


@pytest.mark.parametrize(
    ("code", "kind", "transport"),
    [
        (408, ErrorKind.TIMED_OUT, True),
        (INVALID_PARAMS, ErrorKind.BAD_INPUT, False),
        (METHOD_NOT_FOUND, ErrorKind.NOT_FOUND, False),
        (URL_ELICITATION_REQUIRED, ErrorKind.UNAUTHORIZED, False),
        (-32603, ErrorKind.UPSTREAM_ERROR, False),
    ],
)
async def test_an_mcp_error_inside_the_lease_is_stamped_with_its_codes_kind(
    code: int, kind: ErrorKind, transport: bool
) -> None:
    raised = await _raise_inside_lease(McpError(ErrorData(code=code, message="m")))
    assert error_kind(raised) is kind
    assert is_transport_error(raised) is transport


async def test_a_wrapper_raised_from_a_stamped_408_resolves_timed_out_and_is_not_transport():
    raised = await _raise_inside_lease(McpError(ErrorData(code=408, message="read timeout")))

    class _DispatchError(Exception):
        """Shaped like a dispatch seam's own error raised ``from`` the MCP error."""

    wrapper = _DispatchError("dispatch failed")
    wrapper.__cause__ = raised
    assert error_kind(wrapper) is ErrorKind.TIMED_OUT
    assert is_transport_error(wrapper) is False


async def test_an_already_stamped_mcp_error_keeps_its_stamp():
    err = McpError(ErrorData(code=INVALID_PARAMS, message="m"))
    err.__tai_error_kind__ = ErrorKind.CONFLICT  # type: ignore[attr-defined]
    raised = await _raise_inside_lease(err)
    assert error_kind(raised) is ErrorKind.CONFLICT


async def test_a_non_mcp_error_inside_the_lease_is_left_unstamped():
    raised = await _raise_inside_lease(KeyError("caller bug"))
    assert error_kind(raised) is ErrorKind.UNKNOWN
    assert is_transport_error(raised) is False
