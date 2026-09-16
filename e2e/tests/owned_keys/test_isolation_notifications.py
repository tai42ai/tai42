"""Per-identity isolation A/B negatives on the notifications seam. The operator addresses
records by AUDIENCE (a who), never by recipient (a where): A sees only what is addressed
to it, B sees only its own, a broadcast is hidden from a restricted identity, and the
operator sees everything. A completeness pin proves A reads its OWN per-identity feed
under a shared-feed flood, and a key-own-vs-owner pin proves isolation follows the key's
OWN id — a ``notify_user`` with no audience lands on the caller's own id, and addressing
any foreign identity (including the shared owner) is a loud 403."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack

from ._owned_support import create_service_owner, mint_key_for, provision_operator, two_service_identities


async def test_notification_audience_isolation_and_completeness(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    owned_a_id, owned_a_raw, owned_b_id, owned_b_raw = await two_service_identities(owned_keys_stack, uniq)
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    # The unrestricted operator: an admin session is a top-level principal with no owner
    # claim, so it is never confined to a slice — it addresses records by audience and reads
    # the full shared feed. The seeded admin root key is unrestricted too.
    operator = root.with_token(await provision_operator(owned_keys_stack, root, uniq))
    owned_a = root.with_token(owned_a_raw)
    owned_b = root.with_token(owned_b_raw)

    msg_a = uniq("na")
    msg_b = uniq("nb")
    msg_broadcast = uniq("bcast")
    # The operator addresses each record by AUDIENCE (the identity's OWN id), NOT its owner.
    # msg_a is addressed to A by audience but its recipient (a delivery ADDRESS) is set to
    # B's identity string, and msg_recip_a is a BROADCAST whose recipient is A's identity
    # string. Crossing the two axes makes the isolation below pass ONLY if it keys on
    # audience (a who), never on recipient (a where).
    await operator.post("/api/notifications", json={"message": msg_a, "audience": owned_a_id, "recipient": owned_b_id})
    await operator.post("/api/notifications", json={"message": msg_b, "audience": owned_b_id})
    await operator.post("/api/notifications", json={"message": msg_broadcast})
    msg_recip_a = uniq("recipa")
    await operator.post("/api/notifications", json={"message": msg_recip_a, "recipient": owned_a_id})

    a_feed = (await owned_a.get("/api/notifications"))["notifications"]
    a_messages = [record["message"] for record in a_feed]
    assert msg_a in a_messages
    assert msg_b not in a_messages
    # A broadcast (no audience) is hidden from a restricted identity (default-deny)...
    assert msg_broadcast not in a_messages
    # ...and STILL hidden when its recipient names A's own identity: recipient does not
    # route the in-app feed, so recipient==A alone never pulls a record into A's inbox.
    assert msg_recip_a not in a_messages
    # recipient (a where) is independent of audience (a who): A sees msg_a by audience
    # even though its stored recipient names B, and the recipient is stored untouched.
    record_a = next(record for record in a_feed if record["message"] == msg_a)
    assert record_a["audience"] == owned_a_id
    assert record_a["recipient"] == owned_b_id

    b_messages = [record["message"] for record in (await owned_b.get("/api/notifications"))["notifications"]]
    assert msg_b in b_messages
    # B never sees msg_a even though its recipient names B's identity — the in-app
    # isolation keys on audience (=A), never on recipient.
    assert msg_a not in b_messages
    assert msg_broadcast not in b_messages
    assert msg_recip_a not in b_messages

    # The unrestricted operator sees every record, addressed or broadcast.
    operator_messages = [record["message"] for record in (await operator.get("/api/notifications"))["notifications"]]
    assert {msg_a, msg_b, msg_broadcast, msg_recip_a} <= set(operator_messages)

    # Completeness pin: A's addressed record survives a broadcast flood that overflows
    # the shared feed (feed max is 5 on this stack), proving A reads its OWN per-identity
    # feed — the shared feed has evicted the record (the sentinel).
    msg_a2 = uniq("na2")
    await operator.post("/api/notifications", json={"message": msg_a2, "audience": owned_a_id})
    for _ in range(6):
        await operator.post("/api/notifications", json={"message": uniq("flood")})

    a_after = [record["message"] for record in (await owned_a.get("/api/notifications"))["notifications"]]
    assert msg_a2 in a_after
    operator_after = [record["message"] for record in (await operator.get("/api/notifications"))["notifications"]]
    assert msg_a2 not in operator_after


async def test_key_own_not_owner_notifications_two_siblings_under_one_owner(
    owned_keys_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """The key-own-vs-owner pin for the NOTIFICATIONS seam. TWO keys minted under the SAME
    service owner have the same owner claim but DIFFERENT own ids, so under the key-keyed
    model each is its OWN island: isolation follows the OWN id, never the shared owner. A
    restricted caller's ``notify_user`` with no audience lands on ITS OWN id (proven by the
    stored ``audience`` and by the sibling — same owner — never seeing it), and addressing
    ANY foreign identity — INCLUDING the shared owner — is a loud 403."""
    root = owned_keys_stack.api(port=owned_keys_stack.port_a)
    owner_id = await create_service_owner(root, uniq)
    # Two siblings under the one owner: same owner claim, distinct own ids.
    owned_1_id, owned_1_raw = await mint_key_for(root, uniq, owner_id)
    owned_2_id, owned_2_raw = await mint_key_for(root, uniq, owner_id)
    assert owned_1_id != owner_id
    assert owned_2_id != owner_id
    owned_1 = root.with_token(owned_1_raw)
    owned_2 = root.with_token(owned_2_raw)

    # A restricted caller's notify with NO audience is clamped to its OWN id (not the
    # owner, not escalated to operators): the record lands on its own feed and stores
    # its own id as the audience.
    msg_self = uniq("self")
    await owned_1.post("/api/notifications", json={"message": msg_self})
    one_feed = (await owned_1.get("/api/notifications"))["notifications"]
    assert msg_self in [record["message"] for record in one_feed]
    record_self = next(record for record in one_feed if record["message"] == msg_self)
    assert record_self["audience"] == owned_1_id, "audience=None must clamp to the key's OWN id, not the owner"

    # The SIBLING under the SAME owner never sees it — isolation follows the own id, so
    # a shared owner does NOT share a feed (the owner-keyed model would leak it here).
    assert msg_self not in [record["message"] for record in (await owned_2.get("/api/notifications"))["notifications"]]

    # Addressing the OWNER as an explicit audience is a foreign-identity 403 (the key is
    # NOT its owner) — the loud cross-identity write denial, not a silent redirect.
    denied_owner = await owned_1.request_raw(
        "POST", "/api/notifications", json={"message": uniq("atowner"), "audience": owner_id}
    )
    assert denied_owner.status_code == 403, denied_owner.text
    # Addressing a SIBLING (also foreign) is a 403 too.
    denied_sibling = await owned_1.request_raw(
        "POST", "/api/notifications", json={"message": uniq("atsibling"), "audience": owned_2_id}
    )
    assert denied_sibling.status_code == 403, denied_sibling.text
    # Addressing its OWN id explicitly passes.
    msg_own_explicit = uniq("ownexplicit")
    allowed = await owned_1.request_raw(
        "POST", "/api/notifications", json={"message": msg_own_explicit, "audience": owned_1_id}
    )
    assert allowed.status_code == 200, allowed.text
    assert msg_own_explicit in [
        record["message"] for record in (await owned_1.get("/api/notifications"))["notifications"]
    ]
