"""Tests for the ``register_whatsapp_template`` tool: the JSON POST body and URL, the returned
Graph response, argument validation, non-2xx propagation, and the token staying out of the raise."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_whatsapp.tools.register_whatsapp_template import register_whatsapp_template

_COMPONENTS = [{"type": "BODY", "text": "Hello {{1}}"}]


def _responder(*, code: int = 200, body: str = '{"id": "tpl_1", "status": "PENDING"}') -> Callable[..., Any]:
    def responder(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        return code, {"Content-Type": "application/json"}, body

    return responder


def _call(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "name": "welcome",
        "language": "en_US",
        "category": "UTILITY",
        "components": _COMPONENTS,
    }
    kwargs.update(overrides)
    return asyncio.run(register_whatsapp_template(**kwargs))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_posts_json_body(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder())

    result = _call()
    assert result == {"id": "tpl_1", "status": "PENDING"}

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/WABA_1/message_templates"
    assert request["headers"]["authorization"] == "Bearer tok-123"
    assert request["headers"]["content-type"] == "application/json"
    body = json.loads(request["body"])
    assert body == {"name": "welcome", "language": "en_US", "category": "UTILITY", "components": _COMPONENTS}


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("name", "", "name"),
        ("language", "", "language"),
        ("category", "", "category"),
        ("components", [], "components"),
    ],
)
def test_empty_argument_raises(field: str, value: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _call(**{field: value})


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates_without_leaking_token(whatsapp_env: Callable[..., None], stub_server: Any) -> None:
    whatsapp_env(access_token="super-secret-token", api_base_url=stub_server.base_url)
    stub_server.set_responder(_responder(code=400, body='{"error": {"message": "invalid category"}}'))
    with pytest.raises(ValueError, match="register failed") as excinfo:
        _call()
    message = str(excinfo.value)
    assert "400" in message
    assert "invalid category" in message
    assert "super-secret-token" not in message


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_whatsapp.tools.register_whatsapp_template")
    assert set(app.tools.registered) == {"register_whatsapp_template"}
