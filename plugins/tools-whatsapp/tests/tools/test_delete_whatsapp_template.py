"""Tests for the ``delete_whatsapp_template`` tool: the DELETE to the templates endpoint with the
name query, percent-encoding of a special-character name, the empty-name guard, and non-2xx
propagation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_whatsapp.tools.delete_whatsapp_template import delete_whatsapp_template


def _responder(*, code: int = 200, body: str = '{"success": true}') -> Callable[..., Any]:
    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return code, {"Content-Type": "application/json"}, body

    return responder


@pytest.mark.usefixtures("curl_app")
def test_happy_path_deletes_by_name(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = asyncio.run(delete_whatsapp_template("welcome"))
    assert result == {"success": True}

    request = stub_server.requests[0]
    assert request["method"] == "DELETE"
    assert request["path"] == "/WABA_1/message_templates"
    assert request["query"]["name"] == ["welcome"]
    assert request["body"] == ""


@pytest.mark.usefixtures("curl_app")
def test_name_is_percent_encoded_into_query(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder(body="{}"))

    # A raw '&'/'=' would split into extra params if unencoded; a single decoded name proves the
    # value was percent-encoded before it reached the wire.
    asyncio.run(delete_whatsapp_template("a b&c=d"))
    request = stub_server.requests[0]
    assert request["query"] == {"name": ["a b&c=d"]}


def test_empty_name_raises() -> None:
    with pytest.raises(ValueError, match="name"):
        asyncio.run(delete_whatsapp_template(""))


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder(code=404, body='{"error": {"message": "no such template"}}'))
    with pytest.raises(ValueError, match="delete failed") as excinfo:
        asyncio.run(delete_whatsapp_template("welcome"))
    assert "404" in str(excinfo.value)
    assert "no such template" in str(excinfo.value)


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_whatsapp.tools.delete_whatsapp_template")
    assert set(app.tools.registered) == {"delete_whatsapp_template"}
