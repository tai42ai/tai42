"""The ``webhooks`` import refusals: per-item malformations, the
token-free-evaluable rule at the import door, and old-shape / malformed envelopes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM
from tai42_contract.hooks import HookParams

from tai42_skeleton.backup import webhooks_section
from tai42_skeleton.hooks import trigger_links

from .conftest import _link_record

# -- per-item malformations ---------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        {
            "name": "g",
            "topic": "t",
            "execution_key": "k-fire",
            "execution_key_fingerprint": "fp",
            "created_at": "2026-07-21T00:00:00+00:00",
            "expires_at": "garbage",
        },
        {
            "name": "g",
            "topic": "",
            "execution_key": "k-fire",
            "execution_key_fingerprint": "fp",
            "created_at": "2026-07-21T00:00:00+00:00",
            "expires_at": None,
        },
        # A link record naming no execution key has no bounded authority to fire under.
        {"name": "g", "topic": "t", "execution_key": "", "created_at": "2026-07-21T00:00:00+00:00", "expires_at": None},
        {"name": "g", "topic": "t", "created_at": "2026-07-21T00:00:00+00:00", "expires_at": None},
        {
            "name": "g",
            "topic": "t",
            "execution_key": "k-fire",
            "execution_key_fingerprint": "fp",
            "tool_kwargs": [1],
            "created_at": "2026-07-21T00:00:00+00:00",
            "expires_at": None,
        },
    ],
)
async def test_per_item_malformation_error_and_skipped_rest_proceeds(store, record) -> None:
    doc = {
        "hooks": [
            {"name": "h", "topic": "t", "tool": "notify", "execution_key": "k-fire", "execution_key_fingerprint": "fp"}
        ],
        "trigger_links": [{"name": "g", "token_hash": "f" * 64, "record": record}],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert report["errors"]  # the malformed item is surfaced
    assert report["skipped"] == 1
    assert report["created"] == 1  # the hook still restored
    assert set(await store.manager.list_hooks()) == {"h"}


@pytest.mark.parametrize(
    "keyless",
    [
        {"name": "keyless", "topic": "t", "tool": "notify"},
        {"name": "keyless", "topic": "t", "tool": "notify", "execution_key": ""},
    ],
)
async def test_import_rejects_a_keyless_hook_per_record_rest_restored(store, keyless) -> None:
    # A hook record naming no execution key has no bounded authority to fire under, and
    # the server's own is not a substitute: refused PER RECORD into ``errors``, never
    # written, never aborting the rest.
    doc = {
        "hooks": [
            keyless,
            {
                "name": "bound",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
        ],
        "trigger_links": [],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert len(report["errors"]) == 1
    assert "keyless" in report["errors"][0]
    assert "execution_key" in report["errors"][0]
    assert report["skipped"] == 1
    assert report["created"] == 1
    # The keyless record was never stored; the record after it still was.
    assert set(await store.manager.list_hooks()) == {"bound"}


async def test_non_string_name_or_hash_per_item_error_valid_entries_restored(store) -> None:
    # A JSON-valid but ill-typed triple (non-str name, non-str token_hash) must
    # become a per-item report error + skipped — never an uncaught TypeError that
    # aborts the whole import after the hook and earlier records were written.
    good = {
        "name": "good",
        "topic": "t",
        "execution_key": "k-fire",
        "execution_key_fingerprint": "fp",
        "require_api_key": False,
        "tool_kwargs": None,
        "created_by": None,
        "created_at": "2026-07-21T00:00:00+00:00",
        "expires_at": None,
    }
    doc = {
        "hooks": [
            {"name": "h", "topic": "t", "tool": "notify", "execution_key": "k-fire", "execution_key_fingerprint": "fp"}
        ],
        "trigger_links": [
            {"name": 123, "token_hash": "a" * 64, "record": {**good, "name": "x"}},
            {"name": "y", "token_hash": 456, "record": {**good, "name": "y"}},
            {"name": "good", "token_hash": "b" * 64, "record": good},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)
    assert len(report["errors"]) == 2  # both ill-typed rows surfaced per-item
    assert report["skipped"] == 2
    assert report["created"] == 2  # the hook + the one valid trigger link
    # The hook and the valid trigger link were restored despite the two bad rows.
    assert set(await store.manager.list_hooks()) == {"h"}
    assert store.settings.trigger_record_key("b" * 64) in store.redis._strings
    assert trigger_links._as_str(store.redis._get_str(store.settings.trigger_name_key("good"))) == "b" * 64


async def test_non_string_tombstone_per_item_error_section_proceeds(store) -> None:
    # A JSON-valid but ill-typed tombstone entry (non-str) must become a per-item
    # report error + skipped — never an uncaught TypeError that aborts the whole
    # webhooks section after the hooks and earlier tombstones were written.
    good = {
        "name": "good",
        "topic": "t",
        "execution_key": "k-fire",
        "execution_key_fingerprint": "fp",
        "require_api_key": False,
        "tool_kwargs": None,
        "created_by": None,
        "created_at": "2026-07-21T00:00:00+00:00",
        "expires_at": None,
    }
    doc = {
        "hooks": [
            {"name": "h", "topic": "t", "tool": "notify", "execution_key": "k-fire", "execution_key_fingerprint": "fp"}
        ],
        "trigger_links": [{"name": "good", "token_hash": "b" * 64, "record": good}],
        "topic_verifiers": {},
        "tombstones": [123, "a" * 64],
    }
    report = await webhooks_section._import_webhooks(doc)
    # The bad tombstone is surfaced per-item and skipped; the section does NOT abort.
    assert len(report["errors"]) == 1
    assert report["skipped"] == 1
    assert report["created"] == 2  # the hook + the one valid trigger link
    # The hook, the valid trigger link, and the valid tombstone were all applied.
    assert set(await store.manager.list_hooks()) == {"h"}
    assert store.settings.trigger_record_key("b" * 64) in store.redis._strings
    assert store.redis._get_str(store.settings.trigger_tomb_key("a" * 64)) is not None


async def test_hook_with_non_compiling_jq_per_record_rest_restored(store) -> None:
    # An inline jq that does not compile is a bad RECORD: refused per hook, never an
    # abort that leaves earlier hooks written while the router reports nothing created.
    doc = {
        "hooks": [
            {
                "name": "broken",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
                "start_expr": {"content": ".foo |"},
            },
            {
                "name": "sound",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
        ],
        "trigger_links": [],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert len(report["errors"]) == 1
    assert "broken" in report["errors"][0]
    assert "not valid jq" in report["errors"][0]
    assert report["skipped"] == 1
    assert report["created"] == 1
    assert set(await store.manager.list_hooks()) == {"sound"}


# -- the token-free-evaluable rule at the import door -------------------------

# Evaluable / unevaluable by a tokenless fire: under the reduced claim set any identity
# claim beyond ``.identity.owner_user_id`` is absent.
_EVALUABLE = '.sub != "banned"'
_UNEVALUABLE = '.identity.description == "ops"'


async def test_import_refuses_records_bound_to_a_key_no_fire_can_evaluate(store, policy_store) -> None:
    # BOTH import writers assert token-free-evaluability: a hook and a trigger link
    # naming an unevaluable key are each refused per record into ``errors``.
    policy_store.add_policy("k-blind", condition={"content": _UNEVALUABLE}, policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    policy_store.add_policy("k-fire", scopes=["hooks"], policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    doc = {
        "hooks": [
            {
                "name": "blind",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-blind",
                "execution_key_fingerprint": "fp",
            },
            {
                "name": "bound",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
        ],
        "trigger_links": [
            {"name": "blind", "token_hash": "a" * 64, "record": _link_record("blind", "k-blind")},
            {"name": "bound", "token_hash": "b" * 64, "record": _link_record("bound", "k-fire")},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert len(report["errors"]) == 2
    assert all("k-blind" in error and "unusable at a fire" in error for error in report["errors"])
    assert report["skipped"] == 2
    assert report["created"] == 2  # the hook and the link bound to the evaluable key
    assert set(await store.manager.list_hooks()) == {"bound"}
    assert store.settings.trigger_record_key("a" * 64) not in store.redis._strings
    assert store.settings.trigger_record_key("b" * 64) in store.redis._strings


async def test_import_refuses_records_bound_to_a_key_with_no_policy_row(store, policy_store) -> None:
    # The EXISTENCE half of the gate: a key with no policy row passes the evaluable half
    # vacuously, so both writers must refuse it per record or store a dead record.
    policy_store.add_policy("k-fire", scopes=["hooks"], policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    doc = {
        "hooks": [
            {
                "name": "ghost",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-ghost",
                "execution_key_fingerprint": "fp",
            },
            {
                "name": "bound",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
        ],
        "trigger_links": [
            {"name": "ghost", "token_hash": "a" * 64, "record": _link_record("ghost", "k-ghost")},
            {"name": "bound", "token_hash": "b" * 64, "record": _link_record("bound", "k-fire")},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert len(report["errors"]) == 2
    assert all("k-ghost" in error and "has no policy" in error for error in report["errors"])
    assert report["skipped"] == 2
    assert report["created"] == 2
    assert set(await store.manager.list_hooks()) == {"bound"}
    assert store.settings.trigger_record_key("a" * 64) not in store.redis._strings


async def test_import_refuses_a_record_whose_bound_fingerprint_no_longer_matches(store, policy_store) -> None:
    # A remint writes a fresh per-mint fingerprint: a record bound to the OLD one is
    # refused per record, so a stale record is never revived as the reminted key.
    policy_store.add_policy("svc", scopes=["hooks"], policy_data={KEY_FINGERPRINT_CLAIM: "F2"})
    stale_link = _link_record("stale", "svc")
    stale_link["execution_key_fingerprint"] = "F1"
    fresh_link = _link_record("fresh", "svc")
    fresh_link["execution_key_fingerprint"] = "F2"
    doc = {
        "hooks": [
            {
                "name": "stale",
                "topic": "t",
                "tool": "notify",
                "execution_key": "svc",
                "execution_key_fingerprint": "F1",
            },
            {
                "name": "fresh",
                "topic": "t",
                "tool": "notify",
                "execution_key": "svc",
                "execution_key_fingerprint": "F2",
            },
        ],
        "trigger_links": [
            {"name": "stale", "token_hash": "a" * 64, "record": stale_link},
            {"name": "fresh", "token_hash": "b" * 64, "record": fresh_link},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert len(report["errors"]) == 2
    assert all("no longer matches the bound key identity" in error for error in report["errors"])
    assert report["skipped"] == 2
    assert report["created"] == 2  # the hook and the link carrying the current fingerprint
    assert set(await store.manager.list_hooks()) == {"fresh"}
    assert store.settings.trigger_record_key("a" * 64) not in store.redis._strings
    assert store.settings.trigger_record_key("b" * 64) in store.redis._strings


async def test_a_corrupt_stored_policy_fails_the_section_instead_of_blaming_the_record(store, policy_store) -> None:
    # A policy-store integrity fault is not a bad record: it propagates as the section's
    # own failure, not a per-hook rejection blaming an intact backup.
    policy_store.add_policy("k-corrupt", policy_data=[1])
    doc = {
        "hooks": [
            {
                "name": "h",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-corrupt",
                "execution_key_fingerprint": "fp",
            }
        ],
        "trigger_links": [],
        "topic_verifiers": {},
        "tombstones": [],
    }
    with pytest.raises(ValidationError):
        await webhooks_section._import_webhooks(doc)

    assert await store.manager.list_hooks() == {}


async def test_one_execution_key_is_read_once_for_the_whole_import(store, policy_store) -> None:
    # Each DISTINCT execution key is asserted once, so the policy read and condition
    # render do not repeat per record.
    policy_store.add_policy("k-fire", condition={"content": _EVALUABLE}, policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    doc = {
        "hooks": [
            {
                "name": "h1",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
            {
                "name": "h2",
                "topic": "t",
                "tool": "notify",
                "execution_key": "k-fire",
                "execution_key_fingerprint": "fp",
            },
        ],
        "trigger_links": [
            {"name": "l1", "token_hash": "a" * 64, "record": _link_record("l1", "k-fire")},
            {"name": "l2", "token_hash": "b" * 64, "record": _link_record("l2", "k-fire")},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert report["created"] == 4
    assert report["errors"] == []
    assert policy_store.rendered == [_EVALUABLE]


async def test_a_refused_key_is_read_once_and_refuses_every_record_naming_it(store, policy_store) -> None:
    # Caching the verdict must not merge the refusals: every record naming the unusable
    # key still gets its own error, off one read and one render.
    policy_store.add_policy("k-bad", condition={"content": _UNEVALUABLE}, policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    doc = {
        "hooks": [
            {"name": "h1", "topic": "t", "tool": "notify", "execution_key": "k-bad", "execution_key_fingerprint": "fp"},
            {"name": "h2", "topic": "t", "tool": "notify", "execution_key": "k-bad", "execution_key_fingerprint": "fp"},
        ],
        "trigger_links": [
            {"name": "l1", "token_hash": "a" * 64, "record": _link_record("l1", "k-bad")},
        ],
        "topic_verifiers": {},
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert report["created"] == 0
    assert len(report["errors"]) == 3
    assert all("unusable at a fire" in error for error in report["errors"])
    assert policy_store.rendered == [_UNEVALUABLE]


async def test_keys_of_one_owner_read_the_owner_row_once_for_the_batch(store, policy_store) -> None:
    # The batch holds ONE enforcer, so the OWNER row two distinct execution keys share
    # is fetched once for the whole restore instead of once per key.
    owner_claim = {"owner_user_id": "acct", KEY_FINGERPRINT_CLAIM: "fp"}
    policy_store.add_policy("acct", scopes=["hooks"], condition={"content": _EVALUABLE})
    policy_store.add_policy("k-one", scopes=["hooks"], policy_data=owner_claim)
    policy_store.add_policy("k-two", scopes=["hooks"], policy_data=owner_claim)
    doc = {
        "hooks": [
            {"name": "h1", "topic": "t", "tool": "notify", "execution_key": "k-one", "execution_key_fingerprint": "fp"},
            {"name": "h2", "topic": "t", "tool": "notify", "execution_key": "k-two", "execution_key_fingerprint": "fp"},
        ],
        "topic_verifiers": {},
        "trigger_links": [],
        "tombstones": [],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert report["created"] == 2
    assert report["errors"] == []
    selects = [sql for sql in policy_store.executed if sql.startswith("SELECT") and "access_control_policies" in sql]
    assert len(selects) == 3  # k-one, k-two, and the owner ONCE


async def test_a_tombstoned_record_skips_benignly_before_its_key_is_read(store, policy_store) -> None:
    # The tombstone check runs BEFORE the key assertion: a tombstoned record is a benign
    # skip, not an import error over something nothing can revive.
    policy_store.add_policy("k-blind", condition={"content": _UNEVALUABLE}, policy_data={KEY_FINGERPRINT_CLAIM: "fp"})
    doc = {
        "hooks": [],
        "trigger_links": [{"name": "dead", "token_hash": "a" * 64, "record": _link_record("dead", "k-blind")}],
        "topic_verifiers": {},
        "tombstones": ["a" * 64],
    }
    report = await webhooks_section._import_webhooks(doc)

    assert report["errors"] == []
    assert report["skipped"] == 1
    assert report["created"] == 0
    assert policy_store.rendered == []
    assert store.settings.trigger_record_key("a" * 64) not in store.redis._strings


# -- old shape + malformed envelope ------------------------------------------


async def test_old_list_shape_imports_clean(store) -> None:
    payload = [
        {"name": "h1", "topic": "t", "tool": "notify", "execution_key": "k-fire", "execution_key_fingerprint": "fp"}
    ]
    report = await webhooks_section._import_webhooks(payload)
    assert report["created"] == 1
    assert set(await store.manager.list_hooks()) == {"h1"}


@pytest.mark.parametrize("missing", ["hooks", "topic_verifiers", "trigger_links", "tombstones"])
async def test_envelope_missing_key_raises(store, missing) -> None:
    doc = {"hooks": [], "topic_verifiers": {}, "trigger_links": [], "tombstones": []}
    del doc[missing]
    with pytest.raises(ValueError, match="missing the required"):
        await webhooks_section._import_webhooks(doc)


@pytest.mark.parametrize(("key", "value"), [("hooks", {}), ("tombstones", "x")])
async def test_envelope_non_list_key_whole_section_refusal_zero_written(store, key, value) -> None:
    # A hand-edited envelope with a NON-LIST value for a required key is a loud
    # whole-section refusal naming the key, BEFORE any write — never a silent
    # degrade (an empty dict iterating to nothing) or an ungraceful failure deeper in.
    await store.manager.register(
        HookParams(name="pre", topic="t", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp")
    )
    doc = {"hooks": [], "topic_verifiers": {}, "trigger_links": [], "tombstones": []}
    doc[key] = value
    with pytest.raises(ValueError, match=f"{key!r} must be a list"):
        await webhooks_section._import_webhooks(doc)
    # Zero keys written — the pre-existing hook is untouched.
    assert set(await store.manager.list_hooks()) == {"pre"}


async def test_envelope_non_mapping_topic_verifiers_whole_section_refusal_zero_written(store) -> None:
    # The bindings ride as a MAPPING; a hand-edited list is a loud whole-section
    # refusal before any write, exactly as a non-list required key is.
    await store.manager.register(
        HookParams(name="pre", topic="t", tool="notify", execution_key="k-fire", execution_key_fingerprint="fp")
    )
    doc = {"hooks": [], "topic_verifiers": [], "trigger_links": [], "tombstones": []}
    with pytest.raises(ValueError, match="'topic_verifiers' must be a mapping"):
        await webhooks_section._import_webhooks(doc)
    assert set(await store.manager.list_hooks()) == {"pre"}
