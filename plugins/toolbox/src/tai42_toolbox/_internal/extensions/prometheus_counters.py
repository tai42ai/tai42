"""Metric settings and counters for the ``prometheus_metrics`` tool extension.

Holds the namespace/runtime settings, the process-wide counter and histogram
singletons, and :func:`record_tool_metrics` that the extension calls per
invocation.

Needs the ``prometheus`` extra (``prometheus-client``); the module fails loudly at
import with an install hint when it is absent, never a silent skip.
"""

from functools import lru_cache

from pydantic import Field
from tai42_kit.settings import TaiBaseSettings, settings_cache

try:
    import prometheus_client  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "The 'prometheus_metrics' tool extension needs the prometheus-client library. "
        "Install it with: pip install 'tai42-toolbox[prometheus]'"
    ) from exc


class PrometheusSettings(TaiBaseSettings):
    """Namespace and runtime label applied to the emitted metrics."""

    prometheus_metrics_namespace: str = Field(default="tai")
    prometheus_runtime: str = Field(default="main")


@settings_cache
def prometheus_settings() -> PrometheusSettings:
    return PrometheusSettings()


@lru_cache(maxsize=1)
def tool_call_count():
    from prometheus_client import Counter

    return Counter(
        f"{prometheus_settings().prometheus_metrics_namespace}_tool_call_count",
        "Total number of tool calls",
        ["title", "name", "runtime"],
    )


@lru_cache(maxsize=1)
def tool_error_count():
    from prometheus_client import Counter

    return Counter(
        f"{prometheus_settings().prometheus_metrics_namespace}_tool_error_count",
        "Total number of tool call errors",
        ["title", "name", "runtime"],
    )


@lru_cache(maxsize=1)
def tool_run_time():
    from prometheus_client import Histogram

    return Histogram(
        f"{prometheus_settings().prometheus_metrics_namespace}_tool_run_time_seconds",
        "Duration of tool calls in seconds",
        ["title", "name", "runtime"],
        buckets=[0.1, 0.5, 1, 2, 5, 10],
    )


def record_tool_metrics(title: str, tool_name: str, success: bool, duration: float) -> None:
    labels = {"title": title, "name": tool_name, "runtime": prometheus_settings().prometheus_runtime}
    tool_call_count().labels(**labels).inc()
    if not success:
        tool_error_count().labels(**labels).inc()
    tool_run_time().labels(**labels).observe(duration)
