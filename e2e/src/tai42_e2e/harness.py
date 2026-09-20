"""Reusable stack orchestration: infra connect and per-stack resource allocation.

These live in the installable package (not in ``tests/conftest.py``) so BOTH the
pytest suite and the standalone ``tai42-e2e-studio-stack`` console runner drive one
implementation. The pytest fixtures wrap these; the runner calls them directly. The
access-control seed rows they pair with live in ``tai42_e2e.seeding``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from tai42_e2e.pg import PostgresAdmin
from tai42_e2e.redisx import RedisAdmin
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.topology import Infra, InfraUnavailableError, StackResources
from tai42_e2e.variants import resolve_variants


def connect_infra(settings: HarnessSettings) -> Infra:
    """Resolve the variant set, connect the Redis + Postgres admin clients,
    verify everything the selected variants need is reachable (loudly, with the
    compose hint on failure), apply the DDL template, and return the
    :class:`Infra` bundle. The caller owns closing ``infra.redis``."""
    variants = resolve_variants(settings)
    host, port = settings.redis_host_port
    redis_admin = RedisAdmin(host, port)
    pg_admin = PostgresAdmin(settings)
    try:
        redis_admin.check_reachable()
    except Exception as exc:
        raise InfraUnavailableError(f"Redis not usable ({exc}). Start it with `docker compose up -d`.") from exc
    try:
        pg_admin.check_reachable()
    except Exception as exc:
        raise InfraUnavailableError(f"Postgres not reachable ({exc}). Start it with `docker compose up -d`.") from exc
    # Extra reachability the backend needs beyond the shared Redis + Postgres
    # (celery's broker); a no-op for the Redis-only backends.
    variants.backend.infra_check(settings)
    # The module-capable checkpoint Redis is optional (present only when its URL is set);
    # when set it must be reachable, failing loudly like the main Redis. Its allocator owns
    # DB 0 ONLY: RediSearch refuses indexes on db != 0 and index names are server-global,
    # so a stack on DB 1 would silently read another stack's index. A one-slot pool makes a
    # second concurrent checkpoint stack fail loudly instead of mis-indexing; FLUSHDB at
    # allocation drops the indexes so successive stacks reuse DB 0 cleanly.
    checkpoint_redis: RedisAdmin | None = None
    if settings.checkpoint_redis_url is not None:
        ck_host, ck_port = settings.checkpoint_redis_host_port
        checkpoint_redis = RedisAdmin(ck_host, ck_port, stack_db_range=range(1))
        try:
            checkpoint_redis.check_reachable()
        except Exception as exc:
            raise InfraUnavailableError(
                f"Checkpoint Redis not reachable ({exc}). Start it with `docker compose --profile agents-redis up -d`."
            ) from exc
        try:
            # The checkpoint Redis exists only for its modules; a module-free image
            # here is a misconfiguration, caught loudly at session start.
            checkpoint_redis.check_search_json_modules()
        except Exception as exc:
            raise InfraUnavailableError(
                f"Checkpoint Redis lacks the RediSearch/RedisJSON modules the langgraph redis provider needs "
                f"({exc}). Use a module-capable image (redis:8); `docker compose --profile agents-redis up -d`."
            ) from exc
    pg_admin.ensure_template()
    return Infra(
        settings=settings, redis=redis_admin, pg=pg_admin, variants=variants, checkpoint_redis=checkpoint_redis
    )


def allocate_resources(
    infra: Infra,
    root: Path,
    *,
    allocate_checkpoint_db: bool = False,
    redis_host: str | None = None,
    redis_port: int | None = None,
    bus_redis_host: str | None = None,
    bus_redis_port: int | None = None,
    pg_host: str | None = None,
    pg_port: int | None = None,
    **extra: object,
) -> StackResources:
    """Reserve a Redis logical DB, a per-stack Postgres database clone, and (for
    a broker-bearing backend) a per-stack broker lease, then pack the coordinates
    a manifest/env builder needs.

    ``allocate_checkpoint_db`` additionally reserves a logical DB on the
    module-capable checkpoint Redis (the langgraph redis checkpoint/store leg);
    it requires the checkpoint Redis to be configured (``connect_infra`` built
    it), raising loudly otherwise so a mis-gated fixture never silently runs the
    memory provider.

    ``redis_host``/``redis_port`` and ``pg_host``/``pg_port`` point the SUT at
    DIFFERENT connection ENDPOINTS for the stores the harness still allocates and
    seeds on the real infra: the logical Redis DB and the Postgres database clone
    are created as always, and the returned resources simply reach them through
    the given endpoints — the infra-outage specs point them at a per-stack TCP
    relay they can sever. The Redis override is an endpoint, not a URL, because
    the logical DB index is chosen here: the returned ``redis_url`` always selects
    the index this call reserved, so ``redis_url`` and ``redis_idx`` can never
    disagree. ``probe_redis_url`` is deliberately NOT rerouted — it stays on the
    real Redis so the harness can still read probe records while the SUT's own
    endpoint is severed.

    ``bus_redis_host``/``bus_redis_port`` reroute the app worker bus through its OWN
    endpoint, INDEPENDENT of the feature-Redis one, so a bus-outage spec severs the
    bus (its own relay) without severing auth/feature stores. Unset, the bus rides
    the real Redis directly. The returned ``bus_redis_url`` selects the stack's own
    logical DB (presence keys flush with the DB on release); ``bus_namespace`` is
    unique per stack because bus pub/sub + presence keys are server-global and the
    logical-DB isolation does NOT isolate them."""
    # No TaiStack exists yet to reap these on failure, so if a later step raises, release
    # each already-acquired resource here — a partial allocation must not leak. Each cleanup
    # runs only for a resource actually acquired.
    if allocate_checkpoint_db and infra.checkpoint_redis is None:
        raise RuntimeError(
            "allocate_checkpoint_db=True but the checkpoint Redis is not configured "
            "(set TAI_E2E_CHECKPOINT_REDIS_URL and start `docker compose --profile agents-redis up -d`)"
        )
    idx = infra.redis.allocate_db()
    checkpoint_idx: int | None = None
    lease = None
    dbname: str | None = None
    try:
        if allocate_checkpoint_db:
            assert infra.checkpoint_redis is not None
            checkpoint_idx = infra.checkpoint_redis.allocate_db()
        stack_id = uuid.uuid4().hex[:6]
        dbname = f"tai42_e2e_{stack_id}"
        infra.pg.create_stack_db(dbname)
        storage_root = root / "storage"
        storage_root.mkdir(parents=True, exist_ok=True)
        lease = infra.variants.backend.allocate_broker(infra, stack_id)
        settings = infra.settings
        checkpoint_url = (
            infra.checkpoint_redis.url_for(checkpoint_idx)
            if checkpoint_idx is not None and infra.checkpoint_redis is not None
            else None
        )
        infra_redis_host, infra_redis_port = settings.redis_host_port
        sut_redis_host = redis_host if redis_host is not None else infra_redis_host
        sut_redis_port = redis_port if redis_port is not None else infra_redis_port
        sut_bus_host = bus_redis_host if bus_redis_host is not None else infra_redis_host
        sut_bus_port = bus_redis_port if bus_redis_port is not None else infra_redis_port
        # The bus namespace reuses the per-stack id (``dbname`` is ``tai42_e2e_<id>``):
        # a single unique-per-stack token isolates the server-global bus channels.
        # Inside the reap guard: an unknown key in ``extra`` raises TypeError here, and
        # that must not strand the DB clone, the vhost lease and the logical indices.
        return StackResources(
            redis_idx=idx,
            redis_url=f"redis://{sut_redis_host}:{sut_redis_port}/{idx}",
            probe_redis_url=infra.redis.url_for(0),
            bus_redis_url=f"redis://{sut_bus_host}:{sut_bus_port}/{idx}",
            bus_namespace=dbname,
            pg_host=pg_host if pg_host is not None else settings.pg_host,
            pg_port=pg_port if pg_port is not None else settings.pg_port,
            pg_user=settings.pg_user,
            pg_password=settings.pg_password,
            pg_db=dbname,
            storage_root=str(storage_root),
            broker_url=lease.broker_url if lease is not None else None,
            broker_lease=lease,
            checkpoint_redis_idx=checkpoint_idx,
            checkpoint_redis_url=checkpoint_url,
            **extra,  # type: ignore[arg-type]
        )
    except BaseException:
        if lease is not None:
            lease.release()
        if dbname is not None:
            infra.pg.drop_stack_db(dbname)
        if checkpoint_idx is not None and infra.checkpoint_redis is not None:
            infra.checkpoint_redis.release_db(checkpoint_idx)
        infra.redis.release_db(idx)
        raise


def release_resources(infra: Infra, resources: StackResources) -> None:
    """Reap an allocation that no :class:`TaiStack` owns yet — the mirror of
    ``TaiStack.teardown``'s resource release, for a failure between
    ``allocate_resources`` and the stack that would otherwise reap it. Every resource
    is released even if one release raises (a flaky vhost delete must not strand the
    Postgres clone and the Redis index); failures are collected and raised together so
    they surface without displacing the error that led here (they chain under it)."""
    errors: list[str] = []
    if resources.broker_lease is not None:
        try:
            resources.broker_lease.release()
        except Exception as exc:
            errors.append(f"release broker lease: {exc!r}")
    try:
        infra.pg.drop_stack_db(resources.pg_db)
    except Exception as exc:
        errors.append(f"drop stack db {resources.pg_db}: {exc!r}")
    if resources.checkpoint_redis_idx is not None:
        if infra.checkpoint_redis is None:
            errors.append("resources hold a checkpoint Redis DB but infra.checkpoint_redis is None (leak)")
        else:
            infra.checkpoint_redis.release_db(resources.checkpoint_redis_idx)
    infra.redis.release_db(resources.redis_idx)
    if errors:
        raise RuntimeError("releasing an unowned allocation found leaks:\n  " + "\n  ".join(errors))
