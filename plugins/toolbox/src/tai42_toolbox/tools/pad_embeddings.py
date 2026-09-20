"""The ``pad_embeddings`` tool: zero-pad embedding vectors to a fixed width.

Carries no heavy backing dependency, so this module imports cleanly in the base
install.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(tags={"embeddings"})
def pad_embeddings(
    embeddings: list[float] | list[list[float]],
    target_dim: int = 3072,
) -> list[list[float]]:
    """Pad each embedding vector with zeros up to ``target_dim``.

    Always returns a list of vectors, even for a single input vector (wrapped as
    ``[[...]]``). An empty input returns an empty list. ``target_dim`` must be at
    least the current width of every vector; a vector already wider than
    ``target_dim`` raises loudly rather than silently truncating.

    Args:
        embeddings: A single vector (``list[float]``) or a batch
            (``list[list[float]]``).
        target_dim: The desired width; must be ``>=`` every vector's width.

    Returns:
        The padded vector(s) as ``list[list[float]]``.
    """
    if not embeddings:
        return []

    def pad_single(vec: list[float]) -> list[float]:
        current_dim = len(vec)
        if current_dim > target_dim:
            raise ValueError(
                f"cannot pad a length-{current_dim} vector to target_dim={target_dim}: "
                "target_dim must be >= the vector's current width"
            )
        return vec + [0.0] * (target_dim - current_dim)

    if isinstance(embeddings[0], int | float):
        return [pad_single(embeddings)]  # type: ignore[arg-type]
    return [pad_single(vec) for vec in embeddings]  # type: ignore[arg-type]
