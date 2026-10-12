"""Import every ``tai42_skeleton`` module as the first skeleton import of its own process.

Run as ``python -I isolated_import_driver.py [--package NAME] [--path DIR]
[--import-timeout SECONDS] [--bind-timeout SECONDS]`` (default package
``tai42_skeleton``; ``--path`` puts ``DIR`` first on ``sys.path``); prints one JSON object whose ``failures`` maps each
module that failed to import on its own to its error, ``{}`` when every module
imports.

Each module's import is bounded by ``--import-timeout`` (default
:data:`DEFAULT_IMPORT_TIMEOUT_SECONDS`), counted from the moment its child reports
that its own start is done; that start (binding the stand-in app) has its own
bound, ``--bind-timeout`` (default :data:`DEFAULT_BIND_TIMEOUT_SECONDS`), and a
child that outlives it fails naming the stand-in binding. A child still
importing when its bound runs out is killed from outside (SIGKILL, which no code in the import can catch)
and its module fails, named, with the bound in the error. A module whose own
import does not finish, inside the package or outside it, is named once: every
module that imports it is refused that import in its child and fails as
importing it, even when it catches the refusal, without waiting a bound of its
own. So each distinct hanging module costs two bounds however many modules
import it: one in the dependency probe below, and one in its own child, where it
is judged alone (those children run in parallel, one per CPU).

Each module is imported in a child forked from a process that holds no
``tai42_skeleton`` module, so the module under test is always the first skeleton
import and its own import order is the one exercised. To keep that affordable
the forking process first imports the third-party and other first-party modules
the skeleton pulls in: an import cycle lives among the skeleton's own modules,
so a dependency that is already loaded changes no skeleton import order. That
dependency set is read from a throw-away child that imports every skeleton
module; an error there is not judged, because every module is then imported and
judged on its own. The probe child announces each module body as it starts and
ends, whatever package the module belongs to, and the parent kills the probe
when no body finishes or starts within the bound; the innermost module still
running is the one that hung, and the probe is started again with that module
refused, so the probe costs at most one bound per distinct module that hangs, on
top of its ordinary import time. A module whose import reached a refused module,
or imports one that did, may have finished only by catching the refusal, so it is
left out of the warm set; the forking process imports the warm set refused and
bounded the same way (every module body that starts or finishes restarts the
bound), and ends naming the module if one of those imports stalls.

A module that registers on the app at import (a router, a builtin tool) refuses
loudly through the unbound ``tai42_app`` handle in a process that bound no app.
Such a module is imported again in a fresh child with a stand-in app bound, and
that import must complete, so an import cycle behind the registration still
fails here.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import selectors
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any

DEFAULT_PACKAGE = "tai42_skeleton"
UNBOUND_HANDLE_MODULE = "tai42_contract/app/handle.py"

# The per-module import bound. The slowest skeleton module, imported first in its own child
# with every sibling child running, took 23.2 s on a 20-core host at load average 369; the
# bound is ten times that, rounded up to whole minutes, so only an import that never
# finishes reaches it.
DEFAULT_IMPORT_TIMEOUT_SECONDS = 240.0

# The stand-in app binding's bound, separate from the per-module bound: a child that binds the
# stand-in app first loads its whole closure (about 400 modules) before it imports the module
# under test, so that start is not any one module's import. It took 14.5 s at most on a 20-core
# host at load average 298; the bound is ten times that, rounded up to whole minutes.
DEFAULT_BIND_TIMEOUT_SECONDS = 180.0

# The line a child writes once its own start (the stand-in binding, the refusals) is done.
_READY = b"R\n"


class _RefusedModuleError(Exception):
    """Raised in a child for an import of a module whose own import did not finish within the bound.

    Not an ``ImportError``, so an optional-import fallback does not take it for a missing module;
    the child's verdict reads :attr:`_RefuseModules.refusals`, so it holds even when the
    importer catches this error.
    """

    def __init__(self, refused: str) -> None:
        super().__init__(f"{refused} is refused here: its own import did not finish within the bound")
        self.refused = refused


class _ImportRecord:
    """What a probe child saw: which module bodies run on each thread, what imports what, and the tainted ones.

    A module is tainted when a refusal happens while its body runs (directly or in a nested import),
    or when it imports a tainted module, even one already loaded. A tainted module may have finished
    only because it caught the refusal, so it never enters the warm set.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self.tainted: set[str] = set()
        self.edges: dict[str, set[str]] = {}

    def running(self) -> list[str]:
        stack: list[str] | None = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack

    def refused(self, name: str) -> None:
        self.tainted.add(name)
        self.tainted.update(self.running())

    def imported(self, importer: str, target: str) -> None:
        self.edges.setdefault(importer, set()).add(target)

    def closure(self) -> set[str]:
        tainted = set(self.tainted)
        changed = True
        while changed:
            changed = False
            for importer, targets in self.edges.items():
                if importer not in tainted and not tainted.isdisjoint(targets):
                    tainted.add(importer)
                    changed = True
        return tainted


