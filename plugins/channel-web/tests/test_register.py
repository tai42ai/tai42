"""Registration: importing the register module fires the channel + route side-effects;
importing the bare package registers nothing (library-safe)."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

from tai42_contract.channels import Channel

import tai42_channel_web
from tai42_channel_web import WebChannel


def test_importing_register_registers_channel_and_routes(stub_app):
    # Re-importing register (and, via it, routes) fires the side effects afresh; the
    # module objects and the shared stub-app registrations are snapshotted and
    # restored so a re-imported ``routes`` module never leaks a second, non-identical
    # copy of its handler classes into a later test.
    saved_modules = {name: sys.modules.get(name) for name in ("tai42_channel_web.register", "tai42_channel_web.routes")}
    saved_channels = dict(stub_app.channels.registered)
    saved_routes = list(stub_app.http.routes)
    for name in saved_modules:
        sys.modules.pop(name, None)
    stub_app.channels.registered.clear()
    stub_app.http.routes.clear()
    try:
        importlib.import_module("tai42_channel_web.register")

        assert list(stub_app.channels.registered) == ["web"]
        assert isinstance(stub_app.channels.registered["web"], WebChannel)
        # Routes are declared RELATIVE to the item's mount base; the runtime resolves
        # them to absolute paths and the public flag from ``tai-plugin.yml``, so the
        # module passes no explicit ``authed``.
        paths = {route.path for route in stub_app.http.routes}
        assert paths == {
            "/chat/{identity}",
            "/assets/{file}",
            "/messages",
            "/stream",
            "/questions/{interaction_id}/answer",
            "/session/rotate",
            "/gates/{identity}",
            "/gates/{identity}/codes",
            "/gates/{identity}/codes/{code_id}",
        }
        # The module defers auth to the declaration for every door (no explicit
        # ``authed``). The chat doors pass no action; the entry-gate management doors
        # each declare an explicit action-class (an authed door with none refuses to
        # register).
        assert all(route.authed is None for route in stub_app.http.routes)
        public = {route.path for route in stub_app.http.routes if route.action is None}
        managed = {
            (route.path, tuple(route.methods), route.action)
            for route in stub_app.http.routes
            if route.action is not None
        }
        assert public == {
            "/chat/{identity}",
            "/assets/{file}",
            "/messages",
            "/stream",
            "/questions/{interaction_id}/answer",
            "/session/rotate",
        }
        assert managed == {
            ("/gates/{identity}", ("GET",), "read"),
            ("/gates/{identity}", ("PUT",), "write"),
            ("/gates/{identity}/codes", ("POST",), "write"),
            ("/gates/{identity}/codes/{code_id}", ("DELETE",), "write"),
        }
    finally:
        for name, module in saved_modules.items():
            if module is not None:
                sys.modules[name] = module
        stub_app.channels.registered.clear()
        stub_app.channels.registered.update(saved_channels)
        stub_app.http.routes[:] = saved_routes


def test_bare_package_import_does_not_register():
    # `import tai42_channel_web` (library use) must not touch the app handle; only
    # the register module carries the side-effect. Checked in a clean subprocess (no
    # stub app bound, no CHANNEL_WEB_* env) so the module cache cannot mask it.
    code = "import sys; import tai42_channel_web; assert 'tai42_channel_web.register' not in sys.modules"
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHANNEL_WEB_")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_channel_satisfies_the_channel_protocol():
    assert isinstance(WebChannel(), Channel)


def test_channel_advertises_no_capability_flags():
    channel = WebChannel()
    assert getattr(channel, "supports_media_notifications", False) is False
    assert getattr(channel, "supports_template_notifications", False) is False


def test_package_exports():
    assert tai42_channel_web.__all__ == ["WebChannel", "WebSettings", "web_settings"]
    for name in tai42_channel_web.__all__:
        assert getattr(tai42_channel_web, name) is not None
