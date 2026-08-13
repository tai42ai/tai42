"""Unit tests for the token-injection + token_expired retry flow in
``mcp_tool_call_wrapper``.

These drive the full wrapper (resolve → transport branch → meta/header
injection → token_expired detection → force_refresh → retry-once) with a
scripted ``FastMCPClient``. The runtime resolver / force-refresher are
patched at the token-injection module's import site so the real
``resolve_managed_auth_for_config`` / ``_force_refresh`` wrappers (is_managed
gating, ConnectorRef unwrap, empty-token rejection) still run.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from tai42_contract.connectors.models import ConnectorRef
from tai42_contract.errors import ClientConnectError, ClientDisconnectedError
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

# Import the app first so ``app`` is bound, so the adapter import
# chain resolves against a constructed app.
import tai42_skeleton.app.instance  # noqa: F401
from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorReconnectRequiredError,
    ManagedAuth,
)
from tai42_skeleton.connectors.token_injection import (
    CONNECTOR_ERROR_PREFIX,
    CONNECTOR_META_TOKEN_KEY,
    extract_connector_error_payload,
)
from tai42_skeleton.tools import mcp_health
from tai42_skeleton.tools.adapters.mcp_tool_to_func import (
    _build_output_schema,
    mcp_tool_call_wrapper,
)

CONN_ID = "11111111-1111-1111-1111-111111111111"

_RESOLVER = "tai42_skeleton.connectors.token_injection.resolve_managed_auth"
_REFRESHER = "tai42_skeleton.connectors.token_injection.force_refresh"


def _managed_config() -> TaiMCPConfig:
    return TaiMCPConfig(
        title="google_gmail_work",
        config=MCPConfig(
            type="http",
            url="https://gmail.test/sse",
            headers={},
        ),
        managed=ConnectorRef(
            connection_id=CONN_ID,
            provider_id="google",
            sub_service="gmail",
        ),
    )


# -- scripted client ----------------------------------------------------------


class _FakeMcpClient:
    """Minimal FastMCPClient stand-in. Yields a stub session per
    ``current(config=...)`` enter; the stub's ``call_tool_mcp`` pops the
    next scripted response (or raises the next scripted exception)."""

    def __init__(self, *, responses: list) -> None:
        # ``responses`` may contain either ``mcp.types.CallToolResult``
        # instances OR ``BaseException`` instances; the stub raises the
        # exception ones and returns the result ones.
        self._responses = list(responses)
        self.captured_metas: list = []
        self.captured_configs: list = []
        self.call_count = 0

    def current(self, *, config):
        self.captured_configs.append(config)
        return self._session()

    def _session(self):
        outer = self

        class _Session:
            async def __aenter__(self):
                return self

            @staticmethod
            async def __aexit__(*exc):
                return False

            @staticmethod
            async def call_tool_mcp(name, arguments, meta=None, timeout=None):
                outer.captured_metas.append(meta)
                outer.call_count += 1
                if not outer._responses:
                    raise AssertionError("fake client out of scripted responses")
                nxt = outer._responses.pop(0)
                if isinstance(nxt, BaseException):
                    raise nxt
                return nxt

        return _Session()


def _text_result(text: str, *, is_error: bool = False):
    import mcp.types

    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=text)],
        isError=is_error,
    )


def _hub_error_result(payload: dict):
    import json

    return _text_result(
        f"{CONNECTOR_ERROR_PREFIX}{json.dumps(payload)}",
        is_error=True,
    )


def _ok_result(payload: dict):
    import json

    return _text_result(json.dumps(payload), is_error=False)


def _trivial_input_model():
    from pydantic import BaseModel

    class _NoArgs(BaseModel):
        pass

    return _NoArgs


_CLIENT = "tai42_skeleton.tools.adapters.mcp_tool_to_func.FastMCPClient"


async def _run_wrapper(*, client: _FakeMcpClient, config: TaiMCPConfig):
    # The wrapper builds its own ``FastMCPClient`` — patch the class so it
    # returns the scripted fake (the ``current(config=...)`` contract matches).
    with patch(_CLIENT, return_value=client):
        return await mcp_tool_call_wrapper(
            config=config,
            tool_name="list_messages",
            tool_input_model=_trivial_input_model(),
            tool_arguments={},
        )


# -- _build_output_schema (no caller mutation) --------------------------------


def test_build_output_schema_does_not_mutate_caller():
    """Unwrapping the ``{"result": ...}`` envelope must copy the nested schema,
    never write ``$defs`` back into the caller's ``mcp.Tool``."""
    import mcp

    output_schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "$defs": {"Foo": {"type": "object"}},
    }
    tool = mcp.Tool(name="t", inputSchema={"type": "object"}, outputSchema=output_schema)

    inner = _build_output_schema(tool)

    # The returned inner schema carries the hoisted $defs...
    assert inner["$defs"] == {"Foo": {"type": "object"}}
    # ...but the caller's own nested result schema is left untouched.
    assert tool.outputSchema is not None
    assert "$defs" not in tool.outputSchema["properties"]["result"]


