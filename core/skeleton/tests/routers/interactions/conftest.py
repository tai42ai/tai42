"""Shared fixtures for the interactions router tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tai42_skeleton.interactions import InteractionStore
from tai42_skeleton.interactions import helper as helper_module
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.operations import interactions as ops
from tai42_skeleton.routers import interactions as router


@pytest.fixture(autouse=True)
def _interactions_store_configured(monkeypatch):
    # the interactions surface is OFF with no Redis. These tests exercise the ON
    # feature, so configure its store — the fake connection still stands in; only the
    # presence gate reads this env var.
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
def wired(monkeypatch, fake_redis, fake_client_ctx):
    settings = InteractionsSettings(public_base_url="https://cb.example")
    monkeypatch.setattr(router, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(router, "interactions_settings", lambda: settings)
    # The answer door is an operation in ``operations.interactions``; it reads
    # ``client_ctx``/``interactions_settings`` from that module, so the same seams
    # are wired there too (the router patches still cover the callback/stream
    # handlers that stay in the router).
    monkeypatch.setattr(ops, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(ops, "interactions_settings", lambda: settings)
    monkeypatch.setattr(helper_module, "client_ctx", fake_client_ctx)
    monkeypatch.setattr(helper_module, "interactions_settings", lambda: settings)
    monkeypatch.setattr(helper_module.secrets, "token_urlsafe", lambda n: "TKT")
    store = InteractionStore(settings.key_prefix)
    return SimpleNamespace(settings=settings, store=store, fake=fake_redis, monkeypatch=monkeypatch)


@pytest.fixture
def verifier_registry():
    """The process app's verifier registry with ``tai42_app`` bound to it; the
    callback route resolves verifiers from here. Cleared after the test."""
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import build_app

    app = build_app()
    tai42_app.bind(app)
    reg = app._webhook_verifier_registry
    try:
        yield reg
    finally:
        reg.reset()
