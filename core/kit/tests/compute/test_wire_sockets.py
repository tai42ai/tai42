"""The record of the compute wire's socket ends: a close made inside it, and the ends it receives.

A close that reaches the record from inside one of its own calls on the same thread is made here
by a wrapped standard-library call the record makes, as the garbage collector would when it
finalizes a job that held an end. Each such test runs in a forked child or a fresh interpreter
within a bound, so a close that waited on itself fails the test instead of holding the session.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tai42_kit.compute.wire_sockets import EndNotReceivedError, wire_sockets

from ..backend.preludes import RESHAPING_PRELUDES  # noqa: TID252 shared with the backend tests
from .descriptor_limit import at_descriptor_limit
from .forked import fork_child, in_forked_process

_BOUND = 10.0


def _open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _closing_once(victim: socket.socket) -> Callable[[], None]:
    """A call that closes the recorded ``victim`` through the record, the first time only."""
    closes: list[socket.socket] = [victim]

    def close() -> None:
        if closes:
            wire_sockets.close(closes.pop())

    return close


def _making_a_pair(close: Callable[[], None]) -> list[socket.socket]:
    real = socket.socketpair

    def socketpair(*args: object, **kwargs: object) -> tuple[socket.socket, socket.socket]:
        pair = real(*args, **kwargs)  # type: ignore[arg-type]
        close()
        return pair

    socket.socketpair = socketpair  # type: ignore[assignment]
    try:
        return list(wire_sockets.socketpair())
    finally:
        socket.socketpair = real


def _receiving_an_end(close: Callable[[], None]) -> list[socket.socket]:
    serving, client = wire_sockets.socketpair()
    serving.setblocking(False)
    mine, theirs = wire_sockets.socketpair()
    socket.send_fds(client, [b"\0"], [theirs.fileno()])
    real = socket.socket.recvmsg

    def recvmsg(self: socket.socket, *args: object) -> object:
        close()
        return real(self, *args)  # type: ignore[arg-type]

    socket.socket.recvmsg = recvmsg  # type: ignore[method-assign,assignment]
    try:
        _, received = wire_sockets.received(serving)
    finally:
        socket.socket.recvmsg = real  # type: ignore[method-assign]
    return [serving, client, mine, theirs, *received]


def _closing_an_end(close: Callable[[], None]) -> list[socket.socket]:
    closed, other = wire_sockets.socketpair()
    real = socket.socket.close

    def closing(self: socket.socket) -> None:
        if self is closed:
            close()
        real(self)

    socket.socket.close = closing  # type: ignore[method-assign]
    try:
        wire_sockets.close(closed)
    finally:
        socket.socket.close = real  # type: ignore[method-assign]
    return [other]


@pytest.mark.parametrize("section", [_making_a_pair, _receiving_an_end, _closing_an_end])
def test_a_close_made_inside_a_call_of_the_record_on_the_same_thread_returns_and_the_record_stays_right(
    section: Callable[[Callable[[], None]], list[socket.socket]],
) -> None:
    def body() -> object:
        kept, kept_peer = wire_sockets.socketpair()
        wire_sockets.keep_in_children(kept)
        victim, victim_peer = wire_sockets.socketpair()
        held = section(_closing_once(victim))
        held_fds = [end.fileno() for end in held if end.fileno() != -1]
        # A child forked now closes every end the record holds but the kept one.
        child = fork_child(lambda: {"kept_open": _open(kept.fileno()), "held_open": [_open(fd) for fd in held_fds]})
        seen = {"victim_closed": victim.fileno() == -1, "held": len(held_fds), "in_a_child": child.result(_BOUND)}
        for end in [kept, kept_peer, victim_peer, *held]:
            wire_sockets.close(end)
        return seen

    seen = in_forked_process(body, _BOUND)

    assert isinstance(seen, dict)
    assert seen["held"] > 0
    assert seen == {
        "victim_closed": True,
        "held": seen["held"],
        "in_a_child": {"kept_open": True, "held_open": [False] * seen["held"]},
    }


# A fresh interpreter whose own fork hooks, registered before the record's, close recorded ends
# through the record while the record's fork holds its lock: the before hook and the parent's after
# hook in the parent, the child's after hook in the child. In the child, the first end the
# record's own hook closes closes one more recorded end through the record. It forks once.
_FORK_HOOKS_SCRIPT = """
import json
import os
import socket

