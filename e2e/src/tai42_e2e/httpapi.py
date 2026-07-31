"""httpx helper for the skeleton admin HTTP API.

Success bodies are ``{"data": ...}`` and failures ``{"error": ...}``; this
wrapper unwraps ``data`` for the happy path and, on an unexpected status,
raises an assertion error that includes the raw response body so a failure is
actionable without a re-run. A few public routes (``/universal_webhook``,
``/health``) answer with a different or bare shape — use :meth:`request_raw`
for those."""

from __future__ import annotations

from typing import Any

import httpx

from tai42_e2e.waiting import wait_for_async


class ApiError(AssertionError):
    """An admin API call returned an unexpected status; carries the body."""


def _is_reloading(response: httpx.Response) -> bool:
    """Whether a response is the reload gate's retriable ``503 reloading`` rejection —
    the boot-time self-resync (or an in-flight config reload) holding the gate."""
    if response.status_code != 503:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("reloading") is True


class ApiClient:
    """Async admin-API client for one server port. Each call opens a short-lived
    connection, so the client is not bound to any event loop."""

    def __init__(self, base_url: str, *, auth_token: str | None = None, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {}
        if auth_token is not None:
            self._headers["Authorization"] = f"Bearer {auth_token}"

    def with_token(self, auth_token: str) -> ApiClient:
        """A sibling client for the same base URL authenticating as another key."""
        return ApiClient(self._base_url, auth_token=auth_token, timeout=self._timeout)

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Issue a request and return the raw response (no envelope unwrapping)."""
        merged = {**self._headers, **(headers or {})}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            return await client.request(method, path, json=json, headers=merged, content=content)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        expect: int = 200,
        retry_on_reloading: bool = False,
        ready_deadline: float = 10.0,
    ) -> Any:
        """Issue a request, assert the status, and return the unwrapped
        ``data``. Raises :class:`ApiError` with the body on a status mismatch.

        With ``retry_on_reloading=True`` a request that hits the boot-time reload gate
        (a retriable ``503 reloading``) is polled past on the sanctioned
        :func:`wait_for_async` interval until the worker leaves the gate or
        ``ready_deadline`` elapses — the retry every real client applies. A gated
        route fired the instant a stack is ready races the ~2s self-resync."""
        if retry_on_reloading:

            async def settled() -> httpx.Response | None:
                resp = await self.request_raw(method, path, json=json, headers=headers)
                return None if _is_reloading(resp) else resp

            response = await wait_for_async(
                settled, deadline=ready_deadline, message=f"{method} {path} never left the reload gate"
            )
        else:
            response = await self.request_raw(method, path, json=json, headers=headers)
        if response.status_code != expect:
            raise ApiError(f"{method} {path} -> {response.status_code} (expected {expect}); body: {response.text}")
        body = response.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    async def get(
        self, path: str, *, expect: int = 200, headers: dict[str, str] | None = None, retry_on_reloading: bool = False
    ) -> Any:
        return await self.request("GET", path, expect=expect, headers=headers, retry_on_reloading=retry_on_reloading)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        expect: int = 200,
        headers: dict[str, str] | None = None,
        retry_on_reloading: bool = False,
    ) -> Any:
        return await self.request(
            "POST", path, json=json, expect=expect, headers=headers, retry_on_reloading=retry_on_reloading
        )

    async def put(
        self,
        path: str,
        *,
        json: Any = None,
        expect: int = 200,
        headers: dict[str, str] | None = None,
        retry_on_reloading: bool = False,
    ) -> Any:
        return await self.request(
            "PUT", path, json=json, expect=expect, headers=headers, retry_on_reloading=retry_on_reloading
        )

    async def delete(
        self,
        path: str,
        *,
        json: Any = None,
        expect: int = 200,
        headers: dict[str, str] | None = None,
        retry_on_reloading: bool = False,
    ) -> Any:
        return await self.request(
            "DELETE", path, json=json, expect=expect, headers=headers, retry_on_reloading=retry_on_reloading
        )