# -- token_expired retry-once flow (http) -------------------------------------


def test_token_expired_retry_once_force_refreshes_and_succeeds():
    """First call returns token_expired sentinel → force_refresh fires
    once → second call succeeds with the refreshed token in the
    Authorization header (http transport)."""
    resolver = AsyncMock(return_value=ManagedAuth(access_token="initial-token"))
    refresher = AsyncMock(return_value=ManagedAuth(access_token="refreshed-token"))

    # First call: hub server returns token_expired. Second call: success.
    client = _FakeMcpClient(
        responses=[
            _hub_error_result({"code": "token_expired"}),
            _ok_result({"result": "ok"}),
        ]
    )

    with patch(_RESOLVER, new=resolver), patch(_REFRESHER, new=refresher):
        asyncio.run(_run_wrapper(client=client, config=_managed_config()))

    assert client.call_count == 2, "wrapper must retry exactly once"
    refresher.assert_awaited_once_with(CONN_ID, failed_access_token="initial-token")

    # http-managed mode merges the token via header, NOT _meta — both call
    # metas are None and the second call's config carries the refreshed
    # Authorization header.
    assert client.captured_metas == [None, None]
    second_headers = client.captured_configs[1]["config"]["headers"]
    assert second_headers["authorization"] == "Bearer refreshed-token"


def test_token_expired_persists_after_retry_returns_structured_error():
    """Second call still returns token_expired → the adapter surfaces a structured
    connector-error result (code ``auth_expired``) carrying the connection identity,
    so a client can offer a reconnect instead of seeing a generic error string."""
    resolver = AsyncMock(return_value=ManagedAuth(access_token="t1"))
    refresher = AsyncMock(return_value=ManagedAuth(access_token="t2"))

    client = _FakeMcpClient(
        responses=[
            _hub_error_result({"code": "token_expired"}),
            _hub_error_result({"code": "token_expired"}),
        ]
    )

    with patch(_RESOLVER, new=resolver), patch(_REFRESHER, new=refresher):
        result = asyncio.run(_run_wrapper(client=client, config=_managed_config()))

    payload = extract_connector_error_payload(result)
    assert payload is not None
    assert payload["code"] == "auth_expired"
    assert payload["connection_id"] == CONN_ID
    assert payload["provider_id"] == "google"
    assert payload["sub_service"] == "gmail"


def test_reconnect_required_surfaces_structured_error():
    """A resolver ``invalid_grant`` (ConnectorReconnectRequiredError) is surfaced
    as a structured connector-error result (code ``reconnect_required``), not a raw
    exception, so a client can offer a reconnect instead of a generic error."""
    resolver = AsyncMock(side_effect=ConnectorReconnectRequiredError("invalid_grant", connection_id=CONN_ID))
    client = _FakeMcpClient(responses=[])

    with patch(_RESOLVER, new=resolver):
        result = asyncio.run(_run_wrapper(client=client, config=_managed_config()))

    payload = extract_connector_error_payload(result)
    assert payload is not None
    assert payload["code"] == "reconnect_required"
    assert payload["connection_id"] == CONN_ID


def test_force_refresh_failure_propagates():
    """force_refresh raising → the failure propagates (no swallowing)."""
    resolver = AsyncMock(return_value=ManagedAuth(access_token="t1"))

    class _RefreshOutage(RuntimeError):
        pass

    refresher = AsyncMock(side_effect=_RefreshOutage("upstream OAuth down"))

    client = _FakeMcpClient(
        responses=[
            _hub_error_result({"code": "token_expired"}),
        ]
    )

    with patch(_RESOLVER, new=resolver), patch(_REFRESHER, new=refresher), pytest.raises(_RefreshOutage):
        asyncio.run(_run_wrapper(client=client, config=_managed_config()))


# -- stdio managed-call _meta-injection ---------------------------------------
#
# The stdio token injection path: a managed stdio config carries the resolved
# access token through the wrapper into ``client.call_tool_mcp(..., meta=...)``.


