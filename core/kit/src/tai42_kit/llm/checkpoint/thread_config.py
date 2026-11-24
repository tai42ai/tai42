"""The thread a saver call's configuration addresses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig


def config_thread_id(config: RunnableConfig | None) -> Any:
    """The ``configurable.thread_id`` of ``config`` as the caller gave it; ``None`` when it names none."""
    return ((config or {}).get("configurable") or {}).get("thread_id")
