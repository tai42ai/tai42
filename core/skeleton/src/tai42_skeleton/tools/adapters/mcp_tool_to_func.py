import keyword
import logging
from collections.abc import Callable
from inspect import Parameter, Signature
from typing import Any, cast

import mcp
import pydantic_core
from makefun import create_function
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from tai42_contract.errors import ClientDisconnectedError
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.monitoring import MonitoringLevel
from tai42_kit.clients.impl.mcp import FastMCPClient
from tai42_kit.utils.data.json_schema_util import json_schema_to_pydantic_model
from tai42_kit.utils.data.mcp_output_util import (
    extract_tool_error,
    extract_tool_output,
    tool_has_error,
)
from tai42_kit.utils.data.string_util import makefun_func_name

from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorAuthExpiredError,
    ConnectorConnectionError,
)
from tai42_skeleton.connectors.token_injection import (
    call_with_auth,
    check_managed_transport,
    extract_connector_error_payload,
    handle_token_expired,
    is_token_expired,
    managed_auth_error_result,
    resolve_managed_auth_for_config,
    upstream_mcp_unavailable_result,
)
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.settings.mcp_settings import mcp_dispatch_settings
from tai42_skeleton.tools import mcp_health

logger = logging.getLogger(__name__)

# C0 controls (incl. CR/LF), DEL, and the Unicode line-boundary characters
# (U+0085 NEL, U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR), each mapped to
# a visible ``\x``-prefixed hex escape so an untrusted name cannot inject a line break.
_LOG_ESCAPE = {c: f"\\x{c:02x}" for c in (*range(0x20), 0x7F, 0x85, 0x2028, 0x2029)}


def _log_safe(name: str) -> str:
    """A downstream-advertised tool name rendered safe for a log line: its control
    and line-boundary characters cannot forge or split a log record. Downstream
    names are untrusted."""
    return name.translate(_LOG_ESCAPE)


def _exposed_name(field_name: str, field: FieldInfo) -> str:
    """The signature parameter presented for a model field.

    The presented signature IS the tool's advertised contract, so a usable alias
    (the original JSON property name) is exposed verbatim; an alias that is not a
    valid Python identifier or is a keyword keeps the sanitized field name — an
    inherent limit of callable-shaped contracts. Single source for both the built
    signature and the kwarg lookup that inverts it, so the two cannot drift.
    """
    alias = field.alias
    if alias and alias.isidentifier() and not keyword.iskeyword(alias):
        return alias
    return field_name


