"""Registration: importing the register module fires the channel + route side-effects;
importing the bare package registers nothing (library-safe)."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

from tai42_contract.channels import Channel

import tai42_channel_whatsapp
from tai42_channel_whatsapp import WhatsAppChannel


def test_importing_register_registers_channel_and_route(stub_app):
    sys.modules.pop("tai42_channel_whatsapp.register", None)
    sys.modules.pop("tai42_channel_whatsapp.provisioning_route", None)
    # ``inbound`` is a package: evict it AND every submodule so re-importing it re-runs
    # the route-registration side-effect (importing the package alone would find the
    # cached ``webhook`` submodule and skip the @custom_route decorator).
    for name in [
        key
        for key in sys.modules
        if key == "tai42_channel_whatsapp.inbound" or key.startswith("tai42_channel_whatsapp.inbound.")
    ]:
        sys.modules.pop(name, None)
    stub_app.channels.registered.clear()
    stub_app.http.routes.clear()

    importlib.import_module("tai42_channel_whatsapp.register")

    assert list(stub_app.channels.registered) == ["whatsapp"]
    assert isinstance(stub_app.channels.registered["whatsapp"], WhatsAppChannel)
    routes = {route.path: route for route in stub_app.http.routes}
    # The webhook (GET verification + POST events), the reacting-form Flow data endpoint, and the
    # admin door that registers the Flow encryption public key.
    assert set(routes) == {"/inbound", "/flow-data", "/flow-encryption"}
    assert routes["/inbound"].methods == ["GET", "POST"]
    assert routes["/flow-data"].methods == ["POST"]
    assert routes["/flow-encryption"].methods == ["POST"]
    # Every route is declared relative with no explicit ``authed``: the mount base and the public
    # flag come from ``tai-plugin.yml``, resolved by the runtime. The provisioning door is the
    # admin-only ``fenced`` class; the two vendor doors leave the class to the runtime.
    for path in ("/inbound", "/flow-data", "/flow-encryption"):
        assert routes[path].authed is None
    assert routes["/flow-encryption"].action == "fenced"
    assert routes["/inbound"].action is None
    assert routes["/flow-data"].action is None


def test_the_registered_channel_declares_an_identity_hint(stub_app):
    sys.modules.pop("tai42_channel_whatsapp.register", None)
    stub_app.channels.registered.clear()

    importlib.import_module("tai42_channel_whatsapp.register")

    hint = getattr(stub_app.channels.registered["whatsapp"], "identity_hint", None)
    assert isinstance(hint, str)
    assert hint.strip()


def test_bare_package_import_does_not_register():
    # `import tai42_channel_whatsapp` (library use) must not touch the app
    # handle; only the register module carries the side-effect. Checked in a clean
    # subprocess (no stub app bound, no CHANNEL_WHATSAPP_* env) so the
    # in-process module cache cannot mask it.
    code = "import sys; import tai42_channel_whatsapp; assert 'tai42_channel_whatsapp.register' not in sys.modules"
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHANNEL_WHATSAPP_")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_channel_satisfies_the_channel_protocol():
    assert isinstance(WhatsAppChannel(), Channel)


def test_package_exports():
    assert tai42_channel_whatsapp.__all__ == [
        "WhatsAppChannel",
        "WhatsAppSettings",
        "whatsapp_settings",
    ]
    for name in tai42_channel_whatsapp.__all__:
        assert getattr(tai42_channel_whatsapp, name) is not None
