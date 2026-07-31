"""LLM provider factories + the LangGraph checkpoint/store registries.

The chat-model and embedding factories cache per ``(provider, kwargs)`` and
import their langchain provider package lazily inside the build; the registry
accessors return the running event loop's singletons that pool checkpoint/store
connection resources. Importing this package pulls ``langchain_core`` /
``langgraph`` — that is the opt-in boundary (the ``tai42_kit`` top level stays
vendor-free).
"""

from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.embedding import get_embedding, get_embedding_async
from tai42_kit.llm.models import get_llm, get_llm_async
from tai42_kit.llm.store.store_registry import store_registry

__all__ = [
    "checkpoint_registry",
    "get_embedding",
    "get_embedding_async",
    "get_llm",
    "get_llm_async",
    "store_registry",
]
