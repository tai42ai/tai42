"""The checkpoint store-format generation: a store written in an older format is refused at its first use.

The kit's checkpoint encoding has a generation. Opening a store records the generation in an empty
store and refuses a store that holds checkpoints written in another (or no recorded) generation, so
old-format data fails loudly instead of being read wrongly. It reads nothing of the old format beyond
"does data exist". Each store's registration keeps the marker its own way; the rule is applied here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
    from tai42_kit.llm.checkpoint.providers import CheckpointProvider

CHECKPOINT_STORE_FORMAT_GENERATION: Final[int] = 4


class CheckpointStoreFormatError(RuntimeError):
    """The checkpoint store holds data written in a format this version does not read."""


def _refusal(provider: str, found: int | None) -> CheckpointStoreFormatError:
    return CheckpointStoreFormatError(
        f"the {provider} checkpoint store holds data written in an older format (generation "
        f"{found if found is not None else 'none'}, this version writes {CHECKPOINT_STORE_FORMAT_GENERATION}); "
        "reset the checkpoint store before starting this version"
    )


async def ensure_store_format(registration: CheckpointProvider, resource: CheckpointResource) -> None:
    """Record the format generation in an empty store; raise :class:`CheckpointStoreFormatError` on old data."""
    found = await registration.recorded_generation(resource)
    if found is None:
        if await registration.holds_checkpoints(resource):
            raise _refusal(registration.name, None)
        # Two processes building together converge on the one marker recorded first.
        await registration.record_generation(resource, CHECKPOINT_STORE_FORMAT_GENERATION)
        found = await registration.recorded_generation(resource)
    if found != CHECKPOINT_STORE_FORMAT_GENERATION:
        raise _refusal(registration.name, found)


def generation_marker(raw: bytes | str | None) -> int | None:
    """The generation a stored text marker holds; a marker that is not a number raises."""
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return int(text)
    except ValueError:
        raise CheckpointStoreFormatError(
            f"the checkpoint store-format marker holds {text!r}, not a generation"
        ) from None