def _managed_stdio_config() -> TaiMCPConfig:
    return TaiMCPConfig(
        title="google_gmail_work",
        config=MCPConfig(
            type="stdio",
            command="uvx",
            args=["--from", "git+ssh://git@example/repo", "gmail-mcp"],
        ),
        managed=ConnectorRef(
            connection_id=CONN_ID,
            provider_id="google",
            sub_service="gmail",
        ),
    )


def test_stdio_managed_call_injects_access_token_into_meta():
    resolver = AsyncMock(
        return_value=ManagedAuth(access_token="initial-stdio-token"),
    )

    client = _FakeMcpClient(responses=[_ok_result({"result": "ok"})])

    with patch(_RESOLVER, new=resolver):
        asyncio.run(_run_wrapper(client=client, config=_managed_stdio_config()))

    assert client.call_count == 1
    assert client.captured_metas == [
        {CONNECTOR_META_TOKEN_KEY: "initial-stdio-token"},
    ], "stdio managed call MUST carry the resolved access token in _meta — this is the branch's headline behaviour."


def test_stdio_managed_call_carries_refreshed_token_after_token_expired():
    resolver = AsyncMock(
        return_value=ManagedAuth(access_token="initial-stdio-token"),
    )
    refresher = AsyncMock(
        return_value=ManagedAuth(access_token="refreshed-stdio-token"),
    )

    client = _FakeMcpClient(
        responses=[
            _hub_error_result({"code": "token_expired"}),
            _ok_result({"result": "ok"}),
        ]
    )

    with patch(_RESOLVER, new=resolver), patch(_REFRESHER, new=refresher):
        asyncio.run(_run_wrapper(client=client, config=_managed_stdio_config()))

    assert client.call_count == 2, "wrapper must retry exactly once"
    refresher.assert_awaited_once_with(CONN_ID, failed_access_token="initial-stdio-token")
    # Critical: the SECOND call carries the REFRESHED token, not a
    # stale duplicate of the first.
    assert client.captured_metas == [
        {CONNECTOR_META_TOKEN_KEY: "initial-stdio-token"},
        {CONNECTOR_META_TOKEN_KEY: "refreshed-stdio-token"},
    ]


def test_stdio_non_managed_call_has_no_meta():
    """Token-meta injection is gated on ``config.is_managed`` — a
    hand-authored stdio entry without a ``managed`` ref MUST receive
    ``meta=None``, never an empty dict and never a stale token. The
    runtime resolver MUST NOT be reached for a non-managed entry."""
    resolver = AsyncMock(
        side_effect=AssertionError(
            "resolver MUST NOT be called for a non-managed entry",
        ),
    )

    non_managed = TaiMCPConfig(
        title="local_dev_stdio",
        config=MCPConfig(
            type="stdio",
            command="python",
            args=["-m", "some.local.server"],
        ),
    )
    client = _FakeMcpClient(responses=[_ok_result({"result": "ok"})])

    with patch(_RESOLVER, new=resolver):
        asyncio.run(_run_wrapper(client=client, config=non_managed))

    assert client.captured_metas == [None]
    resolver.assert_not_awaited()


# -- reconnect: ONE transparent retry on ClientDisconnectedError --------------


def _plain_http_config() -> TaiMCPConfig:
    """A hand-authored (non-managed) http entry — the resolver is never reached,
    so the reconnect behaviour is isolated from the token flow."""
    return TaiMCPConfig(
        title="local_http",
        config=MCPConfig(type="http", url="https://svc.test/mcp", headers={}),
    )


def test_disconnect_retries_once_and_second_attempt_succeeds(caplog):
    """A first-dispatch ``ClientDisconnectedError`` triggers exactly one
    fresh-session retry; the second dispatch succeeds and a WARNING is logged."""
    client = _FakeMcpClient(
        responses=[
            ClientDisconnectedError("session died — retry the operation"),
            _ok_result({"result": "ok"}),
        ]
    )

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    assert client.call_count == 2, "wrapper must retry exactly once after a disconnect"
    assert "retrying once" in caplog.text
    assert "local_http" in caplog.text


