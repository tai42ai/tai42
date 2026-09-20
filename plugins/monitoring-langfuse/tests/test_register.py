"""Registration: importing the register module applies the app decorator to a
zero-arg builder that constructs the Langfuse backend."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

from tai42_monitoring_langfuse import LangfuseMonitoring


def _import_register_module(stub_monitoring):
    """(Re-)import the register module so its decorator side-effect fires."""
    stub_monitoring.registered_builders.clear()
    sys.modules.pop("tai42_monitoring_langfuse.register", None)
    importlib.import_module("tai42_monitoring_langfuse.register")


def test_import_registers_zero_arg_builder(stub_monitoring, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost")

    _import_register_module(stub_monitoring)

    # Exactly one builder registered per import (the manifest module import
    # is the single registration point).
    assert len(stub_monitoring.registered_builders) == 1
    builder = stub_monitoring.registered_builders[0]
    backend = builder()
    assert isinstance(backend, LangfuseMonitoring)
    assert backend.writer is not None
    assert backend.reader is not None


def test_reimport_fires_registration_again(stub_monitoring, monkeypatch):
    # A live reload re-imports the module (popped from sys.modules first) and
    # must re-fire the decorator with a fresh builder.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost")

    _import_register_module(stub_monitoring)
    _import_register_module(stub_monitoring)
    assert len(stub_monitoring.registered_builders) == 1  # cleared + re-registered


def test_package_import_alone_does_not_register():
    # `import tai42_monitoring_langfuse` (library use) must not build a backend
    # or touch the app handle; only the register module carries the side-effect.
    # Checked in a clean subprocess so the module cache can't mask it.
    code = (
        "import sys; import tai42_monitoring_langfuse; assert 'tai42_monitoring_langfuse.register' not in sys.modules"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