closing = {}


def close_through_the_record(name):
    def hook():
        end = closing.pop(name, None)
        if end is not None:
            wire_sockets.close(end)

    return hook


os.register_at_fork(
    before=close_through_the_record("before"),
    after_in_parent=close_through_the_record("after_in_parent"),
    after_in_child=close_through_the_record("after_in_child"),
)

from tai42_kit.compute.wire_sockets import wire_sockets

pairs = {name: wire_sockets.socketpair() for name in ("before", "after_in_parent", "after_in_child", "in_the_hook")}
for name, (end, _) in pairs.items():
    closing[name] = end
kept, kept_peer = wire_sockets.socketpair()
wire_sockets.keep_in_children(kept)
fds = {name: [end.fileno(), peer.fileno()] for name, (end, peer) in pairs.items()}

def is_open(fd):
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


parent = os.getpid()
real_dup2 = os.dup2


def dup2(*args, **kwargs):
    if os.getpid() != parent:
        close_through_the_record("in_the_hook")()
    return real_dup2(*args, **kwargs)


os.dup2 = dup2
reports, report_to = os.pipe()
forks = 0
if forks:
    raise SystemExit("this script forks once")
forks += 1
pid = os.fork()
if pid == 0:
    try:
        os.dup2 = real_dup2
        seen = {
            "kept_open": is_open(kept.fileno()),
            "closed": {name: [not is_open(fd) for fd in pair] for name, pair in fds.items()},
        }
        os.write(report_to, json.dumps(seen).encode())
    finally:
        os._exit(0)
os.dup2 = real_dup2
os.close(report_to)
child = json.loads(os.read(reports, 65536))
os.waitpid(pid, 0)
print(json.dumps({
    "parent_closed": {name: end.fileno() == -1 for name, (end, _) in pairs.items()},
    "child": child,
    "a_pair_after": len(wire_sockets.socketpair()),
}), flush=True)
"""


@pytest.fixture
def fresh_interpreter() -> Iterator[Callable[..., subprocess.CompletedProcess[str]]]:
    """Run a script, its arguments as ``sys.argv[1:]``, in a fresh interpreter in a process group of its own.

    The group is killed past the bound.
    """
    groups: list[int] = []

    def run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(script), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        groups.append(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=_BOUND)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(f"the script did not finish within {_BOUND} s:\n{stdout}\n{stderr}")
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    yield run
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            continue


def test_a_close_made_by_a_fork_hook_while_the_records_fork_holds_its_lock_returns_and_the_record_stays_right(
    fresh_interpreter: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    done = fresh_interpreter(_FORK_HOOKS_SCRIPT)

    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert json.loads(done.stdout) == {
        "parent_closed": {"before": True, "after_in_parent": True, "after_in_child": False, "in_the_hook": False},
        "child": {
            "kept_open": True,
            "closed": {name: [True, True] for name in ("before", "after_in_parent", "after_in_child", "in_the_hook")},
        },
        "a_pair_after": 2,
    }


@pytest.mark.parametrize("flag_on_the_receive", [True, False], ids=["close-on-exec-flag", "no-such-flag"])
def test_an_end_received_through_the_record_is_not_inheritable(
    monkeypatch: pytest.MonkeyPatch, flag_on_the_receive: bool
) -> None:
    if not flag_on_the_receive:
        monkeypatch.delattr(socket, "MSG_CMSG_CLOEXEC", raising=False)
    serving, client = wire_sockets.socketpair()
    serving.setblocking(False)
    mine, theirs = wire_sockets.socketpair()
    received: list[socket.socket] = []
    try:
        socket.send_fds(client, [b"\0"], [theirs.fileno()])
        data, received = wire_sockets.received(serving)

        assert data == b"\0"
        assert len(received) == 1
        assert received[0].get_inheritable() is False
    finally:
        for end in [serving, client, mine, theirs, *received]:
            wire_sockets.close(end)


def test_an_end_passed_while_this_process_has_no_descriptor_free_raises_and_the_next_end_is_received() -> None:
    serving, client = wire_sockets.socketpair()
    serving.setblocking(False)
    mine, theirs = wire_sockets.socketpair()
    mine.settimeout(_BOUND)
    later_mine, later_theirs = wire_sockets.socketpair()
    received: list[socket.socket] = []
    try:
        socket.send_fds(client, [b"\0"], [theirs.fileno()])
        wire_sockets.close(theirs)
        held = set(wire_sockets._held)

        with pytest.raises(EndNotReceivedError), at_descriptor_limit():
            wire_sockets.received(serving)

        assert set(wire_sockets._held) == held
        assert mine.recv(1) == b""
        socket.send_fds(client, [b"\0"], [later_theirs.fileno()])
        data, received = wire_sockets.received(serving)
        assert data == b"\0"
        assert len(received) == 1
    finally:
        for end in [serving, client, mine, later_mine, later_theirs, *received]:
            wire_sockets.close(end)


# A fresh interpreter, under the prelude ``sys.argv[1]`` names (none when empty), holds one end of
# the wire it has no use for in a child, and forks once while it has no descriptor free. The child
# marks itself first, never forks, raises its own limit, checks, reports and pauses until it is
# killed; the parent reads the end's peer once it closed its own copy.
_FORK_AT_THE_LIMIT_SCRIPT = """
import importlib
import json
import os
import resource
import select
import signal
import stat
import sys
import threading

