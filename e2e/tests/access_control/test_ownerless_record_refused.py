"""C-access-control — an ownerless api-key identity record is refused loudly at read.

Every api key belongs to a principal, so every identity record must carry the owner claim.
A record written WITHOUT it (a corrupt shape) is an invariant breach: the read path
refuses it loudly and denies the credential, never resolving it as a valid identity.

Redis-identity variant only: the ownerless record is written directly into the redis
provider's ``ac:key:*`` storage, which the fixture-identity leg does not use.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest
import redis as redis_lib

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for


def _write_ownerless_record(stack: TaiStack, raw: str, user_id: str) -> None:
    """Write an identity record with NO owner claim plus a policy row for ``user_id`` — the
    corrupt ownerless shape the read path must refuse."""
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    host, port = stack.infra.settings.redis_host_port
    client = redis_lib.Redis(host=host, port=port, db=stack.resources.redis_idx, decode_responses=True)
    try:
        client.hset(f"ac:key:{hashed}", mapping={"user_id": user_id, "description": "ownerless"})
        client.set(f"ac:management:key:{user_id}", hashed)
    finally:
        client.close()
    res = stack.resources
    with (
        psycopg.connect(
            host=res.pg_host, port=res.pg_port, user=res.pg_user, password=res.pg_password, dbname=res.pg_db
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO access_control_policies (user_id, scopes) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET scopes = EXCLUDED.scopes",
            (user_id, ["*"]),
        )
        conn.commit()


async def test_ownerless_identity_record_is_refused(setup_stack: TaiStack) -> None:
    if setup_stack.infra.variants.identity.name != "redis":
        pytest.skip("ownerless-record spec writes the redis provider's storage; runs only on TAI_E2E_IDENTITY=redis")

    raw = "sk-ownerless-ghost-record"
    _write_ownerless_record(setup_stack, raw, "ghost")

    # The credential resolves to a record with no owner claim, so it authenticates NOTHING —
    # a full ``*`` policy row for it notwithstanding.
    ghost = ApiClient(f"http://{setup_stack.host}:{setup_stack.port_a}", auth_token=raw)
    resp = await ghost.request_raw("GET", "/api/auth/me")
    assert resp.status_code == 401, f"an ownerless key record must 401: {resp.status_code} {resp.text}"

    # The refusal is loud: the read path logs the ownerless-record breach.
    log_path = setup_stack.process("serve").log_path

    def logged() -> bool:
        return "ownerless key record" in log_path.read_text(encoding="utf-8", errors="replace")

    wait_for(logged, deadline=10.0, message="the ownerless-record refusal never reached the serve log")
