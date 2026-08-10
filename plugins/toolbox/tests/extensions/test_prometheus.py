"""Tests for the ``prometheus_metrics`` tool extension."""

import asyncio
import inspect

import pytest
from prometheus_client import REGISTRY
from tai42_contract.extensions import ExtensionKind

import tai42_toolbox.extensions.prometheus as prometheus_module
from tai42_toolbox._internal.extensions.prometheus_counters import prometheus_settings
from tai42_toolbox.extensions.prometheus import prometheus_metrics

from .conftest import FakeTools


def _tool(x: int) -> str:
    return str(x)


class _ResultWithError:
    """A result carrying ``is_error=True``; the metrics wrapper derives success from
    whether the call raised, ignoring this field."""

    is_error = True


def test_registers_as_wrapper_named_prometheus_metrics(capture_registration):
    assert capture_registration(prometheus_module) == [("prometheus_metrics", ExtensionKind.WRAPPER, False)]


def test_declares_no_reserved_params():
    assert getattr(prometheus_metrics, "reserved_params", None) is None


def test_composed_signature_equals_original_exactly():
    original = inspect.signature(_tool)
    composed = inspect.signature(prometheus_metrics(_tool, "tool", "desc"))
    assert composed == original


def test_counter_increments_on_call(bind_fake_app):
    bind_fake_app(FakeTools(tool_title=lambda func: "TestTitle"))

    async def tool(x: int) -> str:
        return str(x)

    wrapped = prometheus_metrics(tool, "prom_counter_tool", "desc")

    labels = {"title": "TestTitle", "name": "prom_counter_tool", "runtime": prometheus_settings().prometheus_runtime}
    namespace = prometheus_settings().prometheus_metrics_namespace
    sample_name = f"{namespace}_tool_call_count_total"

    before = REGISTRY.get_sample_value(sample_name, labels) or 0.0
    asyncio.run(wrapped(7))
    after = REGISTRY.get_sample_value(sample_name, labels) or 0.0

    assert after - before == 1.0


def test_non_raising_call_counts_as_success_ignoring_is_error(bind_fake_app):
    bind_fake_app(FakeTools(tool_title=lambda func: "SuccessTitle"))

    async def tool() -> _ResultWithError:
        return _ResultWithError()

    wrapped = prometheus_metrics(tool, "prom_success_tool", "desc")

    labels = {"title": "SuccessTitle", "name": "prom_success_tool", "runtime": prometheus_settings().prometheus_runtime}
    namespace = prometheus_settings().prometheus_metrics_namespace
    error_sample = f"{namespace}_tool_error_count_total"

    before = REGISTRY.get_sample_value(error_sample, labels) or 0.0
    result = asyncio.run(wrapped())
    after = REGISTRY.get_sample_value(error_sample, labels) or 0.0

    assert isinstance(result, _ResultWithError)
    assert after - before == 0.0


def test_wraps_a_synchronous_tool(bind_fake_app):
    bind_fake_app(FakeTools(tool_title=lambda func: "SyncTitle"))

    def tool(x: int) -> str:
        return str(x)

    wrapped = prometheus_metrics(tool, "prom_sync_tool", "desc")

    labels = {"title": "SyncTitle", "name": "prom_sync_tool", "runtime": prometheus_settings().prometheus_runtime}
    namespace = prometheus_settings().prometheus_metrics_namespace
    sample_name = f"{namespace}_tool_call_count_total"

    before = REGISTRY.get_sample_value(sample_name, labels) or 0.0
    assert asyncio.run(wrapped(7)) == "7"
    after = REGISTRY.get_sample_value(sample_name, labels) or 0.0

    assert after - before == 1.0


def test_raising_call_counts_error_and_propagates(bind_fake_app):
    bind_fake_app(FakeTools(tool_title=lambda func: "FailTitle"))

    async def tool() -> None:
        raise ValueError("boom")

    wrapped = prometheus_metrics(tool, "prom_fail_tool", "desc")

    labels = {"title": "FailTitle", "name": "prom_fail_tool", "runtime": prometheus_settings().prometheus_runtime}
    namespace = prometheus_settings().prometheus_metrics_namespace
    error_sample = f"{namespace}_tool_error_count_total"

    before = REGISTRY.get_sample_value(error_sample, labels) or 0.0
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(wrapped())
    after = REGISTRY.get_sample_value(error_sample, labels) or 0.0

    assert after - before == 1.0
