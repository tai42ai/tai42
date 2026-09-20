"""Langfuse ``Monitoring`` backend for the TAI ecosystem.

Importing this ``__init__`` alone does NOT register the backend; registration
lives in :mod:`tai42_monitoring_langfuse.register`, which builds from the
``LANGFUSE_*`` environment as an import side-effect.
"""

from tai42_monitoring_langfuse.factory import build_langfuse_backend
from tai42_monitoring_langfuse.monitoring import LangfuseMonitoring
from tai42_monitoring_langfuse.reader import LangfuseReader
from tai42_monitoring_langfuse.settings import LangfuseSettings, langfuse_settings
from tai42_monitoring_langfuse.writer import LangfuseSpan, LangfuseWriter

__all__ = [
    "LangfuseMonitoring",
    "LangfuseReader",
    "LangfuseSettings",
    "LangfuseSpan",
    "LangfuseWriter",
    "build_langfuse_backend",
    "langfuse_settings",
]