def test_second_consecutive_disconnect_returns_structured_unavailable(caplog):
    """Two consecutive ``ClientDisconnectedError``s (the reconnect retry hit a dead
    session too) → a structured ``upstream_mcp_unavailable`` tool-error naming the
    MCP by title, NOT the raw fastmcp disconnect text; the full exception is logged."""
    client = _FakeMcpClient(
        responses=[
            ClientDisconnectedError("first disconnect"),
            ClientDisconnectedError("second disconnect — raw fastmcp text"),
        ]
    )

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    assert client.call_count == 2, "exactly one retry — the second disconnect is not retried again"

    payload = extract_connector_error_payload(result)
    assert payload is not None
    assert payload["code"] == "upstream_mcp_unavailable"
    assert "local_http" in payload["message"]
    # The raw fastmcp disconnect text is never the consumer-facing message.
    assert "raw fastmcp text" not in payload["message"]
    # ...but the full exception IS logged at the dispatch seam.
    assert "upstream unavailable" in caplog.text


def test_downstream_tool_name_control_chars_cannot_forge_log_lines(caplog):
    """A downstream MCP advertising a tool name with a newline must not be able to
    forge or split a log record — the control char is escaped where the name is
    interpolated into the retry WARNING and the upstream-unavailable ERROR."""
    hostile = "list\nFORGED admin granted"
    client = _FakeMcpClient(
        responses=[
            ClientDisconnectedError("first disconnect"),
            ClientDisconnectedError("second disconnect"),
        ]
    )

    with caplog.at_level(logging.WARNING), patch(_CLIENT, return_value=client):
        asyncio.run(
            mcp_tool_call_wrapper(
                config=_plain_http_config(),
                tool_name=hostile,
                tool_input_model=_trivial_input_model(),
                tool_arguments={},
            )
        )

    tool_records = [r for r in caplog.records if "tool=" in r.getMessage()]
    assert tool_records, "both the retry WARNING and the ERROR interpolate the tool name"
    for record in tool_records:
        assert "\n" not in record.getMessage(), "a raw newline could forge a second log line"
    # The name still shows, its control character rendered as a visible escape.
    assert any("\\x0aFORGED" in r.getMessage() for r in tool_records)


def test_downstream_tool_name_unicode_line_separators_cannot_forge_log_lines(caplog):
    """A downstream MCP name carrying Unicode line-boundary characters (U+2028 LINE
    SEPARATOR, U+0085 NEL) must not be able to forge or split a log record — logging
    and many log viewers treat these as line breaks, so each is escaped where the
    name is interpolated into the retry WARNING and the upstream-unavailable ERROR."""
    hostile = "list\u2028FORGED\x85admin granted"
    client = _FakeMcpClient(
        responses=[
            ClientDisconnectedError("first disconnect"),
            ClientDisconnectedError("second disconnect"),
        ]
    )

    with caplog.at_level(logging.WARNING), patch(_CLIENT, return_value=client):
        asyncio.run(
            mcp_tool_call_wrapper(
                config=_plain_http_config(),
                tool_name=hostile,
                tool_input_model=_trivial_input_model(),
                tool_arguments={},
            )
        )

    tool_records = [r for r in caplog.records if "tool=" in r.getMessage()]
    assert tool_records, "both the retry WARNING and the ERROR interpolate the tool name"
    for record in tool_records:
        message = record.getMessage()
        assert "\u2028" not in message, "a raw U+2028 could forge a second log line"
        assert "\x85" not in message, "a raw U+0085 (NEL) could forge a second log line"
    # The name still shows, its separators rendered as visible escapes.
    assert any("\\x2028FORGED\\x85admin" in r.getMessage() for r in tool_records)


# -- connect-failure at acquire time (ClientConnectError) ---------------------
#
# A fresh connect that fails raises ``ClientConnectError`` (a
# ``ClientDisconnectedError`` subclass) from the pooled client's ``_create``,
# which surfaces at the ``async with mcp_client.current(...)`` enter — before any
# ``call_tool_mcp`` runs. These fakes raise per attempt at acquire (enter) time.


class _AttemptScriptedClient:
    """FastMCPClient stand-in scripted per ``current()`` attempt. Each attempt
    either raises at acquire (async-enter) time — modelling a ``_create`` connect
    failure that never reaches the call — or enters and its ``call_tool_mcp``
    raises/returns the scripted body outcome."""

    def __init__(self, *, attempts: list) -> None:
        # Each attempt is a (enter_error, body) pair: ``enter_error`` (a
        # BaseException) is raised at acquire time; otherwise ``body`` is raised
        # (BaseException) or returned (CallToolResult) from call_tool_mcp.
        self._attempts = list(attempts)
        self.enter_count = 0
        self.call_count = 0

    def current(self, *, config):
        if not self._attempts:
            raise AssertionError("fake client out of scripted attempts")
        enter_error, body = self._attempts.pop(0)
        outer = self

        class _Session:
            async def __aenter__(self):
                outer.enter_count += 1
                if enter_error is not None:
                    raise enter_error
                return self

            @staticmethod
            async def __aexit__(*exc):
                return False

            @staticmethod
            async def call_tool_mcp(name, arguments, meta=None, timeout=None):
                outer.call_count += 1
                if isinstance(body, BaseException):
                    raise body
                return body

        return _Session()


