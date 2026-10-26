"""The refusal and the preview over holders render the acting caller's view of them.

A holder under a feature tag the caller's role may not read is shown as one count per kind,
never by name; the refusal's last line says who can clear what the caller cannot.
"""

from __future__ import annotations

import pytest
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.operations.holders import holders_preview, holders_refusal
from tai42_skeleton.tools import holder_kinds

from .._fakes.caller_reach import bound_caller

_HEAD = "stored template 'jq/gate' cannot be deleted"
_TAIL = "remove those references first"

_PRINCIPALS = [
    holder_kinds.PRINCIPAL.holder(name=user, summary=f"principal {user!r} names 'jq/gate'") for user in ("alice", "bob")
]
_ROLE = holder_kinds.ROLE.holder(name="viewer", summary="role 'viewer' names 'jq/gate'")
_HOOK = holder_kinds.HOOK.holder(name="nightly", summary="hook 'nightly' names 'jq/gate'")
_SCHEDULE = holder_kinds.SCHEDULE.holder(name="hourly", summary="schedule 'hourly' names 'jq/gate'")


def _count(kind: holder_kinds.HolderKind, count: int) -> ReferenceHolder:
    return ReferenceHolder(
        kind=kind.kind,
        tag=kind.tag,
        name=kind.tag,
        locator={"count": count},
        summary=f"{kind.kind}: {count} not readable with your role",
    )


async def test_every_holder_withheld_reads_who_can_remove_them(monkeypatch: pytest.MonkeyPatch) -> None:
    holders = [_PRINCIPALS[0], _ROLE, _HOOK, _PRINCIPALS[1]]
    with bound_caller(monkeypatch, {"presets": "write"}):
        refused = await holders_refusal(_HEAD, holders, _TAIL)
    assert str(refused) == (
        f"{_HEAD} — held by:\n"
        "- principal: 2 not readable with your role\n"
        "- role: 1 not readable with your role\n"
        "- hook: 1 not readable with your role\n"
        "ask someone with access to 'access-control' and 'hooks' to remove those references first"
    )
    assert refused.extra == {
        "holders": [
            _count(holder_kinds.PRINCIPAL, 2).model_dump(),
            _count(holder_kinds.ROLE, 1).model_dump(),
            _count(holder_kinds.HOOK, 1).model_dump(),
        ]
    }
    assert not any(name in str(refused) + repr(refused.extra) for name in ("alice", "bob", "viewer", "nightly"))


async def test_some_holders_withheld_read_the_named_ones_and_who_clears_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holders = [_HOOK, _PRINCIPALS[0], _SCHEDULE, _PRINCIPALS[1]]
    with bound_caller(monkeypatch, {"hooks": "read", "schedules": "write"}):
        refused = await holders_refusal(_HEAD, holders, _TAIL)
    assert str(refused) == (
        f"{_HEAD} — held by:\n"
        "- hook 'nightly' names 'jq/gate'\n"
        "- principal: 2 not readable with your role\n"
        "- schedule 'hourly' names 'jq/gate'\n"
        "remove those references first; the others need someone with access to 'access-control'"
    )
    assert refused.extra == {
        "holders": [_HOOK.model_dump(), _count(holder_kinds.PRINCIPAL, 2).model_dump(), _SCHEDULE.model_dump()]
    }


async def test_every_holder_readable_reads_each_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    with bound_caller(monkeypatch, {"hooks": "read", "schedules": "read"}):
        refused = await holders_refusal(_HEAD, [_HOOK, _SCHEDULE], _TAIL)
    assert str(refused) == (
        f"{_HEAD} — held by:\n"
        "- hook 'nightly' names 'jq/gate'\n"
        "- schedule 'hourly' names 'jq/gate'\n"
        "remove those references first"
    )
    assert refused.extra == {"holders": [_HOOK.model_dump(), _SCHEDULE.model_dump()]}


async def test_an_admin_reads_every_holder_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    holders = [_PRINCIPALS[0], _ROLE, _HOOK]
    with bound_caller(monkeypatch, admin=True):
        refused = await holders_refusal(_HEAD, holders, _TAIL)
        preview = await holders_preview("delete", holders)
    assert str(refused).endswith("\nremove those references first")
    assert refused.extra == {"holders": [holder.model_dump() for holder in holders]}
    assert preview == {"action": "delete", "holders": [holder.model_dump() for holder in holders]}


async def test_the_preview_reads_the_same_view_the_refusal_does(monkeypatch: pytest.MonkeyPatch) -> None:
    holders = [_HOOK, _PRINCIPALS[0], _PRINCIPALS[1]]
    with bound_caller(monkeypatch, {"hooks": "read"}):
        preview = await holders_preview("rename", holders)
        refused = await holders_refusal(_HEAD, holders, _TAIL)
    assert preview == {"action": "rename", **refused.extra}
    assert preview["holders"] == [_HOOK.model_dump(), _count(holder_kinds.PRINCIPAL, 2).model_dump()]


async def test_no_caller_bound_reads_every_holder_as_a_count() -> None:
    refused = await holders_refusal(_HEAD, [_HOOK, _SCHEDULE], _TAIL)
    assert str(refused) == (
        f"{_HEAD} — held by:\n"
        "- hook: 1 not readable with your role\n"
        "- schedule: 1 not readable with your role\n"
        "ask someone with access to 'hooks' and 'schedules' to remove those references first"
    )
