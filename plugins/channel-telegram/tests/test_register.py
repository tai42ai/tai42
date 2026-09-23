"""Registration side-effects: importing the register module — and ONLY it —
registers the channel, the inbound route, and the setWebhook startup hook."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram import TelegramChannel

_TOKEN = "123456:test-token"


def _import_register_module(stub_app):
    """(Re-)import the register module so its import side-effects fire."""
    stub_app.channels.registered.clear()
    stub_app.http.routes.clear()
    stub_app.lifecycle.startup_hooks.clear()
    sys.modules.pop("tai42_channel_telegram.register", None)
    sys.modules.pop("tai42_channel_telegram.inbound", None)
    importlib.import_module("tai42_channel_telegram.register")


def test_import_registers_channel_route_and_hook(stub_app):
    _import_register_module(stub_app)

    assert list(stub_app.channels.registered) == ["telegram"]
    assert isinstance(stub_app.channels.registered["telegram"], TelegramChannel)
    inbound_routes = [r for r in stub_app.http.routes if r.path == "/inbound"]
    assert len(inbound_routes) == 1
    assert len(stub_app.lifecycle.startup_hooks) == 1


def test_reimport_fires_registration_again(stub_app):
    # A live reload re-imports the module (popped from sys.modules first) and
    # must re-fire all three registrations against the fresh registries.
    _import_register_module(stub_app)
    _import_register_module(stub_app)
    assert list(stub_app.channels.registered) == ["telegram"]
    assert len([r for r in stub_app.http.routes if r.path == "/inbound"]) == 1
    assert len(stub_app.lifecycle.startup_hooks) == 1


def test_duplicate_registration_raises(stub_app):
    _import_register_module(stub_app)
    sys.modules.pop("tai42_channel_telegram.register", None)
    with pytest.raises(ValueError, match="'telegram' is already registered"):
        importlib.import_module("tai42_channel_telegram.register")


def test_package_import_alone_does_not_register():
    # `import tai42_channel_telegram` (library use) must not touch the app handle;
    # only the register module carries the side-effect. Checked in a clean
    # subprocess (no stub app bound, no CHANNEL_TELEGRAM_* env) so the
    # in-process module cache cannot mask it.
    code = "import sys; import tai42_channel_telegram; assert 'tai42_channel_telegram.register' not in sys.modules"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHANNEL_TELEGRAM_")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


async def test_startup_hook_sets_webhook(stub_app, http_recorder):
    _import_register_module(stub_app)
    await stub_app.lifecycle.startup_hooks[0]()

    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/setWebhook"
    assert json.loads(request.content) == {
        "url": "https://example.test/api/channels/telegram/inbound",
        "secret_token": "s3cret_token",
        "allowed_updates": ["message", "callback_query"],
    }


async def test_startup_hook_normalizes_trailing_slash(stub_app, http_recorder, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_PUBLIC_BASE_URL", "https://example.test/")
    reset_all_settings()
    _import_register_module(stub_app)
    await stub_app.lifecycle.startup_hooks[0]()

    body = json.loads(http_recorder.requests[0].content)
    assert body["url"] == "https://example.test/api/channels/telegram/inbound"


async def test_startup_hook_follows_remapped_mount_base(stub_app, http_recorder, monkeypatch: pytest.MonkeyPatch):
    # An operator-remapped route base moves the inbound mount; setWebhook must point
    # at the resolved mount register captured at import, not the default path.
    monkeypatch.setattr(stub_app.http, "mount_base_value", "/api/channels/tg-remap")
    _import_register_module(stub_app)
    await stub_app.lifecycle.startup_hooks[0]()

    body = json.loads(http_recorder.requests[0].content)
    assert body["url"] == "https://example.test/api/channels/tg-remap/inbound"


async def test_startup_hook_raises_on_ok_false(stub_app, http_recorder):
    _import_register_module(stub_app)
    http_recorder.responder = lambda request: httpx.Response(
        200,
        json={"ok": False, "error_code": 401, "description": "Unauthorized", "parameters": {"retry_after": 3}},
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=401") as excinfo:
        await stub_app.lifecycle.startup_hooks[0]()
    # The full documented error rides the message, none dropped.
    assert "description='Unauthorized'" in str(excinfo.value)
    assert 'parameters={"retry_after":3}' in str(excinfo.value)


async def test_startup_hook_raises_on_http_error(stub_app, http_recorder):
    _import_register_module(stub_app)
    # Telegram answers its structured error on the HTTP-error status too — the non-200
    # arm is a live vendor-error path, so the detail surfaces error_code/description.
    http_recorder.responder = lambda request: httpx.Response(
        403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot blocked"}
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=403") as excinfo:
        await stub_app.lifecycle.startup_hooks[0]()
    assert "Forbidden: bot blocked" in str(excinfo.value)


async def test_startup_hook_non_json_error_body_falls_back_to_bounded_text(stub_app, http_recorder):
    _import_register_module(stub_app)
    http_recorder.responder = lambda request: httpx.Response(502, text="<html>" + "x" * 2000 + "</html>")
    with pytest.raises(ChannelDeliveryError, match="setWebhook rejected") as excinfo:
        await stub_app.lifecycle.startup_hooks[0]()
    assert len(str(excinfo.value)) < 600  # detail bounded to 500 chars


async def test_startup_hook_missing_public_base_url_aborts(stub_app, http_recorder, monkeypatch: pytest.MonkeyPatch):
    _import_register_module(stub_app)
    monkeypatch.delenv("CHANNEL_TELEGRAM_PUBLIC_BASE_URL")
    reset_all_settings()
    with pytest.raises(ValueError, match="set CHANNEL_TELEGRAM_PUBLIC_BASE_URL"):
        await stub_app.lifecycle.startup_hooks[0]()
    assert http_recorder.requests == []


async def test_startup_hook_no_recipients_aborts(stub_app, http_recorder, monkeypatch: pytest.MonkeyPatch):
    _import_register_module(stub_app)
    monkeypatch.delenv("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    monkeypatch.delenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")
    reset_all_settings()
    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_DEFAULT_RECIPIENT"):
        await stub_app.lifecycle.startup_hooks[0]()
    assert http_recorder.requests == []


async def test_startup_hook_empty_webhook_secret_aborts(stub_app, http_recorder, monkeypatch: pytest.MonkeyPatch):
    _import_register_module(stub_app)
    monkeypatch.setenv("CHANNEL_TELEGRAM_WEBHOOK_SECRET", "")
    reset_all_settings()
    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_WEBHOOK_SECRET"):
        await stub_app.lifecycle.startup_hooks[0]()
    assert http_recorder.requests == []