async def _run_attempts(*, client: _AttemptScriptedClient, config: TaiMCPConfig):
    with patch(_CLIENT, return_value=client):
        return await mcp_tool_call_wrapper(
            config=config,
            tool_name="list_messages",
            tool_input_model=_trivial_input_model(),
            tool_arguments={},
        )


def test_in_body_death_then_retry_connect_failure_returns_structured_unavailable(caplog):
    """First attempt dies in the body (ClientDisconnectedError), the retry's fresh
    connect fails at acquire time (ClientConnectError) → the wrapper converges on the
    structured ``upstream_mcp_unavailable`` result and records a health failure."""
    mcp_health._HEALTH.clear()
    client = _AttemptScriptedClient(
        attempts=[
            (None, ClientDisconnectedError("in-body death")),
            (ClientConnectError("MCP client connect failed: connection refused"), None),
        ]
    )

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(_run_attempts(client=client, config=_plain_http_config()))

    assert client.enter_count == 2, "exactly one retry — the second attempt fails at connect"
    assert client.call_count == 1, "the retry never reached call_tool_mcp — it died at acquire"

    payload = extract_connector_error_payload(result)
    assert payload is not None
    assert payload["code"] == "upstream_mcp_unavailable"
    assert "local_http" in payload["message"]
    # The raw connect text is never the consumer-facing message.
    assert "connection refused" not in payload["message"]
    assert "upstream unavailable" in caplog.text

    health = mcp_health.snapshot("local_http")
    assert health["last_error"]["type"] == "ClientConnectError"
    assert health["consecutive_failures"] == 1
    assert health["failing_since"] is not None


def test_cold_start_both_attempts_connect_failure_returns_structured_unavailable(caplog):
    """Cold start against a down upstream: BOTH the first dispatch and the retry
    fail at acquire time with ClientConnectError → the same structured
    ``upstream_mcp_unavailable`` result, health failure recorded."""
    mcp_health._HEALTH.clear()
    client = _AttemptScriptedClient(
        attempts=[
            (ClientConnectError("MCP client connect failed: connection refused"), None),
            (ClientConnectError("MCP client connect failed: connection refused"), None),
        ]
    )

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(_run_attempts(client=client, config=_plain_http_config()))

    assert client.enter_count == 2, "exactly one retry — both attempts fail at connect"
    assert client.call_count == 0, "no attempt ever reached call_tool_mcp"

    payload = extract_connector_error_payload(result)
    assert payload is not None
    assert payload["code"] == "upstream_mcp_unavailable"
    assert "local_http" in payload["message"]
    assert "connection refused" not in payload["message"]
    assert "upstream unavailable" in caplog.text

    health = mcp_health.snapshot("local_http")
    assert health["last_error"]["type"] == "ClientConnectError"
    assert health["consecutive_failures"] == 1


def test_non_disconnect_error_is_never_retried():
    """A non-disconnect error is never retried — the reconnect one-shot is scoped
    to ``ClientDisconnectedError`` alone."""

    class _Boom(RuntimeError):
        pass

    client = _FakeMcpClient(responses=[_Boom("unrelated failure")])

    with pytest.raises(_Boom, match="unrelated failure"):
        asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    assert client.call_count == 1, "a non-disconnect error must not trigger the reconnect retry"


def test_managed_uds_config_raises_in_preflight():
    """A managed entry on an unsupported transport (UDS) raises in the
    pre-flight, before any dispatch."""
    resolver = AsyncMock(return_value=ManagedAuth(access_token="tok"))

    uds_config = TaiMCPConfig(
        title="google_gmail_work",
        config=MCPConfig(type="uds", uds="/tmp/x.sock"),
        managed=ConnectorRef(
            connection_id=CONN_ID,
            provider_id="google",
            sub_service="gmail",
        ),
    )
    client = _FakeMcpClient(responses=[])  # never reached

    with patch(_RESOLVER, new=resolver), pytest.raises(RuntimeError, match="not supported"):
        asyncio.run(_run_wrapper(client=client, config=uds_config))

    assert client.call_count == 0, "wrapper must not dispatch"


