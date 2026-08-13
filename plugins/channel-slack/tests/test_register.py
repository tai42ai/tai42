"""Registration: importing the register module registers the channel and the
inbound route; a bare package import registers nothing (library-safe split)."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest
from tai42_contract.channels import Channel

from tai42_channel_slack import SlackChannel


def _import_register_module(stub_app) -> None:
    """(Re-)import the register module so its side-effects fire freshly."""
    stub_app.channels.registered.clear()
    stub_app.http.routes.clear()
    sys.modules.pop("tai42_channel_slack.register", None)
    sys.modules.pop("tai42_channel_slack.inbound", None)
    importlib.import_module("tai42_channel_slack.register")


def test_import_registers_channel_and_inbound_route(stub_app):
    _import_register_module(stub_app)

    assert isinstance(stub_app.channels.registered["slack"], SlackChannel)
    route = stub_app.http.routes["/api/channels/slack/inbound"]
    assert route.methods == ["POST"]
    assert route.authed is False
    assert route.summary
    assert route.tags == ["channels"]


def test_import_registers_the_interactivity_route(stub_app):
    _import_register_module(stub_app)

    route = stub_app.http.routes["/api/channels/slack/interactive"]
    assert route.methods == ["POST"]
    assert route.authed is False
    assert route.summary
    assert route.tags == ["channels"]


def test_slack_channel_advertises_form_delivery():
    assert SlackChannel.supports_form_delivery is True


def test_duplicate_registration_raises(stub_app):
    _import_register_module(stub_app)

    # Re-import WITHOUT clearing the captures: the second module-level
    # register call must raise through the registry's duplicate guard.
    sys.modules.pop("tai42_channel_slack.register", None)
    with pytest.raises(ValueError, match="already registered"):
        importlib.import_module("tai42_channel_slack.register")


def test_bare_package_import_does_not_register():
    # `import tai42_channel_slack` (library use) must not touch the app handle or
    # demand any CHANNEL_SLACK_* env; only the register module carries the
    # side-effect. Checked in a clean subprocess (no stub app bound, no env)
    # so the in-process module cache cannot mask it.
    code = "import sys; import tai42_channel_slack; assert 'tai42_channel_slack.register' not in sys.modules"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHANNEL_SLACK_")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_slack_channel_satisfies_the_contract_protocol():
    assert isinstance(SlackChannel(), Channel)