def _record_import_edges(record: _ImportRecord) -> None:
    """Wrap ``builtins.__import__`` so ``record`` learns every import statement's importer and target."""
    import builtins

    original = builtins.__import__

    def recording_import(
        name: str,
        globals: dict[str, Any] | None = None,  # noqa: A002 - the signature of builtins.__import__
        locals: dict[str, Any] | None = None,  # noqa: A002 - the signature of builtins.__import__
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        importer = (globals or {}).get("__name__")
        if isinstance(importer, str):
            package = (globals or {}).get("__package__") or ""
            target = importlib.util.resolve_name("." * level + name, package) if level else name
            record.imported(importer, target)
            for item in fromlist or ():
                if item != "*":
                    record.imported(importer, f"{target}.{item}")
        return original(name, globals, locals, fromlist, level)

    builtins.__import__ = recording_import


class _RefuseModules:
    """A ``sys.meta_path`` finder that refuses the named modules and records every refusal it makes."""

    def __init__(self, refused: Iterable[str], record: _ImportRecord | None = None) -> None:
        self._refused = frozenset(refused)
        self._record = record
        self.refusals: list[str] = []

    def find_spec(self, fullname: str, path: Sequence[str] | None = None, target: object = None) -> ModuleSpec | None:
        if fullname in self._refused:
            self.refusals.append(fullname)
            if self._record is not None:
                self._record.refused(fullname)
            raise _RefusedModuleError(fullname)
        return None


def _refuse(refused: Iterable[str], record: _ImportRecord | None = None) -> _RefuseModules:
    """Install a :class:`_RefuseModules` for ``refused`` first on ``sys.meta_path`` and return it."""
    finder = _RefuseModules(refused, record)
    sys.meta_path.insert(0, finder)
    return finder


def module_names(src: Path, package: str) -> list[str]:
    """Every importable module under ``package`` (found in ``src``), packages included."""
    names = []
    for path in sorted((src / package).rglob("*.py")):
        parts = list(path.relative_to(src).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def _in_package(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.close(fd)


def _read_all(fd: int) -> bytes:
    data = b""
    while chunk := os.read(fd, 1 << 16):
        data += chunk
    os.close(fd)
    return data


def _bind_stand_in_app() -> None:
    from unittest.mock import MagicMock

    from tai42_contract.app import tai42_app

    tai42_app.bind(MagicMock())


class _AnnouncingLoader:
    """Wraps a loader so ``boundary("S", module)`` runs before the module's body and ``boundary("E", module)`` after."""

    def __init__(self, loader: Any, name: str, boundary: Callable[[str, str], None]) -> None:
        self.wrapped = loader
        self._name = name
        self._boundary = boundary

    def create_module(self, spec: ModuleSpec) -> Any:
        return self.wrapped.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self._boundary("S", self._name)
        try:
            self.wrapped.exec_module(module)
        finally:
            self._boundary("E", self._name)

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self.wrapped, attribute)


class _AnnounceImports:
    """A ``sys.meta_path`` finder that wraps every newly imported module's loader in an :class:`_AnnouncingLoader`.

    Every module is announced, inside the package or outside it, so the innermost module still
    running when an import stalls is the one whose body stalled. A loader without ``exec_module``
    is left unwrapped: the import system loads it through its own path.
    """

    def __init__(self, boundary: Callable[[str, str], None]) -> None:
        self._boundary = boundary
        self._wrapped: list[tuple[str, ModuleSpec, _AnnouncingLoader]] = []

    def find_spec(self, fullname: str, path: Sequence[str] | None = None, target: Any = None) -> ModuleSpec | None:
        later = sys.meta_path[sys.meta_path.index(self) + 1 :]
        for finder in later:
            find_spec = getattr(finder, "find_spec", None)
            spec = find_spec(fullname, path, target) if find_spec is not None else None
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    announcing = _AnnouncingLoader(spec.loader, fullname, self._boundary)
                    spec.loader = announcing
                    self._wrapped.append((fullname, spec, announcing))
                return spec
        return None

    def unwrap(self) -> None:
        """Give every module this finder wrapped its own loader back, on its spec and as ``__loader__``."""
        for name, spec, announcing in self._wrapped:
            spec.loader = announcing.wrapped
            module = sys.modules.get(name)
            if module is not None and getattr(module, "__loader__", None) is announcing:
                module.__loader__ = announcing.wrapped
        self._wrapped.clear()


def _probe_once(
    names: list[str], package: str, refused: frozenset[str], import_timeout: float, bind_timeout: float
) -> list[str] | str:
    """Run one probe child; return the dependency modules it loaded, or the module whose import outlived the bound.

    The child writes ``R`` once it has bound the stand-in app, then ``S <thread> <module>`` when a
    module's body starts running and ``E <thread> <module>`` when it ends (nested imports included,
    every thread and every package) and ``D <json>`` with its result. The parent allows
    ``bind_timeout`` seconds for the binding, then ``import_timeout`` seconds after ``R`` and after
    every later line; when that runs out it kills the child from outside and returns
    the module started last of those still running, the innermost one, whose own body did not finish.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        _bind_stand_in_app()
        os.write(write_fd, _READY)
        record = _ImportRecord()
        _record_import_edges(record)

        def announce(kind: str, module: str) -> None:
            running = record.running()
            if kind == "S":
                running.append(module)
            else:
                running.pop()
            os.write(write_fd, f"{kind} {threading.get_ident()} {module}\n".encode())

        sys.meta_path.insert(0, _AnnounceImports(announce))
        _refuse(refused, record)
        for name in names:
            try:
                importlib.import_module(name)
            except Exception:  # judged per module by the forked children, never here
                continue
        tainted = record.closure()
        loaded = sorted(
            key
            for key, module in list(sys.modules.items())
            if not _in_package(key, package)
            and key not in tainted
            and (spec := getattr(module, "__spec__", None)) is not None
            and spec.name == key
        )
        _write_all(write_fd, f"D {json.dumps(loaded)}\n".encode())
        os._exit(0)
    os.close(write_fd)
    running: list[tuple[str, str]] = []
    ready = False
    deadline = time.monotonic() + bind_timeout
    received = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(read_fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(read_fd)
                if not ready:
                    raise RuntimeError(
                        f"the stand-in binding in the dependency probe did not finish within {bind_timeout:.0f} s"
                    )
                if not running:
                    raise RuntimeError(
                        f"the dependency probe went {import_timeout:.0f} s with no module running (between imports)"
                    )
                return running[-1][1]
            chunk = os.read(read_fd, 1 << 16)
            if not chunk:
                break
            received += chunk
            while b"\n" in received:
                line, _, rest = bytes(received).partition(b"\n")
                received = bytearray(rest)
                kind, _, value = line.decode().partition(" ")
                deadline = time.monotonic() + import_timeout
                if kind == "R":
                    ready = True
                elif kind in ("S", "E"):
                    thread, _, module = value.partition(" ")
                    entry = (thread, module)
                    if kind == "S":
                        running.append(entry)
                    else:
                        started = [index for index, item in enumerate(running) if item[0] == thread]
                        if not started or running[started[-1]] != entry:
                            raise RuntimeError(f"the dependency probe reported the end of {module} out of order")
                        del running[started[-1]]
                elif kind == "D":
                    os.close(read_fd)
                    _, status = os.waitpid(pid, 0)
                    if status != 0:
                        raise RuntimeError(f"the dependency probe child exited with status {status}")
                    return json.loads(value)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    raise RuntimeError(
        f"the dependency probe child ended without its result (status {status}) while running "
        f"{[module for _thread, module in running] or 'nothing'}"
    )


def _dependency_modules(
    names: list[str], package: str, import_timeout: float, bind_timeout: float
) -> tuple[list[str], set[str]]:
    """The modules outside ``package`` loaded once every module of it is imported, and the modules that hung there.

    Read in throw-away probe children. A module whose import outlives the bound is remembered and
    refused in every later probe child, so neither it nor any module importing it costs the probe
    another bound; the probe runs at most once more than the number of distinct modules that hang.
    """
    hung: set[str] = set()
    while True:
        outcome = _probe_once(names, package, frozenset(hung), import_timeout, bind_timeout)
        if isinstance(outcome, list):
            return outcome, hung
        hung.add(outcome)


def _refused_by_unbound_handle(exc: BaseException) -> bool:
    """Whether ``exc`` is the unbound ``tai42_app`` handle's own refusal."""
    if not isinstance(exc, AttributeError) or "accessed before bind()" not in str(exc):
        return False
    frames = traceback.extract_tb(exc.__traceback__)
    return bool(frames) and frames[-1].filename.replace(os.sep, "/").endswith(UNBOUND_HANDLE_MODULE)


def _spawn(name: str, *, bind: bool, refused: frozenset[str], import_timeout: float) -> tuple[int, int]:
    """Fork a child that imports ``name`` first and writes its verdict; return ``(pid, read_fd)``.

    The modules in ``refused`` did not finish their own import within the bound; an import of
    one of them in this child is refused, and ``name`` fails as importing it whether or not
    its import catches the refusal. The child writes ``R`` once its own start (the stand-in
    binding when ``bind``) is done, then its verdict.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        verdict: dict[str, str] = {"status": "ok"}
        refuser = _refuse(refused)
        try:
            if bind:
                _bind_stand_in_app()
            os.write(write_fd, _READY)
            importlib.import_module(name)
        except BaseException as exc:  # the verdict carries every failure to the parent
            if not refuser.refusals and not bind and _refused_by_unbound_handle(exc):
                verdict = {"status": "registers"}
            else:
                verdict = {"status": "error", "error": "".join(traceback.format_exception(exc))}
        if refuser.refusals:
            verdict = {
                "status": "error",
                "error": (
                    f"{name} imports {refuser.refusals[0]}, whose import did not finish within {import_timeout:.0f} s"
                ),
            }
        _write_all(write_fd, json.dumps(verdict).encode())
        os._exit(0)
    os.close(write_fd)
    return pid, read_fd


def _import_each(
    names: list[str],
    *,
    bind: bool,
    import_timeout: float,
    bind_timeout: float,
    refused: frozenset[str] = frozenset(),
) -> dict[str, dict[str, str]]:
    """Import each module first in its own forked child, at most one child per CPU at a time.

    Every running child's pipe is drained as its bytes arrive and the child is reaped only
    after its pipe reaches end-of-file, so a verdict larger than the pipe buffer never
    leaves a child blocked in its write while the parent waits for it. A child gets
    ``bind_timeout`` seconds for its own start and, once it writes ``R``, ``import_timeout``
    seconds for its import; a child past either is killed, and its module fails with an
    error naming the stand-in binding or the import, and the bound.
    """
    limit = os.cpu_count() or 2
    pending = iter(names)
    selector = selectors.DefaultSelector()
    received: dict[int, bytearray] = {}
    deadlines: dict[int, float] = {}
    started: set[int] = set()
    timed_out: set[int] = set()
    verdicts: dict[str, dict[str, str]] = {}

    def start_next() -> bool:
        name = next(pending, None)
        if name is None:
            return False
        pid, read_fd = _spawn(name, bind=bind, refused=refused, import_timeout=import_timeout)
        received[read_fd] = bytearray()
        deadlines[read_fd] = time.monotonic() + bind_timeout
        selector.register(read_fd, selectors.EVENT_READ, (name, pid))
        return True

    def kill_overdue() -> None:
        now = time.monotonic()
        for read_fd, deadline in deadlines.items():
            if read_fd not in timed_out and deadline <= now:
                _name, pid = selector.get_key(read_fd).data
                os.kill(pid, signal.SIGKILL)
                timed_out.add(read_fd)

    while len(received) < limit and start_next():
        pass
    while received:
        waiting = [deadline for read_fd, deadline in deadlines.items() if read_fd not in timed_out]
        timeout = max(0.0, min(waiting) - time.monotonic()) if waiting else None
        for key, _events in selector.select(timeout):
            read_fd = key.fd
            chunk = os.read(read_fd, 1 << 16)
            if chunk:
                received[read_fd] += chunk
                if read_fd not in started and received[read_fd].startswith(_READY):
                    started.add(read_fd)
                    del received[read_fd][: len(_READY)]
                    deadlines[read_fd] = time.monotonic() + import_timeout
                continue
            name, pid = key.data
            selector.unregister(read_fd)
            os.close(read_fd)
            data = bytes(received.pop(read_fd))
            del deadlines[read_fd]
            was_started = read_fd in started
            started.discard(read_fd)
            _, status = os.waitpid(pid, 0)
            if read_fd in timed_out:
                timed_out.discard(read_fd)
                verdicts[name] = {
                    "status": "error",
                    "error": (
                        f"the import of {name} did not finish within {import_timeout:.0f} s"
                        if was_started
                        else f"the stand-in binding for {name} did not finish within {bind_timeout:.0f} s"
                    ),
                }
            elif status != 0 or not data:
                verdicts[name] = {"status": "error", "error": f"the import child exited with status {status}"}
            else:
                verdicts[name] = json.loads(data)
            start_next()
        kill_overdue()
    selector.close()
    return verdicts


def _warm(dependencies: list[str], refused: set[str], import_timeout: float) -> None:
    """Import the warm set in this process, each import refused and bounded as the probe's are.

    The modules that hung in the probe are refused; a warm import that reaches one raises, naming
    both. The bound works as the probe's does: every module body that starts or finishes, at any
    depth, restarts it, so a warm import that loads a large closure is never cut off while it makes
    progress. A watchdog thread ends this process from outside the import, naming the warm module
    and the innermost module still running, when no body starts or finishes for ``import_timeout``
    seconds. Every loader is given back and the watchdog is stopped before any child is forked.
    """
    refuser = _refuse(refused)
    lock = threading.Lock()
    progressed = [time.monotonic()]
    running: list[str] = []
    current = [""]

    def boundary(kind: str, module: str) -> None:
        with lock:
            if kind == "S":
                running.append(module)
            elif module in running:
                del running[len(running) - 1 - running[::-1].index(module)]
            progressed[0] = time.monotonic()

    announcer = _AnnounceImports(boundary)
    sys.meta_path.insert(1, announcer)
    done = threading.Event()

    def watchdog() -> None:
        while not done.wait(0.5):
            with lock:
                silent = time.monotonic() - progressed[0]
                innermost = running[-1] if running else current[0]
            if current[0] and silent > import_timeout:
                sys.stderr.write(
                    f"the warm-up import of {current[0]} started or finished no module for {import_timeout:.0f} s "
                    f"(innermost running: {innermost})\n"
                )
                sys.stderr.flush()
                os._exit(1)

    guard = threading.Thread(target=watchdog, daemon=True)
    guard.start()
    try:
        for name in dependencies:
            if name in sys.modules:
                continue
            with lock:
                current[0] = name
                progressed[0] = time.monotonic()
            importlib.import_module(name)
            if refuser.refusals:
                raise RuntimeError(
                    f"the warm-up import of {name} reached {refuser.refusals[0]}, "
                    "whose import did not finish within the bound"
                )
    finally:
        done.set()
        guard.join()
        sys.meta_path.remove(announcer)
        announcer.unwrap()
        sys.meta_path.remove(refuser)


def main() -> None:
    """Print the modules that do not import on their own, with their errors, as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--path", type=Path, default=None)
    parser.add_argument("--import-timeout", type=float, default=DEFAULT_IMPORT_TIMEOUT_SECONDS)
    parser.add_argument("--bind-timeout", type=float, default=DEFAULT_BIND_TIMEOUT_SECONDS)
    args = parser.parse_args()
    package: str = args.package
    import_timeout: float = args.import_timeout
    bind_timeout: float = args.bind_timeout
    if args.path is not None:
        sys.path.insert(0, str(args.path))
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(f"{package} is not importable in this interpreter")
    src = Path(next(iter(spec.submodule_search_locations))).parent
    names = module_names(src, package)
    dependencies, hung_in_probe = _dependency_modules(names, package, import_timeout, bind_timeout)
    _warm(dependencies, hung_in_probe, import_timeout)
    if any(_in_package(name, package) for name in sys.modules):
        raise RuntimeError(f"a dependency import loaded a {package} module before the per-module imports")

    timed_out_message = "the import of {name} did not finish within " + f"{import_timeout:.0f} s"
    failures: dict[str, str] = {}
    registering: list[str] = []

    def judge(batch: list[str], refused: frozenset[str]) -> None:
        first = _import_each(
            batch, bind=False, import_timeout=import_timeout, bind_timeout=bind_timeout, refused=refused
        )
        batch_registering = [name for name, verdict in first.items() if verdict["status"] == "registers"]
        registering.extend(batch_registering)
        failures.update({name: v["error"] for name, v in first.items() if v["status"] == "error"})
        second = _import_each(
            batch_registering, bind=True, import_timeout=import_timeout, bind_timeout=bind_timeout, refused=refused
        )
        failures.update({name: v["error"] for name, v in second.items() if v["status"] != "ok"})

    # The modules that hung in the probe, inside the package or outside it, are judged first, each
    # in its own child; the ones whose own import does not finish there are refused to every other
    # module's child.
    suspects = [name for name in names if name in hung_in_probe] + sorted(hung_in_probe.difference(names))
    judge(suspects, frozenset())
    hanging = frozenset(name for name in suspects if failures.get(name) == timed_out_message.format(name=name))
    judge([name for name in names if name not in hung_in_probe], hanging)
    print(json.dumps({"checked": len(names), "registering": len(registering), "failures": failures}))


if __name__ == "__main__":
    main()
