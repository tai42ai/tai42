"""The ``generate_embeddings`` tool: generate embedding vectors for text.

Needs the ``embeddings`` extra; fails loudly at import when it is absent.
"""

from __future__ import annotations

from typing import Any

try:
    from tai42_kit.llm.embedding import get_embedding_async
except ImportError as exc:
    raise ImportError(
        "tai42-toolbox 'embeddings' tools require the 'embeddings' optional "
        "dependency (tai42-kit[llm]). "
        "Install it with: pip install 'tai42-toolbox[embeddings]'"
    ) from exc

from tai42_contract.app import tai42_app
from tai42_kit.llm.settings import embedding_settings, llm_provider_settings


@tai42_app.tools.tool(tags={"embeddings"})
async def generate_embeddings(
    texts: str | list[str],
    embedding_provider: str | None = None,
    embedding_kwargs: dict[str, Any] | None = None,
) -> list[list[float]]:
    """Generate embeddings for a single string or a list of strings.

    ``embedding_kwargs`` keys such as ``base_url`` and ``api_key`` are legitimate
    multi-model routing options, but they are caller-supplied on an LLM-fillable
    tool: expose ``generate_embeddings`` only to trusted callers, since a caller
    can redirect embedding requests to an arbitrary endpoint through them.

    Args:
        texts: A string or list of strings to embed.
        embedding_provider: Embedding provider to use; defaults to the
            configured provider.
        embedding_kwargs: Extra provider options; override the configured
            embedding settings.

    Returns:
        One embedding vector (list of floats) per input string.
    """
    if isinstance(texts, str):
        texts = [texts]

    embedding_provider = embedding_provider or llm_provider_settings().embedding
    model = await get_embedding_async(
        provider=embedding_provider,
        **embedding_settings().with_fallbacks(embedding_kwargs or {}),
    )
    return await model.aembed_documents(texts)