sys.path.insert(0, sys.argv[2])
if sys.argv[1]:
    module_name, _, callable_name = sys.argv[1].partition(":")
    getattr(importlib.import_module(module_name), callable_name)()

from descriptor_limit import at_descriptor_limit
from tai42_kit.compute.wire_sockets import wire_sockets

IN_CHILD = False
FORKS = 0


def is_socket(fd):
    try:
        return stat.S_ISSOCK(os.fstat(fd).st_mode)
    except OSError:
        return False


held, peer = wire_sockets.socketpair()
held_fd = held.fileno()
reports, report_to = os.pipe()
with at_descriptor_limit():
    if IN_CHILD or FORKS >= 1:
        raise SystemExit("this script forks once")
    FORKS += 1
    pid = os.fork()
    if pid == 0:
        IN_CHILD = True
        try:
            try:
                holds_the_end = is_socket(held_fd)
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
                made = []
                caller = threading.Thread(target=lambda: made.append(len(wire_sockets.socketpair())), daemon=True)
                caller.start()
                caller.join(3)
                seen = {"holds_the_end": holds_the_end, "a_wire_call_returned": made == [2]}
            except BaseException as exc:
                seen = f"the child raised {exc!r}"
            os.write(report_to, json.dumps(seen).encode())
            signal.pause()
        finally:
            os._exit(0)
os.close(report_to)
try:
    ready, _, _ = select.select([reports], [], [], 5)
    report = os.read(reports, 65536) if ready else b'"no report within 5 s"'
    child = json.loads(report) if report else "the child ended with no report"
    wire_sockets.close(held)
    ready, _, _ = select.select([peer], [], [], 2)
    peer_read_it_closed = bool(ready) and peer.recv(1) == b""
finally:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
print(json.dumps({"child": child, "peer_read_it_closed_at_once": peer_read_it_closed}), flush=True)
"""


@pytest.mark.parametrize("prelude", ["", *RESHAPING_PRELUDES], ids=["no-prelude", *RESHAPING_PRELUDES])
def test_a_child_forked_while_the_process_has_no_descriptor_free_lets_go_of_its_ends_and_its_wire_calls_return(
    fresh_interpreter: Callable[..., subprocess.CompletedProcess[str]], prelude: str
) -> None:
    done = fresh_interpreter(_FORK_AT_THE_LIMIT_SCRIPT, prelude, str(Path(__file__).parent))

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {
        "child": {"holds_the_end": False, "a_wire_call_returned": True},
        "peer_read_it_closed_at_once": True,
    }, done.stderr


# A fresh interpreter holds a pair of the wire numbered above the null devices it opened and then
# closed, lowers its soft descriptor limit below both ends' numbers, and forks once. The child marks
# itself first, never forks, checks, reports and pauses until it is killed; the parent restores its
# limit and reads the end's peer once it closed its own copy.
_FORK_WITH_AN_END_ABOVE_THE_LIMIT_SCRIPT = """
import json
import os
import resource
import select
import signal
import stat

