"""The worker built at boot reads the current settings after a reload re-imports the plugin.

The arq ``Worker`` is built once per process and keeps the job functions of the modules it
was built from. A reload re-imports the plugin package, which defines the settings accessor
again under the same name; every job the boot worker runs afterwards still reads the
configuration of the reload that is current, never an instance frozen at the first read.
"""

from __future__ import annotations

import importlib
import sys
from types import FunctionType

import pytest
from tai42_kit.settings import cache_registry, reset_all_settings

import tai42_backend_arq
from tai42_backend_arq import settings as settings_module
from tai42_backend_arq.worker import ArqWorkerRuntime


async def test_a_boot_worker_job_reads_the_reloaded_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARQ_CALLBACK_TIMEOUT", "5")
    settings_module.arq_settings.cache_clear()
    runtime = ArqWorkerRuntime.from_args([])
    await runtime.build()
    boot_job = runtime.worker.functions["tool_execution"].coroutine
    assert isinstance(boot_job, FunctionType)
    read = boot_job.__globals__["arq_settings"]  # what every job of the boot worker reads through
    try:
        assert read().callback_timeout == 5

        # The re-import replaces the module in ``sys.modules`` and on the package; both are
        # restored after the test, so the rest of the suite keeps the module it imported.
        monkeypatch.delitem(sys.modules, "tai42_backend_arq.settings")
        monkeypatch.setattr(tai42_backend_arq, "settings", settings_module)
        importlib.import_module("tai42_backend_arq.settings")  # the reload re-imports the package
        monkeypatch.setenv("ARQ_CALLBACK_TIMEOUT", "9")
        reset_all_settings()

        assert read().callback_timeout == 9
    finally:
        await runtime.aclose()
        # Bind the one accessor back to the module the rest of the suite holds.
        cache_registry._CACHE_CLEARS[f"{settings_module.__name__}.arq_settings"].rebind(settings_module.ArqSettings)
        settings_module.arq_settings.cache_clear()
