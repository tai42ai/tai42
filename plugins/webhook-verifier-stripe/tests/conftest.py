"""Bind a capturing app to the ``tai42_app`` handle before any package import.

Importing the package runs its module-level ``webhook_verifiers.register(...)`` call,
so a capturing app must be bound first or the import fails with an unbound handle.
The ``load_registrations`` fixture rebinds a fresh capturing app per test.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from typing import Any, cast

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.webhooks import WebhookVerifier


class _CaptureVerifiers:
    """Records every verifier registered through ``webhook_verifiers.register``."""

    def __init__(self) -> None:
        self.registered: dict[str, WebhookVerifier] = {}

    def register(self, name: str, verifier: WebhookVerifier) -> None:
        # Mirror a real app's duplicate-name raise so a double-registration bug surfaces.
        if name in self.registered:
            raise ValueError(f"verifier {name!r} is already registered")
        self.registered[name] = verifier

    def get(self, name: str) -> WebhookVerifier:
        return self.registered[name]


class _CaptureApp:
    def __init__(self) -> None:
        self.webhook_verifiers = _CaptureVerifiers()


# Bind at import time so the package's module-level register call has an app.
tai42_app.bind(_CaptureApp())


@pytest.fixture
def load_registrations() -> Iterator[Any]:
    """Return a loader that (re)imports the package under a fresh capturing app and
    returns it. Restores the previously bound app afterwards.
    """
    previous = cast("Any", tai42_app)._impl

    def load(module_name: str) -> _CaptureApp:
        app = _CaptureApp()
        tai42_app.bind(app)
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
        return app

    yield load
    tai42_app.bind(previous)
