"""The connection settings every LangGraph Postgres component (checkpoint saver, store) requires.

The saver and the store need connections that autocommit, skip prepared statements, and yield
dict rows; they ride as a dedicated pool's per-connection kwargs.
"""

from types import MappingProxyType
from typing import Any, Final

from psycopg.rows import dict_row

LANGGRAPH_CONNECTION_KWARGS: Final[MappingProxyType[str, Any]] = MappingProxyType(
    {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
)
