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
    sys.modules.pop("tai42_channel_whatsapp.inbound", None)
    stub_app.channels.registered.clear()
    stub_app.http.routes.clear()

    importlib.import_module("tai42_channel_whatsapp.register")

    assert list(stub_app.channels.registered) == ["whatsapp"]
    assert isinstance(stub_app.channels.registered["whatsapp"], WhatsAppChannel)
    paths = {route.path for route in stub_app.http.routes}
    assert paths == {"/inbound"}
    assert len(stub_app.http.routes) == 1
    route = stub_app.http.routes[0]
    assert route.methods == ["GET", "POST"]
    # The route is declared relative and passes no explicit ``authed``: the mount
    # base and the public flag come from ``tai-plugin.yml``, resolved by the runtime.
    assert route.authed is None


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
