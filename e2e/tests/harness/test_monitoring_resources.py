"""Harness self-tests for the compose monitoring profile's coordinates: the
Langfuse host and the OpenTelemetry traces endpoint follow the compose file's
port variables, defaulting to the ports the compose file publishes."""

from __future__ import annotations

import pytest

from tai42_e2e.manifests.monitoring import compose_monitoring_resources

pytestmark = pytest.mark.needs("no-stack")


def test_coordinates_default_to_the_compose_published_ports() -> None:
    resources = compose_monitoring_resources({})
    assert resources["langfuse_host"] == "http://127.0.0.1:3000"
    assert resources["otel_traces_endpoint"] == "http://127.0.0.1:4318/v1/traces"


def test_coordinates_follow_the_remapped_compose_ports() -> None:
    resources = compose_monitoring_resources({"TAI_E2E_LANGFUSE_PORT": "13000", "TAI_E2E_OTEL_HTTP_PORT": "14318"})
    assert resources["langfuse_host"] == "http://127.0.0.1:13000"
    assert resources["otel_traces_endpoint"] == "http://127.0.0.1:14318/v1/traces"


def test_coordinates_carry_the_compose_headless_init_key_pair() -> None:
    resources = compose_monitoring_resources({})
    assert resources["langfuse_public_key"] == "pk-lf-e2e0000000000000000000000000000"
    assert resources["langfuse_secret_key"] == "sk-lf-e2e0000000000000000000000000000"
