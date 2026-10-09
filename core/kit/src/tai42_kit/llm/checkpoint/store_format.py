"""The checkpoint store-format generation: a store written in an older format is refused at its first use.

The kit's checkpoint encoding has a generation. Building a saver on a store records the generation
in an empty store and refuses a store that holds checkpoints written in another (or no recorded)
generation, so old-format data fails loudly instead of being read wrongly. It reads nothing of the
old format beyond "does data exist". The in-process ``memory`` store starts empty and carries no
marker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource

CHECKPOINT_STORE_FORMAT_GENERATION: Final[int] = 3

# The Redis key holding the store's format generation.
REDIS_FORMAT_KEY: Final = "tai42:checkpoint:format"

_SQLITE_FORMAT_TABLE: Final = "tai42_checkpoint_format"


class CheckpointStoreFormatError(RuntimeError):
    """The checkpoint store holds data written in a format this version does not read."""


def _refusal(provider: str, found: int | None) -> CheckpointStoreFormatError:
    return CheckpointStoreFormatError(
        f"the {provider} checkpoint store holds data written in an older format (generation "
        f"{found if found is not None else 'none'}, this version writes {CHECKPOINT_STORE_FORMAT_GENERATION}); "
        "reset the checkpoint store before starting this version"
    )


async def ensure_store_format(resource: CheckpointResource) -> None:
    """Record the format generation in an empty store; raise :class:`CheckpointStoreFormatError` on old data."""
    match resource.provider:
        case "redis":
            await _ensure_redis(resource)
        case "postgres":
            await _ensure_postgres(resource)
        case "sqlite":
            await _ensure_sqlite(resource)
        case _:
            return


def _check(provider: str, found: int | None) -> None:
    if found != CHECKPOINT_STORE_FORMAT_GENERATION:
        raise _refusal(provider, found)


async def _ensure_redis(resource: CheckpointResource) -> None:
    client = resource.redis_client
    if client is None:
        raise ValueError("a redis checkpoint resource carries its client")
    found = _generation(await client.get(REDIS_FORMAT_KEY))
    if found is None:
        info = await resource.handle.checkpoints_index.info()
        if int(info["num_docs"]) != 0:
            raise _refusal("redis", None)
        # Two processes building together converge on the one marker written first.
        await client.set(REDIS_FORMAT_KEY, str(CHECKPOINT_STORE_FORMAT_GENERATION), nx=True)
        found = _generation(await client.get(REDIS_FORMAT_KEY))
    _check("redis", found)


async def _ensure_postgres(resource: CheckpointResource) -> None:
    from tai42_kit.llm.checkpoint import postgres_store

    pool = resource.handle
    found = await postgres_store.read_format_generation(pool)
    if found is None:
        if await postgres_store.holds_checkpoints(pool):
            raise _refusal("postgres", None)
        await postgres_store.write_format_generation(pool, CHECKPOINT_STORE_FORMAT_GENERATION)
        found = await postgres_store.read_format_generation(pool)
    _check("postgres", found)


async def _ensure_sqlite(resource: CheckpointResource) -> None:
    conn = resource.handle
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_SQLITE_FORMAT_TABLE} "
        "(id SMALLINT PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL)"
    )
    await conn.commit()
    found = await _sqlite_generation(conn)
    if found is None:
        async with conn.execute("SELECT 1 FROM checkpoints LIMIT 1") as cursor:
            holds_data = await cursor.fetchone() is not None
        if holds_data:
            raise _refusal("sqlite", None)
        await conn.execute(
            f"INSERT INTO {_SQLITE_FORMAT_TABLE} (id, generation) VALUES (1, ?) ON CONFLICT (id) DO NOTHING",  # noqa: S608 -- constant table name
            (CHECKPOINT_STORE_FORMAT_GENERATION,),
        )
        await conn.commit()
        found = await _sqlite_generation(conn)
    _check("sqlite", found)


async def _sqlite_generation(conn: Any) -> int | None:
    async with conn.execute(f"SELECT generation FROM {_SQLITE_FORMAT_TABLE} WHERE id = 1") as cursor:  # noqa: S608 -- constant table name
        row = await cursor.fetchone()
    return None if row is None else int(row[0])


def _generation(raw: Any) -> int | None:
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return int(text)
    except ValueError:
        raise CheckpointStoreFormatError(
            f"the checkpoint store-format marker holds {text!r}, not a generation"
        ) from None
