"""Tests for the ``list_whatsapp_templates`` tool: the GET URL, paging-cursor exhaustion, the
single-page case, and non-2xx propagation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_whatsapp.tools.list_whatsapp_templates import list_whatsapp_templates


def _page(data: list[dict[str, Any]], next_url: str | None = None) -> str:
    payload: dict[str, Any] = {"data": data}
    if next_url is not None:
        payload["paging"] = {"next": next_url}
    return json.dumps(payload)


def _pages_responder(*pages: str) -> Callable[..., Any]:
    """Answer successive requests with successive ``pages`` bodies (the last repeated)."""
    state = {"index": 0}

    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        body = pages[min(state["index"], len(pages) - 1)]
        state["index"] += 1
        return 200, {"Content-Type": "application/json"}, body

    return responder


@pytest.mark.usefixtures("curl_app")
def test_single_page_returns_data(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_pages_responder(_page([{"name": "a"}, {"name": "b"}])))

    result = asyncio.run(list_whatsapp_templates())
    assert result == [{"name": "a"}, {"name": "b"}]

    request = stub_server.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/WABA_1/message_templates"
    assert request["headers"]["authorization"] == "Bearer tok-123"


@pytest.mark.usefixtures("curl_app")
def test_paging_followed_to_exhaustion(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    templates_url = f"{stub_server.base_url}/WABA_1/message_templates"
    stub_server.set_responder(
        _pages_responder(
            _page([{"name": "a"}], next_url=f"{templates_url}?after=c1"),
            _page([{"name": "b"}], next_url=f"{templates_url}?after=c2"),
            _page([{"name": "c"}]),
        )
    )

    result = asyncio.run(list_whatsapp_templates())
    assert result == [{"name": "a"}, {"name": "b"}, {"name": "c"}]

    # Three pages fetched; pages two and three used the cursor the prior page named.
    assert len(stub_server.requests) == 3
    assert [r["path"] for r in stub_server.requests] == ["/WABA_1/message_templates"] * 3
    assert [r["query"].get("after") for r in stub_server.requests] == [None, ["c1"], ["c2"]]


@pytest.mark.usefixtures("curl_app")
def test_list_raises_at_page_ceiling(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    # A self-referential ``paging.next`` that never terminates: the ceiling must stop the follow.
    next_url = f"{stub_server.base_url}/WABA_1/message_templates?after=loop"
    stub_server.set_responder(lambda _r: (200, {"Content-Type": "application/json"}, _page([{"name": "x"}], next_url)))

    with pytest.raises(ValueError, match="page ceiling"):
        asyncio.run(list_whatsapp_templates())
    # Exactly the ceiling number of pages were fetched before the raise, never one more.
    assert len([r for r in stub_server.requests if r["method"] == "GET"]) == 100


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(lambda _r: (500, {"Content-Type": "text/plain"}, "upstream boom"))
    with pytest.raises(ValueError, match="list failed") as excinfo:
        asyncio.run(list_whatsapp_templates())
    assert "500" in str(excinfo.value)
    assert "upstream boom" in str(excinfo.value)


@pytest.mark.usefixtures("curl_app")
def test_list_does_not_follow_redirect_and_hides_token(
    whatsapp_env: Callable[..., None], stub_server: Any, local_server: Any
) -> None:
    whatsapp_env(access_token="super-secret-token", api_base_url=stub_server.base_url)
    target = f"{local_server.base_url}/WABA_1/message_templates"
    stub_server.set_responder(lambda _r: (302, {"Location": target}, ""))

    with pytest.raises(ValueError, match="list failed: HTTP 302") as excinfo:
        asyncio.run(list_whatsapp_templates())
    # The redirect target recorded no request: the Authorization header never left the pinned host.
    assert local_server.requests == []
    message = str(excinfo.value)
    assert "super-secret-token" not in message
    assert "Bearer" not in message


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_whatsapp.tools.list_whatsapp_templates")
    assert set(app.tools.registered) == {"list_whatsapp_templates"}