def test_no_auth_forged_token_expired_does_not_force_refresh():
    """A no-auth managed entry (headers, no token) that returns a forged
    token_expired sentinel must NOT trigger force_refresh — the retry is gated
    on an OAuth token being present."""
    # No-auth resolution: headers, no access_token.
    resolver = AsyncMock(return_value=ManagedAuth(headers={"x_api_key": "k"}))
    refresher = AsyncMock()

    config = TaiMCPConfig(
        title="acme_api_prod",
        config=MCPConfig(type="http", url="https://acme.test/mcp", headers={}),
        managed=ConnectorRef(
            connection_id=CONN_ID,
            provider_id="acme",
            sub_service="api",
        ),
    )
    # Server forges the token_expired sentinel; the wrapper must NOT retry.
    client = _FakeMcpClient(responses=[_hub_error_result({"code": "token_expired"})])

    with patch(_RESOLVER, new=resolver), patch(_REFRESHER, new=refresher):
        asyncio.run(_run_wrapper(client=client, config=config))

    assert client.call_count == 1, "no-auth must not retry"
    refresher.assert_not_awaited()
    # The client's no-auth header was injected (no Authorization bearer).
    sent = client.captured_configs[0]["config"]["headers"]
    assert sent.get("x_api_key") == "k"
    assert "authorization" not in {k.lower() for k in sent}


# -- dispatch-seam health recording -------------------------------------------


def test_dispatch_seam_records_success():
    """A completed dispatch records a success and no failure for the config title."""
    mcp_health._HEALTH.clear()
    client = _FakeMcpClient(responses=[_ok_result({"result": "ok"})])

    asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    health = mcp_health.snapshot("local_http")
    assert health["last_success"] is not None
    assert health["last_error"] is None
    assert health["consecutive_failures"] == 0
    assert health["failing_since"] is None


def test_dispatch_seam_records_failure_on_upstream_unavailable():
    """The second-disconnect structured-error path records a health failure — the
    return value is unchanged (control flow is never altered by the recording)."""
    mcp_health._HEALTH.clear()
    client = _FakeMcpClient(
        responses=[
            ClientDisconnectedError("first"),
            ClientDisconnectedError("second"),
        ]
    )

    asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    health = mcp_health.snapshot("local_http")
    assert health["last_success"] is None
    assert health["last_error"]["type"] == "ClientDisconnectedError"
    assert health["consecutive_failures"] == 1
    assert health["failing_since"] is not None


def test_dispatch_seam_records_failure_on_raw_propagation():
    """A raw-propagating (non-disconnect) exception records a health failure first,
    then re-raises unchanged."""
    mcp_health._HEALTH.clear()

    class _Boom(RuntimeError):
        pass

    client = _FakeMcpClient(responses=[_Boom("unrelated failure")])

    with pytest.raises(_Boom, match="unrelated failure"):
        asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    health = mcp_health.snapshot("local_http")
    assert health["last_error"]["type"] == "_Boom"
    assert health["consecutive_failures"] == 1


def test_dispatch_seam_records_nothing_for_auth_blocked():
    """An auth-blocked call (resolver reconnect_required) records NOTHING — the
    seam signals connection health, not credential state, so the store stays at
    the all-empty block for the title."""
    mcp_health._HEALTH.clear()
    resolver = AsyncMock(side_effect=ConnectorReconnectRequiredError("invalid_grant", connection_id=CONN_ID))
    client = _FakeMcpClient(responses=[])

    with patch(_RESOLVER, new=resolver):
        asyncio.run(_run_wrapper(client=client, config=_managed_config()))

    assert mcp_health.snapshot("google_gmail_work") == {
        "last_success": None,
        "last_error": None,
        "consecutive_failures": 0,
        "failing_since": None,
    }


def test_dispatch_seam_records_success_after_reconnect_retry():
    """A first-dispatch disconnect that the one-shot retry recovers records a
    success — last_success set and no active failure run."""
    mcp_health._HEALTH.clear()
    client = _FakeMcpClient(
        responses=[
            ClientDisconnectedError("session died — retry the operation"),
            _ok_result({"result": "ok"}),
        ]
    )

    asyncio.run(_run_wrapper(client=client, config=_plain_http_config()))

    health = mcp_health.snapshot("local_http")
    assert health["last_success"] is not None
    assert health["consecutive_failures"] == 0