def _build_signature(
    input_model: type[BaseModel],
    output_schema: dict[str, Any],
    schema_max_depth: int,
) -> Signature:
    return_annotation = json_schema_to_pydantic_model(output_schema, "OutputModel", max_depth=schema_max_depth)

    params = []
    for name, field in input_model.model_fields.items():
        default = Parameter.empty if field.default is pydantic_core.PydanticUndefined else field.default
        params.append(
            Parameter(
                name=_exposed_name(name, field),
                kind=Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )

    return Signature(parameters=params, return_annotation=return_annotation)


def _build_input_model(tool, schema_max_depth: int, model_name: str = "InputModel"):
    input_schema_dict = tool.inputSchema or {"type": "object", "properties": {}}
    return json_schema_to_pydantic_model(input_schema_dict, model_name, max_depth=schema_max_depth)


def _wraps_result_envelope(output_schema_dict: dict[str, Any]) -> bool:
    """Whether the child advertised its output under a single ``result`` wrapper.

    A FastMCP server auto-wraps a NON-object tool return (a list or scalar) as
    ``{"result": <value>}`` and flags the wrap with the ``x-fastmcp-wrap-result``
    marker on the advertised object output schema. That marker is the
    AUTHORITATIVE signal — FastMCP itself gates unwrapping on it — so gating
    structurally (an object schema that merely has a ``result`` property) would
    false-fire on a genuine object-returning tool with a ``result`` field and
    wrongly strip its sibling fields / unwrap its value. The mount strips the
    wrapper from BOTH the forwarded schema (:func:`_build_output_schema`) and —
    symmetrically — the forwarded value (:func:`_unwrap_result_envelope`), so
    this server's own FastMCP re-wrap produces a single, schema-matching envelope
    instead of a double ``{"result": {"result": ...}}`` the strict-client
    validator rejects.
    """
    return output_schema_dict.get("x-fastmcp-wrap-result") is True


def _unwrap_result_envelope(output: Any) -> Any:
    """Strip the child's single ``result`` wrapper off a forwarded structured value.

    Applied only when the child advertised the wrap (see
    :func:`_wraps_result_envelope`). A ``{"result": <value>}`` dict yields its
    inner value; any other shape — an error response (a ``CallToolResult``, not a
    plain dict) or a value the child did not wrap — passes through untouched.
    """
    if isinstance(output, dict) and set(output) == {"result"}:
        return output["result"]
    return output


def _build_output_schema(tool):
    output_schema_dict = tool.outputSchema or {}
    if _wraps_result_envelope(output_schema_dict):
        # Copy the nested schema before annotating it — writing ``$defs`` onto the
        # aliased dict would inject a key into the caller's ``mcp.Tool`` that the
        # server never sent.
        inner_output_schema = {**output_schema_dict["properties"]["result"]}
        if "$defs" in output_schema_dict:
            inner_output_schema["$defs"] = output_schema_dict["$defs"]
        return inner_output_schema
    return output_schema_dict


def _build_input_value(input_model: type[BaseModel], **kwargs):
    data = {}
    for field_name, field in input_model.model_fields.items():
        # makefun binds kwargs by the EXPOSED signature name, so invert with the
        # same mapping; validation still keys by alias.
        exposed = _exposed_name(field_name, field)
        if exposed in kwargs:
            data[field.alias or field_name] = kwargs[exposed]
    input_instance = input_model.model_validate(data)
    return input_instance.model_dump(exclude_none=True, by_alias=True)


# -- Transport dispatch -------------------------------------------------------


_FIELD_TO_TRANSPORT = {"command": "stdio", "url": "http", "uds": "uds"}


def _detect_transport(inner: MCPConfig) -> str:
    """Return ``"stdio"`` | ``"http"`` | ``"uds"`` from the set transport field.

    Raises if no field or more than one is set.
    """
    flags = [
        k
        for k, v in (
            ("command", inner.command),
            ("url", inner.url),
            ("uds", inner.uds),
        )
        if v
    ]
    if not flags:
        raise RuntimeError("MCPConfig has no transport set (need command, url, or uds)")
    if len(flags) > 1:
        raise RuntimeError(f"MCPConfig has ambiguous transport: {flags!r}")
    return _FIELD_TO_TRANSPORT[flags[0]]


# -- Call wrapper -------------------------------------------------------------


async def mcp_tool_call_wrapper(
    config: TaiMCPConfig,
    tool_name: str,
    tool_input_model: type[BaseModel],
    tool_arguments: dict[str, Any],
):
    """Pre-flight, dispatch, token-expired retry. ``is_managed`` (immutable)
    gates every connector-wired branch.

    The ``FastMCPClient`` pool object is built here and handed to the callees;
    each ``call_with_auth`` opens its own per-config connection through it
    (``mcp_client.current(config=...)``), pooled per event-loop. The retry path
    rotates the config (rotated Authorization) and so opens a distinct
    connection — there is no single stable config to pin at this level.
    """
    mcp_client = FastMCPClient()
    arguments = _build_input_value(tool_input_model, **tool_arguments)
    transport = _detect_transport(config.config)
    check_managed_transport(config, transport)

    async def _dispatch():
        """Resolve auth, dispatch the call, and drive the token-expired retry.

        Re-runnable end to end: auth is re-resolved on every call so a reconnect
        re-run picks up a token rotated in the meantime and opens a fresh pooled
        session (the prior one was already evicted on disconnect).
        """
        auth = await resolve_managed_auth_for_config(config)

        response = await call_with_auth(
            config,
            auth,
            transport,
            tool_name,
            arguments,
            mcp_client,
        )

        # Only an OAuth managed entry (resolved a token) can token-expire; a no-auth
        # entry has no token to refresh, so a forged token_expired must not drive
        # force_refresh (which raises for no-auth).
        if config.is_managed and auth is not None and auth.access_token:
            payload = extract_connector_error_payload(response)
            if is_token_expired(payload):
                response = await handle_token_expired(
                    config,
                    transport,
                    tool_name,
                    arguments,
                    mcp_client,
                    auth,
                    failed_access_token=auth.access_token,
                )
        return response

    try:
        try:
            response = await _dispatch()
        except ClientDisconnectedError:
            # An unavailable pooled session — a mid-run disconnect (downstream MCP
            # restarted, evicting the dead session) OR a connect/init failure
            # building the session (ClientConnectError, e.g. a down upstream on
            # cold start) — gets exactly ONE fresh-session retry.
            logger.warning(
                "MCP dispatch lost its session or could not connect — retrying once (mcp='%s' tool='%s')",
                config.title,
                _log_safe(tool_name),
            )
            try:
                response = await _dispatch()
            except ClientDisconnectedError as exc:
                # The retry also failed (dead session again or a connect that still
                # could not build): the upstream MCP is unavailable. Convert to a
                # structured tool-error result so the raw fastmcp disconnect/connect
                # text never reaches the tool-call consumer — it lands in this log
                # line and the health store's last_error, both operator surfaces.
                logger.error(
                    "MCP reconnect failed — upstream unavailable (mcp='%s' tool='%s')",
                    config.title,
                    _log_safe(tool_name),
                    exc_info=True,
                )
                mcp_health.record_failure(config.title, exc)
                response = upstream_mcp_unavailable_result(config)
            else:
                mcp_health.record_success(config.title)
        else:
            mcp_health.record_success(config.title)
    except (ConnectorConnectionError, ConnectorAuthExpiredError) as exc:
        # A managed call blocked on user action — invalid_grant (reconnect),
        # refresh budget exhausted, or auth still expired after a forced refresh —
        # is surfaced as a structured connector-error result, not a raw exception,
        # so a client can offer a reconnect instead of showing generic error text.
        # It then flows through the same span-annotation + output path below as any
        # tool error.
        response = managed_auth_error_result(exc)
    except Exception as exc:
        # Any other dispatch failure propagates raw to the caller unchanged; record
        # the health failure first so the status op sees it, then re-raise.
        mcp_health.record_failure(config.title, exc)
        raise

    # An error response carries no usable output; annotate the active span here
    # (the extraction util is framework-agnostic and does not touch monitoring)
    # and hand back the response unchanged via extract_tool_output.
    if tool_has_error(response):
        error_text = extract_tool_error(response)
        get_monitoring().writer.update_current_span(level=MonitoringLevel.ERROR, status_message=error_text)

    return extract_tool_output(response)


def mcp_tool_to_func(
    config: TaiMCPConfig, tool: mcp.Tool, name: str, module: str, schema_max_depth: int | None = None
) -> Callable:
    # Resolve the schema-depth bound once. The binding pass passes it in — resolved
    # OUTSIDE its per-tool skip guard, so a malformed TAI_MCP_SCHEMA_MAX_DEPTH fails
    # loudly as a config error rather than being caught per-tool and mis-logged as
    # every tool having an unusable schema. A direct caller resolves it from settings.
    depth = schema_max_depth if schema_max_depth is not None else mcp_dispatch_settings().schema_max_depth
    tool_name = tool.name
    tool_input_model = _build_input_model(tool, depth)
    inner_output_schema = _build_output_schema(tool)
    sig = _build_signature(tool_input_model, inner_output_schema, depth)
    # The output schema forwarded above is unwrapped when the child auto-wrapped a
    # non-object return under ``result``; the runtime value must be unwrapped the
    # same way, so re-binding this tool onto THIS server's ``/mcp`` re-wraps it once
    # (schema and emitted ``structuredContent`` stay a single, consistent wrap).
    wraps_result = _wraps_result_envelope(tool.outputSchema or {})

    async def func_impl(**kwargs):
        output = await mcp_tool_call_wrapper(config, tool_name, tool_input_model, kwargs)
        return _unwrap_result_envelope(output) if wraps_result else output

    safe_name = makefun_func_name(name)
    return create_function(
        func_signature=sig,
        # makefun's ``func_impl`` is annotated ``Callable[[Any], Any]`` but accepts
        # any callable, driven by ``func_signature``.
        func_impl=cast(Callable[[Any], Any], func_impl),
        func_name=safe_name,
        qualname=safe_name,
        module_name=module,
        # makefun's ``doc`` is annotated ``str`` but accepts ``None`` (its default).
        doc=cast(str, tool.description),
    )
