"""Config writes are exclusive across the replicas sharing one config store.

A settings-profile apply on replica A drops a stored env key. Once A has validated the
profile and saved the pre-apply env as ``@previous``, and while it is still building the new
generation, replica B adds an mcp entry whose header is an ``!ENV`` marker on that key. Each
write holds the store's exclusive write span from its first read through its persist and
validates its markers against the env the store converges every replica to, so B's write
waits for A's persist and is then refused naming the key. The store never holds a marker on
a key it no longer carries.

The wait is observed, not inferred from the refusal: while both writes are in flight the
kernel's lock table (``/proc/locks``) shows a replica-B process blocked on the span's lock
file while a replica-A process holds it and the stored env still carries the key, so B's
write had started waiting before A persisted and could only enter after A left the span.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

# An mcp entry on an address nothing listens on: it lands in the manifest and is never served.
_UNSERVED_URL = "http://127.0.0.1:1/mcp"

# The reserved profile a settings-profile apply saves the pre-apply stored env into, after
# validating the profile and before building the generation that serves it.
_PREVIOUS_PROFILE = "@previous"


def _span_lock_table(lock_file: Path) -> tuple[set[int], set[int]]:
    """The pids holding and the pids blocked waiting on a POSIX record lock of *lock_file*.

    Read from ``/proc/locks``, where a lock reads ``N: POSIX ADVISORY WRITE <pid> <maj>:<min>:<inode> ...``
    and a blocked request carries ``->`` after ``N:``.
    """
    st = os.stat(lock_file)
    subject = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    holders: set[int] = set()
    waiters: set[int] = set()
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()[1:]
        blocked = bool(fields) and fields[0] == "->"
        fields = fields[1:] if blocked else fields
        if len(fields) >= 5 and fields[0] == "POSIX" and fields[4] == subject:
            (waiters if blocked else holders).add(int(fields[3]))
    return holders, waiters


def _session_of(pid: int) -> int | None:
    """The session id of *pid*, or ``None`` once it has exited."""
    try:
        return os.getsid(pid)
    except ProcessLookupError:
        return None


async def _send(api: ApiClient, method: str, path: str, body: dict | None = None) -> httpx.Response:
    """Send one write, re-sending on the documented retriable ``503 reloading``; return its final answer.

    A write on one replica fans a reload out to the other, whose reload gate answers its own
    concurrent write with a retriable 503 before that write reaches the store.
    """
    answer: list[httpx.Response] = []

    async def _attempt() -> bool:
        resp = await api.request_raw(method, path, json=body)
        if resp.status_code == 503:
            return False
        answer.append(resp)
        return True

    await wait_for_async(_attempt, deadline=60.0, message=f"{method} {path} never left the reloading gate")
    return answer[0]


@pytest.mark.timeout(300)
@pytest.mark.needs(
    "mutable",
    "topology:replicas",
    "setting:config-provider:file",
    "setting:exclusive-env-store",
    "setting:TAI_URL_GUARD_ALLOW_CIDRS=loopback",
)
async def test_a_marker_written_on_one_replica_during_a_profile_apply_on_another_is_refused(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    key = uniq("E2E_XW_GONE").upper()
    title = uniq("xw_marker")

    await api_a.post("/api/config/env", json={"env": {key: "1"}}, retry_on_reloading=True)
    baseline = (await api_a.get("/api/config/env"))["env"]
    assert key in baseline, baseline
    # Every other stored key keeps its value, so the profile's only change is dropping ``key``.
    profile_env = {name: value for name, value in baseline.items() if name != key}
    profile = uniq("xw_drop")
    await api_a.put(f"/api/config/profiles/{profile}", json={"env": profile_env}, retry_on_reloading=True)

    async def _previous_holds_key() -> bool:
        resp = await api_b.request_raw("GET", f"/api/config/profiles/{_PREVIOUS_PROFILE}")
        return resp.status_code == 200 and key in resp.json()["data"]["env"]

    async def _marker_once_the_profile_builds() -> httpx.Response:
        await wait_for_async(
            _previous_holds_key,
            deadline=60.0,
            interval=0.02,
            message=f"the profile apply never saved {_PREVIOUS_PROFILE} holding {key}",
        )
        entry = {"title": title, "config": {"url": _UNSERVED_URL, "headers": {"X-Race": f"!ENV ${{{key}}}"}}}
        return await _send(api_b, "POST", "/api/mcp-config/entries", {"entries": [entry]})

    config_dir = replicas_stack.root / "config"
    lock_file = config_dir / ".config-write.lock"
    # Each replica's master leads its own session, which its workers inherit.
    session_a = replicas_stack.process("serve-a").pid
    session_b = replicas_stack.process("serve-b").pid

    def _key_stored() -> bool:
        lines = (config_dir / ".env").read_text(encoding="utf-8").splitlines()
        return any(line.startswith(f"{key}=") for line in lines)

    async def _b_waits_on_the_span_a_holds() -> str | None:
        if not lock_file.exists():
            return None
        holders, waiters = _span_lock_table(lock_file)
        held_by_a = bool(holders) and {_session_of(pid) for pid in holders} == {session_a}
        b_waiting = any(_session_of(pid) == session_b for pid in waiters)
        # Read after the lock table: the key still stored then means A had not persisted yet.
        if held_by_a and b_waiting and _key_stored():
            return f"holders {sorted(holders)}, waiters {sorted(waiters)}"
        return None

    async def _profile_while_watching_the_span() -> httpx.Response:
        apply = asyncio.create_task(_send(api_a, "POST", f"/api/config/profiles/{profile}/apply"))

        async def _seen_before_the_apply_answers() -> str | None:
            seen = await _b_waits_on_the_span_a_holds()
            if seen is None and apply.done():
                answer = apply.result()
                raise AssertionError(
                    f"the profile apply answered {answer.status_code} before replica B's write was seen "
                    "blocked on the write span replica A held"
                )
            return seen

        try:
            await wait_for_async(
                _seen_before_the_apply_answers,
                deadline=60.0,
                interval=0.005,
                message="replica B's write was never seen blocked on the write span replica A held",
            )
        except BaseException:
            apply.cancel()
            raise
        return await apply

    try:
        profile_answer, marker_answer = await asyncio.gather(
            _profile_while_watching_the_span(),
            _marker_once_the_profile_builds(),
        )
        assert profile_answer.status_code == 200, profile_answer.text
        assert marker_answer.status_code == 400, marker_answer.text
        assert "dangling" in marker_answer.text, marker_answer.text
        assert key in marker_answer.text, marker_answer.text

        for api in (api_a, api_b):
            assert key not in (await api.get("/api/config/env"))["env"]
            entries = (await api.get("/api/manifest/preserved")).get("mcp") or []
            assert all(item.get("title") != title for item in entries), "the refused marker write reached the store"
    finally:
        # The entry exists only when the marker write was accepted.
        removal = await _send(api_a, "DELETE", f"/api/mcp-config/entries/{title}")
        assert removal.status_code in {200, 404}, removal.text
