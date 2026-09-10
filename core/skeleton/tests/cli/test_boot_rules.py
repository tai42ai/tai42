"""Worker-bus boot rules: three shapes refuse to start without the bus.

* more than one server worker (siblings would serve stale config after a reload),
* a task backend registered in the manifest (server + backend-runtime must
  converge), and
* any non-``file`` config mode (an external provider serves shared config).

Every refusal names ``TAI_BUS_REDIS_URL`` so the operator knows the fix, and the
shared-config check runs BEFORE any config-manager construction so a busless
shared-config boot fails on the bus var rather than first on the provider's
connection. The single-worker, file-mode, no-backend, no-bus shape is supported and
runs on ``WorkerBus.local``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import boot_rules
from tai42_skeleton.cli import mcp_app

if TYPE_CHECKING:
    from tai42_skeleton.manifest import Manifest


@pytest.fixture(autouse=True)
def _reset_settings_after() -> Iterator[None]:
    yield
    reset_all_settings()


def _busless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()


def _with_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()


class _Manifest:
    def __init__(self, backend_module: str) -> None:
        self.backend_module = backend_module


# -- workers rule -------------------------------------------------------------


def test_multi_worker_busless_refuses_naming_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    with pytest.raises(RuntimeError, match="TAI_BUS_REDIS_URL") as exc:
        boot_rules.require_bus_for_workers(4)
    assert "4 workers" in str(exc.value)


def test_multi_worker_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    boot_rules.require_bus_for_workers(4)


def test_single_worker_busless_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    boot_rules.require_bus_for_workers(1)


def test_run_mcp_app_serve_dash_w4_busless_raises_naming_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    with pytest.raises(RuntimeError, match="TAI_BUS_REDIS_URL"):
        mcp_app.run_mcp_app(
            manifest_path="unused.yml",
            transport="http",
            host="127.0.0.1",
            port=8000,
            workers=4,
            stateless_http=True,
        )


# -- backend rule -------------------------------------------------------------


def test_registered_backend_busless_refuses_naming_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    with pytest.raises(RuntimeError, match="TAI_BUS_REDIS_URL"):
        boot_rules.require_bus_for_backend(cast("Manifest", _Manifest("myapp.backend")))


def test_registered_backend_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    boot_rules.require_bus_for_backend(cast("Manifest", _Manifest("myapp.backend")))


def test_no_backend_busless_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    boot_rules.require_bus_for_backend(cast("Manifest", _Manifest("")))


# -- shared-config rule (any non-file mode; fails on the bus var, BEFORE the
#    external provider is ever contacted) ---------------------------------------


def test_shared_config_mode_busless_refuses_on_the_bus_var_not_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    _busless(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        boot_rules.require_bus_for_shared_config()
    message = str(exc.value)
    # The refusal names the bus var and the active mode, and runs before the config
    # manager is ever constructed — so it never reflects a provider connection error.
    assert "TAI_BUS_REDIS_URL" in message
    assert "external" in message


def test_shared_config_mode_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    _with_bus(monkeypatch)
    boot_rules.require_bus_for_shared_config()


def test_file_mode_busless_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "file")
    _busless(monkeypatch)
    boot_rules.require_bus_for_shared_config()


def test_run_mcp_app_shared_config_busless_raises_on_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    # A busless shared-config `tai serve` boot refuses naming TAI_BUS_REDIS_URL — the
    # check precedes config-manager construction, so no provider is contacted first.
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    _busless(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        mcp_app.run_mcp_app(
            manifest_path="unused.yml",
            transport="http",
            host="127.0.0.1",
            port=8000,
            workers=1,
        )
    assert "TAI_BUS_REDIS_URL" in str(exc.value)