from tai42_kit.compute.wire_sockets import wire_sockets

IN_CHILD = False
FORKS = 0


def is_socket(fd):
    try:
        return stat.S_ISSOCK(os.fstat(fd).st_mode)
    except OSError:
        return False


fillers = [os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC) for _ in range(32)]
held, peer = wire_sockets.socketpair()
for fd in fillers:
    os.close(fd)
held_fd = held.fileno()
reports, report_to = os.pipe()
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
lowered = len(os.listdir("/proc/self/fd")) + 8
assert lowered <= held_fd, (lowered, held_fd)
resource.setrlimit(resource.RLIMIT_NOFILE, (lowered, hard))
try:
    if IN_CHILD or FORKS >= 1:
        raise SystemExit("this script forks once")
    FORKS += 1
    pid = os.fork()
    if pid == 0:
        IN_CHILD = True
        try:
            try:
                seen = {"holds_an_end": is_socket(held_fd) or is_socket(peer.fileno())}
            except BaseException as exc:
                seen = f"the child raised {exc!r}"
            os.write(report_to, json.dumps(seen).encode())
            signal.pause()
        finally:
            os._exit(0)
finally:
    if not IN_CHILD:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
os.close(report_to)
try:
    ready, _, _ = select.select([reports], [], [], 5)
    report = os.read(reports, 65536) if ready else b'"no report within 5 s"'
    child = json.loads(report) if report else "the child ended with no report"
    wire_sockets.close(held)
    ready, _, _ = select.select([peer], [], [], 2)
    peer_read_it_closed = bool(ready) and peer.recv(1) == b""
finally:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
print(json.dumps({"child": child, "peer_read_it_closed_at_once": peer_read_it_closed}), flush=True)
"""


def test_a_child_forked_while_two_ends_are_numbered_above_the_soft_descriptor_limit_holds_none_and_reports_both(
    fresh_interpreter: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    done = fresh_interpreter(_FORK_WITH_AN_END_ABOVE_THE_LIMIT_SCRIPT)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {
        "child": {"holds_an_end": False},
        "peer_read_it_closed_at_once": True,
    }, done.stderr
    assert done.stderr.count("OSError(9, 'Bad file descriptor')") == 2, done.stderr


# A fresh interpreter counts its open descriptors as its record of the wire takes ends and lets
# them go, and as it makes a pair with at most two descriptors free.
_PLACEHOLDER_SCRIPT = """
import errno
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
from descriptor_limit import at_descriptor_limit

from tai42_kit.compute.wire_sockets import wire_sockets

base = len(os.listdir("/proc/self/fd"))


def opened():
    return len(os.listdir("/proc/self/fd")) - base


seen = {"at_the_limit": {}}
for free in (0, 1, 2):
    with at_descriptor_limit(free=free):
        try:
            wire_sockets.socketpair()
        except OSError as exc:
            raised = errno.errorcode[exc.errno]
        else:
            raised = "nothing"
    seen["at_the_limit"][free] = {"raised": raised, "opened": opened(), "recorded": len(wire_sockets._held)}
first = wire_sockets.socketpair()
seen["first_pair"] = opened()
second = wire_sockets.socketpair()
seen["second_pair"] = opened()
for end in [*first, *second]:
    wire_sockets.close(end)
seen["every_end_closed"] = opened()
print(json.dumps(seen), flush=True)
"""


def test_the_record_opens_one_null_device_when_it_first_holds_an_end_and_a_pair_it_cannot_make_leaves_nothing(
    fresh_interpreter: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    done = fresh_interpreter(_PLACEHOLDER_SCRIPT, str(Path(__file__).parent))

    assert done.returncode == 0, done.stderr
    nothing_left = {"raised": "EMFILE", "opened": 0, "recorded": 0}
    assert json.loads(done.stdout) == {
        "at_the_limit": {"0": nothing_left, "1": nothing_left, "2": nothing_left},
        "first_pair": 3,
        "second_pair": 5,
        "every_end_closed": 1,
    }
