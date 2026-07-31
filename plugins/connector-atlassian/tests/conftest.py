"""Bind a recording fake ``tai42_app`` at conftest import, before any test
imports the connector, so its import-time ``register_connector`` call is captured
for assertions."""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.connectors.providers import ProviderDescriptor

REGISTERED: list[ProviderDescriptor] = []


class _RecordingConnectors:
    def register_connector(self, descriptor: ProviderDescriptor) -> None:
        REGISTERED.append(descriptor)


class _FakeApp:
    connectors = _RecordingConnectors()


tai42_app.bind(_FakeApp())
