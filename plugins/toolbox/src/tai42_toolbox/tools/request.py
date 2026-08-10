"""The ``request`` tool: an HTTP request backed by tai42-kit's pooled curl client.

Requests without a ``session_key`` use a one-shot session; a ``session_key`` reuses a pooled session
(shared cookies and connections) across calls. Execution, serialization, and SSRF pinning live in
:mod:`tai42_toolbox._internal.tools.http_client`. Needs the ``http`` extra; fails loudly at import.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_toolbox._internal.tools.http_client import perform_request


@tai42_app.tools.tool(tags={"http"})
async def request(
    url: str,
    method: str = "GET",
    session_key: str | None = None,
    clear_session_cookies: bool = True,
    session_params: dict[str, Any] | None = None,
    request_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an HTTP request through a curl-backed session.

    Args:
        url: The target URL.
        method: HTTP method (e.g. ``"GET"``, ``"POST"``). Defaults to ``"GET"``.
        session_key: Reuse key. Requests sharing a key share cookies and
            connections; omit it for a one-shot session.
        clear_session_cookies: Clear cookies before the request. Defaults to
            ``True``.
        session_params: ``AsyncSession`` configuration (e.g.
            ``{"impersonate": "chrome110", "proxies": {...}}``).
        request_params: ``session.request`` configuration (e.g.
            ``{"headers": {...}, "json": {...}}``).

    Returns:
        The response data (status, body, headers, cookies, timing, and the
        request's transfer metrics — measured across the full body read when the
        guard is on, otherwise curl's own values).
    """
    return await perform_request(
        url,
        method=method,
        session_key=session_key,
        clear_session_cookies=clear_session_cookies,
        session_params=session_params,
        request_params=request_params,
    )
